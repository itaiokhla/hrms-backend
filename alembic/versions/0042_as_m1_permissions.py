"""AS-M1 shift and roster permissions (AS 06 §3 S1-S9, §4.1; 03-shifts/01 §15; A-14; registry 2026.10.6).

The registry (code) adds ``shift:read``, ``shift:manage``, ``shift:assign`` and ``roster:read|plan|publish|request|
approve|exempt`` (``roster:exempt`` explicit-only, restricted, never partner-delegable). This revision seeds existing
tenants so behaviour is preserved and the shift pages open for the people who could edit shifts before:

* grant copy (A-14, fixes BUG-AS-38): every (``department:manage``, COMPANY) grant of any role, system or custom,
  gets (``shift:manage``, COMPANY) and (``shift:assign``, COMPANY);
* the seeded system roles get the default grants of AS 06 §4.1 that exist today (employee, manager, hr_admin,
  auditor and the self-service set of the roles built on it).

Holders' ``permission_version`` is bumped. Downgrade removes every ``shift:*`` / ``roster:*`` grant.

Revision ID: 0042_as_m1_permissions
Revises: 0041_as_m1_shift_rules
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_as_m1_permissions"
down_revision: Union[str, None] = "0041_as_m1_shift_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen default grants by system_key (AS 06 §4.1; roles that do not exist yet arrive with LH-M2 / X-M2).
SELF_SERVICE = (("shift:read", "SELF"), ("roster:read", "SELF"), ("roster:request", "SELF"))
NEW_GRANTS = {
    "employee": SELF_SERVICE,
    "payroll_manager": SELF_SERVICE,
    "manager": SELF_SERVICE + (("shift:read", "REPORTING_TREE"), ("roster:read", "REPORTING_TREE"),
                               ("roster:approve", "REPORTING_TREE")),
    "hr_admin": SELF_SERVICE + tuple((code, "COMPANY") for code in (
        "shift:read", "shift:manage", "shift:assign", "roster:read", "roster:plan", "roster:publish",
        "roster:approve")),
    "auditor": (("shift:read", "COMPANY"), ("roster:read", "COMPANY")),
}


def _bump(bind, company_id: int, role_id: int) -> None:
    bind.execute(sa.text(
        "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
        "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
        {"cid": company_id, "r": role_id})


def _add(bind, company_id: int, role_id: int, code: str, scope: str) -> bool:
    exists = bind.execute(sa.text(
        "SELECT 1 FROM role_permissions WHERE role_id = :r AND permission_code = :c AND scope = :s"),
        {"r": role_id, "c": code, "s": scope}).first()
    if exists:
        return False
    bind.execute(sa.text(
        "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
        "VALUES (:cid, :r, :c, :s, now(), now())"), {"cid": company_id, "r": role_id, "c": code, "s": scope})
    return True


def upgrade() -> None:
    bind = op.get_bind()
    changed = set()
    holders = bind.execute(sa.text(
        "SELECT DISTINCT r.id, r.company_id FROM roles r JOIN role_permissions p ON p.role_id = r.id "
        "WHERE p.permission_code = 'department:manage' AND p.scope = 'COMPANY' ORDER BY r.id")).all()
    for role_id, company_id in holders:
        for code in ("shift:manage", "shift:assign"):
            if _add(bind, company_id, role_id, code, "COMPANY"):
                changed.add((company_id, role_id))
    for key, grants in NEW_GRANTS.items():
        roles = bind.execute(sa.text(
            "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true ORDER BY id"),
            {"k": key}).all()
        for role_id, company_id in roles:
            for code, scope in grants:
                if _add(bind, company_id, role_id, code, scope):
                    changed.add((company_id, role_id))
    for company_id, role_id in sorted(changed):
        _bump(bind, company_id, role_id)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_code LIKE 'shift:%' OR permission_code LIKE 'roster:%'"))
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))
