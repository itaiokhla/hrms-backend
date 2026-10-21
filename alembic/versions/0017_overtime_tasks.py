"""Overtime entries and the tasks module (tasks, assignees, evidence, rate cards).

Additive (3.4 §9-§10, 3.9 §3.5, audit 03 §8 item 12):
  * overtime_entries: requested, computed and approved minutes (0-1440 each, approved never above
    requested), optional span, source, status, comp-off, versions (an edit of an approved entry is
    a new version superseding it), approval request and the run employee that consumed it;
  * tasks (external_ref unique per tenant, EC-TSK-01; accepted_quantity <= quantity, EC-TSK-04),
    task_assignees (split percentages), task_evidence (private storage pointers) and task_rate_cards.
Each table has row-level security, grants for the app role and composite same-company foreign keys.

Triggers (this PostgreSQL build has no btree_gist for exclusion constraints):
  * overtime_entries_guard serialises an employee's entries (row lock on the employee) and refuses
    overlapping spans and more than 24 hours on one date among live entries; a predecessor that has
    a live successor counts once (EC-OT-10);
  * task_rate_cards_guard refuses overlapping windows for the same task type and criteria
    (advisory lock 6171, company_id).

Revision ID: 0017_overtime_tasks
Revises: 0016_time_inputs
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0017_overtime_tasks'
down_revision: Union[str, None] = '0016_time_inputs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TENANT_TABLES = ("overtime_entries", "tasks", "task_assignees", "task_evidence", "task_rate_cards")

# Frozen copies: later model changes must not alter what this revision does.
OVERTIME_STATUSES = "('PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'CANCELLED', 'SUPERSEDED')"
OVERTIME_SOURCES = "('APPROVED_REQUESTS', 'ATTENDANCE_WITH_APPROVAL', 'ATTENDANCE_AUTO')"
DAY_TYPES = "('WORKDAY', 'WEEKLY_OFF', 'HOLIDAY')"
TASK_STATUSES = "('DRAFT', 'SUBMITTED', 'APPROVED', 'REJECTED', 'DISPUTED', 'PAID', 'REVERSED')"

OVERTIME_GUARD = """
CREATE OR REPLACE FUNCTION overtime_entries_guard() RETURNS trigger AS $$
DECLARE
    used integer;
BEGIN
    IF NEW.status NOT IN ('PENDING_APPROVAL', 'APPROVED') THEN
        RETURN NEW;
    END IF;
    PERFORM 1 FROM employees WHERE id = NEW.employee_id AND company_id = NEW.company_id FOR UPDATE;
    IF NEW.start_at IS NOT NULL AND EXISTS (
        SELECT 1 FROM overtime_entries o
        WHERE o.company_id = NEW.company_id AND o.employee_id = NEW.employee_id AND o.id <> NEW.id
          AND o.status IN ('PENDING_APPROVAL', 'APPROVED') AND o.start_at IS NOT NULL
          AND o.id IS DISTINCT FROM NEW.supersedes_id AND o.supersedes_id IS DISTINCT FROM NEW.id
          AND NOT EXISTS (SELECT 1 FROM overtime_entries s WHERE s.company_id = o.company_id
                          AND s.supersedes_id = o.id AND s.id <> NEW.id
                          AND s.status IN ('PENDING_APPROVAL', 'APPROVED'))
          AND tstzrange(o.start_at, o.end_at) && tstzrange(NEW.start_at, NEW.end_at)
    ) THEN
        RAISE EXCEPTION 'Overtime entries of one employee cannot overlap' USING ERRCODE = 'exclusion_violation';
    END IF;
    SELECT COALESCE(sum(COALESCE(o.minutes_approved, o.minutes_requested)), 0) INTO used
    FROM overtime_entries o
    WHERE o.company_id = NEW.company_id AND o.employee_id = NEW.employee_id AND o.date = NEW.date
      AND o.id <> NEW.id AND o.status IN ('PENDING_APPROVAL', 'APPROVED')
      AND o.id IS DISTINCT FROM NEW.supersedes_id AND o.supersedes_id IS DISTINCT FROM NEW.id
      AND NOT EXISTS (SELECT 1 FROM overtime_entries s WHERE s.company_id = o.company_id
                      AND s.supersedes_id = o.id AND s.id <> NEW.id
                      AND s.status IN ('PENDING_APPROVAL', 'APPROVED'));
    IF used + COALESCE(NEW.minutes_approved, NEW.minutes_requested) > 1440 THEN
        RAISE EXCEPTION 'More than 24 hours of overtime on one date' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

RATE_CARD_GUARD = """
CREATE OR REPLACE FUNCTION task_rate_cards_guard() RETURNS trigger AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(6171, NEW.company_id);
    IF EXISTS (
        SELECT 1 FROM task_rate_cards r
        WHERE r.company_id = NEW.company_id AND r.id <> NEW.id AND r.task_type = NEW.task_type
          AND r.criteria = NEW.criteria
          AND daterange(r.effective_from, r.effective_to, '[]') && daterange(NEW.effective_from, NEW.effective_to, '[]')
    ) THEN
        RAISE EXCEPTION 'Rate card rows for the same task type and criteria cannot overlap'
            USING ERRCODE = 'exclusion_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _base():
    return [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _keys(table: str):
    return [
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{table}_company_id_id'),
    ]


def _minutes(column: str):
    return sa.CheckConstraint(f"{column} IS NULL OR {column} BETWEEN 0 AND 1440", name=f'ck_overtime_entries_{column}')


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        'overtime_entries',
        *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('end_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('minutes_requested', sa.Integer(), nullable=False),
        sa.Column('minutes_computed', sa.Integer(), nullable=True),
        sa.Column('minutes_approved', sa.Integer(), nullable=True),
        sa.Column('bucket', sa.String(12), nullable=True),
        sa.Column('source', sa.String(30), nullable=False, server_default='APPROVED_REQUESTS'),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING_APPROVAL'),
        sa.Column('comp_off', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('pre_approval', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('supersedes_id', sa.Integer(), nullable=True),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consumed_by_run_employee_id', sa.Integer(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        *_keys('overtime_entries'),
        sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'],
                                name='fk_overtime_entries_employee'),
        sa.ForeignKeyConstraint(['company_id', 'supersedes_id'], ['overtime_entries.company_id', 'overtime_entries.id'],
                                name='fk_overtime_entries_supersedes'),
        sa.ForeignKeyConstraint(['company_id', 'approval_request_id'],
                                ['approval_requests.company_id', 'approval_requests.id'],
                                name='fk_overtime_entries_approval_request'),
        sa.ForeignKeyConstraint(['company_id', 'requested_by_user_id'], ['users.company_id', 'users.id'],
                                name='fk_overtime_entries_requested_by'),
        sa.CheckConstraint(f"status IN {OVERTIME_STATUSES}", name='ck_overtime_entries_status'),
        sa.CheckConstraint(f"source IN {OVERTIME_SOURCES}", name='ck_overtime_entries_source'),
        sa.CheckConstraint(f"bucket IS NULL OR bucket IN {DAY_TYPES}", name='ck_overtime_entries_bucket'),
        sa.CheckConstraint("minutes_requested BETWEEN 0 AND 1440", name='ck_overtime_entries_minutes_requested'),
        _minutes('minutes_computed'),
        _minutes('minutes_approved'),
        sa.CheckConstraint("minutes_approved IS NULL OR minutes_approved <= minutes_requested",
                           name='ck_overtime_entries_approved_le'),
        sa.CheckConstraint("status NOT IN ('APPROVED', 'SUPERSEDED') OR minutes_approved IS NOT NULL",
                           name='ck_overtime_entries_approved_minutes'),
        sa.CheckConstraint("(start_at IS NULL) = (end_at IS NULL)", name='ck_overtime_entries_span_pair'),
        sa.CheckConstraint("start_at IS NULL OR (end_at > start_at AND end_at <= start_at + interval '24 hours')",
                           name='ck_overtime_entries_span'),
        sa.CheckConstraint("version >= 1 AND (version = 1) = (supersedes_id IS NULL)",
                           name='ck_overtime_entries_version'),
        sa.CheckConstraint("consumed_by_run_employee_id IS NULL OR status IN ('APPROVED', 'SUPERSEDED')",
                           name='ck_overtime_entries_consumed'),
        sa.CheckConstraint("(status = 'PENDING_APPROVAL') = (decided_at IS NULL)", name='ck_overtime_entries_decided'),
        sa.CheckConstraint("row_version >= 1", name='ck_overtime_entries_row_version'),
    )
    op.create_index('ix_overtime_entries_company_id', 'overtime_entries', ['company_id'])
    op.create_index('ix_overtime_entries_employee_id', 'overtime_entries', ['employee_id'])
    op.create_index('ix_overtime_entries_employee_date', 'overtime_entries', ['company_id', 'employee_id', 'date'])
    op.create_index('uq_overtime_entries_live_successor', 'overtime_entries', ['company_id', 'supersedes_id'],
                    unique=True,
                    postgresql_where=sa.text("supersedes_id IS NOT NULL AND status IN ('PENDING_APPROVAL', 'APPROVED')"))

    op.create_table(
        'tasks',
        *_base(),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('project', sa.String(100), nullable=True),
        sa.Column('category', sa.String(100), nullable=True),
        sa.Column('task_type', sa.String(50), nullable=False),
        sa.Column('quantity', sa.Numeric(12, 4), nullable=False),
        sa.Column('unit', sa.String(30), nullable=False),
        sa.Column('rate', sa.Numeric(19, 4), nullable=True),
        sa.Column('rate_currency', sa.CHAR(3), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('completed_on', sa.Date(), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='DRAFT'),
        sa.Column('external_ref', sa.String(100), nullable=True),
        sa.Column('accepted_quantity', sa.Numeric(12, 4), nullable=True),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('approved_on', sa.Date(), nullable=True),
        sa.Column('paid_in_run_id', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        *_keys('tasks'),
        sa.ForeignKeyConstraint(['company_id', 'approval_request_id'],
                                ['approval_requests.company_id', 'approval_requests.id'],
                                name='fk_tasks_approval_request'),
        sa.ForeignKeyConstraint(['company_id', 'created_by_user_id'], ['users.company_id', 'users.id'],
                                name='fk_tasks_created_by'),
        sa.CheckConstraint(f"status IN {TASK_STATUSES}", name='ck_tasks_status'),
        sa.CheckConstraint("quantity > 0", name='ck_tasks_quantity'),
        sa.CheckConstraint("accepted_quantity IS NULL OR (accepted_quantity >= 0 AND accepted_quantity <= quantity)",
                           name='ck_tasks_accepted_quantity'),
        sa.CheckConstraint("(rate IS NULL) = (rate_currency IS NULL)", name='ck_tasks_rate_currency'),
        sa.CheckConstraint("rate IS NULL OR rate >= 0", name='ck_tasks_rate'),
        sa.CheckConstraint("rate_currency IS NULL OR rate_currency ~ '^[A-Z]{3}$'", name='ck_tasks_currency'),
        sa.CheckConstraint("status NOT IN ('APPROVED', 'PAID', 'REVERSED') OR approved_on IS NOT NULL",
                           name='ck_tasks_approved_on'),
        sa.CheckConstraint("(status IN ('PAID', 'REVERSED')) = (paid_in_run_id IS NOT NULL)", name='ck_tasks_paid'),
        sa.CheckConstraint("row_version >= 1", name='ck_tasks_row_version'),
    )
    op.create_index('ix_tasks_company_id', 'tasks', ['company_id'])
    op.create_index('ix_tasks_status', 'tasks', ['company_id', 'status'])
    op.create_index('uq_tasks_external_ref', 'tasks', ['company_id', 'external_ref'], unique=True,
                    postgresql_where=sa.text("external_ref IS NOT NULL"))

    op.create_table(
        'task_assignees',
        *_base(),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('split_percent', sa.Numeric(5, 2), nullable=False, server_default='100'),
        *_keys('task_assignees'),
        sa.UniqueConstraint('task_id', 'employee_id', name='uq_task_assignees_employee'),
        sa.ForeignKeyConstraint(['company_id', 'task_id'], ['tasks.company_id', 'tasks.id'], ondelete='CASCADE',
                                name='fk_task_assignees_task'),
        sa.ForeignKeyConstraint(['company_id', 'employee_id'], ['employees.company_id', 'employees.id'],
                                name='fk_task_assignees_employee'),
        sa.CheckConstraint("split_percent > 0 AND split_percent <= 100", name='ck_task_assignees_split'),
    )
    op.create_index('ix_task_assignees_company_id', 'task_assignees', ['company_id'])
    op.create_index('ix_task_assignees_task_id', 'task_assignees', ['task_id'])
    op.create_index('ix_task_assignees_employee_id', 'task_assignees', ['employee_id'])

    op.create_table(
        'task_evidence',
        *_base(),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('file_name', sa.String(200), nullable=False),
        sa.Column('storage_path', sa.String(300), nullable=False),
        sa.Column('mime', sa.String(100), nullable=False),
        sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.CHAR(64), nullable=False),
        sa.Column('uploaded_by_user_id', sa.Integer(), nullable=True),
        *_keys('task_evidence'),
        sa.UniqueConstraint('task_id', 'sha256', name='uq_task_evidence_file'),
        sa.ForeignKeyConstraint(['company_id', 'task_id'], ['tasks.company_id', 'tasks.id'], ondelete='CASCADE',
                                name='fk_task_evidence_task'),
        sa.ForeignKeyConstraint(['company_id', 'uploaded_by_user_id'], ['users.company_id', 'users.id'],
                                name='fk_task_evidence_uploaded_by'),
        sa.CheckConstraint("bytes > 0", name='ck_task_evidence_bytes'),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='ck_task_evidence_sha256'),
    )
    op.create_index('ix_task_evidence_company_id', 'task_evidence', ['company_id'])
    op.create_index('ix_task_evidence_task_id', 'task_evidence', ['task_id'])

    op.create_table(
        'task_rate_cards',
        *_base(),
        sa.Column('task_type', sa.String(50), nullable=False),
        sa.Column('criteria', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('rate', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        *_keys('task_rate_cards'),
        sa.ForeignKeyConstraint(['company_id', 'created_by_user_id'], ['users.company_id', 'users.id'],
                                name='fk_task_rate_cards_created_by'),
        sa.CheckConstraint("rate >= 0", name='ck_task_rate_cards_rate'),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name='ck_task_rate_cards_currency'),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name='ck_task_rate_cards_window'),
        sa.CheckConstraint("jsonb_typeof(criteria) = 'object'", name='ck_task_rate_cards_criteria'),
    )
    op.create_index('ix_task_rate_cards_company_id', 'task_rate_cards', ['company_id'])
    op.create_index('ix_task_rate_cards_type', 'task_rate_cards', ['company_id', 'task_type', 'effective_from'])

    _install_security(bind)


def _install_security(bind) -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")

    op.execute(OVERTIME_GUARD)
    op.execute(
        "CREATE TRIGGER trg_overtime_entries_guard BEFORE INSERT OR UPDATE ON overtime_entries "
        "FOR EACH ROW EXECUTE FUNCTION overtime_entries_guard()"
    )
    op.execute(RATE_CARD_GUARD)
    op.execute(
        "CREATE TRIGGER trg_task_rate_cards_guard BEFORE INSERT OR UPDATE ON task_rate_cards "
        "FOR EACH ROW EXECUTE FUNCTION task_rate_cards_guard()"
    )

    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TENANT_TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_task_rate_cards_guard ON task_rate_cards")
    op.execute("DROP FUNCTION IF EXISTS task_rate_cards_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_overtime_entries_guard ON overtime_entries")
    op.execute("DROP FUNCTION IF EXISTS overtime_entries_guard()")
    for table in ('task_rate_cards', 'task_evidence', 'task_assignees', 'tasks', 'overtime_entries'):
        op.drop_table(table)
