from datetime import date
from typing import List, Literal, Optional

from pydantic import Field

from app.schemas.auth import StrictModel


class LeaveApply(StrictModel):
    leave_type: str = Field(..., min_length=1, max_length=30)
    start_date: date
    end_date: date
    reason: Optional[str] = Field(None, max_length=1000)
    # Half days (leave.half_day.enabled): the first day may start at the second half, the last end at the first.
    start_portion: Literal["FULL", "SECOND_HALF"] = "FULL"
    end_portion: Literal["FULL", "FIRST_HALF"] = "FULL"


class LeaveDecision(StrictModel):
    remarks: Optional[str] = Field(None, max_length=1000)


class LeaveTypeWrite(StrictModel):
    code: str = Field(..., min_length=2, max_length=30, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(..., min_length=1, max_length=100)
    annual_quota: int = Field(0, ge=0, le=366)
    is_paid: bool = True
    is_unlimited: bool = False
    is_active: bool = True


class ChainStepWrite(StrictModel):
    approver_type: str = Field(..., pattern=r"^(primary_manager|skip_level_manager|department_head|role)$")
    role_id: Optional[int] = None
    min_days: Optional[int] = Field(None, ge=1, le=366)


class ChainWrite(StrictModel):
    steps: List[ChainStepWrite]
