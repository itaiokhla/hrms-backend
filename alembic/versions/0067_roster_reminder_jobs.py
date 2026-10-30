"""Shift reminders and skill-tag expiry jobs (shift module, FU-AS-53; spine §9.2, §9.3; 03-shifts/02 §14).

Scheduled rows only: ``roster.shift_reminders`` (hourly, slot 13: employees whose published shift starts within the
lead time, ``roster.shift_reminder``) and ``shift.tag_expiry`` (daily-local 07:30: planners, 30 days before an employee
skill tag ends, ``shift.tag_expiring``). Handlers in ``app/services/attendance_jobs.py``.

Revision ID: 0067_roster_reminder_jobs
Revises: 0066_tzdata_impact_scan
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0067_roster_reminder_jobs"
down_revision: Union[str, None] = "0066_tzdata_impact_scan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, enabled, next_run_at) "
        "VALUES ('roster.shift_reminders', 'HOURLY', 13, true, now()) ON CONFLICT (job_type) DO NOTHING"))
    bind.execute(sa.text(
        "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
        "VALUES ('shift.tag_expiry', 'HOURLY', 59, CAST('07:30' AS time), true, now()) "
        "ON CONFLICT (job_type) DO NOTHING"))


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM scheduled_jobs WHERE job_type IN ('roster.shift_reminders', 'shift.tag_expiry')"))
