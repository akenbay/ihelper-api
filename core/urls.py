from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from . import views

router = DefaultRouter()
router.register("coordinators", views.CoordinatorViewSet, basename="coordinator")
router.register("tutors", views.TutorViewSet, basename="tutor")
router.register("students", views.StudentViewSet, basename="student")
router.register("subjects", views.SubjectViewSet, basename="subject")
router.register("groups", views.GroupViewSet, basename="group")
router.register("journals", views.JournalViewSet, basename="journal")
router.register("lesson-reports", views.LessonReportViewSet, basename="lessonreport")
router.register("tests", views.TestViewSet, basename="test")
router.register("test-results", views.TestResultViewSet, basename="testresult")
router.register("schedule", views.ScheduleEntryViewSet, basename="schedule")
router.register("materials", views.MaterialViewSet, basename="material")
router.register("parent-invite", views.ParentInviteViewSet, basename="parentinvite")

auth_patterns = [
    path("login", views.LoginView.as_view(), name="login"),
    path("refresh", TokenRefreshView.as_view(), name="token_refresh"),
    path("verify", TokenVerifyView.as_view(), name="token_verify"),
    path("me", views.MeView.as_view(), name="me"),
    path("password-reset", views.PasswordResetRequestView.as_view(), name="password_reset"),
    path(
        "password-reset/confirm",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("invite/accept", views.InviteAcceptView.as_view(), name="invite_accept"),
    path("invite/<str:token>", views.InviteInfoView.as_view(), name="invite_info"),
]

urlpatterns = [
    path("auth/", include(auth_patterns)),
    path("reports/journal/", views.JournalReportView.as_view(), name="report_journal"),
    path("", include(router.urls)),
]
