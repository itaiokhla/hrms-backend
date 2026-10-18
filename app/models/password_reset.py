from sqlalchemy import Column, String, Boolean, DateTime
from app.models.base import BaseModel


class PasswordResetOTP(BaseModel):
    __tablename__ = "password_reset_otps"

    email = Column(String(255), nullable=False, index=True)

    otp_hash = Column(String(255), nullable=False)

    is_used = Column(Boolean, default=False)

    expires_at = Column(DateTime, nullable=False)