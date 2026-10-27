"""LH-M1 scheduler (LH order 2; LH 11 §4.2.15, LH 02 §8.2-§8.3, LH 14 WI-1.1): the platform table scheduled_jobs.

A platform table without company_id and without row-level security (a reviewed exception of the platform contract
tests): one row per periodic job type. The application role gets SELECT and UPDATE only (the tick moves
next_run_at); rows are seeded here. Types whose handler exists (idempotency.purge, payroll.generate_periods) or ships in this goal are enabled with next_run_at = now() so
the first tick fires them; types of later goals are seeded disabled. Daily-local types have HOURLY rows with a local
threshold (at_local_time), because the fan-out must look at every zone once an hour (LH 02 §8.2 rule 1).

Downgrade drops the table.

Revision ID: 0039_lh_m1_scheduler
Revises: 0038_lh_m1_permissions
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_lh_m1_scheduler"
down_revision: Union[str, None] = "0038_lh_m1_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy: (job_type, cadence, at_minute, at_local_time, enabled). Daily-local = HOURLY + at_local_time.
SEED = (
    ("idempotency.purge", "DAILY", 90, None, True),
    ("holiday.link_activate", "HOURLY", 5, "00:30", True),
    ("holiday.rollover", "DAILY", 120, None, False),
    ("holiday.reminders", "HOURLY", 10, "08:00", False),
    ("leave.accrue", "HOURLY", 15, "00:30", False),
    ("leave.expire", "HOURLY", 20, "00:30", False),
    ("leave.expiry_reminders", "HOURLY", 25, "00:30", False),
    ("leave.substitute_credits", "HOURLY", 30, "00:30", False),
    ("leave.year_end", "HOURLY", 35, "00:30", False),
    ("leave.sla_scan", "HOURLY", 40, None, False),
    ("leave.analytics_refresh", "HOURLY", 45, "02:00", False),
    ("leave.balances_verify", "DAILY", 180, None, False),
    ("notifications.digest", "HOURLY", 50, "08:00", False),
    ("payroll.generate_periods", "DAILY", 60, None, True),  # BUG-29: replaces the self-reschedule
)


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("job_type", sa.String(60), nullable=False, unique=True),
        sa.Column("cadence", sa.String(8), nullable=False),
        sa.Column("at_minute", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("at_local_time", sa.Time(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.CheckConstraint("cadence IN ('HOURLY', 'DAILY', 'YEARLY')", name="ck_scheduled_jobs_cadence"),
        sa.CheckConstraint("at_minute BETWEEN 0 AND 1439", name="ck_scheduled_jobs_at_minute"),
    )
    for job_type, cadence, minute, local, enabled in SEED:
        bind.execute(sa.text(
            "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
            "VALUES (:t, :c, :m, CAST(:l AS time), :e, CASE WHEN :e THEN now() END)"),
            {"t": job_type, "c": cadence, "m": minute, "l": local, "e": enabled})
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, UPDATE ON scheduled_jobs TO {quoted}")


def downgrade() -> None:
    op.drop_table("scheduled_jobs")
