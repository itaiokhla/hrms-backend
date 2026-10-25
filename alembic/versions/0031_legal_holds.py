"""Legal holds and document retention (EC-SEC-16, 3.6 document retention, 09 §6; FOLLOW-UPS F-73).

* ``legal_holds``: tenant table (RLS, grants, composite same-company foreign keys) with the scope of a hold
  (employee, legal entity, document type or one document), its reason, creator and, once released, the releaser
  (never the creator: maker-checker, CHECK) and reason. Trigger ``legal_holds_guard`` freezes everything but the
  release, refuses to reopen a released hold and refuses every DELETE (the application role holds the DELETE
  privilege like on documents, the trigger is what stops it) (holds stay as evidence).
* ``documents.retention_redacted_at``: set when the retention sweep deleted the stored file of a payslip PDF or a
  register. The finalized records themselves (the document row with its frozen content and hashes, run lines)
  are never touched. A redacted document is never re-rendered or re-stored (services refuse; trigger below).
* ``legal_hold_covers_document(company, hold, document)`` and ``document_under_legal_hold(company, document)``:
  the one definition of who a hold covers, used by the sweep, the screens and the trigger.
* Trigger ``documents_retention_guard`` (BEFORE UPDATE on documents): a document can be redacted only if it has
  a retention date that is not in the future (two days of slack for time zones), is a payslip or register, is
  under NO active hold (``LEGAL_HOLD``), and its stored file pointer is gone; a redaction is final. Deleting a document stays impossible for everybody (documents_immutable, 0023), held
  or not.

Documents issued before this revision have no retention date and are kept: the sweep never invents one.

Downgrade drops the triggers, functions, the column and the table.

Revision ID: 0031_legal_holds
Revises: 0030_partner_delegations
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0031_legal_holds'
down_revision: Union[str, None] = '0030_partner_delegations'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"

COVERS = """
CREATE OR REPLACE FUNCTION legal_hold_covers_document(p_company_id integer, p_hold_id integer, p_document_id integer)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1
        FROM legal_holds h
        JOIN documents d ON d.company_id = h.company_id
        WHERE h.company_id = p_company_id AND h.id = p_hold_id AND d.id = p_document_id
          AND ((h.scope = 'DOCUMENT' AND h.document_id = d.id)
            OR (h.scope = 'DOCUMENT_TYPE' AND h.doc_type = d.doc_type)
            OR (h.scope = 'LEGAL_ENTITY' AND h.legal_entity_id = d.legal_entity_id)
            OR (h.scope = 'EMPLOYEE' AND (d.employee_id = h.employee_id
                OR (d.employee_id IS NULL AND d.run_id IS NOT NULL AND EXISTS (
                    SELECT 1 FROM payroll_run_employees e
                    WHERE e.company_id = d.company_id AND e.run_id = d.run_id AND e.employee_id = h.employee_id)))))
    )
$$
"""

UNDER_HOLD = """
CREATE OR REPLACE FUNCTION document_under_legal_hold(p_company_id integer, p_document_id integer)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM legal_holds h
        WHERE h.company_id = p_company_id AND h.status = 'ACTIVE'
          AND legal_hold_covers_document(p_company_id, h.id, p_document_id)
    )
$$
"""

HOLDS_GUARD = """
CREATE OR REPLACE FUNCTION legal_holds_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'LEGAL_HOLD_IMMUTABLE: legal holds are never deleted; release them'
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.status = 'RELEASED' THEN
        RAISE EXCEPTION 'LEGAL_HOLD_IMMUTABLE: a released legal hold never changes; place a new one'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.scope IS DISTINCT FROM OLD.scope OR NEW.employee_id IS DISTINCT FROM OLD.employee_id
            OR NEW.legal_entity_id IS DISTINCT FROM OLD.legal_entity_id OR NEW.doc_type IS DISTINCT FROM OLD.doc_type
            OR NEW.document_id IS DISTINCT FROM OLD.document_id OR NEW.reason IS DISTINCT FROM OLD.reason
            OR NEW.reference IS DISTINCT FROM OLD.reference
            OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id
            OR NEW.company_id IS DISTINCT FROM OLD.company_id OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'LEGAL_HOLD_IMMUTABLE: the scope and reason of a legal hold never change'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

RETENTION_GUARD = """
CREATE OR REPLACE FUNCTION documents_retention_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.retention_redacted_at IS NOT NULL THEN
        IF NEW.retention_redacted_at IS DISTINCT FROM OLD.retention_redacted_at THEN
            RAISE EXCEPTION 'RETENTION_REDACTED: a redaction is final' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.storage_path IS NOT NULL THEN
            RAISE EXCEPTION 'RETENTION_REDACTED: the stored file was deleted by retention and is never restored'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.retention_redacted_at IS NOT NULL THEN
        IF document_under_legal_hold(NEW.company_id, NEW.id) THEN
            RAISE EXCEPTION 'LEGAL_HOLD: a document under an active legal hold is never deleted'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.doc_type NOT IN ('PAYSLIP_V2', 'PAYROLL_REGISTER') THEN
            RAISE EXCEPTION 'RETENTION_TYPE: only payslip PDFs and payroll registers are deleted by retention'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.retention_until IS NULL OR NEW.retention_until > current_date + 2 THEN
            RAISE EXCEPTION 'RETENTION_NOT_DUE: the retention period of this document has not ended'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.storage_path IS NOT NULL THEN
            RAISE EXCEPTION 'RETENTION_REDACTED: a redacted document keeps no stored file pointer'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
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


def _fk(column, target, name):
    return sa.ForeignKeyConstraint(['company_id', column], [f'{target}.company_id', f'{target}.id'],
                                   name=f'fk_legal_holds_{name}')


def upgrade() -> None:
    op.create_table(
        'legal_holds', *_base(),
        sa.Column('scope', sa.String(14), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('legal_entity_id', sa.Integer(), nullable=True),
        sa.Column('doc_type', sa.String(30), nullable=True),
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('reference', sa.String(120), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=False),
        sa.Column('released_by_user_id', sa.Integer(), nullable=True),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('release_reason', sa.Text(), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.UniqueConstraint('company_id', 'id', name='uq_legal_holds_company_id_id'),
        _fk('employee_id', 'employees', 'employee'),
        _fk('legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk('document_id', 'documents', 'document'),
        _fk('created_by_user_id', 'users', 'created_by'),
        _fk('released_by_user_id', 'users', 'released_by'),
        sa.CheckConstraint("scope IN ('EMPLOYEE', 'LEGAL_ENTITY', 'DOCUMENT_TYPE', 'DOCUMENT')",
                           name='ck_legal_holds_scope'),
        sa.CheckConstraint("status IN ('ACTIVE', 'RELEASED')", name='ck_legal_holds_status'),
        sa.CheckConstraint(
            "(scope = 'EMPLOYEE' AND employee_id IS NOT NULL AND legal_entity_id IS NULL AND doc_type IS NULL "
            "AND document_id IS NULL) OR "
            "(scope = 'LEGAL_ENTITY' AND legal_entity_id IS NOT NULL AND employee_id IS NULL AND doc_type IS NULL "
            "AND document_id IS NULL) OR "
            "(scope = 'DOCUMENT_TYPE' AND doc_type IS NOT NULL AND employee_id IS NULL AND legal_entity_id IS NULL "
            "AND document_id IS NULL) OR "
            "(scope = 'DOCUMENT' AND document_id IS NOT NULL AND employee_id IS NULL AND legal_entity_id IS NULL "
            "AND doc_type IS NULL)", name='ck_legal_holds_target'),
        sa.CheckConstraint("(status = 'RELEASED') = (released_at IS NOT NULL AND released_by_user_id IS NOT NULL "
                           "AND release_reason IS NOT NULL)", name='ck_legal_holds_released'),
        sa.CheckConstraint("released_by_user_id IS NULL OR released_by_user_id <> created_by_user_id",
                           name='ck_legal_holds_maker_checker'),
        sa.CheckConstraint("length(btrim(reason)) > 0", name='ck_legal_holds_reason'),
        sa.CheckConstraint("row_version >= 1", name='ck_legal_holds_row_version'),
    )
    op.create_index('ix_legal_holds_company_id', 'legal_holds', ['company_id'])
    op.create_index('ix_legal_holds_active', 'legal_holds', ['company_id', 'scope'],
                    postgresql_where=sa.text("status = 'ACTIVE'"))

    op.add_column('documents', sa.Column('retention_redacted_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_documents_retention_due', 'documents', ['company_id', 'retention_until'],
                    postgresql_where=sa.text('retention_redacted_at IS NULL AND retention_until IS NOT NULL'))

    op.execute(COVERS)
    op.execute(UNDER_HOLD)
    op.execute(HOLDS_GUARD)
    op.execute(RETENTION_GUARD)
    op.execute("CREATE TRIGGER trg_legal_holds_guard BEFORE UPDATE OR DELETE ON legal_holds "
               "FOR EACH ROW EXECUTE FUNCTION legal_holds_guard()")
    op.execute("CREATE TRIGGER trg_documents_retention_guard BEFORE UPDATE ON documents "
               "FOR EACH ROW EXECUTE FUNCTION documents_retention_guard()")
    _install_security(op.get_bind())


def _install_security(bind) -> None:
    op.execute("ALTER TABLE legal_holds ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON legal_holds USING ({TENANT_PREDICATE}) "
               f"WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON legal_holds TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE legal_holds_id_seq TO {quoted}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_documents_retention_guard ON documents")
    op.execute("DROP TRIGGER IF EXISTS trg_legal_holds_guard ON legal_holds")
    op.execute("DROP FUNCTION IF EXISTS documents_retention_guard()")
    op.execute("DROP FUNCTION IF EXISTS legal_holds_guard()")
    op.execute("DROP FUNCTION IF EXISTS document_under_legal_hold(integer, integer)")
    op.execute("DROP FUNCTION IF EXISTS legal_hold_covers_document(integer, integer, integer)")
    op.drop_index('ix_documents_retention_due', table_name='documents')
    op.drop_column('documents', 'retention_redacted_at')
    op.drop_table('legal_holds')
