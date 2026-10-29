"""AS-M7 devices and imports (AS 04-attendance/01 §7, §8; AS 14 WI-AS-7.1).

NEW tenant tables (RLS ``tenant_isolation``, unique ``(company_id, id)``, composite same-company foreign keys, grants):
``attendance_device_user_mappings``, ``attendance_device_commands``, ``attendance_ingest_rejects``,
``attendance_imports`` (app/models/devices.py documents them).

NEW platform tables (no tenant column semantics, no RLS, no privilege for the app role; only the system session reads
and writes them, like ``auth_handoff_codes``): ``device_quarantine`` (unknown serials, first contact = proof of
possession) and ``device_registry_index`` (serial → company and device of activated PUSH / AGENT devices).

``tenant_data_keys.purpose`` accepts ``DEVICE_SECRET`` (AGENT HMAC secrets and rejected raw lines are encrypted with
it, SP-AS-011). Downgrade refuses while mappings, imports or registry rows exist.

Revision ID: 0055_as_m7_devices
Revises: 0054_attendance_team
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0055_as_m7_devices"
down_revision: Union[str, None] = "0054_attendance_team"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
COMMANDS = ("SYNC_TIME", "UPLOAD_USER", "DISABLE_USER", "DELETE_USER", "QUERY_USERS", "RESEND_LOGS", "REFRESH_SETTINGS")
OLD_PURPOSES = ("PAYROLL_PII", "MFA_SECRET", "ATTENDANCE_BIOMETRIC")
NEW_PURPOSES = OLD_PURPOSES + ("DEVICE_SECRET",)


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _fk(table: str, column: str, target: str, name: str, ondelete: str = "RESTRICT") -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE {ondelete}")


TABLES = {
    "attendance_device_user_mappings": f"""
        {BASE}, device_id INTEGER, device_user_id VARCHAR(32) NOT NULL, device_user_label VARCHAR(32),
        employee_id INTEGER NOT NULL, effective_from DATE NOT NULL, effective_to DATE,
        sync_state VARCHAR(16) NOT NULL DEFAULT 'NOT_APPLICABLE', enrolment JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        created_by_user_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_device_user_mappings_company_id_id UNIQUE (company_id, id),
        {_fk('attendance_device_user_mappings', 'device_id', 'attendance_devices', 'device')},
        {_fk('attendance_device_user_mappings', 'employee_id', 'employees', 'employee')},
        CONSTRAINT ck_attendance_device_user_mappings_sync CHECK (sync_state IN
            ('PENDING', 'SYNCED', 'FAILED', 'NOT_APPLICABLE')),
        CONSTRAINT ck_attendance_device_user_mappings_dates CHECK (effective_to IS NULL OR effective_to >= effective_from)""",
    "attendance_device_commands": f"""
        {BASE}, device_id INTEGER NOT NULL, command VARCHAR(20) NOT NULL, payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        status VARCHAR(10) NOT NULL DEFAULT 'QUEUED', return_code VARCHAR(20), expires_at TIMESTAMPTZ NOT NULL,
        sent_at TIMESTAMPTZ, acked_at TIMESTAMPTZ, requested_by_user_id INTEGER, row_version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_attendance_device_commands_company_id_id UNIQUE (company_id, id),
        {_fk('attendance_device_commands', 'device_id', 'attendance_devices', 'device')},
        CONSTRAINT ck_attendance_device_commands_command CHECK (command IN {_in(COMMANDS)}),
        CONSTRAINT ck_attendance_device_commands_status CHECK (status IN
            ('QUEUED', 'SENT', 'ACKED', 'FAILED', 'EXPIRED', 'CANCELLED'))""",
    "attendance_ingest_rejects": f"""
        {BASE}, device_id INTEGER, ingest_batch_id VARCHAR(64) NOT NULL, line_no INTEGER NOT NULL,
        payload_hash VARCHAR(64) NOT NULL, raw_line_enc TEXT, reason VARCHAR(60) NOT NULL,
        status VARCHAR(10) NOT NULL DEFAULT 'OPEN', resolved_by_user_id INTEGER, resolved_at TIMESTAMPTZ,
        CONSTRAINT uq_attendance_ingest_rejects_company_id_id UNIQUE (company_id, id),
        {_fk('attendance_ingest_rejects', 'device_id', 'attendance_devices', 'device')},
        CONSTRAINT ck_attendance_ingest_rejects_status CHECK (status IN ('OPEN', 'DISMISSED', 'FIXED'))""",
    "attendance_imports": f"""
        {BASE}, file_name VARCHAR(200) NOT NULL, status VARCHAR(10) NOT NULL DEFAULT 'DRY_RUN',
        rows JSONB NOT NULL DEFAULT '[]'::jsonb, totals JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        created_by_user_id INTEGER, committed_by_user_id INTEGER, committed_at TIMESTAMPTZ,
        CONSTRAINT uq_attendance_imports_company_id_id UNIQUE (company_id, id),
        CONSTRAINT ck_attendance_imports_status CHECK (status IN ('DRY_RUN', 'COMMITTED')),
        CONSTRAINT ck_attendance_imports_rows CHECK (jsonb_typeof(rows) = 'array')""",
}
INDEXES = (
    "CREATE INDEX ix_attendance_device_user_mappings_lookup ON attendance_device_user_mappings (company_id, device_user_id)",
    "CREATE INDEX ix_attendance_device_commands_queue ON attendance_device_commands (company_id, device_id) "
    "WHERE status = 'QUEUED'",
    "CREATE INDEX ix_attendance_ingest_rejects_open ON attendance_ingest_rejects (company_id, device_id) "
    "WHERE status = 'OPEN'",
)
PLATFORM = {
    "device_quarantine": """
        id SERIAL PRIMARY KEY, serial_number VARCHAR(64) NOT NULL UNIQUE, vendor_guess VARCHAR(20),
        first_seen_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL, last_ip_hash VARCHAR(64),
        payload_count INTEGER NOT NULL DEFAULT 0, status VARCHAR(10) NOT NULL DEFAULT 'SEEN', claimed_company_id INTEGER,
        CONSTRAINT ck_device_quarantine_status CHECK (status IN ('SEEN', 'CLAIMED', 'BLOCKED'))""",
    "device_registry_index": """
        id SERIAL PRIMARY KEY, serial_number VARCHAR(64) NOT NULL UNIQUE, company_id INTEGER NOT NULL,
        device_id INTEGER NOT NULL, activated_at TIMESTAMPTZ NOT NULL, active BOOLEAN NOT NULL DEFAULT true""",
}


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
    for table, body in PLATFORM.items():
        op.execute(f"CREATE TABLE {table} ({body})")
    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
        for table in PLATFORM:  # the system session only (defence in depth for older grants)
            op.execute(f"REVOKE ALL ON {table} FROM {quoted}")
    op.drop_constraint("ck_tenant_data_keys_purpose", "tenant_data_keys", type_="check")
    op.create_check_constraint("ck_tenant_data_keys_purpose", "tenant_data_keys", f"purpose IN {NEW_PURPOSES}")


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text(
        "SELECT (SELECT count(*) FROM attendance_device_user_mappings) + (SELECT count(*) FROM attendance_imports) "
        "+ (SELECT count(*) FROM device_registry_index) + (SELECT count(*) FROM tenant_data_keys "
        "WHERE purpose = 'DEVICE_SECRET')")).scalar()
    if used:
        raise RuntimeError("device mappings, imports, registered serials or device keys exist")
    op.drop_constraint("ck_tenant_data_keys_purpose", "tenant_data_keys", type_="check")
    op.create_check_constraint("ck_tenant_data_keys_purpose", "tenant_data_keys", f"purpose IN {OLD_PURPOSES}")
    for table in PLATFORM:
        op.execute(f"DROP TABLE {table}")
    for table in reversed(list(TABLES)):
        op.execute(f"DROP TABLE {table}")
