"""Documents v2: documents, numbering sequences, access log, export jobs, invoices and credit notes.

Additive (3.6, 3.9 §3.7, 01 §5.7, decision I-19); every table is a tenant table with row-level
security, grants and composite same-company foreign keys (app.models.pay_documents documents each
table). Legacy payslips stay in issued_documents, untouched: v2 documents live in their own table
and namespace (PAYSLIP_V2, source_key "run_employee:<id>"), so a legacy payroll id equal to a v2
run-employee id can never serve the wrong payslip.

Database rules that services cannot be trusted to keep alone:

* documents_immutable: a document is never deleted; its identity (type, number once set, entity,
  subject, source key, version), frozen content, hashes, template, brand version, public id and
  issue date never change. Publication state, the storage pointer (after a re-render) and the
  re-render flags may change (EC-DOC-03).
* uq_documents_number: unique (company, legal entity, type, number); numbers come from
  document_sequences rows locked FOR UPDATE in the issuing transaction (EC-DOC-08/09).
* document_access_log_append_only: the access trail cannot be edited or deleted.
* uq_invoice_sources_live: an approved task is billed by at most one live invoice.

Downgrade drops the tables, triggers and functions.

Revision ID: 0023_documents
Revises: 0022_payments
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0023_documents'
down_revision: Union[str, None] = '0022_payments'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("documents", "document_sequences", "export_jobs", "document_access_log", "document_generation_tasks",
              "document_notifications", "invoices", "invoice_sources", "self_billing_agreements",
              "document_requests", "payslip_pdf_secrets")
DOC_TYPES = (
    "PAYSLIP_V2", "REVERSAL_STATEMENT", "PAYMENT_ADVICE", "INVOICE", "SELF_BILLED_INVOICE", "CREDIT_NOTE",
    "PAYROLL_REGISTER", "BANK_TRANSFER_REPORT", "FUNDING_REQUEST", "FINAL_SETTLEMENT_STATEMENT",
    "SALARY_CERTIFICATE", "EMPLOYMENT_LETTER", "INCREMENT_LETTER", "LOAN_STATEMENT", "REIMBURSEMENT_STATEMENT",
)
DOCUMENT_STATUSES = ("GENERATED", "PUBLISHED", "VIEWED", "EMAIL_SENT", "EMAIL_FAILED", "SUPERSEDED", "REVERSED")
SUBJECT_TYPES = ("EMPLOYEE", "PAYEE", "RUN", "BATCH", "INSTRUCTION", "INVOICE")
PUBLISH_TRIGGERS = ("ON_FINALIZE", "ON_PAYMENT_SUBMITTED", "ON_SETTLED", "SCHEDULED", "MANUAL", "ON_ISSUE")
ACCESS_ACTIONS = ("VIEW", "DOWNLOAD", "EMAIL", "VERIFY", "REGENERATE", "EXPORT")
TASK_STATUSES = ("QUEUED", "GENERATED", "FAILED")
NOTIFICATION_CHANNELS = ("IN_APP", "EMAIL")
NOTIFICATION_STATUSES = ("PENDING", "SENT", "FAILED", "SKIPPED")
EXPORT_KINDS = ("PAYSLIPS_ZIP", "REGISTER_CSV", "REGISTER_XLSX")
EXPORT_STATUSES = ("QUEUED", "GENERATING", "READY", "FAILED", "EXPIRED")
INVOICE_KINDS = ("INVOICE", "CREDIT_NOTE")
INVOICE_MODES = ("INVOICE", "SELF_BILLED")
INVOICE_STATUSES = ("DRAFT", "ISSUED", "APPROVED", "PAID", "CREDITED")
INVOICE_SOURCE_TYPES = ("TASK",)
AGREEMENT_STATUSES = ("ACTIVE", "REVOKED")
REQUEST_DOC_TYPES = ("SALARY_CERTIFICATE", "EMPLOYMENT_LETTER")
REQUEST_STATUSES = ("PENDING", "APPROVED", "REJECTED", "ISSUED")

FUNCTIONS = ("documents_immutable", "document_access_log_append_only")

DOCUMENTS_IMMUTABLE = """
CREATE OR REPLACE FUNCTION documents_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'DOCUMENT_IMMUTABLE: issued documents are never deleted' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.doc_type IS DISTINCT FROM OLD.doc_type
            OR (OLD.number IS NOT NULL AND NEW.number IS DISTINCT FROM OLD.number)
            OR NEW.legal_entity_id IS DISTINCT FROM OLD.legal_entity_id
            OR NEW.pay_group_id IS DISTINCT FROM OLD.pay_group_id
            OR NEW.public_id IS DISTINCT FROM OLD.public_id
            OR NEW.subject_type IS DISTINCT FROM OLD.subject_type
            OR NEW.subject_id IS DISTINCT FROM OLD.subject_id
            OR NEW.employee_id IS DISTINCT FROM OLD.employee_id
            OR NEW.run_id IS DISTINCT FROM OLD.run_id
            OR NEW.run_employee_id IS DISTINCT FROM OLD.run_employee_id
            OR NEW.batch_id IS DISTINCT FROM OLD.batch_id
            OR NEW.instruction_id IS DISTINCT FROM OLD.instruction_id
            OR NEW.period_start IS DISTINCT FROM OLD.period_start
            OR NEW.period_end IS DISTINCT FROM OLD.period_end
            OR NEW.issue_date IS DISTINCT FROM OLD.issue_date
            OR NEW.source_key IS DISTINCT FROM OLD.source_key
            OR NEW.version IS DISTINCT FROM OLD.version
            OR NEW.source_refs IS DISTINCT FROM OLD.source_refs
            OR NEW.title IS DISTINCT FROM OLD.title
            OR NEW.draft IS DISTINCT FROM OLD.draft
            OR NEW.content IS DISTINCT FROM OLD.content
            OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
            OR NEW.sha256 IS DISTINCT FROM OLD.sha256
            OR NEW.bytes IS DISTINCT FROM OLD.bytes
            OR NEW.mime IS DISTINCT FROM OLD.mime
            OR NEW.brand_version IS DISTINCT FROM OLD.brand_version
            OR NEW.brand_profile_id IS DISTINCT FROM OLD.brand_profile_id
            OR NEW.entity_brand_profile_id IS DISTINCT FROM OLD.entity_brand_profile_id
            OR NEW.template_version IS DISTINCT FROM OLD.template_version
            OR NEW.template_policy_version_id IS DISTINCT FROM OLD.template_policy_version_id
            OR NEW.renderer IS DISTINCT FROM OLD.renderer
            OR NEW.locale IS DISTINCT FROM OLD.locale
            OR NEW.languages IS DISTINCT FROM OLD.languages
            OR NEW.labels IS DISTINCT FROM OLD.labels
            OR NEW.supersedes_document_id IS DISTINCT FROM OLD.supersedes_document_id
            OR NEW.issued_by_user_id IS DISTINCT FROM OLD.issued_by_user_id
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
            OR NEW.company_id IS DISTINCT FROM OLD.company_id THEN
        RAISE EXCEPTION 'DOCUMENT_IMMUTABLE: an issued document never changes; issue a new version'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

ACCESS_LOG_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION document_access_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'document_access_log is append-only' USING ERRCODE = 'check_violation';
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


def _uq(table):
    return sa.UniqueConstraint('company_id', 'id', name=f'uq_{table}_company_id_id')


def _in(table, column, values, nullable=False):
    rule = f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"
    if nullable:
        rule = f"{column} IS NULL OR {rule}"
    return sa.CheckConstraint(rule, name=f'ck_{table}_{column}')


def _row_version():
    return sa.Column('row_version', sa.Integer(), nullable=False, server_default='1')


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    empty_object, empty_array = sa.text("'{}'::jsonb"), sa.text("'[]'::jsonb")

    op.create_table(
        'documents', *_base(),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pay_group_id', sa.Integer(), nullable=True),
        sa.Column('doc_type', sa.String(30), nullable=False),
        sa.Column('number', sa.String(80), nullable=True),
        sa.Column('public_id', sa.String(32), nullable=False),
        sa.Column('subject_type', sa.String(12), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('run_employee_id', sa.Integer(), nullable=True),
        sa.Column('batch_id', sa.Integer(), nullable=True),
        sa.Column('instruction_id', sa.Integer(), nullable=True),
        sa.Column('period_start', sa.Date(), nullable=True),
        sa.Column('period_end', sa.Date(), nullable=True),
        sa.Column('issue_date', sa.Date(), nullable=False),
        sa.Column('source_key', sa.String(80), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('source_refs', jsonb, nullable=False, server_default=empty_object),
        sa.Column('title', sa.String(150), nullable=False),
        sa.Column('status', sa.String(15), nullable=False, server_default='GENERATED'),
        sa.Column('draft', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('content', jsonb, nullable=False),
        sa.Column('content_hash', sa.CHAR(64), nullable=False),
        sa.Column('storage_path', sa.String(300), nullable=True),
        sa.Column('sha256', sa.CHAR(64), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False),
        sa.Column('mime', sa.String(80), nullable=False, server_default='application/pdf'),
        sa.Column('file_name', sa.String(150), nullable=False),
        sa.Column('brand_version', sa.String(64), nullable=True),
        sa.Column('brand_profile_id', sa.Integer(), nullable=True),
        sa.Column('entity_brand_profile_id', sa.Integer(), nullable=True),
        sa.Column('template_version', sa.String(60), nullable=False),
        sa.Column('template_policy_version_id', sa.Integer(), nullable=True),
        sa.Column('renderer', sa.String(60), nullable=False),
        sa.Column('locale', sa.String(35), nullable=False),
        sa.Column('languages', jsonb, nullable=False, server_default=empty_array),
        sa.Column('labels', jsonb, nullable=False, server_default=empty_array),
        sa.Column('publish_trigger', sa.String(25), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('published_by_user_id', sa.Integer(), nullable=True),
        sa.Column('publish_reason', sa.Text(), nullable=True),
        sa.Column('first_viewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('supersedes_document_id', sa.Integer(), nullable=True),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reversed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reversal_document_id', sa.Integer(), nullable=True),
        sa.Column('rerendered', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('rerendered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rerender_mismatch', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('retention_until', sa.Date(), nullable=True),
        sa.Column('issued_by_user_id', sa.Integer(), nullable=True),
        _row_version(),
        _uq('documents'),
        sa.UniqueConstraint('public_id', name='uq_documents_public_id'),
        sa.UniqueConstraint('company_id', 'doc_type', 'source_key', 'version', name='uq_documents_source_version'),
        _fk('documents', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('documents', 'pay_group_id', 'pay_groups', 'pay_group'),
        _fk('documents', 'employee_id', 'employees', 'employee'),
        _fk('documents', 'run_id', 'payroll_runs', 'run'),
        _fk('documents', 'run_employee_id', 'payroll_run_employees', 'run_employee'),
        _fk('documents', 'supersedes_document_id', 'documents', 'supersedes'),
        _fk('documents', 'reversal_document_id', 'documents', 'reversal'),
        _fk('documents', 'published_by_user_id', 'users', 'published_by'),
        _fk('documents', 'issued_by_user_id', 'users', 'issued_by'),
        _in('documents', 'doc_type', DOC_TYPES),
        _in('documents', 'status', DOCUMENT_STATUSES),
        _in('documents', 'subject_type', SUBJECT_TYPES),
        _in('documents', 'publish_trigger', PUBLISH_TRIGGERS, nullable=True),
        sa.CheckConstraint("public_id ~ '^[A-Za-z0-9_-]{20,32}$'", name='ck_documents_public_id'),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='ck_documents_sha256'),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name='ck_documents_content_hash'),
        sa.CheckConstraint("bytes > 0", name='ck_documents_bytes'),
        sa.CheckConstraint("version >= 1", name='ck_documents_version'),
        sa.CheckConstraint("row_version >= 1", name='ck_documents_row_version'),
        sa.CheckConstraint("jsonb_typeof(content) = 'object'", name='ck_documents_content'),
        sa.CheckConstraint("jsonb_typeof(labels) = 'array'", name='ck_documents_labels'),
        sa.CheckConstraint("jsonb_typeof(languages) = 'array'", name='ck_documents_languages'),
        sa.CheckConstraint("(status = 'GENERATED') OR published_at IS NOT NULL", name='ck_documents_published'),
        sa.CheckConstraint("(status <> 'REVERSED') OR reversed_at IS NOT NULL", name='ck_documents_reversed'),
        sa.CheckConstraint("supersedes_document_id IS NULL OR supersedes_document_id <> id",
                           name='ck_documents_not_self'),
        sa.CheckConstraint("period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
                           name='ck_documents_period'),
    )
    op.create_index('ix_documents_company_id', 'documents', ['company_id'])
    op.create_index('ix_documents_legal_entity_id', 'documents', ['legal_entity_id'])
    op.create_index('ix_documents_employee_id', 'documents', ['employee_id'])
    op.create_index('ix_documents_run_id', 'documents', ['run_id'])
    op.create_index('ix_documents_run_employee_id', 'documents', ['run_employee_id'])
    op.create_index('ix_documents_filters', 'documents', ['company_id', 'doc_type', 'status', 'period_end'])
    op.create_index('uq_documents_number', 'documents', ['company_id', 'legal_entity_id', 'doc_type', 'number'],
                    unique=True, postgresql_where=sa.text('number IS NOT NULL'))

    op.create_table(
        'document_sequences', *_base(),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('doc_type', sa.String(30), nullable=False),
        sa.Column('scope_key', sa.String(20), nullable=False),
        sa.Column('pattern', sa.String(64), nullable=False),
        sa.Column('next_value', sa.BigInteger(), nullable=False, server_default='1'),
        sa.Column('gapless', sa.Boolean(), nullable=False, server_default=sa.false()),
        _row_version(),
        _uq('document_sequences'),
        sa.UniqueConstraint('company_id', 'legal_entity_id', 'doc_type', 'scope_key',
                            name='uq_document_sequences_scope'),
        _fk('document_sequences', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _in('document_sequences', 'doc_type', DOC_TYPES),
        sa.CheckConstraint("next_value >= 1", name='ck_document_sequences_next_value'),
        sa.CheckConstraint("row_version >= 1", name='ck_document_sequences_row_version'),
    )
    op.create_index('ix_document_sequences_company_id', 'document_sequences', ['company_id'])

    op.create_table(
        'export_jobs', *_base(),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('params', jsonb, nullable=False, server_default=empty_object),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='QUEUED'),
        sa.Column('item_count', sa.Integer(), nullable=True),
        sa.Column('storage_path', sa.String(300), nullable=True),
        sa.Column('sha256', sa.CHAR(64), nullable=True),
        sa.Column('bytes', sa.BigInteger(), nullable=True),
        sa.Column('mime', sa.String(80), nullable=True),
        sa.Column('file_name', sa.String(150), nullable=True),
        sa.Column('watermark', sa.String(200), nullable=True),
        sa.Column('error', sa.String(500), nullable=True),
        sa.Column('ready_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        _row_version(),
        _uq('export_jobs'),
        _fk('export_jobs', 'requested_by_user_id', 'users', 'requested_by'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name='fk_export_jobs_job', ondelete='SET NULL'),
        _in('export_jobs', 'kind', EXPORT_KINDS),
        _in('export_jobs', 'status', EXPORT_STATUSES),
        sa.CheckConstraint("status <> 'READY' OR (storage_path IS NOT NULL AND sha256 IS NOT NULL "
                           "AND expires_at IS NOT NULL)", name='ck_export_jobs_ready'),
        sa.CheckConstraint("jsonb_typeof(params) = 'object'", name='ck_export_jobs_params'),
    )
    op.create_index('ix_export_jobs_company_id', 'export_jobs', ['company_id'])
    op.create_index('ix_export_jobs_requested_by_user_id', 'export_jobs', ['requested_by_user_id'])

    op.create_table(
        'document_access_log', *_base(),
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('export_job_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(12), nullable=False),
        sa.Column('ip', sa.String(64), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('details', jsonb, nullable=False, server_default=empty_object),
        _uq('document_access_log'),
        _fk('document_access_log', 'document_id', 'documents', 'document'),
        _fk('document_access_log', 'export_job_id', 'export_jobs', 'export_job'),
        _fk('document_access_log', 'user_id', 'users', 'user'),
        _in('document_access_log', 'action', ACCESS_ACTIONS),
        sa.CheckConstraint("document_id IS NOT NULL OR export_job_id IS NOT NULL",
                           name='ck_document_access_log_target'),
    )
    op.create_index('ix_document_access_log_company_id', 'document_access_log', ['company_id'])
    op.create_index('ix_document_access_log_document_id', 'document_access_log', ['document_id'])
    op.create_index('ix_document_access_log_export_job_id', 'document_access_log', ['export_job_id'])

    op.create_table(
        'document_generation_tasks', *_base(),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('run_employee_id', sa.Integer(), nullable=False),
        sa.Column('doc_type', sa.String(30), nullable=False),
        sa.Column('document_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('status', sa.String(10), nullable=False, server_default='QUEUED'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('retries', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.String(500), nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('document_id', sa.Integer(), nullable=True),
        _row_version(),
        _uq('document_generation_tasks'),
        sa.UniqueConstraint('company_id', 'run_employee_id', 'doc_type', 'document_version',
                            name='uq_document_generation_tasks_identity'),
        _fk('document_generation_tasks', 'run_id', 'payroll_runs', 'run'),
        _fk('document_generation_tasks', 'run_employee_id', 'payroll_run_employees', 'run_employee'),
        _fk('document_generation_tasks', 'document_id', 'documents', 'document'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name='fk_document_generation_tasks_job',
                                ondelete='SET NULL'),
        _in('document_generation_tasks', 'doc_type', DOC_TYPES),
        _in('document_generation_tasks', 'status', TASK_STATUSES),
        sa.CheckConstraint("(status = 'GENERATED') = (document_id IS NOT NULL)",
                           name='ck_document_generation_tasks_doc'),
        sa.CheckConstraint("document_version >= 1 AND attempts >= 0 AND retries >= 0",
                           name='ck_document_generation_tasks_counts'),
    )
    op.create_index('ix_document_generation_tasks_company_id', 'document_generation_tasks', ['company_id'])
    op.create_index('ix_document_generation_tasks_run_id', 'document_generation_tasks', ['run_id'])

    op.create_table(
        'document_notifications', *_base(),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('channel', sa.String(10), nullable=False),
        sa.Column('recipient_user_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='PENDING'),
        sa.Column('notification_id', sa.Integer(), nullable=True),
        sa.Column('detail', sa.String(300), nullable=True),
        sa.Column('attached', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        _uq('document_notifications'),
        sa.UniqueConstraint('company_id', 'document_id', 'channel', name='uq_document_notifications_once'),
        _fk('document_notifications', 'document_id', 'documents', 'document'),
        _fk('document_notifications', 'recipient_user_id', 'users', 'recipient'),
        _in('document_notifications', 'channel', NOTIFICATION_CHANNELS),
        _in('document_notifications', 'status', NOTIFICATION_STATUSES),
    )
    op.create_index('ix_document_notifications_company_id', 'document_notifications', ['company_id'])

    op.create_table(
        'invoices', *_base(),
        sa.Column('kind', sa.String(12), nullable=False, server_default='INVOICE'),
        sa.Column('mode', sa.String(12), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('payee_employee_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='DRAFT'),
        sa.Column('number', sa.String(80), nullable=True),
        sa.Column('external_number', sa.String(80), nullable=True),
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('credit_of_invoice_id', sa.Integer(), nullable=True),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('seller', jsonb, nullable=False, server_default=empty_object),
        sa.Column('buyer', jsonb, nullable=False, server_default=empty_object),
        sa.Column('lines', jsonb, nullable=False, server_default=empty_array),
        sa.Column('taxes', jsonb, nullable=False, server_default=empty_array),
        sa.Column('subtotal', sa.Numeric(19, 4), nullable=False),
        sa.Column('tax_total', sa.Numeric(19, 4), nullable=False, server_default='0'),
        sa.Column('total', sa.Numeric(19, 4), nullable=False),
        sa.Column('issue_date', sa.Date(), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('payment_terms', sa.String(200), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('issued_by_user_id', sa.Integer(), nullable=True),
        sa.Column('issued_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('credited_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        _row_version(),
        _uq('invoices'),
        _fk('invoices', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('invoices', 'payee_employee_id', 'employees', 'payee'),
        _fk('invoices', 'document_id', 'documents', 'document'),
        _fk('invoices', 'credit_of_invoice_id', 'invoices', 'credit_of'),
        _fk('invoices', 'created_by_user_id', 'users', 'created_by'),
        _fk('invoices', 'issued_by_user_id', 'users', 'issued_by'),
        _fk('invoices', 'approved_by_user_id', 'users', 'approved_by'),
        _in('invoices', 'kind', INVOICE_KINDS),
        _in('invoices', 'mode', INVOICE_MODES),
        _in('invoices', 'status', INVOICE_STATUSES),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name='ck_invoices_currency'),
        sa.CheckConstraint("(kind = 'CREDIT_NOTE') = (credit_of_invoice_id IS NOT NULL)",
                           name='ck_invoices_credit_of'),
        sa.CheckConstraint("status = 'DRAFT' OR number IS NOT NULL", name='ck_invoices_numbered'),
        sa.CheckConstraint("subtotal >= 0 AND tax_total >= 0 AND total >= 0", name='ck_invoices_amounts'),
        sa.CheckConstraint("approved_by_user_id IS NULL OR approved_by_user_id <> issued_by_user_id",
                           name='ck_invoices_maker_checker'),
        sa.CheckConstraint("jsonb_typeof(lines) = 'array' AND jsonb_typeof(taxes) = 'array'",
                           name='ck_invoices_shape'),
        sa.CheckConstraint("row_version >= 1", name='ck_invoices_row_version'),
    )
    op.create_index('ix_invoices_company_id', 'invoices', ['company_id'])
    op.create_index('ix_invoices_legal_entity_id', 'invoices', ['legal_entity_id'])
    op.create_index('ix_invoices_payee_employee_id', 'invoices', ['payee_employee_id'])
    op.create_index('uq_invoices_number', 'invoices', ['company_id', 'legal_entity_id', 'kind', 'mode', 'number'],
                    unique=True, postgresql_where=sa.text('number IS NOT NULL'))

    op.create_table(
        'invoice_sources', *_base(),
        sa.Column('invoice_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(10), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        _uq('invoice_sources'),
        _fk('invoice_sources', 'invoice_id', 'invoices', 'invoice', ondelete='CASCADE'),
        _in('invoice_sources', 'source_type', INVOICE_SOURCE_TYPES),
    )
    op.create_index('ix_invoice_sources_company_id', 'invoice_sources', ['company_id'])
    op.create_index('ix_invoice_sources_invoice_id', 'invoice_sources', ['invoice_id'])
    op.create_index('uq_invoice_sources_live', 'invoice_sources', ['company_id', 'source_type', 'source_id'],
                    unique=True, postgresql_where=sa.text('active'))

    op.create_table(
        'self_billing_agreements', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('reference', sa.String(100), nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        _uq('self_billing_agreements'),
        _fk('self_billing_agreements', 'employee_id', 'employees', 'employee'),
        _fk('self_billing_agreements', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('self_billing_agreements', 'created_by_user_id', 'users', 'created_by'),
        _in('self_billing_agreements', 'status', AGREEMENT_STATUSES),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                           name='ck_self_billing_agreements_window'),
    )
    op.create_index('ix_self_billing_agreements_company_id', 'self_billing_agreements', ['company_id'])
    op.create_index('ix_self_billing_agreements_employee_id', 'self_billing_agreements', ['employee_id'])

    op.create_table(
        'document_requests', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('doc_type', sa.String(30), nullable=False),
        sa.Column('include_amounts', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('purpose', sa.String(300), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='PENDING'),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=False),
        sa.Column('decided_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decision_note', sa.Text(), nullable=True),
        sa.Column('document_id', sa.Integer(), nullable=True),
        _row_version(),
        _uq('document_requests'),
        _fk('document_requests', 'employee_id', 'employees', 'employee'),
        _fk('document_requests', 'requested_by_user_id', 'users', 'requested_by'),
        _fk('document_requests', 'decided_by_user_id', 'users', 'decided_by'),
        _fk('document_requests', 'document_id', 'documents', 'document'),
        _in('document_requests', 'doc_type', REQUEST_DOC_TYPES),
        _in('document_requests', 'status', REQUEST_STATUSES),
        sa.CheckConstraint("(status = 'ISSUED') = (document_id IS NOT NULL)", name='ck_document_requests_issued'),
        sa.CheckConstraint("decided_by_user_id IS NULL OR decided_by_user_id <> requested_by_user_id",
                           name='ck_document_requests_maker_checker'),
    )
    op.create_index('ix_document_requests_company_id', 'document_requests', ['company_id'])
    op.create_index('ix_document_requests_employee_id', 'document_requests', ['employee_id'])

    op.create_table(
        'payslip_pdf_secrets', *_base(),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('secret_enc', sa.Text(), nullable=False),
        sa.Column('set_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        _row_version(),
        _uq('payslip_pdf_secrets'),
        sa.UniqueConstraint('company_id', 'employee_id', name='uq_payslip_pdf_secrets_employee'),
        _fk('payslip_pdf_secrets', 'employee_id', 'employees', 'employee', ondelete='CASCADE'),
    )
    op.create_index('ix_payslip_pdf_secrets_company_id', 'payslip_pdf_secrets', ['company_id'])

    op.execute(DOCUMENTS_IMMUTABLE)
    op.execute(ACCESS_LOG_APPEND_ONLY)
    op.execute("CREATE TRIGGER trg_documents_immutable BEFORE UPDATE OR DELETE ON documents "
               "FOR EACH ROW EXECUTE FUNCTION documents_immutable()")
    op.execute("CREATE TRIGGER trg_document_access_log_append_only BEFORE UPDATE OR DELETE ON document_access_log "
               "FOR EACH ROW EXECUTE FUNCTION document_access_log_append_only()")
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
            privileges = "SELECT, INSERT" if table == "document_access_log" else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_document_access_log_append_only ON document_access_log")
    op.execute("DROP TRIGGER IF EXISTS trg_documents_immutable ON documents")
    for function in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    for table in ("payslip_pdf_secrets", "document_requests", "self_billing_agreements", "invoice_sources",
                  "invoices", "document_notifications", "document_generation_tasks", "document_access_log",
                  "export_jobs", "document_sequences", "documents"):
        op.drop_table(table)
