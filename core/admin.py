from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    Group,
    Journal,
    LessonReport,
    Material,
    Organization,
    ParentInvite,
    ScheduleEntry,
    Student,
    Subject,
    Test,
    TestResult,
    User,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ["username", "full_name", "role", "email", "is_active"]
    list_filter = ["role", "is_active"]
    search_fields = ["username", "full_name", "email"]
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("IHelper", {"fields": ("role", "full_name", "phone", "organization", "children")}),
    )
    filter_horizontal = ["groups", "user_permissions", "children"]


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ["full_name", "grade", "language", "svc_category", "parent_email", "is_active"]
    list_filter = ["grade", "language", "is_active"]
    search_fields = ["full_name", "parent_name", "parent_email"]


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ["name", "grade", "tutor", "start_date", "is_active"]
    list_filter = ["grade", "is_active"]
    filter_horizontal = ["subjects", "students"]


@admin.register(Journal)
class JournalAdmin(admin.ModelAdmin):
    list_display = ["student", "subject", "group", "is_active"]
    list_filter = ["subject", "is_active"]
    search_fields = ["student__full_name"]


@admin.register(LessonReport)
class LessonReportAdmin(admin.ModelAdmin):
    list_display = ["date", "journal", "topic", "attended", "lesson_grade", "homework_grade"]
    list_filter = ["date", "attended", "subject"]


@admin.register(TestResult)
class TestResultAdmin(admin.ModelAdmin):
    list_display = ["journal", "test", "score"]
    list_filter = ["test__test_type"]


admin.site.register([Subject, ScheduleEntry, Test, Material, ParentInvite, Organization])

admin.site.site_header = "IHelper — администрирование"
admin.site.site_title = "IHelper"
admin.site.index_title = "Управление платформой"
