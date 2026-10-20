from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import EmailStr, Field

from app.schemas.auth import Money, StrictModel


class EmployeeCreate(StrictModel):
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    email: EmailStr
    employee_code: str = Field(..., min_length=1, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    date_of_birth: Optional[date] = None
    gender: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=500)
    city: Optional[str] = Field(None, max_length=50)
    state: Optional[str] = Field(None, max_length=50)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=50)
    location: Optional[str] = Field(None, max_length=100)
    department_id: Optional[int] = None
    shift_id: Optional[int] = None
    position: Optional[str] = Field(None, max_length=100)
    hire_date: date
    # Requires compensation:manage.
    base_salary: Optional[Money] = None
    # Requires org:manage_hierarchy.
    manager_id: Optional[int] = None


class EmployeeUpdate(StrictModel):
    first_name: Optional[str] = Field(None, min_length=1, max_length=50)
    last_name: Optional[str] = Field(None, min_length=1, max_length=50)
    email: Optional[EmailStr] = None
    employee_code: Optional[str] = Field(None, min_length=1, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    date_of_birth: Optional[date] = None
    gender: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=500)
    city: Optional[str] = Field(None, max_length=50)
    state: Optional[str] = Field(None, max_length=50)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=50)
    location: Optional[str] = Field(None, max_length=100)
    department_id: Optional[int] = None
    shift_id: Optional[int] = None
    position: Optional[str] = Field(None, max_length=100)
    hire_date: Optional[date] = None
    base_salary: Optional[Money] = None


class ProfileUpdate(StrictModel):
    phone: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=500)
    city: Optional[str] = Field(None, max_length=50)
    state: Optional[str] = Field(None, max_length=50)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field(None, max_length=50)


class DepartmentWrite(StrictModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    head_employee_id: Optional[int] = None
    is_active: bool = True


class ShiftWrite(StrictModel):
    name: str = Field(..., min_length=1, max_length=100)
    start_time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_hours: Optional[int] = Field(None, ge=1, le=24)
    is_flexible: bool = False
    description: Optional[str] = Field(None, max_length=500)
    is_active: bool = True


class ManagerAssignment(StrictModel):
    manager_id: Optional[int] = None


class SecondaryLineCreate(StrictModel):
    employee_id: int
    manager_id: int
    relation_type: str = Field(..., pattern=r"^(functional|project|dotted_line)$")
    effective_from: date
    effective_until: Optional[date] = None


class DelegationCreate(StrictModel):
    delegator_employee_id: Optional[int] = None
    delegate_employee_id: int
    starts_on: date
    ends_on: date
    reason: Optional[str] = Field(None, max_length=255)
