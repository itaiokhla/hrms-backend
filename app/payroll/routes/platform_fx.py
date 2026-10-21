"""Platform FX rates, managed by platform operators on the platform host (3.2 §4.2, 3.9 §3.3)."""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import PlatformPrincipal, get_platform_principal, require_platform_host
from app.core.rate_limit import client_ip
from app.database.db import system_session
from app.payroll.api_types import DecimalStr, StrictModel
from app.payroll.services import fx
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/api/v1/platform/fx-rates", tags=["Platform FX"],
                   dependencies=[Depends(require_platform_host)])

AUDIT_MODULE = "Platform"


class PlatformRateIn(StrictModel):
    base_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    quote_currency: str = Field(..., pattern=r"^[A-Z]{3}$")
    rate: DecimalStr
    rate_type: Literal["MID", "BUY", "SELL"] = "MID"
    as_of: date
    # A licensed provider's code, or PLATFORM for operator-entered reference rates.
    source: str = Field(fx.PLATFORM_SOURCE, pattern=r"^[A-Z][A-Z0-9_]{1,39}$")


def _audit(db: Session, admin: PlatformPrincipal, request: Request, action: str, row: dict, description: str) -> None:
    AuditLogService.log(
        db, None, AUDIT_MODULE, action, description, "fx_rate", row["id"], client_ip(request), company_id=None,
        actor_type="platform", actor_id=admin.admin_id, actor_email=admin.email,
        details={key: row[key] for key in ("base_currency", "quote_currency", "rate", "rate_type", "as_of", "status",
                                           "source")},
    )


@router.get("")
def list_platform_rates(base: Optional[str] = Query(None, pattern=r"^[A-Z]{3}$"),
                        quote: Optional[str] = Query(None, pattern=r"^[A-Z]{3}$"),
                        on: Optional[date] = Query(None, alias="date"),
                        status: Optional[Literal["ACTIVE", "PENDING_APPROVAL", "REJECTED"]] = None,
                        _: PlatformPrincipal = Depends(get_platform_principal)):
    with system_session() as db:
        rows = fx.list_rates(db, None, base=base, quote=quote, on=on, status=status, platform_only=True)
        return [fx.serialize(row, for_platform=True) for row in rows]


@router.post("", status_code=201)
def create_platform_rate(data: PlatformRateIn, request: Request,
                         admin: PlatformPrincipal = Depends(get_platform_principal)):
    with system_session() as db:
        row = fx.create_platform_rate(db, admin.admin_id, base=data.base_currency, quote=data.quote_currency,
                                      rate=data.rate, rate_type=data.rate_type, as_of=data.as_of, source=data.source)
        _audit(db, admin, request, "FX_RATE_CREATED", row,
               f"Platform FX rate {row['base_currency']}/{row['quote_currency']} for {row['as_of']} ({row['status']})")
        db.commit()
        return row


def _decide(rate_id: int, request: Request, admin: PlatformPrincipal, approve: bool) -> dict:
    with system_session() as db:
        row = fx.decide_platform_rate(db, admin.admin_id, rate_id, approve=approve)
        action = "FX_RATE_APPROVED" if approve else "FX_RATE_REJECTED"
        _audit(db, admin, request, action, row,
               f"Platform FX rate {row['base_currency']}/{row['quote_currency']} for {row['as_of']} "
               f"{'approved' if approve else 'rejected'}")
        db.commit()
        return row


@router.post("/{rate_id}/approve")
def approve_platform_rate(rate_id: int, request: Request, admin: PlatformPrincipal = Depends(get_platform_principal)):
    return _decide(rate_id, request, admin, True)


@router.post("/{rate_id}/reject")
def reject_platform_rate(rate_id: int, request: Request, admin: PlatformPrincipal = Depends(get_platform_principal)):
    return _decide(rate_id, request, admin, False)
