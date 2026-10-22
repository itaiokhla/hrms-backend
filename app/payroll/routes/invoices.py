"""Contractor invoices API (3.9 §4 Invoices; 3.6 §10): ``GET/POST /api/v1/payroll/invoices``, ``GET …/{id}``,
``POST …/{id}/issue|approve|credit``, ``GET …/{id}/download`` (signed link), self-billing agreements, and
``GET /api/v1/me/payroll/invoices`` for a contractor's own issued invoices.

Server-owned fields (status, numbers, amounts, approver) are never accepted; amounts come from the approved
tasks. Taxes are entered explicitly (decimal-string rates) and printed as entered.
"""

from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.core.rate_limit import limiter
from app.database.db import get_db
from app.models.pay_documents import INVOICE_KINDS, INVOICE_MODES, INVOICE_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import documents, downloads, idempotency
from app.payroll.services import invoices as service

router = APIRouter(prefix="/api/v1", tags=["Invoices"])

Status = Literal[INVOICE_STATUSES]  # type: ignore[valid-type]
Kind = Literal[INVOICE_KINDS]  # type: ignore[valid-type]
Mode = Literal[INVOICE_MODES]  # type: ignore[valid-type]


def _reason(value: str) -> str:
    if not value.strip():
        raise ValueError("Give a reason.")
    return value.strip()


class TaxIn(StrictModel):
    label: str = Field(min_length=1, max_length=60)
    rate_percent: str = Field(pattern=r"^\d{1,3}(\.\d{1,4})?$")
    reverse_charge: bool = False


class SellerIn(StrictModel):
    address: Optional[str] = Field(None, max_length=300)
    tax_registration: Optional[str] = Field(None, max_length=60)


class InvoiceIn(StrictModel):
    mode: Mode
    payee_employee_id: int = Field(ge=1)
    task_ids: List[int] = Field(min_length=1, max_length=service.MAX_TASKS)
    external_number: Optional[str] = Field(None, max_length=80)
    seller: Optional[SellerIn] = None
    taxes: List[TaxIn] = Field(default_factory=list, max_length=10)
    due_date: Optional[date] = None
    payment_terms: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=2000)


class VersionIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


class CreditIn(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _reason(value)


class AgreementIn(StrictModel):
    employee_id: int = Field(ge=1)
    legal_entity_id: int = Field(ge=1)
    reference: str = Field(min_length=1, max_length=100)
    effective_from: date
    effective_to: Optional[date] = None


class RevokeIn(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _reason(value)


@router.get("/payroll/invoices")
def list_invoices(
    status: Optional[Status] = Query(None),
    kind: Optional[Kind] = Query(None),
    mode: Optional[Mode] = Query(None),
    payee_employee_id: Optional[int] = Query(None, ge=1),
    legal_entity_id: Optional[int] = Query(None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return service.list_invoices(db, principal, status=status, kind=kind, mode=mode,
                                 payee_employee_id=payee_employee_id, legal_entity_id=legal_entity_id, skip=skip,
                                 limit=limit)


@router.post("/payroll/invoices", status_code=201)
def create_invoice(data: InvoiceIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    def handler():
        payload = data.model_dump(mode="json")
        payload["due_date"] = data.due_date
        row = service.create_invoice(db, principal, payload)
        return 201, service.invoice_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/invoices/{invoice_id}")
def get_invoice(invoice_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.invoice_json(service.get_for_staff(db, principal, invoice_id))


def _action(db: Session, principal: Principal, request: Request, invoice_id: int, action: str, body: dict,
            row_version: Optional[int], reason: Optional[str] = None):
    def handler():
        service.get_for_staff(db, principal, invoice_id)  # 404 before anything for ids out of scope
        row = service.transition(db, principal, invoice_id, action, reason=reason, expected_version=row_version)
        return 200, service.invoice_json(row)

    return idempotency.execute(db, principal, request, handler, body=body)


@router.post("/payroll/invoices/{invoice_id}/issue")
def issue_invoice(invoice_id: int, data: VersionIn, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    """Issue: gapless number from the issuer's sequence (EC-DOC-08) and the immutable document."""
    return _action(db, principal, request, invoice_id, "ISSUE", data.model_dump(mode="json"), data.row_version)


@router.post("/payroll/invoices/{invoice_id}/approve")
def approve_invoice(invoice_id: int, data: VersionIn, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    return _action(db, principal, request, invoice_id, "APPROVE", data.model_dump(mode="json"), data.row_version)


@router.post("/payroll/invoices/{invoice_id}/credit")
def credit_invoice(invoice_id: int, data: CreditIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    """Cancel by credit note: the original stays and its number is never reused (EC-DOC-09)."""
    return _action(db, principal, request, invoice_id, "CREDIT", data.model_dump(mode="json"), data.row_version,
                   reason=data.reason)


@router.get("/payroll/invoices/{invoice_id}/download")
def invoice_download_link(invoice_id: int, principal: Principal = Depends(get_principal),
                          db: Session = Depends(get_db)):
    limiter.check(f"payslip-link:{principal.company_id}:{principal.user_id}", 120, 60)
    row = service.get_for_staff(db, principal, invoice_id)
    if row.document_id is None:
        from app.core.errors import conflict

        raise conflict("A draft invoice has no document yet.", "NOT_ISSUED")
    doc, _ = documents.get_visible(db, principal, row.document_id)
    return downloads.issue_download(db, principal, doc)


@router.get("/payroll/self-billing-agreements")
def list_agreements(employee_id: Optional[int] = Query(None, ge=1), principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    return {"items": service.list_agreements(db, principal, employee_id=employee_id)}


@router.post("/payroll/self-billing-agreements", status_code=201)
def create_agreement(data: AgreementIn, request: Request, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    def handler():
        row = service.create_agreement(db, principal, employee_id=data.employee_id, legal_entity_id=data.legal_entity_id,
                                       reference=data.reference, effective_from=data.effective_from,
                                       effective_to=data.effective_to)
        return 201, service.agreement_json(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/payroll/self-billing-agreements/{agreement_id}/revoke")
def revoke_agreement(agreement_id: int, data: RevokeIn, request: Request,
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        return 200, service.agreement_json(service.revoke_agreement(db, principal, agreement_id, data.reason))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/me/payroll/invoices")
def my_invoices(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """A contractor's own issued invoices and credit notes (never drafts)."""
    return {"items": service.list_own(db, principal)}


@router.get("/me/payroll/invoices/{invoice_id}")
def my_invoice(invoice_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.invoice_json(service.get_own(db, principal, invoice_id), for_self=True)
