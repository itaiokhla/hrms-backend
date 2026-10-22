"""Funding accounts API (3.7 §6; 3.9 §4 Funding accounts).

Company accounts per legal entity that fund payment batches. Everything needs
payroll:funding_accounts:manage at COMPANY scope or the account's LEGAL_ENTITY; an entity outside the
caller's scope (or another tenant's) is a 404.

* ``GET /payroll/funding-accounts`` lists every account in scope (filters: entity, status, purpose);
  ``GET/POST /payroll/legal-entities/{id}/funding-accounts`` lists and creates per entity (step-up).
* ``PATCH /payroll/funding-accounts/{id}`` changes name, purpose, signatories, formats, provider profile
  or bank details (bank details need a fresh step-up); ``If-Match`` or ``row_version`` is required (409 on
  a stale version). ``POST .../deactivate`` (reason, step-up): accounts are never deleted.
* ``POST .../reveal`` returns the full number (payroll:bank_details:read_full, reason, step-up; audited
  BANK_DETAILS_REVEALED, rate limited, ``Cache-Control: no-store``, never stored for idempotent replay).

Responses mask account numbers. Idempotency keys hash the request with the numbers replaced by keyed
hashes (app.payroll.routes.bank_accounts.BankDetailsIn.idempotency_body).
"""

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.core.rate_limit import client_ip
from app.database.db import get_db
from app.models.payments import FUNDING_PURPOSES, FUNDING_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.routes.bank_accounts import NO_STORE, BankDetailsIn, RevealIn, _if_match
from app.payroll.services import funding_accounts as service
from app.payroll.services import idempotency

router = APIRouter(prefix="/api/v1", tags=["Funding accounts"])

Purpose = Literal[FUNDING_PURPOSES]  # type: ignore[valid-type]
Status = Literal[FUNDING_STATUSES]  # type: ignore[valid-type]


class FundingAccountIn(StrictModel):
    name: str = Field(..., min_length=1, max_length=100)
    purpose: Purpose = "PAYROLL"
    signatory_user_ids: List[int] = Field(..., min_length=1, max_length=service.MAX_SIGNATORIES)
    formats: List[str] = Field(default_factory=lambda: ["GENERIC_CSV"], min_length=1, max_length=10)
    provider_profile: Optional[str] = Field(None, max_length=40)
    details: BankDetailsIn

    def to_data(self) -> service.FundingData:
        return service.FundingData(name=self.name, purpose=self.purpose, signatory_user_ids=self.signatory_user_ids,
                                   formats=self.formats, provider_profile=self.provider_profile,
                                   details=self.details.to_details())

    def idempotency_body(self) -> dict:
        body = self.model_dump(mode="json", exclude={"details"})
        body["details"] = self.details.idempotency_body()
        return body


class FundingAccountPatch(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    purpose: Optional[Purpose] = None
    signatory_user_ids: Optional[List[int]] = Field(None, min_length=1, max_length=service.MAX_SIGNATORIES)
    formats: Optional[List[str]] = Field(None, min_length=1, max_length=10)
    provider_profile: Optional[str] = Field(None, max_length=40)
    details: Optional[BankDetailsIn] = None
    row_version: Optional[int] = Field(None, ge=1)

    def to_data(self) -> service.FundingData:
        return service.FundingData(name=self.name, purpose=self.purpose, signatory_user_ids=self.signatory_user_ids,
                                   formats=self.formats, provider_profile=self.provider_profile,
                                   details=self.details.to_details() if self.details is not None else None)

    def idempotency_body(self) -> dict:
        body = self.model_dump(mode="json", exclude={"details"})
        body["details"] = self.details.idempotency_body() if self.details is not None else None
        return body


class DeactivateIn(StrictModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


def _result(row) -> dict:
    return service.serialize(row)


@router.get("/payroll/funding-accounts")
def list_funding_accounts(
    legal_entity_id: Optional[int] = Query(None, ge=1),
    status: Optional[Status] = Query(None),
    purpose: Optional[Purpose] = Query(None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return {"items": service.list_accounts(db, principal, legal_entity_id=legal_entity_id, status=status,
                                           purpose=purpose)}


@router.get("/payroll/legal-entities/{legal_entity_id}/funding-accounts")
def entity_funding_accounts(
    legal_entity_id: int,
    status: Optional[Status] = Query(None),
    purpose: Optional[Purpose] = Query(None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return {"items": service.list_accounts(db, principal, legal_entity_id=legal_entity_id, status=status,
                                           purpose=purpose)}


@router.post("/payroll/legal-entities/{legal_entity_id}/funding-accounts", status_code=201)
def create_funding_account(legal_entity_id: int, data: FundingAccountIn, request: Request,
                           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        return 201, _result(service.create(db, principal, legal_entity_id, data.to_data()))

    return idempotency.execute(db, principal, request, handler,
                               body={**data.idempotency_body(), "legal_entity_id": legal_entity_id})


@router.get("/payroll/funding-accounts/{account_id}")
def get_funding_account(account_id: int, principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)):
    return service.get(db, principal, account_id)


@router.patch("/payroll/funding-accounts/{account_id}")
def update_funding_account(account_id: int, data: FundingAccountPatch, request: Request,
                           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.update(db, principal, account_id, data.to_data(), _if_match(request, data.row_version))
        return 200, _result(row)

    return idempotency.execute(db, principal, request, handler, body=data.idempotency_body())


@router.post("/payroll/funding-accounts/{account_id}/deactivate")
def deactivate_funding_account(account_id: int, data: DeactivateIn, request: Request,
                               principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.deactivate(db, principal, account_id, data.reason, _if_match(request, data.row_version))
        return 200, _result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/funding-accounts/{account_id}/reveal")
def reveal_funding_account(account_id: int, data: RevealIn, request: Request,
                           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Full details (read_full + reason + step-up). Never replayed from an idempotency store."""
    result = service.reveal(db, principal, account_id, data.reason, client_ip(request))
    db.commit()
    return JSONResponse(jsonable_encoder(result), headers=NO_STORE)


__all__: List[str] = ["router"]
