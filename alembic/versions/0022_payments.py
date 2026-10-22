"""Payments: obligations, batches, instructions, append-only events, payment files, statement imports and lines, reconciliation matches, webhook events.

Additive (3.7 §7-§11, 3.9 §3.8, decisions I-10 and I-17); app.models.payment_flows documents each table.
Every table but ``payment_webhook_events`` is a tenant table with row-level security, grants and
composite same-company foreign keys, including to the bank-accounts milestone's ``bank_accounts`` and
``funding_accounts`` (0021), so an instruction can never point at another tenant's account and a batch
or statement never at another tenant's funding account.

Database rules that services cannot be trusted to keep alone:

* ck_payment_instructions_settled_evidence: SETTLED / RECONCILED only with VERIFIED evidence
  (contract §23.2, I-17); ck_payment_instructions_submitted: a submitted status names its channel.
* uq_payment_instructions_identity: one instruction per (obligation, account, sequence) (3.7 §8.5);
  uq_payment_instructions_provider_ref: provider references are unique across tenants.
* payment_events are append-only (trigger payment_events_append_only); a provider event id is unique
  per tenant and source (EC-PAY-06).
* payment_files never hold a storage path: files with full account numbers are regenerated, not kept.
* bank statements import once per funding account and file hash; a line once per funding account and
  line hash (F-11).
* payment_manual_settlements: the reviewer differs from the uploader (EC-PAY-14).
* payment_webhook_events (platform inbox, system session only, no grants to the application role): one
  verified event per adapter and event id (EC-PAY-06, EC-PAY-08). It stores no company or instruction id
  (the applied event is the tenant's payment_events row), so it is not a tenant table.

Downgrade drops the tables, the trigger and its function.

Revision ID: 0022_payments
Revises: 0021_bank_accounts
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0022_payments'
down_revision: Union[str, None] = '0021_bank_accounts'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TENANT_TABLES = ("payment_obligations", "payment_batches", "payment_instructions", "payment_events", "payment_files",
                 "bank_statement_imports", "bank_statement_lines", "reconciliation_matches",
                 "payment_manual_settlements")
OBLIGATION_SOURCES = ("RUN_EMPLOYEE", "CLAIM", "LOAN", "INVOICE")
PAYEE_TYPES = ("EMPLOYEE", "CONTRACTOR", "NOMINEE")
OBLIGATION_STATUSES = ("OPEN", "PARTIAL", "SETTLED", "UNCLAIMED", "CANCELLED")
PAYMENT_METHODS = ("BANK_FILE", "API", "MANUAL")
BATCH_STATUSES = (
    "DRAFT", "APPROVAL_PENDING", "APPROVED", "FILE_GENERATED", "API_QUEUED", "SUBMITTED", "PARTIALLY_SETTLED",
    "SETTLED", "RECONCILED", "CANCELLED",
)
INSTRUCTION_STATUSES = (
    "NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED", "QUEUED", "SUBMITTED", "ACKNOWLEDGED", "PROCESSING",
    "PARTIALLY_SETTLED", "SETTLED", "RECONCILED", "ON_HOLD", "CANCELLED", "REJECTED", "FAILED", "RETURNED",
    "REVERSED", "REISSUED",
)
SUBMITTED_STATUSES = ("SUBMITTED", "ACKNOWLEDGED", "PROCESSING", "PARTIALLY_SETTLED", "SETTLED", "RECONCILED")
SUBMISSION_CHANNELS = ("MANUAL_UPLOAD", "API", "SANDBOX", "MANUAL_OFFLINE")
EVIDENCE_STATUSES = ("UNVERIFIED", "VERIFIED")
EVIDENCE_SOURCES = ("STATEMENT", "WEBHOOK", "MANUAL_PROOF")
EVENT_SOURCES = ("FILE", "WEBHOOK", "STATEMENT", "USER", "SYSTEM")
EVENT_OUTCOMES = ("APPLIED", "DUPLICATE", "STALE", "CONFLICT", "RECORDED")
STATEMENT_FORMATS = ("CSV", "CAMT053")
DIRECTIONS = ("DEBIT", "CREDIT")
MATCH_STATUSES = (
    "MATCHED", "MISMATCH", "UNMATCHED", "DUPLICATE", "RETURN", "IGNORED", "ACCEPTED_VARIANCE", "LINKED", "ESCALATED",
)
MANUAL_METHODS = ("CHEQUE", "CASH", "BANK_TRANSFER", "OTHER")
MANUAL_STATUSES = ("PENDING_REVIEW", "APPROVED", "REJECTED")
WEBHOOK_OUTCOMES = ("PROCESSED", "DUPLICATE", "REJECTED", "UNRESOLVED", "CONFLICT")

APPEND_ONLY = """
CREATE OR REPLACE FUNCTION payment_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'payment_events are append-only' USING ERRCODE = 'check_violation';
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


def _version(table):
    return sa.CheckConstraint("row_version >= 1", name=f'ck_{table}_row_version')


def _row_version():
    return sa.Column('row_version', sa.Integer(), nullable=False, server_default='1')


def _money(name, nullable=False):
    return sa.Column(name, sa.Numeric(19, 4), nullable=nullable)


def _indexes(table, *columns):
    op.create_index(f'ix_{table}_company_id', table, ['company_id'])
    for column in columns:
        op.create_index(f'ix_{table}_{column}', table, [column])


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    empty_object, empty_array = sa.text("'{}'::jsonb"), sa.text("'[]'::jsonb")

    op.create_table(
        'payment_obligations', *_base(),
        sa.Column('source_type', sa.String(15), nullable=False, server_default='RUN_EMPLOYEE'),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('run_employee_id', sa.Integer(), nullable=True),
        sa.Column('payee_type', sa.String(12), nullable=False, server_default='EMPLOYEE'),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=True),
        _money('amount'),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='OPEN'),
        sa.Column('pay_date', sa.Date(), nullable=True),
        sa.Column('carried_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('carried_reason', sa.Text(), nullable=True),
        _row_version(),
        _uq('payment_obligations'),
        sa.UniqueConstraint('company_id', 'run_employee_id', 'currency', name='uq_payment_obligations_run_employee'),
        _fk('payment_obligations', 'run_id', 'payroll_runs', 'run'),
        _fk('payment_obligations', 'run_employee_id', 'payroll_run_employees', 'run_employee'),
        _fk('payment_obligations', 'employee_id', 'employees', 'employee'),
        _fk('payment_obligations', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('payment_obligations', 'pay_group_id', 'pay_groups', 'pay_group'),
        sa.CheckConstraint(f"source_type IN {OBLIGATION_SOURCES}", name='ck_payment_obligations_source'),
        sa.CheckConstraint(f"payee_type IN {PAYEE_TYPES}", name='ck_payment_obligations_payee_type'),
        sa.CheckConstraint(f"status IN {OBLIGATION_STATUSES}", name='ck_payment_obligations_status'),
        sa.CheckConstraint("amount > 0", name='ck_payment_obligations_amount'),
        sa.CheckConstraint("(source_type = 'RUN_EMPLOYEE') = (run_employee_id IS NOT NULL AND run_id IS NOT NULL)",
                           name='ck_payment_obligations_run_source'),
        sa.CheckConstraint("carried_reason IS NULL OR carried_at IS NOT NULL", name='ck_payment_obligations_carried'),
        _currency('payment_obligations'),
        _version('payment_obligations'),
    )
    _indexes('payment_obligations', 'run_id', 'run_employee_id', 'employee_id', 'legal_entity_id')

    op.create_table(
        'payment_batches', *_base(),
        sa.Column('reference', sa.String(40), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('funding_account_id', sa.Integer(), nullable=False),
        sa.Column('method', sa.String(10), nullable=False),
        sa.Column('file_format', sa.String(30), nullable=True),
        sa.Column('adapter', sa.String(20), nullable=True),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='DRAFT'),
        sa.Column('item_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_amount', sa.Numeric(19, 4), nullable=False, server_default='0'),
        sa.Column('totals', jsonb, nullable=False, server_default=empty_object),
        sa.Column('value_date', sa.Date(), nullable=True),
        sa.Column('prepared_by_user_id', sa.Integer(), nullable=False),
        sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('released_by_user_id', sa.Integer(), nullable=True),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('submission_channel', sa.String(15), nullable=True),
        sa.Column('cancelled_by_user_id', sa.Integer(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('details', jsonb, nullable=False, server_default=empty_object),
        _row_version(),
        _uq('payment_batches'),
        sa.UniqueConstraint('company_id', 'reference', name='uq_payment_batches_reference'),
        _fk('payment_batches', 'run_id', 'payroll_runs', 'run'),
        _fk('payment_batches', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('payment_batches', 'funding_account_id', 'funding_accounts', 'funding_account'),
        _fk('payment_batches', 'prepared_by_user_id', 'users', 'prepared_by'),
        _fk('payment_batches', 'approved_by_user_id', 'users', 'approved_by'),
        _fk('payment_batches', 'released_by_user_id', 'users', 'released_by'),
        _fk('payment_batches', 'cancelled_by_user_id', 'users', 'cancelled_by'),
        sa.CheckConstraint(f"method IN {PAYMENT_METHODS}", name='ck_payment_batches_method'),
        sa.CheckConstraint(f"status IN {BATCH_STATUSES}", name='ck_payment_batches_status'),
        sa.CheckConstraint(f"submission_channel IS NULL OR submission_channel IN {SUBMISSION_CHANNELS}",
                           name='ck_payment_batches_channel'),
        sa.CheckConstraint("(method = 'BANK_FILE') = (file_format IS NOT NULL)", name='ck_payment_batches_format'),
        sa.CheckConstraint("(method = 'API') = (adapter IS NOT NULL)", name='ck_payment_batches_adapter'),
        sa.CheckConstraint("item_count >= 0 AND total_amount >= 0", name='ck_payment_batches_totals'),
        sa.CheckConstraint("jsonb_typeof(totals) = 'object'", name='ck_payment_batches_totals_shape'),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name='ck_payment_batches_details'),
        sa.CheckConstraint("status NOT IN ('SUBMITTED', 'PARTIALLY_SETTLED', 'SETTLED', 'RECONCILED') OR "
                           "(released_by_user_id IS NOT NULL AND released_at IS NOT NULL AND submission_channel IS NOT "
                           "NULL)", name='ck_payment_batches_released'),
        sa.CheckConstraint("(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name='ck_payment_batches_cancelled'),
        _currency('payment_batches'),
        _version('payment_batches'),
    )
    _indexes('payment_batches', 'run_id', 'legal_entity_id', 'funding_account_id')

    op.create_table(
        'payment_instructions', *_base(),
        sa.Column('obligation_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('batch_id', sa.Integer(), nullable=True),
        sa.Column('funding_account_id', sa.Integer(), nullable=True),
        sa.Column('bank_account_id', sa.Integer(), nullable=True),
        sa.Column('sequence', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('idempotency_key', sa.String(120), nullable=False),
        sa.Column('reference', sa.String(35), nullable=True),
        sa.Column('beneficiary_snapshot', jsonb, nullable=False, server_default=empty_object),
        sa.Column('beneficiary_enc', sa.Text(), nullable=True),
        _money('amount'),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        _money('account_amount', nullable=True),
        sa.Column('account_currency', sa.CHAR(3), nullable=True),
        sa.Column('fx_conversion_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='NOT_READY'),
        sa.Column('status_reason', sa.String(40), nullable=True),
        sa.Column('status_note', sa.Text(), nullable=True),
        sa.Column('submission_channel', sa.String(15), nullable=True),
        sa.Column('evidence_status', sa.String(10), nullable=False, server_default='UNVERIFIED'),
        sa.Column('evidence_source', sa.String(15), nullable=True),
        sa.Column('evidence_ref', sa.String(200), nullable=True),
        sa.Column('provider_ref', sa.String(80), nullable=True),
        sa.Column('reissue_of_instruction_id', sa.Integer(), nullable=True),
        sa.Column('reissue_request', jsonb, nullable=True),
        sa.Column('expected_settlement_date', sa.Date(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        _money('settled_amount', nullable=True),
        _money('variance_amount', nullable=True),
        sa.Column('variance_reason', sa.Text(), nullable=True),
        sa.Column('reconciled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('returned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_code', sa.String(40), nullable=True),
        sa.Column('failure_message', sa.String(300), nullable=True),
        sa.Column('carried_at', sa.DateTime(timezone=True), nullable=True),
        _row_version(),
        _uq('payment_instructions'),
        sa.UniqueConstraint('company_id', 'idempotency_key', name='uq_payment_instructions_idempotency'),
        sa.UniqueConstraint('company_id', 'reference', name='uq_payment_instructions_reference'),
        _fk('payment_instructions', 'obligation_id', 'payment_obligations', 'obligation'),
        _fk('payment_instructions', 'run_id', 'payroll_runs', 'run'),
        _fk('payment_instructions', 'employee_id', 'employees', 'employee'),
        _fk('payment_instructions', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('payment_instructions', 'batch_id', 'payment_batches', 'batch'),
        _fk('payment_instructions', 'funding_account_id', 'funding_accounts', 'funding_account'),
        _fk('payment_instructions', 'bank_account_id', 'bank_accounts', 'bank_account'),
        _fk('payment_instructions', 'reissue_of_instruction_id', 'payment_instructions', 'reissue_of'),
        _fk('payment_instructions', 'fx_conversion_id', 'fx_conversions', 'fx_conversion'),
        sa.CheckConstraint(f"status IN {INSTRUCTION_STATUSES}", name='ck_payment_instructions_status'),
        sa.CheckConstraint(f"submission_channel IS NULL OR submission_channel IN {SUBMISSION_CHANNELS}",
                           name='ck_payment_instructions_channel'),
        sa.CheckConstraint(f"evidence_status IN {EVIDENCE_STATUSES}", name='ck_payment_instructions_evidence'),
        sa.CheckConstraint(f"evidence_source IS NULL OR evidence_source IN {EVIDENCE_SOURCES}",
                           name='ck_payment_instructions_evidence_source'),
        sa.CheckConstraint("amount > 0", name='ck_payment_instructions_amount'),
        sa.CheckConstraint("sequence >= 1", name='ck_payment_instructions_sequence'),
        sa.CheckConstraint("(account_amount IS NULL) = (account_currency IS NULL)", name='ck_payment_instructions_fx'),
        sa.CheckConstraint("jsonb_typeof(beneficiary_snapshot) = 'object'", name='ck_payment_instructions_snapshot'),
        sa.CheckConstraint("reissue_request IS NULL OR jsonb_typeof(reissue_request) = 'object'",
                           name='ck_payment_instructions_reissue_request'),
        sa.CheckConstraint("reissue_of_instruction_id IS NULL OR reissue_of_instruction_id <> id",
                           name='ck_payment_instructions_not_own_reissue'),
        sa.CheckConstraint("status NOT IN ('SETTLED', 'RECONCILED') OR evidence_status = 'VERIFIED'",
                           name='ck_payment_instructions_settled_evidence'),
        sa.CheckConstraint(f"status NOT IN {SUBMITTED_STATUSES} OR (submission_channel IS NOT NULL "
                           "AND submitted_at IS NOT NULL)", name='ck_payment_instructions_submitted'),
        sa.CheckConstraint("evidence_status = 'UNVERIFIED' OR evidence_source IS NOT NULL",
                           name='ck_payment_instructions_evidence_named'),
        sa.CheckConstraint("status NOT IN ('READY', 'APPROVAL_PENDING', 'APPROVED', 'QUEUED') OR "
                           "(bank_account_id IS NOT NULL AND beneficiary_enc IS NOT NULL)",
                           name='ck_payment_instructions_beneficiary'),
        sa.CheckConstraint("status NOT IN ('APPROVAL_PENDING', 'APPROVED', 'QUEUED') OR batch_id IS NOT NULL",
                           name='ck_payment_instructions_batched'),
        sa.CheckConstraint("submission_channel <> 'SANDBOX' OR provider_ref IS NOT NULL",
                           name='ck_payment_instructions_sandbox_ref'),
        _currency('payment_instructions'),
        _version('payment_instructions'),
    )
    _indexes('payment_instructions', 'obligation_id', 'run_id', 'employee_id', 'legal_entity_id', 'batch_id',
             'funding_account_id', 'bank_account_id', 'reissue_of_instruction_id')
    op.create_index('uq_payment_instructions_identity', 'payment_instructions',
                    ['company_id', 'obligation_id', sa.text('COALESCE(bank_account_id, 0)'), 'sequence'], unique=True)
    op.create_index('uq_payment_instructions_provider_ref', 'payment_instructions', ['provider_ref'], unique=True,
                    postgresql_where=sa.text("provider_ref IS NOT NULL"))

    op.create_table(
        'payment_events', *_base(),
        sa.Column('instruction_id', sa.Integer(), nullable=False),
        sa.Column('event_status', sa.String(25), nullable=False),
        sa.Column('outcome', sa.String(10), nullable=False),
        sa.Column('from_status', sa.String(20), nullable=False),
        sa.Column('to_status', sa.String(20), nullable=False),
        sa.Column('source', sa.String(10), nullable=False),
        sa.Column('external_event_id', sa.String(120), nullable=True),
        sa.Column('evidence_ref', sa.String(200), nullable=True),
        sa.Column('sandbox', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('details', jsonb, nullable=False, server_default=empty_object),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        _uq('payment_events'),
        _fk('payment_events', 'instruction_id', 'payment_instructions', 'instruction'),
        _fk('payment_events', 'actor_user_id', 'users', 'actor'),
        sa.CheckConstraint(f"source IN {EVENT_SOURCES}", name='ck_payment_events_source'),
        sa.CheckConstraint(f"outcome IN {EVENT_OUTCOMES}", name='ck_payment_events_outcome'),
        sa.CheckConstraint(f"from_status IN {INSTRUCTION_STATUSES} AND to_status IN {INSTRUCTION_STATUSES}",
                           name='ck_payment_events_statuses'),
        sa.CheckConstraint("outcome IN ('APPLIED', 'RECORDED') OR from_status = to_status",
                           name='ck_payment_events_unapplied'),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name='ck_payment_events_details'),
    )
    _indexes('payment_events', 'instruction_id')
    op.create_index('uq_payment_events_external', 'payment_events', ['company_id', 'source', 'external_event_id'],
                    unique=True, postgresql_where=sa.text("external_event_id IS NOT NULL"))

    op.create_table(
        'payment_files', *_base(),
        sa.Column('batch_id', sa.Integer(), nullable=False),
        sa.Column('format', sa.String(30), nullable=False),
        sa.Column('filename', sa.String(120), nullable=False),
        sa.Column('media_type', sa.String(60), nullable=False),
        sa.Column('storage_path', sa.String(300), nullable=True),
        sa.Column('sha256', sa.CHAR(64), nullable=False),
        sa.Column('record_count', sa.Integer(), nullable=False),
        sa.Column('totals', jsonb, nullable=False, server_default=empty_object),
        sa.Column('generated_by_user_id', sa.Integer(), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('download_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_downloaded_at', sa.DateTime(timezone=True), nullable=True),
        _uq('payment_files'),
        sa.UniqueConstraint('company_id', 'batch_id', name='uq_payment_files_batch'),
        _fk('payment_files', 'batch_id', 'payment_batches', 'batch'),
        _fk('payment_files', 'generated_by_user_id', 'users', 'generated_by'),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='ck_payment_files_sha256'),
        sa.CheckConstraint("record_count >= 1 AND download_count >= 0", name='ck_payment_files_counts'),
        sa.CheckConstraint("jsonb_typeof(totals) = 'object'", name='ck_payment_files_totals'),
        sa.CheckConstraint("storage_path IS NULL", name='ck_payment_files_not_stored'),
    )
    _indexes('payment_files')

    op.create_table(
        'bank_statement_imports', *_base(),
        sa.Column('funding_account_id', sa.Integer(), nullable=False),
        sa.Column('format', sa.String(10), nullable=False),
        sa.Column('file_name', sa.String(200), nullable=False),
        sa.Column('file_sha256', sa.CHAR(64), nullable=False),
        sa.Column('statement_ref', sa.String(100), nullable=True),
        sa.Column('account_masked', sa.String(40), nullable=True),
        sa.Column('line_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('new_line_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('rejected', jsonb, nullable=False, server_default=empty_array),
        sa.Column('counts', jsonb, nullable=False, server_default=empty_object),
        sa.Column('imported_by_user_id', sa.Integer(), nullable=False),
        _uq('bank_statement_imports'),
        sa.UniqueConstraint('company_id', 'funding_account_id', 'file_sha256', name='uq_bank_statement_imports_file'),
        _fk('bank_statement_imports', 'funding_account_id', 'funding_accounts', 'funding_account'),
        _fk('bank_statement_imports', 'imported_by_user_id', 'users', 'imported_by'),
        sa.CheckConstraint(f"format IN {STATEMENT_FORMATS}", name='ck_bank_statement_imports_format'),
        sa.CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name='ck_bank_statement_imports_sha256'),
        sa.CheckConstraint("line_count >= 0 AND new_line_count >= 0 AND new_line_count <= line_count",
                           name='ck_bank_statement_imports_counts'),
        sa.CheckConstraint("jsonb_typeof(rejected) = 'array'", name='ck_bank_statement_imports_rejected'),
        sa.CheckConstraint("jsonb_typeof(counts) = 'object'", name='ck_bank_statement_imports_counts_shape'),
    )
    _indexes('bank_statement_imports', 'funding_account_id')

    op.create_table(
        'bank_statement_lines', *_base(),
        sa.Column('import_id', sa.Integer(), nullable=False),
        sa.Column('funding_account_id', sa.Integer(), nullable=False),
        sa.Column('source_row', sa.Integer(), nullable=False),
        sa.Column('value_date', sa.Date(), nullable=False),
        sa.Column('booking_date', sa.Date(), nullable=True),
        _money('amount'),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('direction', sa.String(6), nullable=False),
        sa.Column('reference', sa.String(140), nullable=True),
        sa.Column('end_to_end_id', sa.String(140), nullable=True),
        sa.Column('bank_reference', sa.String(140), nullable=True),
        sa.Column('counterparty', sa.String(200), nullable=True),
        sa.Column('counterparty_account_masked', sa.String(40), nullable=True),
        sa.Column('remittance', sa.Text(), nullable=True),
        sa.Column('return_reason', sa.String(10), nullable=True),
        sa.Column('line_hash', sa.CHAR(64), nullable=False),
        _uq('bank_statement_lines'),
        sa.UniqueConstraint('company_id', 'funding_account_id', 'line_hash', name='uq_bank_statement_lines_hash'),
        _fk('bank_statement_lines', 'import_id', 'bank_statement_imports', 'import', ondelete='CASCADE'),
        _fk('bank_statement_lines', 'funding_account_id', 'funding_accounts', 'funding_account'),
        sa.CheckConstraint(f"direction IN {DIRECTIONS}", name='ck_bank_statement_lines_direction'),
        sa.CheckConstraint("amount > 0", name='ck_bank_statement_lines_amount'),
        sa.CheckConstraint("line_hash ~ '^[0-9a-f]{64}$'", name='ck_bank_statement_lines_hash'),
        _currency('bank_statement_lines'),
    )
    _indexes('bank_statement_lines', 'import_id', 'funding_account_id')

    op.create_table(
        'reconciliation_matches', *_base(),
        sa.Column('statement_line_id', sa.Integer(), nullable=False),
        sa.Column('funding_account_id', sa.Integer(), nullable=False),
        sa.Column('instruction_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('reason', sa.String(40), nullable=False),
        sa.Column('message', sa.String(300), nullable=True),
        sa.Column('candidate_ids', jsonb, nullable=False, server_default=empty_array),
        _money('variance_amount', nullable=True),
        sa.Column('variance_currency', sa.CHAR(3), nullable=True),
        sa.Column('decided_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decision_reason', sa.Text(), nullable=True),
        _row_version(),
        _uq('reconciliation_matches'),
        sa.UniqueConstraint('company_id', 'statement_line_id', name='uq_reconciliation_matches_line'),
        _fk('reconciliation_matches', 'statement_line_id', 'bank_statement_lines', 'line', ondelete='CASCADE'),
        _fk('reconciliation_matches', 'funding_account_id', 'funding_accounts', 'funding_account'),
        _fk('reconciliation_matches', 'instruction_id', 'payment_instructions', 'instruction'),
        _fk('reconciliation_matches', 'decided_by_user_id', 'users', 'decided_by'),
        sa.CheckConstraint(f"status IN {MATCH_STATUSES}", name='ck_reconciliation_matches_status'),
        sa.CheckConstraint("jsonb_typeof(candidate_ids) = 'array'", name='ck_reconciliation_matches_candidates'),
        sa.CheckConstraint("(variance_amount IS NULL) = (variance_currency IS NULL)",
                           name='ck_reconciliation_matches_variance'),
        sa.CheckConstraint("status NOT IN ('ACCEPTED_VARIANCE', 'LINKED', 'ESCALATED') OR (decided_by_user_id IS NOT "
                           "NULL AND decision_reason IS NOT NULL)", name='ck_reconciliation_matches_decided'),
        sa.CheckConstraint("status NOT IN ('MATCHED', 'RETURN', 'ACCEPTED_VARIANCE', 'LINKED') OR instruction_id IS "
                           "NOT NULL", name='ck_reconciliation_matches_instruction'),
        _version('reconciliation_matches'),
    )
    _indexes('reconciliation_matches', 'funding_account_id', 'instruction_id')

    op.create_table(
        'payment_manual_settlements', *_base(),
        sa.Column('instruction_id', sa.Integer(), nullable=False),
        sa.Column('method', sa.String(15), nullable=False),
        sa.Column('evidence_reference', sa.String(100), nullable=False),
        sa.Column('evidence_name', sa.String(200), nullable=False),
        sa.Column('evidence_mime', sa.String(60), nullable=False),
        sa.Column('evidence_storage_path', sa.String(300), nullable=False),
        sa.Column('evidence_sha256', sa.CHAR(64), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('status', sa.String(15), nullable=False, server_default='PENDING_REVIEW'),
        sa.Column('submitted_by_user_id', sa.Integer(), nullable=False),
        sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_note', sa.Text(), nullable=True),
        _row_version(),
        _uq('payment_manual_settlements'),
        _fk('payment_manual_settlements', 'instruction_id', 'payment_instructions', 'instruction'),
        _fk('payment_manual_settlements', 'submitted_by_user_id', 'users', 'submitted_by'),
        _fk('payment_manual_settlements', 'reviewed_by_user_id', 'users', 'reviewed_by'),
        sa.CheckConstraint(f"method IN {MANUAL_METHODS}", name='ck_payment_manual_settlements_method'),
        sa.CheckConstraint(f"status IN {MANUAL_STATUSES}", name='ck_payment_manual_settlements_status'),
        sa.CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name='ck_payment_manual_settlements_sha256'),
        sa.CheckConstraint("reviewed_by_user_id IS NULL OR reviewed_by_user_id <> submitted_by_user_id",
                           name='ck_payment_manual_settlements_second_reviewer'),
        sa.CheckConstraint("(status = 'PENDING_REVIEW') = (reviewed_at IS NULL)",
                           name='ck_payment_manual_settlements_reviewed'),
        sa.CheckConstraint("status = 'PENDING_REVIEW' OR reviewed_by_user_id IS NOT NULL",
                           name='ck_payment_manual_settlements_reviewer'),
        _version('payment_manual_settlements'),
    )
    _indexes('payment_manual_settlements', 'instruction_id')
    op.create_index('uq_payment_manual_settlements_pending', 'payment_manual_settlements',
                    ['company_id', 'instruction_id'], unique=True,
                    postgresql_where=sa.text("status = 'PENDING_REVIEW'"))

    op.create_table(
        'payment_webhook_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('adapter', sa.String(20), nullable=False),
        sa.Column('event_id', sa.String(120), nullable=True),
        sa.Column('claimed_event_id', sa.String(120), nullable=True),
        sa.Column('event_type', sa.String(60), nullable=True),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('outcome', sa.String(12), nullable=False),
        sa.Column('error_code', sa.String(40), nullable=True),
        sa.Column('payload_sha256', sa.CHAR(64), nullable=False),
        sa.UniqueConstraint('adapter', 'event_id', name='uq_payment_webhook_events_event'),
        sa.CheckConstraint(f"outcome IN {WEBHOOK_OUTCOMES}", name='ck_payment_webhook_events_outcome'),
        sa.CheckConstraint("outcome = 'REJECTED' OR event_id IS NOT NULL", name='ck_payment_webhook_events_verified'),
        sa.CheckConstraint("outcome <> 'REJECTED' OR event_id IS NULL", name='ck_payment_webhook_events_rejected'),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name='ck_payment_webhook_events_sha256'),
    )

    op.execute(APPEND_ONLY)
    op.execute("CREATE TRIGGER trg_payment_events_append_only BEFORE UPDATE OR DELETE ON payment_events "
               "FOR EACH ROW EXECUTE FUNCTION payment_events_append_only()")
    _install_security(op.get_bind())


def _install_security(bind) -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TENANT_TABLES:
            privileges = "SELECT, INSERT" if table == "payment_events" else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
        # The webhook inbox is read and written through the system session only.
        op.execute(f"REVOKE ALL ON payment_webhook_events FROM {quoted}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_payment_events_append_only ON payment_events")
    op.execute("DROP FUNCTION IF EXISTS payment_events_append_only()")
    for table in ("payment_webhook_events", "payment_manual_settlements", "reconciliation_matches",
                  "bank_statement_lines", "bank_statement_imports", "payment_files", "payment_events",
                  "payment_instructions", "payment_batches", "payment_obligations"):
        op.drop_table(table)
