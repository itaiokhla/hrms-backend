"""Rotation auto-publish job (shift module S2; 03-shifts/02 §3.5, EC AS-REL-06).

Scheduled row only: ``roster.rotation_generate`` (daily-local 01:10) publishes the days of auto-publish rotation
assignments up to ``shift.roster.horizon_days`` ahead (publication kind ``ROTATION_AUTO``, existing column
``rotation_assignments.auto_publish`` and CHECK value since 0045). Handler in ``app/services/attendance_jobs.py``.

Revision ID: 0060_roster_rotation_generate
Revises: 0059_shift_roster_jobs
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0060_roster_rotation_generate"
down_revision: Union[str, None] = "0059_shift_roster_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
        "VALUES ('roster.rotation_generate', 'HOURLY', 47, CAST('01:10' AS time), true, now()) "
        "ON CONFLICT (job_type) DO NOTHING"))


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type = 'roster.rotation_generate'"))
