"""Payroll v2 set-up for a newly provisioned tenant.

Called once from company_service.provision_company, after the default legal entity, branch,
leave types and first administrator exist. Existing tenants are never touched here; they are
handled by migrations (docs/payroll/implementation/DECISIONS.md I-08, I-12).
"""

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.employee import Employee
from app.models.user import User


def provision_tenant(db: Session, company: Company, admin_user: User, admin_employee: Employee) -> None:
    from app.payroll.services import component_seed, org_starter, payroll_roles, settings_starter

    org_starter.provision(db, company)
    payroll_roles.grant_founder(db, company, admin_user)
    settings_starter.apply(db, company)
    # Country-neutral components of 3.3 §2.4 (BASIC, FIXED_ALLOWANCE, OT_*, …); packs add statutory ones later.
    component_seed.seed(db, company)
