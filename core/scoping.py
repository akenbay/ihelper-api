"""Видимость объектов по ролям.

Живёт отдельным модулем, потому что нужен в двух местах сразу:
  - во вьюсетах (get_queryset) — что роль может ЧИТАТЬ;
  - в сериализаторах (queryset у FK-полей) — на что роль может ССЫЛАТЬСЯ при записи.

Без второго тьютор мог бы отправить lesson report с чужим journal_id и записать
оценку в журнал чужого ученика: permission-класс метод разрешает, а объект
никто не проверяет. Оба места должны спрашивать одну и ту же функцию.
"""

from .models import Journal, ScheduleEntry, Student


def scoped_students(user):
    """Ученики, доступные пользователю."""
    if user.is_admin or user.is_coordinator:
        return Student.objects.all()
    if user.is_tutor:
        # Тьютор видит профили только тех учеников, что есть в его группах.
        return Student.objects.filter(groups__tutor=user).distinct()
    if user.is_parent:
        return user.children.all()
    return Student.objects.none()


def scoped_journals(user):
    """Журналы, доступные пользователю."""
    qs = Journal.objects.select_related("student", "subject", "group")
    if user.is_admin or user.is_coordinator:
        return qs
    if user.is_tutor:
        return qs.filter(group__tutor=user)
    if user.is_parent:
        return qs.filter(student__in=user.children.all())
    return qs.none()


def scoped_schedule_entries(user):
    """Занятия расписания, доступные пользователю."""
    qs = ScheduleEntry.objects.select_related("group__tutor", "subject")
    if user.is_admin or user.is_coordinator:
        return qs
    if user.is_tutor:
        return qs.filter(group__tutor=user)
    if user.is_parent:
        return qs.filter(group__students__in=user.children.all()).distinct()
    return qs.none()
