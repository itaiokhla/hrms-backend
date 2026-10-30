"""Roster compliance rescan job (shift module, FU-AS-53; 03-shifts/02 §6.5, appendix-a §14 step 6b, EC AS-CTY-06).

Scheduled row only: ``roster.compliance_rescan`` (daily-local 02:20) re-validates published future entries against
the working-time rules and notifies planners per branch when the findings change. Handler in
``app/services/attendance_jobs.py`` (service ``roster_consents.compliance_rescan``).

Revision ID: 0065_roster_compliance_rescan
Revises: 0064_lh_m2_permissions
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0065_roster_compliance_rescan"
down_revision: Union[str, None] = "0064_lh_m2_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
        "VALUES ('roster.compliance_rescan', 'HOURLY', 53, CAST('02:20' AS time), true, now()) "
        "ON CONFLICT (job_type) DO NOTHING"))


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type = 'roster.compliance_rescan'"))
