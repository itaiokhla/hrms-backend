"""Nominees and legal heirs of a deceased employee's final settlement (3.5 §15, 3.7 §2.1; EC-BNK-15).

HR with ``payroll:bank_details:edit`` (and read access in the case's scope) records the nominees of a DEATH
final-settlement case, their shares and their bank accounts; another person with ``payroll:bank_details:approve``
approves each account through the bank change requests (``/payroll/bank-change-requests/{id}/approve`` or the
approval inbox). Every change needs a fresh step-up (403 STEP_UP_REQUIRED). Responses carry masked account
numbers only; the full number is revealed through ``/payroll/bank-accounts/{id}/reveal`` (read_full, reason,
step-up). Nominees outside the caller's scope are 404.
"""

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.nominees import NOMINEE_KINDS, NOMINEE_STATUSES
from app.models.payments import CHANGE_STATUSES
from app.payroll.api_types import DecimalStr, StrictModel
from app.payroll.routes.bank_accounts import BankAccountIn, _change_result, _if_match, _meta
from app.payroll.services import bank_accounts, idempotency
from app.payroll.services import nominees as service

router = APIRouter(prefix="/api/v1", tags=["Nominees"])

Kind = Literal[NOMINEE_KINDS]  # type: ignore[valid-type]
Status = Literal[NOMINEE_STATUSES]  # type: ignore[valid-type]
ChangeStatus = Literal[CHANGE_STATUSES]  # type: ignore[valid-type]


def _strip(value: Optional[str]) -> Optional[str]:
    return (value.strip() or None) if value is not None else None


class NomineeIn(StrictModel):
    final_settlement_case_id: int = Field(..., ge=1)
    kind: Kind = "NOMINEE"
    name: str = Field(..., min_length=1, max_length=140)
    relationship: str = Field(..., min_length=1, max_length=60)
    proof_ref: str = Field(..., min_length=1, max_length=300)
    share_percent: DecimalStr
    country: str = Field(..., min_length=2, max_length=2)
    email: Optional[str] = Field(None, max_length=254)

    _clean = field_validator("email")(classmethod(lambda cls, v: _strip(v)))


class NomineePatch(StrictModel):
    kind: Optional[Kind] = None
    name: Optional[str] = Field(None, min_length=1, max_length=140)
    relationship: Optional[str] = Field(None, min_length=1, max_length=60)
    proof_ref: Optional[str] = Field(None, min_length=1, max_length=300)
    share_percent: Optional[DecimalStr] = None
    country: Optional[str] = Field(None, min_length=2, max_length=2)
    email: Optional[str] = Field(None, max_length=254)
    row_version: Optional[int] = Field(None, ge=1)

    _clean = field_validator("email")(classmethod(lambda cls, v: _strip(v)))


class EndIn(StrictModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


# Static paths first: they must not be read as a nominee id.
@router.get("/payroll/nominees/cases/{case_id}")
def case_nominees(case_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """The nominees of a final-settlement case with their shares and readiness."""
    return service.list_for_case(db, principal, case_id)


@router.get("/payroll/nominees/cases/{case_id}/payments")
def case_payments(case_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """The settlement obligation and its payment per nominee (masked)."""
    return service.payments_of_case(db, principal, case_id)


@router.get("/payroll/nominees/bank-change-requests")
def change_requests(
    status: Optional[ChangeStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Bank change requests of nominee accounts (approve or reject them at /payroll/bank-change-requests/{id})."""
    total, items = service.list_changes(db, principal, status=status, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.get("/payroll/nominees")
def list_nominees(
    case_id: Optional[int] = Query(None, ge=1),
    employee_id: Optional[int] = Query(None, ge=1),
    status: Optional[Status] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_nominees(db, principal, case_id=case_id, employee_id=employee_id, status=status,
                                         skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/payroll/nominees", status_code=201)
def create_nominee(data: NomineeIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    def handler():
        nominee = service.create_nominee(db, principal, data.final_settlement_case_id, service.NomineeData(
            name=data.name, relationship=data.relationship, proof_ref=data.proof_ref,
            share_percent=data.share_percent, country=data.country, kind=data.kind, email=data.email))
        return 201, {"id": nominee.id, "final_settlement_case_id": nominee.final_settlement_case_id,
                     "status": nominee.status, "row_version": nominee.row_version}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/nominees/{nominee_id}")
def get_nominee(nominee_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_one(db, principal, nominee_id)


@router.patch("/payroll/nominees/{nominee_id}")
def update_nominee(nominee_id: int, data: NomineePatch, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        changes = data.model_dump(exclude_unset=True, exclude={"row_version"})
        nominee = service.update_nominee(db, principal, nominee_id, changes,
                                         expected_row_version=_if_match(request, data.row_version))
        return 200, {"id": nominee.id, "status": nominee.status, "row_version": nominee.row_version}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/payroll/nominees/{nominee_id}/end")
def end_nominee(nominee_id: int, data: EndIn, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        nominee = service.end_nominee(db, principal, nominee_id, data.reason,
                                      expected_row_version=_if_match(request, data.row_version))
        return 200, {"id": nominee.id, "status": nominee.status, "row_version": nominee.row_version}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/payroll/nominees/{nominee_id}/bank-accounts")
def nominee_accounts(nominee_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return {"items": bank_accounts.payee_accounts(db, principal, "nominee", nominee_id)}


@router.post("/payroll/nominees/{nominee_id}/bank-accounts", status_code=201)
def submit_nominee_account(nominee_id: int, data: BankAccountIn, request: Request,
                           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Submit a bank account of a nominee: step-up, checks, approval by another person, cooling-off."""
    def handler():
        change = bank_accounts.submit_account(db, principal, payee_type="NOMINEE", payee_id=nominee_id,
                                              meta=_meta(request), **data.submit_kwargs())
        return 201, _change_result(change)

    body = {**data.idempotency_body(), "payee": f"NOMINEE:{nominee_id}"}
    return idempotency.execute(db, principal, request, handler, body=body)


@router.get("/payroll/nominees/{nominee_id}/statements")
def nominee_statements(nominee_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """The settlement statements issued for the nominee (open them with /payroll/documents/{id})."""
    return {"items": service.statements_of(db, principal, nominee_id)}


@router.post("/payroll/nominees/{nominee_id}/statements", status_code=201)
def issue_nominee_statement(nominee_id: int, request: Request, principal: Principal = Depends(get_principal),
                            db: Session = Depends(get_db)):
    def handler():
        return 201, {"items": service.issue_statement(db, principal, nominee_id)}

    return idempotency.execute(db, principal, request, handler, body={"nominee_id": nominee_id})


__all__: List[str] = ["router"]
