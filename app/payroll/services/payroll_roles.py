"""Explicit payroll role for the founding administrator of a new tenant (decision I-08).

Full access never implies payroll-confidential permissions (3.9 §5.1, contract §9), so a new tenant
would otherwise have nobody able to run payroll. The founder gets the ordinary payroll_manager role:
an audited assignment shown in Roles & access that any administrator can remove.
"""

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.user import User
from app.payroll import audit as payroll_audit
from app.services import rbac_service
from app.services.audit_log_service import AuditLogService

FOUNDER_ROLE_KEY = "payroll_manager"


def grant_founder(db: Session, company: Company, admin_user: User) -> None:
    rbac_service.assign_role_by_key(db, company.id, admin_user, FOUNDER_ROLE_KEY)
    AuditLogService.log(
        db,
        None,
        payroll_audit.PAYROLL_ROLES,
        payroll_audit.ROLE_GRANTED,
        f"Granted the Payroll Manager role to {admin_user.email}: the explicit payroll role of the founding "
        "administrator, because full access does not include payroll data (decision I-08). It can be removed "
        "in Roles & access.",
        "user",
        admin_user.id,
        company_id=company.id,
        actor_type="system",
        details={"role": FOUNDER_ROLE_KEY, "reason": "FOUNDING_ADMINISTRATOR", "decision": "I-08"},
    )
