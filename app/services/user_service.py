"""Company user management.

The company always comes from the administrator's session. Accounts are
created through invitations (the administrator never chooses or learns the
user's password), and every change to who-can-do-what is validated by the
delegation rules in rbac_service.
"""

from typing import List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal, load_grants
from app.core.security import utcnow
from app.database.db import system_session
from app.models.company import Company
from app.models.employee import Employee
from app.models.rbac import Role, ScopeAssignment, UserRoleAssignment
from app.models.user import User, UserSession
from app.services import auth_service, domain_service, location_service, rbac_service
from app.services.audit_log_service import AuditLogService
from app.services.email_service import EmailService


def _get(db: Session, principal: Principal, user_id: int) -> User:
    user = (
        db.query(User)
        .options(selectinload(User.role_assignments))
        .filter(User.company_id == principal.company_id, User.id == user_id)
        .first()
    )
    if user is None:
        raise not_found("User")
    return user


def _is_protected_holder(db: Session, user: User) -> bool:
    role_ids = [a.role_id for a in user.role_assignments]
    if not role_ids:
        return False
    return db.query(Role.id).filter(Role.id.in_(role_ids), Role.is_protected.is_(True)).first() is not None


def _guard_target(db: Session, principal: Principal, user: User) -> None:
    if user.id == principal.user_id:
        raise forbidden("Use your own profile settings to change your account.")
    if _is_protected_holder(db, user) and not principal.is_company_admin:
        raise forbidden("Only company administrators can manage another administrator's account.")


def serialize(db: Session, user: User, roles_by_id: dict, employee: Optional[Employee]) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "status": user.status,
        "mfa_enabled": user.mfa_enabled,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat(),
        "roles": [
            {"id": a.role_id, "name": roles_by_id.get(a.role_id, {}).get("name"),
             "is_protected": roles_by_id.get(a.role_id, {}).get("is_protected", False)}
            for a in user.role_assignments
        ],
        "employee": (
            {"id": employee.id, "full_name": employee.full_name, "employee_code": employee.employee_code,
             "position": employee.position, "department": employee.department.name if employee.department else None}
            if employee
            else None
        ),
    }


def list_users(db: Session, principal: Principal, q: Optional[str], status: Optional[str], role_id: Optional[int],
               skip: int, limit: int):
    query = db.query(User).options(selectinload(User.role_assignments)).filter(User.company_id == principal.company_id)
    if q:
        term = f"%{q.strip()}%"
        query = query.filter(or_(User.email.ilike(term), User.full_name.ilike(term)))
    if status:
        query = query.filter(User.status == status)
    if role_id:
        query = query.filter(User.id.in_(db.query(UserRoleAssignment.user_id).filter(UserRoleAssignment.role_id == role_id)))
    total = query.order_by(None).count()
    users = query.order_by(User.full_name, User.id).offset(skip).limit(limit).all()
    roles = {r.id: {"name": r.name, "is_protected": r.is_protected} for r in db.query(Role).filter(Role.company_id == principal.company_id)}
    employees = {
        e.user_id: e
        for e in db.query(Employee).filter(Employee.user_id.in_([u.id for u in users])).all()
    } if users else {}
    return total, [serialize(db, u, roles, employees.get(u.id)) for u in users]


def get_user(db: Session, principal: Principal, user_id: int) -> dict:
    user = _get(db, principal, user_id)
    roles = {r.id: {"name": r.name, "is_protected": r.is_protected} for r in db.query(Role).filter(Role.company_id == principal.company_id)}
    employee = db.query(Employee).filter(Employee.user_id == user.id).first()
    data = serialize(db, user, roles, employee)
    data["access"] = rbac_service.access_preview(db, user)
    data["active_sessions"] = db.query(func.count(UserSession.id)).filter(
        UserSession.user_id == user.id, UserSession.revoked_at.is_(None), UserSession.expires_at > utcnow()
    ).scalar()
    data["assigned_employee_ids"] = [
        r[0] for r in db.query(ScopeAssignment.employee_id).filter(ScopeAssignment.user_id == user.id).all()
    ]
    return data


def _link_employee(db: Session, principal: Principal, user: User, employee_id: Optional[int]) -> None:
    current = db.query(Employee).filter(Employee.user_id == user.id).first()
    if employee_id is None:
        if current:
            current.user_id = None
        return
    employee = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    if employee.user_id not in (None, user.id):
        raise conflict("This employee is already linked to another login.")
    if current and current.id != employee.id:
        current.user_id = None
        db.flush()
    employee.user_id = user.id


def invite(db: Session, principal: Principal, data) -> dict:
    email = data.email.strip().lower()
    # Login identifiers are globally unique; check across tenants on the system connection.
    with system_session() as sysdb:
        if sysdb.query(User.id).filter(func.lower(User.email) == email).first():
            raise conflict("An account with this email already exists.", "EMAIL_TAKEN")

    roles = []
    if data.role_ids:
        roles = (
            db.query(Role)
            .options(selectinload(Role.permissions))
            .filter(Role.company_id == principal.company_id, Role.id.in_(set(data.role_ids)))
            .all()
        )
        if len(roles) != len(set(data.role_ids)):
            raise not_found("Role")
        for role in roles:
            rbac_service.assert_can_manage_role_membership(principal, role)

    user = User(company_id=principal.company_id, email=email, full_name=data.full_name.strip(), status="invited")
    db.add(user)
    db.flush()
    _link_employee(db, principal, user, data.employee_id)
    now = utcnow()
    for role in roles:
        db.add(UserRoleAssignment(company_id=principal.company_id, user_id=user.id, role_id=role.id,
                                  assigned_by_user_id=principal.user_id, effective_from=now))
    token = auth_service.create_invitation(db, user, principal.user_id)
    # Links come from the tenant's verified primary host in the database, never the request Host.
    invite_url = f"{domain_service.public_base_url(principal.company_id)}/accept-invite?token={token}"
    company = db.get(Company, principal.company_id)
    AuditLogService.log(
        db, principal, "Users", "INVITE",
        f"Invited {email} with roles [{', '.join(r.name for r in roles)}]", "user", user.id,
    )
    db.flush()
    emailed = EmailService.send_invitation(email, company.name, invite_url, company_id=company.id, name=user.full_name)
    return {"user_id": user.id, "invite_url": invite_url, "emailed": emailed}


def resend_invitation(db: Session, principal: Principal, user_id: int) -> dict:
    user = _get(db, principal, user_id)
    if user.status != "invited":
        raise bad_request("This user has already accepted their invitation.")
    token = auth_service.create_invitation(db, user, principal.user_id)
    invite_url = f"{domain_service.public_base_url(principal.company_id)}/accept-invite?token={token}"
    company = db.get(Company, principal.company_id)
    AuditLogService.log(db, principal, "Users", "INVITE_RESENT", f"Re-sent invitation to {user.email}", "user", user.id)
    emailed = EmailService.send_invitation(user.email, company.name, invite_url, company_id=company.id, name=user.full_name)
    return {"user_id": user.id, "invite_url": invite_url, "emailed": emailed}


def update(db: Session, principal: Principal, user_id: int, data) -> None:
    user = _get(db, principal, user_id)
    _guard_target(db, principal, user)
    changes = data.model_dump(exclude_unset=True)
    if "full_name" in changes:
        user.full_name = changes["full_name"].strip()
    if "employee_id" in changes:
        _link_employee(db, principal, user, changes["employee_id"])
    AuditLogService.log(db, principal, "Users", "UPDATE", f"Updated {user.email}: {', '.join(sorted(changes))}", "user", user.id)


def set_status(db: Session, principal: Principal, user_id: int, status: str) -> None:
    user = _get(db, principal, user_id)
    _guard_target(db, principal, user)
    if status not in ("active", "suspended", "deactivated"):
        raise bad_request("Unsupported status.")
    if user.status == "invited" and status == "active":
        raise bad_request("Invited users become active when they accept their invitation.")
    if status != "active":
        rbac_service.assert_not_last_admin(db, principal.company_id, user.id)
        auth_service.revoke_user_sessions(db, user.id, f"account_{status}")
        location_service.stop_for_employee(db, principal.company_id, _employee_id_of(db, user), f"ACCOUNT_{status.upper()}")
    previous = user.status
    user.status = status
    user.permission_version += 1
    AuditLogService.log(db, principal, "Users", "STATUS", f"{user.email}: {previous} -> {status}", "user", user.id)


def revoke_sessions(db: Session, principal: Principal, user_id: int) -> int:
    user = _get(db, principal, user_id)
    _guard_target(db, principal, user)
    count = auth_service.revoke_user_sessions(db, user.id, "admin_revoked")
    AuditLogService.log(db, principal, "Users", "SESSIONS_REVOKED", f"Signed out {count} session(s) of {user.email}", "user", user.id)
    return count


def _employee_id_of(db: Session, user: User) -> Optional[int]:
    return db.query(Employee.id).filter(Employee.user_id == user.id).scalar()


def set_roles(db: Session, principal: Principal, user_id: int, role_ids: List[int]) -> None:
    user = _get(db, principal, user_id)
    rbac_service.set_user_roles(db, principal, user, role_ids)
    db.flush()
    if "attendance:mark" not in load_grants(db, user.id)[0]:
        # Without the right to mark attendance, no further location is collected for the running shift.
        location_service.stop_for_employee(db, principal.company_id, _employee_id_of(db, user), "PERMISSION_REVOKED")


def set_assigned_employees(db: Session, principal: Principal, user_id: int, employee_ids: List[int]) -> None:
    user = _get(db, principal, user_id)
    _guard_target(db, principal, user)
    ids = set(employee_ids)
    if ids:
        found = db.query(func.count(Employee.id)).filter(Employee.company_id == principal.company_id, Employee.id.in_(ids)).scalar()
        if found != len(ids):
            raise not_found("Employee")
    db.query(ScopeAssignment).filter(ScopeAssignment.user_id == user.id).delete(synchronize_session=False)
    for employee_id in sorted(ids):
        db.add(ScopeAssignment(company_id=principal.company_id, user_id=user.id, employee_id=employee_id,
                               created_by_user_id=principal.user_id))
    user.permission_version += 1
    AuditLogService.log(db, principal, "Users", "ASSIGNED_GROUP", f"{user.email} now covers {len(ids)} assigned employee(s)", "user", user.id)
