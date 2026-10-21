"""Wage inputs, reimbursement claims and receipts, loans and advances with their transaction ledger.

Additive (3.4 §13-§15, 3.1 §5.9-§5.11, 3.9 §3.5): tenant tables wage_input_imports, wage_inputs,
reimbursement_claims, reimbursement_receipts, loans, loan_actions and loan_transactions, each with
row-level security, grants and composite same-company foreign keys.

* wage_inputs: amount or units of one pay component (0015) for a target period (a date range until
  pay periods exist; target_period_id gets its foreign key with the pay_periods table). external_ref
  is unique per employee, component and period among live inputs (EC-VAR-02).
* loan_transactions is append-only (trigger): corrections are new ADJUSTMENT rows; at most one
  INSTALMENT per (loan, run employee) (EC-VAR-08) and one DISBURSEMENT per loan.

Downgrade drops the tables, the trigger and its function.

Revision ID: 0018_variable_inputs
Revises: 0017_overtime_tasks
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0018_variable_inputs'
down_revision: Union[str, None] = '0017_overtime_tasks'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("wage_input_imports", "wage_inputs", "reimbursement_claims", "reimbursement_receipts", "loans",
              "loan_actions", "loan_transactions")
WAGE_INPUT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "CONSUMED", "REJECTED", "CANCELLED")
LIVE_WAGE_INPUT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "CONSUMED")
WAGE_INPUT_SOURCES = ("MANUAL", "IMPORT", "TASK", "INCENTIVE_PLAN", "SYSTEM")
IMPORT_STATUSES = ("VALIDATED", "INVALID", "COMMITTING", "COMMITTED", "FAILED")
CLAIM_STATUSES = ("SUBMITTED", "APPROVED", "SCHEDULED", "PAID", "REJECTED")
PAYOUT_ROUTES = ("WITH_PAYROLL", "SEPARATE_PAYMENT")
TAXABILITY = ("TAXABLE", "EXEMPT", "PARTIAL", "UNCLASSIFIED")
LOAN_KINDS = ("LOAN", "ADVANCE")
LOAN_STATUSES = ("PENDING_APPROVAL", "APPROVED", "ACTIVE", "PAUSED", "CLOSED", "REJECTED", "CANCELLED")
LOAN_TRANSACTION_KINDS = ("DISBURSEMENT", "INSTALMENT", "PREPAYMENT", "WAIVER", "ADJUSTMENT")
LOAN_ACTIONS = ("PAUSE", "RESUME", "RESTRUCTURE", "PREPAY", "WAIVE")
LOAN_ACTION_STATUSES = ("PENDING_APPROVAL", "APPLIED", "REJECTED", "CANCELLED")

APPEND_ONLY_GUARD = """
CREATE OR REPLACE FUNCTION loan_transactions_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'loan_transactions is append-only: post an ADJUSTMENT instead'
        USING ERRCODE = 'check_violation';
END;
$$
"""


def _base():
    return [
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _fk(table, columns, target, name, ondelete=None):
    local = ['company_id', *([columns] if isinstance(columns, str) else columns)]
    return sa.ForeignKeyConstraint(local, [f'{target}.company_id', f'{target}.id'], name=f'fk_{table}_{name}',
                                   ondelete=ondelete)


def _currency(table, column='currency'):
    return sa.CheckConstraint(f"{column} ~ '^[A-Z]{{3}}$'", name=f'ck_{table}_{column}')


def _uq(table):
    return sa.UniqueConstraint('company_id', 'id', name=f'uq_{table}_company_id_id')


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        'wage_input_imports', *_base(),
        sa.Column('file_name', sa.String(200), nullable=False),
        sa.Column('storage_path', sa.String(300), nullable=False),
        sa.Column('sha256', sa.CHAR(64), nullable=False),
        sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('row_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('rows', jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('result', jsonb, nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('committed_at', sa.DateTime(timezone=True), nullable=True),
        _uq('wage_input_imports'),
        _fk('wage_input_imports', 'created_by_user_id', 'users', 'created_by'),
        sa.CheckConstraint(f"status IN {IMPORT_STATUSES}", name='ck_wage_input_imports_status'),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='ck_wage_input_imports_sha256'),
        sa.CheckConstraint("bytes > 0 AND row_count >= 0 AND error_count >= 0", name='ck_wage_input_imports_counts'),
        sa.CheckConstraint("jsonb_typeof(rows) = 'array'", name='ck_wage_input_imports_rows'),
    )
    op.create_index('ix_wage_input_imports_company_id', 'wage_input_imports', ['company_id'])

    op.create_table(
        'wage_inputs', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('component_id', sa.Integer(), nullable=False),
        sa.Column('component_code', sa.String(40), nullable=False),
        sa.Column('amount', sa.Numeric(19, 4), nullable=True),
        sa.Column('units', sa.Numeric(10, 4), nullable=True),
        sa.Column('rate', sa.Numeric(19, 4), nullable=True),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('fx_rule', jsonb, nullable=True),
        sa.Column('target_period_id', sa.Integer(), nullable=True),
        sa.Column('target_period_start', sa.Date(), nullable=False),
        sa.Column('target_period_end', sa.Date(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('attachments', jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('source', sa.String(20), nullable=False, server_default='MANUAL'),
        sa.Column('status', sa.String(20), nullable=False, server_default='DRAFT'),
        sa.Column('recurrence', jsonb, nullable=True),
        sa.Column('recurrence_parent_id', sa.Integer(), nullable=True),
        sa.Column('external_ref', sa.String(100), nullable=True),
        sa.Column('import_id', sa.Integer(), nullable=True),
        sa.Column('import_row', sa.Integer(), nullable=True),
        sa.Column('flags', jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('reserved_by_run_employee_id', sa.Integer(), nullable=True),
        sa.Column('consumed_by_run_employee_id', sa.Integer(), nullable=True),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        _uq('wage_inputs'),
        _fk('wage_inputs', 'employee_id', 'employees', 'employee'),
        _fk('wage_inputs', 'pay_group_id', 'pay_groups', 'pay_group'),
        _fk('wage_inputs', 'component_id', 'pay_components', 'component'),
        _fk('wage_inputs', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('wage_inputs', 'recurrence_parent_id', 'wage_inputs', 'recurrence_parent'),
        _fk('wage_inputs', 'import_id', 'wage_input_imports', 'import'),
        _fk('wage_inputs', 'approval_request_id', 'approval_requests', 'approval_request'),
        _fk('wage_inputs', 'created_by_user_id', 'users', 'created_by'),
        sa.CheckConstraint(f"status IN {WAGE_INPUT_STATUSES}", name='ck_wage_inputs_status'),
        sa.CheckConstraint(f"source IN {WAGE_INPUT_SOURCES}", name='ck_wage_inputs_source'),
        _currency('wage_inputs'),
        sa.CheckConstraint("(amount IS NULL) <> (units IS NULL)", name='ck_wage_inputs_amount_or_units'),
        sa.CheckConstraint("amount IS NULL OR amount <> 0", name='ck_wage_inputs_amount'),
        sa.CheckConstraint("units IS NULL OR units > 0", name='ck_wage_inputs_units'),
        sa.CheckConstraint("rate IS NULL OR (units IS NOT NULL AND rate >= 0)", name='ck_wage_inputs_rate'),
        sa.CheckConstraint("target_period_end >= target_period_start", name='ck_wage_inputs_period'),
        sa.CheckConstraint("fx_rule IS NULL OR jsonb_typeof(fx_rule) = 'object'", name='ck_wage_inputs_fx_rule'),
        sa.CheckConstraint("recurrence IS NULL OR jsonb_typeof(recurrence) = 'object'",
                           name='ck_wage_inputs_recurrence'),
        sa.CheckConstraint("jsonb_typeof(attachments) = 'array'", name='ck_wage_inputs_attachments'),
        sa.CheckConstraint("jsonb_typeof(flags) = 'array'", name='ck_wage_inputs_flags'),
        sa.CheckConstraint("(import_id IS NULL) = (import_row IS NULL)", name='ck_wage_inputs_import_row'),
        sa.CheckConstraint("(status = 'CONSUMED') = (consumed_by_run_employee_id IS NOT NULL)",
                           name='ck_wage_inputs_consumed'),
        sa.CheckConstraint("reserved_by_run_employee_id IS NULL OR status IN ('APPROVED', 'CONSUMED')",
                           name='ck_wage_inputs_reserved'),
        sa.CheckConstraint("row_version >= 1", name='ck_wage_inputs_row_version'),
    )
    op.create_index('ix_wage_inputs_company_id', 'wage_inputs', ['company_id'])
    op.create_index('ix_wage_inputs_employee_id', 'wage_inputs', ['employee_id'])
    op.create_index('ix_wage_inputs_recurrence_parent_id', 'wage_inputs', ['recurrence_parent_id'])
    op.create_index('uq_wage_inputs_external_ref', 'wage_inputs',
                    ['company_id', 'employee_id', 'component_id', 'target_period_start', 'external_ref'], unique=True,
                    postgresql_where=sa.text(f"external_ref IS NOT NULL AND status IN {LIVE_WAGE_INPUT_STATUSES}"))
    op.create_index('uq_wage_inputs_import_row', 'wage_inputs', ['import_id', 'import_row'], unique=True,
                    postgresql_where=sa.text("import_id IS NOT NULL"))
    op.create_index('ix_wage_inputs_period', 'wage_inputs', ['company_id', 'employee_id', 'target_period_start'])
    op.create_index('ix_wage_inputs_status', 'wage_inputs', ['company_id', 'status'])

    op.create_table(
        'reimbursement_claims', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('policy_version_id', sa.Integer(), nullable=False),
        sa.Column('claim_type', sa.String(40), nullable=False),
        sa.Column('claim_type_name', sa.String(100), nullable=False),
        sa.Column('taxability', sa.String(12), nullable=False, server_default='UNCLASSIFIED'),
        sa.Column('expense_date', sa.Date(), nullable=False),
        sa.Column('amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('merchant', sa.String(200), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('payout_route', sa.String(20), nullable=False),
        sa.Column('payout_amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('payout_currency', sa.CHAR(3), nullable=False),
        sa.Column('approved_amount', sa.Numeric(19, 4), nullable=True),
        sa.Column('fx_conversion_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='SUBMITTED'),
        sa.Column('flags', jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('scheduled_ref', sa.String(100), nullable=True),
        sa.Column('paid_ref', sa.String(100), nullable=True),
        sa.Column('submitted_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        _uq('reimbursement_claims'),
        _fk('reimbursement_claims', 'employee_id', 'employees', 'employee'),
        _fk('reimbursement_claims', 'pay_group_id', 'pay_groups', 'pay_group'),
        _fk('reimbursement_claims', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('reimbursement_claims', 'policy_version_id', 'policy_versions', 'policy_version'),
        _fk('reimbursement_claims', 'fx_conversion_id', 'fx_conversions', 'fx_conversion'),
        _fk('reimbursement_claims', 'approval_request_id', 'approval_requests', 'approval_request'),
        _fk('reimbursement_claims', 'submitted_by_user_id', 'users', 'submitted_by'),
        sa.CheckConstraint(f"status IN {CLAIM_STATUSES}", name='ck_reimbursement_claims_status'),
        sa.CheckConstraint(f"payout_route IN {PAYOUT_ROUTES}", name='ck_reimbursement_claims_payout_route'),
        sa.CheckConstraint(f"taxability IN {TAXABILITY}", name='ck_reimbursement_claims_taxability'),
        _currency('reimbursement_claims'),
        _currency('reimbursement_claims', 'payout_currency'),
        sa.CheckConstraint("amount > 0 AND payout_amount > 0", name='ck_reimbursement_claims_amount'),
        sa.CheckConstraint("approved_amount IS NULL OR (approved_amount > 0 AND approved_amount <= payout_amount)",
                           name='ck_reimbursement_claims_approved_amount'),
        sa.CheckConstraint("(currency = payout_currency) = (fx_conversion_id IS NULL)",
                           name='ck_reimbursement_claims_fx'),
        sa.CheckConstraint("status NOT IN ('APPROVED', 'SCHEDULED', 'PAID') OR approved_amount IS NOT NULL",
                           name='ck_reimbursement_claims_approved'),
        sa.CheckConstraint("(status = 'SCHEDULED') = (scheduled_ref IS NOT NULL)",
                           name='ck_reimbursement_claims_scheduled'),
        sa.CheckConstraint("(status = 'PAID') = (paid_ref IS NOT NULL)", name='ck_reimbursement_claims_paid'),
        sa.CheckConstraint("jsonb_typeof(flags) = 'array'", name='ck_reimbursement_claims_flags'),
        sa.CheckConstraint("row_version >= 1", name='ck_reimbursement_claims_row_version'),
    )
    op.create_index('ix_reimbursement_claims_company_id', 'reimbursement_claims', ['company_id'])
    op.create_index('ix_reimbursement_claims_employee_id', 'reimbursement_claims', ['employee_id'])
    op.create_index('ix_reimbursement_claims_employee_type', 'reimbursement_claims',
                    ['company_id', 'employee_id', 'claim_type', 'expense_date'])
    op.create_index('ix_reimbursement_claims_status', 'reimbursement_claims', ['company_id', 'status'])

    op.create_table(
        'reimbursement_receipts', *_base(),
        sa.Column('claim_id', sa.Integer(), nullable=False),
        sa.Column('file_name', sa.String(200), nullable=False),
        sa.Column('storage_path', sa.String(300), nullable=False),
        sa.Column('sha256', sa.CHAR(64), nullable=False),
        sa.Column('fingerprint', sa.CHAR(64), nullable=False),
        sa.Column('mime', sa.String(100), nullable=False),
        sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.Column('uploaded_by_user_id', sa.Integer(), nullable=True),
        _uq('reimbursement_receipts'),
        sa.UniqueConstraint('claim_id', 'sha256', name='uq_reimbursement_receipts_file'),
        _fk('reimbursement_receipts', 'claim_id', 'reimbursement_claims', 'claim', ondelete='CASCADE'),
        _fk('reimbursement_receipts', 'uploaded_by_user_id', 'users', 'uploaded_by'),
        sa.CheckConstraint("bytes > 0", name='ck_reimbursement_receipts_bytes'),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND fingerprint ~ '^[0-9a-f]{64}$'",
                           name='ck_reimbursement_receipts_hashes'),
    )
    op.create_index('ix_reimbursement_receipts_company_id', 'reimbursement_receipts', ['company_id'])
    op.create_index('ix_reimbursement_receipts_claim_id', 'reimbursement_receipts', ['claim_id'])
    op.create_index('ix_reimbursement_receipts_sha256', 'reimbursement_receipts', ['company_id', 'sha256'])
    op.create_index('ix_reimbursement_receipts_fingerprint', 'reimbursement_receipts', ['company_id', 'fingerprint'])

    op.create_table(
        'loans', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(8), nullable=False),
        sa.Column('principal', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('purpose', sa.Text(), nullable=True),
        sa.Column('tenure', sa.Integer(), nullable=False),
        sa.Column('interest', jsonb, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('schedule', jsonb, nullable=False),
        sa.Column('schedule_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('opening_balance', sa.Numeric(19, 4), nullable=True),
        sa.Column('recover_balance_on_exit', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING_APPROVAL'),
        sa.Column('policy_version_id', sa.Integer(), nullable=False),
        sa.Column('application_issues', jsonb, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('applied_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('disbursed_on', sa.Date(), nullable=True),
        sa.Column('disbursement_ref', sa.String(100), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        _uq('loans'),
        _fk('loans', 'employee_id', 'employees', 'employee'),
        _fk('loans', 'pay_group_id', 'pay_groups', 'pay_group'),
        _fk('loans', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('loans', 'policy_version_id', 'policy_versions', 'policy_version'),
        _fk('loans', 'approval_request_id', 'approval_requests', 'approval_request'),
        _fk('loans', 'applied_by_user_id', 'users', 'applied_by'),
        sa.CheckConstraint(f"kind IN {LOAN_KINDS}", name='ck_loans_kind'),
        sa.CheckConstraint(f"status IN {LOAN_STATUSES}", name='ck_loans_status'),
        _currency('loans'),
        sa.CheckConstraint("principal > 0", name='ck_loans_principal'),
        sa.CheckConstraint("tenure >= 1 AND (kind = 'LOAN' OR tenure <= 3)", name='ck_loans_tenure'),
        sa.CheckConstraint("schedule_version >= 1 AND (schedule_version = 1) = (opening_balance IS NULL)",
                           name='ck_loans_schedule_version'),
        sa.CheckConstraint("jsonb_typeof(schedule) = 'object' AND jsonb_typeof(interest) = 'object'",
                           name='ck_loans_json'),
        sa.CheckConstraint("status NOT IN ('ACTIVE', 'PAUSED', 'CLOSED') OR disbursed_on IS NOT NULL",
                           name='ck_loans_disbursed'),
        sa.CheckConstraint("row_version >= 1", name='ck_loans_row_version'),
    )
    op.create_index('ix_loans_company_id', 'loans', ['company_id'])
    op.create_index('ix_loans_employee_id', 'loans', ['employee_id'])
    op.create_index('ix_loans_employee_status', 'loans', ['company_id', 'employee_id', 'status'])

    op.create_table(
        'loan_actions', *_base(),
        sa.Column('loan_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(12), nullable=False),
        sa.Column('params', jsonb, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING_APPROVAL'),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        _uq('loan_actions'),
        _fk('loan_actions', 'loan_id', 'loans', 'loan'),
        _fk('loan_actions', 'approval_request_id', 'approval_requests', 'approval_request'),
        _fk('loan_actions', 'requested_by_user_id', 'users', 'requested_by'),
        sa.CheckConstraint(f"action IN {LOAN_ACTIONS}", name='ck_loan_actions_action'),
        sa.CheckConstraint(f"status IN {LOAN_ACTION_STATUSES}", name='ck_loan_actions_status'),
        sa.CheckConstraint("jsonb_typeof(params) = 'object'", name='ck_loan_actions_params'),
        sa.CheckConstraint("(status = 'PENDING_APPROVAL') = (decided_at IS NULL)", name='ck_loan_actions_decided'),
    )
    op.create_index('ix_loan_actions_company_id', 'loan_actions', ['company_id'])
    op.create_index('ix_loan_actions_loan_id', 'loan_actions', ['loan_id'])
    op.create_index('uq_loan_actions_one_pending', 'loan_actions', ['company_id', 'loan_id'], unique=True,
                    postgresql_where=sa.text("status = 'PENDING_APPROVAL'"))

    op.create_table(
        'loan_transactions', *_base(),
        sa.Column('loan_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(12), nullable=False),
        sa.Column('amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('schedule_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('run_employee_id', sa.Integer(), nullable=True),
        sa.Column('scheduled_amount', sa.Numeric(19, 4), nullable=True),
        sa.Column('effective_on', sa.Date(), nullable=False),
        sa.Column('reference', sa.String(100), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('action_id', sa.Integer(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        _uq('loan_transactions'),
        _fk('loan_transactions', 'loan_id', 'loans', 'loan'),
        _fk('loan_transactions', 'action_id', 'loan_actions', 'action'),
        _fk('loan_transactions', 'created_by_user_id', 'users', 'created_by'),
        sa.CheckConstraint(f"kind IN {LOAN_TRANSACTION_KINDS}", name='ck_loan_transactions_kind'),
        _currency('loan_transactions'),
        sa.CheckConstraint(
            "(kind = 'ADJUSTMENT' AND amount <> 0) OR (kind = 'INSTALMENT' AND amount >= 0) "
            "OR (kind IN ('DISBURSEMENT', 'PREPAYMENT', 'WAIVER') AND amount > 0)",
            name='ck_loan_transactions_amount',
        ),
        sa.CheckConstraint("(kind = 'INSTALMENT') = (run_employee_id IS NOT NULL)", name='ck_loan_transactions_run'),
        sa.CheckConstraint("scheduled_amount IS NULL OR (kind = 'INSTALMENT' AND scheduled_amount >= 0)",
                           name='ck_loan_transactions_scheduled'),
        sa.CheckConstraint("schedule_version >= 1", name='ck_loan_transactions_schedule_version'),
    )
    op.create_index('ix_loan_transactions_company_id', 'loan_transactions', ['company_id'])
    op.create_index('ix_loan_transactions_loan_id', 'loan_transactions', ['loan_id'])
    op.create_index('uq_loan_transactions_instalment', 'loan_transactions', ['loan_id', 'run_employee_id'],
                    unique=True, postgresql_where=sa.text("kind = 'INSTALMENT'"))
    op.create_index('uq_loan_transactions_disbursement', 'loan_transactions', ['loan_id'], unique=True,
                    postgresql_where=sa.text("kind = 'DISBURSEMENT'"))

    op.execute(APPEND_ONLY_GUARD)
    op.execute(
        "CREATE TRIGGER trg_loan_transactions_append_only BEFORE UPDATE OR DELETE ON loan_transactions "
        "FOR EACH ROW EXECUTE FUNCTION loan_transactions_append_only()"
    )
    _install_security(op.get_bind())


def _install_security(bind) -> None:
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in NEW_TABLES:
            privileges = "SELECT, INSERT" if table == "loan_transactions" else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_loan_transactions_append_only ON loan_transactions")
    op.execute("DROP FUNCTION IF EXISTS loan_transactions_append_only()")
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
