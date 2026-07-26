"""Сервисный слой: бизнес-логика, которую нельзя дублировать во вьюхах.

Здесь живёт авто-создание журналов и выдача/приём приглашений родителей.
"""

import hashlib
import secrets

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .emails import send_parent_invite_email
from .models import Journal, ParentInvite, Role, User

# --- Журналы ------------------------------------------------------------


@transaction.atomic
def sync_group_journals(group):
    """Приводит журналы группы в соответствие с её составом и предметами.

    Для КАЖДОГО ученика группы должен существовать по одному журналу на КАЖДЫЙ
    предмет группы:
      - только математика            → 1 журнал на ученика
      - математика + английский      → 2 журнала на ученика

    Идемпотентна: повторный вызов не создаёт дублей (защищено ещё и уникальным
    индексом (student, subject) на уровне БД). Вызывается при создании группы,
    при смене состава учеников и при смене набора предметов.

    Ученик, исключённый из группы (или предмет, убранный у группы): журнал не
    удаляется — в нём история оценок, — а деактивируется. Возврат ученика в
    группу реактивирует тот же журнал вместе с историей.
    """
    students = list(group.students.all())
    subjects = list(group.subjects.all())

    expected = {(s.id, subj.id) for s in students for subj in subjects}

    existing = {
        (j.student_id, j.subject_id): j
        for j in Journal.objects.filter(group=group).select_related("student", "subject")
    }

    # Создать/реактивировать недостающие.
    for student in students:
        for subject in subjects:
            journal, created = Journal.objects.get_or_create(
                student=student,
                subject=subject,
                defaults={"group": group, "is_active": True},
            )
            if not created and (journal.group_id != group.id or not journal.is_active):
                # Ученик перешёл в другую группу по этому предмету, либо вернулся
                # в эту — переподвешиваем журнал и включаем обратно.
                journal.group = group
                journal.is_active = True
                journal.save(update_fields=["group", "is_active"])

    # Деактивировать те, что привязаны к группе, но больше в неё не входят.
    stale = [j for key, j in existing.items() if key not in expected and j.is_active]
    for journal in stale:
        journal.is_active = False
        journal.save(update_fields=["is_active"])

    return {"active": len(expected), "deactivated": len(stale)}


# --- Приглашения родителей ---------------------------------------------


def hash_token(raw_token: str) -> str:
    """SHA-256 от сырого токена. Токен длинный и случайный, соль не нужна —
    перебором его не взять, а быстрый хеш даёт O(1) поиск по индексу."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


@transaction.atomic
def create_parent_invite(student, created_by=None, email=None):
    """Создаёт одноразовое приглашение и отправляет ссылку родителю.

    Возвращает (invite, raw_token). Сырой токен нигде не сохраняется: он уходит
    только в письмо. Старые неиспользованные приглашения для этого ученика
    аннулируются, чтобы жила ровно одна активная ссылка.
    """
    email = (email or student.parent_email or "").strip().lower()
    if not email:
        raise ValueError("У ученика не указан email родителя (parent_email).")

    ParentInvite.objects.filter(student=student, accepted_at__isnull=True).delete()

    raw_token = secrets.token_urlsafe(32)
    invite = ParentInvite.objects.create(
        student=student,
        email=email,
        token_hash=hash_token(raw_token),
        expires_at=timezone.now()
        + timezone.timedelta(hours=settings.PARENT_INVITE_TTL_HOURS),
        created_by=created_by,
    )

    link = f"{settings.FRONTEND_URL}/invite/{raw_token}"
    send_parent_invite_email(email=email, student=student, link=link)
    return invite, raw_token


def get_usable_invite(raw_token: str):
    """Возвращает пригодное приглашение по сырому токену, иначе None."""
    invite = (
        ParentInvite.objects.select_related("student")
        .filter(token_hash=hash_token(raw_token))
        .first()
    )
    if invite is None or not invite.is_usable:
        return None
    return invite


@transaction.atomic
def accept_parent_invite(raw_token: str, password: str, full_name: str = ""):
    """Родитель перешёл по ссылке и задал пароль.

    Переход по ссылке = подтверждение email, отдельная верификация не нужна.
    Если аккаунт с таким email уже есть (второй ребёнок) — не создаём второй,
    а привязываем ещё одного ребёнка к существующему родителю.
    """
    invite = get_usable_invite(raw_token)
    if invite is None:
        return None

    parent = User.objects.filter(username__iexact=invite.email).first()
    if parent is None:
        parent = User(
            username=invite.email,
            email=invite.email,
            role=Role.PARENT,
            full_name=full_name or invite.student.parent_name or "",
            phone=invite.student.parent_phone or "",
        )
        parent.set_password(password)  # хеширует Django, своего крипто нет
        parent.save()
    elif parent.role != Role.PARENT:
        # Email занят сотрудником — ссылку не принимаем, аккаунт не трогаем.
        return None
    else:
        # Повторное приглашение существующему родителю: пароль не перезаписываем
        # (иначе владение ссылкой = смена пароля чужого аккаунта). Только привязка.
        pass

    parent.children.add(invite.student)

    invite.accepted_at = timezone.now()
    invite.save(update_fields=["accepted_at"])
    return parent


# --- Сброс пароля -------------------------------------------------------
# Токен самодостаточный: uid и подпись Django упакованы в одну строку "uid.token",
# поэтому фронту достаточно прислать только token (без отдельного uid). Ничего в
# БД не храним — используется штатный default_token_generator (одноразовый, с TTL
# через PASSWORD_RESET_TIMEOUT).


def make_password_reset_token(user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"{uid}.{token}"


def parse_password_reset_token(raw_token: str):
    """Возвращает пользователя по валидному токену сброса, иначе None."""
    if not raw_token or "." not in raw_token:
        return None
    uid_b64, _, token = raw_token.partition(".")
    try:
        uid = force_str(urlsafe_base64_decode(uid_b64))
        user = User.objects.get(pk=uid)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        return None
    if not default_token_generator.check_token(user, token):
        return None
    return user
