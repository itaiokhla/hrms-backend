"""Overtime entries: filing, editing, approval and attendance proposals (3.4 §9, 3.9 §4 Inputs)."""

import datetime as dt
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.work_items import MAX_OVERTIME_MINUTES, OVERTIME_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency
from app.payroll.services import overtime as service

router = APIRouter(prefix="/api/v1/overtime", tags=["Overtime"])

StatusName = Literal[OVERTIME_STATUSES]  # type: ignore[valid-type]


def _text(value: Optional[str]) -> Optional[str]:
    return (value.strip() or None) if value is not None else None


class OvertimeCreate(StrictModel):
    employee_id: Optional[int] = Field(None, ge=1, description="Omit to file your own overtime")
    date: dt.date
    minutes: int = Field(..., ge=0, le=MAX_OVERTIME_MINUTES, strict=True)
    reason: str = Field(..., min_length=1, max_length=1000)
    start_at: Optional[dt.datetime] = None
    end_at: Optional[dt.datetime] = None
    comp_off: bool = False

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Give a reason for the overtime.")
        return value


class OvertimeUpdate(StrictModel):
    date: Optional[dt.date] = None
    minutes: Optional[int] = Field(None, ge=0, le=MAX_OVERTIME_MINUTES, strict=True)
    reason: Optional[str] = Field(None, min_length=1, max_length=1000)
    start_at: Optional[dt.datetime] = None
    end_at: Optional[dt.datetime] = None
    comp_off: Optional[bool] = None
    row_version: Optional[int] = Field(None, ge=1)

    @model_validator(mode="after")
    def _no_nulls(self) -> "OvertimeUpdate":
        for name in ("date", "minutes", "reason", "comp_off"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason cannot be blank")
        return self


class Remarks(StrictModel):
    remarks: Optional[str] = Field(None, max_length=1000)

    @field_validator("remarks")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return _text(value)


class OvertimeApprove(Remarks):
    minutes_approved: Optional[int] = Field(None, ge=0, le=MAX_OVERTIME_MINUTES, strict=True)
    comp_off: Optional[bool] = None
    expected_step: Optional[int] = Field(None, ge=1)


class OvertimeReject(StrictModel):
    remarks: str = Field(..., min_length=1, max_length=1000)
    expected_step: Optional[int] = Field(None, ge=1)

    @field_validator("remarks")
    @classmethod
    def _required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Give a reason for the rejection.")
        return value


class ProposalRequest(StrictModel):
    start: dt.date
    end: dt.date
    employee_ids: Optional[List[int]] = Field(None, min_length=1, max_length=service.MAX_PROPOSAL_EMPLOYEES)


@router.get("/entries")
def list_entries(
    employee_id: Optional[int] = Query(None, ge=1),
    status: Optional[StatusName] = Query(None),
    start: Optional[dt.date] = Query(None),
    end: Optional[dt.date] = Query(None),
    mine: bool = Query(False, description="Only the caller's own overtime"),
    awaiting: bool = Query(False, description="Only entries awaiting the caller's approval"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=service.MAX_LIST),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_entries(db, principal, employee_id=employee_id, status=status, start=start, end=end,
                                        mine=mine, awaiting=awaiting, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/entries", status_code=201)
def create_entry(data: OvertimeCreate, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        entry = service.create_entry(
            db, principal, employee_id=data.employee_id, day=data.date, minutes=data.minutes, reason=data.reason,
            start_at=data.start_at, end_at=data.end_at, comp_off=data.comp_off,
        )
        return 201, service.action_result(entry)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/proposals/generate")
def generate_proposals(data: ProposalRequest, request: Request, principal: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)):
    def handler():
        return 200, service.generate_proposals(db, principal, start=data.start, end=data.end,
                                               employee_ids=data.employee_ids)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/entries/{entry_id}")
def get_entry(entry_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_entry(db, principal, entry_id)


@router.patch("/entries/{entry_id}")
def update_entry(entry_id: int, data: OvertimeUpdate, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    changes = data.model_dump(exclude_unset=True, exclude={"row_version"})
    if "minutes" in changes:
        changes["minutes"] = data.minutes
    if "reason" in changes:
        changes["reason"] = data.reason.strip()

    def handler():
        entry = service.update_entry(db, principal, entry_id, changes, row_version=data.row_version)
        return 200, service.action_result(entry)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json", exclude_unset=True))


@router.post("/entries/{entry_id}/cancel")
def cancel_entry(entry_id: int, request: Request, data: Optional[Remarks] = None,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or Remarks()

    def handler():
        return 200, service.action_result(service.cancel_entry(db, principal, entry_id, data.remarks))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/entries/{entry_id}/approve")
def approve_entry(entry_id: int, request: Request, data: Optional[OvertimeApprove] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or OvertimeApprove()

    def handler():
        entry = service.decide_entry(db, principal, entry_id, "APPROVE", minutes_approved=data.minutes_approved,
                                     comp_off=data.comp_off, remarks=data.remarks, expected_step=data.expected_step)
        return 200, service.action_result(entry)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/entries/{entry_id}/reject")
def reject_entry(entry_id: int, data: OvertimeReject, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        entry = service.decide_entry(db, principal, entry_id, "REJECT", remarks=data.remarks,
                                     expected_step=data.expected_step)
        return 200, service.action_result(entry)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())
