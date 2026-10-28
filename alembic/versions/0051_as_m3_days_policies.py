"""AS-M3 evaluations, part 1: ``attendance_days`` and ``attendance_day_versions``, the change-log record types of
JC-AS-11b and a BIGINT ``source_change_log.record_id`` (spine §3.3; AS 11 §1.7, §4.2–§4.3; AS 05 §7).

NEW tenant tables (RLS ``tenant_isolation``, unique ``(company_id, id)``, composite same-company foreign keys, grants;
app/models/attendance.py documents them):

* ``attendance_days`` (BIGSERIAL; one row per employee and shift date, derived and rebuildable; written only by the
  evaluation write path ``app/services/attendance_days.py``). ``override_request_id`` gets its foreign key in
  ``as_m4_requests``; ``roster_entry_id`` points at the ``roster_entries`` that ``as_m5_rosters`` already created;
* ``attendance_day_versions`` (BIGSERIAL, append-only: trigger ``attendance_day_versions_guard`` refuses UPDATE /
  DELETE, the app role holds SELECT, INSERT) — the payroll lineage of every decided version (INV-21).

``source_change_log``: ``record_id`` becomes BIGINT (``attendance_days.id`` is BIGSERIAL, A-07) and the record-type
CHECK accepts ``attendance_day``, ``attendance_request``, ``roster_entry``.

No data step: rows come from the day close and the cut-over jobs. The settings keys (``attendance.v2.enabled`` …)
and ``ATTENDANCE_RULES_POLICY`` are registry entries (no rows; legacy tenants resolve the registry defaults).

Downgrade refuses while any tenant has ``attendance.v2.enabled`` (V2_ACTIVE) or the change log holds the new record
types (append-only history), then drops everything above.

Revision ID: 0051_as_m3_days_policies
Revises: 0050_as_m2_sites_consents
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0051_as_m3_days_policies"
down_revision: Union[str, None] = "0050_as_m2_sites_consents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BIG_BASE = ("id BIGSERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")

# Frozen copies of the vocabularies (AS 11 §5 at this revision).
EVALUATION_STATES = ("OPEN", "PROVISIONAL", "FINAL")
RULES_MODES = ("LEGACY_V1", "STANDARD")
PORTION_RESULTS = ("PRESENT", "ABSENT", "MISSING_PUNCH", "UNMARKED", "PENDING", "OFF")
DAY_RESULTS = ("PRESENT", "HALF_DAY", "ABSENT", "MISSING_PUNCH", "UNMARKED", "PENDING", "OFF")
PRESENCE_BASES = ("SESSION", "ON_DUTY", "REMOTE_WORK", "OVERRIDE", "PRESUMED")
PENDING_REASONS = ("SHIFT_IN_PROGRESS", "DEVICE_SYNC", "REQUEST_PENDING", "LEAVE_PENDING", "NOT_EVALUATED")
EXCUSE_SOURCES = ("REQUEST", "POLICY")
EXPECTED_PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF", "NONE")
SHIFT_SOURCES = ("ROSTER", "RULE", "PLACEMENT", "NONE")
WORK_MODES = ("ON_SITE", "REMOTE", "FIELD")
EVALUATED_BY = ("SYSTEM", "USER")
EVALUATION_TRIGGERS = (
    "PUNCH", "SESSION_CLOSED", "PAIRING", "REQUEST_DECIDED", "ON_DUTY_DECIDED", "LEAVE_CHANGED", "HOLIDAY_CHANGED",
    "WORKWEEK_CHANGED", "ROSTER_PUBLISHED", "SHIFT_VERSION", "POLICY_CHANGED", "SETTING_CHANGED",
    "PLACEMENT_CHANGED", "MAPPING_CHANGED", "CONSENT_CHANGED", "DAY_CLOSE", "BACKFILL", "MANUAL_RECOMPUTE",
)
EXCEPTION_CODES = (
    "WORKED_ON_LEAVE", "UNMARKED_DAY", "MISSING_PUNCH_OUT", "NON_WORKING_DAY_WORKED", "LEAVE_PENDING_PAST_DATE",
    "LEAVE_ON_PUNCHED_DATE", "MISSING_PUNCH_IN", "LATE_IN", "EARLY_OUT", "SHORT_HOURS", "ABSENT", "NO_SHIFT_ASSIGNED",
    "SHIFT_MISMATCH", "ROSTER_UNPUBLISHED", "UNSCHEDULED_WORK", "OUTSIDE_GEOFENCE", "EVIDENCE_FLAGGED",
    "OVERTIME_UNAPPROVED", "OVERTIME_CONSENT_MISSING", "BREAK_MISSED", "BREAK_OVERSTAY", "MEAL_BREAK_LATE",
    "MEAL_BREAK_SHORT", "MEAL_BREAK_MISSED", "REST_BREAK_MISSED", "DEVICE_SYNC_PENDING", "REQUEST_PENDING",
    "REST_VIOLATION", "CONSECUTIVE_ABSENCE", "BREAK_OFF_WINDOW", "BREAK_NOT_TAKEN", "SHORT_VISIT",
    "OVERTIME_ON_ABSENT_DAY", "NIGHT_WORK_WITHOUT_CONSENT", "NO_PLACEMENT",
)
OLD_RECORD_TYPES = (
    "attendance", "leave", "holiday", "lop_input", "suspension", "on_duty", "optional_holiday",
    "overtime", "task", "wage_input", "assignment", "work_week_version", "calendar_link",
    "shift_version", "shift_rule", "holiday_year", "working_day",
)
NEW_RECORD_TYPES = OLD_RECORD_TYPES + ("attendance_day", "attendance_request", "roster_entry")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _array(values) -> str:
    return "ARRAY[" + ", ".join(f"'{v}'" for v in values) + "]::text[]"


def _fk(table: str, column: str, target: str, name: str) -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE RESTRICT")


TABLES = {
    "attendance_days": f"""
        {BIG_BASE}, employee_id INTEGER NOT NULL, shift_date DATE NOT NULL, legal_entity_id INTEGER,
        branch_id INTEGER, work_timezone VARCHAR(64) NOT NULL, evaluation_state VARCHAR(12) NOT NULL,
        rules_mode VARCHAR(10) NOT NULL, version INTEGER NOT NULL DEFAULT 1, row_version INTEGER NOT NULL DEFAULT 1,
        inputs_hash VARCHAR(64) NOT NULL, content_hash VARCHAR(64) NOT NULL,
        policy_version_ids JSONB NOT NULL DEFAULT '{{}}'::jsonb, calendar_version VARCHAR(64),
        work_source VARCHAR(20), shift_source VARCHAR(10), shift_id INTEGER, shift_version_id INTEGER,
        roster_entry_id INTEGER, roster_version INTEGER, expected_portion VARCHAR(12) NOT NULL,
        expected_minutes INTEGER, expected_start_at TIMESTAMPTZ, expected_end_at TIMESTAMPTZ,
        session_count SMALLINT NOT NULL DEFAULT 0, first_in_at TIMESTAMPTZ, last_out_at TIMESTAMPTZ,
        gross_seconds INTEGER, net_seconds INTEGER, gross_minutes INTEGER, break_minutes INTEGER,
        unpaid_break_minutes INTEGER, net_minutes INTEGER, auto_deducted_minutes INTEGER NOT NULL DEFAULT 0,
        rounding_delta_minutes INTEGER NOT NULL DEFAULT 0, late_minutes INTEGER, early_minutes INTEGER,
        short_minutes INTEGER, late_mark BOOLEAN NOT NULL DEFAULT false, late_excused BOOLEAN NOT NULL DEFAULT false,
        late_excuse_source VARCHAR(8), early_mark BOOLEAN NOT NULL DEFAULT false,
        early_excused BOOLEAN NOT NULL DEFAULT false, early_excuse_source VARCHAR(8),
        short_mark BOOLEAN NOT NULL DEFAULT false, late_converted BOOLEAN NOT NULL DEFAULT false,
        early_converted BOOLEAN NOT NULL DEFAULT false, night_minutes INTEGER NOT NULL DEFAULT 0,
        ot_eligible_seconds INTEGER NOT NULL DEFAULT 0, ot_eligible_minutes INTEGER NOT NULL DEFAULT 0,
        ot_candidate_minutes INTEGER NOT NULL DEFAULT 0, meal_violations SMALLINT NOT NULL DEFAULT 0,
        rest_violations SMALLINT NOT NULL DEFAULT 0, first_half_result VARCHAR(14) NOT NULL,
        second_half_result VARCHAR(14) NOT NULL, day_result VARCHAR(14) NOT NULL, presence_basis VARCHAR(12),
        work_mode VARCHAR(12), exception_codes TEXT[] NOT NULL DEFAULT '{{}}'::text[],
        pending_reasons TEXT[] NOT NULL DEFAULT '{{}}'::text[], override_request_id INTEGER,
        evaluated_at TIMESTAMPTZ NOT NULL, evaluated_by VARCHAR(8) NOT NULL DEFAULT 'SYSTEM',
        trigger VARCHAR(30) NOT NULL,
        CONSTRAINT uq_attendance_days_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_days_employee_date UNIQUE (company_id, employee_id, shift_date),
        {_fk('attendance_days', 'employee_id', 'employees', 'employee')},
        {_fk('attendance_days', 'legal_entity_id', 'legal_entities', 'legal_entity')},
        {_fk('attendance_days', 'branch_id', 'branches', 'branch')},
        {_fk('attendance_days', 'shift_id', 'shifts', 'shift')},
        {_fk('attendance_days', 'shift_version_id', 'shift_versions', 'shift_version')},
        {_fk('attendance_days', 'roster_entry_id', 'roster_entries', 'roster_entry')},
        CONSTRAINT ck_attendance_days_state CHECK (evaluation_state IN {_in(EVALUATION_STATES)}),
        CONSTRAINT ck_attendance_days_rules_mode CHECK (rules_mode IN {_in(RULES_MODES)}),
        CONSTRAINT ck_attendance_days_versions CHECK (version >= 1 AND row_version >= 1),
        CONSTRAINT ck_attendance_days_hashes CHECK (inputs_hash ~ '^[0-9a-f]{{64}}$' AND content_hash ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT ck_attendance_days_shift_source CHECK (shift_source IS NULL OR shift_source IN {_in(SHIFT_SOURCES)}),
        CONSTRAINT ck_attendance_days_expected_portion CHECK (expected_portion IN {_in(EXPECTED_PORTIONS)}),
        CONSTRAINT ck_attendance_days_expected_minutes CHECK (expected_minutes IS NULL OR expected_minutes >= 0),
        CONSTRAINT ck_attendance_days_session_count CHECK (session_count >= 0),
        CONSTRAINT ck_attendance_days_mark_minutes CHECK ((late_minutes IS NULL OR late_minutes >= 0)
            AND (early_minutes IS NULL OR early_minutes >= 0) AND (short_minutes IS NULL OR short_minutes >= 0)),
        CONSTRAINT ck_attendance_days_late_minutes CHECK (NOT late_mark OR late_minutes IS NOT NULL),
        CONSTRAINT ck_attendance_days_early_minutes CHECK (NOT early_mark OR early_minutes IS NOT NULL),
        CONSTRAINT ck_attendance_days_late_excused CHECK (NOT late_excused OR late_mark),
        CONSTRAINT ck_attendance_days_early_excused CHECK (NOT early_excused OR early_mark),
        CONSTRAINT ck_attendance_days_late_converted CHECK (NOT late_converted OR NOT late_mark),
        CONSTRAINT ck_attendance_days_early_converted CHECK (NOT early_converted OR NOT early_mark),
        CONSTRAINT ck_attendance_days_late_source CHECK (late_excuse_source IS NULL
            OR late_excuse_source IN {_in(EXCUSE_SOURCES)}),
        CONSTRAINT ck_attendance_days_early_source CHECK (early_excuse_source IS NULL
            OR early_excuse_source IN {_in(EXCUSE_SOURCES)}),
        CONSTRAINT ck_attendance_days_late_source_set CHECK ((late_excuse_source IS NOT NULL) = late_excused),
        CONSTRAINT ck_attendance_days_early_source_set CHECK ((early_excuse_source IS NOT NULL) = early_excused),
        CONSTRAINT ck_attendance_days_counts CHECK (night_minutes >= 0 AND ot_eligible_minutes >= 0
            AND ot_candidate_minutes >= 0 AND ot_eligible_seconds >= 0 AND meal_violations >= 0
            AND rest_violations >= 0),
        CONSTRAINT ck_attendance_days_portions CHECK (first_half_result IN {_in(PORTION_RESULTS)}
            AND second_half_result IN {_in(PORTION_RESULTS)}),
        CONSTRAINT ck_attendance_days_day_result CHECK (day_result IN {_in(DAY_RESULTS)}),
        CONSTRAINT ck_attendance_days_basis CHECK (presence_basis IS NULL OR presence_basis IN {_in(PRESENCE_BASES)}),
        CONSTRAINT ck_attendance_days_work_mode CHECK (work_mode IS NULL OR work_mode IN {_in(WORK_MODES)}),
        CONSTRAINT ck_attendance_days_exceptions CHECK (exception_codes <@ {_array(EXCEPTION_CODES)}),
        CONSTRAINT ck_attendance_days_pending CHECK (pending_reasons <@ {_array(PENDING_REASONS)}),
        CONSTRAINT ck_attendance_days_evaluated_by CHECK (evaluated_by IN {_in(EVALUATED_BY)}),
        CONSTRAINT ck_attendance_days_trigger CHECK (trigger IN {_in(EVALUATION_TRIGGERS)})""",
    "attendance_day_versions": f"""
        {BIG_BASE}, day_id BIGINT NOT NULL, version INTEGER NOT NULL, outputs JSONB NOT NULL,
        inputs_hash VARCHAR(64) NOT NULL, trigger VARCHAR(30) NOT NULL, actor_user_id INTEGER,
        CONSTRAINT uq_attendance_day_versions_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_day_versions_version UNIQUE (company_id, day_id, version),
        {_fk('attendance_day_versions', 'day_id', 'attendance_days', 'day')},
        {_fk('attendance_day_versions', 'actor_user_id', 'users', 'actor')},
        CONSTRAINT ck_attendance_day_versions_version CHECK (version >= 1),
        CONSTRAINT ck_attendance_day_versions_hash CHECK (inputs_hash ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT ck_attendance_day_versions_trigger CHECK (trigger IN {_in(EVALUATION_TRIGGERS)})""",
}
APPEND_ONLY = ("attendance_day_versions",)
INDEXES = (
    "CREATE INDEX ix_attendance_days_date_branch ON attendance_days (company_id, shift_date, branch_id)",
    "CREATE INDEX ix_attendance_days_employee ON attendance_days (company_id, employee_id, shift_date DESC)",
    "CREATE INDEX ix_attendance_days_exceptions ON attendance_days USING gin (exception_codes)",
    "CREATE INDEX ix_attendance_days_open ON attendance_days (company_id, shift_date) "
    "WHERE evaluation_state <> 'FINAL'",
    "CREATE INDEX ix_attendance_days_dashboard ON attendance_days (company_id, shift_date) "
    "INCLUDE (employee_id, day_result)",
)
GUARD = """
CREATE OR REPLACE FUNCTION {name}_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '{name} is append-only: a change is a new row'
        USING ERRCODE = 'check_violation', CONSTRAINT = '{name}_guard';
END;
$$ LANGUAGE plpgsql;
"""


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
    for table in APPEND_ONLY:
        op.execute(GUARD.format(name=table))
        op.execute(f"CREATE TRIGGER trg_{table}_guard BEFORE UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION {table}_guard()")
    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            privileges = "SELECT, INSERT" if table in APPEND_ONLY else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
    op.execute("ALTER TABLE source_change_log ALTER COLUMN record_id TYPE BIGINT")
    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               f"record_type IN {NEW_RECORD_TYPES}")


def downgrade() -> None:
    bind = op.get_bind()
    active = bind.execute(sa.text(
        "SELECT count(*) FROM setting_values WHERE key = 'attendance.v2.enabled' AND value = 'true'::jsonb "
        "AND status = 'ACTIVE'")).scalar()
    if active:
        raise RuntimeError("a tenant is on attendance v2 (V2_ACTIVE): roll it back to v1 before downgrading")
    leftover = bind.execute(sa.text(
        "SELECT count(*) FROM source_change_log WHERE record_type IN "
        "('attendance_day', 'attendance_request', 'roster_entry') OR record_id > 2147483647")).scalar()
    if leftover:
        raise RuntimeError("source_change_log holds attendance-day changes (append-only history)")
    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               f"record_type IN {OLD_RECORD_TYPES}")
    op.execute("ALTER TABLE source_change_log ALTER COLUMN record_id TYPE INTEGER")
    for table in reversed(list(TABLES)):
        op.execute(f"DROP TABLE {table}")
    for table in APPEND_ONLY:
        op.execute(f"DROP FUNCTION IF EXISTS {table}_guard()")
