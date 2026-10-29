"""Team attendance: managers punch for their team and delete (void) team sessions (IG-AS-98..100).

* permission ``attendance:proxy_punch`` (registry 2026.10.10): system ``manager`` roles DIRECT_REPORTS, ``hr_admin``
  COMPANY; holders' ``permission_version`` is bumped;
* ``attendance.void_reason`` accepts ``ADMIN`` (a session deleted by HR or a manager on a v1 tenant; v2 tenants void
  through a DIRECT ``VOID_SESSION`` request, reason ``REQUEST``).

Settings ``attendance.team.*`` are registry entries (registry 2026.10.11; no rows). Downgrade refuses while ADMIN
voids exist, then removes the grants.

Revision ID: 0054_attendance_team
Revises: 0053_as_m4_permissions
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0054_attendance_team"
down_revision: Union[str, None] = "0053_as_m4_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_VOID_REASONS = ("REQUEST", "REPAIRED", "DUPLICATE")
NEW_VOID_REASONS = OLD_VOID_REASONS + ("ADMIN",)
NEW_GRANTS = {
    "hr_admin": (("attendance:proxy_punch", "COMPANY"),),
    "manager": (("attendance:proxy_punch", "DIRECT_REPORTS"),),
}


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _void_check(values) -> None:
    op.drop_constraint("ck_attendance_void_reason", "attendance", type_="check")
    op.create_check_constraint("ck_attendance_void_reason", "attendance",
                               f"void_reason IS NULL OR void_reason IN {_in(values)}")


def upgrade() -> None:
    bind = op.get_bind()
    _void_check(NEW_VOID_REASONS)
    changed = set()
    for key, grants in NEW_GRANTS.items():
        roles = bind.execute(sa.text(
            "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true ORDER BY id"),
            {"k": key}).all()
        for role_id, company_id in roles:
            for code, scope in grants:
                if bind.execute(sa.text("SELECT 1 FROM role_permissions WHERE role_id = :r AND permission_code = :c "
                                        "AND scope = :s"), {"r": role_id, "c": code, "s": scope}).first():
                    continue
                bind.execute(sa.text(
                    "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
                    "VALUES (:cid, :r, :c, :s, now(), now())"), {"cid": company_id, "r": role_id, "c": code, "s": scope})
                changed.add((company_id, role_id))
    for company_id, role_id in sorted(changed):
        bind.execute(sa.text(
            "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
            "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
            {"cid": company_id, "r": role_id})


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM attendance WHERE void_reason = 'ADMIN'")).scalar():
        raise RuntimeError("sessions deleted by an administrator exist (void_reason ADMIN)")
    _void_check(OLD_VOID_REASONS)
    bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_code = 'attendance:proxy_punch'"))
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))
