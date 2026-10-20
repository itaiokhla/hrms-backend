"""Transactional email delivery.

Messages are rendered from the shared branded layout (email_templates) for the
recipient's tenant, with links built from the tenant's primary *verified* host in the
database (never from a request's Host header).

Sender identity (01-white-label §7.1): the display name is the brand's sender name or
company name; the address is the platform's configured SMTP_FROM, because sending
domains per tenant need an email provider with domain-identity APIs (SES, Postmark,
SendGrid, Mailgun) that is not configured here. Reply-To is the brand's support email.

Adapters: SMTP when configured. Otherwise, in development and test only, messages are
written to EMAIL_OUTBOX_DIR as .eml files (they may contain one-time codes and links,
so this is never done in staging or production).
"""

import logging
import re
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    html: str
    text: str
    from_name: str
    reply_to: Optional[str]
    template_key: str
    company_id: Optional[int]


class EmailService:
    @staticmethod
    def configured() -> bool:
        return bool(settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.smtp_from)

    @staticmethod
    def _mime(message: OutgoingEmail) -> EmailMessage:
        sender = settings.smtp_from or "no-reply@localhost"
        mime = EmailMessage()
        mime["From"] = formataddr((message.from_name, sender))
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or "localhost")
        if message.reply_to:
            mime["Reply-To"] = message.reply_to
        mime["X-HRMS-Template"] = message.template_key
        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")
        return mime

    @staticmethod
    def deliver(message: OutgoingEmail, dev_secret_hint: str = "") -> bool:
        mime = EmailService._mime(message)
        if not EmailService.configured():
            if settings.environment in ("development", "test"):
                EmailService._write_outbox(message, mime)
                if settings.environment == "development":
                    # Local development only: SMTP is not configured, so surface the one-time
                    # value in the server console instead of emailing it.
                    logger.warning("[DEV ONLY] Email to %s (%s): %s", message.to, message.subject, dev_secret_hint)
            else:
                log = logger.error if settings.environment == "production" else logger.info
                log("Email delivery is not configured; message '%s' to %s was not sent", message.template_key, message.to)
            return False
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(mime)
            return True
        except (smtplib.SMTPException, OSError):
            logger.exception("Failed to send '%s' email", message.template_key)
            return False

    @staticmethod
    def _write_outbox(message: OutgoingEmail, mime: EmailMessage) -> None:
        try:
            folder = Path(settings.email_outbox_dir).resolve() / str(message.company_id or "platform")
            folder.mkdir(parents=True, exist_ok=True)
            safe_to = re.sub(r"[^a-zA-Z0-9@._-]", "_", message.to)
            name = f"{message.template_key}-{safe_to}-{uuid.uuid4().hex[:8]}"
            (folder / f"{name}.eml").write_bytes(bytes(mime))
            (folder / f"{name}.html").write_text(message.html, encoding="utf-8")
        except OSError:
            logger.exception("Could not write the development email outbox")

    # ------------------------------------------------------------------ composing
    @staticmethod
    def compose(template_key: str, to: str, context: dict, company_id: Optional[int]) -> OutgoingEmail:
        from app.services import email_branding

        return email_branding.compose(template_key, to, context, company_id)

    @staticmethod
    def send_template(template_key: str, to: str, context: dict, company_id: Optional[int], hint: str = "") -> bool:
        try:
            message = EmailService.compose(template_key, to, context, company_id)
        except Exception:
            logger.exception("Could not compose '%s' email for %s", template_key, to)
            return False
        return EmailService.deliver(message, hint)

    @staticmethod
    def send_password_reset_code(to: str, code: str, company_id: Optional[int] = None, name: Optional[str] = None) -> bool:
        context = {
            "recipient": _recipient(name, to),
            "reset": {"code": code, "expires_minutes": settings.password_reset_token_expire_minutes},
        }
        return EmailService.send_template("auth.password_reset", to, context, company_id, f"code={code}")

    @staticmethod
    def send_invitation(
        to: str, company_name: str, invite_url: str, company_id: Optional[int] = None, name: Optional[str] = None
    ) -> bool:
        context = {
            "recipient": _recipient(name, to),
            "invite": {"url": invite_url, "expires_hours": settings.invitation_expire_hours},
        }
        return EmailService.send_template("auth.invite", to, context, company_id, f"url={invite_url}")


def _recipient(name: Optional[str], email: str) -> dict:
    full = (name or "").strip() or email.split("@")[0]
    return {"name": full, "first_name": full.split()[0], "email": email}
