"""Payment flows: obligations, batches, instructions, events, files, statements, reconciliation (3.7 §7-§11,
3.9 §3.8; migration 0022_payments; decisions I-10, I-17).

* ``payment_obligations``: what is owed, one row per (run employee, currency) for net pay (or a claim,
  loan or invoice source later). ``status`` OPEN | PARTIAL | SETTLED | UNCLAIMED | CANCELLED is derived
  from the obligation's instructions by ``services.obligations.refresh_obligation``. A zero or negative
  net creates no obligation (EC-PAY-15, EC-PAY-16).
* ``payment_instructions``: what is sent, one row per (obligation, bank account, sequence) with the
  beneficiary snapshot taken at creation (masked JSON + the account identifiers sealed with the tenant
  data key, ``beneficiary_enc``), so a later bank change never redirects it (EC-BNK-03). ``status``
  uses ``state_machines.PAYMENT_INSTRUCTION`` and changes only through
  ``services.payment_events.transition``; ``submission_channel`` and ``evidence_status`` carry the I-17
  vocabulary and the database refuses a settled status without VERIFIED evidence (contract §23.2).
  ``provider_ref`` (sandbox API submissions) is unique across tenants: inbound webhooks resolve their
  tenant from it. A reissue is a new row pointing at the failed one (``reissue_of_instruction_id``).
* ``payment_events``: append-only history of every instruction status change and every piece of
  evidence, applied or not (DUPLICATE / STALE / CONFLICT outcomes are kept for review); a provider event
  id is unique per source (EC-PAY-06). UPDATE and DELETE are refused by a trigger.
* ``payment_batches``: instructions grouped by funding account, method and currency
  (``state_machines.PAYMENT_BATCH``), with preparer / approver / releaser for dual control (3.7 §8.4).
* ``payment_files``: one generated file per batch: format, SHA-256, counts and totals. The bytes are
  never stored (they contain full account numbers); a download regenerates them from the encrypted
  snapshots and must reproduce the stored hash exactly, otherwise it fails loudly (3.7 §7.1).
* ``bank_statement_imports`` / ``bank_statement_lines``: imported statements (CSV mapping, CAMT.053),
  idempotent by file SHA-256 per funding account and by ``line_hash`` per funding account (F-11).
  Counterparty account numbers are kept masked only; free text is scrubbed of account-like numbers.
* ``reconciliation_matches``: one decision per statement line (MATCHED, MISMATCH, UNMATCHED, DUPLICATE,
  RETURN, IGNORED, ACCEPTED_VARIANCE, LINKED, ESCALATED): the mismatch, unmatched and escalation queues.
* ``payment_manual_settlements``: uploaded proof of a payment made outside the system, reviewed by a
  second person before the instruction may settle (EC-PAY-14).
* ``payment_webhook_events`` (platform table, no row-level security, system session only): the inbound
  webhook inbox. ``event_id`` is set only for signature-verified events and is unique per adapter, so a
  replayed or duplicate delivery is processed once (EC-PAY-06, EC-PAY-08); rejected deliveries are logged
  with the untrusted id in ``claimed_event_id``. It holds no tenant data (no company or instruction id):
  the tenant-side record of an applied event is its ``payment_events`` row (row-level security, same
  ``external_event_id``), so the inbox is not a tenant table and needs no reviewed exception.

Bank accounts and funding accounts are the bank-accounts milestone's tables (``bank_accounts``,
``funding_accounts``); the composite same-company foreign keys to them live in the migration.
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

from app.models.base import BaseModel, TenantMixin, utcnow

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
SETTLED_STATUSES = ("PARTIALLY_SETTLED", "SETTLED", "RECONCILED")
SUBMITTED_STATUSES = ("SUBMITTED", "ACKNOWLEDGED", "PROCESSING") + SETTLED_STATUSES
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


def _fk(table: str, columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    remote = [f"{target}.company_id", f"{target}.id"]
    return ForeignKeyConstraint(local, remote, name=f"fk_{table}_{name}", ondelete=ondelete)


def _uq(table: str) -> UniqueConstraint:
    return UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id")


def _currency(table: str, column: str = "currency") -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^[A-Z]{{3}}$'", name=f"ck_{table}_{column}")


def _version(table: str) -> CheckConstraint:
    return CheckConstraint("row_version >= 1", name=f"ck_{table}_row_version")


class PaymentObligation(TenantMixin, BaseModel):
    """What is owed to one payee in one currency (3.7 §11.3)."""

    __tablename__ = "payment_obligations"

    source_type = Column(String(15), nullable=False, default="RUN_EMPLOYEE")
    run_id = Column(Integer, nullable=True, index=True)
    run_employee_id = Column(Integer, nullable=True, index=True)
    payee_type = Column(String(12), nullable=False, default="EMPLOYEE")
    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False, index=True)
    pay_group_id = Column(Integer, nullable=True)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    status = Column(String(10), nullable=False, default="OPEN")
    pay_date = Column(Date, nullable=True)
    carried_at = Column(DateTime(timezone=True), nullable=True)
    carried_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payment_obligations"),
        UniqueConstraint("company_id", "run_employee_id", "currency", name="uq_payment_obligations_run_employee"),
        _fk("payment_obligations", "run_id", "payroll_runs", "run"),
        _fk("payment_obligations", "run_employee_id", "payroll_run_employees", "run_employee"),
        _fk("payment_obligations", "employee_id", "employees", "employee"),
        _fk("payment_obligations", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("payment_obligations", "pay_group_id", "pay_groups", "pay_group"),
        CheckConstraint(f"source_type IN {OBLIGATION_SOURCES}", name="ck_payment_obligations_source"),
        CheckConstraint(f"payee_type IN {PAYEE_TYPES}", name="ck_payment_obligations_payee_type"),
        CheckConstraint(f"status IN {OBLIGATION_STATUSES}", name="ck_payment_obligations_status"),
        CheckConstraint("amount > 0", name="ck_payment_obligations_amount"),
        CheckConstraint("(source_type = 'RUN_EMPLOYEE') = (run_employee_id IS NOT NULL AND run_id IS NOT NULL)",
                        name="ck_payment_obligations_run_source"),
        CheckConstraint("carried_reason IS NULL OR carried_at IS NOT NULL", name="ck_payment_obligations_carried"),
        _currency("payment_obligations"),
        _version("payment_obligations"),
    )


class PaymentBatch(TenantMixin, BaseModel):
    """Instructions released together from one funding account (3.7 §8.2-§8.4)."""

    __tablename__ = "payment_batches"

    reference = Column(String(40), nullable=False)
    run_id = Column(Integer, nullable=True, index=True)
    legal_entity_id = Column(Integer, nullable=False, index=True)
    funding_account_id = Column(Integer, nullable=False, index=True)
    method = Column(String(10), nullable=False)
    file_format = Column(String(30), nullable=True)
    adapter = Column(String(20), nullable=True)
    currency = Column(CHAR(3), nullable=False)
    status = Column(String(20), nullable=False, default="DRAFT")
    item_count = Column(Integer, nullable=False, default=0)
    total_amount = Column(Numeric(19, 4), nullable=False, default=0)
    totals = Column(JSONB, nullable=False, default=dict)
    value_date = Column(Date, nullable=True)
    prepared_by_user_id = Column(Integer, nullable=False)
    approved_by_user_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    released_by_user_id = Column(Integer, nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    submission_channel = Column(String(15), nullable=True)
    cancelled_by_user_id = Column(Integer, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    status_reason = Column(Text, nullable=True)
    details = Column(JSONB, nullable=False, default=dict)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payment_batches"),
        UniqueConstraint("company_id", "reference", name="uq_payment_batches_reference"),
        _fk("payment_batches", "run_id", "payroll_runs", "run"),
        _fk("payment_batches", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("payment_batches", "prepared_by_user_id", "users", "prepared_by"),
        _fk("payment_batches", "approved_by_user_id", "users", "approved_by"),
        _fk("payment_batches", "released_by_user_id", "users", "released_by"),
        _fk("payment_batches", "cancelled_by_user_id", "users", "cancelled_by"),
        CheckConstraint(f"method IN {PAYMENT_METHODS}", name="ck_payment_batches_method"),
        CheckConstraint(f"status IN {BATCH_STATUSES}", name="ck_payment_batches_status"),
        CheckConstraint(f"submission_channel IS NULL OR submission_channel IN {SUBMISSION_CHANNELS}",
                        name="ck_payment_batches_channel"),
        CheckConstraint("(method = 'BANK_FILE') = (file_format IS NOT NULL)", name="ck_payment_batches_format"),
        CheckConstraint("(method = 'API') = (adapter IS NOT NULL)", name="ck_payment_batches_adapter"),
        CheckConstraint("item_count >= 0 AND total_amount >= 0", name="ck_payment_batches_totals"),
        CheckConstraint("jsonb_typeof(totals) = 'object'", name="ck_payment_batches_totals_shape"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_payment_batches_details"),
        CheckConstraint("status NOT IN ('SUBMITTED', 'PARTIALLY_SETTLED', 'SETTLED', 'RECONCILED') OR "
                        "(released_by_user_id IS NOT NULL AND released_at IS NOT NULL AND submission_channel IS NOT NULL)",
                        name="ck_payment_batches_released"),
        CheckConstraint("(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name="ck_payment_batches_cancelled"),
        _currency("payment_batches"),
        _version("payment_batches"),
    )


class PaymentInstruction(TenantMixin, BaseModel):
    """One payment to one account, with the beneficiary snapshot taken at creation (3.7 §3.4, §8.1)."""

    __tablename__ = "payment_instructions"

    obligation_id = Column(Integer, nullable=False, index=True)
    run_id = Column(Integer, nullable=True, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False, index=True)
    batch_id = Column(Integer, nullable=True, index=True)
    funding_account_id = Column(Integer, nullable=True, index=True)
    bank_account_id = Column(Integer, nullable=True, index=True)
    sequence = Column(Integer, nullable=False, default=1)
    idempotency_key = Column(String(120), nullable=False)
    reference = Column(String(35), nullable=True)
    beneficiary_snapshot = Column(JSONB, nullable=False, default=dict)
    beneficiary_enc = Column(Text, nullable=True)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    account_amount = Column(Numeric(19, 4), nullable=True)
    account_currency = Column(CHAR(3), nullable=True)
    fx_conversion_id = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="NOT_READY")
    status_reason = Column(String(40), nullable=True)
    status_note = Column(Text, nullable=True)
    submission_channel = Column(String(15), nullable=True)
    evidence_status = Column(String(10), nullable=False, default="UNVERIFIED")
    evidence_source = Column(String(15), nullable=True)
    evidence_ref = Column(String(200), nullable=True)
    provider_ref = Column(String(80), nullable=True)
    reissue_of_instruction_id = Column(Integer, nullable=True, index=True)
    reissue_request = Column(JSONB(none_as_null=True), nullable=True)
    expected_settlement_date = Column(Date, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    settled_amount = Column(Numeric(19, 4), nullable=True)
    variance_amount = Column(Numeric(19, 4), nullable=True)
    variance_reason = Column(Text, nullable=True)
    reconciled_at = Column(DateTime(timezone=True), nullable=True)
    returned_at = Column(DateTime(timezone=True), nullable=True)
    failure_code = Column(String(40), nullable=True)
    failure_message = Column(String(300), nullable=True)
    carried_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payment_instructions"),
        UniqueConstraint("company_id", "idempotency_key", name="uq_payment_instructions_idempotency"),
        UniqueConstraint("company_id", "reference", name="uq_payment_instructions_reference"),
        # Instruction identity (3.7 §8.5): one per (obligation, account, sequence); NULL account = none on file yet.
        Index("uq_payment_instructions_identity", "company_id", "obligation_id", text("COALESCE(bank_account_id, 0)"),
              "sequence", unique=True),
        # Provider references are unique across tenants: webhooks resolve their tenant from them.
        Index("uq_payment_instructions_provider_ref", "provider_ref", unique=True,
              postgresql_where=text("provider_ref IS NOT NULL")),
        _fk("payment_instructions", "obligation_id", "payment_obligations", "obligation"),
        _fk("payment_instructions", "run_id", "payroll_runs", "run"),
        _fk("payment_instructions", "employee_id", "employees", "employee"),
        _fk("payment_instructions", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("payment_instructions", "batch_id", "payment_batches", "batch"),
        _fk("payment_instructions", "reissue_of_instruction_id", "payment_instructions", "reissue_of"),
        _fk("payment_instructions", "fx_conversion_id", "fx_conversions", "fx_conversion"),
        CheckConstraint(f"status IN {INSTRUCTION_STATUSES}", name="ck_payment_instructions_status"),
        CheckConstraint(f"submission_channel IS NULL OR submission_channel IN {SUBMISSION_CHANNELS}",
                        name="ck_payment_instructions_channel"),
        CheckConstraint(f"evidence_status IN {EVIDENCE_STATUSES}", name="ck_payment_instructions_evidence"),
        CheckConstraint(f"evidence_source IS NULL OR evidence_source IN {EVIDENCE_SOURCES}",
                        name="ck_payment_instructions_evidence_source"),
        CheckConstraint("amount > 0", name="ck_payment_instructions_amount"),
        CheckConstraint("sequence >= 1", name="ck_payment_instructions_sequence"),
        CheckConstraint("(account_amount IS NULL) = (account_currency IS NULL)", name="ck_payment_instructions_fx"),
        CheckConstraint("jsonb_typeof(beneficiary_snapshot) = 'object'", name="ck_payment_instructions_snapshot"),
        CheckConstraint("reissue_request IS NULL OR jsonb_typeof(reissue_request) = 'object'",
                        name="ck_payment_instructions_reissue_request"),
        CheckConstraint("reissue_of_instruction_id IS NULL OR reissue_of_instruction_id <> id",
                        name="ck_payment_instructions_not_own_reissue"),
        # Contract §23.2 / decision I-17: never "Paid" without verified settlement evidence.
        CheckConstraint("status NOT IN ('SETTLED', 'RECONCILED') OR evidence_status = 'VERIFIED'",
                        name="ck_payment_instructions_settled_evidence"),
        CheckConstraint(f"status NOT IN {SUBMITTED_STATUSES} OR (submission_channel IS NOT NULL "
                        "AND submitted_at IS NOT NULL)", name="ck_payment_instructions_submitted"),
        CheckConstraint("evidence_status = 'UNVERIFIED' OR evidence_source IS NOT NULL",
                        name="ck_payment_instructions_evidence_named"),
        CheckConstraint("status NOT IN ('READY', 'APPROVAL_PENDING', 'APPROVED', 'QUEUED') OR "
                        "(bank_account_id IS NOT NULL AND beneficiary_enc IS NOT NULL)",
                        name="ck_payment_instructions_beneficiary"),
        CheckConstraint("status NOT IN ('APPROVAL_PENDING', 'APPROVED', 'QUEUED') OR batch_id IS NOT NULL",
                        name="ck_payment_instructions_batched"),
        CheckConstraint("submission_channel <> 'SANDBOX' OR provider_ref IS NOT NULL",
                        name="ck_payment_instructions_sandbox_ref"),
        _currency("payment_instructions"),
        _version("payment_instructions"),
    )


class PaymentEvent(TenantMixin, BaseModel):
    """Append-only history of an instruction: every status change and every piece of evidence (3.7 §8.1)."""

    __tablename__ = "payment_events"

    instruction_id = Column(Integer, nullable=False, index=True)
    event_status = Column(String(25), nullable=False)
    outcome = Column(String(10), nullable=False)
    from_status = Column(String(20), nullable=False)
    to_status = Column(String(20), nullable=False)
    source = Column(String(10), nullable=False)
    external_event_id = Column(String(120), nullable=True)
    evidence_ref = Column(String(200), nullable=True)
    sandbox = Column(Boolean, nullable=False, default=False)
    actor_user_id = Column(Integer, nullable=True)
    details = Column(JSONB, nullable=False, default=dict)
    at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        _uq("payment_events"),
        Index("uq_payment_events_external", "company_id", "source", "external_event_id", unique=True,
              postgresql_where=text("external_event_id IS NOT NULL")),
        _fk("payment_events", "instruction_id", "payment_instructions", "instruction"),
        _fk("payment_events", "actor_user_id", "users", "actor"),
        CheckConstraint(f"source IN {EVENT_SOURCES}", name="ck_payment_events_source"),
        CheckConstraint(f"outcome IN {EVENT_OUTCOMES}", name="ck_payment_events_outcome"),
        CheckConstraint(f"from_status IN {INSTRUCTION_STATUSES} AND to_status IN {INSTRUCTION_STATUSES}",
                        name="ck_payment_events_statuses"),
        CheckConstraint("outcome IN ('APPLIED', 'RECORDED') OR from_status = to_status",
                        name="ck_payment_events_unapplied"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_payment_events_details"),
    )


class PaymentFile(TenantMixin, BaseModel):
    """The generated file of a batch: hash and totals only, the bytes are regenerated (3.7 §7.1)."""

    __tablename__ = "payment_files"

    batch_id = Column(Integer, nullable=False)
    format = Column(String(30), nullable=False)
    filename = Column(String(120), nullable=False)
    media_type = Column(String(60), nullable=False)
    storage_path = Column(String(300), nullable=True)
    sha256 = Column(CHAR(64), nullable=False)
    record_count = Column(Integer, nullable=False)
    totals = Column(JSONB, nullable=False, default=dict)
    generated_by_user_id = Column(Integer, nullable=False)
    generated_at = Column(DateTime(timezone=True), nullable=False)
    download_count = Column(Integer, nullable=False, default=0)
    last_downloaded_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        _uq("payment_files"),
        UniqueConstraint("company_id", "batch_id", name="uq_payment_files_batch"),
        _fk("payment_files", "batch_id", "payment_batches", "batch"),
        _fk("payment_files", "generated_by_user_id", "users", "generated_by"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_payment_files_sha256"),
        CheckConstraint("record_count >= 1 AND download_count >= 0", name="ck_payment_files_counts"),
        CheckConstraint("jsonb_typeof(totals) = 'object'", name="ck_payment_files_totals"),
        # Plain account numbers never rest on disk: a file is regenerated for each download.
        CheckConstraint("storage_path IS NULL", name="ck_payment_files_not_stored"),
    )


class BankStatementImport(TenantMixin, BaseModel):
    """One imported statement file of a funding account (3.7 §9); the same file imports once."""

    __tablename__ = "bank_statement_imports"

    funding_account_id = Column(Integer, nullable=False, index=True)
    format = Column(String(10), nullable=False)
    file_name = Column(String(200), nullable=False)
    file_sha256 = Column(CHAR(64), nullable=False)
    statement_ref = Column(String(100), nullable=True)
    account_masked = Column(String(40), nullable=True)
    line_count = Column(Integer, nullable=False, default=0)
    new_line_count = Column(Integer, nullable=False, default=0)
    rejected = Column(JSONB, nullable=False, default=list)
    counts = Column(JSONB, nullable=False, default=dict)
    imported_by_user_id = Column(Integer, nullable=False)

    __table_args__ = (
        _uq("bank_statement_imports"),
        UniqueConstraint("company_id", "funding_account_id", "file_sha256", name="uq_bank_statement_imports_file"),
        _fk("bank_statement_imports", "imported_by_user_id", "users", "imported_by"),
        CheckConstraint(f"format IN {STATEMENT_FORMATS}", name="ck_bank_statement_imports_format"),
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="ck_bank_statement_imports_sha256"),
        CheckConstraint("line_count >= 0 AND new_line_count >= 0 AND new_line_count <= line_count",
                        name="ck_bank_statement_imports_counts"),
        CheckConstraint("jsonb_typeof(rejected) = 'array'", name="ck_bank_statement_imports_rejected"),
        CheckConstraint("jsonb_typeof(counts) = 'object'", name="ck_bank_statement_imports_counts_shape"),
    )


class BankStatementLine(TenantMixin, BaseModel):
    """One statement line; a line seen in an earlier import of the account is not stored again (F-11)."""

    __tablename__ = "bank_statement_lines"

    import_id = Column(Integer, nullable=False, index=True)
    funding_account_id = Column(Integer, nullable=False, index=True)
    source_row = Column(Integer, nullable=False)
    value_date = Column(Date, nullable=False)
    booking_date = Column(Date, nullable=True)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    direction = Column(String(6), nullable=False)
    reference = Column(String(140), nullable=True)
    end_to_end_id = Column(String(140), nullable=True)
    bank_reference = Column(String(140), nullable=True)
    counterparty = Column(String(200), nullable=True)
    counterparty_account_masked = Column(String(40), nullable=True)
    remittance = Column(Text, nullable=True)
    return_reason = Column(String(10), nullable=True)
    line_hash = Column(CHAR(64), nullable=False)

    __table_args__ = (
        _uq("bank_statement_lines"),
        UniqueConstraint("company_id", "funding_account_id", "line_hash", name="uq_bank_statement_lines_hash"),
        _fk("bank_statement_lines", "import_id", "bank_statement_imports", "import", ondelete="CASCADE"),
        CheckConstraint(f"direction IN {DIRECTIONS}", name="ck_bank_statement_lines_direction"),
        CheckConstraint("amount > 0", name="ck_bank_statement_lines_amount"),
        CheckConstraint("line_hash ~ '^[0-9a-f]{64}$'", name="ck_bank_statement_lines_hash"),
        _currency("bank_statement_lines"),
    )


class ReconciliationMatch(TenantMixin, BaseModel):
    """The reconciliation decision for one statement line (3.7 §9 queues)."""

    __tablename__ = "reconciliation_matches"

    statement_line_id = Column(Integer, nullable=False)
    funding_account_id = Column(Integer, nullable=False, index=True)
    instruction_id = Column(Integer, nullable=True, index=True)
    status = Column(String(20), nullable=False)
    reason = Column(String(40), nullable=False)
    message = Column(String(300), nullable=True)
    candidate_ids = Column(JSONB, nullable=False, default=list)
    variance_amount = Column(Numeric(19, 4), nullable=True)
    variance_currency = Column(CHAR(3), nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("reconciliation_matches"),
        UniqueConstraint("company_id", "statement_line_id", name="uq_reconciliation_matches_line"),
        _fk("reconciliation_matches", "statement_line_id", "bank_statement_lines", "line", ondelete="CASCADE"),
        _fk("reconciliation_matches", "instruction_id", "payment_instructions", "instruction"),
        _fk("reconciliation_matches", "decided_by_user_id", "users", "decided_by"),
        CheckConstraint(f"status IN {MATCH_STATUSES}", name="ck_reconciliation_matches_status"),
        CheckConstraint("jsonb_typeof(candidate_ids) = 'array'", name="ck_reconciliation_matches_candidates"),
        CheckConstraint("(variance_amount IS NULL) = (variance_currency IS NULL)",
                        name="ck_reconciliation_matches_variance"),
        CheckConstraint("status NOT IN ('ACCEPTED_VARIANCE', 'LINKED', 'ESCALATED') OR (decided_by_user_id IS NOT "
                        "NULL AND decision_reason IS NOT NULL)", name="ck_reconciliation_matches_decided"),
        CheckConstraint("status NOT IN ('MATCHED', 'RETURN', 'ACCEPTED_VARIANCE', 'LINKED') OR instruction_id IS NOT "
                        "NULL", name="ck_reconciliation_matches_instruction"),
        _version("reconciliation_matches"),
    )


class PaymentManualSettlement(TenantMixin, BaseModel):
    """Proof of a payment made outside the system, reviewed by a second person (3.7 §7.3, EC-PAY-14)."""

    __tablename__ = "payment_manual_settlements"

    instruction_id = Column(Integer, nullable=False, index=True)
    method = Column(String(15), nullable=False)
    evidence_reference = Column(String(100), nullable=False)
    evidence_name = Column(String(200), nullable=False)
    evidence_mime = Column(String(60), nullable=False)
    evidence_storage_path = Column(String(300), nullable=False)
    evidence_sha256 = Column(CHAR(64), nullable=False)
    note = Column(Text, nullable=True)
    status = Column(String(15), nullable=False, default="PENDING_REVIEW")
    submitted_by_user_id = Column(Integer, nullable=False)
    reviewed_by_user_id = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payment_manual_settlements"),
        Index("uq_payment_manual_settlements_pending", "company_id", "instruction_id", unique=True,
              postgresql_where=text("status = 'PENDING_REVIEW'")),
        _fk("payment_manual_settlements", "instruction_id", "payment_instructions", "instruction"),
        _fk("payment_manual_settlements", "submitted_by_user_id", "users", "submitted_by"),
        _fk("payment_manual_settlements", "reviewed_by_user_id", "users", "reviewed_by"),
        CheckConstraint(f"method IN {MANUAL_METHODS}", name="ck_payment_manual_settlements_method"),
        CheckConstraint(f"status IN {MANUAL_STATUSES}", name="ck_payment_manual_settlements_status"),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="ck_payment_manual_settlements_sha256"),
        # Maker-checker: the person who uploads the proof never reviews it.
        CheckConstraint("reviewed_by_user_id IS NULL OR reviewed_by_user_id <> submitted_by_user_id",
                        name="ck_payment_manual_settlements_second_reviewer"),
        CheckConstraint("(status = 'PENDING_REVIEW') = (reviewed_at IS NULL)",
                        name="ck_payment_manual_settlements_reviewed"),
        CheckConstraint("status = 'PENDING_REVIEW' OR reviewed_by_user_id IS NOT NULL",
                        name="ck_payment_manual_settlements_reviewer"),
        _version("payment_manual_settlements"),
    )


class PaymentWebhookEvent(BaseModel):
    """Inbound provider webhook inbox (platform table: no row-level security, system session only).

    ``event_id`` is the provider's event id, set only after the signature and timestamp verified; it is
    unique per adapter, so a replayed or duplicated delivery is processed once (EC-PAY-06, EC-PAY-08).
    Rejected deliveries keep the untrusted id in ``claimed_event_id`` and never block a genuine event.
    No tenant identifiers are stored here; the applied event lives in the tenant's ``payment_events``.
    """

    __tablename__ = "payment_webhook_events"

    adapter = Column(String(20), nullable=False)
    event_id = Column(String(120), nullable=True)
    claimed_event_id = Column(String(120), nullable=True)
    event_type = Column(String(60), nullable=True)
    signed_at = Column(DateTime(timezone=True), nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    outcome = Column(String(12), nullable=False)
    error_code = Column(String(40), nullable=True)
    payload_sha256 = Column(CHAR(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("adapter", "event_id", name="uq_payment_webhook_events_event"),
        CheckConstraint(f"outcome IN {WEBHOOK_OUTCOMES}", name="ck_payment_webhook_events_outcome"),
        CheckConstraint("outcome = 'REJECTED' OR event_id IS NOT NULL", name="ck_payment_webhook_events_verified"),
        CheckConstraint("outcome <> 'REJECTED' OR event_id IS NULL", name="ck_payment_webhook_events_rejected"),
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="ck_payment_webhook_events_sha256"),
    )
