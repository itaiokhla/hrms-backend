from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import EmailStr, Field

from app.schemas.attendance import BreakWrite
from app.schemas.auth import Money, StrictModel


class UserInvite(StrictModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=150)
    employee_id: Optional[int] = None
    role_ids: List[int] = Field(default_factory=list, max_length=20)


class UserUpdate(StrictModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=150)
    employee_id: Optional[int] = None


class UserStatusUpdate(StrictModel):
    status: str = Field(..., pattern=r"^(active|suspended|deactivated)$")


class UserRolesUpdate(StrictModel):
    role_ids: List[int] = Field(default_factory=list, max_length=20)


class AssignedEmployeesUpdate(StrictModel):
    employee_ids: List[int] = Field(default_factory=list, max_length=1000)


class GrantItem(StrictModel):
    permission: str = Field(..., max_length=64)
    scope: str = Field(..., max_length=20)


class RoleCreate(StrictModel):
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    grants: List[GrantItem] = Field(default_factory=list, max_length=200)


class RoleUpdate(StrictModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    grants: Optional[List[GrantItem]] = Field(None, max_length=200)


_WALL_TIME = r"^([01]\d|2[0-3]):[0-5]\d$"


class ManualAttendance(StrictModel):
    attendance_id: Optional[int] = None
    employee_id: int
    date: date
    # Instants with an offset (a naive value is refused with TIMEZONE_REQUIRED, BUG-AS-14) ...
    check_in_time: Optional[datetime] = None
    check_out_time: Optional[datetime] = None
    # ... or wall times in the employee's work time zone; an end at or before the start is the next day (BUG-AS-05).
    check_in_local: Optional[str] = Field(None, pattern=_WALL_TIME)
    check_out_local: Optional[str] = Field(None, pattern=_WALL_TIME)
    lunch_start_local: Optional[str] = Field(None, pattern=_WALL_TIME)
    lunch_end_local: Optional[str] = Field(None, pattern=_WALL_TIME)
    # "absent" may be sent without times: it is recorded with no hours (BUG-AS-04, form part).
    status: str = Field("present", pattern=r"^(present|absent|late|half-day)$")
    notes: Optional[str] = Field(None, max_length=500)
    # When given, replaces the session's lunch breaks (the original events stay in the log).
    breaks: Optional[List[BreakWrite]] = Field(None, max_length=10)


class SalaryStructureCreate(StrictModel):
    employee_id: int
    basic_salary: Money
    house_rent_allowance: Money = Decimal("0")
    dearness_allowance: Money = Decimal("0")
    conveyance_allowance: Money = Decimal("0")
    other_allowances: Money = Decimal("0")
    effective_from: date
    effective_to: Optional[date] = None


class SalaryStructureUpdate(StrictModel):
    basic_salary: Optional[Money] = None
    house_rent_allowance: Optional[Money] = None
    dearness_allowance: Optional[Money] = None
    conveyance_allowance: Optional[Money] = None
    other_allowances: Optional[Money] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class HolidayWrite(StrictModel):
    name: str = Field(..., min_length=1, max_length=150)
    date: date
    location: Optional[str] = Field(None, max_length=100)
    is_optional: bool = False
    description: Optional[str] = Field(None, max_length=500)


class AssetWrite(StrictModel):
    asset_tag: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=150)
    category: str = Field(..., min_length=1, max_length=50)
    serial_number: Optional[str] = Field(None, max_length=100)
    purchase_date: Optional[date] = None
    purchase_cost: Optional[Money] = None
    notes: Optional[str] = Field(None, max_length=1000)


class AssetAssign(StrictModel):
    employee_id: int
    notes: Optional[str] = Field(None, max_length=500)


class AssetReturn(StrictModel):
    condition: Optional[str] = Field(None, max_length=50)
    status: str = Field("in_stock", pattern=r"^(in_stock|maintenance|retired)$")


class AssetStatus(StrictModel):
    status: str = Field(..., pattern=r"^(in_stock|maintenance|retired)$")
