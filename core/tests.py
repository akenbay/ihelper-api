"""Тесты на то, что легко сломать: авто-журналы, веса итоговой оценки, права.

python manage.py test
"""

from datetime import time, timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core import services
from core.models import (
    Group,
    Journal,
    LessonReport,
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
            {
                "username": "new.coord",
                "full_name": "Новый Координатор",
                "password": "StrongPass!2024",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(User.objects.get(username="new.coord").role, Role.COORDINATOR)

    def test_coordinator_creates_tutor(self):
        self.auth(self.coordinator)
        response = self.client.post(
            reverse("tutor-list"),
            {"username": "new.tutor", "full_name": "Новый Тьютор", "password": "StrongPass!2024"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(User.objects.get(username="new.tutor").role, Role.TUTOR)

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
