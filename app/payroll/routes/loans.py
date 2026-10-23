"""Loans and advances API (3.4 §15, 3.9 §4 Claims & loans).

Payroll staff: /api/v1/payroll/loans with approve, reject, cancel and the approved actions pause,
resume, restructure, prepay and waive (each action is a request decided through the approvals engine);
/api/v1/payroll/employees/{id}/loan-policy previews the limits that apply to an employee in scope.
Self-service: /api/v1/me/payroll/loans (apply for and read one's own). Disbursement is recorded from
payment evidence by the payments milestone (services.loans.record_disbursement), not through this API.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.variable_inputs import LOAN_KINDS, LOAN_STATUSES
from app.payroll.api_types import StrictModel, money_in_model
from app.payroll.routes.approvals import ApproveBody, RejectBody
from app.payroll.services import idempotency
from app.payroll.services import loans as service

router = APIRouter(prefix="/api/v1", tags=["Loans and advances"])

StatusName = Literal[LOAN_STATUSES]  # type: ignore[valid-type]
KindName = Literal[LOAN_KINDS]  # type: ignore[valid-type]
LoanMoneyIn = money_in_model("LoanMoneyIn", payroll_currency_only=True, allow_negative=False)


def _clean(value: Optional[str]) -> Optional[str]:
    return (value.strip() or None) if value is not None else None


class MyLoanIn(StrictModel):
    kind: KindName  # type: ignore[valid-type]
    amount: LoanMoneyIn  # type: ignore[valid-type]
    # Months for a loan (default: the policy maximum), runs for an advance (default: the policy's).
    tenure: Optional[int] = Field(None, ge=1, le=600)
    purpose: Optional[str] = Field(None, max_length=1000)

    @field_validator("purpose")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return _clean(value)

    def to_data(self, employee_id: int) -> service.LoanData:
        return service.LoanData(employee_id=employee_id, kind=self.kind, amount=self.amount.decimal,
                                currency=self.amount.currency, tenure=self.tenure, purpose=self.purpose)


class LoanIn(MyLoanIn):
    employee_id: int = Field(ge=1)


class ReasonBody(StrictModel):
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give a reason.")
        return value.strip()


class OptionalReasonBody(StrictModel):
    reason: Optional[str] = Field(None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _strip(cls, value: Optional[str]) -> Optional[str]:
        return _clean(value)


class RestructureBody(ReasonBody):
    tenure: int = Field(ge=1, le=600)


class PrepayBody(OptionalReasonBody):
    amount: LoanMoneyIn  # type: ignore[valid-type]
    reference: Optional[str] = Field(None, max_length=100)


class WaiveBody(ReasonBody):
    amount: LoanMoneyIn  # type: ignore[valid-type]


def _result(loan) -> dict:
    return {"id": loan.id, "status": loan.status, "row_version": loan.row_version,
            "approval_request_id": loan.approval_request_id,
            "issues": [i.get("code") for i in loan.application_issues or []]}


def _action_result(action) -> dict:
    return {"id": action.id, "loan_id": action.loan_id, "action": action.action, "status": action.status,
            "approval_request_id": action.approval_request_id}


# ---------------------------------------------------------------------------
# Payroll staff
# ---------------------------------------------------------------------------
@router.get("/payroll/loans")
def list_loans(
    status: Optional[StatusName] = Query(None),
    kind: Optional[KindName] = Query(None),
    employee_id: Optional[int] = Query(None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_loans(db, principal, status=status, kind=kind, employee_id=employee_id, skip=skip,
                                      limit=limit)
    return {"total": total, "items": items}


@router.post("/payroll/loans", status_code=201)
def create_loan(data: LoanIn, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        return 201, _result(service.apply_loan(db, principal, data.to_data(data.employee_id)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/payroll/loans/{loan_id}")
def get_loan(loan_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_loan(db, principal, loan_id)


@router.post("/payroll/loans/{loan_id}/approve")
def approve_loan(loan_id: int, request: Request, data: Optional[ApproveBody] = None,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or ApproveBody()

    def handler():
        return 200, _result(service.decide(db, principal, loan_id, "APPROVE", data.remarks, data.expected_step))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/loans/{loan_id}/reject")
def reject_loan(loan_id: int, data: RejectBody, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        return 200, _result(service.decide(db, principal, loan_id, "REJECT", data.remarks, data.expected_step))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/loans/{loan_id}/cancel")
def cancel_loan(loan_id: int, data: ReasonBody, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def handler():
        return 200, _result(service.cancel_loan(db, principal, loan_id, data.reason))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


def _action(db: Session, principal: Principal, request: Request, loan_id: int, action: service.ActionData,
            body: dict):
    def handler():
        return 202, _action_result(service.request_action(db, principal, loan_id, action))

    return idempotency.execute(db, principal, request, handler, body=body)


@router.post("/payroll/loans/{loan_id}/pause", status_code=202)
def pause_loan(loan_id: int, data: ReasonBody, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    return _action(db, principal, request, loan_id, service.ActionData("PAUSE", data.reason), data.model_dump())


@router.post("/payroll/loans/{loan_id}/resume", status_code=202)
def resume_loan(loan_id: int, request: Request, data: Optional[OptionalReasonBody] = None,
                principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or OptionalReasonBody()
    return _action(db, principal, request, loan_id, service.ActionData("RESUME", data.reason), data.model_dump())


@router.post("/payroll/loans/{loan_id}/restructure", status_code=202)
def restructure_loan(loan_id: int, data: RestructureBody, request: Request,
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _action(db, principal, request, loan_id, service.ActionData("RESTRUCTURE", data.reason, tenure=data.tenure),
                   data.model_dump())


@router.post("/payroll/loans/{loan_id}/prepay", status_code=202)
def prepay_loan(loan_id: int, data: PrepayBody, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    action = service.ActionData("PREPAY", data.reason, amount=data.amount.decimal, reference=data.reference,
                                currency=data.amount.currency)
    return _action(db, principal, request, loan_id, action, data.model_dump(mode="json"))


@router.post("/payroll/loans/{loan_id}/waive", status_code=202)
def waive_loan(loan_id: int, data: WaiveBody, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    action = service.ActionData("WAIVE", data.reason, amount=data.amount.decimal, currency=data.amount.currency)
    return _action(db, principal, request, loan_id, action, data.model_dump(mode="json"))


@router.get("/payroll/employees/{employee_id}/loan-policy")
def employee_loan_policy(employee_id: int, principal: Principal = Depends(get_principal),
                         db: Session = Depends(get_db)):
    """The loan and advance limits that apply to an employee in scope (payroll:loans:read; 404 outside it)."""
    return service.policy_preview(db, principal, employee_id)


# ---------------------------------------------------------------------------
# Self-service (GET /me/payroll/loan-policy lives in routes/me_payroll.py)
# ---------------------------------------------------------------------------
@router.get("/me/payroll/loans")
def my_loans(status: Optional[StatusName] = Query(None), skip: int = Query(0, ge=0),
             limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db)):
    total, items = service.list_loans(db, principal, own=True, status=status, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.post("/me/payroll/loans", status_code=201)
def apply_my_loan(data: MyLoanIn, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    def handler():
        employee_id = principal.employee_id if principal.employee_id is not None else 0
        return 201, _result(service.apply_loan(db, principal, data.to_data(employee_id), self_service=True))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/me/payroll/loans/{loan_id}")
def my_loan(loan_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_loan(db, principal, loan_id, own=True)
