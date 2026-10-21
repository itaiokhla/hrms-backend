"""Tenant FX rates: list, lookup, manual entry with maker-checker approval (3.2 §4, 3.9 §4)."""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.errors import forbidden
from app.core.principal import Principal, require
from app.database.db import get_db
from app.payroll.api_types import DecimalStr, StrictModel
from app.payroll.money import MoneyError
from app.payroll.services import fx, idempotency

router = APIRouter(prefix="/api/v1/payroll/fx-rates", tags=["Payroll FX"])

READ = "payroll:configuration:read"
WRITE = "payroll:configuration:write"
Currency = Field(..., pattern=r"^[A-Z]{3}$")
CurrencyQuery = Query(None, pattern=r"^[A-Z]{3}$")
RateType = Literal["MID", "BUY", "SELL"]
Status = Literal["ACTIVE", "PENDING_APPROVAL", "REJECTED"]


class ManualRateIn(StrictModel):
    base_currency: str = Currency
    quote_currency: str = Currency
    rate: DecimalStr
    rate_type: RateType = "MID"
    as_of: date


def _writer(principal: Principal) -> None:
    # FX rates apply to the whole company, so entering or deciding one needs company-wide configuration rights.
    if not principal.has_company_scope(WRITE):
        raise forbidden()


@router.get("")
def list_rates(base: Optional[str] = CurrencyQuery, quote: Optional[str] = CurrencyQuery,
               on: Optional[date] = Query(None, alias="date"), status: Optional[Status] = None,
               limit: int = Query(200, ge=1, le=500),
               principal: Principal = Depends(require(READ)), db: Session = Depends(get_db)):
    rows = fx.list_rates(db, principal.company_id, base=base, quote=quote, on=on, status=status, limit=limit)
    return [fx.serialize(row) for row in rows]


@router.get("/lookup")
def lookup(base: str = Query(..., pattern=r"^[A-Z]{3}$"), quote: str = Query(..., pattern=r"^[A-Z]{3}$"),
           on: date = Query(..., alias="date"), rate_type: Optional[RateType] = None,
           principal: Principal = Depends(require(READ)), db: Session = Depends(get_db)):
    """The rate a conversion on `date` would use, or the issue that blocks it (EC-MON-05/06)."""
    try:
        found = fx.lookup(db, principal.company_id, base, quote, on, rate_type=rate_type)
    except MoneyError as exc:  # same currency, unknown or non-monetary code
        return {"ok": False, "rate": None, "issue": {"code": exc.code, "message": exc.message}}
    if not found.ok:
        return {"ok": False, "rate": None, "issue": found.issue.to_dict()}
    return {"ok": True, "rate": found.rate.to_record(), "quotation": found.rate.quotation, "issue": None}


@router.get("/{rate_id}")
def get_rate(rate_id: int, principal: Principal = Depends(require(READ)), db: Session = Depends(get_db)):
    return fx.serialize(fx.get_visible(db, principal.company_id, rate_id))


@router.post("", status_code=201)
def create_rate(data: ManualRateIn, request: Request, principal: Principal = Depends(require(WRITE)),
                db: Session = Depends(get_db)):
    _writer(principal)
    return idempotency.execute(
        db, principal, request,
        lambda: (201, fx.create_manual_rate(
            db, principal, base=data.base_currency, quote=data.quote_currency, rate=data.rate,
            rate_type=data.rate_type, as_of=data.as_of,
        )),
        body=data.model_dump(mode="json"),
    )


@router.post("/{rate_id}/approve")
def approve_rate(rate_id: int, request: Request, principal: Principal = Depends(require(WRITE)),
                 db: Session = Depends(get_db)):
    _writer(principal)
    return idempotency.execute(db, principal, request,
                               lambda: (200, fx.decide_manual_rate(db, principal, rate_id, approve=True)))


@router.post("/{rate_id}/reject")
def reject_rate(rate_id: int, request: Request, principal: Principal = Depends(require(WRITE)),
                db: Session = Depends(get_db)):
    _writer(principal)
    return idempotency.execute(db, principal, request,
                               lambda: (200, fx.decide_manual_rate(db, principal, rate_id, approve=False)))
