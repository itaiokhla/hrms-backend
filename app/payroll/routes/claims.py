"""Reimbursement claims API (3.4 §14, 3.9 §4 Claims & loans).

Payroll staff: /api/v1/payroll/reimbursement-claims (scope of payroll:reimbursements:*).
Self-service: /api/v1/me/payroll/reimbursement-claims (the caller's own claims only).
State-changing responses are compact action results; amounts are read with GET.
"""

import hashlib
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.variable_inputs import CLAIM_STATUSES
from app.payroll.api_types import StrictModel, money_in_model
from app.payroll.routes.approvals import ApproveBody, CancelBody, RejectBody
from app.payroll.services import claims as service
from app.payroll.services import idempotency

router = APIRouter(prefix="/api/v1", tags=["Reimbursement claims"])

StatusName = Literal[CLAIM_STATUSES]  # type: ignore[valid-type]
ClaimMoneyIn = money_in_model("ClaimMoneyIn", payroll_currency_only=True, allow_negative=False)
FILE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class MyClaimIn(StrictModel):
    claim_type: str = Field(min_length=1, max_length=40)
    expense_date: date
    amount: ClaimMoneyIn  # type: ignore[valid-type]
    merchant: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)

    @field_validator("merchant", "description")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return (value.strip() or None) if value is not None else None

    def to_data(self, employee_id: int) -> service.ClaimData:
        return service.ClaimData(employee_id=employee_id, claim_type=self.claim_type, expense_date=self.expense_date,
                                 amount=self.amount.decimal, currency=self.amount.currency, merchant=self.merchant,
                                 description=self.description)


class ClaimIn(MyClaimIn):
    employee_id: int = Field(ge=1)


def _result(row) -> dict:
    return {"id": row.id, "status": row.status, "row_version": row.row_version,
            "approval_request_id": row.approval_request_id, "flags": [f.get("code") for f in row.flags or []]}


async def _read(file: UploadFile) -> bytes:
    return await file.read(app_settings.max_upload_size + 1)


def _file_body(file: UploadFile, data: bytes) -> dict:
    return {"file_name": file.filename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _receipt_result(receipt) -> dict:
    return {"id": receipt.id, "claim_id": receipt.claim_id, "file_name": receipt.file_name, "mime": receipt.mime,
            "bytes": receipt.bytes, "sha256": receipt.sha256}


# ---------------------------------------------------------------------------
# Payroll staff
# ---------------------------------------------------------------------------
@router.get("/payroll/reimbursement-claims")
def list_claims(
    status: Optional[StatusName] = Query(None),
    employee_id: Optional[int] = Query(None, ge=1),
    claim_type: Optional[str] = Query(None, max_length=40),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_claims(db, principal, status=status, employee_id=employee_id, claim_type=claim_type,
                                       skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.get("/payroll/reimbursement-claim-types")
def list_claim_types(
    employee_id: Optional[int] = Query(None, ge=1, description="Default: the caller"),
    on: Optional[date] = Query(None, alias="date"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """Claim types of the reimbursement policy resolved for the caller or an employee in scope (F-44)."""
    return service.claim_types(db, principal, employee_id, on)


@router.post("/payroll/reimbursement-claims", status_code=201)
def create_claim(data: ClaimIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 201, _result(service.submit_claim(db, principal, data.to_data(data.employee_id)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/reimbursement-claims/{claim_id}")
def get_claim(claim_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_claim(db, principal, claim_id)


@router.post("/payroll/reimbursement-claims/{claim_id}/approve")
def approve_claim(claim_id: int, request: Request, data: Optional[ApproveBody] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or ApproveBody()

    def handler():
        return 200, _result(service.decide_claim(db, principal, claim_id, "APPROVE", data.remarks, data.expected_step))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/reimbursement-claims/{claim_id}/reject")
def reject_claim(claim_id: int, data: RejectBody, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, _result(service.decide_claim(db, principal, claim_id, "REJECT", data.remarks, data.expected_step))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/reimbursement-claims/{claim_id}/receipts", status_code=201)
async def add_receipt(claim_id: int, request: Request, file: UploadFile = File(...),
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = await _read(file)

    def handler():
        receipt = service.add_receipt(db, principal, claim_id, file.filename or "", data)
        return 201, _receipt_result(receipt)

    return idempotency.execute(db, principal, request, handler, body=_file_body(file, data))


@router.get("/payroll/reimbursement-claims/{claim_id}/receipts/{receipt_id}")
def download_receipt(claim_id: int, receipt_id: int, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    path, receipt = service.receipt_file(db, principal, claim_id, receipt_id)
    db.commit()
    return FileResponse(path, media_type=receipt.mime, filename=receipt.file_name, headers=FILE_HEADERS)


# ---------------------------------------------------------------------------
# Self-service
# ---------------------------------------------------------------------------
@router.get("/me/payroll/reimbursement-claims")
def my_claims(
    status: Optional[StatusName] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_claims(db, principal, own=True, status=status, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/me/payroll/reimbursement-claims", status_code=201)
def submit_my_claim(data: MyClaimIn, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    def handler():
        employee_id = principal.employee_id if principal.employee_id is not None else 0
        return 201, _result(service.submit_claim(db, principal, data.to_data(employee_id), self_service=True))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/me/payroll/reimbursement-claims/{claim_id}")
def my_claim(claim_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_claim(db, principal, claim_id, own=True)


@router.post("/me/payroll/reimbursement-claims/{claim_id}/receipts", status_code=201)
async def add_my_receipt(claim_id: int, request: Request, file: UploadFile = File(...),
                         principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = await _read(file)

    def handler():
        receipt = service.add_receipt(db, principal, claim_id, file.filename or "", data, own=True)
        return 201, _receipt_result(receipt)

    return idempotency.execute(db, principal, request, handler, body=_file_body(file, data))


@router.get("/me/payroll/reimbursement-claims/{claim_id}/receipts/{receipt_id}")
def download_my_receipt(claim_id: int, receipt_id: int, principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db)):
    path, receipt = service.receipt_file(db, principal, claim_id, receipt_id, own=True)
    db.commit()
    return FileResponse(path, media_type=receipt.mime, filename=receipt.file_name, headers=FILE_HEADERS)


@router.post("/me/payroll/reimbursement-claims/{claim_id}/withdraw")
def withdraw_my_claim(claim_id: int, request: Request, data: Optional[CancelBody] = None,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or CancelBody()

    def handler():
        return 200, _result(service.withdraw_claim(db, principal, claim_id, data.remarks))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())
