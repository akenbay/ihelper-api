"""Видимость объектов по ролям — двухуровневая изоляция.

Уровень 1 (организация): данные разных организаций взаимно невидимы для ВСЕХ
ролей, включая администратора.
Уровень 2 (владелец-координатор): внутри организации координатор видит только
свои строки (owner=self). Только администратор видит все строки своей организации.

Эти функции нужны в двух местах сразу:
  - во вьюсетах (get_queryset) — что роль может ЧИТАТЬ;
  - в сериализаторах (queryset у FK-полей) — на что роль может ССЫЛАТЬСЯ при записи.
Оба места обязаны спрашивать одну и ту же функцию, иначе появляется дыра: метод
разрешён, а объект (чужой журнал / чужой тьютор / чужая организация) не проверен.

Материалы — исключение: организация-скоуп есть, координатор-скоупа нет (общая база
на всю организацию). См. MaterialViewSet.
"""

from .models import (
    Group,
    Journal,
    ScheduleEntry,
    Student,
    Subject,
    Test,
    Tutor,
)


def _org(user):
    return user.organization_id


def scoped_students(user):
    """Ученики, доступные пользователю."""
    if user.is_admin:
        return Student.objects.filter(organization_id=_org(user))
    if user.is_coordinator:
        return Student.objects.filter(owner=user)
    if user.is_tutor:
        return Student.objects.filter(
            groups__tutor=user, organization_id=_org(user)
        ).distinct()
    if user.is_parent:
        return user.children.filter(organization_id=_org(user))
    return Student.objects.none()


def scoped_tutors(user):
    """Тьюторы, на которых пользователь может ссылаться / которых видит."""
    if user.is_admin:
        return Tutor.objects.filter(organization_id=_org(user))
    if user.is_coordinator:
        return Tutor.objects.filter(owner=user)
    return Tutor.objects.none()


def scoped_subjects(user):
    """Предметы своей организации (шарятся внутри организации)."""
    return Subject.objects.filter(organization_id=_org(user))


def scoped_groups(user):
    """Группы, доступные пользователю."""
    qs = Group.objects.select_related("tutor").prefetch_related("subjects", "students")
    if user.is_admin:
        return qs.filter(organization_id=_org(user))
    if user.is_coordinator:
        return qs.filter(owner=user)
    if user.is_tutor:
        return qs.filter(tutor=user, organization_id=_org(user))
    if user.is_parent:
        return qs.filter(
            students__in=user.children.all(), organization_id=_org(user)
        ).distinct()
    return qs.none()


def scoped_journals(user):
    """Журналы, доступные пользователю (у журнала нет своего organization —
    изоляция идёт через ученика/группу)."""
    qs = Journal.objects.select_related("student", "subject", "group")
    if user.is_admin:
        return qs.filter(student__organization_id=_org(user))
    if user.is_coordinator:
        # Журналы учеников этого координатора.
        return qs.filter(student__owner=user)
    if user.is_tutor:
        return qs.filter(group__tutor=user, student__organization_id=_org(user))
    if user.is_parent:
        return qs.filter(
            student__in=user.children.all(), student__organization_id=_org(user)
        )
    return qs.none()


def scoped_tests(user):
    """Тесты, доступные пользователю."""
    qs = Test.objects.select_related("subject", "group")
    if user.is_admin:
        return qs.filter(organization_id=_org(user))
    if user.is_coordinator:
        # Тесты групп этого координатора.
        return qs.filter(group__owner=user)
    if user.is_tutor:
        return qs.filter(group__tutor=user, organization_id=_org(user)).distinct()
    if user.is_parent:
        return qs.filter(
            group__students__in=user.children.all(), organization_id=_org(user)
        ).distinct()
    return qs.none()


def scoped_schedule_entries(user):
    """Занятия расписания, доступные пользователю."""
    qs = ScheduleEntry.objects.select_related("group__tutor", "subject")
    if user.is_admin:
        return qs.filter(organization_id=_org(user))
    if user.is_coordinator:
        return qs.filter(group__owner=user)
    if user.is_tutor:
        return qs.filter(group__tutor=user, organization_id=_org(user))
    if user.is_parent:
        return qs.filter(
            group__students__in=user.children.all(), organization_id=_org(user)
        ).distinct()
    return qs.none()
