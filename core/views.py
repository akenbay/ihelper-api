"""API.

Правило по всему файлу: permission_classes решают «можно ли дёргать ручку»,
get_queryset() решает «какие объекты видно». Тьютор ограничен своими группами,
родитель — своими детьми, и это ограничение стоит в queryset, а не в фильтрах
запроса — иначе его можно было бы обойти, запросив чужой объект по id.

ORG-SCOPE: когда появится вторая организация (СДФ), сюда добавляется общий
фильтр `.filter(organization=self.request.user.organization)` — по одной строке
в каждый get_queryset() ниже.
"""

from django.conf import settings
from django.db.models import Prefetch, Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from . import services
from .emails import send_password_reset_email
from .models import (
    Coordinator,
    Group,
    Invite,
    LessonReport,
    Material,
    Role,
    Student,
    Subject,
    Test,
    TestResult,
    Tutor,
    User,
)
from .permissions import (
    IsAdmin,
    IsAdminOrCoordinator,
    IsAdminOrCoordinatorOrReadOnly,
    RolePermission,
)
from .scoping import scoped_journals, scoped_schedule_entries, scoped_students
from .serializers import (
    CoordinatorSerializer,
    GroupSerializer,
    InviteAcceptSerializer,
    JournalDetailSerializer,
    JournalSerializer,
    LessonReportSerializer,
    LoginSerializer,
    MaterialSerializer,
    ParentInviteSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ScheduleEntrySerializer,
    StudentSerializer,
    SubjectSerializer,
    TestResultSerializer,
    TestSerializer,
    TutorSerializer,
    UserSerializer,
)

# --- Аутентификация -----------------------------------------------------


class LoginView(TokenObtainPairView):
    """POST /api/auth/login — выдаёт access/refresh + профиль."""

    serializer_class = LoginSerializer
    throttle_scope = "auth"


class MeView(APIView):
    """GET /api/auth/me — текущий пользователь."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class PasswordResetRequestView(APIView):
    """POST /api/auth/password-reset — запрос ссылки на сброс пароля.

    Всегда отвечает 200 с одинаковым текстом, даже если такого email нет:
    иначе ручка превращается в перебор существующих аккаунтов (user enumeration).
    """

    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            token = services.make_password_reset_token(user)
            link = f"{settings.FRONTEND_URL}/reset-password/{token}"
            send_password_reset_email(email=user.email, link=link)

        return Response(
            {"detail": "Если такой email зарегистрирован, мы отправили на него ссылку."}
        )


class PasswordResetConfirmView(APIView):
    """POST /api/auth/password-reset/confirm — установка нового пароля по токену."""

    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = services.parse_password_reset_token(data["token"])
        if user is None:
            return Response(
                {"detail": "Ссылка недействительна или устарела."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(data["password"])
        user.save(update_fields=["password"])
        return Response({"detail": "Пароль обновлён."})


class InviteInfoView(APIView):
    """GET /api/auth/invite/<token> — проверка ссылки перед показом формы пароля.

    Работает одинаково для любой роли. `student_name` заполнен только у родительских
    приглашений (у сотрудников он null) — форма фронта уже читает email/student_name,
    поле `role` добавлено сверху и обратную совместимость не ломает.
    """

    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def get(self, request, token):
        invite = services.get_usable_invite(token)
        if invite is None:
            return Response(
                {"detail": "Ссылка недействительна или устарела."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "email": invite.email,
                "role": invite.role,
                "student_name": invite.student.full_name if invite.student else None,
                "full_name": invite.user.full_name if invite.user else "",
            }
        )


class InviteAcceptView(APIView):
    """POST /api/auth/invite/accept — приём приглашения для любой роли.

    Родителю создаёт аккаунт, сотруднику (координатор/тьютор) ставит пароль на уже
    заведённый аккаунт. Контракт (тело/ответ) один и тот же — фронт не меняется.
    Переход по ссылке из письма/ручной ссылки = подтверждение владения email.
    """

    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def post(self, request):
        serializer = InviteAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = services.accept_invite(
            raw_token=data["token"],
            password=data["password"],
            full_name=data.get("full_name", ""),
        )
        if user is None:
            return Response(
                {"detail": "Ссылка недействительна или устарела."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"detail": "Аккаунт готов. Теперь вы можете войти.", "username": user.username},
            status=status.HTTP_201_CREATED,
        )


# --- Аккаунты -----------------------------------------------------------


class StaffInviteCreateMixin:
    """Создание сотрудника → аккаунт без пароля + одноразовое приглашение.

    В ответ на создание кладём invite_link, чтобы админ/координатор скопировал
    ссылку и передал её вручную (письмо этим ролям пока не шлём). Пароль сотрудник
    задаёт по ссылке через /api/auth/invite/accept.
    """

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        _invite, raw_token = services.create_staff_invite(user, created_by=request.user)

        data = dict(serializer.data)
        data["invite_link"] = services.invite_link(raw_token)
        headers = self.get_success_headers(serializer.data)
        return Response(data, status=status.HTTP_201_CREATED, headers=headers)


class CoordinatorViewSet(StaffInviteCreateMixin, viewsets.ModelViewSet):
    """/api/coordinators/ — только админ: создаёт и удаляет координаторов.

    POST возвращает поля координатора + invite_link.
    """

    queryset = Coordinator.objects.all().order_by("full_name")
    serializer_class = CoordinatorSerializer
    permission_classes = [IsAdmin]
    search_fields = ["full_name", "username", "email"]


class TutorViewSet(StaffInviteCreateMixin, viewsets.ModelViewSet):
    """/api/tutors/ — координатор (и админ) создаёт и удаляет тьюторов.

    POST возвращает поля тьютора + invite_link.
    """

    queryset = Tutor.objects.all().order_by("full_name")
    serializer_class = TutorSerializer
    permission_classes = [IsAdminOrCoordinator]
    search_fields = ["full_name", "username", "email", "phone"]


# --- Ученики, справочники ----------------------------------------------


class StudentViewSet(viewsets.ModelViewSet):
    """/api/students/

    Координатор — полный CRUD. Тьютор — чтение своих учеников. Родитель — чтение
    своих детей.
    """

    serializer_class = StudentSerializer
    permission_classes = [IsAdminOrCoordinatorOrReadOnly]
    filterset_fields = ["grade", "language", "is_active"]
    search_fields = ["full_name", "parent_name", "parent_email"]

    def get_queryset(self):
        return scoped_students(self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"], permission_classes=[IsAdminOrCoordinator])
    def invite_parent(self, request, pk=None):
        """POST /api/students/{id}/invite_parent/ — выслать приглашение родителю."""
        student = self.get_object()
        try:
            invite, _raw = services.create_parent_invite(
                student=student, created_by=request.user
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            ParentInviteSerializer(invite).data, status=status.HTTP_201_CREATED
        )


class SubjectViewSet(viewsets.ModelViewSet):
    """/api/subjects/ — управляет координатор, читают все."""

    queryset = Subject.objects.all()
    serializer_class = SubjectSerializer
    permission_classes = [IsAdminOrCoordinatorOrReadOnly]
    search_fields = ["name"]


class MaterialViewSet(viewsets.ModelViewSet):
    """/api/materials/ — база материалов. Тьютор читает, координатор наполняет."""

    queryset = Material.objects.select_related("subject")
    serializer_class = MaterialSerializer
    permission_classes = [IsAdminOrCoordinatorOrReadOnly]
    filterset_fields = ["subject", "grade"]
    search_fields = ["title", "description"]

    def get_queryset(self):
        # Родителю база материалов не нужна.
        if self.request.user.is_parent:
            return Material.objects.none()
        return super().get_queryset()


# --- Группы -------------------------------------------------------------


class GroupViewSet(viewsets.ModelViewSet):
    """/api/groups/

    Координатор: создание/удаление групп, смена тьютора, состав учеников.
    Создание группы и любое изменение состава автоматически подтягивает журналы
    (сигнал → services.sync_group_journals).
    """

    serializer_class = GroupSerializer
    permission_classes = [IsAdminOrCoordinatorOrReadOnly]
    filterset_fields = ["grade", "language", "tutor", "is_active"]
    search_fields = ["name"]

    def get_queryset(self):
        user = self.request.user
        qs = Group.objects.select_related("tutor").prefetch_related("subjects", "students")
        if user.is_admin or user.is_coordinator:
            return qs
        if user.is_tutor:
            return qs.filter(tutor=user)
        if user.is_parent:
            return qs.filter(students__in=user.children.all()).distinct()
        return qs.none()

    @action(detail=True, methods=["get"])
    def journals(self, request, pk=None):
        """GET /api/groups/{id}/journals/ — все журналы группы."""
        group = self.get_object()
        journals = scoped_journals(request.user).filter(group=group)
        return Response(JournalSerializer(journals, many=True).data)


# --- Журналы ------------------------------------------------------------


class JournalViewSet(viewsets.ReadOnlyModelViewSet):
    """/api/journals/ — только чтение.

    Журналы не создаются руками: они появляются автоматически вместе с группой.
    Наполняются через /api/lesson-reports/ и /api/test-results/.

    Видимость: админ и координатор — все; тьютор — журналы своих групп;
    родитель — журналы своих детей (и ничего больше).
    """

    serializer_class = JournalSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["student", "subject", "group", "is_active"]

    def get_queryset(self):
        qs = scoped_journals(self.request.user)
        if self.action == "retrieve":
            qs = qs.prefetch_related(
                Prefetch("lesson_reports", queryset=LessonReport.objects.select_related("subject")),
                Prefetch("test_results", queryset=TestResult.objects.select_related("test")),
            )
        return qs

    def get_serializer_class(self):
        return JournalDetailSerializer if self.action == "retrieve" else JournalSerializer

    @action(detail=True, methods=["get"])
    def final_grade(self, request, pk=None):
        """GET /api/journals/{id}/final_grade/ — итог с разбивкой по весам."""
        journal = self.get_object()
        return Response(journal.calculate_final_grade())


class LessonReportViewSet(viewsets.ModelViewSet):
    """/api/lesson-reports/ — записи уроков. Создаёт тьютор."""

    serializer_class = LessonReportSerializer
    permission_classes = [RolePermission]
    allowed_roles = {
        "read": [Role.ADMIN, Role.COORDINATOR, Role.TUTOR, Role.PARENT],
        "write": [Role.TUTOR, Role.ADMIN],
    }
    filterset_fields = ["journal", "subject", "date", "attended"]

    def get_queryset(self):
        # Журналы уже отскоуплены по роли — записи уроков наследуют это же ограничение.
        return LessonReport.objects.filter(
            journal__in=scoped_journals(self.request.user)
        ).select_related("journal__student", "subject")

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class TestViewSet(viewsets.ModelViewSet):
    """/api/tests/ — тесты (входной / промежуточный / итоговый). Заводит координатор."""

    queryset = Test.objects.select_related("subject", "group")
    serializer_class = TestSerializer
    permission_classes = [IsAdminOrCoordinatorOrReadOnly]
    filterset_fields = ["test_type", "subject", "group"]

    def get_queryset(self):
        user = self.request.user
        if user.is_tutor:
            return super().get_queryset().filter(group__tutor=user)
        if user.is_parent:
            return super().get_queryset().filter(group__students__in=user.children.all()).distinct()
        return super().get_queryset()


class TestResultViewSet(viewsets.ModelViewSet):
    """/api/test-results/ — баллы за тестирование вносит координатор."""

    serializer_class = TestResultSerializer
    permission_classes = [IsAdminOrCoordinatorOrReadOnly]
    filterset_fields = ["journal", "test", "test__test_type"]

    def get_queryset(self):
        return TestResult.objects.filter(
            journal__in=scoped_journals(self.request.user)
        ).select_related("test", "journal__student")

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


# --- Расписание ---------------------------------------------------------


class ScheduleEntryViewSet(viewsets.ModelViewSet):
    """/api/schedule/ — расписание составляет координатор.

    Тьютор видит своё расписание и берёт из него дату при создании записи урока.
    Перенос занятия оформляется комментарием, а не правкой даты.
    """

    serializer_class = ScheduleEntrySerializer
    permission_classes = [IsAdminOrCoordinatorOrReadOnly]
    filterset_fields = ["group", "subject", "date", "group__tutor"]

    def get_queryset(self):
        return scoped_schedule_entries(self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


# --- Приглашения родителей ---------------------------------------------


class ParentInviteViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """/api/parent-invite/ — координатор создаёт и переотправляет приглашения родителей.

    Только родительские приглашения (staff-приглашения выдаются при создании аккаунта
    на /api/coordinators/ и /api/tutors/).
    """

    queryset = Invite.objects.filter(role=Role.PARENT).select_related("student")
    serializer_class = ParentInviteSerializer
    permission_classes = [IsAdminOrCoordinator]
    filterset_fields = ["student", "email"]

    def create(self, request, *args, **kwargs):
        student_id = request.data.get("student")
        student = Student.objects.filter(pk=student_id).first()
        if student is None:
            return Response(
                {"student": "Ученик не найден."}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            invite, _raw = services.create_parent_invite(
                student=student, created_by=request.user, email=request.data.get("email")
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            self.get_serializer(invite).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        """POST /api/parent-invite/{id}/resend/ — выслать новую ссылку (старая гасится)."""
        invite = self.get_object()
        if invite.accepted_at:
            return Response(
                {"detail": "Приглашение уже принято."}, status=status.HTTP_400_BAD_REQUEST
            )
        new_invite, _raw = services.create_parent_invite(
            student=invite.student, created_by=request.user, email=invite.email
        )
        return Response(self.get_serializer(new_invite).data)


# --- Отчёты -------------------------------------------------------------


class JournalReportView(APIView):
    """GET /api/reports/journal/ — выгрузка журнала за период.

    Параметры: student, subject, group, date_from, date_to (все опциональны).
    Отдаёт по каждому журналу: уроки за период, тесты и итог с разбивкой.
    Видит ровно то, что разрешено роли (родитель — только своего ребёнка).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        journals = scoped_journals(request.user)

        for param, field in (
            ("student", "student_id"),
            ("subject", "subject_id"),
            ("group", "group_id"),
        ):
            value = request.query_params.get(param)
            if value:
                journals = journals.filter(**{field: value})

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")

        lesson_filter = Q()
        if date_from:
            lesson_filter &= Q(date__gte=date_from)
        if date_to:
            lesson_filter &= Q(date__lte=date_to)

        payload = []
        for journal in journals.prefetch_related("lesson_reports", "test_results__test"):
            lessons = journal.lesson_reports.filter(lesson_filter) if (
                date_from or date_to
            ) else journal.lesson_reports.all()

            payload.append(
                {
                    "journal_id": journal.id,
                    "student": journal.student.full_name,
                    "grade": journal.student.grade,
                    "subject": journal.subject.name,
                    "group": journal.group.name if journal.group else None,
                    "period": {"from": date_from, "to": date_to},
                    "lessons": LessonReportSerializer(lessons, many=True).data,
                    "test_results": TestResultSerializer(
                        journal.test_results.all(), many=True
                    ).data,
                    # Итог считается по ВСЕМ данным журнала, а не только за период:
                    # это оценка за курс, а не за выбранные недели.
                    "summary": journal.calculate_final_grade(),
                }
            )

        return Response({"count": len(payload), "results": payload})
