"""Approvals inbox and decisions for payroll and HR modules other than leave (3.5 §11, 3.9 §4)."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.approvals import APPROVAL_REQUEST_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import approvals, idempotency

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll approvals"])

ModuleName = Literal[approvals.ENGINE_MODULES]  # type: ignore[valid-type]
StatusName = Literal[APPROVAL_REQUEST_STATUSES]  # type: ignore[valid-type]


class _OptionalRemarks(StrictModel):
    remarks: Optional[str] = Field(None, max_length=1000)

    @field_validator("remarks")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return (value.strip() or None) if value is not None else None


class ApproveBody(_OptionalRemarks):
    # The step the caller saw; a request that moved on since is a 409 (EC-RUN-08).
    expected_step: Optional[int] = Field(None, ge=1)


class RejectBody(StrictModel):
    remarks: str = Field(..., min_length=1, max_length=1000)
    expected_step: Optional[int] = Field(None, ge=1)

    @field_validator("remarks")
    @classmethod
    def _required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Give a reason for the rejection.")
        return value


class CancelBody(_OptionalRemarks):
    pass


@router.get("/approvals")
def list_approvals(
    module: Optional[ModuleName] = Query(None),
    status: Optional[StatusName] = Query(None),
    mine: bool = Query(True, description="Only requests awaiting the caller's decision"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = approvals.list_requests(
        db, principal, module=module, status=status, mine=mine, skip=skip, limit=limit
    )
    return {"total": total, "items": items}


@router.get("/approvals/{request_id}")
def get_approval(request_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return approvals.get_request(db, principal, request_id)


@router.post("/approvals/{request_id}/approve")
def approve(
    request_id: int,
    request: Request,
    data: Optional[ApproveBody] = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    data = data or ApproveBody()

    def handler():
        decided = approvals.decide(db, principal, request_id, "APPROVE", data.remarks, data.expected_step)
        return 200, approvals.action_result(decided)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/approvals/{request_id}/reject")
def reject(
    request_id: int,
    data: RejectBody,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    def handler():
        decided = approvals.decide(db, principal, request_id, "REJECT", data.remarks, data.expected_step)
        return 200, approvals.action_result(decided)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/approvals/{request_id}/cancel")
def cancel(
    request_id: int,
    request: Request,
    data: Optional[CancelBody] = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    data = data or CancelBody()

    def handler():
        return 200, approvals.action_result(approvals.cancel(db, principal, request_id, data.remarks))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())
