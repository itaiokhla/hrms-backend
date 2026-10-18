import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.config import settings


class EmailService:

    @staticmethod
    def send_otp(email: str, otp: str):

        subject = "Password Reset OTP"

        body = f"""
Hello,

Your OTP for password reset is:

{otp}

This OTP will expire in 10 minutes.

If you didn't request this, ignore this email.

Salary Management System
"""

        msg = MIMEMultipart()
        msg["From"] = settings.smtp_from
        msg["To"] = email
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP(
            settings.smtp_host,
            settings.smtp_port,
        )

        server.starttls()

        print("=" * 40)
        print("SMTP USER:", settings.smtp_username)
        print("SMTP PASS:", settings.smtp_password)
        print("=" * 40)
        
        server.login(
            settings.smtp_username,
            settings.smtp_password,
        )

        server.sendmail(
            settings.smtp_from,
            email,
            msg.as_string(),
        )

        server.quit()