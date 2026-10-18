import random
import hashlib
from datetime import datetime, timedelta
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.auth_service import AuthService
from app.models.password_reset import PasswordResetOTP
from app.services.email_service import EmailService
from app.core.security import hash_password



class PasswordResetService:

    @staticmethod
    def reset_password(
        db: Session,
        email: str,
        new_password: str
        ):

        user = (
            db.query(User)
            .filter(User.email == email)
            .first()
        )

        if not user:
            raise HTTPException(
                status_code=404,
                detail="User not found."
            )

        user.password_hash = hash_password(new_password)
    

        db.commit()

        return {
            "message": "Password updated successfully."
        }

    @staticmethod
    def generate_otp():
        return str(random.randint(100000, 999999))

    @staticmethod
    def verify_otp(db: Session, email: str, otp: str):

        otp_record = (
            db.query(PasswordResetOTP)
            .filter(
                PasswordResetOTP.email == email,
                PasswordResetOTP.is_used == False,
            )
            .order_by(PasswordResetOTP.id.desc())
            .first()
        )

        if not otp_record:
            raise HTTPException(
                status_code=404,
                detail="OTP not found."
            )

        if otp_record.expires_at < datetime.utcnow():
            raise HTTPException(
                status_code=400,
                detail="OTP expired."
            )

        otp_hash = PasswordResetService.hash_otp(otp)

        if otp_hash != otp_record.otp_hash:
            raise HTTPException(
                status_code=400,
                detail="Invalid OTP."
            )

        otp_record.is_used = True

        db.commit()

        return {
            "message": "OTP verified successfully."
        }

    @staticmethod
    def hash_otp(otp: str):
        return hashlib.sha256(otp.encode()).hexdigest()

    @staticmethod
    def send_otp(db: Session, email: str):

        user = db.query(User).filter(User.email == email).first()

        if not user:
            raise HTTPException(
                status_code=404,
                detail="No account found with this email."
            )

        otp = PasswordResetService.generate_otp()

        otp_hash = PasswordResetService.hash_otp(otp)

        db.query(PasswordResetOTP).filter(
            PasswordResetOTP.email == email
        ).delete()

        otp_record = PasswordResetOTP(
            email=email,
            otp_hash=otp_hash,
            expires_at=datetime.utcnow() + timedelta(minutes=10),
            is_used=False,
        )

        db.add(otp_record)
        db.commit()

        EmailService.send_otp(email, otp)

        return {
            "message": "OTP sent successfully."
        }