"""Payroll v2 models (see docs/payroll/implementation/ARCHITECTURE.md): payee bank accounts, their change
control and verification, and company funding accounts (3.7 §2-§6, §12; 3.9 §3.8).

Payment obligations, batches, instructions and reconciliation live in app/models/payment_flows.py.

* ``bank_accounts``: one row per account of a payee (employees now; ``payee_type`` keeps room for
  contractors and nominees). Account numbers and IBANs are stored only as envelope ciphertexts
  (app.payroll.services.keys, purpose-bound and tied to the row by associated data) with a separate
  ``last4`` for display and a per-tenant keyed ``fingerprint`` for duplicate detection. Rows are never
  deleted and the identifying details never change (trigger): a change is a new account that replaces
  the old one. ``usable_from`` ends the cooling-off period and ``ended_at`` is when the account stopped
  serving (deactivated, or replaced once its replacement became usable), so history answers "which
  account was paid on day X" without status guessing.
* ``bank_account_change_requests``: one per submitted account (the change channel, step-up time,
  IP address and device, risk flags, alert recipients and the decision). At most one change per payee
  is open at a time (partial unique index), which keeps velocity checks and approvals race-free.
* ``bank_verifications``: append-only evidence of every verification event (format check, document
  review with evidence in private storage, sandbox penny drop or name match, payment returns, manual
  reviews).
* ``funding_accounts``: company accounts per legal entity that fund payment batches (encrypted
  details, currency, purpose, signatories, supported file formats).
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.models.base import BaseModel, TenantMixin

PAYEE_TYPES = ("EMPLOYEE", "CONTRACTOR", "NOMINEE")
BANK_ACCOUNT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "ACTIVE", "INACTIVE")
VERIFICATION_STATUSES = ("NOT_SUBMITTED", "PENDING", "VERIFIED", "FAILED", "EXPIRED", "REQUIRES_REVIEW")
VERIFICATION_METHODS = ("FORMAT", "DOCUMENT_REVIEW", "PENNY_DROP", "PROVIDER_NAME_MATCH")
# Events recorded in bank_verifications: the methods plus payment returns and manual reviews.
VERIFICATION_EVENT_METHODS = VERIFICATION_METHODS + ("PAYMENT_RETURN", "MANUAL_REVIEW")
VERIFICATION_RESULTS = ("PASSED", "FAILED", "EVIDENCE_SUBMITTED", "FLAGGED", "CLEARED", "RESUBMITTED")
ACCOUNT_TYPES = ("CHECKING", "CURRENT", "SAVINGS")
ALLOCATION_TYPES = ("PRIMARY", "PERCENT", "FIXED", "REMAINDER")
HOLD_REASONS = ("DUPLICATE_FINGERPRINT",)

CHANGE_CHANNELS = ("SELF", "HR", "BUREAU")
CHANGE_KINDS = ("ADD", "REPLACE")
CHANGE_STATUSES = ("SECURITY_REVIEW", "PENDING_APPROVAL", "APPLIED", "REJECTED", "CANCELLED")
OPEN_CHANGE_STATUSES = ("SECURITY_REVIEW", "PENDING_APPROVAL")

FUNDING_PURPOSES = ("PAYROLL", "STATUTORY", "CONTRACTORS", "REIMBURSEMENTS")
FUNDING_STATUSES = ("ACTIVE", "INACTIVE")


def _fk(table: str, columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    remote = [f"{target}.company_id", f"{target}.id"]
    return ForeignKeyConstraint(local, remote, name=f"fk_{table}_{name}", ondelete=ondelete)


def _sql_list(values) -> str:
    """``('A', 'B')`` for a CHECK constraint; never a Python tuple repr (a 1-tuple renders ``('A',)``)."""
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def _in(table: str, column: str, values) -> CheckConstraint:
    return CheckConstraint(f"{column} IN {_sql_list(values)}", name=f"ck_{table}_{column}")


class BankAccount(TenantMixin, BaseModel):
    """A payee's bank account (3.7 §2.2); numbers only as ciphertexts, masked everywhere else."""

    __tablename__ = "bank_accounts"

    payee_type = Column(String(12), nullable=False, default="EMPLOYEE")
    payee_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=True, index=True)  # payee_type EMPLOYEE
    holder_name = Column(String(140), nullable=False)
    country = Column(CHAR(2), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    account_number_enc = Column(Text, nullable=True)
    iban_enc = Column(Text, nullable=True)
    last4 = Column(String(4), nullable=False, default="")
    routing = Column(JSONB, nullable=False, default=dict)  # public identifiers only (IFSC, sort code, BIC...)
    bank_name = Column(String(140), nullable=True)
    branch_name = Column(String(140), nullable=True)
    bank_address = Column(String(300), nullable=True)
    account_type = Column(String(10), nullable=True)
    allocation = Column(JSONB, nullable=False, default=dict)  # {type, value (decimal string), order}
    status = Column(String(20), nullable=False, default="DRAFT")
    verification_status = Column(String(20), nullable=False, default="NOT_SUBMITTED")
    verification_method = Column(String(30), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    usable_from = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)
    fingerprint = Column(String(80), nullable=False)
    fingerprint_version = Column(SmallInteger, nullable=False, default=1)
    # [{"code", "open", "at", "detail"}]: DUPLICATE_ACCOUNT, HOLDER_NAME_MISMATCH, PAYMENT_RETURNED
    risk_flags = Column(JSONB, nullable=False, default=list)
    payment_hold = Column(Boolean, nullable=False, default=False)
    hold_reason = Column(String(40), nullable=True)
    replaces_account_id = Column(Integer, nullable=True)
    replaced_by_account_id = Column(Integer, nullable=True)
    first_paid_at = Column(DateTime(timezone=True), nullable=True)
    last_failed_at = Column(DateTime(timezone=True), nullable=True)
    failure_reason = Column(String(200), nullable=True)
    status_reason = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_bank_accounts_company_id_id"),
        _fk("bank_accounts", "employee_id", "employees", "employee"),
        _fk("bank_accounts", "replaces_account_id", "bank_accounts", "replaces"),
        _fk("bank_accounts", "replaced_by_account_id", "bank_accounts", "replaced_by"),
        _fk("bank_accounts", "created_by_user_id", "users", "created_by"),
        _fk("bank_accounts", "approved_by_user_id", "users", "approved_by"),
        _in("bank_accounts", "payee_type", PAYEE_TYPES),
        _in("bank_accounts", "status", BANK_ACCOUNT_STATUSES),
        _in("bank_accounts", "verification_status", VERIFICATION_STATUSES),
        CheckConstraint(f"verification_method IS NULL OR verification_method IN {_sql_list(VERIFICATION_METHODS)}",
                        name="ck_bank_accounts_verification_method"),
        CheckConstraint(f"account_type IS NULL OR account_type IN {_sql_list(ACCOUNT_TYPES)}", name="ck_bank_accounts_account_type"),
        CheckConstraint(f"hold_reason IS NULL OR hold_reason IN {_sql_list(HOLD_REASONS)}", name="ck_bank_accounts_hold_reason"),
        CheckConstraint("(payee_type = 'EMPLOYEE') = (employee_id IS NOT NULL)", name="ck_bank_accounts_employee"),
        CheckConstraint("employee_id IS NULL OR employee_id = payee_id", name="ck_bank_accounts_payee"),
        CheckConstraint("account_number_enc IS NOT NULL OR iban_enc IS NOT NULL", name="ck_bank_accounts_number"),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name="ck_bank_accounts_country"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_bank_accounts_currency"),
        CheckConstraint("last4 ~ '^[A-Z0-9]{0,4}$'", name="ck_bank_accounts_last4"),
        CheckConstraint("jsonb_typeof(routing) = 'object'", name="ck_bank_accounts_routing"),
        CheckConstraint("jsonb_typeof(allocation) = 'object'", name="ck_bank_accounts_allocation"),
        CheckConstraint("jsonb_typeof(risk_flags) = 'array'", name="ck_bank_accounts_risk_flags"),
        CheckConstraint("payment_hold = (hold_reason IS NOT NULL)", name="ck_bank_accounts_hold"),
        CheckConstraint("status NOT IN ('ACTIVE', 'INACTIVE') OR (approved_at IS NOT NULL AND usable_from IS NOT NULL)",
                        name="ck_bank_accounts_approved"),
        CheckConstraint("(status = 'INACTIVE') = (ended_at IS NOT NULL)", name="ck_bank_accounts_ended"),
        CheckConstraint("effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
                        name="ck_bank_accounts_effective"),
        CheckConstraint("row_version >= 1", name="ck_bank_accounts_row_version"),
        Index("ix_bank_accounts_fingerprint", "company_id", "fingerprint"),
        Index("ix_bank_accounts_payee", "company_id", "payee_type", "payee_id"),
    )


class BankAccountChangeRequest(TenantMixin, BaseModel):
    """One submitted bank account change and its diversion defences (3.7 §3.2)."""

    __tablename__ = "bank_account_change_requests"

    bank_account_id = Column(Integer, nullable=False, index=True)
    payee_type = Column(String(12), nullable=False, default="EMPLOYEE")
    payee_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=True, index=True)
    kind = Column(String(10), nullable=False, default="ADD")
    replaces_account_id = Column(Integer, nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    channel = Column(String(10), nullable=False)
    step_up_at = Column(DateTime(timezone=True), nullable=True)
    step_up_method = Column(String(20), nullable=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)
    risk_flags = Column(JSONB, nullable=False, default=list)  # [{"code", "detail"}]
    contact_emails = Column(JSONB, nullable=False, default=list)  # alert recipients known at submission
    status = Column(String(20), nullable=False)
    approval_request_id = Column(Integer, nullable=True)
    security_reviewed_by_user_id = Column(Integer, nullable=True)
    security_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    security_review_reason = Column(Text, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_reason = Column(Text, nullable=True)
    cooling_off_waived_reason = Column(Text, nullable=True)
    cooling_off_waived_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_bank_account_change_requests_company_id_id"),
        _fk("bank_account_change_requests", "bank_account_id", "bank_accounts", "account"),
        _fk("bank_account_change_requests", "replaces_account_id", "bank_accounts", "replaces"),
        _fk("bank_account_change_requests", "employee_id", "employees", "employee"),
        _fk("bank_account_change_requests", "requested_by_user_id", "users", "requested_by"),
        _fk("bank_account_change_requests", "decided_by_user_id", "users", "decided_by"),
        _fk("bank_account_change_requests", "security_reviewed_by_user_id", "users", "security_reviewed_by"),
        _fk("bank_account_change_requests", "cooling_off_waived_by_user_id", "users", "waived_by"),
        _fk("bank_account_change_requests", "approval_request_id", "approval_requests", "approval_request"),
        _in("bank_account_change_requests", "payee_type", PAYEE_TYPES),
        _in("bank_account_change_requests", "kind", CHANGE_KINDS),
        _in("bank_account_change_requests", "channel", CHANGE_CHANNELS),
        _in("bank_account_change_requests", "status", CHANGE_STATUSES),
        CheckConstraint("(payee_type = 'EMPLOYEE') = (employee_id IS NOT NULL)",
                        name="ck_bank_account_change_requests_employee"),
        CheckConstraint("(kind = 'REPLACE') = (replaces_account_id IS NOT NULL)",
                        name="ck_bank_account_change_requests_replaces"),
        CheckConstraint("jsonb_typeof(risk_flags) = 'array'", name="ck_bank_account_change_requests_risk_flags"),
        CheckConstraint("jsonb_typeof(contact_emails) = 'array'", name="ck_bank_account_change_requests_contacts"),
        CheckConstraint("row_version >= 1", name="ck_bank_account_change_requests_row_version"),
        Index("uq_bank_account_change_requests_open", "company_id", "payee_type", "payee_id", unique=True,
              postgresql_where=text(f"status IN {_sql_list(OPEN_CHANGE_STATUSES)}")),
        Index("ix_bank_account_change_requests_payee", "company_id", "payee_type", "payee_id", "created_at"),
    )


class BankVerification(TenantMixin, BaseModel):
    """One verification event of a bank account (append-only, 3.7 §4)."""

    __tablename__ = "bank_verifications"

    bank_account_id = Column(Integer, nullable=False, index=True)
    method = Column(String(30), nullable=False)
    result = Column(String(20), nullable=False)
    provider = Column(String(20), nullable=True)  # SANDBOX for simulated provider checks
    provider_ref = Column(String(80), nullable=True)
    name_match_score = Column(Numeric(5, 4), nullable=True)
    evidence_of_id = Column(Integer, nullable=True)  # the EVIDENCE_SUBMITTED row a review decided
    evidence_storage_path = Column(String(255), nullable=True)
    evidence_sha256 = Column(CHAR(64), nullable=True)
    evidence_mime = Column(String(50), nullable=True)
    evidence_bytes = Column(Integer, nullable=True)
    evidence_file_name = Column(String(200), nullable=True)
    reason = Column(Text, nullable=True)
    details = Column(JSONB, nullable=False, default=dict)
    performed_by_user_id = Column(Integer, nullable=True)
    at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_bank_verifications_company_id_id"),
        _fk("bank_verifications", "bank_account_id", "bank_accounts", "account"),
        _fk("bank_verifications", "evidence_of_id", "bank_verifications", "evidence_of"),
        _fk("bank_verifications", "performed_by_user_id", "users", "performed_by"),
        _in("bank_verifications", "method", VERIFICATION_EVENT_METHODS),
        _in("bank_verifications", "result", VERIFICATION_RESULTS),
        CheckConstraint("(result = 'EVIDENCE_SUBMITTED') = (evidence_storage_path IS NOT NULL)",
                        name="ck_bank_verifications_evidence"),
        CheckConstraint("evidence_sha256 IS NULL OR evidence_sha256 ~ '^[0-9a-f]{64}$'",
                        name="ck_bank_verifications_sha256"),
        CheckConstraint("name_match_score IS NULL OR (name_match_score >= 0 AND name_match_score <= 1)",
                        name="ck_bank_verifications_score"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_bank_verifications_details"),
    )


class FundingAccount(TenantMixin, BaseModel):
    """A company account of a legal entity that funds payment batches (3.7 §6)."""

    __tablename__ = "funding_accounts"

    legal_entity_id = Column(Integer, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    holder_name = Column(String(140), nullable=False)
    country = Column(CHAR(2), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    account_number_enc = Column(Text, nullable=True)
    iban_enc = Column(Text, nullable=True)
    last4 = Column(String(4), nullable=False, default="")
    routing = Column(JSONB, nullable=False, default=dict)
    bank_name = Column(String(140), nullable=True)
    bank_address = Column(String(300), nullable=True)
    account_type = Column(String(10), nullable=True)
    fingerprint = Column(String(80), nullable=False)
    purpose = Column(String(20), nullable=False, default="PAYROLL")
    signatory_user_ids = Column(ARRAY(Integer), nullable=False, default=list)
    formats = Column(ARRAY(String(30)), nullable=False, default=list)
    provider_profile = Column(String(40), nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    status_reason = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_funding_accounts_company_id_id"),
        _fk("funding_accounts", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("funding_accounts", "created_by_user_id", "users", "created_by"),
        _in("funding_accounts", "purpose", FUNDING_PURPOSES),
        _in("funding_accounts", "status", FUNDING_STATUSES),
        CheckConstraint(f"account_type IS NULL OR account_type IN {_sql_list(ACCOUNT_TYPES)}",
                        name="ck_funding_accounts_account_type"),
        CheckConstraint("account_number_enc IS NOT NULL OR iban_enc IS NOT NULL", name="ck_funding_accounts_number"),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name="ck_funding_accounts_country"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_funding_accounts_currency"),
        CheckConstraint("last4 ~ '^[A-Z0-9]{0,4}$'", name="ck_funding_accounts_last4"),
        CheckConstraint("jsonb_typeof(routing) = 'object'", name="ck_funding_accounts_routing"),
        CheckConstraint("cardinality(signatory_user_ids) >= 1", name="ck_funding_accounts_signatories"),
        CheckConstraint("row_version >= 1", name="ck_funding_accounts_row_version"),
        Index("uq_funding_accounts_active_fingerprint", "company_id", "legal_entity_id", "fingerprint", unique=True,
              postgresql_where=text("status = 'ACTIVE'")),
    )
