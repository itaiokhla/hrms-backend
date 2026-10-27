"""Bank accounts API (3.7 §2-§5, §12; 3.9 §4 Bank accounts).

Payroll staff: /api/v1/payroll/bank-accounts, /payroll/payees/{type}/{id}/bank-accounts and
/payroll/bank-change-requests (scope of payroll:bank_details:*). Self-service: /api/v1/me/payroll/bank-accounts
and /me/payroll/bank-change-requests (the caller's own records only).

Every response masks account numbers; only POST .../reveal returns them (read_full, reason, step-up,
audited, never stored as an idempotent replay, ``Cache-Control: no-store``). Changes need a fresh step-up
(403 STEP_UP_REQUIRED). Idempotency keys hash the request with the numbers replaced by keyed hashes, and
replays store compact masked results only.
"""

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import concurrency
from app.core.principal import Principal, get_principal
from app.core.rate_limit import client_ip
from app.core.security import hash_token
from app.database.db import get_db
from app.models.payments import BANK_ACCOUNT_STATUSES, CHANGE_STATUSES, VERIFICATION_STATUSES
from app.payroll.api_types import DecimalStr, StrictModel
from app.payroll.services import bank_accounts as service
from app.payroll.services import idempotency

router = APIRouter(prefix="/api/v1", tags=["Bank accounts"])

AccountStatus = Literal[BANK_ACCOUNT_STATUSES]  # type: ignore[valid-type]
VerificationStatus = Literal[VERIFICATION_STATUSES]  # type: ignore[valid-type]
ChangeStatus = Literal[CHANGE_STATUSES]  # type: ignore[valid-type]
PayeeType = Literal["employee", "contractor", "nominee"]
NO_STORE = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


def _strip(value: Optional[str]) -> Optional[str]:
    return (value.strip() or None) if value is not None else None


class RoutingIn(StrictModel):
    ifsc: Optional[str] = Field(None, max_length=40)
    sort_code: Optional[str] = Field(None, max_length=40)
    aba: Optional[str] = Field(None, max_length=40)
    bsb: Optional[str] = Field(None, max_length=40)
    wps_routing_code: Optional[str] = Field(None, max_length=40)
    bic: Optional[str] = Field(None, max_length=40)
    bank_code: Optional[str] = Field(None, max_length=40)
    branch_code: Optional[str] = Field(None, max_length=40)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None and v.strip()}


class AllocationBody(StrictModel):
    type: Literal["PRIMARY", "PERCENT", "FIXED", "REMAINDER"]
    value: Optional[DecimalStr] = None
    order: int = Field(0, ge=0, le=99)

    def to_data(self) -> service.AllocationIn:
        return service.AllocationIn(type=self.type, value=self.value, order=self.order)


class BankDetailsIn(StrictModel):
    holder_name: str = Field(..., min_length=1, max_length=140)
    country: str = Field(..., min_length=2, max_length=2)
    currency: str = Field(..., min_length=3, max_length=3)
    account_number: Optional[str] = Field(None, max_length=64)
    iban: Optional[str] = Field(None, max_length=64)
    routing: RoutingIn = Field(default_factory=RoutingIn)
    account_type: Optional[str] = Field(None, max_length=10)
    bank_name: Optional[str] = Field(None, max_length=140)
    bank_address: Optional[str] = Field(None, max_length=300)

    _clean = field_validator("account_number", "iban", "account_type", "bank_name", "bank_address")(
        classmethod(lambda cls, v: _strip(v)))

    def to_details(self, branch_name: Optional[str] = None) -> service.BankDetails:
        return service.BankDetails(
            holder_name=self.holder_name, country=self.country, currency=self.currency,
            account_number=self.account_number, iban=self.iban, routing=self.routing.to_dict(),
            account_type=self.account_type, bank_name=self.bank_name, branch_name=branch_name,
            bank_address=self.bank_address)

    def idempotency_body(self) -> dict:
        """The request for idempotency hashing, with the numbers replaced by keyed hashes."""
        body = self.model_dump(mode="json")
        for key in ("account_number", "iban"):
            if body.get(key):
                body[key] = hash_token(f"bank-idem|{body[key]}")
        return body


class BankAccountIn(BankDetailsIn):
    branch_name: Optional[str] = Field(None, max_length=140)
    allocation: Optional[AllocationBody] = None
    replaces_account_id: Optional[int] = Field(None, ge=1)

    def submit_kwargs(self) -> dict:
        return {"details": self.to_details(self.branch_name),
                "allocation": self.allocation.to_data() if self.allocation is not None else None,
                "replaces_account_id": self.replaces_account_id}


class ReasonIn(StrictModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class VerifyIn(StrictModel):
    method: Literal["FORMAT", "DOCUMENT_REVIEW", "PENNY_DROP", "PROVIDER_NAME_MATCH"]
    decision: Optional[Literal["PASS", "FAIL"]] = None
    evidence_id: Optional[int] = Field(None, ge=1)
    reason: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class ReviewIn(StrictModel):
    action: Literal["CLEAR_REVIEW", "MARK_VERIFIED", "MARK_FAILED", "RESUBMIT"]
    reason: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class RevealIn(StrictModel):
    reason: str = Field(..., min_length=1, max_length=500)


class ApproveIn(StrictModel):
    remarks: Optional[str] = Field(None, max_length=1000)
    expected_step: Optional[int] = Field(None, ge=1)
    waive_cooling_off_reason: Optional[str] = Field(None, max_length=1000)


class RejectIn(StrictModel):
    remarks: str = Field(..., min_length=1, max_length=1000)
    expected_step: Optional[int] = Field(None, ge=1)


class SecurityReviewIn(StrictModel):
    decision: Literal["RELEASE", "REJECT"]
    reason: str = Field(..., min_length=1, max_length=1000)


class CancelIn(StrictModel):
    remarks: Optional[str] = Field(None, max_length=1000)


def _meta(request: Request) -> service.ClientMeta:
    return service.ClientMeta(ip_address=client_ip(request), user_agent=request.headers.get("user-agent"))


def _if_match(request: Request, body_version: Optional[int]) -> Optional[int]:
    return concurrency.if_match(request, body_version)


def _change_result(change, *, for_self: bool = False) -> dict:
    data = {"id": change.id, "change_request_id": change.id, "bank_account_id": change.bank_account_id,
            "status": change.status, "approval_request_id": change.approval_request_id,
            "row_version": change.row_version}
    if not for_self:
        data["risk_flags"] = [f.get("code") for f in change.risk_flags or []]
    return data


def _account_result(row) -> dict:
    return {"id": row.id, "status": row.status, "verification_status": row.verification_status,
            "account_masked": service.masked_number(row), "usable_from": row.usable_from,
            "payment_hold": row.payment_hold, "row_version": row.row_version}


async def _read(file: UploadFile) -> bytes:
    return await file.read(app_settings.max_upload_size + 1)


def _file_body(file: UploadFile, data: bytes) -> dict:
    import hashlib

    return {"file_name": file.filename, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _evidence_result(event) -> dict:
    return {"id": event.id, "bank_account_id": event.bank_account_id, "file_name": event.evidence_file_name,
            "mime": event.evidence_mime, "bytes": event.evidence_bytes, "sha256": event.evidence_sha256}


# ---------------------------------------------------------------------------
# Payroll staff: accounts
# ---------------------------------------------------------------------------
@router.get("/payroll/bank-accounts")
def list_accounts(
    employee_id: Optional[int] = Query(None, ge=1),
    status: Optional[AccountStatus] = Query(None),
    verification_status: Optional[VerificationStatus] = Query(None),
    payment_hold: Optional[bool] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_accounts(db, principal, employee_id=employee_id, status=status,
                                         verification_status=verification_status, payment_hold=payment_hold,
                                         skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.get("/payroll/bank-accounts/{account_id}")
def get_account(account_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_account(db, principal, account_id)


@router.get("/payroll/payees/{payee_type}/{payee_id}/bank-accounts")
def payee_accounts(payee_type: PayeeType, payee_id: int, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return {"items": service.payee_accounts(db, principal, payee_type, payee_id)}


@router.post("/payroll/payees/{payee_type}/{payee_id}/bank-accounts", status_code=201)
def create_payee_account(payee_type: PayeeType, payee_id: int, data: BankAccountIn, request: Request,
                         principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        change = service.submit_account(db, principal, payee_type=service.payee_type_of(payee_type),
                                        payee_id=payee_id, meta=_meta(request), **data.submit_kwargs())
        return 201, _change_result(change)

    body = {**data.idempotency_body(), "payee": f"{payee_type}:{payee_id}"}
    return idempotency.execute(db, principal, request, handler, body=body)


@router.post("/payroll/bank-accounts/{account_id}/deactivate")
def deactivate_account(account_id: int, data: ReasonIn, request: Request,
                       principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.deactivate_account(db, principal, account_id, data.reason,
                                         expected_row_version=_if_match(request, data.row_version))
        return 200, _account_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/bank-accounts/{account_id}/waive-cooling-off")
def waive_cooling_off(account_id: int, data: ReasonIn, request: Request,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.waive_cooling_off(db, principal, account_id, data.reason,
                                        expected_row_version=_if_match(request, data.row_version))
        return 200, _account_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/bank-accounts/{account_id}/evidence", status_code=201)
async def add_evidence(account_id: int, request: Request, file: UploadFile = File(...),
                       principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = await _read(file)

    def handler():
        return 201, _evidence_result(service.add_evidence(db, principal, account_id, file.filename or "", data))

    return idempotency.execute(db, principal, request, handler, body=_file_body(file, data))


@router.get("/payroll/bank-accounts/{account_id}/evidence/{verification_id}")
def download_evidence(account_id: int, verification_id: int, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    path, event = service.evidence_file(db, principal, account_id, verification_id)
    db.commit()
    return FileResponse(path, media_type=event.evidence_mime, filename=event.evidence_file_name, headers=NO_STORE)


@router.post("/payroll/bank-accounts/{account_id}/verify")
def verify_account(account_id: int, data: VerifyIn, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row, event = service.verify_account(db, principal, account_id, data.method, decision=data.decision,
                                            evidence_id=data.evidence_id, reason=data.reason,
                                            expected_row_version=_if_match(request, data.row_version))
        return 200, {**_account_result(row), "verification": {
            "id": event.id, "method": event.method, "result": event.result, "provider": event.provider,
            "sandbox": event.provider == service.SANDBOX,
            "label": service.SANDBOX_LABEL if event.provider == service.SANDBOX else None,
            "name_match_score": str(event.name_match_score) if event.name_match_score is not None else None}}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/bank-accounts/{account_id}/review")
def review_account(account_id: int, data: ReviewIn, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.review_account(db, principal, account_id, data.action, data.reason,
                                     expected_row_version=_if_match(request, data.row_version))
        return 200, _account_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/bank-accounts/{account_id}/reveal")
def reveal_account(account_id: int, data: RevealIn, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Full details (read_full + reason + step-up). Never replayed from an idempotency store."""
    result = service.reveal_account(db, principal, account_id, data.reason, client_ip(request))
    db.commit()
    return JSONResponse(jsonable_encoder(result), headers=NO_STORE)


# ---------------------------------------------------------------------------
# Payroll staff: change requests
# ---------------------------------------------------------------------------
@router.get("/payroll/bank-change-requests")
def list_change_requests(
    status: Optional[ChangeStatus] = Query(None),
    employee_id: Optional[int] = Query(None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_change_requests(db, principal, status=status, employee_id=employee_id, skip=skip,
                                                limit=limit)
    return {"total": total, "items": items}


@router.get("/payroll/bank-change-requests/{change_id}")
def get_change_request(change_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_change_request(db, principal, change_id)


@router.post("/payroll/bank-change-requests/{change_id}/approve")
def approve_change(change_id: int, request: Request, data: Optional[ApproveIn] = None,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or ApproveIn()

    def handler():
        change = service.decide_change_request(db, principal, change_id, "APPROVE", data.remarks, data.expected_step,
                                               waive_cooling_off_reason=data.waive_cooling_off_reason)
        return 200, _change_result(change)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/bank-change-requests/{change_id}/reject")
def reject_change(change_id: int, data: RejectIn, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    def handler():
        change = service.decide_change_request(db, principal, change_id, "REJECT", data.remarks, data.expected_step)
        return 200, _change_result(change)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/bank-change-requests/{change_id}/security-review")
def security_review(change_id: int, data: SecurityReviewIn, request: Request,
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        change = service.security_review(db, principal, change_id, data.decision, data.reason)
        return 200, _change_result(change)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


# ---------------------------------------------------------------------------
# Self-service
# ---------------------------------------------------------------------------
@router.get("/me/payroll/bank-accounts")
def my_accounts(
    status: Optional[AccountStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_accounts(db, principal, own=True, status=status, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/me/payroll/bank-accounts", status_code=201)
def submit_my_account(data: BankAccountIn, request: Request, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    def handler():
        change = service.submit_account(db, principal, meta=_meta(request), self_service=True, **data.submit_kwargs())
        return 201, _change_result(change, for_self=True)

    return idempotency.execute(db, principal, request, handler, body=data.idempotency_body())


@router.get("/me/payroll/bank-accounts/{account_id}")
def my_account(account_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_account(db, principal, account_id, own=True)


@router.post("/me/payroll/bank-accounts/{account_id}/deactivate")
def deactivate_my_account(account_id: int, data: ReasonIn, request: Request,
                          principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    def handler():
        row = service.deactivate_account(db, principal, account_id, data.reason, own=True,
                                         expected_row_version=_if_match(request, data.row_version))
        return 200, _account_result(row)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/me/payroll/bank-accounts/{account_id}/evidence", status_code=201)
async def add_my_evidence(account_id: int, request: Request, file: UploadFile = File(...),
                          principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = await _read(file)

    def handler():
        event = service.add_evidence(db, principal, account_id, file.filename or "", data, own=True)
        return 201, _evidence_result(event)

    return idempotency.execute(db, principal, request, handler, body=_file_body(file, data))


@router.get("/me/payroll/bank-change-requests")
def my_change_requests(
    status: Optional[ChangeStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_change_requests(db, principal, own=True, status=status, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.get("/me/payroll/bank-change-requests/{change_id}")
def my_change_request(change_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_change_request(db, principal, change_id, own=True)


@router.post("/me/payroll/bank-change-requests/{change_id}/cancel")
def cancel_my_change(change_id: int, request: Request, data: Optional[CancelIn] = None,
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or CancelIn()

    def handler():
        return 200, _change_result(service.cancel_change_request(db, principal, change_id, data.remarks),
                                   for_self=True)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


__all__: List[str] = ["router"]
