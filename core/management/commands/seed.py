"""python manage.py seed — демо-данные для разработки и демонстрации.

Идемпотентна: повторный запуск не плодит дубли (всё через get_or_create).
Пароль у всех демо-аккаунтов один — ihelper2024. Только для дев-окружения.
"""

import random
from datetime import time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

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

DEMO_PASSWORD = "ihelper2024"

TOPICS = {
    "Математика": [
        "Линейные уравнения",
        "Дроби и проценты",
        "Квадратные уравнения",
        "Геометрия: треугольники",
        "Системы уравнений",
        "Функции и графики",
    ],
    "Английский язык": [
        "Present Simple и Present Continuous",
        "Лексика: семья и дом",
        "Past Simple: неправильные глаголы",
        "Аудирование и разговорная практика",
        "Модальные глаголы",
        "Чтение: работа с текстом",
    ],
}

FEEDBACKS = [
    "Хорошо работает на уроке, задаёт вопросы по теме.",
    "Тема усвоена частично — нужно повторить дома.",
    "Отличный прогресс по сравнению с прошлым занятием.",
    "Невнимателен на уроке, часто отвлекается.",
    "Домашнее задание выполнено аккуратно и полностью.",
    "Требуется дополнительная отработка практических заданий.",
    "Активно участвует в обсуждении, уверенно отвечает.",
]


class Command(BaseCommand):
    help = "Создаёт демо-данные IHelper (казахские имена, русские подписи)."

    @transaction.atomic
    def handle(self, *args, **options):
        random.seed(42)  # воспроизводимые демо-данные

        self.stdout.write("Создаю демо-данные…")

        admin = self._user(
            "admin", "Админ Администраторов", Role.ADMIN, is_staff=True, is_superuser=True
        )

        coordinators = [
            self._user("a.nurpeisova", "Айгерим Нурпеисова", Role.COORDINATOR, phone="+7 701 111 22 33"),
            self._user("d.sagyndyk", "Данияр Сагындык", Role.COORDINATOR, phone="+7 701 222 33 44"),
        ]

        tutors = [
            self._user("m.abenov", "Мадияр Абенов", Role.TUTOR, phone="+7 705 333 44 55"),
            self._user("z.karimova", "Зарина Каримова", Role.TUTOR, phone="+7 705 444 55 66"),
            self._user("t.zhaksylyk", "Тимур Жаксылык", Role.TUTOR, phone="+7 705 555 66 77"),
        ]

        math, _ = Subject.objects.get_or_create(name="Математика")
        english, _ = Subject.objects.get_or_create(name="Английский язык")

        students_data = [
            ("Алишер Бекжанов", "5", "русский", "SVC-1: малообеспеченная семья",
             "Гульнара Бекжанова", "+7 707 100 10 01", "gulnara.bekzhanova@example.kz"),
            ("Аружан Сериккызы", "5", "русский", "SVC-2: многодетная семья",
             "Серик Мураталиев", "+7 707 100 10 02", "serik.muratalyev@example.kz"),
            ("Ерасыл Токтаров", "5", "казахский", "SVC-1: малообеспеченная семья",
             "Асель Токтарова", "+7 707 100 10 03", "asel.toktarova@example.kz"),
            ("Диана Аманжолова", "6", "русский", "SVC-3: опекунство",
             "Марат Аманжолов", "+7 707 100 10 04", "marat.amanzholov@example.kz"),
            ("Нурислам Кайратулы", "6", "казахский", "SVC-2: многодетная семья",
             "Айнур Кайратова", "+7 707 100 10 05", "ainur.kairatova@example.kz"),
        ]

        students = []
        for full_name, grade, language, svc, p_name, p_phone, p_email in students_data:
            student, _ = Student.objects.get_or_create(
                full_name=full_name,
                defaults={
                    "grade": grade,
                    "language": language,
                    "svc_category": svc,
                    "phone": "+7 777 000 00 00",
                    "parent_name": p_name,
                    "parent_phone": p_phone,
                    "parent_email": p_email,
                    "created_by": coordinators[0],
                },
            )
            students.append(student)

        # «Группа 5А» — математика + английский → по 2 журнала на ученика.
        group_5a = self._group("Группа 5А", "5", "русский", tutors[0], [math, english], students[:3])
        # «Группа 6Б» — только математика → по 1 журналу на ученика.
        group_6b = self._group("Группа 6Б", "6", "казахский", tutors[1], [math], students[3:])

        self.stdout.write(f"  журналов создано: {Journal.objects.count()}")

        # Расписание: по 6 занятий на каждый предмет группы, раз в неделю.
        for group in (group_5a, group_6b):
            self._schedule(group)

        # Тесты: входной, промежуточный, итоговый на каждый предмет каждой группы.
        for group in (group_5a, group_6b):
            for subject in group.subjects.all():
                self._tests_and_results(group, subject, coordinators[0])

        # Записи уроков по расписанию.
        self._lesson_reports(group_5a, tutors[0])
        self._lesson_reports(group_6b, tutors[1])

        # Один родитель уже с аккаунтом (остальные — по приглашению).
        parent = self._user(
            students[0].parent_email,
            students[0].parent_name,
            Role.PARENT,
            phone=students[0].parent_phone,
            email=students[0].parent_email,
        )
        parent.children.add(students[0])

        materials = [
            ("Сборник задач по алгебре, 5 класс", "Задания для отработки уравнений.",
             math, "5", "https://materials.ihelper.kz/math/5/algebra-tasks.pdf"),
            ("Рабочая тетрадь: Present Simple", "Упражнения на времена глагола.",
             english, "5", "https://materials.ihelper.kz/eng/5/present-simple.pdf"),
            ("Карточки: неправильные глаголы", "Набор карточек для запоминания.",
             english, "5", "https://materials.ihelper.kz/eng/5/irregular-verbs.pdf"),
            ("Диагностические тесты по математике", "Материалы для входного тестирования.",
             math, "6", "https://materials.ihelper.kz/math/6/diagnostics.pdf"),
            ("Геометрия: треугольники, 6 класс", "Теория и задачи по треугольникам.",
             math, "6", "https://materials.ihelper.kz/math/6/triangles.pdf"),
        ]
        for title, desc, subject, grade, link in materials:
            Material.objects.get_or_create(
                title=title,
                defaults={"description": desc, "subject": subject, "grade": grade, "link": link},
            )

        self._report(admin, coordinators, tutors, parent)

    # --- helpers --------------------------------------------------------

    def _user(self, handle, full_name, role, **extra):
        # username == email — как у аккаунтов, создаваемых через API. `handle` —
        # локальная часть логина (или уже готовый email для родителя).
        email = extra.pop("email", f"{handle}@ihelper.kz")
        user, created = User.objects.get_or_create(
            username=email,
            defaults={"full_name": full_name, "role": role, "email": email, **extra},
        )
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
        return user

    def _group(self, name, grade, language, tutor, subjects, students):
        group, _ = Group.objects.get_or_create(
            name=name,
            defaults={
                "grade": grade,
                "language": language,
                "tutor": tutor,
                "start_date": timezone.localdate() - timedelta(days=60),
            },
        )
        # set() на m2m дёргает сигнал → журналы создаются автоматически.
        group.subjects.set(subjects)
        group.students.set(students)
        return group

    def _schedule(self, group):
        start = timezone.localdate() - timedelta(days=56)
        for subject_index, subject in enumerate(group.subjects.all()):
            for week in range(6):
                ScheduleEntry.objects.get_or_create(
                    group=group,
                    subject=subject,
                    date=start + timedelta(days=week * 7 + subject_index * 2),
                    time=time(15, 0) if subject_index == 0 else time(16, 30),
                    defaults={"comment": ""},
                )
        # Пример переноса: изменение фиксируется комментарием, а не правкой даты.
        entry = group.schedule_entries.order_by("date").last()
        if entry and not entry.comment:
            entry.comment = "Занятие перенесено на 30 минут позже по просьбе тьютора."
            entry.save(update_fields=["comment"])

    def _tests_and_results(self, group, subject, coordinator):
        specs = [
            (TestType.ENTRY, "Входное тестирование", -56),
            (TestType.MIDTERM, "Промежуточное тестирование", -28),
            (TestType.FINAL, "Итоговое тестирование", -3),
        ]
        journals = Journal.objects.filter(group=group, subject=subject)

        for test_type, label, day_offset in specs:
            test, _ = Test.objects.get_or_create(
                name=f"{label} — {subject.name} ({group.name})",
                defaults={
                    "test_type": test_type,
                    "subject": subject,
                    "group": group,
                    "date": timezone.localdate() + timedelta(days=day_offset),
                    "max_score": 100,
                },
            )
            for journal in journals:
                # Входной ниже (диагностика «на входе»), к итоговому — рост.
                base = {"entry": (35, 60), "midterm": (55, 85), "final": (60, 95)}[test_type]
                TestResult.objects.get_or_create(
                    journal=journal,
                    test=test,
                    defaults={
                        "score": random.randint(*base),
                        "comment": random.choice(
                            [
                                "Хороший результат.",
                                "Есть пробелы по теме, разобрали на уроке.",
                                "Заметный прогресс.",
                                "",
                            ]
                        ),
                        "created_by": coordinator,
                    },
                )

    def _lesson_reports(self, group, tutor):
        for entry in group.schedule_entries.select_related("subject"):
            topics = TOPICS[entry.subject.name]
            for journal in Journal.objects.filter(group=group, subject=entry.subject):
                if LessonReport.objects.filter(
                    journal=journal, schedule_entry=entry
                ).exists():
                    continue

                attended = random.random() > 0.12
                homework_assigned = random.random() > 0.25

                LessonReport.objects.create(
                    journal=journal,
                    schedule_entry=entry,
                    date=entry.date,
                    subject=entry.subject,
                    topic=random.choice(topics),
                    attended=attended,
                    # Отсутствовал → оценки за урок нет (валидация это и требует).
                    lesson_grade=random.randint(55, 98) if attended else None,
                    homework_assigned=homework_assigned,
                    homework_grade=(
                        random.randint(50, 100)
                        if homework_assigned and attended and random.random() > 0.15
                        else None
                    ),
                    feedback=(
                        random.choice(FEEDBACKS)
                        if attended
                        else "Пропуск занятия. Тема отправлена родителю."
                    ),
                    created_by=tutor,
                )

    def _report(self, admin, coordinators, tutors, parent):
        out = self.stdout
        out.write(self.style.SUCCESS("\nГотово. Демо-данные созданы.\n"))
        out.write(f"  Учеников:   {Student.objects.count()}")
        out.write(f"  Групп:      {Group.objects.count()}")
        out.write(f"  Журналов:   {Journal.objects.count()}")
        out.write(f"  Уроков:     {LessonReport.objects.count()}")
        out.write(f"  Тестов:     {Test.objects.count()} / результатов: {TestResult.objects.count()}")
        out.write(f"  Расписание: {ScheduleEntry.objects.count()} занятий\n")

        out.write(f"Пароль у всех демо-аккаунтов: {DEMO_PASSWORD}")
        out.write("Логин = email (username == email).\n")
        out.write(f"  Админ:        {admin.username}")
        for c in coordinators:
            out.write(f"  Координатор:  {c.username} ({c.full_name})")
        for t in tutors:
            out.write(f"  Тьютор:       {t.username} ({t.full_name})")
        out.write(f"  Родитель:     {parent.username} ({parent.full_name})")

        journal = Journal.objects.filter(is_active=True).first()
        if journal:
            summary = journal.calculate_final_grade()
            out.write(
                f"\nПример итога — {journal.student.full_name}, {journal.subject.name}: "
                f"{summary['final_grade']} "
                f"(входной тест, диагностика: {summary['entry_test']['score']})"
            )
