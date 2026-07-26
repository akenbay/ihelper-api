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
from .models import Invite, Journal, Role, User

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


# --- Приглашения (coordinator / tutor / parent) -------------------------
# Единый механизм: одна модель Invite, одна логика токена/хеша/протухания и один
# путь accept. Отличаются только create-функции (к чему привязываем приглашение).


def hash_token(raw_token: str) -> str:
    """SHA-256 от сырого токена. Токен длинный и случайный, соль не нужна —
    перебором его не взять, а быстрый хеш даёт O(1) поиск по индексу."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def invite_link(raw_token: str) -> str:
    """Ссылка-приглашение с сырым токеном. Одна форма для всех ролей."""
    return f"{settings.FRONTEND_URL}/invite/{raw_token}"


def _create_invite(*, role, email, ttl_hours, user=None, student=None, created_by=None):
    """Низкоуровневое создание приглашения. Возвращает (invite, raw_token)."""
    raw_token = secrets.token_urlsafe(32)
    invite = Invite.objects.create(
        role=role,
        email=email,
        user=user,
        student=student,
        token_hash=hash_token(raw_token),
        expires_at=timezone.now() + timezone.timedelta(hours=ttl_hours),
        created_by=created_by,
    )
    return invite, raw_token


@transaction.atomic
def create_staff_invite(user, created_by=None):
    """Приглашение сотруднику (координатор/тьютор): аккаунт уже создан без пароля.

    Возвращает (invite, raw_token). Письмо пока НЕ шлём — ссылку показываем тому,
    кто создал аккаунт (invite_link в ответе API), он передаёт её вручную. Позже
    сюда можно добавить отправку через Brevo, как у родителей. Старые неиспользованные
    приглашения этого пользователя аннулируются — активной остаётся одна ссылка.
    """
    Invite.objects.filter(user=user, accepted_at__isnull=True).delete()
    return _create_invite(
        role=user.role,
        email=user.email,
        ttl_hours=settings.STAFF_INVITE_TTL_HOURS,
        user=user,
        created_by=created_by,
    )


@transaction.atomic
def create_parent_invite(student, created_by=None, email=None):
    """Приглашение родителя и отправка ссылки на email (Brevo).

    Возвращает (invite, raw_token). Аккаунт родителя создаётся уже на accept.
    Старые неиспользованные приглашения по этому ученику аннулируются.
    """
    email = (email or student.parent_email or "").strip().lower()
    if not email:
        raise ValueError("У ученика не указан email родителя (parent_email).")

    Invite.objects.filter(
        student=student, role=Role.PARENT, accepted_at__isnull=True
    ).delete()

    invite, raw_token = _create_invite(
        role=Role.PARENT,
        email=email,
        ttl_hours=settings.PARENT_INVITE_TTL_HOURS,
        student=student,
        created_by=created_by,
    )

    send_parent_invite_email(email=email, student=student, link=invite_link(raw_token))
    return invite, raw_token


def get_usable_invite(raw_token: str):
    """Возвращает пригодное приглашение по сырому токену, иначе None."""
    invite = (
        Invite.objects.select_related("student", "user")
        .filter(token_hash=hash_token(raw_token))
        .first()
    )
    if invite is None or not invite.is_usable:
        return None
    return invite


def _accept_staff_invite(invite, password, full_name):
    """Сотрудник задаёт пароль своему (уже созданному) аккаунту."""
    user = invite.user
    if user is None:
        return None
    user.set_password(password)  # хеширует Django, своего крипто нет
    if full_name and not user.full_name:
        user.full_name = full_name
    user.is_active = True
    user.save()
    return user


def _accept_parent_invite(invite, password, full_name):
    """Родитель задаёт пароль; аккаунт создаётся (или находится) и получает ребёнка.

    Если аккаунт с таким email уже есть (второй ребёнок) — не создаём второй,
    а привязываем ещё одного ученика к существующему родителю.
    """
    parent = User.objects.filter(username__iexact=invite.email).first()
    if parent is None:
        parent = User(
            username=invite.email,
            email=invite.email,
            role=Role.PARENT,
            full_name=full_name or invite.student.parent_name or "",
            phone=invite.student.parent_phone or "",
        )
        parent.set_password(password)
        parent.save()
    elif parent.role != Role.PARENT:
        # Email занят сотрудником — ссылку не принимаем, аккаунт не трогаем.
        return None
    else:
        # Повторное приглашение существующему родителю: пароль не перезаписываем
        # (иначе владение ссылкой = смена пароля чужого аккаунта). Только привязка.
        pass

    parent.children.add(invite.student)
    return parent


@transaction.atomic
def accept_invite(raw_token: str, password: str, full_name: str = ""):
    """Единый приём приглашения для любой роли. Возвращает пользователя или None.

    Переход по ссылке = подтверждение владения email, отдельная верификация не нужна.
    Приглашение помечается принятым только при успехе (одноразовое).
    """
    invite = get_usable_invite(raw_token)
    if invite is None:
        return None

    if invite.role == Role.PARENT:
        user = _accept_parent_invite(invite, password, full_name)
    else:
        user = _accept_staff_invite(invite, password, full_name)

    if user is None:
        return None

    invite.accepted_at = timezone.now()
    invite.save(update_fields=["accepted_at"])
    return user


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
