"""tz database impact scan (shift module, FU-AS-53; 02-system-design §7.4 runbook TZDATA-1, EC AS-TZ-03).

* ``tz_zone_snapshots`` (tenant, RLS): per company and zone in use, the UTC offset at the window start and every
  offset change over the next 24 months as the last scan saw them.
* Scheduled row ``tzdata.impact_scan`` (daily-local 03:10): compares the installed tz database with the snapshots;
  published entries whose instants move get one SYSTEM DIRECT publication (reason ``TZ_RULE_CHANGE``), planners and
  employees are told, and validation re-runs. Handler in ``app/services/attendance_jobs.py``.

Revision ID: 0066_tzdata_impact_scan
Revises: 0065_roster_compliance_rescan
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0066_tzdata_impact_scan"
down_revision: Union[str, None] = "0065_roster_compliance_rescan"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TABLE = "tz_zone_snapshots"


def _app_roles(bind) -> list:
    return [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"""CREATE TABLE {TABLE} (
        id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        zone VARCHAR(64) NOT NULL, tzdata_version VARCHAR(16) NOT NULL, window_from TIMESTAMPTZ NOT NULL,
        window_to TIMESTAMPTZ NOT NULL, initial_offset_minutes INTEGER NOT NULL,
        transitions JSONB NOT NULL DEFAULT '[]'::jsonb,
        CONSTRAINT uq_tz_zone_snapshots_zone UNIQUE (company_id, zone),
        CONSTRAINT ck_tz_zone_snapshots_transitions CHECK (jsonb_typeof(transitions) = 'array'),
        CONSTRAINT ck_tz_zone_snapshots_window CHECK (window_to > window_from))""")
    op.execute(f"CREATE INDEX ix_{TABLE}_company_id ON {TABLE} (company_id)")
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {TABLE} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {TABLE}_id_seq TO {quoted}")
    bind.execute(sa.text(
        "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
        "VALUES ('tzdata.impact_scan', 'HOURLY', 57, CAST('03:10' AS time), true, now()) "
        "ON CONFLICT (job_type) DO NOTHING"))


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type = 'tzdata.impact_scan'"))
    op.execute(f"DROP TABLE {TABLE}")
