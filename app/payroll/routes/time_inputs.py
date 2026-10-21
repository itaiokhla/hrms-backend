"""Time inputs API (3.4 §4.2, §6, §8): manual LOP inputs, suspensions, on-duty, optional holidays, day-ledger preview."""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal, require
from app.database.db import get_db
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency
from app.payroll.services import time_inputs as service

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll time inputs"])

Portion = Literal["FULL", "FIRST_HALF", "SECOND_HALF"]
InputStatus = Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"]


class LopInputIn(StrictModel):
    employee_id: int = Field(ge=1)
    date: date
    portion: Portion = "FULL"
    reason: str = Field(min_length=1, max_length=1000)


class SuspensionIn(StrictModel):
    employee_id: int = Field(ge=1)
    start_date: date
    end_date: date
    paid: bool = False
    reason: str = Field(min_length=1, max_length=1000)


class OnDutyIn(StrictModel):
    employee_id: Optional[int] = Field(None, ge=1)
    date: date
    portion: Portion = "FULL"
    reason: str = Field(min_length=1, max_length=1000)


class OptionalHolidayIn(StrictModel):
    holiday_id: int = Field(ge=1)


class CancelIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


def _dump(data: Optional[StrictModel]):
    return data.model_dump(mode="json") if data is not None else None


# -- manual LOP -------------------------------------------------------------------------------------
@router.get("/lop-inputs")
def list_lop_inputs(employee_id: Optional[int] = Query(None, ge=1), start: Optional[date] = None,
                    end: Optional[date] = None, status: Optional[InputStatus] = None,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.list_lop_inputs(db, principal, employee_id=employee_id, start=start, end=end, status=status)


@router.post("/lop-inputs", status_code=201)
def create_lop_input(data: LopInputIn, request: Request, principal: Principal = Depends(require(service.INPUTS_EDIT)),
                     db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_lop_input(
        db, principal, employee_id=data.employee_id, day=data.date, portion=data.portion, reason=data.reason)),
        body=_dump(data))


@router.post("/lop-inputs/{lop_id}/cancel")
def cancel_lop_input(lop_id: int, request: Request, data: Optional[CancelIn] = Body(None),
                     principal: Principal = Depends(require(service.INPUTS_EDIT)), db: Session = Depends(get_db)):
    version = data.row_version if data else None
    return idempotency.execute(db, principal, request, lambda: (200, service.cancel_lop_input(
        db, principal, lop_id, row_version=version)), body=_dump(data))


# -- suspensions --------------------------------------------------------------------------------------
@router.get("/suspensions")
def list_suspensions(employee_id: Optional[int] = Query(None, ge=1), start: Optional[date] = None,
                     end: Optional[date] = None, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    return service.list_suspensions(db, principal, employee_id=employee_id, start=start, end=end)


@router.post("/suspensions", status_code=201)
def create_suspension(data: SuspensionIn, request: Request, principal: Principal = Depends(require(service.INPUTS_EDIT)),
                      db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_suspension(
        db, principal, employee_id=data.employee_id, start=data.start_date, end=data.end_date, paid=data.paid,
        reason=data.reason)), body=_dump(data))


@router.post("/suspensions/{suspension_id}/cancel")
def cancel_suspension(suspension_id: int, request: Request, data: Optional[CancelIn] = Body(None),
                      principal: Principal = Depends(require(service.INPUTS_EDIT)), db: Session = Depends(get_db)):
    version = data.row_version if data else None
    return idempotency.execute(db, principal, request, lambda: (200, service.cancel_suspension(
        db, principal, suspension_id, row_version=version)), body=_dump(data))


# -- on-duty -------------------------------------------------------------------------------------------
@router.get("/on-duty")
def list_on_duty(employee_id: Optional[int] = Query(None, ge=1), start: Optional[date] = None,
                 end: Optional[date] = None, status: Optional[InputStatus] = None,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.list_on_duty(db, principal, employee_id=employee_id, start=start, end=end, status=status)


@router.post("/on-duty", status_code=201)
def create_on_duty(data: OnDutyIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.create_on_duty(
        db, principal, employee_id=data.employee_id, day=data.date, portion=data.portion, reason=data.reason)),
        body=_dump(data))


@router.post("/on-duty/{request_id}/cancel")
def cancel_on_duty(request_id: int, request: Request, data: Optional[CancelIn] = Body(None),
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    version = data.row_version if data else None
    return idempotency.execute(db, principal, request, lambda: (200, service.cancel_on_duty(
        db, principal, request_id, row_version=version)), body=_dump(data))


# -- optional holidays ------------------------------------------------------------------------------------
@router.get("/optional-holidays")
def optional_holidays(year: int = Query(..., ge=2000, le=2100), employee_id: Optional[int] = Query(None, ge=1),
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.optional_holidays(db, principal, year=year, employee_id=employee_id)


@router.post("/optional-holidays", status_code=201)
def choose_optional_holiday(data: OptionalHolidayIn, request: Request, principal: Principal = Depends(get_principal),
                            db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (201, service.choose_optional_holiday(
        db, principal, data.holiday_id)), body=_dump(data))


@router.post("/optional-holidays/{holiday_id}/cancel")
def cancel_optional_holiday(holiday_id: int, request: Request, principal: Principal = Depends(get_principal),
                            db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request, lambda: (200, service.cancel_optional_holiday(
        db, principal, holiday_id)))


# -- day-ledger preview -----------------------------------------------------------------------------------
@router.get("/employees/{employee_id}/day-ledger-preview")
def day_ledger_preview(employee_id: int, start: date = Query(...), end: date = Query(...),
                       principal: Principal = Depends(require(service.INPUTS_READ)), db: Session = Depends(get_db)):
    return service.day_ledger_preview(db, principal, employee_id, start, end)
