"""Role-aware dashboard.

The payload is assembled from the caller's permissions: every section is
computed with the same scoped services used elsewhere, and sections the
caller has no access to are simply absent.
"""

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import authz
from app.core.permission_registry import Scope
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.employee import Employee
from app.services import attendance_service, leave_service, payroll_service

router = APIRouter(prefix="/api/v1", tags=["Dashboard"])


def _beyond_self(principal: Principal, permission: str) -> bool:
    return bool(principal.scopes(permission) - {Scope.SELF.value})


@router.get("/dashboard")
def dashboard(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data: dict = {"sections": []}

    if principal.employee_id:
        data["me"] = {
            "attendance": attendance_service.my_today(db, principal),
            "leave_balance": leave_service.balances(db, principal.company_id, principal.employee_id, date.today().year),
        }
        if principal.has("payroll:read", Scope.SELF.value):
            payslips = payroll_service.my_payslips(db, principal)
            data["me"]["latest_payslip"] = payslips[0] if payslips else None
        data["sections"].append("me")

    if _beyond_self(principal, "employee:read"):
        scope = authz.employee_scope(db, principal, "employee:read")
        data["headcount"] = db.query(func.count(Employee.id)).filter(
            Employee.company_id == principal.company_id, Employee.is_active.is_(True), scope.clause(Employee.id)
        ).scalar()
        data["scope"] = "company" if scope.all_company else "team"
        data["sections"].append("people")

    if _beyond_self(principal, "attendance:read"):
        data["attendance"] = attendance_service.today_summary(db, principal)
        data["attendance_trend"] = attendance_service.trend(db, principal, 7)
        data["recent_activity"] = attendance_service.recent_activity(db, principal, 8)
        data["sections"].append("attendance")

    if _beyond_self(principal, "leave:read"):
        data["upcoming_leaves"] = leave_service.upcoming_approved(db, principal)
        data["sections"].append("leave")

    inbox = leave_service.approval_inbox(db, principal)
    data["pending_approvals"] = len(inbox)
    if inbox:
        data["sections"].append("approvals")

    if principal.has_company_scope("payroll:read"):
        today = date.today()
        data["payroll"] = payroll_service.summary(db, principal, today.year, today.month)
        data["sections"].append("payroll")
    return data
