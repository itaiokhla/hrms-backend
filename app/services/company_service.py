import re
import secrets
from datetime import date, timedelta
from typing import Optional

import pytz
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import bad_request
from app.core.security import utcnow
from app.database.db import bind_tenant
from app.models.company import Company
from app.models.employee import Department, Employee, Shift
from app.models.leave import ApprovalChainStep, LeaveType
from app.models.user import User
from app.services import rbac_service

DEFAULT_LEAVE_TYPES = [
    # code, name, annual quota, paid, unlimited
    ("casual", "Casual Leave", 10, True, False),
    ("sick", "Sick Leave", 12, True, False),
    ("earned", "Earned Leave", 15, True, False),
    ("maternity", "Maternity Leave", 90, True, False),
    ("paternity", "Paternity Leave", 7, True, False),
    ("unpaid", "Unpaid Leave", 0, False, True),
]


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "company"


def unique_slug(db: Session, name: str) -> str:
    base = slugify(name)
    slug = base
    while db.query(Company.id).filter(Company.slug == slug).first():
        slug = f"{base}-{secrets.token_hex(3)}"
    return slug


def validate_timezone(tz: Optional[str]) -> str:
    tz = tz or "Asia/Kolkata"
    if tz not in pytz.all_timezones_set:
        raise bad_request("Unknown timezone")
    return tz


def provision_company(
    db: Session,
    *,
    company_name: str,
    admin_full_name: str,
    admin_email: str,
    admin_password_hash: str,
    timezone: Optional[str] = None,
) -> tuple:
    """Create a tenant with its default configuration and first administrator.

    Must run on a system session (pre-authentication). Returns (company, user).
    """
    company = Company(
        name=company_name.strip(),
        slug=unique_slug(db, company_name),
        status="active",
        subscription_status="trialing",
        subscription_plan="trial",
        trial_ends_at=utcnow() + timedelta(days=settings.trial_days),
        timezone=validate_timezone(timezone),
    )
    db.add(company)
    db.flush()
    bind_tenant(db, company.id)

    roles = rbac_service.seed_company_roles(db, company.id)

    for code, name, quota, paid, unlimited in DEFAULT_LEAVE_TYPES:
        db.add(LeaveType(code=code, name=name, annual_quota=quota, is_paid=paid, is_unlimited=unlimited))
    # Default route: direct manager, then HR for requests of 3+ days.
    db.add(ApprovalChainStep(module="leave", step_order=1, approver_type="primary_manager"))
    db.add(
        ApprovalChainStep(
            module="leave", step_order=2, approver_type="role", role_id=roles["hr_admin"].id, min_days=3
        )
    )
    department = Department(name="General", description="Default department")
    shift = Shift(name="General Shift", start_time="09:00", end_time="18:00", duration_hours=9)
    db.add_all([department, shift])
    db.flush()

    user = User(
        email=admin_email.strip().lower(),
        full_name=admin_full_name.strip(),
        password_hash=admin_password_hash,
        status="active",
        password_changed_at=utcnow(),
    )
    db.add(user)
    db.flush()
    rbac_service.assign_role_by_key(db, company.id, user, "company_admin")

    first, _, last = admin_full_name.strip().partition(" ")
    db.add(
        Employee(
            user_id=user.id,
            employee_code="EMP-0001",
            first_name=first or admin_full_name,
            last_name=last or "-",
            email=user.email,
            department_id=department.id,
            shift_id=shift.id,
            position="Administrator",
            hire_date=today_for(company),
        )
    )
    db.flush()
    return company, user


def today_for(company: Company) -> date:
    return utcnow().astimezone(pytz.timezone(company.timezone or "UTC")).date()


def now_local(company: Company):
    return utcnow().astimezone(pytz.timezone(company.timezone or "UTC"))
