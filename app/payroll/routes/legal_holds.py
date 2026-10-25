"""Legal holds and document retention API (EC-SEC-16, 3.6 document retention) under /api/v1/payroll/legal-holds.

* ``GET/POST /legal-holds``, ``GET /legal-holds/{id}``: holds (``payroll:legal_hold:manage``, listing also for
  ``payroll:audit:read``). Placing a hold takes an ``Idempotency-Key``.
* ``POST /legal-holds/{id}/release``: another person than the creator, with a recent step-up.
* ``GET /legal-holds/summary``, ``GET /legal-holds/sweeps``: what retention would delete, what is held back and
  the recent sweeps (counts only).
* ``POST /legal-holds/sweeps``: run the retention sweep now (step-up). The periodic job does the same daily.
* ``POST /legal-holds/erasure-requests``: an employee's erasure or anonymisation request (step-up); refused with
  409 ``LEGAL_HOLD`` while a hold covers the employee's data.

Without the permission the answer is 403, ahead of the step-up prompt; unknown or foreign ids are 404.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.errors import AppError, forbidden
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.legal_holds import HOLD_SCOPES, HOLD_STATUSES
from app.models.pay_documents import DOC_TYPES
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency, legal_holds, step_up

router = APIRouter(prefix="/api/v1/payroll/legal-holds", tags=["Legal holds"])

Scope = Literal[HOLD_SCOPES]  # type: ignore[valid-type]
Status = Literal[HOLD_STATUSES]  # type: ignore[valid-type]
DocType = Literal[DOC_TYPES]  # type: ignore[valid-type]


def _required(value: str) -> str:
    if not value.strip():
        raise ValueError("Give a reason.")
    return value.strip()


class HoldIn(StrictModel):
    scope: Scope
    employee_id: Optional[int] = Field(None, ge=1)
    legal_entity_id: Optional[int] = Field(None, ge=1)
    doc_type: Optional[DocType] = None
    document_id: Optional[int] = Field(None, ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    reference: Optional[str] = Field(None, max_length=120)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class ReleaseIn(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


class ErasureIn(StrictModel):
    employee_id: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _required(value)


def _manager_with_step_up(principal: Principal, db: Session) -> Principal:
    """Permission first (403), then the step-up (403 STEP_UP_REQUIRED)."""
    if not principal.has(legal_holds.MANAGE):
        raise forbidden()
    step_up.ensure_step_up(db, principal)
    return principal


@router.get("")
def list_holds(
    status: Optional[Status] = Query(None),
    scope: Optional[Scope] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = legal_holds.list_holds(db, principal, status=status, scope=scope, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("", status_code=201)
def create_hold(data: HoldIn, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        hold = legal_holds.create(db, principal, scope=data.scope, employee_id=data.employee_id,
                                  legal_entity_id=data.legal_entity_id, doc_type=data.doc_type,
                                  document_id=data.document_id, reason=data.reason, reference=data.reference)
        covered = legal_holds.document_ids_under_hold(db, principal.company_id, hold.id).count()
        return 201, legal_holds.serialize(hold, covered=covered)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/summary")
def summary(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return legal_holds.summary(db, principal)


@router.get("/sweeps")
def list_sweeps(limit: int = Query(20, ge=1, le=100), principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    return {"items": legal_holds.list_sweeps(db, principal, limit=limit)}


@router.post("/sweeps")
def run_sweep(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    principal = _manager_with_step_up(principal, db)
    result = legal_holds.run_sweep_now(db, principal)
    db.commit()
    return result


@router.post("/erasure-requests")
def request_erasure(data: ErasureIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    principal = _manager_with_step_up(principal, db)
    try:
        result = legal_holds.erase_employee(db, principal, data.employee_id, data.reason)
    except AppError as exc:
        if exc.code == "LEGAL_HOLD":
            db.commit()  # keep the audit row of the refusal; nothing else was changed
        raise
    db.commit()
    return result


@router.get("/{hold_id}")
def get_hold(hold_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return legal_holds.get_hold(db, principal, hold_id)


@router.post("/{hold_id}/release")
def release_hold(hold_id: int, data: ReleaseIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    principal = _manager_with_step_up(principal, db)

    def handler():
        hold = legal_holds.release(db, principal, hold_id, data.reason, expected_version=data.row_version)
        return 200, legal_holds.serialize(hold)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))
