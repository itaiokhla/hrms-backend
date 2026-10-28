"""LH-M1 holiday imports and rollover state (LH 11 §4.2.14, §8; 04-holiday/02 §8, §9; LH 14 WI-1.4, WI-1.11).

* NEW ``holiday_imports`` (adopted, SPINE V3-031): one import (dry run, then commit) of a CSV or ICS file into one
  calendar year; ``report`` holds the parsed lines with their classification and errors, ``target_hash`` the
  content hash of the target year at dry run (a commit against a changed year is refused).
* ``holiday_calendar_years`` gains the rollover state of the Next year page: ``generation_status`` (NONE / OK /
  FAILED), ``generation_error`` and ``generation_summary`` (counts of the last generation).
* The scheduled rows ``holiday.rollover`` (DAILY) and ``holiday.reminders`` (daily-local 08:00) seeded disabled by
  0039 are enabled now that their handlers ship (``app/services/holiday_rollover.py``).

Revision ID: 0046_lh_m1_holiday_imports
Revises: 0045_as_m5_rosters
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046_lh_m1_holiday_imports"
down_revision: Union[str, None] = "0045_as_m5_rosters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
KINDS = ("CSV_DATED", "CSV_RULES", "ICS", "PACK", "CONNECTOR")
STATUSES = ("DRY_RUN", "COMMITTED", "FAILED", "EXPIRED")
GENERATION = ("NONE", "OK", "FAILED")
JOBS = ("holiday.rollover", "holiday.reminders")


def _secure(bind, table):
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "holiday_imports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("calendar_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("target_year", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="DRY_RUN"),
        sa.Column("file_name", sa.String(200), nullable=True),
        sa.Column("file_path", sa.String(300), nullable=True),
        sa.Column("sha256", sa.CHAR(64), nullable=True),
        sa.Column("source_id", sa.String(60), nullable=True),
        sa.Column("source_version", sa.String(40), nullable=True),
        sa.Column("report", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("target_hash", sa.CHAR(64), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("company_id", "id", name="uq_holiday_imports_company_id_id"),
        sa.ForeignKeyConstraint(["company_id", "calendar_id"], ["holiday_calendars.company_id", "holiday_calendars.id"],
                                name="fk_holiday_imports_calendar", ondelete="CASCADE"),
        sa.CheckConstraint(f"kind IN {KINDS}", name="ck_holiday_imports_kind"),
        sa.CheckConstraint(f"status IN {STATUSES}", name="ck_holiday_imports_status"),
        sa.CheckConstraint("target_year BETWEEN 2000 AND 2100", name="ck_holiday_imports_year"),
        sa.CheckConstraint("row_version >= 1", name="ck_holiday_imports_row_version"),
    )
    op.create_index("ix_holiday_imports_company_id", "holiday_imports", ["company_id"])
    op.create_index("ix_holiday_imports_calendar", "holiday_imports", ["company_id", "calendar_id", "target_year"])
    _secure(bind, "holiday_imports")

    op.add_column("holiday_calendar_years", sa.Column("generation_status", sa.String(8), nullable=False,
                                                      server_default="NONE"))
    op.add_column("holiday_calendar_years", sa.Column("generation_error", sa.String(500), nullable=True))
    op.add_column("holiday_calendar_years", sa.Column("generation_summary", postgresql.JSONB(none_as_null=True),
                                                      nullable=True))
    op.create_check_constraint("ck_holiday_calendar_years_generation", "holiday_calendar_years",
                               f"generation_status IN {GENERATION}")
    op.execute("UPDATE holiday_calendar_years SET generation_status = 'OK' WHERE generated_at IS NOT NULL")
    op.execute(f"UPDATE scheduled_jobs SET enabled = true, next_run_at = COALESCE(next_run_at, now()) "
               f"WHERE job_type IN {JOBS}")


def downgrade() -> None:
    op.execute(f"UPDATE scheduled_jobs SET enabled = false WHERE job_type IN {JOBS}")
    op.drop_constraint("ck_holiday_calendar_years_generation", "holiday_calendar_years", type_="check")
    for column in ("generation_summary", "generation_error", "generation_status"):
        op.drop_column("holiday_calendar_years", column)
    op.drop_table("holiday_imports")
