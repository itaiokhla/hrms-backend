"""Unclaimed pay API (EC-PAY-19, company policy; app.payroll.services.unclaimed).

Setting ``payroll.payments.unclaimed_after_days`` (default null = off) is a company policy value; escheatment
rules are country-specific and not implemented. Payment users (``payroll:payment:prepare|approve|submit|reconcile``,
legal-entity scope; out-of-scope objects are 404):

``GET /api/v1/payroll/unclaimed-payments`` (UNCLAIMED obligations with the unclaimed totals per currency),
``GET …/{obligation_id}/history`` (marking and release log), ``POST …/scan`` (run the policy scan now;
``payroll:payment:approve``), ``POST …/{obligation_id}/release`` (``payroll:payment:approve``, step-up, a reason).

Nothing here pays or deletes: marking holds unsent payments, releasing returns the obligation to the payment flow.
State-changing requests accept ``Idempotency-Key`` and ``row_version``.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.unclaimed import REASON_MAX
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency
from app.payroll.services import unclaimed as service

router = APIRouter(prefix="/api/v1/payroll/unclaimed-payments", tags=["Unclaimed pay"])


class ScanIn(StrictModel):
    legal_entity_id: Optional[int] = Field(None, ge=1)


class ReleaseIn(StrictModel):
    reason: str = Field(min_length=3, max_length=REASON_MAX)
    row_version: Optional[int] = Field(None, ge=1)


@router.get("")
def list_unclaimed(
    legal_entity_id: Optional[int] = Query(None, ge=1),
    currency: Optional[str] = Query(None, pattern="^[A-Z]{3}$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return service.list_unclaimed(db, principal, legal_entity_id=legal_entity_id, currency=currency, skip=skip,
                                  limit=limit)


@router.get("/{obligation_id}/history")
def unclaimed_history(obligation_id: int, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    return {"items": service.history(db, principal, obligation_id)}


@router.post("/scan")
def scan_now(request: Request, data: Optional[ScanIn] = None, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    data = data or ScanIn()

    def handler():
        outcome = service.run_scan(db, principal, legal_entity_id=data.legal_entity_id)
        return 200, {"checked": outcome["checked"], "marked": outcome["marked"], "skipped": outcome["skipped"],
                     "entities_off": outcome["entities_off"], "errors": outcome["errors"]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/{obligation_id}/release")
def release_unclaimed(obligation_id: int, data: ReleaseIn, request: Request,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.release(db, principal, obligation_id, data.reason, expected_version=data.row_version)
        return 200, {"id": row.id, "status": row.status, "row_version": row.row_version}

    return idempotency.execute(db, principal, request, handler, body={"obligation_id": obligation_id,
                                                                      **data.model_dump()})
