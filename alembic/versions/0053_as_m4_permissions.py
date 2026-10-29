"""AS-M4 permissions: grant copy for the request and approval codes (spine §7.1; AS 04-attendance/04 §5.1 OD-8,
§9.1; JC-AS-14). Registry 2026.10.9.

* every role holding ``attendance:regularize`` at a scope also gets ``attendance:approve`` and
  ``attendance:request_on_behalf`` at that scope (the engine module ``attendance`` now needs ``attendance:approve``,
  so PENDING on-duty requests keep an eligible approver);
* every role holding ``attendance:mark`` (SELF) gets ``attendance:request`` (SELF);
* system ``hr_admin`` roles get ``attendance:override``, ``attendance:muster``, ``attendance:close_period`` (COMPANY).

Holders' ``permission_version`` is bumped. The engine re-routes pending requests on the next decision or job
(``approvals.reroute_pending``). Downgrade removes the six codes.

Revision ID: 0053_as_m4_permissions
Revises: 0052_as_m4_requests
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0053_as_m4_permissions"
down_revision: Union[str, None] = "0052_as_m4_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_CODES = ("attendance:request", "attendance:request_on_behalf", "attendance:approve", "attendance:override",
             "attendance:muster", "attendance:close_period")
COPIES = (("attendance:regularize", ("attendance:approve", "attendance:request_on_behalf")),
          ("attendance:mark", ("attendance:request",)))
HR_ADMIN = (("attendance:override", "COMPANY"), ("attendance:muster", "COMPANY"),
            ("attendance:close_period", "COMPANY"))
# The default-role grants this revision adds (registry 2026.10.9 DEFAULT_ROLES); the grant copy above gives the same
# rows to the seeded roles, and the literal keeps tests/payroll/test_permissions_v2.py's registry comparison exact.
NEW_GRANTS = {
    "hr_admin": (("attendance:request", "SELF"), ("attendance:approve", "COMPANY"),
                 ("attendance:request_on_behalf", "COMPANY")) + HR_ADMIN,
    "payroll_manager": (("attendance:request", "SELF"),),
    "manager": (("attendance:request", "SELF"), ("attendance:approve", "DIRECT_REPORTS"),
                ("attendance:request_on_behalf", "DIRECT_REPORTS")),
    "employee": (("attendance:request", "SELF"),),
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


def upgrade() -> None:
    bind = op.get_bind()
    changed = set()
    for source, targets in COPIES:
        rows = bind.execute(sa.text(
            "SELECT company_id, role_id, scope FROM role_permissions WHERE permission_code = :c ORDER BY id"),
            {"c": source}).all()
        for company_id, role_id, scope in rows:
            for code in targets:
                if code == "attendance:request" and scope != "SELF":
                    continue
                if _add_grant(bind, company_id, role_id, code, scope):
                    changed.add((company_id, role_id))
    for key, grants in NEW_GRANTS.items():
        roles = bind.execute(sa.text(
            "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true ORDER BY id"),
            {"k": key}).all()
        for role_id, company_id in roles:
            for code, scope in grants:
                if _add_grant(bind, company_id, role_id, code, scope):
                    changed.add((company_id, role_id))
    for company_id, role_id in sorted(changed):
        bind.execute(sa.text(
            "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
            "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
            {"cid": company_id, "r": role_id})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_code IN " + _in(NEW_CODES)))
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))
