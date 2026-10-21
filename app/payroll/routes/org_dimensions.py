"""Organisation dimensions API (02-multi-level-company §12, §20): pay groups, grades, cost centers,
employment types, designations, work weeks, holiday calendars and branch calendar links.

Reads need org:read (pay groups also payroll:configuration:read) or org:manage_dimensions and are
limited to the caller's legal entities where they are entity-bound; writes need org:manage_dimensions.
State-changing requests accept an Idempotency-Key. Nothing is deleted: set status INACTIVE instead.
"""

from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, require, require_any
from app.database.db import get_db
from app.models.pay_org import PAY_FREQUENCIES, WORKER_CLASSES
from app.payroll.api_types import DecimalStr, StrictModel
from app.payroll.services import idempotency, org_dimensions

router = APIRouter(prefix="/api/v1/payroll/org", tags=["Payroll organisation"])

Status = Literal["ACTIVE", "INACTIVE"]
Frequency = Literal[PAY_FREQUENCIES]  # type: ignore[valid-type]
WorkerClass = Literal[WORKER_CLASSES]  # type: ignore[valid-type]
READ = require_any(*org_dimensions.READ_CODES)
READ_PAY_GROUPS = require_any(*org_dimensions.PAY_GROUP_READ_CODES)
WRITE = require("org:manage_dimensions")


class _Common(StrictModel):
    code: Optional[str] = Field(None, max_length=30)
    name: Optional[str] = Field(None, max_length=100)
    status: Optional[Status] = None


class PayGroupWrite(_Common):
    legal_entity_id: Optional[int] = None
    currency: Optional[str] = Field(None, max_length=3)
    pay_frequency: Optional[Frequency] = None
    calendar_rules: Optional[Dict[str, Any]] = None
    description: Optional[str] = Field(None, max_length=500)


class SalaryRange(StrictModel):
    currency: str = Field(..., max_length=3)
    min: DecimalStr
    mid: Optional[DecimalStr] = None
    max: DecimalStr


class GradeWrite(_Common):
    rank: Optional[int] = Field(None, ge=0, le=10000)
    salary_ranges: Optional[List[SalaryRange]] = Field(None, max_length=20)


class CostCenterWrite(_Common):
    legal_entity_id: Optional[int] = None


class EmploymentTypeWrite(_Common):
    worker_class: Optional[WorkerClass] = None


class DesignationWrite(_Common):
    grade_id: Optional[int] = None


class NthWeekdayOff(StrictModel):
    weekday: int
    nth: List[int] = Field(..., max_length=6)


class WorkWeekWrite(_Common):
    weekly_off_days: Optional[List[int]] = Field(None, max_length=7)
    nth_weekday_offs: Optional[List[NthWeekdayOff]] = Field(None, max_length=7)


class HolidayCalendarWrite(_Common):
    code: Optional[str] = Field(None, max_length=50)
    name: Optional[str] = Field(None, max_length=150)
    parent_calendar_id: Optional[int] = None
    country_code: Optional[str] = Field(None, max_length=2)
    subdivision_code: Optional[str] = Field(None, max_length=6)


class BranchLinkWrite(StrictModel):
    holiday_calendar_id: Optional[int] = None
    work_week_pattern_id: Optional[int] = None


def _payload(data: StrictModel) -> dict:
    values = data.model_dump(exclude_unset=True)
    if isinstance(data, GradeWrite) and data.salary_ranges is not None:
        values["salary_ranges"] = [r.model_dump(exclude_none=True) for r in data.salary_ranges]
    return values


def _write(db: Session, principal: Principal, request: Request, kind: str, data: StrictModel,
           save: Callable, object_id: Optional[int] = None):
    def handler():
        row = save(db, principal, _payload(data), object_id)
        return (200 if object_id else 201), org_dimensions.serialize(kind, row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json", exclude_unset=True))


def _crud(path: str, kind: str, model: type, save: Callable, read=READ) -> None:
    """GET list, POST, GET one and PATCH for one dimension."""

    @router.get(f"/{path}", name=f"list_{kind}")
    def list_(status: Optional[Status] = None, legal_entity_id: Optional[int] = None,
              principal: Principal = Depends(read), db: Session = Depends(get_db)):
        return org_dimensions.list_rows(db, principal, kind, status=status, legal_entity_id=legal_entity_id)

    @router.post(f"/{path}", status_code=201, name=f"create_{kind}")
    def create(data: model, request: Request, principal: Principal = Depends(WRITE),  # type: ignore[valid-type]
               db: Session = Depends(get_db)):
        return _write(db, principal, request, kind, data, save)

    @router.get(f"/{path}/{{object_id}}", name=f"get_{kind}")
    def get(object_id: int, principal: Principal = Depends(read), db: Session = Depends(get_db)):
        return org_dimensions.get_row(db, principal, kind, object_id)

    @router.patch(f"/{path}/{{object_id}}", name=f"update_{kind}")
    def update(object_id: int, data: model, request: Request,  # type: ignore[valid-type]
               principal: Principal = Depends(WRITE), db: Session = Depends(get_db)):
        return _write(db, principal, request, kind, data, save, object_id)


_crud("pay-groups", "pay_group", PayGroupWrite, org_dimensions.save_pay_group, read=READ_PAY_GROUPS)
_crud("grades", "grade", GradeWrite, org_dimensions.save_grade)
_crud("cost-centers", "cost_center", CostCenterWrite, org_dimensions.save_cost_center)
_crud("employment-types", "employment_type", EmploymentTypeWrite, org_dimensions.save_employment_type)
_crud("designations", "designation", DesignationWrite, org_dimensions.save_designation)
_crud("work-weeks", "work_week", WorkWeekWrite, org_dimensions.save_work_week)
_crud("holiday-calendars", "holiday_calendar", HolidayCalendarWrite, org_dimensions.save_holiday_calendar)


@router.put("/branches/{branch_id}/calendar")
def link_branch(branch_id: int, data: BranchLinkWrite, request: Request, principal: Principal = Depends(WRITE),
                db: Session = Depends(get_db)):
    """Choose a branch's holiday calendar and/or work week."""
    def handler():
        branch = org_dimensions.link_branch(db, principal, branch_id, data.model_dump(exclude_unset=True))
        return 200, org_dimensions.serialize_branch_link(branch)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json", exclude_unset=True))
