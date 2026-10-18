from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime, date
from decimal import Decimal
from .change_password import ChangePasswordRequest
from .password_reset_request import ForgotPasswordRequest
from .verify_otp_request import VerifyOTPRequest
from .reset_password_request import ResetPasswordRequest
from .notification import NotificationResponse
class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    role: str = "employee"


class UserCreate(UserBase):
    password: str = Field(..., min_length=8)


class UserResponse(UserBase):
    id: int
    is_active: bool
    last_login: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class TokenRefresh(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class DepartmentBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None


class DepartmentResponse(DepartmentBase):
    id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class ShiftBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    start_time: str
    end_time: str
    duration_hours: Optional[int] = None
    is_flexible: bool = False


class ShiftResponse(ShiftBase):
    id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class EmployeeBase(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    email: EmailStr
    phone: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    address: Optional[str] = None
    employee_id: str = Field(..., min_length=1, max_length=50)
    department_id: int
    shift_id: int
    position: Optional[str] = None
    hire_date: date
    base_salary: Decimal


class EmployeeCreate(EmployeeBase):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)


class EmployeeUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    position: Optional[str] = None


class EmployeeResponse(EmployeeBase):
    id: int
    user_id: int
    is_active: bool
    profile_complete: bool
    created_at: datetime

    class Config:
        from_attributes = True


class AttendanceBase(BaseModel):
    employee_id: int
    check_in_time: datetime
    check_out_time: Optional[datetime] = None
    status: str = "present"


class AttendanceResponse(AttendanceBase):
    id: int
    date: date
    face_match_score: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True


class FaceEncodingBase(BaseModel):
    employee_id: int
    quality_score: Optional[Decimal] = None


class FaceEncodingResponse(FaceEncodingBase):
    id: int
    is_primary: bool
    created_at: datetime

    class Config:
        from_attributes = True


class PayrollBase(BaseModel):
    employee_id: int
    salary_month: date
    basic_salary: Decimal
    gross_salary: Decimal
    net_salary: Decimal


class PayrollResponse(PayrollBase):
    id: int
    status: str
    present_days: int
    absent_days: int
    created_at: datetime




    class Config:
        from_attributes = True

class SalaryStructureCreate(BaseModel):
    employee_id: int
    basic_salary: Decimal
    house_rent_allowance: Decimal = 0
    dearness_allowance: Decimal = 0
    conveyance_allowance: Decimal = 0
    other_allowances: Decimal = 0
    effective_from: date
    effective_to: Optional[date] = None


class SalaryStructureUpdate(BaseModel):
    basic_salary: Optional[Decimal] = None
    house_rent_allowance: Optional[Decimal] = None
    dearness_allowance: Optional[Decimal] = None
    conveyance_allowance: Optional[Decimal] = None
    other_allowances: Optional[Decimal] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class SalaryStructureResponse(BaseModel):
    id: int
    employee_id: int
    basic_salary: Decimal
    house_rent_allowance: Decimal
    dearness_allowance: Decimal
    conveyance_allowance: Decimal
    other_allowances: Decimal
    effective_from: date
    effective_to: Optional[date]

    class Config:
        from_attributes = True        
