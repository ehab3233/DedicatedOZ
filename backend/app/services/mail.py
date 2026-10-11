"""Outgoing email, plain text, over SMTP. Off until DOZ_SMTP_HOST is set.

Nothing here is required for the platform to work: a message that cannot be
sent is logged and dropped, never retried, and never fails the job that
wanted it sent.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

log = logging.getLogger(__name__)


def configured() -> bool:
    return bool(settings.smtp_host and settings.mail_from)


def send(to: str, subject: str, body: str) -> bool:
    """Deliver one message. True when the SMTP server accepted it."""
    if not configured():
        log.info("mail to %s not sent (no SMTP configured): %s", to, subject)
        return False
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("mail to %s failed: %s", to, exc)
        return False
    return True
