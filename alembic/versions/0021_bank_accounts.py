"""Bank accounts: encrypted payee accounts, change requests, verifications, funding accounts.

Additive (3.7 §2-§6, §12; 3.9 §3.8): tenant tables bank_accounts, bank_account_change_requests,
bank_verifications and funding_accounts, each with row-level security, grants and composite
same-company foreign keys.

* bank_accounts keeps account numbers and IBANs only as envelope ciphertexts (TEXT), a display
  ``last4`` and a keyed ``fingerprint`` (INDEX (company_id, fingerprint)). Rows are never deleted and
  the identifying details (payee, holder, country, currency, routing, last4, fingerprint, allocation)
  never change: a trigger refuses both. Ciphertext columns stay updatable for key rotation.
* bank_account_change_requests: at most one open change (SECURITY_REVIEW or PENDING_APPROVAL) per
  payee (partial unique index).
* bank_verifications is append-only (trigger, and the application role gets SELECT and INSERT only).
* funding_accounts: one ACTIVE account per entity and fingerprint.

Downgrade drops the tables, triggers and functions.

Revision ID: 0021_bank_accounts
Revises: 0020_runs_extensions
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0021_bank_accounts'
down_revision: Union[str, None] = '0020_runs_extensions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("bank_accounts", "bank_account_change_requests", "bank_verifications", "funding_accounts")
PRIVILEGES = {
    "bank_accounts": "SELECT, INSERT, UPDATE",
    "bank_account_change_requests": "SELECT, INSERT, UPDATE",
    "bank_verifications": "SELECT, INSERT",
    "funding_accounts": "SELECT, INSERT, UPDATE",
}
PAYEE_TYPES = ("EMPLOYEE", "CONTRACTOR", "NOMINEE")
BANK_ACCOUNT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "ACTIVE", "INACTIVE")
VERIFICATION_STATUSES = ("NOT_SUBMITTED", "PENDING", "VERIFIED", "FAILED", "EXPIRED", "REQUIRES_REVIEW")
VERIFICATION_METHODS = ("FORMAT", "DOCUMENT_REVIEW", "PENNY_DROP", "PROVIDER_NAME_MATCH")
VERIFICATION_EVENT_METHODS = VERIFICATION_METHODS + ("PAYMENT_RETURN", "MANUAL_REVIEW")
VERIFICATION_RESULTS = ("PASSED", "FAILED", "EVIDENCE_SUBMITTED", "FLAGGED", "CLEARED", "RESUBMITTED")
ACCOUNT_TYPES = ("CHECKING", "CURRENT", "SAVINGS")
HOLD_REASONS = ("DUPLICATE_FINGERPRINT",)
CHANGE_CHANNELS = ("SELF", "HR", "BUREAU")
CHANGE_KINDS = ("ADD", "REPLACE")
CHANGE_STATUSES = ("SECURITY_REVIEW", "PENDING_APPROVAL", "APPLIED", "REJECTED", "CANCELLED")
OPEN_CHANGE_STATUSES = ("SECURITY_REVIEW", "PENDING_APPROVAL")
FUNDING_PURPOSES = ("PAYROLL", "STATUTORY", "CONTRACTORS", "REIMBURSEMENTS")
FUNDING_STATUSES = ("ACTIVE", "INACTIVE")

BANK_ACCOUNT_GUARD = """
CREATE OR REPLACE FUNCTION bank_accounts_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bank_accounts are never deleted: deactivate the account instead'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.company_id IS DISTINCT FROM OLD.company_id
       OR NEW.payee_type IS DISTINCT FROM OLD.payee_type
       OR NEW.payee_id IS DISTINCT FROM OLD.payee_id
       OR NEW.employee_id IS DISTINCT FROM OLD.employee_id
       OR NEW.holder_name IS DISTINCT FROM OLD.holder_name
       OR NEW.country IS DISTINCT FROM OLD.country
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.routing IS DISTINCT FROM OLD.routing
       OR NEW.last4 IS DISTINCT FROM OLD.last4
       OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
       OR NEW.allocation IS DISTINCT FROM OLD.allocation
       OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id
       OR (NEW.account_number_enc IS NULL) <> (OLD.account_number_enc IS NULL)
       OR (NEW.iban_enc IS NULL) <> (OLD.iban_enc IS NULL) THEN
        RAISE EXCEPTION 'bank account details are immutable: submit a new account instead'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

VERIFICATIONS_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION bank_verifications_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'bank_verifications is append-only: record a new verification event instead'
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


def _sql_list(values):
    """``('A', 'B')`` for a CHECK constraint; never a Python tuple repr (a 1-tuple renders ``('A',)``)."""
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def _in(table, column, values):
    return sa.CheckConstraint(f"{column} IN {_sql_list(values)}", name=f'ck_{table}_{column}')


def _uq(table):
    return sa.UniqueConstraint('company_id', 'id', name=f'uq_{table}_company_id_id')


def upgrade() -> None:
    op.create_table(
        'bank_accounts',
        *_base(),
        sa.Column('payee_type', sa.String(12), nullable=False, server_default='EMPLOYEE'),
        sa.Column('payee_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('holder_name', sa.String(140), nullable=False),
        sa.Column('country', sa.CHAR(2), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('account_number_enc', sa.Text(), nullable=True),
        sa.Column('iban_enc', sa.Text(), nullable=True),
        sa.Column('last4', sa.String(4), nullable=False, server_default=''),
        sa.Column('routing', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('bank_name', sa.String(140), nullable=True),
        sa.Column('branch_name', sa.String(140), nullable=True),
        sa.Column('bank_address', sa.String(300), nullable=True),
        sa.Column('account_type', sa.String(10), nullable=True),
        sa.Column('allocation', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('status', sa.String(20), nullable=False, server_default='DRAFT'),
        sa.Column('verification_status', sa.String(20), nullable=False, server_default='NOT_SUBMITTED'),
        sa.Column('verification_method', sa.String(30), nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
        sa.Column('usable_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('effective_from', sa.Date(), nullable=True),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('fingerprint', sa.String(80), nullable=False),
        sa.Column('fingerprint_version', sa.SmallInteger(), nullable=False, server_default='1'),
        sa.Column('risk_flags', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('payment_hold', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('hold_reason', sa.String(40), nullable=True),
        sa.Column('replaces_account_id', sa.Integer(), nullable=True),
        sa.Column('replaced_by_account_id', sa.Integer(), nullable=True),
        sa.Column('first_paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_failed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_reason', sa.String(200), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        _uq('bank_accounts'),
        _fk('bank_accounts', 'employee_id', 'employees', 'employee'),
        _fk('bank_accounts', 'replaces_account_id', 'bank_accounts', 'replaces'),
        _fk('bank_accounts', 'replaced_by_account_id', 'bank_accounts', 'replaced_by'),
        _fk('bank_accounts', 'created_by_user_id', 'users', 'created_by'),
        _fk('bank_accounts', 'approved_by_user_id', 'users', 'approved_by'),
        _in('bank_accounts', 'payee_type', PAYEE_TYPES),
        _in('bank_accounts', 'status', BANK_ACCOUNT_STATUSES),
        _in('bank_accounts', 'verification_status', VERIFICATION_STATUSES),
        sa.CheckConstraint(f"verification_method IS NULL OR verification_method IN {_sql_list(VERIFICATION_METHODS)}",
                           name='ck_bank_accounts_verification_method'),
        sa.CheckConstraint(f"account_type IS NULL OR account_type IN {_sql_list(ACCOUNT_TYPES)}",
                           name='ck_bank_accounts_account_type'),
        sa.CheckConstraint(f"hold_reason IS NULL OR hold_reason IN {_sql_list(HOLD_REASONS)}", name='ck_bank_accounts_hold_reason'),
        sa.CheckConstraint("(payee_type = 'EMPLOYEE') = (employee_id IS NOT NULL)", name='ck_bank_accounts_employee'),
        sa.CheckConstraint("employee_id IS NULL OR employee_id = payee_id", name='ck_bank_accounts_payee'),
        sa.CheckConstraint("account_number_enc IS NOT NULL OR iban_enc IS NOT NULL", name='ck_bank_accounts_number'),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name='ck_bank_accounts_country'),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name='ck_bank_accounts_currency'),
        sa.CheckConstraint("last4 ~ '^[A-Z0-9]{0,4}$'", name='ck_bank_accounts_last4'),
        sa.CheckConstraint("jsonb_typeof(routing) = 'object'", name='ck_bank_accounts_routing'),
        sa.CheckConstraint("jsonb_typeof(allocation) = 'object'", name='ck_bank_accounts_allocation'),
        sa.CheckConstraint("jsonb_typeof(risk_flags) = 'array'", name='ck_bank_accounts_risk_flags'),
        sa.CheckConstraint("payment_hold = (hold_reason IS NOT NULL)", name='ck_bank_accounts_hold'),
        sa.CheckConstraint("status NOT IN ('ACTIVE', 'INACTIVE') OR (approved_at IS NOT NULL AND usable_from IS NOT NULL)",
                           name='ck_bank_accounts_approved'),
        sa.CheckConstraint("(status = 'INACTIVE') = (ended_at IS NOT NULL)", name='ck_bank_accounts_ended'),
        sa.CheckConstraint("effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
                           name='ck_bank_accounts_effective'),
        sa.CheckConstraint("row_version >= 1", name='ck_bank_accounts_row_version'),
    )
    op.create_index('ix_bank_accounts_company_id', 'bank_accounts', ['company_id'])
    op.create_index('ix_bank_accounts_employee_id', 'bank_accounts', ['employee_id'])
    op.create_index('ix_bank_accounts_fingerprint', 'bank_accounts', ['company_id', 'fingerprint'])
    op.create_index('ix_bank_accounts_payee', 'bank_accounts', ['company_id', 'payee_type', 'payee_id'])

    op.create_table(
        'bank_account_change_requests',
        *_base(),
        sa.Column('bank_account_id', sa.Integer(), nullable=False),
        sa.Column('payee_type', sa.String(12), nullable=False, server_default='EMPLOYEE'),
        sa.Column('payee_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(10), nullable=False, server_default='ADD'),
        sa.Column('replaces_account_id', sa.Integer(), nullable=True),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=True),
        sa.Column('channel', sa.String(10), nullable=False),
        sa.Column('step_up_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('step_up_method', sa.String(20), nullable=True),
        sa.Column('ip_address', sa.String(64), nullable=True),
        sa.Column('user_agent', sa.String(255), nullable=True),
        sa.Column('risk_flags', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('contact_emails', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('approval_request_id', sa.Integer(), nullable=True),
        sa.Column('security_reviewed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('security_reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('security_review_reason', sa.Text(), nullable=True),
        sa.Column('decided_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decision_reason', sa.Text(), nullable=True),
        sa.Column('cooling_off_waived_reason', sa.Text(), nullable=True),
        sa.Column('cooling_off_waived_by_user_id', sa.Integer(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        _uq('bank_account_change_requests'),
        _fk('bank_account_change_requests', 'bank_account_id', 'bank_accounts', 'account'),
        _fk('bank_account_change_requests', 'replaces_account_id', 'bank_accounts', 'replaces'),
        _fk('bank_account_change_requests', 'employee_id', 'employees', 'employee'),
        _fk('bank_account_change_requests', 'requested_by_user_id', 'users', 'requested_by'),
        _fk('bank_account_change_requests', 'decided_by_user_id', 'users', 'decided_by'),
        _fk('bank_account_change_requests', 'security_reviewed_by_user_id', 'users', 'security_reviewed_by'),
        _fk('bank_account_change_requests', 'cooling_off_waived_by_user_id', 'users', 'waived_by'),
        _fk('bank_account_change_requests', 'approval_request_id', 'approval_requests', 'approval_request'),
        _in('bank_account_change_requests', 'payee_type', PAYEE_TYPES),
        _in('bank_account_change_requests', 'kind', CHANGE_KINDS),
        _in('bank_account_change_requests', 'channel', CHANGE_CHANNELS),
        _in('bank_account_change_requests', 'status', CHANGE_STATUSES),
        sa.CheckConstraint("(payee_type = 'EMPLOYEE') = (employee_id IS NOT NULL)",
                           name='ck_bank_account_change_requests_employee'),
        sa.CheckConstraint("(kind = 'REPLACE') = (replaces_account_id IS NOT NULL)",
                           name='ck_bank_account_change_requests_replaces'),
        sa.CheckConstraint("jsonb_typeof(risk_flags) = 'array'", name='ck_bank_account_change_requests_risk_flags'),
        sa.CheckConstraint("jsonb_typeof(contact_emails) = 'array'", name='ck_bank_account_change_requests_contacts'),
        sa.CheckConstraint("row_version >= 1", name='ck_bank_account_change_requests_row_version'),
    )
    op.create_index('ix_bank_account_change_requests_company_id', 'bank_account_change_requests', ['company_id'])
    op.create_index('ix_bank_account_change_requests_bank_account_id', 'bank_account_change_requests',
                    ['bank_account_id'])
    op.create_index('ix_bank_account_change_requests_employee_id', 'bank_account_change_requests', ['employee_id'])
    op.create_index('ix_bank_account_change_requests_payee', 'bank_account_change_requests',
                    ['company_id', 'payee_type', 'payee_id', 'created_at'])
    op.create_index('uq_bank_account_change_requests_open', 'bank_account_change_requests',
                    ['company_id', 'payee_type', 'payee_id'], unique=True,
                    postgresql_where=sa.text(f"status IN {_sql_list(OPEN_CHANGE_STATUSES)}"))

    op.create_table(
        'bank_verifications',
        *_base(),
        sa.Column('bank_account_id', sa.Integer(), nullable=False),
        sa.Column('method', sa.String(30), nullable=False),
        sa.Column('result', sa.String(20), nullable=False),
        sa.Column('provider', sa.String(20), nullable=True),
        sa.Column('provider_ref', sa.String(80), nullable=True),
        sa.Column('name_match_score', sa.Numeric(5, 4), nullable=True),
        sa.Column('evidence_of_id', sa.Integer(), nullable=True),
        sa.Column('evidence_storage_path', sa.String(255), nullable=True),
        sa.Column('evidence_sha256', sa.CHAR(64), nullable=True),
        sa.Column('evidence_mime', sa.String(50), nullable=True),
        sa.Column('evidence_bytes', sa.Integer(), nullable=True),
        sa.Column('evidence_file_name', sa.String(200), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('details', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('performed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        _uq('bank_verifications'),
        _fk('bank_verifications', 'bank_account_id', 'bank_accounts', 'account'),
        _fk('bank_verifications', 'evidence_of_id', 'bank_verifications', 'evidence_of'),
        _fk('bank_verifications', 'performed_by_user_id', 'users', 'performed_by'),
        _in('bank_verifications', 'method', VERIFICATION_EVENT_METHODS),
        _in('bank_verifications', 'result', VERIFICATION_RESULTS),
        sa.CheckConstraint("(result = 'EVIDENCE_SUBMITTED') = (evidence_storage_path IS NOT NULL)",
                           name='ck_bank_verifications_evidence'),
        sa.CheckConstraint("evidence_sha256 IS NULL OR evidence_sha256 ~ '^[0-9a-f]{64}$'",
                           name='ck_bank_verifications_sha256'),
        sa.CheckConstraint("name_match_score IS NULL OR (name_match_score >= 0 AND name_match_score <= 1)",
                           name='ck_bank_verifications_score'),
        sa.CheckConstraint("jsonb_typeof(details) = 'object'", name='ck_bank_verifications_details'),
    )
    op.create_index('ix_bank_verifications_company_id', 'bank_verifications', ['company_id'])
    op.create_index('ix_bank_verifications_bank_account_id', 'bank_verifications', ['bank_account_id'])

    op.create_table(
        'funding_accounts',
        *_base(),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('holder_name', sa.String(140), nullable=False),
        sa.Column('country', sa.CHAR(2), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('account_number_enc', sa.Text(), nullable=True),
        sa.Column('iban_enc', sa.Text(), nullable=True),
        sa.Column('last4', sa.String(4), nullable=False, server_default=''),
        sa.Column('routing', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('bank_name', sa.String(140), nullable=True),
        sa.Column('bank_address', sa.String(300), nullable=True),
        sa.Column('account_type', sa.String(10), nullable=True),
        sa.Column('fingerprint', sa.String(80), nullable=False),
        sa.Column('purpose', sa.String(20), nullable=False, server_default='PAYROLL'),
        sa.Column('signatory_user_ids', postgresql.ARRAY(sa.Integer()), nullable=False,
                  server_default=sa.text("'{}'::integer[]")),
        sa.Column('formats', postgresql.ARRAY(sa.String(30)), nullable=False,
                  server_default=sa.text("'{}'::varchar[]")),
        sa.Column('provider_profile', sa.String(40), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        _uq('funding_accounts'),
        _fk('funding_accounts', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('funding_accounts', 'created_by_user_id', 'users', 'created_by'),
        _in('funding_accounts', 'purpose', FUNDING_PURPOSES),
        _in('funding_accounts', 'status', FUNDING_STATUSES),
        sa.CheckConstraint(f"account_type IS NULL OR account_type IN {_sql_list(ACCOUNT_TYPES)}",
                           name='ck_funding_accounts_account_type'),
        sa.CheckConstraint("account_number_enc IS NOT NULL OR iban_enc IS NOT NULL", name='ck_funding_accounts_number'),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name='ck_funding_accounts_country'),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name='ck_funding_accounts_currency'),
        sa.CheckConstraint("last4 ~ '^[A-Z0-9]{0,4}$'", name='ck_funding_accounts_last4'),
        sa.CheckConstraint("jsonb_typeof(routing) = 'object'", name='ck_funding_accounts_routing'),
        sa.CheckConstraint("cardinality(signatory_user_ids) >= 1", name='ck_funding_accounts_signatories'),
        sa.CheckConstraint("row_version >= 1", name='ck_funding_accounts_row_version'),
    )
    op.create_index('ix_funding_accounts_company_id', 'funding_accounts', ['company_id'])
    op.create_index('ix_funding_accounts_legal_entity_id', 'funding_accounts', ['legal_entity_id'])
    op.create_index('uq_funding_accounts_active_fingerprint', 'funding_accounts',
                    ['company_id', 'legal_entity_id', 'fingerprint'], unique=True,
                    postgresql_where=sa.text("status = 'ACTIVE'"))

    op.execute(BANK_ACCOUNT_GUARD)
    op.execute(
        "CREATE TRIGGER trg_bank_accounts_guard BEFORE UPDATE OR DELETE ON bank_accounts "
        "FOR EACH ROW EXECUTE FUNCTION bank_accounts_guard()"
    )
    op.execute(VERIFICATIONS_APPEND_ONLY)
    op.execute(
        "CREATE TRIGGER trg_bank_verifications_append_only BEFORE UPDATE OR DELETE ON bank_verifications "
        "FOR EACH ROW EXECUTE FUNCTION bank_verifications_append_only()"
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
            op.execute(f"GRANT {PRIVILEGES[table]} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_bank_verifications_append_only ON bank_verifications")
    op.execute("DROP FUNCTION IF EXISTS bank_verifications_append_only()")
    op.execute("DROP TRIGGER IF EXISTS trg_bank_accounts_guard ON bank_accounts")
    op.execute("DROP FUNCTION IF EXISTS bank_accounts_guard()")
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
