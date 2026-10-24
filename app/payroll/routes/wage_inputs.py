"""Wage inputs API (3.4 §13, 3.9 §4 Inputs): inputs, approvals and CSV imports under /api/v1/payroll.

State-changing responses are compact action results (ids, statuses, row versions); amounts are read
with GET, so idempotency records never hold pay data.
"""

import hashlib
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal, require
from app.database.db import get_db
from app.models.variable_inputs import IMPORT_STATUSES, WAGE_INPUT_STATUSES
from app.payroll.api_types import DecimalStr, PayMoneyIn, StrictModel
from app.payroll.routes.approvals import ApproveBody, RejectBody
from app.payroll.services import idempotency
from app.payroll.services import wage_inputs as service

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll wage inputs"])

StatusName = Literal[WAGE_INPUT_STATUSES]  # type: ignore[valid-type]
ImportStatusName = Literal[IMPORT_STATUSES]  # type: ignore[valid-type]
CSV_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def _text(value: Optional[str]) -> Optional[str]:
    return (value.strip() or None) if value is not None else None


class RecurrenceIn(StrictModel):
    until: date


class WageInputIn(StrictModel):
    employee_id: int = Field(ge=1)
    component_code: str = Field(min_length=1, max_length=40)
    period_start: date
    period_end: date
    amount: Optional[PayMoneyIn] = None
    units: Optional[DecimalStr] = None
    rate: Optional[DecimalStr] = None
    # An achievement measure of the employee's incentive plan (3.1 §5.9, decision I-22): no amount, units or rate.
    measure: Optional[DecimalStr] = None
    # Currency of a units input (defaults to the pay group's); amounts carry their own.
    currency: Optional[str] = Field(None, pattern=r"^[A-Z]{3}$")
    convert_currency: bool = False
    reason: Optional[str] = Field(None, max_length=1000)
    external_ref: Optional[str] = Field(None, max_length=100)
    recurrence: Optional[RecurrenceIn] = None
    submit: bool = True

    @field_validator("reason", "external_ref")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return _text(value)

    @model_validator(mode="after")
    def _shape(self) -> "WageInputIn":
        if sum(v is not None for v in (self.amount, self.units, self.measure)) != 1:
            raise ValueError("Give exactly one of amount, units or measure.")
        if self.measure is not None and (self.currency is not None or self.rate is not None or self.recurrence
                                         is not None or self.convert_currency):
            raise ValueError("A measure has no currency, rate or recurrence.")
        if self.amount is not None and (self.currency is not None or self.rate is not None):
            raise ValueError("currency and rate go with units; an amount carries its own currency.")
        if self.period_end < self.period_start:
            raise ValueError("period_end is before period_start.")
        return self

    def to_data(self) -> service.InputData:
        return service.InputData(
            employee_id=self.employee_id, component_code=self.component_code, period_start=self.period_start,
            period_end=self.period_end, amount=self.amount.decimal if self.amount else None,
            units=self.measure if self.measure is not None else self.units,
            rate=self.rate, currency=self.amount.currency if self.amount else self.currency,
            convert_currency=self.convert_currency, reason=self.reason, external_ref=self.external_ref,
            recurrence_until=self.recurrence.until if self.recurrence else None,
            source=service.incentives.MEASURE_SOURCE if self.measure is not None else "MANUAL",
        )


class WageInputPatch(StrictModel):
    row_version: int = Field(ge=1)
    amount: Optional[PayMoneyIn] = None
    units: Optional[DecimalStr] = None
    rate: Optional[DecimalStr] = None
    reason: Optional[str] = Field(None, max_length=1000)
    external_ref: Optional[str] = Field(None, max_length=100)

    @field_validator("reason", "external_ref")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return _text(value)


class ReasonBody(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give a reason.")
        return value.strip()


def _result(row) -> dict:
    return {"id": row.id, "status": row.status, "row_version": row.row_version,
            "approval_request_id": row.approval_request_id, "recurrence_parent_id": row.recurrence_parent_id,
            "flags": [f["code"] for f in row.flags or []]}


@router.get("/wage-inputs")
def list_inputs(
    status: Optional[StatusName] = Query(None),
    employee_id: Optional[int] = Query(None, ge=1),
    pay_group_id: Optional[int] = Query(None, ge=1),
    period_start: Optional[date] = Query(None),
    component_code: Optional[str] = Query(None, max_length=40),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    legal_entity_id: Optional[int] = Query(None, ge=1),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_inputs(db, principal, status=status, employee_id=employee_id,
                                       pay_group_id=pay_group_id, period_start=period_start,
                                       component_code=component_code, skip=skip, limit=limit,
                                       legal_entity_id=legal_entity_id)
    return {"total": total, "items": items}


@router.post("/wage-inputs", status_code=201)
def create_input(data: WageInputIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        rows = service.create_input(db, principal, data.to_data(), submit=data.submit)
        return 201, {"items": [_result(r) for r in rows]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/wage-inputs/{input_id}")
def get_input(input_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_input(db, principal, input_id)


@router.patch("/wage-inputs/{input_id}")
def update_input(input_id: int, data: WageInputPatch, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    changes = {}
    for name in data.model_fields_set - {"row_version"}:
        value = getattr(data, name)
        changes[name] = value.decimal if name == "amount" and value is not None else value
    row = service.update_input(db, principal, input_id, changes, row_version=data.row_version)
    result = _result(row)
    db.commit()
    return result


@router.post("/wage-inputs/{input_id}/submit")
def submit_input(input_id: int, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, _result(service.submit_input(db, principal, input_id))

    return idempotency.execute(db, principal, request, handler)


@router.post("/wage-inputs/{input_id}/approve")
def approve_input(input_id: int, request: Request, data: Optional[ApproveBody] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or ApproveBody()

    def handler():
        row = service.decide_input(db, principal, input_id, "APPROVE", data.remarks, data.expected_step)
        return 200, _result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/wage-inputs/{input_id}/reject")
def reject_input(input_id: int, data: RejectBody, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        row = service.decide_input(db, principal, input_id, "REJECT", data.remarks, data.expected_step)
        return 200, _result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/wage-inputs/{input_id}/cancel")
def cancel_input(input_id: int, data: ReasonBody, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, _result(service.cancel_input(db, principal, input_id, data.reason))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/wage-inputs/import", status_code=201)
async def import_inputs(request: Request, file: UploadFile = File(...), principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)):
    """Dry run: validate every row and stage the file; nothing is created until the commit."""
    data = await file.read(service.MAX_IMPORT_BYTES + 1)
    body = {"file_name": file.filename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def handler():
        row = service.dry_run_import(db, principal, file.filename or "import.csv", data)
        return 201, service.serialize_import(row)

    return idempotency.execute(db, principal, request, handler, body=body)


@router.get("/wage-input-imports")
def list_imports(
    status: Optional[ImportStatusName] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Imports the caller may open: all of them with company-wide import access, otherwise their own (F-44)."""
    total, items = service.list_imports(db, principal, status=status, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.get("/wage-input-imports/template")
def import_template(principal: Principal = Depends(require(service.IMPORT))):
    return Response(service.template_csv(), media_type="text/csv",
                    headers={**CSV_HEADERS, "Content-Disposition": 'attachment; filename="wage-inputs-template.csv"'})


@router.get("/wage-input-imports/{import_id}")
def get_import(import_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_import(db, principal, import_id)


@router.post("/wage-input-imports/{import_id}/commit", status_code=202)
def commit_import(import_id: int, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    def handler():
        row = service.commit_import(db, principal, import_id)
        return 202, {"id": row.id, "status": row.status, "job_id": row.job_id}

    return idempotency.execute(db, principal, request, handler)


@router.get("/wage-input-imports/{import_id}/result")
def import_result(import_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    content = service.import_result_csv(db, principal, import_id)
    return Response(content, media_type="text/csv",
                    headers={**CSV_HEADERS, "Content-Disposition": f'attachment; filename="import-{import_id}-result.csv"'})
