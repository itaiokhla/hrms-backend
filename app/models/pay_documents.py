"""Payroll v2 documents (3.6, 3.9 §3.7, 01 §5.7; migration 0023_documents).

* ``documents``: every issued payroll document (payslips, payment advice, registers, statements,
  certificates, letters, invoices and credit notes). The row freezes the rendered view model
  (``content``) together with the brand version, template version and renderer that produced the
  PDF, whose bytes are stored privately with their SHA-256 (EC-DOC-03). v2 documents use their own
  namespace (decision I-19): payslips are ``PAYSLIP_V2`` keyed by ``source_key =
  "run_employee:<id>"`` and ``version``, never the legacy ``(PAYSLIP, payroll.id)`` of
  ``issued_documents``. ``status`` uses the DOCUMENT machine (``state_machines.DOCUMENT``);
  identity, content and hash are frozen by trigger ``documents_immutable``.
* ``document_sequences``: one counter per legal entity, document type and scope (fiscal year,
  calendar year or month, as the pattern ``documents.numbering.<type>.pattern`` says). The number
  is allocated with ``SELECT ... FOR UPDATE`` in the issuing transaction, so invoices and credit
  notes are gapless (EC-DOC-08) and no number is ever reused (EC-DOC-09).
* ``document_access_log``: append-only VIEW / DOWNLOAD / EMAIL / VERIFY / REGENERATE trail.
* ``document_generation_tasks``: one task per (run employee, document type, document version),
  the idempotency key of bulk generation (EC-DOC-04), with QUEUED / GENERATED / FAILED.
* ``document_notifications``: at most one notification per document version and channel (EC-DOC-05).
* ``export_jobs``: the download center (bulk ZIPs, CSV/XLSX registers), owned by the requester.
* ``invoices`` + ``invoice_sources``: contractor invoices, self-billed invoices and credit notes
  (INVOICE machine); a source (approved task) is invoiced by at most one live invoice.
* ``self_billing_agreements``: the agreement on file that self-billing requires (3.6 §10).
* ``document_requests``: salary certificate / employment letter requests approved by HR.
* ``payslip_pdf_secrets``: the PDF password an employee sets (EMPLOYEE_SECRET), encrypted with the
  tenant data key (keys.encrypt_field); HR never sees it (EC-DOC-15).
"""

from sqlalchemy import (
    CHAR,
    BigInteger,
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

from app.models.base import BaseModel, TenantMixin, utcnow

# Document types of payroll v2 (3.6 §2). PAYSLIP_V2 keeps v2 payslips out of the legacy PAYSLIP
# namespace (decision I-19).
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
INVOICE_SOURCE_TYPES = ("TASK", "TIMESHEET")  # TIMESHEET: approved weekly timesheets × hourly rate (migration 0058)
AGREEMENT_STATUSES = ("ACTIVE", "REVOKED")
REQUEST_DOC_TYPES = ("SALARY_CERTIFICATE", "EMPLOYMENT_LETTER")
REQUEST_STATUSES = ("PENDING", "APPROVED", "REJECTED", "ISSUED")


def _fk(table: str, columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    remote = [f"{target}.company_id", f"{target}.id"]
    return ForeignKeyConstraint(local, remote, name=f"fk_{table}_{name}", ondelete=ondelete)


def _uq(table: str) -> UniqueConstraint:
    return UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id")


def _in(table: str, column: str, values, *, nullable: bool = False) -> CheckConstraint:
    rule = f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"
    if nullable:
        rule = f"{column} IS NULL OR {rule}"
    return CheckConstraint(rule, name=f"ck_{table}_{column}")


class PayDocument(TenantMixin, BaseModel):
    """One issued document version: frozen view model, rendered PDF bytes (by hash) and publication state."""

    __tablename__ = "documents"

    legal_entity_id = Column(Integer, nullable=False, index=True)
    pay_group_id = Column(Integer, nullable=True)
    doc_type = Column(String(30), nullable=False)
    number = Column(String(80), nullable=True)
    # Random, unguessable id for the public verification page (EC-DOC-18).
    public_id = Column(String(32), nullable=False)
    subject_type = Column(String(12), nullable=False)
    subject_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=True, index=True)
    run_id = Column(Integer, nullable=True, index=True)
    run_employee_id = Column(Integer, nullable=True, index=True)
    batch_id = Column(Integer, nullable=True)
    instruction_id = Column(Integer, nullable=True)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    issue_date = Column(Date, nullable=False)
    # Idempotency of generation: (doc_type, source_key, version) is unique (EC-DOC-04).
    source_key = Column(String(80), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    source_refs = Column(JSONB, nullable=False, default=dict)
    title = Column(String(150), nullable=False)
    status = Column(String(15), nullable=False, default="GENERATED")
    draft = Column(Boolean, nullable=False, default=False)
    content = Column(JSONB, nullable=False)
    content_hash = Column(CHAR(64), nullable=False)
    storage_path = Column(String(300), nullable=True)
    sha256 = Column(CHAR(64), nullable=False)
    bytes = Column(Integer, nullable=False)
    mime = Column(String(80), nullable=False, default="application/pdf")
    file_name = Column(String(150), nullable=False)
    brand_version = Column(String(64), nullable=True)
    brand_profile_id = Column(Integer, nullable=True)
    entity_brand_profile_id = Column(Integer, nullable=True)
    template_version = Column(String(60), nullable=False)
    template_policy_version_id = Column(Integer, nullable=True)
    renderer = Column(String(60), nullable=False)
    locale = Column(String(35), nullable=False)
    languages = Column(JSONB, nullable=False, default=list)
    labels = Column(JSONB, nullable=False, default=list)
    publish_trigger = Column(String(25), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    published_by_user_id = Column(Integer, nullable=True)
    publish_reason = Column(Text, nullable=True)
    first_viewed_at = Column(DateTime(timezone=True), nullable=True)
    supersedes_document_id = Column(Integer, nullable=True)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    reversal_document_id = Column(Integer, nullable=True)
    rerendered = Column(Boolean, nullable=False, default=False)
    rerendered_at = Column(DateTime(timezone=True), nullable=True)
    rerender_mismatch = Column(Boolean, nullable=False, default=False)
    retention_until = Column(Date, nullable=True)
    # Set by the retention sweep when the stored PDF was deleted (migration 0031); the row itself stays.
    retention_redacted_at = Column(DateTime(timezone=True), nullable=True)
    issued_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("documents"),
        UniqueConstraint("public_id", name="uq_documents_public_id"),
        UniqueConstraint("company_id", "doc_type", "source_key", "version", name="uq_documents_source_version"),
        Index("uq_documents_number", "company_id", "legal_entity_id", "doc_type", "number", unique=True,
              postgresql_where=text("number IS NOT NULL")),
        Index("ix_documents_filters", "company_id", "doc_type", "status", "period_end"),
        _fk("documents", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("documents", "pay_group_id", "pay_groups", "pay_group"),
        _fk("documents", "employee_id", "employees", "employee"),
        _fk("documents", "run_id", "payroll_runs", "run"),
        _fk("documents", "run_employee_id", "payroll_run_employees", "run_employee"),
        _fk("documents", "supersedes_document_id", "documents", "supersedes"),
        _fk("documents", "reversal_document_id", "documents", "reversal"),
        _fk("documents", "published_by_user_id", "users", "published_by"),
        _fk("documents", "issued_by_user_id", "users", "issued_by"),
        _in("documents", "doc_type", DOC_TYPES),
        _in("documents", "status", DOCUMENT_STATUSES),
        _in("documents", "subject_type", SUBJECT_TYPES),
        _in("documents", "publish_trigger", PUBLISH_TRIGGERS, nullable=True),
        CheckConstraint("public_id ~ '^[A-Za-z0-9_-]{20,32}$'", name="ck_documents_public_id"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_documents_sha256"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_documents_content_hash"),
        CheckConstraint("bytes > 0", name="ck_documents_bytes"),
        CheckConstraint("version >= 1", name="ck_documents_version"),
        CheckConstraint("row_version >= 1", name="ck_documents_row_version"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="ck_documents_content"),
        CheckConstraint("jsonb_typeof(labels) = 'array'", name="ck_documents_labels"),
        CheckConstraint("jsonb_typeof(languages) = 'array'", name="ck_documents_languages"),
        CheckConstraint("(status = 'GENERATED') OR published_at IS NOT NULL", name="ck_documents_published"),
        CheckConstraint("(status <> 'REVERSED') OR reversed_at IS NOT NULL", name="ck_documents_reversed"),
        CheckConstraint("supersedes_document_id IS NULL OR supersedes_document_id <> id", name="ck_documents_not_self"),
        CheckConstraint("period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
                        name="ck_documents_period"),
    )


class DocumentSequence(TenantMixin, BaseModel):
    """The counter of one legal entity, document type and scope (fiscal year, year, month or ALL)."""

    __tablename__ = "document_sequences"

    legal_entity_id = Column(Integer, nullable=False)
    doc_type = Column(String(30), nullable=False)
    scope_key = Column(String(20), nullable=False)
    pattern = Column(String(64), nullable=False)
    next_value = Column(BigInteger, nullable=False, default=1)
    gapless = Column(Boolean, nullable=False, default=False)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("document_sequences"),
        UniqueConstraint("company_id", "legal_entity_id", "doc_type", "scope_key", name="uq_document_sequences_scope"),
        _fk("document_sequences", "legal_entity_id", "legal_entities", "legal_entity"),
        _in("document_sequences", "doc_type", DOC_TYPES),
        CheckConstraint("next_value >= 1", name="ck_document_sequences_next_value"),
        CheckConstraint("row_version >= 1", name="ck_document_sequences_row_version"),
    )


class DocumentAccessLog(TenantMixin, BaseModel):
    """Append-only record of each view, download, email, verification and regeneration (3.6 §8 Audit)."""

    __tablename__ = "document_access_log"

    document_id = Column(Integer, nullable=True, index=True)
    export_job_id = Column(Integer, nullable=True, index=True)
    user_id = Column(Integer, nullable=True)
    action = Column(String(12), nullable=False)
    ip = Column(String(64), nullable=True)
    at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    details = Column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        _uq("document_access_log"),
        _fk("document_access_log", "document_id", "documents", "document"),
        _fk("document_access_log", "export_job_id", "export_jobs", "export_job"),
        _fk("document_access_log", "user_id", "users", "user"),
        _in("document_access_log", "action", ACCESS_ACTIONS),
        CheckConstraint("document_id IS NOT NULL OR export_job_id IS NOT NULL", name="ck_document_access_log_target"),
    )


class DocumentGenerationTask(TenantMixin, BaseModel):
    """Bulk generation unit: one (run employee, document type, version), retried on its own (EC-DOC-04/16)."""

    __tablename__ = "document_generation_tasks"

    run_id = Column(Integer, nullable=False, index=True)
    run_employee_id = Column(Integer, nullable=False)
    doc_type = Column(String(30), nullable=False)
    document_version = Column(Integer, nullable=False, default=1)
    status = Column(String(10), nullable=False, default="QUEUED")
    attempts = Column(Integer, nullable=False, default=0)
    retries = Column(Integer, nullable=False, default=0)
    last_error = Column(String(500), nullable=True)
    job_id = Column(Integer, nullable=True)
    document_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("document_generation_tasks"),
        UniqueConstraint("company_id", "run_employee_id", "doc_type", "document_version",
                         name="uq_document_generation_tasks_identity"),
        _fk("document_generation_tasks", "run_id", "payroll_runs", "run"),
        _fk("document_generation_tasks", "run_employee_id", "payroll_run_employees", "run_employee"),
        _fk("document_generation_tasks", "document_id", "documents", "document"),
        ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_document_generation_tasks_job", ondelete="SET NULL"),
        _in("document_generation_tasks", "doc_type", DOC_TYPES),
        _in("document_generation_tasks", "status", TASK_STATUSES),
        CheckConstraint("(status = 'GENERATED') = (document_id IS NOT NULL)", name="ck_document_generation_tasks_doc"),
        CheckConstraint("document_version >= 1 AND attempts >= 0 AND retries >= 0",
                        name="ck_document_generation_tasks_counts"),
    )


class DocumentNotification(TenantMixin, BaseModel):
    """The single notification of a document version on a channel (EC-DOC-05)."""

    __tablename__ = "document_notifications"

    document_id = Column(Integer, nullable=False)
    channel = Column(String(10), nullable=False)
    recipient_user_id = Column(Integer, nullable=True)
    status = Column(String(10), nullable=False, default="PENDING")
    notification_id = Column(Integer, nullable=True)
    detail = Column(String(300), nullable=True)
    attached = Column(Boolean, nullable=False, default=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        _uq("document_notifications"),
        UniqueConstraint("company_id", "document_id", "channel", name="uq_document_notifications_once"),
        _fk("document_notifications", "document_id", "documents", "document"),
        _fk("document_notifications", "recipient_user_id", "users", "recipient"),
        _in("document_notifications", "channel", NOTIFICATION_CHANNELS),
        _in("document_notifications", "status", NOTIFICATION_STATUSES),
    )


class ExportJob(TenantMixin, BaseModel):
    """A download-center export requested by one user (3.6 §7.3); only the requester may download it."""

    __tablename__ = "export_jobs"

    requested_by_user_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    params = Column(JSONB, nullable=False, default=dict)
    job_id = Column(Integer, nullable=True)
    status = Column(String(12), nullable=False, default="QUEUED")
    item_count = Column(Integer, nullable=True)
    storage_path = Column(String(300), nullable=True)
    sha256 = Column(CHAR(64), nullable=True)
    bytes = Column(BigInteger, nullable=True)
    mime = Column(String(80), nullable=True)
    file_name = Column(String(150), nullable=True)
    watermark = Column(String(200), nullable=True)
    error = Column(String(500), nullable=True)
    ready_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("export_jobs"),
        _fk("export_jobs", "requested_by_user_id", "users", "requested_by"),
        ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_export_jobs_job", ondelete="SET NULL"),
        _in("export_jobs", "kind", EXPORT_KINDS),
        _in("export_jobs", "status", EXPORT_STATUSES),
        CheckConstraint("status <> 'READY' OR (storage_path IS NOT NULL AND sha256 IS NOT NULL "
                        "AND expires_at IS NOT NULL)", name="ck_export_jobs_ready"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="ck_export_jobs_params"),
    )


class Invoice(TenantMixin, BaseModel):
    """A contractor invoice, a self-billed invoice or a credit note (3.6 §10; INVOICE machine)."""

    __tablename__ = "invoices"

    kind = Column(String(12), nullable=False, default="INVOICE")
    mode = Column(String(12), nullable=False)
    legal_entity_id = Column(Integer, nullable=False, index=True)
    payee_employee_id = Column(Integer, nullable=False, index=True)
    status = Column(String(10), nullable=False, default="DRAFT")
    # Internal gapless number, allocated when issued (EC-DOC-08); the contractor's own number apart.
    number = Column(String(80), nullable=True)
    external_number = Column(String(80), nullable=True)
    document_id = Column(Integer, nullable=True)
    credit_of_invoice_id = Column(Integer, nullable=True)
    currency = Column(CHAR(3), nullable=False)
    seller = Column(JSONB, nullable=False, default=dict)
    buyer = Column(JSONB, nullable=False, default=dict)
    lines = Column(JSONB, nullable=False, default=list)
    taxes = Column(JSONB, nullable=False, default=list)
    subtotal = Column(Numeric(19, 4), nullable=False)
    tax_total = Column(Numeric(19, 4), nullable=False, default=0)
    total = Column(Numeric(19, 4), nullable=False)
    issue_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    payment_terms = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    issued_by_user_id = Column(Integer, nullable=True)
    issued_at = Column(DateTime(timezone=True), nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    credited_at = Column(DateTime(timezone=True), nullable=True)
    status_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("invoices"),
        Index("uq_invoices_number", "company_id", "legal_entity_id", "kind", "mode", "number", unique=True,
              postgresql_where=text("number IS NOT NULL")),
        _fk("invoices", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("invoices", "payee_employee_id", "employees", "payee"),
        _fk("invoices", "document_id", "documents", "document"),
        _fk("invoices", "credit_of_invoice_id", "invoices", "credit_of"),
        _fk("invoices", "created_by_user_id", "users", "created_by"),
        _fk("invoices", "issued_by_user_id", "users", "issued_by"),
        _fk("invoices", "approved_by_user_id", "users", "approved_by"),
        _in("invoices", "kind", INVOICE_KINDS),
        _in("invoices", "mode", INVOICE_MODES),
        _in("invoices", "status", INVOICE_STATUSES),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_invoices_currency"),
        CheckConstraint("(kind = 'CREDIT_NOTE') = (credit_of_invoice_id IS NOT NULL)", name="ck_invoices_credit_of"),
        CheckConstraint("status = 'DRAFT' OR number IS NOT NULL", name="ck_invoices_numbered"),
        CheckConstraint("subtotal >= 0 AND tax_total >= 0 AND total >= 0", name="ck_invoices_amounts"),
        CheckConstraint("approved_by_user_id IS NULL OR approved_by_user_id <> issued_by_user_id",
                        name="ck_invoices_maker_checker"),
        CheckConstraint("jsonb_typeof(lines) = 'array' AND jsonb_typeof(taxes) = 'array'", name="ck_invoices_shape"),
        CheckConstraint("row_version >= 1", name="ck_invoices_row_version"),
    )


class InvoiceSource(TenantMixin, BaseModel):
    """An approved record an invoice bills; a source is billed by one live invoice at a time."""

    __tablename__ = "invoice_sources"

    invoice_id = Column(Integer, nullable=False, index=True)
    source_type = Column(String(10), nullable=False)
    source_id = Column(Integer, nullable=False)
    active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        _uq("invoice_sources"),
        _fk("invoice_sources", "invoice_id", "invoices", "invoice", ondelete="CASCADE"),
        _in("invoice_sources", "source_type", INVOICE_SOURCE_TYPES),
        Index("uq_invoice_sources_live", "company_id", "source_type", "source_id", unique=True,
              postgresql_where=text("active")),
    )


class SelfBillingAgreement(TenantMixin, BaseModel):
    """A contractor's self-billing agreement on file (required for SELF_BILLED invoices)."""

    __tablename__ = "self_billing_agreements"

    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    reference = Column(String(100), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        _uq("self_billing_agreements"),
        _fk("self_billing_agreements", "employee_id", "employees", "employee"),
        _fk("self_billing_agreements", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("self_billing_agreements", "created_by_user_id", "users", "created_by"),
        _in("self_billing_agreements", "status", AGREEMENT_STATUSES),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_self_billing_agreements_window"),
    )


class DocumentRequest(TenantMixin, BaseModel):
    """An employee's request for a salary certificate or employment letter, approved by HR (3.6 §11)."""

    __tablename__ = "document_requests"

    employee_id = Column(Integer, nullable=False, index=True)
    doc_type = Column(String(30), nullable=False)
    include_amounts = Column(Boolean, nullable=False, default=False)
    purpose = Column(String(300), nullable=True)
    status = Column(String(10), nullable=False, default="PENDING")
    requested_by_user_id = Column(Integer, nullable=False)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)
    document_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("document_requests"),
        _fk("document_requests", "employee_id", "employees", "employee"),
        _fk("document_requests", "requested_by_user_id", "users", "requested_by"),
        _fk("document_requests", "decided_by_user_id", "users", "decided_by"),
        _fk("document_requests", "document_id", "documents", "document"),
        _in("document_requests", "doc_type", REQUEST_DOC_TYPES),
        _in("document_requests", "status", REQUEST_STATUSES),
        CheckConstraint("(status = 'ISSUED') = (document_id IS NOT NULL)", name="ck_document_requests_issued"),
        CheckConstraint("decided_by_user_id IS NULL OR decided_by_user_id <> requested_by_user_id",
                        name="ck_document_requests_maker_checker"),
    )


class PayslipPdfSecret(TenantMixin, BaseModel):
    """The employee's PDF password, encrypted with the tenant data key; never shown to anyone."""

    __tablename__ = "payslip_pdf_secrets"

    employee_id = Column(Integer, nullable=False)
    secret_enc = Column(Text, nullable=False)
    set_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payslip_pdf_secrets"),
        UniqueConstraint("company_id", "employee_id", name="uq_payslip_pdf_secrets_employee"),
        _fk("payslip_pdf_secrets", "employee_id", "employees", "employee", ondelete="CASCADE"),
    )
