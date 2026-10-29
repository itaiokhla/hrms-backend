"""AS-M6 shift changes, swaps, open shifts, availability and skill tags (AS 03-shifts/03; spine §3.11, §4.3, §4.4).

NEW tenant tables (RLS ``tenant_isolation``, unique ``(company_id, id)``, composite same-company foreign keys, grants):
``open_shifts``, ``shift_requests`` (one open request per entry, one open claim per open shift and claimant — partial
unique indexes), ``employee_availability``, ``skill_tags``, ``employee_skill_tags`` (app/models/shift_requests.py).

``roster_entries.request_id`` (a plain column since AS-M5) gets its foreign key to ``shift_requests``.

Scheduled sweepers (hourly): ``shift_request.expire`` (slot 7) and ``open_shift.expire`` (slot 9); every transition
also checks expiry under the request lock, so the jobs only tidy up.

Downgrade refuses while any shift request or open shift exists.

Revision ID: 0057_as_m6_shift_requests
Revises: 0056_as_m8_m9
Create Date: 2026-10-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0057_as_m6_shift_requests"
down_revision: Union[str, None] = "0056_as_m8_m9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
KINDS = ("SWAP", "DROP", "RELEASE", "PICKUP", "CHANGE")
STATUSES = ("PENDING_ACCEPTANCE", "PENDING_APPROVAL", "APPROVED", "DECLINED", "REJECTED", "CANCELLED", "EXPIRED",
            "SUPERSEDED")
OPEN = "status IN ('PENDING_ACCEPTANCE', 'PENDING_APPROVAL')"


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(table: str, column: str, target: str, name: str) -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE RESTRICT")


TABLES = {
    "skill_tags": f"""
        {BASE}, code VARCHAR(40) NOT NULL, name VARCHAR(100) NOT NULL, status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE',
        CONSTRAINT uq_skill_tags_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_skill_tags_code UNIQUE (company_id, code),
        CONSTRAINT ck_skill_tags_status CHECK (status IN ('ACTIVE', 'ARCHIVED'))""",
    "employee_skill_tags": f"""
        {BASE}, employee_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, valid_to DATE,
        CONSTRAINT uq_employee_skill_tags_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_employee_skill_tags_pair UNIQUE (company_id, employee_id, tag_id),
        {_fk('employee_skill_tags', 'employee_id', 'employees', 'employee')},
        {_fk('employee_skill_tags', 'tag_id', 'skill_tags', 'tag')}""",
    "open_shifts": f"""
        {BASE}, roster_period_id INTEGER, team_id INTEGER, branch_id INTEGER NOT NULL, site_id INTEGER,
        shift_date DATE NOT NULL, shift_id INTEGER, custom_start_time VARCHAR(5), custom_end_time VARCHAR(5),
        required_count SMALLINT NOT NULL DEFAULT 1, filled_count SMALLINT NOT NULL DEFAULT 0,
        eligibility JSONB NOT NULL DEFAULT '{{}}'::jsonb, priority_until TIMESTAMPTZ,
        approval_required BOOLEAN NOT NULL DEFAULT true, status VARCHAR(10) NOT NULL DEFAULT 'OPEN',
        source VARCHAR(10) NOT NULL DEFAULT 'MANUAL', released_from_entry_id INTEGER, released_by_employee_id INTEGER,
        posted_at TIMESTAMPTZ NOT NULL, filled_at TIMESTAMPTZ, expires_at TIMESTAMPTZ NOT NULL, note VARCHAR(500),
        warnings JSONB NOT NULL DEFAULT '[]'::jsonb, created_by_user_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_open_shifts_company_id_id UNIQUE (company_id, id),
        {_fk('open_shifts', 'roster_period_id', 'roster_periods', 'period')},
        {_fk('open_shifts', 'team_id', 'teams', 'team')},
        {_fk('open_shifts', 'branch_id', 'branches', 'branch')},
        {_fk('open_shifts', 'site_id', 'attendance_sites', 'site')},
        {_fk('open_shifts', 'shift_id', 'shifts', 'shift')},
        {_fk('open_shifts', 'released_from_entry_id', 'roster_entries', 'released_entry')},
        {_fk('open_shifts', 'released_by_employee_id', 'employees', 'released_by')},
        CONSTRAINT ck_open_shifts_status CHECK (status IN ('OPEN', 'FILLED', 'CANCELLED', 'EXPIRED')),
        CONSTRAINT ck_open_shifts_source CHECK (source IN ('MANUAL', 'RELEASED', 'UNCOVERED')),
        CONSTRAINT ck_open_shifts_required CHECK (required_count BETWEEN 1 AND 50),
        CONSTRAINT ck_open_shifts_filled CHECK (filled_count >= 0 AND filled_count <= required_count),
        CONSTRAINT ck_open_shifts_times CHECK (shift_id IS NOT NULL OR
            (custom_start_time IS NOT NULL AND custom_end_time IS NOT NULL)),
        CONSTRAINT ck_open_shifts_filled_at CHECK ((status = 'FILLED') = (filled_at IS NOT NULL)),
        CONSTRAINT ck_open_shifts_released CHECK (source <> 'RELEASED' OR released_from_entry_id IS NOT NULL),
        CONSTRAINT ck_open_shifts_row_version CHECK (row_version >= 1)""",
    "shift_requests": f"""
        {BASE}, kind VARCHAR(8) NOT NULL, status VARCHAR(18) NOT NULL, requester_employee_id INTEGER NOT NULL,
        entry_id INTEGER, target_employee_id INTEGER, counter_entry_id INTEGER, open_shift_id INTEGER,
        shift_date DATE NOT NULL, counter_date DATE, requested_change JSONB, reason VARCHAR(500),
        acknowledged_warnings JSONB NOT NULL DEFAULT '[]'::jsonb, checks JSONB NOT NULL DEFAULT '[]'::jsonb,
        approval_required BOOLEAN NOT NULL DEFAULT true, expires_at TIMESTAMPTZ, submitted_at TIMESTAMPTZ NOT NULL,
        accepted_at TIMESTAMPTZ, decided_at TIMESTAMPTZ, decided_by_user_id INTEGER, decision_note VARCHAR(500),
        applied_publication_id INTEGER, close_reason VARCHAR(40), refile_of_request_id INTEGER,
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_shift_requests_company_id_id UNIQUE (company_id, id),
        {_fk('shift_requests', 'requester_employee_id', 'employees', 'requester')},
        {_fk('shift_requests', 'target_employee_id', 'employees', 'target')},
        {_fk('shift_requests', 'entry_id', 'roster_entries', 'entry')},
        {_fk('shift_requests', 'counter_entry_id', 'roster_entries', 'counter_entry')},
        {_fk('shift_requests', 'open_shift_id', 'open_shifts', 'open_shift')},
        {_fk('shift_requests', 'applied_publication_id', 'roster_publications', 'publication')},
        {_fk('shift_requests', 'decided_by_user_id', 'users', 'decided_by')},
        {_fk('shift_requests', 'refile_of_request_id', 'shift_requests', 'refile_of')},
        CONSTRAINT ck_shift_requests_kind CHECK (kind IN {_in(KINDS)}),
        CONSTRAINT ck_shift_requests_status CHECK (status IN {_in(STATUSES)}),
        CONSTRAINT ck_shift_requests_pickup CHECK ((kind = 'PICKUP') = (open_shift_id IS NOT NULL AND entry_id IS NULL)),
        CONSTRAINT ck_shift_requests_peer CHECK (kind NOT IN ('SWAP', 'DROP') OR target_employee_id IS NOT NULL),
        CONSTRAINT ck_shift_requests_change CHECK (kind <> 'CHANGE' OR requested_change IS NOT NULL),
        CONSTRAINT ck_shift_requests_not_self CHECK (target_employee_id IS NULL OR
            target_employee_id <> requester_employee_id),
        CONSTRAINT ck_shift_requests_acceptance CHECK (status <> 'PENDING_ACCEPTANCE' OR kind IN ('SWAP', 'DROP'))""",
    "employee_availability": f"""
        {BASE}, employee_id INTEGER NOT NULL, kind VARCHAR(12) NOT NULL DEFAULT 'UNAVAILABLE', weekday SMALLINT,
        start_date DATE NOT NULL, end_date DATE, start_time VARCHAR(5), end_time VARCHAR(5), note VARCHAR(300),
        status VARCHAR(10) NOT NULL DEFAULT 'PENDING', decided_by_user_id INTEGER, decided_at TIMESTAMPTZ,
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_employee_availability_company_id_id UNIQUE (company_id, id),
        {_fk('employee_availability', 'employee_id', 'employees', 'employee')},
        {_fk('employee_availability', 'decided_by_user_id', 'users', 'decided_by')},
        CONSTRAINT ck_employee_availability_kind CHECK (kind IN ('UNAVAILABLE', 'PREFERRED')),
        CONSTRAINT ck_employee_availability_status CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
        CONSTRAINT ck_employee_availability_weekday CHECK (weekday IS NULL OR weekday BETWEEN 0 AND 6),
        CONSTRAINT ck_employee_availability_range CHECK (end_date IS NULL OR end_date >= start_date),
        CONSTRAINT ck_employee_availability_times CHECK ((start_time IS NULL) = (end_time IS NULL))""",
}
INDEXES = (
    "CREATE INDEX ix_open_shifts_date ON open_shifts (company_id, shift_date, status)",
    f"CREATE UNIQUE INDEX uq_shift_requests_open_entry ON shift_requests (company_id, entry_id) "
    f"WHERE {OPEN} AND entry_id IS NOT NULL",
    f"CREATE UNIQUE INDEX uq_shift_requests_open_claim ON shift_requests (company_id, open_shift_id, "
    f"requester_employee_id) WHERE {OPEN} AND open_shift_id IS NOT NULL",
    "CREATE INDEX ix_shift_requests_requester ON shift_requests (company_id, requester_employee_id, status)",
    "CREATE INDEX ix_shift_requests_target ON shift_requests (company_id, target_employee_id, status)",
    "CREATE INDEX ix_employee_availability_employee ON employee_availability (company_id, employee_id, status)",
)
SCHEDULED = (("shift_request.expire", "HOURLY", 7), ("open_shift.expire", "HOURLY", 9))


def _app_roles(bind) -> list:
    return [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]


def upgrade() -> None:
    bind = op.get_bind()
    for table, body in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({body})")
        op.execute(f"CREATE INDEX ix_{table}_company_id ON {table} (company_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    for statement in INDEXES:
        op.execute(statement)
    op.execute("ALTER TABLE roster_entries ADD CONSTRAINT fk_roster_entries_request FOREIGN KEY (company_id, request_id) "
               "REFERENCES shift_requests (company_id, id) ON DELETE RESTRICT")
    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
    for job_type, cadence, minute in SCHEDULED:
        bind.execute(sa.text(
            "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, enabled, next_run_at) "
            "VALUES (:t, :c, :m, true, now()) ON CONFLICT (job_type) DO NOTHING"),
            {"t": job_type, "c": cadence, "m": minute})


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text("SELECT (SELECT count(*) FROM shift_requests) + (SELECT count(*) FROM open_shifts)")).scalar()
    if used:
        raise RuntimeError("Shift requests or open shifts exist; refusing to drop the AS-M6 tables.")
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type IN ('shift_request.expire', 'open_shift.expire')"))
    op.execute("ALTER TABLE roster_entries DROP CONSTRAINT IF EXISTS fk_roster_entries_request")
    for table in reversed(list(TABLES)):
        op.execute(f"DROP TABLE IF EXISTS {table}")
