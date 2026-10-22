"""Statutory reports lifecycle, accounting journals and GL account mappings (reports support).

Additive (3.2 §8, 3.1 §5.18, 3.3 §2.2, 3.9 §3.10); every table is a tenant table with row-level
security, grants and composite same-company foreign keys (app.models.reports_compliance documents each).

Database rules that services cannot be trusted to keep alone:

* uq_statutory_reports_live: one live (non-AMENDED) report per legal entity, report code and period;
  an amendment links its original (uq_statutory_reports_amends: one amendment per original, EC-STA-12).
* CHECKs: SUBMITTED and later need a filing reference (EC-STA-11), ACCEPTED an acknowledgement,
  REJECTED the authority's response, READY_TO_FILE an exported file hash; a fixture report carries
  its watermark (decision I-07).
* statutory_reports_guard: the identity of a report (entity, pack, code, period) never changes, its
  content and filing data are frozen once filed, and only a DRAFT report may be deleted.
* uq_accounting_journals_run: one journal per run (never double-posted); accounting_journals_guard
  freezes a POSTED journal and refuses deleting it; accounting_journal_lines are append-only.

Downgrade drops the tables, triggers and functions.

Revision ID: 0024_reports_compliance
Revises: 0023_documents
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0024_reports_compliance'
down_revision: Union[str, None] = '0023_documents'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("statutory_reports", "accounting_journals", "accounting_journal_lines", "gl_account_mappings")
STATUTORY_REPORT_STATUSES = (
    "DRAFT", "CALCULATED", "REVIEW_REQUIRED", "VERIFIED", "READY_TO_FILE", "SUBMITTED", "ACCEPTED", "REJECTED",
    "AMENDED",
)
FILED_STATUSES = ("SUBMITTED", "ACCEPTED", "REJECTED", "AMENDED")
REPORT_FREQUENCIES = ("MONTHLY", "QUARTERLY", "ANNUAL")
JOURNAL_STATUSES = ("POSTED", "FAILED")
JOURNAL_TRIGGERS = ("FINALIZE", "PAYMENT_SETTLED", "MANUAL")
JOURNAL_FORMATS = ("GENERIC_CSV",)
JOURNAL_SIDES = ("DEBIT", "CREDIT")
FUNCTIONS = ("statutory_reports_guard", "accounting_journals_guard", "accounting_journal_lines_append_only")

STATUTORY_REPORTS_GUARD = f"""
CREATE OR REPLACE FUNCTION statutory_reports_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION 'Only a DRAFT statutory report can be deleted' USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.legal_entity_id IS DISTINCT FROM OLD.legal_entity_id
            OR NEW.pack_code IS DISTINCT FROM OLD.pack_code
            OR NEW.report_code IS DISTINCT FROM OLD.report_code
            OR NEW.period_start IS DISTINCT FROM OLD.period_start
            OR NEW.period_end IS DISTINCT FROM OLD.period_end
            OR NEW.version IS DISTINCT FROM OLD.version
            OR NEW.amends_report_id IS DISTINCT FROM OLD.amends_report_id THEN
        RAISE EXCEPTION 'The identity of a statutory report never changes' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status IN {FILED_STATUSES} THEN
        -- EC-STA-12: a filed report is corrected by an amendment, never in place.
        IF NEW.content IS DISTINCT FROM OLD.content
                OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                OR NEW.totals IS DISTINCT FROM OLD.totals
                OR NEW.run_ids IS DISTINCT FROM OLD.run_ids
                OR NEW.pack_version IS DISTINCT FROM OLD.pack_version
                OR NEW.export_sha256 IS DISTINCT FROM OLD.export_sha256
                OR NEW.filing_ref IS DISTINCT FROM OLD.filing_ref
                OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
                OR (OLD.status IN ('ACCEPTED', 'REJECTED', 'AMENDED') AND (
                    NEW.acknowledgement_ref IS DISTINCT FROM OLD.acknowledgement_ref
                    OR NEW.authority_response IS DISTINCT FROM OLD.authority_response))
                OR (OLD.status = 'AMENDED' AND NEW.status <> 'AMENDED') THEN
            RAISE EXCEPTION 'STATUTORY_REPORT_FILED: a filed statutory report is corrected by an amendment'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

JOURNALS_GUARD = """
CREATE OR REPLACE FUNCTION accounting_journals_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'POSTED' THEN
            RAISE EXCEPTION 'A posted journal cannot be deleted' USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'POSTED' THEN
        RAISE EXCEPTION 'JOURNAL_POSTED: a posted journal is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.number IS DISTINCT FROM OLD.number THEN
        RAISE EXCEPTION 'The run and number of a journal never change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

JOURNAL_LINES_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION accounting_journal_lines_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'accounting_journal_lines are append-only' USING ERRCODE = 'check_violation';
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


def _row_version():
    return sa.Column('row_version', sa.Integer(), nullable=False, server_default='1')


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    empty_object, empty_array = sa.text("'{}'::jsonb"), sa.text("'[]'::jsonb")
    when = lambda name: sa.Column(name, sa.DateTime(timezone=True), nullable=True)  # noqa: E731
    user = lambda name: sa.Column(name, sa.Integer(), nullable=True)  # noqa: E731

    op.create_table(
        'statutory_reports', *_base(),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('pack_code', sa.String(10), nullable=False),
        sa.Column('pack_version', sa.String(60), nullable=False),
        sa.Column('report_code', sa.String(60), nullable=False),
        sa.Column('report_name', sa.String(200), nullable=False),
        sa.Column('frequency', sa.String(10), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='DRAFT'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('amends_report_id', sa.Integer(), nullable=True),
        sa.Column('is_fixture', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('watermark', sa.String(100), nullable=True),
        sa.Column('run_ids', jsonb, nullable=False, server_default=empty_array),
        sa.Column('totals', jsonb, nullable=False, server_default=empty_object),
        sa.Column('content', jsonb, nullable=False, server_default=empty_object),
        sa.Column('content_hash', sa.CHAR(64), nullable=True),
        sa.Column('row_count', sa.Integer(), nullable=False, server_default='0'),
        when('generated_at'), user('generated_by_user_id'), when('review_requested_at'),
        when('verified_at'), user('verified_by_user_id'),
        when('exported_at'), user('exported_by_user_id'),
        sa.Column('export_sha256', sa.CHAR(64), nullable=True),
        sa.Column('export_storage_path', sa.String(300), nullable=True),
        sa.Column('export_file_name', sa.String(150), nullable=True),
        sa.Column('filing_ref', sa.String(100), nullable=True),
        when('submitted_at'), user('submitted_by_user_id'),
        sa.Column('acknowledgement_ref', sa.String(100), nullable=True),
        when('acknowledged_at'),
        sa.Column('authority_response', sa.Text(), nullable=True),
        when('rejected_at'), when('amended_at'), user('amended_by_user_id'),
        sa.Column('status_reason', sa.Text(), nullable=True),
        user('created_by_user_id'),
        _row_version(),
        _uq('statutory_reports'),
        _fk('statutory_reports', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('statutory_reports', 'generated_by_user_id', 'users', 'generated_by'),
        _fk('statutory_reports', 'verified_by_user_id', 'users', 'verified_by'),
        _fk('statutory_reports', 'exported_by_user_id', 'users', 'exported_by'),
        _fk('statutory_reports', 'submitted_by_user_id', 'users', 'submitted_by'),
        _fk('statutory_reports', 'amended_by_user_id', 'users', 'amended_by'),
        _fk('statutory_reports', 'created_by_user_id', 'users', 'created_by'),
        sa.CheckConstraint(f"status IN {STATUTORY_REPORT_STATUSES}", name='ck_statutory_reports_status'),
        sa.CheckConstraint(f"frequency IN {REPORT_FREQUENCIES}", name='ck_statutory_reports_frequency'),
        sa.CheckConstraint("period_end >= period_start", name='ck_statutory_reports_window'),
        sa.CheckConstraint("version >= 1", name='ck_statutory_reports_version'),
        sa.CheckConstraint("(amends_report_id IS NULL) = (version = 1)", name='ck_statutory_reports_amendment'),
        sa.CheckConstraint("amends_report_id IS NULL OR amends_report_id <> id", name='ck_statutory_reports_not_self'),
        sa.CheckConstraint("NOT is_fixture OR watermark IS NOT NULL", name='ck_statutory_reports_fixture_watermark'),
        sa.CheckConstraint("jsonb_typeof(run_ids) = 'array'", name='ck_statutory_reports_run_ids'),
        sa.CheckConstraint("jsonb_typeof(totals) = 'object'", name='ck_statutory_reports_totals'),
        sa.CheckConstraint("jsonb_typeof(content) = 'object'", name='ck_statutory_reports_content'),
        sa.CheckConstraint("content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'", name='ck_statutory_reports_hash'),
        sa.CheckConstraint("status IN ('DRAFT') OR content_hash IS NOT NULL", name='ck_statutory_reports_generated'),
        sa.CheckConstraint("export_sha256 IS NULL OR export_sha256 ~ '^[0-9a-f]{64}$'",
                           name='ck_statutory_reports_export_hash'),
        sa.CheckConstraint("status NOT IN ('READY_TO_FILE') OR export_sha256 IS NOT NULL",
                           name='ck_statutory_reports_exported'),
        sa.CheckConstraint(f"status NOT IN {FILED_STATUSES} OR (filing_ref IS NOT NULL AND submitted_at IS NOT NULL)",
                           name='ck_statutory_reports_filing_ref'),
        sa.CheckConstraint("status <> 'ACCEPTED' OR (acknowledgement_ref IS NOT NULL AND acknowledged_at IS NOT NULL)",
                           name='ck_statutory_reports_acknowledged'),
        sa.CheckConstraint("status <> 'REJECTED' OR (authority_response IS NOT NULL AND rejected_at IS NOT NULL)",
                           name='ck_statutory_reports_rejected'),
        sa.CheckConstraint("(status = 'AMENDED') = (amended_at IS NOT NULL)", name='ck_statutory_reports_amended'),
        sa.CheckConstraint("row_version >= 1", name='ck_statutory_reports_row_version'),
    )
    # Self-reference after the table exists (composite same-company key).
    op.create_foreign_key('fk_statutory_reports_amends', 'statutory_reports', 'statutory_reports',
                          ['company_id', 'amends_report_id'], ['company_id', 'id'])
    op.create_index('ix_statutory_reports_company_id', 'statutory_reports', ['company_id'])
    op.create_index('ix_statutory_reports_legal_entity_id', 'statutory_reports', ['legal_entity_id'])
    op.create_index('ix_statutory_reports_entity_period', 'statutory_reports',
                    ['company_id', 'legal_entity_id', 'period_start'])
    op.create_index('uq_statutory_reports_live', 'statutory_reports',
                    ['company_id', 'legal_entity_id', 'report_code', 'period_start'], unique=True,
                    postgresql_where=sa.text("status <> 'AMENDED'"))
    op.create_index('uq_statutory_reports_amends', 'statutory_reports', ['company_id', 'amends_report_id'],
                    unique=True, postgresql_where=sa.text("amends_report_id IS NOT NULL"))

    op.create_table(
        'accounting_journals', *_base(),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.String(30), nullable=False),
        sa.Column('trigger', sa.String(20), nullable=False),
        sa.Column('format', sa.String(20), nullable=False, server_default='GENERIC_CSV'),
        sa.Column('status', sa.String(10), nullable=False),
        sa.Column('posting_date', sa.Date(), nullable=False),
        sa.Column('totals', jsonb, nullable=False, server_default=empty_object),
        sa.Column('line_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('content_hash', sa.CHAR(64), nullable=True),
        sa.Column('errors', jsonb, nullable=False, server_default=empty_array),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('fixture', sa.Boolean(), nullable=False, server_default=sa.false()),
        when('posted_at'), user('posted_by_user_id'),
        _row_version(),
        _uq('accounting_journals'),
        sa.UniqueConstraint('company_id', 'run_id', name='uq_accounting_journals_run'),
        sa.UniqueConstraint('company_id', 'number', name='uq_accounting_journals_number'),
        _fk('accounting_journals', 'run_id', 'payroll_runs', 'run'),
        _fk('accounting_journals', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('accounting_journals', 'posted_by_user_id', 'users', 'posted_by'),
        sa.CheckConstraint(f"status IN {JOURNAL_STATUSES}", name='ck_accounting_journals_status'),
        sa.CheckConstraint(f"trigger IN {JOURNAL_TRIGGERS}", name='ck_accounting_journals_trigger'),
        # A one-item tuple would render as ('GENERIC_CSV',), which is not valid SQL.
        sa.CheckConstraint("format IN (" + ", ".join(f"'{f}'" for f in JOURNAL_FORMATS) + ")",
                           name='ck_accounting_journals_format'),
        sa.CheckConstraint("number ~ '^JE-[0-9]{4}-[0-9]{6}$'", name='ck_accounting_journals_number'),
        sa.CheckConstraint("(status = 'POSTED') = (posted_at IS NOT NULL AND content_hash IS NOT NULL)",
                           name='ck_accounting_journals_posted'),
        sa.CheckConstraint("status <> 'FAILED' OR jsonb_array_length(errors) > 0", name='ck_accounting_journals_errors'),
        sa.CheckConstraint("content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'",
                           name='ck_accounting_journals_hash'),
        sa.CheckConstraint("jsonb_typeof(totals) = 'object'", name='ck_accounting_journals_totals'),
        sa.CheckConstraint("jsonb_typeof(errors) = 'array'", name='ck_accounting_journals_errors_shape'),
        sa.CheckConstraint("attempts >= 1 AND line_count >= 0", name='ck_accounting_journals_counts'),
        sa.CheckConstraint("row_version >= 1", name='ck_accounting_journals_row_version'),
    )
    op.create_index('ix_accounting_journals_company_id', 'accounting_journals', ['company_id'])
    op.create_index('ix_accounting_journals_run_id', 'accounting_journals', ['run_id'])
    op.create_index('ix_accounting_journals_legal_entity_id', 'accounting_journals', ['legal_entity_id'])

    op.create_table(
        'accounting_journal_lines', *_base(),
        sa.Column('journal_id', sa.Integer(), nullable=False),
        sa.Column('line_no', sa.Integer(), nullable=False),
        sa.Column('account', sa.String(60), nullable=False),
        sa.Column('side', sa.String(6), nullable=False),
        sa.Column('amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('currency', sa.CHAR(3), nullable=False),
        sa.Column('cost_center_id', sa.Integer(), nullable=True),
        sa.Column('cost_center_code', sa.String(30), nullable=True),
        sa.Column('component_code', sa.String(40), nullable=False),
        sa.Column('description', sa.String(300), nullable=False),
        _uq('accounting_journal_lines'),
        sa.UniqueConstraint('journal_id', 'line_no', name='uq_accounting_journal_lines_no'),
        _fk('accounting_journal_lines', 'journal_id', 'accounting_journals', 'journal'),
        _fk('accounting_journal_lines', 'cost_center_id', 'cost_centers', 'cost_center'),
        sa.CheckConstraint(f"side IN {JOURNAL_SIDES}", name='ck_accounting_journal_lines_side'),
        sa.CheckConstraint("amount > 0", name='ck_accounting_journal_lines_amount'),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name='ck_accounting_journal_lines_currency'),
        sa.CheckConstraint("line_no >= 1", name='ck_accounting_journal_lines_no'),
    )
    op.create_index('ix_accounting_journal_lines_company_id', 'accounting_journal_lines', ['company_id'])
    op.create_index('ix_accounting_journal_lines_journal_id', 'accounting_journal_lines', ['journal_id'])

    op.create_table(
        'gl_account_mappings', *_base(),
        sa.Column('legal_entity_id', sa.Integer(), nullable=True),
        sa.Column('component_code', sa.String(40), nullable=False),
        sa.Column('debit_account', sa.String(60), nullable=False),
        sa.Column('credit_account', sa.String(60), nullable=False),
        sa.Column('allocate_by_cost_center', sa.Boolean(), nullable=False, server_default=sa.false()),
        user('updated_by_user_id'),
        _row_version(),
        _uq('gl_account_mappings'),
        _fk('gl_account_mappings', 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('gl_account_mappings', 'updated_by_user_id', 'users', 'updated_by'),
        sa.CheckConstraint("component_code ~ '^[A-Z][A-Z0-9_]{0,39}$'", name='ck_gl_account_mappings_code'),
        sa.CheckConstraint("length(trim(debit_account)) > 0 AND length(trim(credit_account)) > 0",
                           name='ck_gl_account_mappings_accounts'),
        sa.CheckConstraint("row_version >= 1", name='ck_gl_account_mappings_row_version'),
    )
    op.create_index('ix_gl_account_mappings_company_id', 'gl_account_mappings', ['company_id'])
    op.create_index('ix_gl_account_mappings_legal_entity_id', 'gl_account_mappings', ['legal_entity_id'])
    op.create_index('uq_gl_account_mappings_key', 'gl_account_mappings',
                    ['company_id', sa.text('COALESCE(legal_entity_id, 0)'), 'component_code'], unique=True)

    op.execute(STATUTORY_REPORTS_GUARD)
    op.execute(JOURNALS_GUARD)
    op.execute(JOURNAL_LINES_APPEND_ONLY)
    op.execute("CREATE TRIGGER trg_statutory_reports_guard BEFORE UPDATE OR DELETE ON statutory_reports "
               "FOR EACH ROW EXECUTE FUNCTION statutory_reports_guard()")
    op.execute("CREATE TRIGGER trg_accounting_journals_guard BEFORE UPDATE OR DELETE ON accounting_journals "
               "FOR EACH ROW EXECUTE FUNCTION accounting_journals_guard()")
    op.execute("CREATE TRIGGER trg_accounting_journal_lines_append_only BEFORE UPDATE OR DELETE "
               "ON accounting_journal_lines FOR EACH ROW EXECUTE FUNCTION accounting_journal_lines_append_only()")
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
            privileges = "SELECT, INSERT" if table == "accounting_journal_lines" else "SELECT, INSERT, UPDATE, DELETE"
            op.execute(f"GRANT {privileges} ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def downgrade() -> None:
    for table, trigger in (("accounting_journal_lines", "trg_accounting_journal_lines_append_only"),
                           ("accounting_journals", "trg_accounting_journals_guard"),
                           ("statutory_reports", "trg_statutory_reports_guard")):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for function in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_constraint('fk_statutory_reports_amends', 'statutory_reports', type_='foreignkey')
    for table in ("gl_account_mappings", "accounting_journal_lines", "accounting_journals", "statutory_reports"):
        op.drop_table(table)
