"""Сериализаторы + валидация."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import (
    Coordinator,
    Group,
    Invite,
    Journal,
    LessonReport,
    Material,
    Role,
    ScheduleEntry,
    Student,
    Subject,
    Test,
    TestResult,
    Tutor,
    User,
)
from .scoping import scoped_journals, scoped_schedule_entries

# --- Auth ---------------------------------------------------------------


class LoginSerializer(TokenObtainPairSerializer):
    """JWT-логин: принимает username ИЛИ email, к токенам добавляет профиль."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Клейм role строго в нижнем регистре — фронт читает его как запасной вариант.
        token["role"] = user.effective_role
        return token

    def validate(self, attrs):
        # Фронт кладёт одно и то же значение в username и email. Принимаем и логин,
        # и email: находим пользователя по username ИЛИ email и подставляем его
        # реальный username, чтобы SimpleJWT смог аутентифицировать по USERNAME_FIELD.
        credential = attrs.get(self.username_field)
        if credential:
            match = (
                User.objects.filter(
                    Q(username__iexact=credential) | Q(email__iexact=credential)
                )
                .order_by("id")
                .first()
            )
            if match is not None:
                attrs[self.username_field] = match.get_username()

        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class UserSerializer(serializers.ModelSerializer):
    # role отдаём через effective_role: суперюзер без явной роли получает "admin".
    role = serializers.CharField(source="effective_role", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "email", "full_name", "phone", "role"]
        read_only_fields = fields


class PasswordField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("write_only", True)
        kwargs.setdefault("style", {"input_type": "password"})
        super().__init__(**kwargs)

    def run_validation(self, data=serializers.empty):
        value = super().run_validation(data)
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class StaffAccountSerializer(serializers.ModelSerializer):
    """Аккаунты, создаваемые «сверху» (координатор, тьютор).

    Публичной регистрации нет. Пароль здесь НЕ задаётся: аккаунт создаётся без
    рабочего пароля, а вьюсет в ответ на создание отдаёт invite_link — сотрудник
    задаёт пароль сам по ссылке-приглашению (тот же механизм, что у родителей).
    """

    role_value = None

    class Meta:
        model = User
        fields = ["id", "username", "email", "full_name", "phone", "is_active"]

    def create(self, validated_data):
        user = User(**validated_data, role=self.role_value)
        # Логин без пароля невозможен, пока сотрудник не примет приглашение.
        user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        return instance


class CoordinatorSerializer(StaffAccountSerializer):
    role_value = Role.COORDINATOR

    class Meta(StaffAccountSerializer.Meta):
        model = Coordinator


class TutorSerializer(StaffAccountSerializer):
    role_value = Role.TUTOR

    class Meta(StaffAccountSerializer.Meta):
        model = Tutor


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    # Токен самодостаточный (внутри закодирован uid) — фронт присылает только его.
    token = serializers.CharField()
    password = PasswordField()


class InviteAcceptSerializer(serializers.Serializer):
    """Родитель переходит по ссылке и задаёт пароль."""

    token = serializers.CharField()
    password = PasswordField()
    full_name = serializers.CharField(required=False, allow_blank=True, max_length=200)


# --- Справочники --------------------------------------------------------


class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ["id", "name", "is_active"]


class MaterialSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)

    class Meta:
        model = Material
        fields = ["id", "title", "description", "link", "subject", "subject_name", "grade"]


# --- Ученики ------------------------------------------------------------


class StudentSerializer(serializers.ModelSerializer):
    has_parent_account = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = [
            "id",
            "full_name",
            "grade",
            "language",
            "svc_category",
            "phone",
            "parent_name",
            "parent_phone",
            "parent_email",
            "is_active",
            "has_parent_account",
            "created_by",
            "created_at",
        ]
        read_only_fields = ["created_by", "created_at"]

    def get_has_parent_account(self, obj):
        return obj.parents.exists()


# --- Группы -------------------------------------------------------------


class GroupSerializer(serializers.ModelSerializer):
    tutor_name = serializers.CharField(source="tutor.full_name", read_only=True)
    subject_names = serializers.SerializerMethodField()
    students_count = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = [
            "id",
            "name",
            "grade",
            "language",
            "tutor",
            "tutor_name",
            "subjects",
            "subject_names",
            "students",
            "students_count",
            "start_date",
            "is_active",
        ]

    def get_subject_names(self, obj):
        return [s.name for s in obj.subjects.all()]

    def get_students_count(self, obj):
        return obj.students.count()

    def validate_subjects(self, value):
        if not value:
            raise serializers.ValidationError("Укажите хотя бы один предмет.")
        return value

    # Журналы досоздаются сигналом на m2m (students/subjects) — см. core/signals.py.
    # Здесь ничего дополнительно делать не нужно, дублировать логику нельзя.


# --- Журнал -------------------------------------------------------------


class ScopedRelationsMixin:
    """Сужает queryset у FK-полей до того, что видно текущей роли.

    Без этого проверка прав была бы только на уровне метода: тьютор, которому
    «можно создавать lesson report», смог бы указать journal чужого ученика и
    поставить оценку в чужой журнал. Ссылаться можно только на своё.
    """

    scoped_fields = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return
        for field_name, scope_fn in self.scoped_fields.items():
            if field_name in self.fields:
                self.fields[field_name].queryset = scope_fn(request.user)


class LessonReportSerializer(ScopedRelationsMixin, serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    student_name = serializers.CharField(source="journal.student.full_name", read_only=True)

    scoped_fields = {
        "journal": scoped_journals,
        "schedule_entry": scoped_schedule_entries,
    }

    class Meta:
        model = LessonReport
        fields = [
            "id",
            "journal",
            "schedule_entry",
            "date",
            "subject",
            "subject_name",
            "student_name",
            "topic",
            "attended",
            "lesson_grade",
            "homework_assigned",
            "homework_grade",
            "feedback",
            "created_by",
            "created_at",
        ]
        # Дату тьютор не вводит: она берётся из расписания (schedule_entry).
        # Предмет — из журнала.
        read_only_fields = ["date", "subject", "created_by", "created_at"]

    def validate_topic(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Тема урока обязательна.")
        return value

    def validate(self, attrs):
        journal = attrs.get("journal") or getattr(self.instance, "journal", None)
        entry = attrs.get("schedule_entry") or getattr(self.instance, "schedule_entry", None)

        if entry is None:
            raise serializers.ValidationError(
                {"schedule_entry": "Укажите занятие из расписания — дата урока берётся оттуда."}
            )

        if journal and entry:
            if entry.group_id != journal.group_id:
                raise serializers.ValidationError(
                    {"schedule_entry": "Это занятие относится к другой группе."}
                )
            if entry.subject_id != journal.subject_id:
                raise serializers.ValidationError(
                    {"schedule_entry": "Предмет занятия не совпадает с предметом журнала."}
                )

        attended = attrs.get("attended", getattr(self.instance, "attended", True))
        lesson_grade = attrs.get(
            "lesson_grade", getattr(self.instance, "lesson_grade", None)
        )
        homework_assigned = attrs.get(
            "homework_assigned", getattr(self.instance, "homework_assigned", False)
        )
        homework_grade = attrs.get(
            "homework_grade", getattr(self.instance, "homework_grade", None)
        )

        if not attended and lesson_grade is not None:
            raise serializers.ValidationError(
                {"lesson_grade": "Ученик отсутствовал — оценку за урок ставить нельзя."}
            )
        if not homework_assigned and homework_grade is not None:
            raise serializers.ValidationError(
                {"homework_grade": "ДЗ не задавалось — оценку за ДЗ ставить нельзя."}
            )
        return attrs


class TestSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    test_type_display = serializers.CharField(source="get_test_type_display", read_only=True)

    class Meta:
        model = Test
        fields = [
            "id",
            "name",
            "test_type",
            "test_type_display",
            "subject",
            "group",
            "date",
            "max_score",
        ]


class TestResultSerializer(ScopedRelationsMixin, serializers.ModelSerializer):
    scoped_fields = {"journal": scoped_journals}

    test_type = serializers.CharField(source="test.test_type", read_only=True)
    test_name = serializers.CharField(source="test.name", read_only=True)
    student_name = serializers.CharField(source="journal.student.full_name", read_only=True)
    max_score = serializers.IntegerField(source="test.max_score", read_only=True)

    class Meta:
        model = TestResult
        fields = [
            "id",
            "journal",
            "test",
            "test_name",
            "test_type",
            "student_name",
            "score",
            "max_score",
            "comment",
            "created_by",
            "created_at",
        ]
        read_only_fields = ["created_by", "created_at"]

    def validate(self, attrs):
        journal = attrs.get("journal") or getattr(self.instance, "journal", None)
        test = attrs.get("test") or getattr(self.instance, "test", None)
        score = attrs.get("score", getattr(self.instance, "score", None))

        if journal and test and test.subject_id != journal.subject_id:
            raise serializers.ValidationError(
                {"test": "Предмет теста не совпадает с предметом журнала."}
            )
        if test and score is not None and score > test.max_score:
            raise serializers.ValidationError(
                {"score": f"Балл не может превышать максимальный ({test.max_score})."}
            )
        return attrs


class JournalSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.full_name", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    group_name = serializers.CharField(source="group.name", read_only=True, default=None)
    summary = serializers.SerializerMethodField()

    class Meta:
        model = Journal
        fields = [
            "id",
            "student",
            "student_name",
            "subject",
            "subject_name",
            "group",
            "group_name",
            "is_active",
            "summary",
        ]
        read_only_fields = fields

    def get_summary(self, obj):
        return obj.calculate_final_grade()


class JournalDetailSerializer(JournalSerializer):
    """Полный журнал: уроки + тесты + итог."""

    lesson_reports = LessonReportSerializer(many=True, read_only=True)
    test_results = TestResultSerializer(many=True, read_only=True)

    class Meta(JournalSerializer.Meta):
        fields = JournalSerializer.Meta.fields + ["lesson_reports", "test_results"]
        read_only_fields = fields


# --- Расписание ---------------------------------------------------------


class ScheduleEntrySerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    tutor_name = serializers.CharField(source="group.tutor.full_name", read_only=True)

    class Meta:
        model = ScheduleEntry
        fields = [
            "id",
            "group",
            "group_name",
            "tutor_name",
            "subject",
            "subject_name",
            "date",
            "time",
            "comment",
            "created_by",
        ]
        read_only_fields = ["created_by"]

    def validate(self, attrs):
        group = attrs.get("group") or getattr(self.instance, "group", None)
        subject = attrs.get("subject") or getattr(self.instance, "subject", None)
        if group and subject and not group.subjects.filter(pk=subject.pk).exists():
            raise serializers.ValidationError(
                {"subject": "У этой группы нет такого предмета."}
            )
        return attrs


# --- Приглашения --------------------------------------------------------


class ParentInviteSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.full_name", read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Invite
        fields = [
            "id",
            "student",
            "student_name",
            "email",
            "status",
            "expires_at",
            "accepted_at",
            "created_at",
        ]
        # Токен наружу не отдаём никогда — он только в письме.
        read_only_fields = ["email", "expires_at", "accepted_at", "created_at"]

    def get_status(self, obj):
        if obj.accepted_at:
            return "accepted"
        return "expired" if obj.is_expired else "pending"
