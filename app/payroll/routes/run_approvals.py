"""Pay runs API, approval and finalization (3.9 §4 Runs) under /api/v1/payroll: submit, approve, reject, rework,
reopen, finalize, close, the self-pay line approval (EC-RUN-06) and their reads.

State-changing POSTs accept ``Idempotency-Key`` (required by finalize, EC-RUN-04) and a ``row_version``
(required by approve and reject: of two concurrent approvers one wins, the other gets 409, EC-RUN-08).
Finalize also needs a recent step-up (403 STEP_UP_REQUIRED) and the run label typed in ``confirm_label``.
Responses are compact action results without amounts, so idempotency records hold no pay data.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.api_types import StrictModel
from app.payroll.services import finalization, idempotency, run_approvals

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll run approval"])


def _text(value: Optional[str]) -> Optional[str]:
    return (value.strip() or None) if value is not None else None


def _required(value: str) -> str:
    if not value.strip():
        raise ValueError("Give a reason.")
    return value.strip()


class SubmitIn(StrictModel):
    comment: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("comment")
    @classmethod
    def _clean(cls, value):
        return _text(value)


class ApproveIn(StrictModel):
    row_version: int = Field(ge=1)
    comment: Optional[str] = Field(None, max_length=1000)
    # The approval step the caller saw; a request that moved on since is a 409 (EC-RUN-08).
    expected_step: Optional[int] = Field(None, ge=1)

    @field_validator("comment")
    @classmethod
    def _clean(cls, value):
        return _text(value)


class RejectIn(StrictModel):
    row_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    expected_step: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _clean(cls, value):
        return _required(value)


class ReworkIn(StrictModel):
    note: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("note")
    @classmethod
    def _clean(cls, value):
        return _text(value)


class ReopenIn(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)
    target: Optional[Literal["REVIEW_REQUIRED", "DRAFT"]] = None
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("reason")
    @classmethod
    def _clean(cls, value):
        return _required(value)


class FinalizeIn(StrictModel):
    confirm_label: str = Field(min_length=1, max_length=150)
    row_version: Optional[int] = Field(None, ge=1)


class CloseIn(StrictModel):
    note: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("note")
    @classmethod
    def _clean(cls, value):
        return _text(value)


class SelfLineIn(StrictModel):
    comment: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("comment")
    @classmethod
    def _clean(cls, value):
        return _text(value)


def _run_result(run) -> dict:
    return {"id": run.id, "number": run.number, "status": run.status, "row_version": run.row_version}


@router.get("/runs/{run_id}/approvals")
def run_approvals_list(run_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """The run's approval decisions (cleared rounds included) and own-pay lines awaiting another approver."""
    return run_approvals.approvals_of(db, principal, run_id)


@router.get("/runs/{run_id}/finalization")
def finalization_status(run_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """What still blocks finalization (the finalize dialog's checklist)."""
    return finalization.finalization_status(db, principal, run_id)


@router.post("/runs/{run_id}/submit")
def submit(run_id: int, request: Request, data: Optional[SubmitIn] = None,
           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or SubmitIn()

    def handler():
        run = run_approvals.submit_run(db, principal, run_id, comment=data.comment, expected_version=data.row_version)
        return 200, {**_run_result(run), "approval_request_id": run.approval_request_id}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/approve")
def approve(run_id: int, data: ApproveIn, request: Request, principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)):
    def handler():
        result = run_approvals.approve_run(db, principal, run_id, comment=data.comment,
                                           expected_version=data.row_version, expected_step=data.expected_step)
        decided = result["request"]
        return 200, {**_run_result(result["run"]), "approval_request": {
            "id": decided.id, "status": decided.status, "current_step": decided.current_step},
            "self_line_pending": result["self_line_pending"]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/reject")
def reject(run_id: int, data: RejectIn, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    def handler():
        return 200, _run_result(run_approvals.reject_run(db, principal, run_id, reason=data.reason,
                                                         expected_version=data.row_version,
                                                         expected_step=data.expected_step))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/rework")
def rework(run_id: int, request: Request, data: Optional[ReworkIn] = None,
           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or ReworkIn()

    def handler():
        return 200, _run_result(run_approvals.rework_run(db, principal, run_id, note=data.note,
                                                         expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/reopen")
def reopen(run_id: int, data: ReopenIn, request: Request, principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    def handler():
        return 200, _run_result(run_approvals.reopen_run(db, principal, run_id, reason=data.reason,
                                                         target=data.target, expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/finalize")
def finalize(run_id: int, data: FinalizeIn, request: Request, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    """Step-up MFA, the typed label and an Idempotency-Key; a retry with the same key replays (EC-RUN-04)."""
    key = idempotency.key_from(request)

    def handler():
        result = finalization.finalize_run(db, principal, run_id, confirm_label=data.confirm_label,
                                           idempotency_key=key, expected_version=data.row_version)
        run = result["run"]
        return 200, {**_run_result(run), "finalized_at": run.finalized_at.isoformat() if run.finalized_at else None,
                     "outbox_event_id": result["outbox_event_id"]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/close")
def close(run_id: int, request: Request, data: Optional[CloseIn] = None,
          principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or CloseIn()

    def handler():
        result = finalization.close_run(db, principal, run_id, note=data.note, expected_version=data.row_version)
        return 200, {**_run_result(result["run"]),
                     "carried_holds": [{k: v for k, v in h.items() if k != "net"} for h in result["carried_holds"]]}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/runs/{run_id}/employees/{run_employee_id}/approve-self-line")
def approve_self_line(run_id: int, run_employee_id: int, request: Request, data: Optional[SelfLineIn] = None,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or SelfLineIn()

    def handler():
        row = run_approvals.approve_self_line(db, principal, run_id, run_employee_id, comment=data.comment,
                                              expected_version=data.row_version)
        return 200, {"id": row.id, "employee_id": row.employee_id,
                     "self_line_approved_by_user_id": row.self_line_approved_by_user_id, "row_version": row.row_version}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())
