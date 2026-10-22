"""Pay runs API, part 1 (3.9 §4 Runs) under /api/v1/payroll: create, list, read, population, pre-checks,
cancel, holds, exceptions and statutory attestations. Calculation, review, approval, finalization and
their reads live in the route modules of those services.

``{eid}`` in runs/{id}/employees/{eid}/... is the run-employee id (``payroll_run_employees.id``), as
listed by GET runs/{id}/population. State-changing POSTs accept ``Idempotency-Key`` and an optional
``row_version`` (409 on conflict). Responses never carry amounts, so idempotency records hold no pay data.
"""

from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.pay_runs import (
    ATTESTATION_STATUSES,
    EXCEPTION_SEVERITIES,
    EXCEPTION_STATUSES,
    HOLD_REASONS,
    HOLD_TYPES,
    RUN_EMPLOYEE_STATUSES,
    RUN_STATUSES,
    RUN_TYPES,
)
from app.payroll.api_types import StrictModel
from app.payroll.services import attestations, idempotency
from app.payroll.services import runs as service

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll runs"])

CreatableType = Literal["REGULAR", "OFF_CYCLE", "RETRO_ADJUSTMENT", "FINAL_SETTLEMENT"]
RunType = Literal[RUN_TYPES]  # type: ignore[valid-type]
RunStatus = Literal[RUN_STATUSES]  # type: ignore[valid-type]
EmployeeStatus = Literal[RUN_EMPLOYEE_STATUSES]  # type: ignore[valid-type]
ExceptionStatus = Literal[EXCEPTION_STATUSES]  # type: ignore[valid-type]
Severity = Literal[EXCEPTION_SEVERITIES]  # type: ignore[valid-type]
HoldType = Literal[HOLD_TYPES]  # type: ignore[valid-type]
HoldReason = Literal[HOLD_REASONS]  # type: ignore[valid-type]
AttestationStatus = Literal[ATTESTATION_STATUSES]  # type: ignore[valid-type]


def _required(value: str) -> str:
    if not value.strip():
        raise ValueError("Give a reason.")
    return value.strip()


class RunCreateIn(StrictModel):
    run_type: CreatableType
    pay_group_id: int = Field(ge=1)
    pay_period_id: int = Field(ge=1)
    label: Optional[str] = Field(None, max_length=150)
    employee_ids: List[int] = Field(default_factory=list, max_length=5000)
    original_period_ids: List[int] = Field(default_factory=list, max_length=60)


class VersionBody(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


class ReasonBody(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class HoldIn(StrictModel):
    hold_type: HoldType
    reason_code: HoldReason
    note: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("note")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class ReleaseIn(StrictModel):
    note: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("note")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class DeferIn(StrictModel):
    to: Literal["NEXT_PERIOD", "RETRO"]
    reason: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class FixIn(StrictModel):
    note: Optional[str] = Field(None, max_length=1000)


class AttestationIn(StrictModel):
    legal_entity_id: int = Field(ge=1)
    effective_from: date
    effective_to: Optional[date] = None
    reason: str = Field(min_length=1, max_length=2000)
    schemes: List[str] = Field(default_factory=list, max_length=50)
    evidence_ref: Optional[str] = Field(None, max_length=300)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


def _run_result(run) -> dict:
    return {"id": run.id, "number": run.number, "status": run.status, "row_version": run.row_version}


def _employee_result(row) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "status": row.status, "hold_type": row.hold_type,
            "row_version": row.row_version}


def _exception_result(row) -> dict:
    return {"id": row.id, "code": row.code, "status": row.status, "deferred_to": row.deferred_to,
            "row_version": row.row_version}


# ---------------------------------------------------------------------------- runs
@router.get("/runs")
def list_runs(
    pay_period_id: Optional[int] = Query(None, ge=1),
    legal_entity_id: Optional[int] = Query(None, ge=1),
    pay_group_id: Optional[int] = Query(None, ge=1),
    run_type: Optional[RunType] = Query(None),
    status: Optional[RunStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_runs(db, principal, pay_period_id=pay_period_id, legal_entity_id=legal_entity_id,
                                     pay_group_id=pay_group_id, run_type=run_type, status=status, skip=skip,
                                     limit=limit)
    return {"total": total, "items": items}


@router.get("/run-population-preview")
def population_preview(
    pay_group_id: int = Query(..., ge=1),
    pay_period_id: int = Query(..., ge=1),
    employee_ids: List[int] = Query(default_factory=list),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """The population a new run would have (eligible = included + excluded with reasons, EC-RUN-11)."""
    return service.population_preview(db, principal, pay_group_id, pay_period_id, employee_ids)


@router.post("/runs", status_code=201)
def create_run(data: RunCreateIn, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    def handler():
        run = service.create_run(db, principal, service.RunRequest(
            run_type=data.run_type, pay_group_id=data.pay_group_id, pay_period_id=data.pay_period_id,
            label=data.label, employee_ids=tuple(data.employee_ids), original_period_ids=tuple(data.original_period_ids)))
        return 201, {**_run_result(run), "population": dict(run.population or {})}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/runs/{run_id}")
def get_run(run_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_run(db, principal, run_id)


@router.get("/runs/{run_id}/population")
def run_population(
    run_id: int,
    status: Optional[EmployeeStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return service.run_population(db, principal, run_id, status=status, skip=skip, limit=limit)


@router.post("/runs/{run_id}/precheck")
def precheck(run_id: int, request: Request, data: Optional[VersionBody] = None,
             principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or VersionBody()

    def handler():
        result = service.precheck(db, principal, run_id, expected_version=data.row_version)
        run = result["run"]
        return 200, {"id": run["id"], "number": run["number"], "status": run["status"],
                     "row_version": run["row_version"], "blocking": result["blocking"], "findings": result["findings"]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: int, data: ReasonBody, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    def handler():
        return 200, _run_result(service.cancel_run(db, principal, run_id, data.reason,
                                                   expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/employees/{run_employee_id}/hold")
def hold(run_id: int, run_employee_id: int, data: HoldIn, request: Request,
         principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.hold_employee(db, principal, run_id, run_employee_id, hold_type=data.hold_type,
                                    reason_code=data.reason_code, note=data.note, expected_version=data.row_version)
        return 200, _employee_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/employees/{run_employee_id}/release")
def release(run_id: int, run_employee_id: int, data: ReleaseIn, request: Request,
            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row, intent = service.release_employee(db, principal, run_id, run_employee_id, note=data.note,
                                               expected_version=data.row_version)
        return 200, {**_employee_result(row), "payment_intent": intent}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


# ---------------------------------------------------------------------------- exceptions
@router.get("/runs/{run_id}/exceptions")
def list_exceptions(
    run_id: int,
    status: Optional[ExceptionStatus] = Query(None),
    severity: Optional[Severity] = Query(None),
    code: Optional[str] = Query(None, max_length=40),
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_exceptions(db, principal, run_id, status=status, severity=severity, code=code,
                                           skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/exceptions/{exception_id}/waive")
def waive(exception_id: int, data: ReasonBody, request: Request, principal: Principal = Depends(get_principal),
          db: Session = Depends(get_db)):
    def handler():
        return 200, _exception_result(service.waive_exception(db, principal, exception_id, data.reason,
                                                              expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/exceptions/{exception_id}/defer")
def defer(exception_id: int, data: DeferIn, request: Request, principal: Principal = Depends(get_principal),
          db: Session = Depends(get_db)):
    def handler():
        return 200, _exception_result(service.defer_exception(db, principal, exception_id, data.to, data.reason,
                                                              expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/exceptions/{exception_id}/fix")
def fix(exception_id: int, request: Request, data: Optional[FixIn] = None,
        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or FixIn()

    def handler():
        return 200, _exception_result(service.fix_exception(db, principal, exception_id, data.note))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


# ---------------------------------------------------------------------------- statutory attestations (I-15)
@router.get("/statutory-attestations")
def list_attestations(
    legal_entity_id: Optional[int] = Query(None, ge=1),
    status: Optional[AttestationStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = attestations.list_attestations(db, principal, legal_entity_id=legal_entity_id, status=status,
                                                  skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/statutory-attestations", status_code=201)
def create_attestation(data: AttestationIn, request: Request, principal: Principal = Depends(get_principal),
                       db: Session = Depends(get_db)):
    def handler():
        row = attestations.create(db, principal, legal_entity_id=data.legal_entity_id,
                                  effective_from=data.effective_from, effective_to=data.effective_to,
                                  reason=data.reason, schemes=data.schemes, evidence_ref=data.evidence_ref)
        return 201, attestations.serialize(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/statutory-attestations/{attestation_id}")
def get_attestation(attestation_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return attestations.get_attestation(db, principal, attestation_id)


@router.post("/statutory-attestations/{attestation_id}/approve")
def approve_attestation(attestation_id: int, request: Request, data: Optional[VersionBody] = None,
                        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or VersionBody()

    def handler():
        return 200, attestations.serialize(attestations.approve(db, principal, attestation_id,
                                                                expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/statutory-attestations/{attestation_id}/revoke")
def revoke_attestation(attestation_id: int, data: ReasonBody, request: Request,
                       principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        return 200, attestations.serialize(attestations.revoke(db, principal, attestation_id, data.reason,
                                                               expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())
