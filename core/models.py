"""Модели IHelper.

Ключевые решения:

1. Ученик (Student) — НЕ пользователь. У него нет аккаунта и логина, это только
   запись в БД. Логинятся admin / coordinator / tutor / parent.

2. Coordinator, Tutor, Parent — proxy-модели над User. Отдельных таблиц профилей
   нет: роль хранится в User.role, ФИО в User.full_name, телефон в User.phone.
   Proxy даёт типизированные менеджеры (Tutor.objects — только тьюторы) и позволяет
   ставить FK «на Tutor», физически указывающий в таблицу пользователей.

3. Все оценки — по единой 100-балльной шкале (GRADE_MIN..GRADE_MAX). Единая шкала
   обязательна: итог считается как взвешенная сумма оценок за уроки, ДЗ и тесты,
   и смешивать 5-балльные оценки со 100-балльными тестами нельзя. Чтобы перейти на
   другую шкалу — поменять две константы ниже.

4. Organization — задел на будущее (вторая организация, СДФ). Сейчас работаем как
   одна организация, поле везде nullable и не заполняется. Места, где при появлении
   второй организации нужно будет фильтровать данные, помечены комментарием
   `ORG-SCOPE`.
"""

from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

# Единая шкала оценок для уроков, ДЗ и тестов.
GRADE_MIN = 0
GRADE_MAX = 100


def grade_validators():
    return [MinValueValidator(GRADE_MIN), MaxValueValidator(GRADE_MAX)]


class Organization(models.Model):
    """Задел на будущее: вторая организация (СДФ) с изолированными данными.

    Сейчас не используется — все записи создаются с organization=None.
    Когда организаций станет две: проставить organization пользователям, ученикам,
    группам и предметам, после чего фильтровать queryset'ы во вьюсетах по
    request.user.organization (см. пометки ORG-SCOPE в core/views.py).
    """

    name = models.CharField("название", max_length=120, unique=True)
    is_active = models.BooleanField("активна", default=True)

    class Meta:
        verbose_name = "организация"
        verbose_name_plural = "организации"

    def __str__(self):
        return self.name


class Role(models.TextChoices):
    ADMIN = "admin", "Администратор"
    COORDINATOR = "coordinator", "Координатор"
    TUTOR = "tutor", "Тьютор"
    PARENT = "parent", "Родитель"


class User(AbstractUser):
    """Аккаунт. Ученики сюда НЕ попадают — у них нет логина."""

    role = models.CharField("роль", max_length=20, choices=Role.choices)
    full_name = models.CharField("ФИО", max_length=200, blank=True)
    phone = models.CharField("телефон", max_length=32, blank=True)
    # ORG-SCOPE: организация пользователя.
    organization = models.ForeignKey(
        Organization,
        verbose_name="организация",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
    )
    # Заполняется только для role=parent: дети этого родителя.
    children = models.ManyToManyField(
        "Student", verbose_name="дети", blank=True, related_name="parents"
    )

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"

    def __str__(self):
        return self.full_name or self.username

    @property
    def effective_role(self):
        """Роль строго в нижнем регистре: "admin" | "coordinator" | "tutor" | "parent".

        Суперпользователь, заведённый через createsuperuser, имеет пустое поле role —
        для фронта и JWT-клейма отдаём "admin", чтобы строка всегда была валидной.
        """
        if not self.role and self.is_superuser:
            return Role.ADMIN
        return self.role

    @property
    def is_admin(self):
        return self.role == Role.ADMIN or self.is_superuser

    @property
    def is_coordinator(self):
        return self.role == Role.COORDINATOR

    @property
    def is_tutor(self):
        return self.role == Role.TUTOR

    @property
    def is_parent(self):
        return self.role == Role.PARENT


class RoleManager(UserManager):
    """Менеджер proxy-модели: отдаёт только пользователей своей роли."""

    role = None

    def get_queryset(self):
        return super().get_queryset().filter(role=self.role)

    def create(self, **kwargs):
        kwargs.setdefault("role", self.role)
        return super().create(**kwargs)


class CoordinatorManager(RoleManager):
    role = Role.COORDINATOR


class TutorManager(RoleManager):
    role = Role.TUTOR


class ParentManager(RoleManager):
    role = Role.PARENT


class Coordinator(User):
    """Координатор (role=coordinator). Создаётся Администратором."""

    objects = CoordinatorManager()

    class Meta:
        proxy = True
        verbose_name = "координатор"
        verbose_name_plural = "координаторы"


class Tutor(User):
    """Тьютор (role=tutor). Создаётся Координатором."""

    objects = TutorManager()

    class Meta:
        proxy = True
        verbose_name = "тьютор"
        verbose_name_plural = "тьюторы"


class Parent(User):
    """Родитель (role=parent). Аккаунт создаётся только по приглашению."""

    objects = ParentManager()

    class Meta:
        proxy = True
        verbose_name = "родитель"
        verbose_name_plural = "родители"


class Student(models.Model):
    """Ученик. Не пользователь: логина нет, только профиль и журналы."""

    full_name = models.CharField("ФИО", max_length=200)
    grade = models.CharField("класс", max_length=10)
    language = models.CharField("язык обучения", max_length=50, blank=True)
    svc_category = models.CharField("категория SVC", max_length=100, blank=True)
    phone = models.CharField("телефон", max_length=32, blank=True)
    parent_name = models.CharField("ФИО родителя", max_length=200, blank=True)
    parent_phone = models.CharField("телефон родителя", max_length=32, blank=True)
    parent_email = models.EmailField("email родителя", blank=True)
    is_active = models.BooleanField("активен", default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кем создан",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_students",
    )
    # ORG-SCOPE
    organization = models.ForeignKey(
        Organization,
        verbose_name="организация",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="students",
    )
    created_at = models.DateTimeField("создан", auto_now_add=True)

    class Meta:
        verbose_name = "ученик"
        verbose_name_plural = "ученики"
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.grade})"


class Subject(models.Model):
    """Предмет: математика, английский и т.д. Управляется координатором."""

    name = models.CharField("название", max_length=100, unique=True)
    is_active = models.BooleanField("активен", default=True)
    # ORG-SCOPE
    organization = models.ForeignKey(
        Organization,
        verbose_name="организация",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="subjects",
    )

    class Meta:
        verbose_name = "предмет"
        verbose_name_plural = "предметы"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Group(models.Model):
    """Группа: тьютор + предметы + ученики.

    При сохранении группы и изменении её состава сервисный слой синхронизирует
    журналы (см. core/services.py::sync_group_journals).
    """

    name = models.CharField("название", max_length=100)
    grade = models.CharField("класс", max_length=10, blank=True)
    language = models.CharField("язык обучения", max_length=50, blank=True)
    tutor = models.ForeignKey(
        Tutor,
        verbose_name="тьютор",
        on_delete=models.PROTECT,
        # Не "groups": это имя уже занято M2M django.contrib.auth (группы прав).
        related_name="tutor_groups",
    )
    subjects = models.ManyToManyField(Subject, verbose_name="предметы", related_name="groups")
    students = models.ManyToManyField(
        Student, verbose_name="ученики", blank=True, related_name="groups"
    )
    start_date = models.DateField("дата начала", default=timezone.localdate)
    is_active = models.BooleanField("активна", default=True)
    # ORG-SCOPE
    organization = models.ForeignKey(
        Organization,
        verbose_name="организация",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="groups",
    )
    created_at = models.DateTimeField("создана", auto_now_add=True)

    class Meta:
        verbose_name = "группа"
        verbose_name_plural = "группы"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Journal(models.Model):
    """Журнал: один на пару (ученик, предмет).

    Создаётся автоматически, когда ученик попадает в группу с этим предметом.
    Если у группы два предмета — у каждого ученика будет два журнала.

    При исключении ученика из группы журнал НЕ удаляется (в нём история оценок),
    а помечается is_active=False. Если ученика вернут в группу — журнал
    реактивируется, история сохраняется.
    """

    student = models.ForeignKey(
        Student, verbose_name="ученик", on_delete=models.CASCADE, related_name="journals"
    )
    subject = models.ForeignKey(
        Subject, verbose_name="предмет", on_delete=models.PROTECT, related_name="journals"
    )
    group = models.ForeignKey(
        Group,
        verbose_name="группа",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="journals",
    )
    is_active = models.BooleanField("активен", default=True)
    created_at = models.DateTimeField("создан", auto_now_add=True)

    class Meta:
        verbose_name = "журнал"
        verbose_name_plural = "журналы"
        ordering = ["student__full_name", "subject__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "subject"], name="unique_journal_per_student_subject"
            )
        ]

    def __str__(self):
        return f"Журнал: {self.student.full_name} — {self.subject.name}"

    # --- Итоговая оценка -------------------------------------------------
    # Веса: урок 20% + ДЗ 20% + промежуточный тест 30% + итоговый тест 30%.
    # Входной (диагностический) тест в итог НЕ входит — показывается отдельно.
    WEIGHTS = {"lessons": 20, "homework": 20, "midterm": 30, "final": 30}

    def _lesson_average(self):
        values = [
            r.lesson_grade
            for r in self.lesson_reports.all()
            if r.lesson_grade is not None
        ]
        return sum(values) / len(values) if values else None

    def _homework_average(self):
        values = [
            r.homework_grade
            for r in self.lesson_reports.all()
            if r.homework_grade is not None
        ]
        return sum(values) / len(values) if values else None

    def _test_average(self, test_type):
        """Средний нормализованный балл (0..100) по тестам этого типа."""
        values = [
            result.normalized_score
            for result in self.test_results.all()
            if result.test.test_type == test_type
        ]
        return sum(values) / len(values) if values else None

    def calculate_final_grade(self):
        """Итоговая оценка с разбивкой по составляющим.

        Возвращает:
          components        — по каждой составляющей: балл, вес, вклад в итог
          final_grade       — итог, посчитанный по ИМЕЮЩИМСЯ составляющим с
                              пересчётом весов (журнал в середине года не штрафуется
                              за то, что итогового теста ещё не было)
          final_grade_raw   — итог по полной формуле, где недостающая составляющая
                              считается нулём (честная картина «на сегодня»)
          entry_test        — входной тест: диагностика, в итог НЕ входит
          is_complete       — все ли четыре составляющие уже есть
        """
        scores = {
            "lessons": self._lesson_average(),
            "homework": self._homework_average(),
            "midterm": self._test_average(TestType.MIDTERM),
            "final": self._test_average(TestType.FINAL),
        }
        labels = {
            "lessons": "Оценки за уроки",
            "homework": "Оценки за ДЗ",
            "midterm": "Промежуточный тест",
            "final": "Итоговый тест",
        }

        components = []
        for key, weight in self.WEIGHTS.items():
            score = scores[key]
            components.append(
                {
                    "key": key,
                    "label": labels[key],
                    "score": round(score, 2) if score is not None else None,
                    "weight": weight,
                    "weighted": round(score * weight / 100, 2) if score is not None else None,
                }
            )

        available = {k: v for k, v in scores.items() if v is not None}
        available_weight = sum(self.WEIGHTS[k] for k in available)

        final_grade = (
            round(
                sum(v * self.WEIGHTS[k] for k, v in available.items()) / available_weight,
                2,
            )
            if available_weight
            else None
        )
        final_grade_raw = round(
            sum(v * self.WEIGHTS[k] / 100 for k, v in available.items()), 2
        )

        entry = self._test_average(TestType.ENTRY)
        return {
            "components": components,
            "final_grade": final_grade,
            "final_grade_raw": final_grade_raw,
            "is_complete": available_weight == 100,
            "entry_test": {
                "label": "Входной тест (диагностика, в итог не входит)",
                "score": round(entry, 2) if entry is not None else None,
            },
        }


class ScheduleEntry(models.Model):
    """Занятие в расписании группы. Создаётся координатором.

    Расписание группы = набор ScheduleEntry. Тьютор дату урока НЕ вводит руками —
    LessonReport берёт её отсюда. Изменения (перенос, отмена) фиксируются в
    comment, а не переписыванием даты.
    """

    group = models.ForeignKey(
        Group, verbose_name="группа", on_delete=models.CASCADE, related_name="schedule_entries"
    )
    subject = models.ForeignKey(
        Subject,
        verbose_name="предмет",
        on_delete=models.PROTECT,
        related_name="schedule_entries",
    )
    date = models.DateField("дата")
    time = models.TimeField("время")
    comment = models.TextField("комментарий (изменения, переносы)", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кем создано",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_schedule_entries",
    )

    class Meta:
        verbose_name = "занятие в расписании"
        verbose_name_plural = "расписание"
        ordering = ["date", "time"]

    def __str__(self):
        return f"{self.group.name} — {self.subject.name} — {self.date} {self.time}"


class LessonReport(models.Model):
    """Запись урока в журнале. Создаётся тьютором."""

    journal = models.ForeignKey(
        Journal, verbose_name="журнал", on_delete=models.CASCADE, related_name="lesson_reports"
    )
    schedule_entry = models.ForeignKey(
        ScheduleEntry,
        verbose_name="занятие по расписанию",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lesson_reports",
    )
    # Дата и предмет проставляются из schedule_entry / journal в save(),
    # тьютор их не вводит. Денормализованы, чтобы отчёт за период строился
    # без джойнов и чтобы запись урока пережила удаление занятия из расписания.
    date = models.DateField("дата урока")
    subject = models.ForeignKey(
        Subject, verbose_name="предмет", on_delete=models.PROTECT, related_name="lesson_reports"
    )
    topic = models.CharField("тема урока", max_length=255)
    attended = models.BooleanField("присутствовал", default=True)
    lesson_grade = models.PositiveSmallIntegerField(
        "оценка за урок", null=True, blank=True, validators=grade_validators()
    )
    homework_assigned = models.BooleanField("задано ДЗ", default=False)
    homework_grade = models.PositiveSmallIntegerField(
        "оценка за ДЗ", null=True, blank=True, validators=grade_validators()
    )
    feedback = models.TextField("фидбек по ученику", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кем создан",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_lesson_reports",
    )
    created_at = models.DateTimeField("создан", auto_now_add=True)

    class Meta:
        verbose_name = "запись урока"
        verbose_name_plural = "записи уроков"
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.date} — {self.journal.student.full_name} — {self.topic}"

    def save(self, *args, **kwargs):
        # Предмет всегда равен предмету журнала; дата — из расписания.
        if self.journal_id and not self.subject_id:
            self.subject_id = self.journal.subject_id
        if self.schedule_entry_id and not self.date:
            self.date = self.schedule_entry.date
        super().save(*args, **kwargs)


class TestType(models.TextChoices):
    ENTRY = "entry", "Входной (диагностический)"
    MIDTERM = "midterm", "Промежуточный"
    FINAL = "final", "Итоговый"


class Test(models.Model):
    """Тестирование. Создаётся координатором."""

    name = models.CharField("название", max_length=150)
    test_type = models.CharField("тип", max_length=20, choices=TestType.choices)
    subject = models.ForeignKey(
        Subject, verbose_name="предмет", on_delete=models.PROTECT, related_name="tests"
    )
    group = models.ForeignKey(
        Group,
        verbose_name="группа",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tests",
    )
    date = models.DateField("дата", default=timezone.localdate)
    max_score = models.PositiveSmallIntegerField("максимальный балл", default=GRADE_MAX)

    class Meta:
        verbose_name = "тест"
        verbose_name_plural = "тесты"
        ordering = ["-date"]

    def __str__(self):
        return f"{self.name} ({self.get_test_type_display()})"


class TestResult(models.Model):
    """Результат теста в журнале ученика. Баллы вводит координатор."""

    journal = models.ForeignKey(
        Journal, verbose_name="журнал", on_delete=models.CASCADE, related_name="test_results"
    )
    test = models.ForeignKey(
        Test, verbose_name="тест", on_delete=models.CASCADE, related_name="results"
    )
    score = models.PositiveSmallIntegerField("балл")
    comment = models.TextField("комментарий", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кем внесён",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_test_results",
    )
    created_at = models.DateTimeField("создан", auto_now_add=True)

    class Meta:
        verbose_name = "результат теста"
        verbose_name_plural = "результаты тестов"
        ordering = ["-test__date"]
        constraints = [
            models.UniqueConstraint(
                fields=["journal", "test"], name="unique_result_per_journal_test"
            )
        ]

    def __str__(self):
        return f"{self.journal.student.full_name} — {self.test.name}: {self.score}"

    @property
    def test_type(self):
        return self.test.test_type

    @property
    def normalized_score(self):
        """Балл, приведённый к шкале 0..100 (тест может быть, например, из 50)."""
        if not self.test.max_score:
            return 0
        return self.score / self.test.max_score * GRADE_MAX


class Material(models.Model):
    """База материалов для тьюторов (чтение)."""

    title = models.CharField("название", max_length=200)
    description = models.TextField("описание", blank=True)
    # Имя поля `link` (не `url`) — так его ждёт фронтенд по контракту API.
    link = models.URLField("ссылка", blank=True)
    subject = models.ForeignKey(
        Subject,
        verbose_name="предмет",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="materials",
    )
    grade = models.CharField("класс", max_length=10, blank=True)
    created_at = models.DateTimeField("создан", auto_now_add=True)

    class Meta:
        verbose_name = "материал"
        verbose_name_plural = "материалы"
        ordering = ["title"]

    def __str__(self):
        return self.title


class Invite(models.Model):
    """Одноразовое приглашение для аккаунта с логином (coordinator/tutor/parent).

    Единый механизм для всех трёх login-ролей — токен генерируется, хешируется и
    протухает одинаково, никакого дублирования. Различие только в том, к чему
    приглашение привязано и что делает accept:

      - staff (coordinator/tutor): `user` уже создан (админом/координатором) БЕЗ
        рабочего пароля; accept ставит пароль этому пользователю.
      - parent: `user` пуст, зато задан `student`; accept создаёт (или находит)
        родителя и привязывает к нему ученика.

    Сырой токен в БД не хранится — только SHA-256 хеш (как пароль). Сырой токен
    живёт лишь в ссылке-приглашении.
    """

    role = models.CharField("роль", max_length=20, choices=Role.choices)
    email = models.EmailField("email", blank=True)
    # staff: заполнен сразу; parent: пуст до accept.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="аккаунт",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="invites",
    )
    # только для parent-приглашений.
    student = models.ForeignKey(
        Student,
        verbose_name="ученик",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="parent_invites",
    )
    token_hash = models.CharField("хеш токена", max_length=64, unique=True)
    expires_at = models.DateTimeField("действует до")
    accepted_at = models.DateTimeField("принято", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="кем создано",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_invites",
    )
    created_at = models.DateTimeField("создано", auto_now_add=True)

    class Meta:
        verbose_name = "приглашение"
        verbose_name_plural = "приглашения"
        ordering = ["-created_at"]

    def __str__(self):
        target = self.student.full_name if self.student else (self.email or "—")
        return f"Приглашение [{self.role}] → {target}"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_usable(self):
        return self.accepted_at is None and not self.is_expired
