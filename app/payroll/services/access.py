"""Payroll data scopes (3.9 §5.4, 02 §15.2) and the payroll access preview (contract K:214, G-17).

Run-level objects (runs, payment batches, funding accounts, entity reports) are in scope through
their legal entity or pay group. Employee-level objects (run employees, payslips, compensation,
bank accounts) are in scope when the employee was inside the scope during the object's period,
so history follows the record's date (EC-ORG-14). Every helper yields a SQL expression, so list,
count, aggregate and export queries filter identically and counts never reveal out-of-scope
records (EC-SEC-05).
"""

from dataclasses import dataclass
from datetime import date
from typing import Dict, FrozenSet, List, Optional, Set

from sqlalchemy import ColumnElement, Select, false, or_, select, true, union
from sqlalchemy.orm import Session

from app.core import authz
from app.core.errors import forbidden, not_found
from app.core.permission_registry import (
    PERMISSIONS,
    REGISTRY_VERSION,
    SCOPE_LABELS,
    TARGETED_SCOPES,
    PermissionDef,
    Scope,
    implied_by_full_access,
)
from app.core.principal import Principal, load_grants
from app.core.security import utcnow
from app.models.org import Branch, LegalEntity, RoleAssignmentTarget
from app.models.pay_org import PayGroup
from app.models.rbac import Role, RolePermission, UserRoleAssignment
from app.models.user import User

PAYROLL_MODULES = ("Payroll", "Payroll (legacy)")
PAYROLL_PERMISSIONS: List[PermissionDef] = [p for p in PERMISSIONS if p.module in PAYROLL_MODULES]
PREVIEW_OTHERS_PERMISSION = "user:read"


# ---------------------------------------------------------------------------
# Run-level scope (runs, batches, funding accounts and other non-employee objects)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class TargetScope:
    """What one payroll permission reaches: the whole company, or explicit targets.

    `employee_select` holds the employee ids reached through SELF / DEPARTMENT / ASSIGNED_GROUP
    (today's placements); it never covers run-level objects.
    """

    all_company: bool = False
    legal_entity_ids: FrozenSet[int] = frozenset()
    pay_group_ids: FrozenSet[int] = frozenset()
    branch_ids: FrozenSet[int] = frozenset()
    employee_select: Optional[Select] = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.all_company or self.legal_entity_ids or self.pay_group_ids or self.branch_ids
            or self.employee_select is not None
        )

    def covers(self, legal_entity_id: Optional[int], pay_group_id: Optional[int] = None) -> bool:
        if self.all_company:
            return True
        return (legal_entity_id is not None and legal_entity_id in self.legal_entity_ids) or (
            pay_group_id is not None and pay_group_id in self.pay_group_ids
        )

    def clause(self, legal_entity_col, pay_group_col=None) -> ColumnElement:
        if self.all_company:
            return true()
        conditions = []
        if self.legal_entity_ids:
            conditions.append(legal_entity_col.in_(sorted(self.legal_entity_ids)))
        if self.pay_group_ids and pay_group_col is not None:
            conditions.append(pay_group_col.in_(sorted(self.pay_group_ids)))
        return or_(*conditions) if conditions else false()


def target_scope(principal: Principal, permission: str) -> TargetScope:
    scopes = principal.scopes(permission)
    if not scopes:
        return TargetScope()
    if Scope.COMPANY.value in scopes:
        return TargetScope(all_company=True)

    def targets(scope: Scope) -> FrozenSet[int]:
        return principal.scope_targets(permission, scope.value) if scope.value in scopes else frozenset()

    reach = authz.relationship_selects(principal, permission)
    return TargetScope(
        legal_entity_ids=targets(Scope.LEGAL_ENTITY),
        pay_group_ids=targets(Scope.PAY_GROUP),
        branch_ids=targets(Scope.BRANCH),
        employee_select=(reach[0] if len(reach) == 1 else union(*reach)) if reach else None,
    )


def run_scope_clause(principal: Principal, permission: str, legal_entity_col, pay_group_col=None) -> ColumnElement:
    """COMPANY -> true; LEGAL_ENTITY / PAY_GROUP targets -> IN lists; anything else -> false.

    Pass `pay_group_col=None` for objects owned by a legal entity only (batches, funding accounts).
    """
    return target_scope(principal, permission).clause(legal_entity_col, pay_group_col)


def ensure_in_scope(
    principal: Principal, permission: str, legal_entity_id: Optional[int], pay_group_id: Optional[int] = None,
    what: str = "Record",
) -> None:
    """403 without the permission at all; 404 when the object lies outside the caller's scope."""
    if not principal.has(permission):
        raise forbidden()
    if not target_scope(principal, permission).covers(legal_entity_id, pay_group_id):
        raise not_found(what)


def ensure_run_access(principal: Principal, permission: str, run, what: str = "Payroll run") -> None:
    """`ensure_in_scope` for a loaded run-like object (anything with legal_entity_id / pay_group_id)."""
    if getattr(run, "company_id", principal.company_id) != principal.company_id:
        raise not_found(what)
    ensure_in_scope(principal, permission, run.legal_entity_id, getattr(run, "pay_group_id", None), what)


def get_in_scope(
    db: Session, principal: Principal, permission: str, model, object_id: int, what: str = "Record", *,
    legal_entity_attr: str = "legal_entity_id", pay_group_attr: Optional[str] = "pay_group_id",
):
    """Load a run-level object of the caller's tenant and scope, or raise 404 (403 without the permission)."""
    if not principal.has(permission):
        raise forbidden()
    pay_group_col = getattr(model, pay_group_attr) if pay_group_attr else None
    found = (
        db.query(model)
        .filter(
            model.company_id == principal.company_id,
            model.id == object_id,
            run_scope_clause(principal, permission, getattr(model, legal_entity_attr), pay_group_col),
        )
        .first()
    )
    if found is None:
        raise not_found(what)
    return found


# ---------------------------------------------------------------------------
# Employee-level scope (run employees, payslips, compensation, bank accounts)
# ---------------------------------------------------------------------------
def payroll_employee_scope(
    db: Session, principal: Principal, permission: str, period_start: date, period_end: date
) -> authz.EmployeeScope:
    """Employees inside the scope at any time during [period_start, period_end] (SELF always own).

    For a run's employees combine with the run check: `and_(run_scope_clause(...), scope.clause(col))`.
    """
    return authz.employee_scope_during(db, principal, permission, period_start, period_end)


def run_employee_clause(
    principal: Principal, permission: str, legal_entity_col, pay_group_col, employee_col
) -> ColumnElement:
    """Rows of run employees / payslips across runs: the run is in scope, or (SELF) the row is the caller's own.

    An employee appears in a run only for the entity and pay group it was placed in during the
    run's period, so a run-level match already implies the dated employee match (EC-ORG-14).
    """
    scope = target_scope(principal, permission)
    if scope.all_company:
        return true()
    conditions = [scope.clause(legal_entity_col, pay_group_col)]
    if Scope.SELF.value in principal.scopes(permission) and principal.employee_id is not None:
        conditions.append(employee_col == principal.employee_id)
    return or_(*conditions)


def ensure_employee_in_scope(
    db: Session, principal: Principal, permission: str, employee_id: int, period_start: date, period_end: date,
    what: str = "Employee",
) -> None:
    """403 without the permission; 404 unless the employee was inside the scope during the period."""
    if not principal.has(permission):
        raise forbidden()
    from app.models.employee import Employee

    scope = payroll_employee_scope(db, principal, permission, period_start, period_end)
    found = db.execute(
        select(Employee.id).where(
            Employee.company_id == principal.company_id, Employee.id == employee_id, scope.clause(Employee.id)
        )
    ).first()
    if found is None:
        raise not_found(what)


# ---------------------------------------------------------------------------
# Access preview (G-17): effective payroll capabilities of one user, never amounts
# ---------------------------------------------------------------------------
def _names(db: Session, model, company_id: int, ids: Set[int], label_attr: str) -> Dict[int, dict]:
    if not ids:
        return {}
    rows = db.query(model).filter(model.company_id == company_id, model.id.in_(sorted(ids))).all()
    return {r.id: {"id": r.id, "code": r.code, "name": getattr(r, label_attr)} for r in rows}


def access_preview(db: Session, principal: Principal, user_id: Optional[int] = None) -> dict:
    """Each payroll permission with its scopes, targets and where it comes from (full access or a role).

    Scopes come from the same loader as live authorization, so the preview never disagrees with
    enforcement; only role, scope and target names are returned, never pay data.
    """
    if user_id is not None and user_id != principal.user_id and not principal.has(PREVIEW_OTHERS_PERMISSION):
        raise forbidden()
    cid = principal.company_id
    user = db.query(User).filter(User.company_id == cid, User.id == (user_id or principal.user_id)).first()
    if user is None:
        raise not_found("User")

    grants, _, _, is_admin = load_grants(db, user.id)
    now = utcnow()
    assignments = (
        db.query(UserRoleAssignment.id, Role.id, Role.name, Role.system_key, Role.is_full_access)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .filter(
            UserRoleAssignment.company_id == cid,
            UserRoleAssignment.user_id == user.id,
            UserRoleAssignment.effective_from <= now,
            or_(UserRoleAssignment.effective_until.is_(None), UserRoleAssignment.effective_until > now),
        )
        .order_by(Role.name)
        .all()
    )
    roles = {role_id: {"id": role_id, "name": name, "system_key": key} for _, role_id, name, key, _ in assignments}
    full_access_roles = [roles[role_id] for _, role_id, _, _, full in assignments if full]
    payroll_codes = [p.code for p in PAYROLL_PERMISSIONS]
    explicit: Dict[tuple, List[int]] = {}
    if roles:
        for role_id, code, scope in db.query(RolePermission.role_id, RolePermission.permission_code, RolePermission.scope).filter(
            RolePermission.company_id == cid, RolePermission.role_id.in_(sorted(roles)),
            RolePermission.permission_code.in_(payroll_codes),
        ):
            explicit.setdefault((code, scope), []).append(role_id)

    # Targets per (role, scope) from this user's assignments of that role.
    assignment_role = {assignment_id: role_id for assignment_id, role_id, _, _, _ in assignments}
    role_targets: Dict[tuple, Set[int]] = {}
    if assignment_role:
        for assignment_id, scope, target_id in db.query(
            RoleAssignmentTarget.user_role_assignment_id, RoleAssignmentTarget.scope, RoleAssignmentTarget.target_id
        ).filter(
            RoleAssignmentTarget.company_id == cid,
            RoleAssignmentTarget.user_role_assignment_id.in_(sorted(assignment_role)),
        ):
            role_targets.setdefault((assignment_role[assignment_id], scope), set()).add(target_id)
    wanted = {s.value: set() for s in TARGETED_SCOPES}
    for (_, scope), ids in role_targets.items():
        wanted.setdefault(scope, set()).update(ids)
    names = {
        Scope.LEGAL_ENTITY.value: _names(db, LegalEntity, cid, wanted[Scope.LEGAL_ENTITY.value], "legal_name"),
        Scope.BRANCH.value: _names(db, Branch, cid, wanted[Scope.BRANCH.value], "name"),
        Scope.PAY_GROUP.value: _names(db, PayGroup, cid, wanted[Scope.PAY_GROUP.value], "name"),
    }
    target_keys = {Scope.LEGAL_ENTITY.value: "legal_entities", Scope.BRANCH.value: "branches",
                   Scope.PAY_GROUP.value: "pay_groups"}
    has_employee = _has_employee(db, cid, user.id)

    permissions = []
    for perm in PAYROLL_PERMISSIONS:
        held = grants.get(perm.code, frozenset())
        entries = []
        for scope in (s for s in Scope if s.value in held):
            via_roles = explicit.get((perm.code, scope.value), [])
            from_full_access = bool(full_access_roles) and implied_by_full_access(perm.code, scope.value)
            entry = {
                "scope": scope.value,
                "scope_label": SCOPE_LABELS[scope],
                "from_full_access": from_full_access,
                "full_access_roles": full_access_roles if from_full_access else [],
                "roles": [roles[r] for r in sorted(via_roles, key=lambda r: roles[r]["name"])],
                "effective": True,
                "note": None,
            }
            if scope in TARGETED_SCOPES:
                ids = set().union(*(role_targets.get((r, scope.value), set()) for r in via_roles)) if via_roles else set()
                entry["targets"] = sorted(
                    (names[scope.value][i] for i in ids if i in names[scope.value]), key=lambda t: (t["name"], t["id"])
                )
                entry["target_type"] = target_keys[scope.value]
                if not entry["targets"]:
                    entry.update(effective=False, note="No targets selected: this grant covers nothing.")
            elif scope == Scope.SELF and not has_employee:
                entry.update(effective=False, note="No employee profile is linked: own records are empty.")
            entries.append(entry)
        permissions.append({
            "permission": perm.code,
            "label": perm.label,
            "description": perm.description,
            "module": perm.module,
            "sensitivity": perm.sensitivity,
            "deprecated": perm.deprecated,
            "held": bool(entries),
            "grants": entries,
        })
    return {
        "user": {"id": user.id, "full_name": user.full_name, "email": user.email, "status": user.status},
        "account_active": user.status == "active",
        "is_company_admin": is_admin,
        "has_full_access": bool(full_access_roles),
        "roles": sorted(roles.values(), key=lambda r: r["name"]),
        "registry_version": REGISTRY_VERSION,
        "permissions": permissions,
        "summary": {
            "held": sum(1 for p in permissions if p["held"]),
            "total": len(permissions),
            "from_full_access": sum(1 for p in permissions if any(g["from_full_access"] for g in p["grants"])),
            "ineffective_grants": sum(1 for p in permissions for g in p["grants"] if not g["effective"]),
        },
    }


def _has_employee(db: Session, company_id: int, user_id: int) -> bool:
    from app.models.employee import Employee

    return db.query(Employee.id).filter(Employee.company_id == company_id, Employee.user_id == user_id).first() is not None
