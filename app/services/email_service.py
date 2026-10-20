import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings

logger = logging.getLogger(__name__)


class EmailService:
    @staticmethod
    def configured() -> bool:
        return bool(settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.smtp_from)

    @staticmethod
    def _send(to: str, subject: str, body: str, dev_secret_hint: str) -> bool:
        if not EmailService.configured():
            if settings.environment == "development":
                # Local development only: SMTP is not configured, so surface the
                # one-time value in the server console instead of emailing it.
                logger.warning("[DEV ONLY] Email to %s (%s): %s", to, subject, dev_secret_hint)
            else:
                log = logger.error if settings.environment == "production" else logger.info
                log("Email delivery is not configured; message '%s' to %s was not sent", subject, to)
            return False
        msg = MIMEMultipart()
        msg["From"] = settings.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                server.starttls()
                server.login(settings.smtp_username, settings.smtp_password)
                server.sendmail(settings.smtp_from, [to], msg.as_string())
            return True
        except (smtplib.SMTPException, OSError):
            logger.exception("Failed to send '%s' email", subject)
            return False

    @staticmethod
    def send_password_reset_code(to: str, code: str) -> bool:
        body = (
            "Hello,\n\n"
            f"Your verification code is: {code}\n\n"
            f"It expires in {settings.password_reset_token_expire_minutes} minutes. "
            "If you did not request a password reset you can ignore this email.\n\n"
            f"{settings.app_name}"
        )
        return EmailService._send(to, "Your password reset code", body, f"code={code}")

    @staticmethod
    def send_invitation(to: str, company_name: str, invite_url: str) -> bool:
        body = (
            "Hello,\n\n"
            f"You have been invited to join {company_name} on {settings.app_name}.\n\n"
            f"Set your password to get started: {invite_url}\n\n"
            f"This link expires in {settings.invitation_expire_hours} hours."
        )
        return EmailService._send(to, f"You're invited to {company_name}", body, f"url={invite_url}")
