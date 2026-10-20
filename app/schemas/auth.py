from decimal import Decimal
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class StrictModel(BaseModel):
    """Rejects unknown fields so clients cannot over-post protected attributes."""

    model_config = ConfigDict(extra="forbid")


class CompanySignupRequest(StrictModel):
    company_name: str = Field(..., min_length=2, max_length=150)
    full_name: str = Field(..., min_length=2, max_length=150)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    timezone: Optional[str] = Field(None, max_length=64)


class LoginRequest(StrictModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class MfaLoginRequest(StrictModel):
    mfa_token: str = Field(..., max_length=2048)
    code: str = Field(..., min_length=6, max_length=8)


class RefreshRequest(StrictModel):
    refresh_token: Optional[str] = Field(None, max_length=512)


class HandoffRequest(StrictModel):
    code: str = Field(..., min_length=20, max_length=128)


class ChangePasswordRequest(StrictModel):
    current_password: str = Field(..., max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class ForgotPasswordRequest(StrictModel):
    email: EmailStr


class VerifyOtpRequest(StrictModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class ResetPasswordRequest(StrictModel):
    reset_token: str = Field(..., max_length=2048)
    new_password: str = Field(..., min_length=8, max_length=128)


class AcceptInvitationRequest(StrictModel):
    token: str = Field(..., max_length=256)
    password: str = Field(..., min_length=8, max_length=128)


class MfaCodeRequest(StrictModel):
    code: str = Field(..., min_length=6, max_length=8)


class MfaDisableRequest(StrictModel):
    password: str = Field(..., max_length=128)
    code: str = Field(..., min_length=6, max_length=8)
