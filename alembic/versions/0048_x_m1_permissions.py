"""X-M1 team permissions and JC-AS-24 grants (X 01 §10.3, §17; AS 06 O1/O2, CV-7; registry 2026.10.7).

The registry (code) adds the scopes TEAM, TEAM_TREE, ASSIGNED_TEAMS (bundles ``_ALL_T``, ``_TEAM_T``,
``_ALL_WITH_PG_T``), the codes ``team:read``, ``team:manage``, ``team:membership:manage`` and widens
``attendance:read`` (+ PAY_GROUP and team scopes), ``leave:read`` / ``leave:approve`` (team scopes, LH V3-156) and the
payroll-registered ``overtime:*`` (JC-AS-24). This revision seeds existing tenants:

* the seeded system roles get the default grants of X 01 §10.3 that exist today: ``employee`` (``team:read`` SELF),
  ``manager`` (``team:read`` REPORTING_TREE), ``hr_admin`` (the three team codes at COMPANY); the LH roles
  (``hr_executive``, ``hr_manager``, ``department_head``) receive theirs from the single default-grant table when
  their seed creates them (A-19);
* JC-AS-24: ``payroll_manager`` and ``payroll_processor`` get ``attendance:read`` at PAY_GROUP, and every assignment
  of those roles gets **all pay groups of the tenant at migration** as targets (a pay group created later is not added
  automatically; AS 06 note 1).

Holders' ``permission_version`` is bumped. Downgrade removes the team codes, every team-scoped grant and the
PAY_GROUP ``attendance:read`` grants with their targets.

Revision ID: 0048_x_m1_permissions
Revises: 0047_x_m1_teams
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0048_x_m1_permissions"
down_revision: Union[str, None] = "0047_x_m1_teams"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen default grants by system_key (X 01 §10.3; JC-AS-24).
NEW_GRANTS = {
    "employee": (("team:read", "SELF"),),
    "manager": (("team:read", "SELF"), ("team:read", "REPORTING_TREE")),
    "hr_admin": (("team:read", "SELF"), ("team:read", "COMPANY"), ("team:manage", "COMPANY"),
                 ("team:membership:manage", "COMPANY")),
    "payroll_manager": (("team:read", "SELF"), ("attendance:read", "PAY_GROUP")),
    "payroll_processor": (("attendance:read", "PAY_GROUP"),),
}
PAY_GROUP_ROLES = ("payroll_manager", "payroll_processor")


def _add(bind, company_id: int, role_id: int, code: str, scope: str) -> bool:
    if bind.execute(sa.text(
            "SELECT 1 FROM role_permissions WHERE role_id = :r AND permission_code = :c AND scope = :s"),
            {"r": role_id, "c": code, "s": scope}).first():
        return False
    bind.execute(sa.text(
        "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
        "VALUES (:cid, :r, :c, :s, now(), now())"), {"cid": company_id, "r": role_id, "c": code, "s": scope})
    return True


def upgrade() -> None:
    bind = op.get_bind()
    changed = set()
    for key, grants in NEW_GRANTS.items():
        roles = bind.execute(sa.text(
            "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true ORDER BY id"),
            {"k": key}).all()
        for role_id, company_id in roles:
            for code, scope in grants:
                if _add(bind, company_id, role_id, code, scope):
                    changed.add((company_id, role_id))
            if key in PAY_GROUP_ROLES:
                bind.execute(sa.text(
                    "INSERT INTO user_role_assignment_targets (company_id, user_role_assignment_id, scope, target_id, "
                    "created_at, updated_at) "
                    "SELECT a.company_id, a.id, 'PAY_GROUP', g.id, now(), now() FROM user_role_assignments a "
                    "JOIN pay_groups g ON g.company_id = a.company_id "
                    "WHERE a.company_id = :cid AND a.role_id = :r AND NOT EXISTS ("
                    "  SELECT 1 FROM user_role_assignment_targets t WHERE t.user_role_assignment_id = a.id "
                    "  AND t.scope = 'PAY_GROUP' AND t.target_id = g.id)"), {"cid": company_id, "r": role_id})
    for company_id, role_id in sorted(changed):
        bind.execute(sa.text(
            "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
            "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
            {"cid": company_id, "r": role_id})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM role_permissions WHERE permission_code LIKE 'team:%' "
        "OR scope IN ('TEAM','TEAM_TREE','ASSIGNED_TEAMS') "
        "OR (permission_code = 'attendance:read' AND scope = 'PAY_GROUP')"))
    bind.execute(sa.text(
        "DELETE FROM user_role_assignment_targets t USING user_role_assignments a, roles r "
        "WHERE t.user_role_assignment_id = a.id AND a.role_id = r.id AND t.scope = 'PAY_GROUP' "
        "AND r.system_key IN ('payroll_manager', 'payroll_processor') AND NOT EXISTS ("
        "  SELECT 1 FROM role_permissions p WHERE p.role_id = r.id AND p.scope = 'PAY_GROUP')"))
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))
