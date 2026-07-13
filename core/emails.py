"""Отправка писем.

Единственная точка интеграции с почтой. Сейчас — Django EmailBackend
(по умолчанию console: письмо печатается в лог). Чтобы подключить Brevo,
достаточно переписать тело send_email() на вызов их API — вызывающий код
менять не придётся.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_email(*, to: str, subject: str, body: str) -> None:
    """Абстрактная отправка. TODO: заменить на Brevo (transactional email API)."""
    logger.info("Отправка письма на %s: %s", to, subject)
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to],
        fail_silently=False,
    )


def send_parent_invite_email(*, email: str, student, link: str) -> None:
    send_email(
        to=email,
        subject="Доступ к журналу IHelper",
        body=(
            f"Здравствуйте!\n\n"
            f"Вас пригласили в систему IHelper, чтобы вы могли следить за успеваемостью "
            f"ученика: {student.full_name}.\n\n"
            f"Перейдите по ссылке и задайте пароль — после этого вам будет доступен "
            f"журнал вашего ребёнка:\n{link}\n\n"
            f"Ссылка одноразовая и действует "
            f"{settings.PARENT_INVITE_TTL_HOURS} часов.\n\n"
            f"Если вы не ожидали это письмо — просто проигнорируйте его.\n\n"
            f"Команда IHelper"
        ),
    )


def send_password_reset_email(*, email: str, link: str) -> None:
    send_email(
        to=email,
        subject="Восстановление пароля IHelper",
        body=(
            f"Здравствуйте!\n\n"
            f"Вы запросили восстановление пароля в системе IHelper.\n"
            f"Чтобы задать новый пароль, перейдите по ссылке:\n{link}\n\n"
            f"Если вы не запрашивали восстановление — просто проигнорируйте это письмо, "
            f"пароль останется прежним.\n\n"
            f"Команда IHelper"
        ),
    )
