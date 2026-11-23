"""SMTP email adapter (stdlib smtplib, run in a worker thread)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import make_msgid

from documind.application.ports import EmailMessage
from documind.core.config import Settings


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        self._s = settings

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._send_sync, message)

    def _build(self, message: EmailMessage) -> MimeMessage:
        mime = MimeMessage()
        mime["From"] = self._s.email_from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime["Message-ID"] = make_msgid(domain="documind")
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        return mime

    def _send_sync(self, message: EmailMessage) -> None:
        s = self._s
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if s.smtp_use_ssl:
            smtp = smtplib.SMTP_SSL(
                s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds, context=context
            )
        else:
            smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
        with smtp:
            if s.smtp_starttls:
                smtp.starttls(context=context)
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(self._build(message))
