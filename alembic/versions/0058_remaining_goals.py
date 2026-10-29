"""Remaining attendance goals and team payroll (owner request 2026-10-29).

* Approval engine: modules ``shift_request`` (AS-M6 routing, team_lead steps, SLA) and ``team_inputs``
  (manager-prepared payroll batches) on the module CHECKs of chains, requests, delegations and routes.
* ``shift_requests.approval_request_id`` (FK) and ``consent_ids`` (notice-window consents, 03-shifts/03 §7.3).
* ``invoice_sources.source_type`` accepts ``TIMESHEET`` (contractor invoices from approved timesheets).
* NEW tenant tables (RLS, composite FKs, grants): ``team_input_batches``, ``team_input_lines``,
  ``team_input_attachments``, ``cost_invoices``, ``notification_channels``, ``channel_deliveries``,
  ``push_subscriptions``, ``calendar_feed_tokens``.
* Permissions (registry 2026.10.12): ``team_pay:read`` (manager REPORTING_TREE, hr_admin COMPANY),
  ``team_pay:submit`` (manager DIRECT_REPORTS, hr_admin COMPANY), ``integrations:manage`` (hr_admin COMPANY); the
  default role ``contractor`` is created in every company. Holders' ``permission_version`` is bumped.
* Scheduled job ``channels.retry`` (hourly, slot 13): re-sends QUEUED / FAILED channel deliveries.

Downgrade refuses while batches, cost invoices or channels exist.

Revision ID: 0058_remaining_goals
Revises: 0057_as_m6_shift_requests
Create Date: 2026-10-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0058_remaining_goals"
down_revision: Union[str, None] = "0057_as_m6_shift_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
OLD_MODULES = ("leave", "payroll_run", "compensation", "wage_input", "overtime", "task", "reimbursement", "loan",
               "bank_change", "payment_batch", "off_cycle", "final_settlement", "lop", "attendance", "settings",
               "timesheet")
NEW_MODULES = OLD_MODULES + ("shift_request", "team_inputs")
MODULE_CHECKS = (("approval_chain_steps", "ck_approval_chain_module"),
                 ("approval_requests", "ck_approval_requests_module"),
                 ("approval_delegations", "ck_delegation_module"),
                 ("approval_routes", "ck_approval_routes_module"))
NEW_GRANTS = {
    "manager": (("team_pay:read", "REPORTING_TREE"), ("team_pay:submit", "DIRECT_REPORTS")),
    "hr_admin": (("team_pay:read", "COMPANY"), ("team_pay:submit", "COMPANY"), ("integrations:manage", "COMPANY")),
}
CONTRACTOR = ("Contractor", "Punches in and out, records timesheets and tasks, and sees own invoices. No payslips or leave.",
              (("employee:read", "SELF"), ("attendance:mark", "SELF"), ("attendance:read", "SELF"),
               ("attendance:request", "SELF"), ("task:read", "SELF"), ("shift:read", "SELF"), ("roster:read", "SELF"),
               ("roster:request", "SELF"), ("team:read", "SELF"), ("payroll:bank_details:read_masked", "SELF"),
               ("payroll:bank_details:edit", "SELF"), ("payroll:tax_profile:read", "SELF"),
               ("payroll:tax_profile:edit", "SELF")))


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(table: str, column: str, target: str, name: str) -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE RESTRICT")


TABLES = {
    "team_input_batches": f"""
        {BASE}, title VARCHAR(120) NOT NULL, period_start DATE NOT NULL, period_end DATE NOT NULL,
        status VARCHAR(10) NOT NULL DEFAULT 'DRAFT', notes TEXT, prepared_by_user_id INTEGER NOT NULL,
        prepared_by_employee_id INTEGER, approval_request_id INTEGER, submitted_at TIMESTAMPTZ, decided_at TIMESTAMPTZ,
        decided_by_user_id INTEGER, decision_note VARCHAR(500), row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_team_input_batches_company_id_id UNIQUE (company_id, id),
        {_fk('team_input_batches', 'prepared_by_user_id', 'users', 'prepared_by')},
        {_fk('team_input_batches', 'prepared_by_employee_id', 'employees', 'prepared_by_employee')},
        {_fk('team_input_batches', 'approval_request_id', 'approval_requests', 'approval')},
        {_fk('team_input_batches', 'decided_by_user_id', 'users', 'decided_by')},
        CONSTRAINT ck_team_input_batches_status CHECK (status IN
            ('DRAFT', 'SUBMITTED', 'RETURNED', 'APPROVED', 'REJECTED', 'CANCELLED')),
        CONSTRAINT ck_team_input_batches_period CHECK (period_end >= period_start)""",
    "team_input_lines": f"""
        {BASE}, batch_id INTEGER NOT NULL, employee_id INTEGER NOT NULL, component_code VARCHAR(40) NOT NULL,
        amount NUMERIC(14, 2), units NUMERIC(10, 2), rate NUMERIC(14, 4), reason VARCHAR(500), wage_input_id INTEGER,
        problem VARCHAR(300),
        CONSTRAINT uq_team_input_lines_company_id_id UNIQUE (company_id, id),
        {_fk('team_input_lines', 'batch_id', 'team_input_batches', 'batch')},
        {_fk('team_input_lines', 'employee_id', 'employees', 'employee')},
        {_fk('team_input_lines', 'wage_input_id', 'wage_inputs', 'wage_input')},
        CONSTRAINT ck_team_input_lines_value CHECK (amount IS NOT NULL OR units IS NOT NULL)""",
    "team_input_attachments": f"""
        {BASE}, batch_id INTEGER NOT NULL, file_name VARCHAR(200) NOT NULL, content_type VARCHAR(100) NOT NULL,
        size_bytes INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL, content BYTEA NOT NULL,
        uploaded_by_user_id INTEGER NOT NULL,
        CONSTRAINT uq_team_input_attachments_company_id_id UNIQUE (company_id, id),
        {_fk('team_input_attachments', 'batch_id', 'team_input_batches', 'batch')},
        {_fk('team_input_attachments', 'uploaded_by_user_id', 'users', 'uploaded_by')},
        CONSTRAINT ck_team_input_attachments_size CHECK (size_bytes > 0 AND size_bytes <= 5242880)""",
    "cost_invoices": f"""
        {BASE}, run_id INTEGER NOT NULL, group_by VARCHAR(14) NOT NULL, group_id INTEGER, group_label VARCHAR(200) NOT NULL,
        number VARCHAR(40), status VARCHAR(8) NOT NULL DEFAULT 'DRAFT', currency VARCHAR(3) NOT NULL,
        bill_to JSONB NOT NULL DEFAULT '{{}}'::jsonb, lines JSONB NOT NULL DEFAULT '[]'::jsonb,
        taxes JSONB NOT NULL DEFAULT '[]'::jsonb, employee_count INTEGER NOT NULL DEFAULT 0,
        gross_total NUMERIC(16, 2) NOT NULL DEFAULT 0, employer_total NUMERIC(16, 2) NOT NULL DEFAULT 0,
        fee_percent NUMERIC(6, 3) NOT NULL DEFAULT 0, fee_total NUMERIC(16, 2) NOT NULL DEFAULT 0,
        subtotal NUMERIC(16, 2) NOT NULL DEFAULT 0, tax_total NUMERIC(16, 2) NOT NULL DEFAULT 0,
        total NUMERIC(16, 2) NOT NULL DEFAULT 0, issue_date DATE, due_date DATE, notes TEXT,
        created_by_user_id INTEGER NOT NULL, issued_by_user_id INTEGER, issued_at TIMESTAMPTZ, void_reason VARCHAR(500),
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_cost_invoices_company_id_id UNIQUE (company_id, id),
        {_fk('cost_invoices', 'run_id', 'payroll_runs', 'run')},
        {_fk('cost_invoices', 'created_by_user_id', 'users', 'created_by')},
        {_fk('cost_invoices', 'issued_by_user_id', 'users', 'issued_by')},
        CONSTRAINT ck_cost_invoices_group_by CHECK (group_by IN
            ('DEPARTMENT', 'BRANCH', 'TEAM', 'LEGAL_ENTITY', 'PAY_GROUP', 'COMPANY')),
        CONSTRAINT ck_cost_invoices_status CHECK (status IN ('DRAFT', 'ISSUED', 'VOID')),
        CONSTRAINT ck_cost_invoices_number CHECK (status = 'DRAFT' OR number IS NOT NULL),
        CONSTRAINT ck_cost_invoices_maker_checker CHECK (issued_by_user_id IS NULL OR
            issued_by_user_id <> created_by_user_id)""",
    "notification_channels": f"""
        {BASE}, kind VARCHAR(10) NOT NULL, name VARCHAR(100) NOT NULL, url_enc TEXT, url_hint VARCHAR(80),
        secret_enc TEXT, events JSONB NOT NULL DEFAULT '[]'::jsonb, active BOOLEAN NOT NULL DEFAULT true,
        last_success_at TIMESTAMPTZ, last_error VARCHAR(300), created_by_user_id INTEGER,
        row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_notification_channels_company_id_id UNIQUE (company_id, id),
        CONSTRAINT ck_notification_channels_kind CHECK (kind IN ('SLACK', 'TEAMS', 'WEBHOOK', 'WHATSAPP')),
        CONSTRAINT ck_notification_channels_events CHECK (jsonb_typeof(events) = 'array')""",
    "channel_deliveries": f"""
        {BASE}, channel_id INTEGER NOT NULL, event_type VARCHAR(60) NOT NULL, event_key VARCHAR(120) NOT NULL,
        payload JSONB NOT NULL DEFAULT '{{}}'::jsonb, status VARCHAR(8) NOT NULL DEFAULT 'QUEUED',
        attempts INTEGER NOT NULL DEFAULT 0, last_error VARCHAR(300), sent_at TIMESTAMPTZ,
        CONSTRAINT uq_channel_deliveries_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_channel_deliveries_event UNIQUE (company_id, channel_id, event_key),
        {_fk('channel_deliveries', 'channel_id', 'notification_channels', 'channel')},
        CONSTRAINT ck_channel_deliveries_status CHECK (status IN ('QUEUED', 'SENT', 'FAILED', 'SKIPPED'))""",
    "push_subscriptions": f"""
        {BASE}, user_id INTEGER NOT NULL, endpoint TEXT NOT NULL, p256dh VARCHAR(200), auth VARCHAR(100),
        user_agent VARCHAR(200), active BOOLEAN NOT NULL DEFAULT true, last_sent_at TIMESTAMPTZ, last_error VARCHAR(300),
        CONSTRAINT uq_push_subscriptions_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_push_subscriptions_endpoint UNIQUE (company_id, user_id, endpoint),
        {_fk('push_subscriptions', 'user_id', 'users', 'user')}""",
    "calendar_feed_tokens": f"""
        {BASE}, user_id INTEGER NOT NULL, employee_id INTEGER NOT NULL, token_hash VARCHAR(64) NOT NULL UNIQUE,
        active BOOLEAN NOT NULL DEFAULT true, last_used_at TIMESTAMPTZ,
        CONSTRAINT uq_calendar_feed_tokens_company_id_id UNIQUE (company_id, id),
        {_fk('calendar_feed_tokens', 'user_id', 'users', 'user')},
        {_fk('calendar_feed_tokens', 'employee_id', 'employees', 'employee')}""",
}
INDEXES = (
    "CREATE INDEX ix_team_input_batches_status ON team_input_batches (company_id, status)",
    "CREATE INDEX ix_team_input_lines_batch_id ON team_input_lines (batch_id)",
    "CREATE INDEX ix_team_input_attachments_batch_id ON team_input_attachments (batch_id)",
    "CREATE INDEX ix_cost_invoices_run_id ON cost_invoices (run_id)",
    "CREATE UNIQUE INDEX uq_cost_invoices_number ON cost_invoices (company_id, number) WHERE number IS NOT NULL",
    "CREATE INDEX ix_channel_deliveries_channel_id ON channel_deliveries (channel_id)",
    "CREATE INDEX ix_channel_deliveries_queued ON channel_deliveries (company_id, status)",
    "CREATE INDEX ix_push_subscriptions_user_id ON push_subscriptions (user_id)",
)


def _app_roles(bind) -> list:
    return [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"))]


def _widen(table: str, name: str, column: str, values) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"{column} IN {_in(values)}")


def upgrade() -> None:
    bind = op.get_bind()
    for table, name in MODULE_CHECKS:
        _widen(table, name, "module", NEW_MODULES)
    _widen("invoice_sources", "ck_invoice_sources_source_type", "source_type", ("TASK", "TIMESHEET"))
    op.add_column("shift_requests", sa.Column("approval_request_id", sa.Integer(), nullable=True))
    op.add_column("shift_requests", sa.Column("consent_ids", sa.dialects.postgresql.JSONB(), nullable=False,
                                              server_default=sa.text("'[]'::jsonb")))
    op.execute("ALTER TABLE shift_requests ADD CONSTRAINT fk_shift_requests_approval FOREIGN KEY "
               "(company_id, approval_request_id) REFERENCES approval_requests (company_id, id) ON DELETE RESTRICT")
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

    changed = set()
    for key, grants in NEW_GRANTS.items():
        for role_id, company_id in bind.execute(sa.text(
                "SELECT id, company_id FROM roles WHERE system_key = :k AND is_system_role = true ORDER BY id"),
                {"k": key}).all():
            for code, scope in grants:
                if bind.execute(sa.text("SELECT 1 FROM role_permissions WHERE role_id = :r AND permission_code = :c "
                                        "AND scope = :s"), {"r": role_id, "c": code, "s": scope}).first():
                    continue
                bind.execute(sa.text(
                    "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
                    "VALUES (:cid, :r, :c, :s, now(), now())"), {"cid": company_id, "r": role_id, "c": code, "s": scope})
                changed.add((company_id, role_id))
    for company_id, role_id in sorted(changed):
        bind.execute(sa.text(
            "UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :cid AND id IN "
            "(SELECT user_id FROM user_role_assignments WHERE company_id = :cid AND role_id = :r)"),
            {"cid": company_id, "r": role_id})
    name, description, grants = CONTRACTOR
    for (company_id,) in bind.execute(sa.text("SELECT id FROM companies ORDER BY id")).all():
        if bind.execute(sa.text("SELECT 1 FROM roles WHERE company_id = :c AND system_key = 'contractor'"),
                        {"c": company_id}).first():
            continue
        role_id = bind.execute(sa.text(
            "INSERT INTO roles (company_id, name, description, system_key, is_system_role, is_protected, "
            "is_full_access, created_at, updated_at) VALUES (:c, :n, :d, 'contractor', true, false, false, now(), now()) "
            "RETURNING id"), {"c": company_id, "n": name, "d": description}).scalar()
        for code, scope in grants:
            bind.execute(sa.text(
                "INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
                "VALUES (:c, :r, :p, :s, now(), now())"), {"c": company_id, "r": role_id, "p": code, "s": scope})
    bind.execute(sa.text(
        "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, enabled, next_run_at) "
        "VALUES ('channels.retry', 'HOURLY', 13, true, now()) ON CONFLICT (job_type) DO NOTHING"))


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text(
        "SELECT (SELECT count(*) FROM team_input_batches) + (SELECT count(*) FROM cost_invoices) "
        "+ (SELECT count(*) FROM notification_channels)")).scalar()
    if used:
        raise RuntimeError("team payroll batches, cost invoices or channels exist; refusing to drop them")
    bind.execute(sa.text("DELETE FROM scheduled_jobs WHERE job_type = 'channels.retry'"))
    for table in reversed(list(TABLES)):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE shift_requests DROP CONSTRAINT IF EXISTS fk_shift_requests_approval")
    op.drop_column("shift_requests", "consent_ids")
    op.drop_column("shift_requests", "approval_request_id")
    _widen("invoice_sources", "ck_invoice_sources_source_type", "source_type", ("TASK",))
    for table, name in MODULE_CHECKS:
        _widen(table, name, "module", OLD_MODULES)
    bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_code IN "
                         "('team_pay:read', 'team_pay:submit', 'team_pay:read_amounts', 'integrations:manage')"))
