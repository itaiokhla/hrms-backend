"""Statutory reports, accounting journals and GL account mappings (3.2 §8, 3.1 §5.18, 3.3 §2.2, 3.9 §3.10;
migration 0024_reports_compliance).

* ``statutory_reports``: one pack-defined report (``CountryPack.reports()``) of one legal entity for one
  reporting period, built only from finalized runs. ``status`` uses ``state_machines.STATUTORY_REPORT``
  and changes only through ``services.statutory_reports.transition``: exporting a file moves a report to
  READY_TO_FILE, never SUBMITTED (EC-STA-11); SUBMITTED needs a filing reference, ACCEPTED an
  acknowledgement, REJECTED the authority's response. A correction after submission is a new report
  (``amends_report_id``, ``version`` + 1) and the original becomes AMENDED (EC-STA-12). One live
  (non-AMENDED) report per (entity, report code, period start) is a partial unique index; the trigger
  ``statutory_reports_guard`` freezes the identity always and the content once submitted. A report of a
  fixture pack version carries the watermark "FIXTURE — not law" (decision I-07).
* ``accounting_journals`` / ``accounting_journal_lines``: the GENERIC_CSV journal of one run, posted on
  ``payroll.accounting.post_on`` (FINALIZE or PAYMENT_SETTLED). One journal per run (never double-posted,
  UNIQUE (company_id, run_id)); a FAILED journal (missing GL mapping, unbalanced) records its errors and
  is retried in place. A POSTED journal and every journal line are immutable (triggers); ``content_hash``
  is re-checked whenever the journal is read back.
* ``gl_account_mappings``: debit/credit accounts for components that carry no ``gl`` of their own, such
  as pack-owned statutory components (read-only for tenants, EC-STA-18), per legal entity or tenant-wide.
"""

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

STATUTORY_REPORT_STATUSES = (
    "DRAFT", "CALCULATED", "REVIEW_REQUIRED", "VERIFIED", "READY_TO_FILE", "SUBMITTED", "ACCEPTED", "REJECTED",
    "AMENDED",
)
# Statuses after a filing: the report's content is frozen (EC-STA-12).
FILED_STATUSES = ("SUBMITTED", "ACCEPTED", "REJECTED", "AMENDED")
REPORT_FREQUENCIES = ("MONTHLY", "QUARTERLY", "ANNUAL")
JOURNAL_STATUSES = ("POSTED", "FAILED")
JOURNAL_TRIGGERS = ("FINALIZE", "PAYMENT_SETTLED", "MANUAL")
JOURNAL_FORMATS = ("GENERIC_CSV",)
JOURNAL_SIDES = ("DEBIT", "CREDIT")


def _fk(table: str, columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    remote = [f"{target}.company_id", f"{target}.id"]
    return ForeignKeyConstraint(local, remote, name=f"fk_{table}_{name}", ondelete=ondelete)


def _uq(table: str) -> UniqueConstraint:
    return UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id")


class StatutoryReport(TenantMixin, BaseModel):
    """A pack statutory report of one legal entity and period (3.2 §8.2)."""

    __tablename__ = "statutory_reports"

    legal_entity_id = Column(Integer, nullable=False, index=True)
    pack_code = Column(String(10), nullable=False)
    pack_version = Column(String(60), nullable=False)
    report_code = Column(String(60), nullable=False)
    report_name = Column(String(200), nullable=False)
    frequency = Column(String(10), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    due_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=False, default="DRAFT")
    version = Column(Integer, nullable=False, default=1)
    amends_report_id = Column(Integer, nullable=True)
    is_fixture = Column(Boolean, nullable=False, default=False)
    watermark = Column(String(100), nullable=True)
    # The finalized runs the content was built from, and the reconciliation totals per component and
    # liability per currency: {"components": {...}, "liabilities": {...}, "employees": n}.
    run_ids = Column(JSONB, nullable=False, default=list)
    totals = Column(JSONB, nullable=False, default=dict)
    content = Column(JSONB, nullable=False, default=dict)
    content_hash = Column(CHAR(64), nullable=True)
    row_count = Column(Integer, nullable=False, default=0)
    generated_at = Column(DateTime(timezone=True), nullable=True)
    generated_by_user_id = Column(Integer, nullable=True)
    review_requested_at = Column(DateTime(timezone=True), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by_user_id = Column(Integer, nullable=True)
    exported_at = Column(DateTime(timezone=True), nullable=True)
    exported_by_user_id = Column(Integer, nullable=True)
    export_sha256 = Column(CHAR(64), nullable=True)
    export_storage_path = Column(String(300), nullable=True)
    export_file_name = Column(String(150), nullable=True)
    filing_ref = Column(String(100), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    submitted_by_user_id = Column(Integer, nullable=True)
    acknowledgement_ref = Column(String(100), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    authority_response = Column(Text, nullable=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    amended_at = Column(DateTime(timezone=True), nullable=True)
    amended_by_user_id = Column(Integer, nullable=True)
    status_reason = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("statutory_reports"),
        _fk("statutory_reports", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("statutory_reports", "amends_report_id", "statutory_reports", "amends"),
        _fk("statutory_reports", "generated_by_user_id", "users", "generated_by"),
        _fk("statutory_reports", "verified_by_user_id", "users", "verified_by"),
        _fk("statutory_reports", "exported_by_user_id", "users", "exported_by"),
        _fk("statutory_reports", "submitted_by_user_id", "users", "submitted_by"),
        _fk("statutory_reports", "amended_by_user_id", "users", "amended_by"),
        _fk("statutory_reports", "created_by_user_id", "users", "created_by"),
        CheckConstraint(f"status IN {STATUTORY_REPORT_STATUSES}", name="ck_statutory_reports_status"),
        CheckConstraint(f"frequency IN {REPORT_FREQUENCIES}", name="ck_statutory_reports_frequency"),
        CheckConstraint("period_end >= period_start", name="ck_statutory_reports_window"),
        CheckConstraint("version >= 1", name="ck_statutory_reports_version"),
        CheckConstraint("(amends_report_id IS NULL) = (version = 1)", name="ck_statutory_reports_amendment"),
        CheckConstraint("amends_report_id IS NULL OR amends_report_id <> id", name="ck_statutory_reports_not_self"),
        CheckConstraint("NOT is_fixture OR watermark IS NOT NULL", name="ck_statutory_reports_fixture_watermark"),
        CheckConstraint("jsonb_typeof(run_ids) = 'array'", name="ck_statutory_reports_run_ids"),
        CheckConstraint("jsonb_typeof(totals) = 'object'", name="ck_statutory_reports_totals"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="ck_statutory_reports_content"),
        CheckConstraint("content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'", name="ck_statutory_reports_hash"),
        CheckConstraint("status IN ('DRAFT') OR content_hash IS NOT NULL", name="ck_statutory_reports_generated"),
        CheckConstraint("export_sha256 IS NULL OR export_sha256 ~ '^[0-9a-f]{64}$'",
                        name="ck_statutory_reports_export_hash"),
        CheckConstraint("status NOT IN ('READY_TO_FILE') OR export_sha256 IS NOT NULL",
                        name="ck_statutory_reports_exported"),
        # EC-STA-11: SUBMITTED (and everything after it) needs the filing reference.
        CheckConstraint(f"status NOT IN {FILED_STATUSES} OR (filing_ref IS NOT NULL AND submitted_at IS NOT NULL)",
                        name="ck_statutory_reports_filing_ref"),
        CheckConstraint("status <> 'ACCEPTED' OR (acknowledgement_ref IS NOT NULL AND acknowledged_at IS NOT NULL)",
                        name="ck_statutory_reports_acknowledged"),
        CheckConstraint("status <> 'REJECTED' OR (authority_response IS NOT NULL AND rejected_at IS NOT NULL)",
                        name="ck_statutory_reports_rejected"),
        CheckConstraint("(status = 'AMENDED') = (amended_at IS NOT NULL)", name="ck_statutory_reports_amended"),
        CheckConstraint("row_version >= 1", name="ck_statutory_reports_row_version"),
        Index("uq_statutory_reports_live", "company_id", "legal_entity_id", "report_code", "period_start",
              unique=True, postgresql_where=text("status <> 'AMENDED'")),
        Index("uq_statutory_reports_amends", "company_id", "amends_report_id", unique=True,
              postgresql_where=text("amends_report_id IS NOT NULL")),
        Index("ix_statutory_reports_entity_period", "company_id", "legal_entity_id", "period_start"),
    )


class AccountingJournal(TenantMixin, BaseModel):
    """The accounting journal of one run (3.1 §5.18): one per run, never posted twice."""

    __tablename__ = "accounting_journals"

    run_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False, index=True)
    number = Column(String(30), nullable=False)
    trigger = Column(String(20), nullable=False)
    format = Column(String(20), nullable=False, default="GENERIC_CSV")
    status = Column(String(10), nullable=False)
    posting_date = Column(Date, nullable=False)
    # {"INR": {"debit": "…", "credit": "…", "lines": n}} — balanced per currency, never summed across them.
    totals = Column(JSONB, nullable=False, default=dict)
    line_count = Column(Integer, nullable=False, default=0)
    content_hash = Column(CHAR(64), nullable=True)
    errors = Column(JSONB, nullable=False, default=list)
    attempts = Column(Integer, nullable=False, default=1)
    fixture = Column(Boolean, nullable=False, default=False)
    posted_at = Column(DateTime(timezone=True), nullable=True)
    posted_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("accounting_journals"),
        UniqueConstraint("company_id", "run_id", name="uq_accounting_journals_run"),
        UniqueConstraint("company_id", "number", name="uq_accounting_journals_number"),
        _fk("accounting_journals", "run_id", "payroll_runs", "run"),
        _fk("accounting_journals", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("accounting_journals", "posted_by_user_id", "users", "posted_by"),
        CheckConstraint(f"status IN {JOURNAL_STATUSES}", name="ck_accounting_journals_status"),
        CheckConstraint(f"trigger IN {JOURNAL_TRIGGERS}", name="ck_accounting_journals_trigger"),
        CheckConstraint(f"format IN {JOURNAL_FORMATS}", name="ck_accounting_journals_format"),
        CheckConstraint("number ~ '^JE-[0-9]{4}-[0-9]{6}$'", name="ck_accounting_journals_number"),
        CheckConstraint("(status = 'POSTED') = (posted_at IS NOT NULL AND content_hash IS NOT NULL)",
                        name="ck_accounting_journals_posted"),
        CheckConstraint("status <> 'FAILED' OR jsonb_array_length(errors) > 0", name="ck_accounting_journals_errors"),
        CheckConstraint("content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'", name="ck_accounting_journals_hash"),
        CheckConstraint("jsonb_typeof(totals) = 'object'", name="ck_accounting_journals_totals"),
        CheckConstraint("jsonb_typeof(errors) = 'array'", name="ck_accounting_journals_errors_shape"),
        CheckConstraint("attempts >= 1 AND line_count >= 0", name="ck_accounting_journals_counts"),
        CheckConstraint("row_version >= 1", name="ck_accounting_journals_row_version"),
    )


class AccountingJournalLine(TenantMixin, BaseModel):
    """One debit or credit of a posted journal; append-only."""

    __tablename__ = "accounting_journal_lines"

    journal_id = Column(Integer, nullable=False, index=True)
    line_no = Column(Integer, nullable=False)
    account = Column(String(60), nullable=False)
    side = Column(String(6), nullable=False)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    cost_center_id = Column(Integer, nullable=True)
    cost_center_code = Column(String(30), nullable=True)
    component_code = Column(String(40), nullable=False)
    description = Column(String(300), nullable=False)

    __table_args__ = (
        _uq("accounting_journal_lines"),
        UniqueConstraint("journal_id", "line_no", name="uq_accounting_journal_lines_no"),
        _fk("accounting_journal_lines", "journal_id", "accounting_journals", "journal"),
        _fk("accounting_journal_lines", "cost_center_id", "cost_centers", "cost_center"),
        CheckConstraint(f"side IN {JOURNAL_SIDES}", name="ck_accounting_journal_lines_side"),
        CheckConstraint("amount > 0", name="ck_accounting_journal_lines_amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_accounting_journal_lines_currency"),
        CheckConstraint("line_no >= 1", name="ck_accounting_journal_lines_no"),
    )


class GlAccountMapping(TenantMixin, BaseModel):
    """GL accounts for a component code without its own ``gl`` (pack-owned components), per entity or tenant."""

    __tablename__ = "gl_account_mappings"

    legal_entity_id = Column(Integer, nullable=True, index=True)
    component_code = Column(String(40), nullable=False)
    debit_account = Column(String(60), nullable=False)
    credit_account = Column(String(60), nullable=False)
    allocate_by_cost_center = Column(Boolean, nullable=False, default=False)
    updated_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("gl_account_mappings"),
        _fk("gl_account_mappings", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("gl_account_mappings", "updated_by_user_id", "users", "updated_by"),
        CheckConstraint("component_code ~ '^[A-Z][A-Z0-9_]{0,39}$'", name="ck_gl_account_mappings_code"),
        CheckConstraint("length(trim(debit_account)) > 0 AND length(trim(credit_account)) > 0",
                        name="ck_gl_account_mappings_accounts"),
        CheckConstraint("row_version >= 1", name="ck_gl_account_mappings_row_version"),
        Index("uq_gl_account_mappings_key", "company_id", text("COALESCE(legal_entity_id, 0)"), "component_code",
              unique=True),
    )
