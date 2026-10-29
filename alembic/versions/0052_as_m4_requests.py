"""AS-M4 requests and approvals, part 1: attendance requests, reason codes, attachments, period closes, timesheet
approvals, on-duty kinds and the engine storage of routes, RETURN and SLA (AS 04-attendance/04; JC-AS-21, -37, -46).

NEW tenant tables (RLS ``tenant_isolation``, unique ``(company_id, id)``, composite same-company foreign keys, grants;
documented in app/models/attendance_requests.py): ``approval_routes``, ``attendance_reason_codes``,
``attendance_requests``, ``attendance_attachments``, ``attendance_period_closes``, ``attendance_timesheet_approvals``.

Engine (interim of LH-M5a, IG-AS-81): module ``timesheet`` and approver type ``team_lead`` in every engine CHECK;
request status ``RETURNED`` and step status ``returned``; ``approval_chain_steps.route_id`` / ``sla_hours`` (the
unique step order becomes per route: ``uq_approval_chain_step`` on ``COALESCE(route_id, 0)``);
``approval_request_steps.due_at`` / ``reminders_sent`` / ``escalation_level``.

``on_duty_requests`` gains ``kind`` (ON_DUTY | REMOTE_WORK), ``start_time`` / ``end_time``, ``batch_id``,
``requires_punch``, ``reason_code``. ``attendance.request_id``, ``attendance_punches.request_id`` and
``attendance_days.override_request_id`` get their foreign keys.

Data: the seeded reason codes (§7.1, SP-AS-018 decision codes) and the ``default`` routes of modules ``attendance``
and ``timesheet`` (1 ``primary_manager`` step, priority 0, §9.2) for every company; scheduled job
``attendance.sla_scan`` (HOURLY).

Downgrade refuses while attendance requests or RETURNED approval requests exist, then drops everything above.

Revision ID: 0052_as_m4_requests
Revises: 0051_as_m3_days_policies
Create Date: 2026-10-28
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0052_as_m4_requests"
down_revision: Union[str, None] = "0051_as_m3_days_policies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")

# Frozen vocabularies at this revision.
OLD_MODULES = ("leave", "payroll_run", "compensation", "wage_input", "overtime", "task", "reimbursement", "loan",
               "bank_change", "payment_batch", "off_cycle", "final_settlement", "lop", "attendance", "settings")
NEW_MODULES = OLD_MODULES + ("timesheet",)
OLD_APPROVER_TYPES = ("primary_manager", "skip_level_manager", "department_head", "role", "finance_role",
                      "specific_user")
NEW_APPROVER_TYPES = OLD_APPROVER_TYPES + ("team_lead",)
OLD_REQUEST_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")
NEW_REQUEST_STATUSES = OLD_REQUEST_STATUSES + ("RETURNED",)
OLD_STEP_STATUSES = ("waiting", "pending", "approved", "rejected", "skipped", "cancelled")
NEW_STEP_STATUSES = OLD_STEP_STATUSES + ("returned",)
KINDS = ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "VOID_SESSION", "MARK_EXCUSE", "STATUS_OVERRIDE")
MODES = ("SELF", "ON_BEHALF", "DIRECT")
STATUSES = ("DRAFT", "PENDING", "RETURNED", "APPROVED", "REJECTED", "CANCELLED", "SUPERSEDED")
APPLIES_TO = KINDS + ("REVIEW", "ROSTER_CHANGE", "DECISION", "ON_DUTY")

# §7.1 seeds + SP-AS-018 decision codes: (code, label, applies_to, counts_toward_limit, sensitive)
_ALL = KINDS
REASON_SEEDS = (
    ("FORGOT_TO_PUNCH", "Forgot to punch", ("MISSED_PUNCH", "ADD_SESSION"), True, False),
    ("DEVICE_FAILURE", "Device not working", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "VOID_SESSION", "REVIEW"),
     False, False),
    ("NETWORK_ISSUE", "Network problem", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION"), False, False),
    ("APP_ISSUE", "App problem", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION"), False, False),
    ("POWER_OUTAGE", "Power cut", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION"), False, False),
    ("ON_CLIENT_SITE", "Worked at a client site", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "ON_DUTY"), True,
     False),
    ("OFFICIAL_WORK", "Official work", ("MISSED_PUNCH", "ADD_SESSION", "MARK_EXCUSE", "ON_DUTY"), True, False),
    ("TRANSPORT_DELAY", "Transport delay", ("MARK_EXCUSE",), True, False),
    ("MEDICAL", "Medical", ("MARK_EXCUSE",), True, True),
    ("PERSONAL", "Personal", ("MARK_EXCUSE",), True, False),
    ("WRONG_PUNCH", "Wrong punch recorded", ("CORRECTION", "VOID_SESSION"), True, False),
    ("SUPERVISOR_INSTRUCTION", "Supervisor's instruction", ("ADD_SESSION", "STATUS_OVERRIDE", "ROSTER_CHANGE"),
     False, False),
    ("DATA_CORRECTION", "Data correction", ("CORRECTION", "VOID_SESSION", "STATUS_OVERRIDE", "REVIEW"), False, False),
    ("EMPLOYER_IDLE", "Work stopped by employer", ("STATUS_OVERRIDE",), False, False),
    ("RAIN_OFF", "Rain / weather stoppage", ("STATUS_OVERRIDE",), False, False),
    ("OTHER", "Other (explain)", _ALL + ("ON_DUTY",), True, False),
    ("NEEDS_DETAILS", "More details needed", ("DECISION",), False, False),
    ("NOT_VERIFIABLE", "Could not be verified", ("DECISION",), False, False),
    ("OUTSIDE_POLICY", "Not allowed by policy", ("DECISION",), False, False),
    ("DUPLICATE_REQUEST", "Duplicate request", ("DECISION",), False, False),
)


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _array(values) -> str:
    return "ARRAY[" + ", ".join(f"'{v}'" for v in values) + "]::text[]"


def _fk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE {ondelete}")


TABLES = {
    "approval_routes": f"""
        {BASE}, module VARCHAR(20) NOT NULL, route_key VARCHAR(40) NOT NULL, name VARCHAR(100) NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0, criteria JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE', row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_approval_routes_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_approval_routes_key UNIQUE (company_id, module, route_key),
        CONSTRAINT ck_approval_routes_module CHECK (module IN {_in(NEW_MODULES)}),
        CONSTRAINT ck_approval_routes_status CHECK (status IN ('ACTIVE', 'INACTIVE')),
        CONSTRAINT ck_approval_routes_criteria CHECK (jsonb_typeof(criteria) = 'object'),
        CONSTRAINT ck_approval_routes_key CHECK (route_key ~ '^[A-Za-z][A-Za-z0-9_-]{{0,39}}$')""",
    "attendance_reason_codes": f"""
        {BASE}, code VARCHAR(40) NOT NULL, label VARCHAR(80) NOT NULL,
        applies_to TEXT[] NOT NULL DEFAULT '{{}}'::text[], requires_attachment BOOLEAN NOT NULL DEFAULT false,
        counts_toward_limit BOOLEAN NOT NULL DEFAULT true, sensitive BOOLEAN NOT NULL DEFAULT false,
        system_only BOOLEAN NOT NULL DEFAULT false, is_seed BOOLEAN NOT NULL DEFAULT false,
        status VARCHAR(10) NOT NULL DEFAULT 'ACTIVE', row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_reason_codes_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_reason_codes_code UNIQUE (company_id, code),
        CONSTRAINT ck_attendance_reason_codes_code CHECK (code ~ '^[A-Z][A-Z0-9_]{{1,39}}$'),
        CONSTRAINT ck_attendance_reason_codes_status CHECK (status IN ('ACTIVE', 'INACTIVE')),
        CONSTRAINT ck_attendance_reason_codes_applies_to CHECK (applies_to <@ {_array(APPLIES_TO)})""",
    "attendance_requests": f"""
        {BASE}, employee_id INTEGER NOT NULL, shift_date DATE NOT NULL, kind VARCHAR(20) NOT NULL,
        mode VARCHAR(10) NOT NULL, status VARCHAR(12) NOT NULL DEFAULT 'PENDING', session_id INTEGER,
        payload JSONB NOT NULL DEFAULT '{{}}'::jsonb, base_day_version INTEGER, base_session_row_version INTEGER,
        reason_code VARCHAR(40) NOT NULL, comment VARCHAR(1000), minutes INTEGER NOT NULL DEFAULT 0,
        counts_toward_limit BOOLEAN NOT NULL DEFAULT true, approval_request_id INTEGER,
        requested_by_user_id INTEGER, decided_by_user_id INTEGER, submitted_at TIMESTAMPTZ,
        first_submitted_on DATE, decided_at TIMESTAMPTZ, applied_at TIMESTAMPTZ, cancelled_at TIMESTAMPTZ,
        decision_remarks VARCHAR(1000), decision_reason_code VARCHAR(40), issues JSONB NOT NULL DEFAULT '[]'::jsonb,
        version INTEGER NOT NULL DEFAULT 1, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_requests_company_id_id UNIQUE (company_id, id),
        {_fk('attendance_requests', 'employee_id', 'employees', 'employee')},
        {_fk('attendance_requests', 'session_id', 'attendance', 'session')},
        {_fk('attendance_requests', 'approval_request_id', 'approval_requests', 'approval')},
        {_fk('attendance_requests', 'requested_by_user_id', 'users', 'requested_by')},
        {_fk('attendance_requests', 'decided_by_user_id', 'users', 'decided_by')},
        CONSTRAINT fk_attendance_requests_reason FOREIGN KEY (company_id, reason_code)
            REFERENCES attendance_reason_codes (company_id, code) ON DELETE RESTRICT,
        CONSTRAINT ck_attendance_requests_kind CHECK (kind IN {_in(KINDS)}),
        CONSTRAINT ck_attendance_requests_mode CHECK (mode IN {_in(MODES)}),
        CONSTRAINT ck_attendance_requests_status CHECK (status IN {_in(STATUSES)}),
        CONSTRAINT ck_attendance_requests_direct CHECK (mode <> 'DIRECT'
            OR status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
        CONSTRAINT ck_attendance_requests_override CHECK (kind <> 'STATUS_OVERRIDE' OR mode <> 'SELF'),
        CONSTRAINT ck_attendance_requests_draft CHECK (status <> 'DRAFT' OR mode = 'SELF'),
        CONSTRAINT ck_attendance_requests_session CHECK (kind NOT IN ('CORRECTION', 'VOID_SESSION')
            OR session_id IS NOT NULL),
        CONSTRAINT ck_attendance_requests_payload CHECK (jsonb_typeof(payload) = 'object'),
        CONSTRAINT ck_attendance_requests_counts CHECK (minutes >= 0 AND version >= 1 AND row_version >= 1)""",
    "attendance_attachments": f"""
        {BASE}, employee_id INTEGER NOT NULL, request_id INTEGER, on_duty_request_id INTEGER,
        uploaded_by_user_id INTEGER, file_name VARCHAR(200) NOT NULL, mime VARCHAR(60) NOT NULL,
        bytes INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL, storage_path TEXT NOT NULL,
        category VARCHAR(24) NOT NULL DEFAULT 'SUPPORTING_DOCUMENT', sensitivity VARCHAR(10) NOT NULL DEFAULT 'GENERAL',
        CONSTRAINT uq_attendance_attachments_company_id_id UNIQUE (company_id, id),
        {_fk('attendance_attachments', 'employee_id', 'employees', 'employee')},
        {_fk('attendance_attachments', 'request_id', 'attendance_requests', 'request', 'CASCADE')},
        {_fk('attendance_attachments', 'on_duty_request_id', 'on_duty_requests', 'on_duty', 'CASCADE')},
        {_fk('attendance_attachments', 'uploaded_by_user_id', 'users', 'uploaded_by')},
        CONSTRAINT ck_attendance_attachments_category CHECK (category IN ('SUPPORTING_DOCUMENT', 'TRAVEL_DOCUMENT')),
        CONSTRAINT ck_attendance_attachments_sensitivity CHECK (sensitivity IN ('GENERAL', 'MEDICAL')),
        CONSTRAINT ck_attendance_attachments_bytes CHECK (bytes > 0)""",
    "attendance_period_closes": f"""
        {BASE}, legal_entity_id INTEGER NOT NULL, branch_id INTEGER, pay_group_id INTEGER, cycle_start DATE NOT NULL,
        cycle_end DATE NOT NULL, status VARCHAR(10) NOT NULL DEFAULT 'OPEN',
        open_exception_count INTEGER NOT NULL DEFAULT 0, closed_by_user_id INTEGER, closed_at TIMESTAMPTZ,
        reopened_by_user_id INTEGER, reopened_at TIMESTAMPTZ, reopen_reason VARCHAR(500),
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_period_closes_company_id_id UNIQUE (company_id, id),
        {_fk('attendance_period_closes', 'legal_entity_id', 'legal_entities', 'legal_entity')},
        {_fk('attendance_period_closes', 'branch_id', 'branches', 'branch')},
        {_fk('attendance_period_closes', 'pay_group_id', 'pay_groups', 'pay_group')},
        {_fk('attendance_period_closes', 'closed_by_user_id', 'users', 'closed_by')},
        {_fk('attendance_period_closes', 'reopened_by_user_id', 'users', 'reopened_by')},
        CONSTRAINT ck_attendance_period_closes_status CHECK (status IN ('OPEN', 'CLOSED', 'REOPENED')),
        CONSTRAINT ck_attendance_period_closes_cycle CHECK (cycle_end >= cycle_start),
        CONSTRAINT ck_attendance_period_closes_count CHECK (open_exception_count >= 0)""",
    "attendance_timesheet_approvals": f"""
        {BASE}, employee_id INTEGER NOT NULL, period_kind VARCHAR(6) NOT NULL DEFAULT 'WEEK',
        period_start DATE NOT NULL, period_end DATE NOT NULL, status VARCHAR(10) NOT NULL DEFAULT 'OPEN',
        content_hash VARCHAR(64), approval_request_id INTEGER, submitted_at TIMESTAMPTZ, decided_at TIMESTAMPTZ,
        decided_by_user_id INTEGER, version INTEGER NOT NULL DEFAULT 1, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_timesheet_approvals_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_attendance_timesheet_approvals_period UNIQUE (company_id, employee_id, period_kind, period_start),
        {_fk('attendance_timesheet_approvals', 'employee_id', 'employees', 'employee')},
        {_fk('attendance_timesheet_approvals', 'approval_request_id', 'approval_requests', 'approval')},
        {_fk('attendance_timesheet_approvals', 'decided_by_user_id', 'users', 'decided_by')},
        CONSTRAINT ck_attendance_timesheet_approvals_status CHECK (status IN
            ('OPEN', 'SUBMITTED', 'APPROVED', 'REJECTED', 'REOPENED')),
        CONSTRAINT ck_attendance_timesheet_approvals_kind CHECK (period_kind IN ('WEEK', 'CYCLE')),
        CONSTRAINT ck_attendance_timesheet_approvals_period_range CHECK (period_end >= period_start)""",
}
INDEXES = (
    "CREATE INDEX ix_attendance_requests_employee_date ON attendance_requests (company_id, employee_id, shift_date)",
    "CREATE INDEX ix_attendance_requests_open ON attendance_requests (company_id, status) "
    "WHERE status IN ('PENDING', 'RETURNED')",
    "CREATE UNIQUE INDEX uq_attendance_requests_open ON attendance_requests "
    "(company_id, employee_id, shift_date, kind, COALESCE(session_id, 0)) WHERE status IN ('PENDING', 'RETURNED')",
    "CREATE INDEX ix_attendance_attachments_request ON attendance_attachments (company_id, request_id)",
    "CREATE UNIQUE INDEX uq_attendance_period_closes ON attendance_period_closes "
    "(company_id, legal_entity_id, COALESCE(branch_id, 0), COALESCE(pay_group_id, 0), cycle_start)",
)
# (table, constraint, old values, new values, column)
WIDENED = (
    ("approval_chain_steps", "ck_approval_chain_module", "module", OLD_MODULES, NEW_MODULES),
    ("approval_chain_steps", "ck_approval_chain_type", "approver_type", OLD_APPROVER_TYPES, NEW_APPROVER_TYPES),
    ("approval_requests", "ck_approval_requests_module", "module", OLD_MODULES, NEW_MODULES),
    ("approval_requests", "ck_approval_requests_status", "status", OLD_REQUEST_STATUSES, NEW_REQUEST_STATUSES),
    ("approval_request_steps", "ck_approval_request_steps_type", "approver_type",
     OLD_APPROVER_TYPES + ("permission_holders",), NEW_APPROVER_TYPES + ("permission_holders",)),
    ("approval_request_steps", "ck_approval_request_steps_status", "status", OLD_STEP_STATUSES, NEW_STEP_STATUSES),
    ("approval_delegations", "ck_delegation_module", "module", OLD_MODULES, NEW_MODULES),
)
REQUEST_FKS = (("attendance", "request_id", "fk_attendance_request"),
               ("attendance_punches", "request_id", "fk_attendance_punches_request"),
               ("attendance_days", "override_request_id", "fk_attendance_days_override_request"))


def _app_roles(bind) -> list:
    return [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]


def _widen(table: str, name: str, column: str, values) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"{column} IN {_in(values)}")


def upgrade() -> None:
    bind = op.get_bind()
    for table, body in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({body})")
        op.execute(f"CREATE INDEX ix_{table}_company_id ON {table} (company_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    for statement in INDEXES:
        op.execute(statement)
    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")

    # Engine storage.
    for table, name, column, _old, new in WIDENED:
        _widen(table, name, column, new)
    op.add_column("approval_chain_steps", sa.Column("route_id", sa.Integer(), nullable=True))
    op.add_column("approval_chain_steps", sa.Column("sla_hours", sa.Integer(), nullable=True))
    op.execute("ALTER TABLE approval_chain_steps ADD CONSTRAINT fk_approval_chain_route FOREIGN KEY "
               "(company_id, route_id) REFERENCES approval_routes (company_id, id) ON DELETE CASCADE")
    op.execute("ALTER TABLE approval_chain_steps ADD CONSTRAINT ck_approval_chain_sla "
               "CHECK (sla_hours IS NULL OR sla_hours BETWEEN 1 AND 720)")
    op.drop_constraint("uq_approval_chain_step", "approval_chain_steps", type_="unique")
    op.execute("CREATE UNIQUE INDEX uq_approval_chain_step ON approval_chain_steps "
               "(company_id, module, COALESCE(route_id, 0), step_order)")
    op.add_column("approval_request_steps", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("approval_request_steps", sa.Column("reminders_sent", sa.Integer(), nullable=False,
                                                      server_default="0"))
    op.add_column("approval_request_steps", sa.Column("escalation_level", sa.Integer(), nullable=False,
                                                      server_default="0"))
    op.execute("CREATE INDEX ix_approval_request_steps_due ON approval_request_steps (company_id, due_at) "
               "WHERE status = 'pending' AND due_at IS NOT NULL")

    # On-duty kinds (JC-AS-37).
    op.add_column("on_duty_requests", sa.Column("kind", sa.String(12), nullable=False, server_default="ON_DUTY"))
    op.add_column("on_duty_requests", sa.Column("start_time", sa.Time(), nullable=True))
    op.add_column("on_duty_requests", sa.Column("end_time", sa.Time(), nullable=True))
    op.add_column("on_duty_requests", sa.Column("batch_id", sa.String(36), nullable=True))
    op.add_column("on_duty_requests", sa.Column("requires_punch", sa.Boolean(), nullable=False,
                                                server_default="false"))
    op.add_column("on_duty_requests", sa.Column("reason_code", sa.String(40), nullable=True))
    op.create_check_constraint("ck_on_duty_requests_kind", "on_duty_requests", "kind IN ('ON_DUTY', 'REMOTE_WORK')")
    op.create_check_constraint(
        "ck_on_duty_requests_window", "on_duty_requests",
        "(start_time IS NULL) = (end_time IS NULL) AND (end_time IS NULL OR end_time > start_time)")
    op.execute("CREATE INDEX ix_on_duty_requests_batch ON on_duty_requests (company_id, batch_id) "
               "WHERE batch_id IS NOT NULL")

    for table, column, name in REQUEST_FKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (company_id, {column}) "
                   "REFERENCES attendance_requests (company_id, id) ON DELETE RESTRICT")

    # Data: reason codes and default routes of every company; the SLA scan job.
    companies = [c for (c,) in bind.execute(sa.text("SELECT id FROM companies ORDER BY id"))]
    for company_id in companies:
        for code, label, applies, counts, sensitive in REASON_SEEDS:
            bind.execute(sa.text(
                "INSERT INTO attendance_reason_codes (company_id, code, label, applies_to, counts_toward_limit, "
                "sensitive, is_seed) VALUES (:c, :code, :label, CAST(:applies AS text[]), :counts, :sensitive, true) "
                "ON CONFLICT (company_id, code) DO NOTHING"),
                {"c": company_id, "code": code, "label": label, "applies": "{" + ",".join(applies) + "}",
                 "counts": counts, "sensitive": sensitive})
        for module in ("attendance", "timesheet"):
            route_id = bind.execute(sa.text(
                "INSERT INTO approval_routes (company_id, module, route_key, name, priority, criteria) "
                "VALUES (:c, :m, 'default', 'Default', 0, CAST(:criteria AS jsonb)) "
                "ON CONFLICT (company_id, module, route_key) DO NOTHING RETURNING id"),
                {"c": company_id, "m": module, "criteria": json.dumps({})}).scalar()
            if route_id is not None:
                bind.execute(sa.text(
                    "INSERT INTO approval_chain_steps (company_id, module, route_id, step_order, approver_type, "
                    "created_at, updated_at) VALUES (:c, :m, :r, 1, 'primary_manager', now(), now())"),
                    {"c": company_id, "m": module, "r": route_id})
    bind.execute(sa.text(
        "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
        "VALUES ('attendance.sla_scan', 'HOURLY', 41, NULL, true, now()) ON CONFLICT (job_type) DO NOTHING"))


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text(
        "SELECT (SELECT count(*) FROM attendance_requests) + (SELECT count(*) FROM approval_requests "
        "WHERE status = 'RETURNED' OR module = 'timesheet') + (SELECT count(*) FROM approval_request_steps "
        "WHERE approver_type = 'team_lead' OR status = 'returned')")).scalar()
    if used:
        raise RuntimeError("attendance requests or AS-M4 approval rows exist: they cannot be dropped")
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type = 'attendance.sla_scan'"))
    for table, column, name in REQUEST_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute("DROP INDEX ix_on_duty_requests_batch")
    for name in ("ck_on_duty_requests_window", "ck_on_duty_requests_kind"):
        op.drop_constraint(name, "on_duty_requests", type_="check")
    for column in ("reason_code", "requires_punch", "batch_id", "end_time", "start_time", "kind"):
        op.drop_column("on_duty_requests", column)
    op.execute("DROP INDEX ix_approval_request_steps_due")
    for column in ("escalation_level", "reminders_sent", "due_at"):
        op.drop_column("approval_request_steps", column)
    op.execute("DELETE FROM approval_chain_steps WHERE route_id IS NOT NULL OR approver_type = 'team_lead' "
               "OR module = 'timesheet'")
    op.execute("DROP INDEX uq_approval_chain_step")
    op.create_unique_constraint("uq_approval_chain_step", "approval_chain_steps", ["company_id", "module", "step_order"])
    op.drop_constraint("ck_approval_chain_sla", "approval_chain_steps", type_="check")
    op.drop_constraint("fk_approval_chain_route", "approval_chain_steps", type_="foreignkey")
    op.drop_column("approval_chain_steps", "sla_hours")
    op.drop_column("approval_chain_steps", "route_id")
    op.execute("DELETE FROM approval_delegations WHERE module = 'timesheet'")
    for table, name, column, old, _new in WIDENED:
        _widen(table, name, column, old)
    for table in reversed(list(TABLES)):
        op.execute(f"DROP TABLE {table}")
