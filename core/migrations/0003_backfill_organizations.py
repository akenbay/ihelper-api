"""Бэкфилл организаций и владельцев для уже существующих строк.

Идёт между 0002 (поля добавлены как nullable) и 0004 (organization становится
NOT NULL). Создаёт организацию по умолчанию (CLA) и привязывает к ней все записи
без организации. Владельца-координатора у учеников подставляем из created_by,
если тот — координатор; остальным owner оставляем пустым (виден только админу).
"""

from django.db import migrations

DEFAULT_ORG_NAME = "CLA"
ORG_MODELS = ["User", "Student", "Subject", "Group", "Test", "ScheduleEntry", "Material"]


def backfill(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    default_org, _ = Organization.objects.get_or_create(
        name=DEFAULT_ORG_NAME, defaults={"is_active": True}
    )

    for model_name in ORG_MODELS:
        Model = apps.get_model("core", model_name)
        Model.objects.filter(organization__isnull=True).update(organization=default_org)

    # Владелец ученика ← создатель, если тот координатор.
    Student = apps.get_model("core", "Student")
    for student in Student.objects.filter(
        owner__isnull=True, created_by__isnull=False
    ).select_related("created_by"):
        creator = student.created_by
        if creator is not None and creator.role == "coordinator":
            student.owner = creator
            student.save(update_fields=["owner"])


def noop(apps, schema_editor):
    # Обратная миграция: organization/owner снова станут nullable в 0002,
    # данные трогать не нужно.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_isolation_add_fields"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
