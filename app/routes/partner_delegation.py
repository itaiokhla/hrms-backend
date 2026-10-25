"""Partner operator delegated access (Goal 2 section 15).

Two routers:

* `tenant_router` (`/api/v1/partner-access`): the tenant administrator's side. Everything needs
  `settings:partner_access:manage`; granting also needs a recent step-up. The grantor can only delegate
  read-only permissions they hold company-wide (app.services.partner_delegation_service.is_delegable).
* `partner_router` (`/api/v1/partner/delegations`, platform host only): the partner operator's side. It lists
  the delegations the partner holds right now (names and counts, never HR data) and exchanges one for a
  10-minute bearer token that tenant routes accept as a read-only, delegated principal.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, ConfigDict, Field
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.principal import PlatformPrincipal, Principal, get_partner_principal, require, require_platform_host
from app.core.rate_limit import client_ip
from app.database.db import get_db
from app.payroll.services import step_up
from app.services import partner_delegation_service as service

tenant_router = APIRouter(prefix="/api/v1/partner-access", tags=["Partner access"])
partner_router = APIRouter(prefix="/api/v1/partner/delegations", tags=["Partner"],
                           dependencies=[Depends(require_platform_host)])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GrantBody(_Strict):
    permission_codes: List[str] = Field(..., min_length=1, max_length=service.MAX_CODES)
    legal_entity_ids: List[int] = Field(default_factory=list, max_length=service.MAX_TARGETS)
    pay_group_ids: List[int] = Field(default_factory=list, max_length=service.MAX_TARGETS)
    starts_at: Optional[AwareDatetime] = None
    expires_at: AwareDatetime
    reason: str = Field(..., min_length=1, max_length=service.REASON_MAX)


class RevokeBody(_Strict):
    reason: str = Field(..., min_length=1, max_length=service.REASON_MAX)


manage = require(service.MANAGE_CODE)


@tenant_router.get("")
def overview(principal: Principal = Depends(manage), db: Session = Depends(get_db)):
    return service.overview(db, principal)


@tenant_router.post("")
def grant(data: GrantBody, request: Request, principal: Principal = Depends(manage), db: Session = Depends(get_db)):
    step_up.ensure_step_up(db, principal)
    row = service.grant(
        db, principal, codes=data.permission_codes, legal_entity_ids=data.legal_entity_ids,
        pay_group_ids=data.pay_group_ids, starts_at=data.starts_at, expires_at=data.expires_at, reason=data.reason,
        ip_address=client_ip(request),
    )
    db.commit()
    partner = service._partner_of(principal.company_id)
    return service.serialize(db, row, partner.name if partner else None)


@tenant_router.post("/{delegation_id}/revoke")
def revoke(delegation_id: int, data: RevokeBody, request: Request, principal: Principal = Depends(manage),
           db: Session = Depends(get_db)):
    # No step-up: ending access must always be one click away.
    row = service.revoke(db, principal, delegation_id, data.reason, client_ip(request))
    db.commit()
    partner = service._partner_of(principal.company_id)
    return service.serialize(db, row, partner.name if partner else None)


@tenant_router.get("/{delegation_id}/activity")
def activity(delegation_id: int, limit: int = Query(100, ge=1, le=200), principal: Principal = Depends(manage),
             db: Session = Depends(get_db)):
    return {"items": service.activity(db, principal, delegation_id, limit)}


@partner_router.get("")
def my_delegations(admin: PlatformPrincipal = Depends(get_partner_principal)):
    return {"items": service.current_for_partner(admin.partner_id)}


@partner_router.post("/{delegation_id}/token")
def open_tenant(delegation_id: int, request: Request, admin: PlatformPrincipal = Depends(get_partner_principal)):
    return service.issue_token(admin, delegation_id, client_ip(request))
