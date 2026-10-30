"""AI assistant tables: conversations, messages and prepared actions (docs/leave_agent.md §10, app/models/agent.py).

``agent_threads``, ``agent_messages`` and ``agent_actions`` are tenant tables: row-level security with the
``tenant_isolation`` policy on ``app.company_id`` (the application role is NOBYPASSRLS), composite
``(company_id, id)`` references between them and to ``users`` (RESTRICT, no cascades), check constraints for roles,
statuses and risk tiers, ``(company_id, idempotency_key)`` unique on actions and ``(company_id, user_id,
client_message_id)`` unique on messages (NULL client ids are not deduplicated). Ids are random UUID strings, so the
tables own no sequence.

Revision ID: 0068_agent_tables
Revises: 0067_roster_reminder_jobs
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0068_agent_tables"
down_revision: Union[str, None] = "0067_roster_reminder_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
BASE = ("id VARCHAR(36) PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE RESTRICT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")


def _fk(table: str, column: str, target: str, name: str) -> str:
    return (f"CONSTRAINT fk_{table}_{name} FOREIGN KEY (company_id, {column}) "
            f"REFERENCES {target} (company_id, id) ON DELETE RESTRICT")


TABLES = {
    "agent_threads": f"""
        {BASE}, user_id INTEGER NOT NULL, module VARCHAR(30) NOT NULL DEFAULT 'leave', title VARCHAR(120),
        status VARCHAR(10) NOT NULL DEFAULT 'active',
        CONSTRAINT uq_agent_threads_company_id_id UNIQUE (company_id, id),
        {_fk('agent_threads', 'user_id', 'users', 'user')},
        CONSTRAINT ck_agent_threads_status CHECK (status IN ('active', 'archived')),
        CONSTRAINT ck_agent_threads_module CHECK (module ~ '^[a-z][a-z_]{{1,29}}$')""",
    "agent_messages": f"""
        {BASE}, thread_id VARCHAR(36) NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(12) NOT NULL,
        content TEXT NOT NULL, client_message_id VARCHAR(100), reply_to_id VARCHAR(36),
        metadata_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        CONSTRAINT uq_agent_messages_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_agent_messages_client_message UNIQUE (company_id, user_id, client_message_id),
        {_fk('agent_messages', 'thread_id', 'agent_threads', 'thread')},
        {_fk('agent_messages', 'user_id', 'users', 'user')},
        {_fk('agent_messages', 'reply_to_id', 'agent_messages', 'reply_to')},
        CONSTRAINT ck_agent_messages_role CHECK (role IN ('user', 'assistant', 'system_event')),
        CONSTRAINT ck_agent_messages_content CHECK (char_length(content) <= 20000),
        CONSTRAINT ck_agent_messages_metadata CHECK (jsonb_typeof(metadata_json) = 'object')""",
    "agent_actions": f"""
        {BASE}, thread_id VARCHAR(36) NOT NULL, user_id INTEGER NOT NULL, message_id VARCHAR(36),
        module VARCHAR(30) NOT NULL, action_type VARCHAR(60) NOT NULL, permission_code VARCHAR(60) NOT NULL,
        risk VARCHAR(4) NOT NULL, payload_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        preview_json JSONB NOT NULL DEFAULT '{{}}'::jsonb, status VARCHAR(10) NOT NULL DEFAULT 'prepared',
        idempotency_key VARCHAR(80) NOT NULL, confirmation_nonce VARCHAR(64) NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ, result_json JSONB, failure_code VARCHAR(60),
        CONSTRAINT uq_agent_actions_company_id_id UNIQUE (company_id, id),
        CONSTRAINT uq_agent_actions_idempotency UNIQUE (company_id, idempotency_key),
        {_fk('agent_actions', 'thread_id', 'agent_threads', 'thread')},
        {_fk('agent_actions', 'user_id', 'users', 'user')},
        {_fk('agent_actions', 'message_id', 'agent_messages', 'message')},
        CONSTRAINT ck_agent_actions_status CHECK (status IN
            ('prepared', 'executing', 'completed', 'cancelled', 'expired', 'failed')),
        CONSTRAINT ck_agent_actions_risk CHECK (risk IN ('R2', 'R3')),
        CONSTRAINT ck_agent_actions_completed CHECK (status <> 'completed' OR completed_at IS NOT NULL),
        CONSTRAINT ck_agent_actions_json CHECK (jsonb_typeof(payload_json) = 'object'
            AND jsonb_typeof(preview_json) = 'object')""",
}
INDEXES = (
    "CREATE INDEX ix_agent_threads_owner ON agent_threads (company_id, user_id, updated_at)",
    "CREATE INDEX ix_agent_messages_thread ON agent_messages (company_id, thread_id, created_at)",
    "CREATE INDEX ix_agent_messages_owner ON agent_messages (company_id, user_id, created_at)",
    "CREATE INDEX ix_agent_actions_thread ON agent_actions (company_id, thread_id, created_at)",
    "CREATE INDEX ix_agent_actions_owner ON agent_actions (company_id, user_id, status, expires_at)",
)


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
    for role in _app_roles(bind):
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")


def downgrade() -> None:
    for table in reversed(list(TABLES)):
        op.execute(f"DROP TABLE IF EXISTS {table}")
