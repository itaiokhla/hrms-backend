"""Tenant role management and delegation rules.

Delegated administration is guarded by dedicated rules rather than by the
basic permission check alone:

* A grant can only be delegated by someone who holds the same permission at
  an equal or broader scope (company administrators can delegate anything).
  Grants at data-defined scopes (legal entity, branch, pay group, assigned
  employees) need the permission at COMPANY scope.
* Protected roles (Company Administrator) can only be granted, revoked or
  edited by company administrators, and full-access roles cannot be created.
* Nobody changes their own role assignments or edits a role they hold. The
  one exception: a company administrator may grant (or give up) their own
  payroll roles, because full access never implies payroll data; such changes
  are audited as ROLE_SELF_GRANTED / ROLE_SELF_REVOKED (EC-SEC-03).
* The last active company administrator can never lose that role or be
  suspended, and the last company-wide payroll run approver is kept while
  anyone can create runs (contract §28.9).
"""

from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.permission_registry import (
    COMPANY_DELEGATED_SCOPES,
    DEFAULT_ROLES,
    PAYROLL_RUN_APPROVE,
    PAYROLL_RUN_CREATE,
    PERMISSIONS,
    SCOPE_LABELS,
    Scope,
    full_access_grants,
    is_payroll_grant,
    is_valid_grant,
    loses_last_payroll_approver,
    scope_covers,
)
from app.core.principal import Principal, load_grants
from app.core.security import utcnow
from app.models.leave import ApprovalChainStep, LeaveApproval
from app.models.rbac import Role, RolePermission, UserRoleAssignment
from app.models.user import User
from app.payroll import audit as payroll_audit
from app.services.audit_log_service import AuditLogService

Grant = Tuple[str, str]
_COMPANY_DELEGATED = {s.value for s in COMPANY_DELEGATED_SCOPES}
_RUN_CODES = (PAYROLL_RUN_APPROVE, PAYROLL_RUN_CREATE)


def seed_company_roles(db: Session, company_id: int) -> Dict[str, Role]:
    roles: Dict[str, Role] = {}
    for definition in DEFAULT_ROLES:
        role = Role(
            company_id=company_id,
            name=definition.name,
            description=definition.description,
            system_key=definition.system_key,
            is_system_role=True,
            is_protected=definition.is_protected,
            is_full_access=definition.is_full_access,
        )
        role.permissions = [
            RolePermission(company_id=company_id, permission_code=code, scope=scope.value)
            for code, scope in sorted(set(definition.grants))
        ]
        db.add(role)
        roles[definition.system_key] = role
    db.flush()
    return roles


def role_grants(role: Role) -> Set[Grant]:
    """What a role gives; for full access the same implied set live authorization uses."""
    if role.is_full_access:
        return set(full_access_grants())
    return {(rp.permission_code, rp.scope) for rp in role.permissions}


def is_payroll_role(role: Role) -> bool:
    """A non-protected role giving payroll-confidential access, which full access never implies."""
    if role.is_protected or role.is_full_access:
        return False
    return any(is_payroll_grant(code, scope) for code, scope in role_grants(role))


def can_delegate(principal: Principal, code: str, scope: str) -> bool:
    """Whether the principal may give (or take away) `code` at `scope`.

    Administrators may delegate anything, including codes no longer registered, so roles holding
    stale grants stay editable (D-9; new grants are validated separately). Others must hold the code
    at a covering scope, and at COMPANY for targeted and assigned-group scopes, whose reach depends
    on per-user data the delegator may not cover (D-2/D-3).
    """
    if principal.is_company_admin:
        return True
    if not is_valid_grant(code, scope):
        return False
    if scope in _COMPANY_DELEGATED:
        return principal.has_company_scope(code)
    return scope_covers(principal.scopes(code), scope)


def _assert_can_delegate_all(principal: Principal, grants: Iterable[Grant]) -> None:
    denied = sorted({code for code, scope in grants if not can_delegate(principal, code, scope)})
    if denied:
        raise forbidden(f"You cannot delegate: {', '.join(denied)}")


def assert_can_manage_role_membership(principal: Principal, role: Role) -> None:
    if role.is_protected or role.is_full_access:
        if not principal.is_company_admin:
            raise forbidden("Only company administrators can grant or revoke this role.")
        return
    _assert_can_delegate_all(principal, role_grants(role))


def active_admin_user_ids(db: Session, company_id: int) -> Set[int]:
    now = utcnow()
    rows = (
        db.query(UserRoleAssignment.user_id)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .join(User, User.id == UserRoleAssignment.user_id)
        .filter(
            Role.company_id == company_id,
            Role.is_protected.is_(True),
            Role.is_full_access.is_(True),
            User.status == "active",
            UserRoleAssignment.effective_from <= now,
            or_(UserRoleAssignment.effective_until.is_(None), UserRoleAssignment.effective_until > now),
        )
        .all()
    )
    return {r[0] for r in rows}


def assert_not_last_admin(db: Session, company_id: int, user_id: int) -> None:
    admins = active_admin_user_ids(db, company_id)
    if user_id in admins and len(admins) <= 1:
        raise conflict(
            "A company must keep at least one active Company Administrator.", "LAST_ADMIN"
        )


def _active_assignment(now):
    return (
        UserRoleAssignment.effective_from <= now,
        or_(UserRoleAssignment.effective_until.is_(None), UserRoleAssignment.effective_until > now),
    )


def _payroll_run_holders(db: Session, company_id: int) -> Dict[int, Dict[int, Set[Grant]]]:
    """Active user -> role -> their payroll:run:approve / payroll:run:create grants."""
    rows = (
        db.query(UserRoleAssignment.user_id, UserRoleAssignment.role_id, RolePermission.permission_code, RolePermission.scope)
        .join(User, User.id == UserRoleAssignment.user_id)
        .join(RolePermission, RolePermission.role_id == UserRoleAssignment.role_id)
        .filter(
            UserRoleAssignment.company_id == company_id,
            User.status == "active",
            RolePermission.permission_code.in_(_RUN_CODES),
            *_active_assignment(utcnow()),
        )
        .all()
    )
    holders: Dict[int, Dict[int, Set[Grant]]] = {}
    for user_id, role_id, code, scope in rows:
        holders.setdefault(user_id, {}).setdefault(role_id, set()).add((code, scope))
    return holders


def _run_grants(grants: Iterable[Grant]) -> Set[Grant]:
    return {g for g in grants if g[0] in _RUN_CODES}


def _assert_keeps_payroll_approver(
    before: Dict[int, Dict[int, Set[Grant]]], after: Dict[int, Dict[int, Set[Grant]]]
) -> None:
    def flat(holders):
        return {user_id: set().union(*roles.values()) for user_id, roles in holders.items()}

    if loses_last_payroll_approver(flat(before), flat(after)):
        raise conflict(
            "This change would leave payroll runs that nobody can approve. "
            "Give another active user the right to approve payroll runs first.",
            "LAST_PAYROLL_APPROVER",
        )


def assert_not_last_payroll_approver(db: Session, company_id: int, user_id: int) -> None:
    """Refuse to take away a user (suspension, deactivation) who is the last company-wide run approver (§28.9)."""
    before = _payroll_run_holders(db, company_id)
    _assert_keeps_payroll_approver(before, {u: roles for u, roles in before.items() if u != user_id})


def get_role(db: Session, principal: Principal, role_id: int) -> Role:
    role = (
        db.query(Role)
        .options(selectinload(Role.permissions))
        .filter(Role.company_id == principal.company_id, Role.id == role_id)
        .first()
    )
    if role is None:
        raise not_found("Role")
    return role


def list_roles(db: Session, principal: Principal) -> List[dict]:
    roles = (
        db.query(Role)
        .options(selectinload(Role.permissions))
        .filter(Role.company_id == principal.company_id)
        .order_by(Role.is_system_role.desc(), Role.name)
        .all()
    )
    counts = dict(
        db.query(UserRoleAssignment.role_id, func.count(UserRoleAssignment.id))
        .filter(UserRoleAssignment.company_id == principal.company_id)
        .group_by(UserRoleAssignment.role_id)
        .all()
    )
    return [serialize_role(r, counts.get(r.id, 0), principal) for r in roles]


def serialize_role(role: Role, member_count: int, principal: Principal) -> dict:
    grants = sorted(role_grants(role))
    editable = not role.is_full_access and not role.is_protected and role.id not in principal.role_ids
    assignable = True
    try:
        assert_can_manage_role_membership(principal, role)
    except AppError:
        assignable = False
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "system_key": role.system_key,
        "is_system_role": role.is_system_role,
        "is_protected": role.is_protected,
        "is_full_access": role.is_full_access,
        "member_count": member_count,
        "grants": [{"permission": code, "scope": scope} for code, scope in grants],
        "can_edit": editable,
        "can_assign": assignable,
    }


def _validate_grants(grants: Sequence[dict]) -> Set[Grant]:
    result: Set[Grant] = set()
    for item in grants:
        code, scope = item["permission"], item["scope"]
        if not is_valid_grant(code, scope):
            raise bad_request(f"'{code}' cannot be granted at scope {scope}")
        result.add((code, scope))
    return result


def create_role(db: Session, principal: Principal, name: str, description: Optional[str], grants: Sequence[dict]) -> Role:
    wanted = _validate_grants(grants)
    _assert_can_delegate_all(principal, wanted)
    name = name.strip()
    if db.query(Role.id).filter(Role.company_id == principal.company_id, func.lower(Role.name) == name.lower()).first():
        raise conflict("A role with this name already exists.")
    role = Role(company_id=principal.company_id, name=name, description=description)
    role.permissions = [
        RolePermission(company_id=principal.company_id, permission_code=c, scope=s) for c, s in sorted(wanted)
    ]
    db.add(role)
    db.flush()
    AuditLogService.log(db, principal, "Roles", "CREATE", f"Created role '{name}' with {len(wanted)} grants", "role", role.id)
    return role


def update_role(
    db: Session,
    principal: Principal,
    role: Role,
    name: Optional[str],
    description: Optional[str],
    grants: Optional[Sequence[dict]],
) -> Role:
    if role.is_full_access or role.is_protected:
        raise forbidden("This role is protected and cannot be edited.")
    if role.id in principal.role_ids and not principal.is_company_admin:
        raise forbidden("You cannot edit a role you hold.")
    if name is not None and name.strip() != role.name:
        clash = (
            db.query(Role.id)
            .filter(Role.company_id == principal.company_id, func.lower(Role.name) == name.strip().lower(), Role.id != role.id)
            .first()
        )
        if clash:
            raise conflict("A role with this name already exists.")
        role.name = name.strip()
    if description is not None:
        role.description = description
    if grants is not None:
        wanted = _validate_grants(grants)
        current = role_grants(role)
        # Both additions and removals require delegation authority.
        _assert_can_delegate_all(principal, wanted ^ current)
        if _run_grants(current - wanted):
            before = _payroll_run_holders(db, principal.company_id)
            after = {u: dict(roles) for u, roles in before.items()}
            members = (
                db.query(UserRoleAssignment.user_id)
                .join(User, User.id == UserRoleAssignment.user_id)
                .filter(UserRoleAssignment.role_id == role.id, User.status == "active", *_active_assignment(utcnow()))
                .all()
            )
            for (user_id,) in members:
                after.setdefault(user_id, {})[role.id] = _run_grants(wanted)
            _assert_keeps_payroll_approver(before, after)
        # Change only the difference: re-adding a kept grant would be inserted before the old row
        # is deleted in the same flush and violate uq_role_permission_scope.
        for permission in list(role.permissions):
            if (permission.permission_code, permission.scope) not in wanted:
                role.permissions.remove(permission)
        for code, scope in sorted(wanted - current):
            role.permissions.append(RolePermission(company_id=principal.company_id, permission_code=code, scope=scope))
        bump_permission_versions_for_role(db, role.id)
    AuditLogService.log(db, principal, "Roles", "UPDATE", f"Updated role '{role.name}'", "role", role.id)
    return role


def delete_role(db: Session, principal: Principal, role: Role) -> None:
    if role.is_protected or role.is_full_access:
        raise forbidden("This role is protected and cannot be deleted.")
    _assert_can_delegate_all(principal, role_grants(role))
    members = db.query(func.count(UserRoleAssignment.id)).filter(UserRoleAssignment.role_id == role.id).scalar()
    if members:
        raise conflict(f"This role is assigned to {members} user(s). Reassign them before deleting it.", "ROLE_IN_USE")
    in_chain = db.query(ApprovalChainStep.id).filter(ApprovalChainStep.role_id == role.id).first()
    in_flight = db.query(LeaveApproval.id).filter(LeaveApproval.role_id == role.id).first()
    if in_chain or in_flight:
        raise conflict("This role is used in an approval chain. Update the chain first.", "ROLE_IN_USE")
    AuditLogService.log(db, principal, "Roles", "DELETE", f"Deleted role '{role.name}'", "role", role.id)
    db.delete(role)


def bump_permission_versions_for_role(db: Session, role_id: int) -> None:
    user_ids = [r[0] for r in db.query(UserRoleAssignment.user_id).filter(UserRoleAssignment.role_id == role_id).all()]
    if user_ids:
        db.query(User).filter(User.id.in_(user_ids)).update(
            {User.permission_version: User.permission_version + 1}, synchronize_session=False
        )


def set_user_roles(db: Session, principal: Principal, target: User, role_ids: Sequence[int]) -> List[Role]:
    self_change = target.id == principal.user_id
    if self_change and not principal.is_company_admin:
        raise forbidden("You cannot change your own roles.")
    wanted_ids = set(role_ids)
    roles = (
        db.query(Role)
        .options(selectinload(Role.permissions))
        .filter(Role.company_id == principal.company_id, Role.id.in_(wanted_ids))
        .all()
        if wanted_ids
        else []
    )
    if len(roles) != len(wanted_ids):
        raise not_found("Role")

    current = {a.role_id: a for a in target.role_assignments}
    current_roles = {
        r.id: r
        for r in db.query(Role).options(selectinload(Role.permissions)).filter(Role.id.in_(list(current))).all()
    } if current else {}

    to_add = [r for r in roles if r.id not in current]
    to_remove = [current_roles[rid] for rid in current if rid not in wanted_ids]

    # An administrator's own roles change only by adding or removing payroll roles (EC-SEC-03).
    if self_change and (not (to_add or to_remove) or not all(is_payroll_role(r) for r in to_add + to_remove)):
        raise forbidden("You cannot change your own roles. Administrators may only grant or remove their own payroll roles.")
    # Holders of protected roles can only be managed by company administrators.
    if any(r.is_protected for r in current_roles.values()) and not principal.is_company_admin:
        raise forbidden("Only company administrators can change an administrator's roles.")
    for role in to_add + to_remove:
        assert_can_manage_role_membership(principal, role)
    if any(r.is_protected for r in to_remove):
        assert_not_last_admin(db, principal.company_id, target.id)
    if target.status == "active" and any(_run_grants(role_grants(r)) for r in to_remove):
        before = _payroll_run_holders(db, principal.company_id)
        after = {u: dict(r) for u, r in before.items()}
        after[target.id] = {r.id: _run_grants(role_grants(r)) for r in roles}
        _assert_keeps_payroll_approver(before, after)

    now = utcnow()
    for role in to_remove:
        assignment = current[role.id]
        target.role_assignments.remove(assignment)
        db.delete(assignment)
    for role in to_add:
        target.role_assignments.append(
            UserRoleAssignment(
                company_id=principal.company_id,
                user_id=target.id,
                role_id=role.id,
                assigned_by_user_id=principal.user_id,
                effective_from=now,
            )
        )
    target.permission_version = (target.permission_version or 1) + 1
    if to_add or to_remove:
        AuditLogService.log(
            db,
            principal,
            "Users",
            "ROLES_CHANGED",
            f"Roles for {target.email}: +[{', '.join(r.name for r in to_add)}] -[{', '.join(r.name for r in to_remove)}]",
            "user",
            target.id,
        )
    _audit_payroll_role_changes(db, principal, target, to_add, to_remove, self_change)
    return roles


def _audit_payroll_role_changes(
    db: Session, principal: Principal, target: User, added: Sequence[Role], removed: Sequence[Role], self_change: bool
) -> None:
    """Payroll-role changes are also written under "Payroll Roles" for payroll auditors (EC-SEC-03)."""
    for role, granted in [(r, True) for r in added] + [(r, False) for r in removed]:
        if not is_payroll_role(role):
            continue
        if self_change:
            action = payroll_audit.ROLE_SELF_GRANTED if granted else payroll_audit.ROLE_SELF_REVOKED
            text = (
                f"Company administrator {target.email} granted themselves the payroll role '{role.name}'; "
                "full access does not include payroll data (EC-SEC-03)"
                if granted
                else f"Company administrator {target.email} removed their own payroll role '{role.name}'"
            )
        else:
            action = payroll_audit.ROLE_GRANTED if granted else payroll_audit.ROLE_REVOKED
            text = f"{'Granted' if granted else 'Revoked'} payroll role '{role.name}' {'to' if granted else 'from'} {target.email}"
        AuditLogService.log(
            db, principal, payroll_audit.PAYROLL_ROLES, action, text, "user", target.id,
            details={"role_id": role.id, "role": role.name, "system_key": role.system_key, "self": self_change},
        )


def access_preview(db: Session, user: User) -> dict:
    """Read-only explanation of a user's effective access.

    Uses the same grant loader as live authorization, so the preview can
    never disagree with what the API actually enforces.
    """
    grants, _, _, is_admin = load_grants(db, user.id)
    by_module: Dict[str, List[dict]] = {}
    for perm in PERMISSIONS:
        held = grants.get(perm.code)
        if not held:
            continue
        ordered = [s for s in Scope if s.value in held]
        by_module.setdefault(perm.module, []).append(
            {
                "permission": perm.code,
                "label": perm.label,
                "sensitivity": perm.sensitivity,
                "scopes": [s.value for s in ordered],
                "scope_labels": [SCOPE_LABELS[s] for s in ordered],
            }
        )
    restricted = [
        {"permission": p.code, "label": p.label, "module": p.module}
        for p in PERMISSIONS
        if p.code not in grants and p.sensitivity != "standard"
    ]
    return {"is_company_admin": is_admin, "capabilities": by_module, "restricted": restricted}


def permission_catalog() -> dict:
    modules: Dict[str, List[dict]] = {}
    for perm in PERMISSIONS:
        modules.setdefault(perm.module, []).append(
            {
                "permission": perm.code,
                "label": perm.label,
                "description": perm.description,
                "sensitivity": perm.sensitivity,
                "scopes": [s.value for s in perm.scopes],
                "full_access_scopes": [s.value for s in perm.implied_scopes],
                "payroll_confidential": perm.payroll_confidential,
                "deprecated": perm.deprecated,
            }
        )
    return {
        "modules": [{"name": name, "permissions": perms} for name, perms in modules.items()],
        "scopes": [{"value": s.value, "label": SCOPE_LABELS[s]} for s in Scope],
    }


def assign_role_by_key(db: Session, company_id: int, user: User, system_key: str, assigned_by: Optional[int] = None) -> None:
    role = db.query(Role).filter(Role.company_id == company_id, Role.system_key == system_key).first()
    if role is None:
        raise not_found("Role")
    db.add(
        UserRoleAssignment(
            company_id=company_id,
            user_id=user.id,
            role_id=role.id,
            assigned_by_user_id=assigned_by,
            effective_from=utcnow(),
        )
    )
