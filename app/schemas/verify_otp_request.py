from pydantic import BaseModel, EmailStr


class VerifyOTPRequest(BaseModel):
    email: EmailStr
    otp: str