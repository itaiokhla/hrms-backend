"""Roster jobs of AS-M5 (shift module S2; 03-shifts/02 §4.2, §14; spine §9.2 / notification ``roster.publish_due``).

Scheduled rows only (no schema change): ``roster.close_periods`` (daily-local 00:40: published periods that ended
and left payroll OPEN become CLOSED) and ``roster.publish_reminders`` (daily-local 08:00: planners of periods whose
``publish_due_at`` is near or past). Handlers live in ``app/services/attendance_jobs.py``.

Revision ID: 0059_shift_roster_jobs
Revises: 0058_remaining_goals
Create Date: 2026-10-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0059_shift_roster_jobs"
down_revision: Union[str, None] = "0058_remaining_goals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEDULED = (("roster.close_periods", "HOURLY", 41, "00:40"), ("roster.publish_reminders", "HOURLY", 43, "08:00"))


def upgrade() -> None:
    bind = op.get_bind()
    for job_type, cadence, minute, local in SCHEDULED:
        bind.execute(sa.text(
            "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
            "VALUES (:t, :c, :m, CAST(:l AS time), true, now()) ON CONFLICT (job_type) DO NOTHING"),
            {"t": job_type, "c": cadence, "m": minute, "l": local})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM scheduled_jobs WHERE job_type IN ('roster.close_periods', 'roster.publish_reminders')"))
