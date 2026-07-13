"""Сигналы.

Журналы синхронизируются при ЛЮБОМ изменении состава группы — из API, из админки,
из seed-команды или из шелла. Поэтому триггер висит на m2m, а не в сериализаторе:
так журналы не разъедутся, каким бы путём группу ни поменяли.
"""

from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from .models import Group
from .services import sync_group_journals


@receiver(m2m_changed, sender=Group.students.through)
@receiver(m2m_changed, sender=Group.subjects.through)
def on_group_composition_changed(sender, instance, action, reverse, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if reverse:
        # Обратная сторона (student.groups.add(...)) — instance это Student/Subject.
        return
    sync_group_journals(instance)
