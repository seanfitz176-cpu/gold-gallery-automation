"""Email alerts for the autonomy engine (docs/autonomy_engine.md §8).

Standard library only (``smtplib`` + ``email``), STARTTLS required. Does
nothing at all unless every APP_SMTP_* / APP_ALERT_EMAIL setting is present
-- those are entered by the owner in .env / .env.production, never by
Claude. Alert bodies contain SKUs, prices and counts only: never
credentials, tokens, or buyer details. A failed send is logged and
swallowed -- an email problem must never stop the engine or the app.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable, Optional

from .config import AppConfig

SmtpFactory = Callable[[str, int, float], smtplib.SMTP]


def _default_smtp(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=timeout)


class Notifier:
    def __init__(
        self,
        config: AppConfig,
        *,
        smtp_factory: SmtpFactory = _default_smtp,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.smtp_factory = smtp_factory
        self.logger = logger or logging.getLogger("ebay_engine")

    @property
    def configured(self) -> bool:
        c = self.config
        return bool(c.smtp_host and c.smtp_user and c.smtp_app_password and c.alert_email)

    def send(self, subject: str, body: str) -> bool:
        if not self.configured:
            return False
        message = EmailMessage()
        message["Subject"] = f"[eBay {self.config.environment.capitalize()}] {subject}"[:200]
        message["From"] = self.config.smtp_user
        message["To"] = self.config.alert_email
        message.set_content(body[:20000])
        try:
            with self.smtp_factory(self.config.smtp_host, self.config.smtp_port, 20.0) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.config.smtp_user, self.config.smtp_app_password)
                smtp.send_message(message)
            return True
        except (OSError, smtplib.SMTPException) as error:
            # Never log the password or the full exception chain.
            self.logger.warning("Alert email could not be sent: %s", type(error).__name__)
            return False
