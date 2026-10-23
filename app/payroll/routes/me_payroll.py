"""Self-service payroll context (3.8 self-service pages).

* ``GET /api/v1/me/payroll/preferences``: payslip query / PDF protection settings, the pay group with its
  currency and next pay date, and the bank account policy, all resolved for the caller's own employee
  (app.payroll.services.self_service). 404 without an employee record.
* ``GET /api/v1/me/payroll/loan-policy``: the loan and advance limits that apply to the caller today and
  their own usage (app.payroll.services.loans.policy_preview; needs payroll:loans:read or :manage covering
  the caller, which the employee role holds at SELF). 404 without an employee record.

Both are read-only and only ever describe the caller's own employee; there is no id to tamper with.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import loans, self_service

router = APIRouter(prefix="/api/v1", tags=["My payroll"])


@router.get("/me/payroll/preferences")
def my_payroll_preferences(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return self_service.preferences(db, principal)


@router.get("/me/payroll/loan-policy")
def my_loan_policy(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return loans.policy_preview(db, principal, own=True)
