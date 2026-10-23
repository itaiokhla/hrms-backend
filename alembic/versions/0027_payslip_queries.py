"""Payslip questions: payslip_queries (3.6, setting payslip.queries_enabled).

Additive: one tenant table with row-level security, grants and composite same-company foreign keys
(app.models.payslip_queries documents the table). Database rules services cannot be trusted to keep alone:
bounded question and answer texts, an answer always records who answered and when, nobody answers their
own question, a closed question records when it was closed.

Downgrade drops the table.

Revision ID: 0027_payslip_queries
Revises: 0026_gapfill_security
Create Date: 2026-10-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0027_payslip_queries'
down_revision: Union[str, None] = '0026_gapfill_security'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TABLE = 'payslip_queries'
STATUSES = ("OPEN", "ANSWERED", "CLOSED")
QUESTION_MAX = 2000
ANSWER_MAX = 4000


def _fk(columns, target, name):
    local = ['company_id', *([columns] if isinstance(columns, str) else columns)]
    return sa.ForeignKeyConstraint(local, [f'{target}.company_id', f'{target}.id'], name=f'fk_{TABLE}_{name}')


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('run_employee_id', sa.Integer(), nullable=True),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=True),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='OPEN'),
        sa.Column('asked_by_user_id', sa.Integer(), nullable=False),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('answered_by_user_id', sa.Integer(), nullable=True),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{TABLE}_company_id_id'),
        _fk('document_id', 'documents', 'document'),
        _fk('run_id', 'payroll_runs', 'run'),
        _fk('run_employee_id', 'payroll_run_employees', 'run_employee'),
        _fk('employee_id', 'employees', 'employee'),
        _fk('legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('pay_group_id', 'pay_groups', 'pay_group'),
        _fk('asked_by_user_id', 'users', 'asked_by'),
        _fk('answered_by_user_id', 'users', 'answered_by'),
        _fk('closed_by_user_id', 'users', 'closed_by'),
        sa.CheckConstraint(f"status IN {STATUSES}", name=f'ck_{TABLE}_status'),
        sa.CheckConstraint(f"char_length(btrim(question)) BETWEEN 1 AND {QUESTION_MAX}", name=f'ck_{TABLE}_question'),
        sa.CheckConstraint(f"answer IS NULL OR char_length(btrim(answer)) BETWEEN 1 AND {ANSWER_MAX}",
                           name=f'ck_{TABLE}_answer'),
        sa.CheckConstraint("(answer IS NULL) = (answered_at IS NULL) AND (answer IS NULL) = (answered_by_user_id IS NULL)",
                           name=f'ck_{TABLE}_answered'),
        sa.CheckConstraint("status <> 'ANSWERED' OR answer IS NOT NULL", name=f'ck_{TABLE}_answered_status'),
        sa.CheckConstraint("(status = 'CLOSED') = (closed_at IS NOT NULL)", name=f'ck_{TABLE}_closed'),
        sa.CheckConstraint("answered_by_user_id IS NULL OR answered_by_user_id <> asked_by_user_id",
                           name=f'ck_{TABLE}_not_self_answered'),
        sa.CheckConstraint("row_version >= 1", name=f'ck_{TABLE}_row_version'),
    )
    op.create_index(f'ix_{TABLE}_company_id', TABLE, ['company_id'])
    op.create_index(f'ix_{TABLE}_document_id', TABLE, ['document_id'])
    op.create_index(f'ix_{TABLE}_employee_id', TABLE, ['employee_id'])
    op.create_index(f'ix_{TABLE}_scope_status', TABLE, ['company_id', 'legal_entity_id', 'status'])
    op.create_index(f'ix_{TABLE}_employee_status', TABLE, ['company_id', 'employee_id', 'status'])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {TABLE} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {TABLE}_id_seq TO {quoted}")


def downgrade() -> None:
    op.drop_table(TABLE)
