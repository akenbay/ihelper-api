"""organization становится NOT NULL на всех доменных моделях.

Выполняется после бэкфилла (0003), поэтому строк без организации уже нет.
"""

import django.db.models.deletion
from django.db import migrations, models


def org_fk(related_name):
    return models.ForeignKey(
        on_delete=django.db.models.deletion.PROTECT,
        related_name=related_name,
        to="core.organization",
        verbose_name="организация",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_backfill_organizations"),
    ]

    operations = [
        migrations.AlterField("user", "organization", org_fk("users")),
        migrations.AlterField("student", "organization", org_fk("students")),
        migrations.AlterField("subject", "organization", org_fk("subjects")),
        migrations.AlterField("group", "organization", org_fk("groups")),
        migrations.AlterField("scheduleentry", "organization", org_fk("schedule_entries")),
        migrations.AlterField("test", "organization", org_fk("tests")),
        migrations.AlterField("material", "organization", org_fk("materials")),
    ]
