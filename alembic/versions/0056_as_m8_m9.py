"""AS-M8 overtime proposals and AS-M9 registers (G-18, G-20).

* permission ``attendance:export`` (registry 2026.10.11): system ``hr_admin`` and ``auditor`` roles COMPANY; holders'
  ``permission_version`` is bumped;
* scheduled job ``attendance.ot_proposals`` (daily-local 02:10, hourly slot 41): overtime proposals from FINAL
  attendance days (04-attendance/05 §5).

No table changes: proposals live in payroll's ``overtime_entries`` (D-AS-29) and registers render the evaluation
ledger (D-AS-87). Downgrade removes the job row and the grants.

Revision ID: 0056_as_m8_m9
Revises: 0055_as_m7_devices
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0056_as_m8_m9"
down_revision: Union[str, None] = "0055_as_m7_devices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_GRANTS = {
    "hr_admin": (("attendance:export", "COMPANY"),),
    "auditor": (("attendance:export", "COMPANY"),),
}
SCHEDULED = (("attendance.ot_proposals", "HOURLY", 41, "02:10"),)


def upgrade() -> None:
    bind = op.get_bind()
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
    for job_type, cadence, minute, local in SCHEDULED:
        bind.execute(sa.text(
            "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
            "VALUES (:t, :c, :m, CAST(:l AS time), true, now()) ON CONFLICT (job_type) DO NOTHING"),
            {"t": job_type, "c": cadence, "m": minute, "l": local})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type = 'attendance.ot_proposals'"))
    bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_code = 'attendance:export'"))
    bind.execute(sa.text("UPDATE users SET permission_version = permission_version + 1"))
