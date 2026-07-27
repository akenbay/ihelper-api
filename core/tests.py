"""Тесты на то, что легко сломать: авто-журналы, веса итоговой оценки, права.

python manage.py test
"""

from datetime import time, timedelta
from unittest import mock

import requests
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core import emails, serializers as core_serializers, services
from core.urls import router
from core.models import (
    Group,
    Journal,
    LessonReport,
    Material,
    Role,
    ScheduleEntry,
    Student,
    Subject,
    Test,
    TestResult,
    TestType,
    User,
)


def make_user(username, role, **extra):
    user = User.objects.create(username=username, role=role, full_name=username, **extra)
    user.set_password("StrongPass!2024")
    user.save()
    return user


class BaseFixture(APITestCase):
    def setUp(self):
        self.coordinator = make_user("coord", Role.COORDINATOR)
        self.tutor = make_user("tutor1", Role.TUTOR)
        self.other_tutor = make_user("tutor2", Role.TUTOR)

        self.math = Subject.objects.create(name="Математика")
        self.english = Subject.objects.create(name="Английский язык")

        self.alisher = Student.objects.create(
            full_name="Алишер Бекжанов", grade="5", parent_email="parent1@example.kz"
        )
        self.aruzhan = Student.objects.create(
            full_name="Аружан Сериккызы", grade="5", parent_email="parent2@example.kz"
        )

        self.group = Group.objects.create(name="Группа 5А", grade="5", tutor=self.tutor)
        self.group.subjects.set([self.math, self.english])
        self.group.students.set([self.alisher, self.aruzhan])

    def auth(self, user):
        self.client.force_authenticate(user=user)


class AutoJournalTests(BaseFixture):
    def test_journal_per_student_per_subject(self):
        # 2 ученика × 2 предмета = 4 журнала.
        self.assertEqual(Journal.objects.filter(group=self.group).count(), 4)
        self.assertEqual(self.alisher.journals.count(), 2)

    def test_single_subject_group_creates_one_journal_per_student(self):
        group = Group.objects.create(name="Группа 6Б", grade="6", tutor=self.other_tutor)
        student = Student.objects.create(full_name="Диана Аманжолова", grade="6")
        group.subjects.set([self.math])
        group.students.set([student])

        self.assertEqual(student.journals.count(), 1)
        self.assertEqual(student.journals.first().subject, self.math)

    def test_adding_student_later_creates_journals(self):
        new_student = Student.objects.create(full_name="Ерасыл Токтаров", grade="5")
        self.group.students.add(new_student)

        self.assertEqual(new_student.journals.count(), 2)

    def test_adding_subject_later_creates_journals_for_all_students(self):
        physics = Subject.objects.create(name="Физика")
        self.group.subjects.add(physics)

        self.assertEqual(Journal.objects.filter(group=self.group, subject=physics).count(), 2)

    def test_no_duplicate_journals_on_resync(self):
        services.sync_group_journals(self.group)
        services.sync_group_journals(self.group)

        self.assertEqual(Journal.objects.filter(group=self.group).count(), 4)

    def test_removed_student_keeps_journal_but_deactivated(self):
        journal = self.alisher.journals.first()
        self.group.students.remove(self.alisher)

        journal.refresh_from_db()
        self.assertFalse(journal.is_active)
        # История не удалена — журнал остаётся в базе.
        self.assertEqual(self.alisher.journals.count(), 2)

    def test_returning_student_reactivates_same_journal(self):
        journal_id = self.alisher.journals.first().id
        self.group.students.remove(self.alisher)
        self.group.students.add(self.alisher)

        journal = Journal.objects.get(pk=journal_id)
        self.assertTrue(journal.is_active)
        self.assertEqual(self.alisher.journals.count(), 2)


class FinalGradeTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.journal = Journal.objects.get(student=self.alisher, subject=self.math)
        self.entry = ScheduleEntry.objects.create(
            group=self.group, subject=self.math, date=timezone.localdate(), time=time(15, 0)
        )

    def _lesson(self, lesson_grade, homework_grade):
        return LessonReport.objects.create(
            journal=self.journal,
            schedule_entry=self.entry,
            date=self.entry.date,
            subject=self.math,
            topic="Дроби",
            attended=True,
            lesson_grade=lesson_grade,
            homework_assigned=homework_grade is not None,
            homework_grade=homework_grade,
        )

    def _test_result(self, test_type, score, max_score=100):
        test = Test.objects.create(
            name=f"Тест {test_type}",
            test_type=test_type,
            subject=self.math,
            group=self.group,
            max_score=max_score,
        )
        return TestResult.objects.create(journal=self.journal, test=test, score=score)

    def test_weights_20_20_30_30(self):
        self._lesson(80, 60)
        self._lesson(90, 80)  # уроки: 85, ДЗ: 70
        self._test_result(TestType.MIDTERM, 60)
        self._test_result(TestType.FINAL, 90)

        summary = self.journal.calculate_final_grade()

        # 85*0.2 + 70*0.2 + 60*0.3 + 90*0.3 = 17 + 14 + 18 + 27 = 76
        self.assertEqual(summary["final_grade"], 76.0)
        self.assertTrue(summary["is_complete"])

        weights = {c["key"]: c["weight"] for c in summary["components"]}
        self.assertEqual(weights, {"lessons": 20, "homework": 20, "midterm": 30, "final": 30})

    def test_entry_test_is_diagnostic_and_excluded_from_final(self):
        self._lesson(80, 80)
        self._test_result(TestType.MIDTERM, 80)
        self._test_result(TestType.FINAL, 80)
        self._test_result(TestType.ENTRY, 10)  # низкий входной не должен тянуть итог вниз

        summary = self.journal.calculate_final_grade()

        self.assertEqual(summary["final_grade"], 80.0)
        self.assertEqual(summary["entry_test"]["score"], 10.0)

    def test_missing_components_renormalize_weights(self):
        # Итогового теста ещё не было — журнал не штрафуется за это.
        self._lesson(90, 90)
        self._test_result(TestType.MIDTERM, 50)

        summary = self.journal.calculate_final_grade()

        # (90*20 + 90*20 + 50*30) / 70 = 72.86
        self.assertEqual(summary["final_grade"], 72.86)
        self.assertFalse(summary["is_complete"])
        # А «сырой» итог по полной формуле показывает картину на сегодня.
        self.assertEqual(summary["final_grade_raw"], 51.0)

    def test_score_normalized_to_100_scale(self):
        self._test_result(TestType.FINAL, 25, max_score=50)  # 25 из 50 = 50%

        summary = self.journal.calculate_final_grade()
        final = next(c for c in summary["components"] if c["key"] == "final")
        self.assertEqual(final["score"], 50.0)

    def test_empty_journal_has_no_final_grade(self):
        summary = self.journal.calculate_final_grade()

        self.assertIsNone(summary["final_grade"])
        self.assertFalse(summary["is_complete"])


class LessonReportApiTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.journal = Journal.objects.get(student=self.alisher, subject=self.math)
        self.entry = ScheduleEntry.objects.create(
            group=self.group, subject=self.math, date=timezone.localdate(), time=time(15, 0)
        )
        self.url = reverse("lessonreport-list")

    def payload(self, **overrides):
        data = {
            "journal": self.journal.id,
            "schedule_entry": self.entry.id,
            "topic": "Квадратные уравнения",
            "attended": True,
            "lesson_grade": 85,
            "homework_assigned": True,
            "homework_grade": 90,
            "feedback": "Хорошо работал на уроке.",
        }
        data.update(overrides)
        return data

    def test_tutor_creates_report_and_date_comes_from_schedule(self):
        self.auth(self.tutor)
        response = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        # Дату тьютор не вводил — она подставилась из расписания.
        self.assertEqual(response.data["date"], str(self.entry.date))
        self.assertEqual(response.data["subject"], self.math.id)
        self.assertEqual(LessonReport.objects.get().created_by, self.tutor)

    def test_topic_is_required(self):
        self.auth(self.tutor)
        response = self.client.post(self.url, self.payload(topic=""), format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("topic", response.data)

    def test_absent_student_cannot_get_lesson_grade(self):
        self.auth(self.tutor)
        response = self.client.post(
            self.url, self.payload(attended=False, lesson_grade=70), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("lesson_grade", response.data)

    def test_homework_grade_requires_assigned_homework(self):
        self.auth(self.tutor)
        response = self.client.post(
            self.url, self.payload(homework_assigned=False, homework_grade=80), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("homework_grade", response.data)

    def test_coordinator_cannot_create_lesson_report(self):
        self.auth(self.coordinator)
        response = self.client.post(self.url, self.payload(), format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_tutor_cannot_write_into_another_tutors_journal(self):
        self.auth(self.other_tutor)
        response = self.client.post(self.url, self.payload(), format="json")

        # Чужой журнал не виден → в валидации его просто нет.
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PermissionTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.admin = make_user("root", Role.ADMIN, is_staff=True)
        self.parent = make_user("parent1", Role.PARENT)
        self.parent.children.add(self.alisher)

    def test_parent_sees_only_own_child_journals(self):
        self.auth(self.parent)
        response = self.client.get(reverse("journal-list"))

        names = {row["student_name"] for row in response.data["results"]}
        self.assertEqual(names, {"Алишер Бекжанов"})

    def test_parent_cannot_open_another_childs_journal(self):
        other = Journal.objects.filter(student=self.aruzhan).first()
        self.auth(self.parent)

        response = self.client.get(reverse("journal-detail", args=[other.id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_parent_is_read_only(self):
        self.auth(self.parent)

        self.assertEqual(
            self.client.post(reverse("student-list"), {"full_name": "X", "grade": "5"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.post(reverse("subject-list"), {"name": "Химия"}).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_tutor_sees_only_students_from_own_groups(self):
        far_student = Student.objects.create(full_name="Чужой Ученик", grade="7")
        other_group = Group.objects.create(name="Группа 7В", tutor=self.other_tutor)
        other_group.subjects.set([self.math])
        other_group.students.set([far_student])

        self.auth(self.tutor)
        response = self.client.get(reverse("student-list"))

        names = {row["full_name"] for row in response.data["results"]}
        self.assertEqual(names, {"Алишер Бекжанов", "Аружан Сериккызы"})

    def test_tutor_cannot_create_students(self):
        self.auth(self.tutor)
        response = self.client.post(
            reverse("student-list"), {"full_name": "Новый", "grade": "5"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_only_admin_manages_coordinators(self):
        self.auth(self.coordinator)
        self.assertEqual(
            self.client.get(reverse("coordinator-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.auth(self.admin)
        response = self.client.post(
            reverse("coordinator-list"),
            {"full_name": "Новый Координатор", "email": "new.coord@ihelper.kz"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        coord = User.objects.get(email="new.coord@ihelper.kz")
        self.assertEqual(coord.role, Role.COORDINATOR)
        self.assertEqual(coord.username, "new.coord@ihelper.kz")  # username == email
        # Пароль не задаётся напрямую — аккаунт создаётся без рабочего пароля.
        self.assertIn("invite_link", response.data)

    def test_coordinator_creates_tutor(self):
        self.auth(self.coordinator)
        response = self.client.post(
            reverse("tutor-list"),
            {"full_name": "Новый Тьютор", "email": "new.tutor@ihelper.kz"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        tutor = User.objects.get(email="new.tutor@ihelper.kz")
        self.assertEqual(tutor.role, Role.TUTOR)
        self.assertEqual(tutor.username, "new.tutor@ihelper.kz")
        self.assertIn("invite_link", response.data)

    def test_anonymous_is_rejected(self):
        self.assertEqual(
            self.client.get(reverse("journal-list")).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


class ParentInviteTests(BaseFixture):
    def test_invite_creates_parent_account_linked_to_student(self):
        invite, raw_token = services.create_parent_invite(
            student=self.alisher, created_by=self.coordinator
        )

        # Сырой токен в БД не хранится — только хеш.
        self.assertNotEqual(invite.token_hash, raw_token)

        response = self.client.post(
            reverse("invite_accept"),
            {"token": raw_token, "password": "StrongPass!2024", "full_name": "Гульнара Бекжанова"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        parent = User.objects.get(username="parent1@example.kz")
        self.assertEqual(parent.role, Role.PARENT)
        self.assertIn(self.alisher, parent.children.all())
        self.assertTrue(parent.check_password("StrongPass!2024"))

    def test_invite_is_single_use(self):
        _, raw_token = services.create_parent_invite(student=self.alisher)
        payload = {"token": raw_token, "password": "StrongPass!2024"}

        self.client.post(reverse("invite_accept"), payload, format="json")
        second = self.client.post(reverse("invite_accept"), payload, format="json")

        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_expired_invite_is_rejected(self):
        invite, raw_token = services.create_parent_invite(student=self.alisher)
        invite.expires_at = timezone.now() - timedelta(hours=1)
        invite.save(update_fields=["expires_at"])

        response = self.client.post(
            reverse("invite_accept"),
            {"token": raw_token, "password": "StrongPass!2024"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_token_is_rejected(self):
        response = self.client.post(
            reverse("invite_accept"),
            {"token": "not-a-real-token", "password": "StrongPass!2024"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_second_child_links_to_existing_parent_without_touching_password(self):
        _, token1 = services.create_parent_invite(student=self.alisher)
        self.client.post(
            reverse("invite_accept"),
            {"token": token1, "password": "StrongPass!2024"},
            format="json",
        )

        # Тот же email — второй ребёнок.
        self.aruzhan.parent_email = "parent1@example.kz"
        self.aruzhan.save()
        _, token2 = services.create_parent_invite(student=self.aruzhan)
        self.client.post(
            reverse("invite_accept"),
            {"token": token2, "password": "AnotherPass!2024"},
            format="json",
        )

        parent = User.objects.get(username="parent1@example.kz")
        self.assertEqual(parent.children.count(), 2)
        # Владение ссылкой не должно давать смену пароля существующего аккаунта.
        self.assertTrue(parent.check_password("StrongPass!2024"))

    def test_only_coordinator_can_invite(self):
        self.auth(self.tutor)
        response = self.client.post(
            reverse("student-invite-parent", args=[self.alisher.id])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PasswordResetTests(BaseFixture):
    def test_reset_does_not_reveal_whether_email_exists(self):
        known = self.client.post(
            reverse("password_reset"), {"email": "coord@ihelper.kz"}, format="json"
        )
        unknown = self.client.post(
            reverse("password_reset"), {"email": "nobody@example.kz"}, format="json"
        )

        self.assertEqual(known.status_code, status.HTTP_200_OK)
        self.assertEqual(unknown.status_code, status.HTTP_200_OK)
        self.assertEqual(known.data, unknown.data)

    def test_confirm_accepts_token_only_and_sets_password(self):
        # Фронт присылает только {token, password}, без uid.
        token = services.make_password_reset_token(self.coordinator)
        response = self.client.post(
            reverse("password_reset_confirm"),
            {"token": token, "password": "BrandNew!2026"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.coordinator.refresh_from_db()
        self.assertTrue(self.coordinator.check_password("BrandNew!2026"))

    def test_confirm_rejects_bad_token(self):
        response = self.client.post(
            reverse("password_reset_confirm"),
            {"token": "garbage.token", "password": "BrandNew!2026"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AuthTests(BaseFixture):
    def test_login_returns_tokens_and_role(self):
        response = self.client.post(
            reverse("login"),
            {"username": "tutor1", "password": "StrongPass!2024"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["role"], Role.TUTOR)

    def test_login_accepts_email_and_username_in_both_keys(self):
        user = make_user("emailtutor", Role.TUTOR, email="tutor.by.email@ihelper.kz")

        # Фронт кладёт одно и то же значение (тут — email) в оба ключа.
        response = self.client.post(
            reverse("login"),
            {
                "username": "tutor.by.email@ihelper.kz",
                "email": "tutor.by.email@ihelper.kz",
                "password": "StrongPass!2024",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["user"]["id"], user.id)

    def test_me_returns_lowercase_role(self):
        self.auth(self.tutor)
        response = self.client.get(reverse("me"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in ("id", "full_name", "role", "email"):
            self.assertIn(key, response.data)
        self.assertEqual(response.data["role"], "tutor")

    def test_superuser_without_role_reports_admin(self):
        root = User.objects.create(username="root", is_superuser=True, is_staff=True, role="")
        root.set_password("StrongPass!2024")
        root.save()

        self.auth(root)
        response = self.client.get(reverse("me"))
        self.assertEqual(response.data["role"], "admin")


class MaterialTests(BaseFixture):
    def setUp(self):
        super().setUp()
        self.material = Material.objects.create(
            title="Сборник задач по алгебре",
            subject=self.math,
            grade="5",
            link="https://materials.ihelper.kz/math/5/tasks.pdf",
            description="Задачи для отработки.",
        )

    def test_tutor_can_read_materials_with_link_field(self):
        self.auth(self.tutor)
        response = self.client.get(reverse("material-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data["results"][0]
        self.assertIn("link", row)
        self.assertEqual(row["link"], "https://materials.ihelper.kz/math/5/tasks.pdf")

    def test_materials_filterable_by_grade_and_subject(self):
        Material.objects.create(title="Английский, 6 класс", subject=self.english, grade="6")
        self.auth(self.tutor)

        response = self.client.get(reverse("material-list"), {"grade": "5", "subject": self.math.id})
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["title"], "Сборник задач по алгебре")

    def test_coordinator_can_create_material(self):
        self.auth(self.coordinator)
        response = self.client.post(
            reverse("material-list"),
            {"title": "Новый материал", "subject": self.math.id, "grade": "5",
             "link": "https://materials.ihelper.kz/x.pdf"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_tutor_cannot_create_material(self):
        self.auth(self.tutor)
        response = self.client.post(
            reverse("material-list"),
            {"title": "Запрещено", "subject": self.math.id, "grade": "5"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(BREVO_API_KEY="test-key", DEFAULT_FROM_EMAIL="IHelper <noreply@ihelper.kz>")
class BrevoEmailTests(BaseFixture):
    """Отправка через Brevo. requests замокан — реальных писем не шлём."""

    def test_send_email_posts_correct_brevo_payload(self):
        with mock.patch("core.emails.requests.post") as post:
            post.return_value = mock.Mock(status_code=201, raise_for_status=mock.Mock())
            ok = emails.send_email(to="p@example.kz", subject="Тема", body="Текст")

        self.assertTrue(ok)
        url, kwargs = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, emails.BREVO_API_URL)
        self.assertEqual(kwargs["headers"]["api-key"], "test-key")
        payload = kwargs["json"]
        # DEFAULT_FROM_EMAIL в формате «Имя <адрес>» разбирается на name/email.
        self.assertEqual(payload["sender"], {"name": "IHelper", "email": "noreply@ihelper.kz"})
        self.assertEqual(payload["to"], [{"email": "p@example.kz"}])
        self.assertEqual(payload["subject"], "Тема")
        self.assertEqual(payload["textContent"], "Текст")

    def test_invite_email_subject_and_link(self):
        with mock.patch("core.emails.requests.post") as post:
            post.return_value = mock.Mock(raise_for_status=mock.Mock())
            emails.send_parent_invite_email(
                email="p@example.kz", student=self.alisher, link="https://front/invite/abc"
            )

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["subject"], "Приглашение в IHelper")
        self.assertIn("https://front/invite/abc", payload["textContent"])

    def test_report_notification_has_no_grades_only_link(self):
        with mock.patch("core.emails.requests.post") as post:
            post.return_value = mock.Mock(raise_for_status=mock.Mock())
            emails.send_report_notification_email(email="p@example.kz", student=self.alisher)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["subject"], "Новый отчёт по успеваемости")
        self.assertIn("/login", payload["textContent"])
        # Data-minimization: ни оценок, ни фидбека в теле.
        self.assertNotIn("оценка", payload["textContent"].lower())

    def test_brevo_failure_does_not_raise_and_returns_false(self):
        with mock.patch("core.emails.requests.post", side_effect=requests.RequestException("boom")):
            ok = emails.send_email(to="p@example.kz", subject="Тема", body="Текст")
        self.assertFalse(ok)

    def test_invite_creation_survives_email_failure(self):
        # Требование: письмо упало → приглашение всё равно создано и токен возвращён.
        with mock.patch(
            "core.emails.requests.post", side_effect=requests.RequestException("boom")
        ):
            invite, raw_token = services.create_parent_invite(
                student=self.alisher, created_by=self.coordinator
            )

        self.assertTrue(raw_token)
        self.assertTrue(services.Invite.objects.filter(pk=invite.pk).exists())
        self.assertTrue(invite.is_usable)


class StaffInviteTests(BaseFixture):
    """Единый invite-механизм для координаторов и тьюторов.

    Создание аккаунта отдаёт invite_link; переход по нему + {token, password}
    даёт рабочий логин с правильной ролью — тот же контракт, что у родителей.
    """

    def setUp(self):
        super().setUp()
        self.admin = make_user("root", Role.ADMIN, is_staff=True)

    @staticmethod
    def _token_from_link(link):
        return link.rsplit("/", 1)[-1]

    def _accept_and_login(self, invite_link, username, password, expected_role):
        token = self._token_from_link(invite_link)

        # GET проверки ссылки работает одинаково для любой роли.
        info = self.client.get(reverse("invite_info", args=[token]))
        self.assertEqual(info.status_code, status.HTTP_200_OK, info.data)
        self.assertEqual(info.data["role"], expected_role)

        accept = self.client.post(
            reverse("invite_accept"),
            {"token": token, "password": password},
            format="json",
        )
        self.assertEqual(accept.status_code, status.HTTP_201_CREATED, accept.data)

        # Рабочий логин с нужной ролью.
        login = self.client.post(
            reverse("login"),
            {"username": username, "password": password},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK, login.data)
        self.assertEqual(login.data["user"]["role"], expected_role)
        return token

    def test_admin_invites_coordinator_end_to_end(self):
        self.auth(self.admin)
        response = self.client.post(
            reverse("coordinator-list"),
            {"full_name": "Новый Координатор", "email": "c@ihelper.kz"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("invite_link", response.data)

        # Аккаунт создан по email (username == email), без рабочего пароля.
        user = User.objects.get(email="c@ihelper.kz")
        self.assertEqual(user.username, "c@ihelper.kz")
        self.assertFalse(user.has_usable_password())

        self.client.credentials()  # приём приглашения — анонимно
        self._accept_and_login(
            response.data["invite_link"], "c@ihelper.kz", "CoordPass!2026", Role.COORDINATOR
        )

    def test_coordinator_invites_tutor_end_to_end(self):
        self.auth(self.coordinator)
        response = self.client.post(
            reverse("tutor-list"),
            {"full_name": "Новый Тьютор", "email": "t@ihelper.kz"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("invite_link", response.data)

        self.client.credentials()
        token = self._accept_and_login(
            response.data["invite_link"], "t@ihelper.kz", "TutorPass!2026", Role.TUTOR
        )

        # Одноразовость: повторный приём той же ссылки отклоняется.
        second = self.client.post(
            reverse("invite_accept"),
            {"token": token, "password": "TutorPass!2026"},
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_invite_link_points_to_shared_invite_endpoint(self):
        self.auth(self.admin)
        response = self.client.post(
            reverse("coordinator-list"),
            {"full_name": "X", "email": "coord.x@ihelper.kz"},
        )
        # Та же форма ссылки, что и у родителей: {FRONTEND_URL}/invite/<token>.
        self.assertIn("/invite/", response.data["invite_link"])

    def _create_coordinator(self, email):
        self.auth(self.admin)
        response = self.client.post(
            reverse("coordinator-list"),
            {"full_name": "Пере Выпуск", "email": email},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def test_reinvite_regenerates_link_and_invalidates_old_token(self):
        created = self._create_coordinator("coord.re@ihelper.kz")
        old_token = self._token_from_link(created["invite_link"])

        # Перевыпуск ссылки (аккаунт ещё не активирован).
        reinvite = self.client.post(reverse("coordinator-reinvite", args=[created["id"]]))
        self.assertEqual(reinvite.status_code, status.HTTP_200_OK, reinvite.data)
        self.assertIn("invite_link", reinvite.data)
        new_token = self._token_from_link(reinvite.data["invite_link"])
        self.assertNotEqual(new_token, old_token)

        # Старый токен больше не работает.
        self.client.credentials()
        old_info = self.client.get(reverse("invite_info", args=[old_token]))
        self.assertEqual(old_info.status_code, status.HTTP_400_BAD_REQUEST)

        # Новый принимается, логин работает, роль верна.
        self._accept_and_login(
            reinvite.data["invite_link"], "coord.re@ihelper.kz", "CoordPass!2026", Role.COORDINATOR
        )

    def test_reinvite_rejected_once_account_active(self):
        created = self._create_coordinator("coord.act@ihelper.kz")
        token = self._token_from_link(created["invite_link"])

        # Активируем аккаунт (принимаем исходное приглашение).
        self.client.credentials()
        accepted = self.client.post(
            reverse("invite_accept"),
            {"token": token, "password": "CoordPass!2026"},
            format="json",
        )
        self.assertEqual(accepted.status_code, status.HTTP_201_CREATED, accepted.data)

        # Теперь перевыпуск запрещён — иначе это был бы сброс пароля активного аккаунта.
        self.auth(self.admin)
        rejected = self.client.post(reverse("coordinator-reinvite", args=[created["id"]]))
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)

    def test_coordinator_can_reinvite_tutor(self):
        self.auth(self.coordinator)
        created = self.client.post(
            reverse("tutor-list"),
            {"full_name": "Тьютор", "email": "tr@ihelper.kz"},
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

        reinvite = self.client.post(reverse("tutor-reinvite", args=[created.data["id"]]))
        self.assertEqual(reinvite.status_code, status.HTTP_200_OK, reinvite.data)
        self.assertIn("invite_link", reinvite.data)


class StaffEmailAsUsernameTests(BaseFixture):
    """Контракт создания сотрудника: username берётся из email, username не шлётся."""

    def setUp(self):
        super().setUp()
        self.admin = make_user("root", Role.ADMIN, is_staff=True)
        self.auth(self.admin)

    def test_create_coordinator_uses_email_as_username(self):
        response = self.client.post(
            reverse("coordinator-list"),
            {"full_name": "Айгерим Ниязова", "email": "a.niyazova@ihelper.kz",
             "phone": "+7 701 000 00 00"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(email="a.niyazova@ihelper.kz")
        self.assertEqual(user.username, "a.niyazova@ihelper.kz")  # ровно email
        self.assertEqual(user.phone, "+7 701 000 00 00")
        self.assertEqual(response.data["username"], "a.niyazova@ihelper.kz")

    def test_username_in_payload_is_ignored(self):
        # username не входит в контракт — присланный игнорируется, берётся email.
        response = self.client.post(
            reverse("coordinator-list"),
            {"username": "ignored", "full_name": "Кто-то", "email": "real@ihelper.kz"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertFalse(User.objects.filter(username="ignored").exists())
        self.assertEqual(User.objects.get(email="real@ihelper.kz").username, "real@ihelper.kz")

    def test_create_without_email_is_rejected(self):
        response = self.client.post(
            reverse("coordinator-list"), {"full_name": "Без Почты"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)
        self.assertEqual(response.data["email"][0], "Email обязателен.")

    def test_duplicate_email_across_roles_is_rejected(self):
        # Тьютор с этим email уже есть → координатора с тем же email не создать.
        self.client.post(
            reverse("tutor-list"),
            {"full_name": "Тьютор", "email": "shared@ihelper.kz"},
        )
        response = self.client.post(
            reverse("coordinator-list"),
            {"full_name": "Координатор", "email": "shared@ihelper.kz"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["email"][0], "Пользователь с таким email уже существует."
        )

    def test_duplicate_email_case_insensitive(self):
        self.client.post(
            reverse("coordinator-list"),
            {"full_name": "Первый", "email": "case@ihelper.kz"},
        )
        response = self.client.post(
            reverse("tutor-list"),
            {"full_name": "Второй", "email": "CASE@ihelper.kz"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_email_update_keeps_username_in_sync(self):
        created = self.client.post(
            reverse("coordinator-list"),
            {"full_name": "Смена Почты", "email": "old@ihelper.kz"},
        )
        coord_id = created.data["id"]

        patched = self.client.patch(
            reverse("coordinator-detail", args=[coord_id]),
            {"email": "new@ihelper.kz"},
            format="json",
        )
        self.assertEqual(patched.status_code, status.HTTP_200_OK, patched.data)

        user = User.objects.get(pk=coord_id)
        self.assertEqual(user.email, "new@ihelper.kz")
        self.assertEqual(user.username, "new@ihelper.kz")  # username следует за email


class EmailFallbackTests(BaseFixture):
    @override_settings(BREVO_API_KEY="")
    def test_without_key_falls_back_to_django_backend(self):
        # Без ключа Brevo не вызывается, letter уходит через Django (console в деве).
        with mock.patch("core.emails.requests.post") as post, mock.patch(
            "core.emails.send_mail"
        ) as send_mail:
            ok = emails.send_email(to="p@example.kz", subject="Тема", body="Текст")

        self.assertTrue(ok)
        post.assert_not_called()
        send_mail.assert_called_once()


class SerializerConfigTests(APITestCase):
    """Ловит класс «поле declared на сериализаторе, но нет в Meta.fields».

    DRF строит карту полей лениво — при первом обращении к .fields (т.е. при первом
    рендере ответа). На пустой БД GET списка может вернуть 200 и НЕ тронуть .fields,
    поэтому баг всплывает только когда в таблице есть строки. Здесь мы принудительно
    строим карту полей у каждого ModelSerializer из core.serializers — ошибка
    вылезает детерминированно, не завися от данных.
    """

    def test_all_model_serializer_field_maps_build(self):
        import inspect

        from rest_framework import serializers as drf

        failures = []
        for name, cls in inspect.getmembers(core_serializers, inspect.isclass):
            if cls.__module__ != core_serializers.__name__:
                continue
            if not issubclass(cls, drf.ModelSerializer):
                continue
            try:
                cls().fields  # noqa: B018 — обращение к .fields запускает get_field_names()
            except Exception as exc:  # AssertionError и т.п. из построения карты полей
                failures.append(f"{name}: {exc}")

        self.assertEqual(
            failures, [], "Некорректная конфигурация Meta.fields:\n" + "\n".join(failures)
        )


class ListEndpointSmokeTests(BaseFixture):
    """Каждый list-эндпоинт из роутера отвечает не-500 для админа.

    Параметризовано по router.registry — новые вьюсеты попадают под проверку
    автоматически, руками перечислять не нужно. Часть эндпоинтов на фикстуре
    BaseFixture уже с данными (координаторы, тьюторы, ученики, группы, журналы),
    так что для них сериализация реально прогоняется на строках; остальной класс
    ошибок ловит SerializerConfigTests.
    """

    def setUp(self):
        super().setUp()
        self.admin = make_user("root@ihelper.kz", Role.ADMIN, is_staff=True)

    def test_all_list_endpoints_do_not_500(self):
        self.auth(self.admin)
        checked = []
        for _prefix, _viewset, basename in router.registry:
            url = reverse(f"{basename}-list")
            response = self.client.get(url)
            self.assertLess(
                response.status_code, 500, f"{basename}-list вернул {response.status_code}"
            )
            checked.append(basename)

        # Санити: роутер действительно обошли и /api/tests/ (регресс из этой задачи) в нём.
        self.assertIn("test", checked)
