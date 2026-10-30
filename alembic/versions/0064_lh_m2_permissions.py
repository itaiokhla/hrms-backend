"""LH-M2 permission seed (SPINE §9; 06 §12.1 "one seed migration is safer than five"). Registry 2026.10.13.

* system ``hr_admin`` roles get every v2 leave code at COMPANY (``leave:view_medical`` included, D-LH-16);
* system ``manager`` roles get ``leave:apply_on_behalf`` (DIRECT_REPORTS);
* the new default roles ``department_head``, ``hr_executive`` (BR), ``hr_manager`` (LE), ``executive`` and ``finance``
  are created in every company that lacks them (a name already taken by a custom role gets a suffix).

Grants are added idempotently (``ON CONFLICT`` by lookup); a tenant that stripped a grant from a system role gets it
back only for the codes this revision introduces. Holders' ``permission_version`` is bumped. Downgrade removes the new
codes and the roles it created when nobody holds them.

Revision ID: 0064_lh_m2_permissions
Revises: 0063_lh_v2_leave_core
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0064_lh_m2_permissions"
down_revision: Union[str, None] = "0063_lh_v2_leave_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

HR_LEAVE_CODES = (
    "leave:apply_on_behalf", "leave:record", "leave:override", "leave:change_any", "leave:balance_adjust",
    "leave:import", "leave:year_end", "leave:view_documents", "leave:verify_documents", "leave:view_medical",
    "leave:analytics_individual", "leave:liability_read",
)
NEW_CODES = HR_LEAVE_CODES
# Default-role grants this revision adds to existing system roles (registry 2026.10.13 DEFAULT_ROLES); listed for
# tests/payroll/test_permissions_v2.py's registry comparison.
NEW_GRANTS = {
    "hr_admin": tuple((code, "COMPANY") for code in HR_LEAVE_CODES),
    "manager": (("leave:apply_on_behalf", "DIRECT_REPORTS"),),
}
_SELF_SERVICE = (
    ("employee:read", "SELF"), ("employee:read_confidential", "SELF"), ("profile:update", "SELF"),
    ("attendance:read", "SELF"), ("attendance:mark", "SELF"), ("attendance:request", "SELF"), ("location:read", "SELF"),
    ("leave:read", "SELF"), ("leave:apply", "SELF"), ("holiday:read", "COMPANY"), ("holiday:choose_optional", "SELF"),
    ("payroll:read", "SELF"), ("compensation:read", "SELF"), ("asset:read", "SELF"), ("asset:acknowledge", "SELF"),
    ("payroll:payslip:read", "SELF"), ("payroll:compensation:read", "SELF"),
    ("payroll:bank_details:read_masked", "SELF"), ("payroll:bank_details:edit", "SELF"),
    ("payroll:tax_profile:read", "SELF"), ("payroll:tax_profile:edit", "SELF"), ("payroll:loans:read", "SELF"),
    ("payroll:reimbursements:read", "SELF"), ("overtime:request", "SELF"), ("task:read", "SELF"),
    ("shift:read", "SELF"), ("roster:read", "SELF"), ("roster:request", "SELF"), ("team:read", "SELF"),
)
NEW_ROLES = {
    "department_head": (
        "Department Head", "Heads a department: sees and approves leave of the departments they head.",
        _SELF_SERVICE + (("employee:read", "DEPARTMENT"), ("leave:read", "DEPARTMENT"), ("leave:approve", "DEPARTMENT"),
                         ("attendance:read", "DEPARTMENT"), ("report:read", "DEPARTMENT"))),
    "hr_executive": (
        "HR Executive",
        "Runs day-to-day leave for the selected branches: applies and records leave and checks documents.",
        _SELF_SERVICE + tuple((code, "BRANCH") for code in (
            "employee:read", "leave:read", "leave:approve", "leave:apply_on_behalf", "leave:record",
            "leave:view_documents", "leave:verify_documents", "report:read"))),
    "hr_manager": (
        "HR Manager",
        "Runs leave and holidays for the selected legal entities: plans, balances, year end and approvals. Leave "
        "settings and medical details stay with the HR Administrator.",
        _SELF_SERVICE + tuple((code, "LEGAL_ENTITY") for code in (
            "employee:read", "leave:read", "leave:approve", "leave:manage_policy", "holiday:manage",
            "holiday:publish") + tuple(c for c in HR_LEAVE_CODES if c != "leave:view_medical")
            + ("report:read", "report:export"))),
    "executive": (
        "Executive",
        "Company-wide view of leave and reports (sensitive leave masked); approves their own reporting tree.",
        _SELF_SERVICE + (("leave:read", "COMPANY"), ("report:read", "COMPANY"), ("leave:approve", "REPORTING_TREE"),
                         ("employee:read", "COMPANY"))),
    "finance": (
        "Finance", "Sees leave liability and aggregate reports.",
        _SELF_SERVICE + (("leave:liability_read", "LEGAL_ENTITY"), ("report:read", "COMPANY"))),
}


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _add_grant(bind, company_id: int, role_id: int, code: str, scope: str) -> bool:
    if bind.execute(sa.text(
            "SELECT 1 FROM role_permissions WHERE role_id = :r AND permission_code = :c AND scope = :s"),
            {"r": role_id, "c": code, "s": scope}).first():
        return False
    bind.execute(sa.text(
        "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
        "VALUES (:cid, :r, :c, :s, now(), now())"), {"cid": company_id, "r": role_id, "c": code, "s": scope})
    return True


def _free_name(bind, company_id: int, name: str) -> str:
    candidate, n = name, 1
    while bind.execute(sa.text("SELECT 1 FROM roles WHERE company_id = :c AND lower(name) = lower(:n)"),
                       {"c": company_id, "n": candidate}).first():
        n += 1
        candidate = f"{name} ({n})"
    return candidate


def upgrade() -> None:
    bind = op.get_bind()
    changed = set()
    for key, grants in NEW_GRANTS.items():
        for role_id, company_id in bind.execute(sa.text(
                "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true ORDER BY id"),
                {"k": key}).all():
            for code, scope in grants:
                if _add_grant(bind, company_id, role_id, code, scope):
                    changed.add((company_id, role_id))
    for (company_id,) in bind.execute(sa.text("SELECT id FROM companies ORDER BY id")).all():
        existing = {k for (k,) in bind.execute(sa.text(
            "SELECT system_key FROM roles WHERE company_id = :c AND system_key IS NOT NULL"), {"c": company_id})}
        for key, (name, description, grants) in NEW_ROLES.items():
            if key in existing:
                continue
            role_id = bind.execute(sa.text(
                "INSERT INTO roles (company_id, name, description, system_key, is_system_role, is_protected, "
                "is_full_access, created_at, updated_at) VALUES (:c, :n, :d, :k, true, false, false, now(), now()) "
                "RETURNING id"), {"c": company_id, "n": _free_name(bind, company_id, name), "d": description,
                                  "k": key}).scalar()
            for code, scope in grants:
                _add_grant(bind, company_id, role_id, code, scope)
    for company_id, role_id in sorted(changed):
        bind.execute(sa.text(
            "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
            "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
            {"cid": company_id, "r": role_id})


def downgrade() -> None:
    bind = op.get_bind()
    keys = _in(NEW_ROLES)
    bind.execute(sa.text(
        f"DELETE FROM role_permissions WHERE role_id IN (SELECT r.id FROM roles r WHERE r.system_key IN {keys} "
        "AND NOT EXISTS (SELECT 1 FROM user_role_assignments a WHERE a.role_id = r.id))"))
    bind.execute(sa.text(
        f"DELETE FROM roles r WHERE r.system_key IN {keys} "
        "AND NOT EXISTS (SELECT 1 FROM user_role_assignments a WHERE a.role_id = r.id)"))
    bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_code IN " + _in(NEW_CODES)))
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))
