"""Sends notification emails with a list export attached.

Used when conversations, the answer cache, or knowledge gaps reach their
configured threshold - see conversation_log.py, miss_log.py, answer_cache.py.
Silently disabled (logs a warning, does nothing) if SMTP_HOST isn't configured,
since not every deployment wants/has outgoing mail set up.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger("magicard.email")


def send_email(
    subject: str,
    body: str,
    attachment_bytes: bytes,
    attachment_filename: str,
    attachment_mimetype: str = "application/json",
) -> None:
    if not settings.smtp_host:
        logger.warning("SMTP_HOST not configured; skipping notification email %r", subject)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_username
    msg["To"] = settings.notify_email
    msg.set_content(body)

    maintype, _, subtype = attachment_mimetype.partition("/")
    msg.add_attachment(
        attachment_bytes,
        maintype=maintype or "application",
        subtype=subtype or "octet-stream",
        filename=attachment_filename,
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)
