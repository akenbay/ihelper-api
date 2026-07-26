"""Отправка писем.

Единственная точка интеграции с почтой. Реальная отправка идёт через Brevo
(transactional email API, https://api.brevo.com/v3/smtp/email) обычным requests —
ради трёх типов писем тащить весь sib-api-v3-sdk смысла нет.

Ключевые правила:
  - Конфиг только из окружения: BREVO_API_KEY, DEFAULT_FROM_EMAIL, FRONTEND_URL.
  - Если BREVO_API_KEY не задан (локальная разработка) — откат на Django
    EMAIL_BACKEND (по умолчанию console: письмо печатается в лог). Так дев-флоу
    приглашений и сброса пароля продолжает работать без ключа.
  - Отправка НИКОГДА не роняет вызвавший её запрос. Ошибка Brevo (плохой ключ,
    сеть, rate limit) логируется, функция возвращает False. Приглашение/токен к
    этому моменту уже созданы и вернутся в ответе API — их можно передать вручную.

Публичные функции (их сигнатуры фиксированы, вызывающий код не меняется):
  send_email                     — низкоуровневая отправка
  send_parent_invite_email       — приглашение (родитель/координатор/тьютор)
  send_password_reset_email      — восстановление пароля
  send_report_notification_email — уведомление родителю о новом отчёте
"""

import logging
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_TIMEOUT_SECONDS = 10


def _sender():
    """Отправитель для Brevo из DEFAULT_FROM_EMAIL.

    Поддерживает и голый адрес, и формат «Имя <адрес>» (стандартный для Django).
    """
    name, addr = parseaddr(settings.DEFAULT_FROM_EMAIL)
    sender = {"email": addr or settings.DEFAULT_FROM_EMAIL}
    if name:
        sender["name"] = name
    return sender


def _send_via_django(*, to: str, subject: str, body: str) -> bool:
    """Откат без Brevo-ключа: Django EMAIL_BACKEND (в деве — console).

    fail_silently=True — локальная отправка тоже не должна ронять запрос.
    """
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to],
        fail_silently=True,
    )
    return True


def send_email(*, to: str, subject: str, body: str, html: str | None = None) -> bool:
    """Отправляет письмо. Возвращает True при успехе, False при ошибке.

    Никогда не бросает исключение наружу — вызывающий поток не должен падать
    из-за проблем с почтой.
    """
    api_key = getattr(settings, "BREVO_API_KEY", "")
    if not api_key:
        logger.info("BREVO_API_KEY не задан — письмо на %s уходит через Django backend", to)
        return _send_via_django(to=to, subject=subject, body=body)

    payload = {
        "sender": _sender(),
        "to": [{"email": to}],
        "subject": subject,
        "textContent": body,
    }
    if html:
        payload["htmlContent"] = html

    try:
        response = requests.post(
            BREVO_API_URL,
            json=payload,
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=BREVO_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # Тело ответа Brevo помогает понять причину (например, unverified sender).
        detail = getattr(getattr(exc, "response", None), "text", "")
        logger.error(
            "Brevo: не удалось отправить письмо на %s (%r): %s %s",
            to,
            subject,
            exc,
            detail,
        )
        return False

    logger.info("Brevo: письмо отправлено на %s (%r)", to, subject)
    return True


def send_parent_invite_email(*, email: str, student, link: str) -> bool:
    """Приглашение. Ссылка вида {FRONTEND_URL}/invite/{token} формируется вызывающим."""
    return send_email(
        to=email,
        subject="Приглашение в IHelper",
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


def send_password_reset_email(*, email: str, link: str) -> bool:
    """Сброс пароля. Ссылка вида {FRONTEND_URL}/reset-password/{token}."""
    return send_email(
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


def send_report_notification_email(*, email: str, student, period: str = "") -> bool:
    """Уведомление родителю о новом отчёте (раз в две недели).

    Сознательно НЕ содержит оценок и фидбека — только уведомление и ссылка на вход
    (data-minimization: письмо уходит за границу через сторонний сервис, персональные
    данные об успеваемости в теле письма не передаём). Родитель смотрит журнал в
    системе после входа.
    """
    period_text = f" за период {period}" if period else ""
    return send_email(
        to=email,
        subject="Новый отчёт по успеваемости",
        body=(
            f"Здравствуйте!\n\n"
            f"По ученику {student.full_name} готов новый отчёт об успеваемости"
            f"{period_text}.\n\n"
            f"Чтобы посмотреть оценки и комментарии тьютора, войдите в систему IHelper:\n"
            f"{settings.FRONTEND_URL}/login\n\n"
            f"Команда IHelper"
        ),
    )
