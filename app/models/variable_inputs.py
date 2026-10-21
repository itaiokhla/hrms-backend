"""Wage inputs, reimbursement claims, loans and advances (3.4 §13-§15, 3.1 §5.9-§5.11, 3.9 §3.5).

* A wage input is an amount or units (x rate) of an INPUT or units-based component for one target
  period of the employee's pay group. Pay periods arrive later in the migration chain, so the target
  is a date range (target_period_id is filled once periods exist; no foreign key yet);
  component_code is kept beside component_id for display and imports. external_ref is unique per employee, component and period among
  live inputs (EC-VAR-02). A run reserves an approved input at lock and consumes it at finalization,
  exactly once (EC-VAR-01); run employees are not foreign keys yet either.
* A reimbursement claim is converted into the pay currency when needed, with the conversion stored
  (EC-VAR-09); receipts live in private storage with their SHA-256 and duplicate fingerprint
  (EC-VAR-04).
* A loan or advance keeps its repayment schedule (schedule_version increases on a restructure); the
  balance is derived from the loan_transactions ledger, which is append-only (trigger) and holds at
  most one instalment per run employee (EC-VAR-08). Pause, resume, restructure, prepayment and waiver
  are loan_actions approved through the approvals engine (EC-VAR-06).
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
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin

WAGE_INPUT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "CONSUMED", "REJECTED", "CANCELLED")
LIVE_WAGE_INPUT_STATUSES = ("DRAFT", "PENDING_APPROVAL", "APPROVED", "CONSUMED")
WAGE_INPUT_SOURCES = ("MANUAL", "IMPORT", "TASK", "INCENTIVE_PLAN", "SYSTEM")
IMPORT_STATUSES = ("VALIDATED", "INVALID", "COMMITTING", "COMMITTED", "FAILED")

CLAIM_STATUSES = ("SUBMITTED", "APPROVED", "SCHEDULED", "PAID", "REJECTED")
PAYOUT_ROUTES = ("WITH_PAYROLL", "SEPARATE_PAYMENT")
TAXABILITY = ("TAXABLE", "EXEMPT", "PARTIAL", "UNCLASSIFIED")

LOAN_KINDS = ("LOAN", "ADVANCE")
LOAN_STATUSES = ("PENDING_APPROVAL", "APPROVED", "ACTIVE", "PAUSED", "CLOSED", "REJECTED", "CANCELLED")
OPEN_LOAN_STATUSES = ("PENDING_APPROVAL", "APPROVED", "ACTIVE", "PAUSED")
LOAN_TRANSACTION_KINDS = ("DISBURSEMENT", "INSTALMENT", "PREPAYMENT", "WAIVER", "ADJUSTMENT")
LOAN_ACTIONS = ("PAUSE", "RESUME", "RESTRUCTURE", "PREPAY", "WAIVE")
LOAN_ACTION_STATUSES = ("PENDING_APPROVAL", "APPLIED", "REJECTED", "CANCELLED")


def _currency(table: str, column: str = "currency") -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^[A-Z]{{3}}$'", name=f"ck_{table}_{column}")


def _fk(table: str, columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    remote = [f"{target}.company_id", f"{target}.id"]
    return ForeignKeyConstraint(local, remote, name=f"fk_{table}_{name}", ondelete=ondelete)


class WageInput(TenantMixin, BaseModel):
    """One wage input for one target period (3.4 §13)."""

    __tablename__ = "wage_inputs"

    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    pay_group_id = Column(Integer, nullable=False)
    component_id = Column(Integer, nullable=False)
    component_code = Column(String(40), nullable=False)
    amount = Column(Numeric(19, 4), nullable=True)
    units = Column(Numeric(10, 4), nullable=True)
    rate = Column(Numeric(19, 4), nullable=True)
    currency = Column(CHAR(3), nullable=False)
    # Inputs in another currency than the pay group's are converted at calculation: {"to": "INR"}.
    fx_rule = Column(JSONB(none_as_null=True), nullable=True)
    target_period_id = Column(Integer, nullable=True)
    target_period_start = Column(Date, nullable=False)
    target_period_end = Column(Date, nullable=False)
    reason = Column(Text, nullable=True)
    attachments = Column(JSONB, nullable=False, default=list)
    source = Column(String(20), nullable=False, default="MANUAL")
    status = Column(String(20), nullable=False, default="DRAFT")
    recurrence = Column(JSONB(none_as_null=True), nullable=True)
    recurrence_parent_id = Column(Integer, nullable=True, index=True)
    external_ref = Column(String(100), nullable=True)
    import_id = Column(Integer, nullable=True)
    import_row = Column(Integer, nullable=True)
    flags = Column(JSONB, nullable=False, default=list)
    approval_request_id = Column(Integer, nullable=True)
    reserved_by_run_employee_id = Column(Integer, nullable=True)
    consumed_by_run_employee_id = Column(Integer, nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    status_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_wage_inputs_company_id_id"),
        _fk("wage_inputs", "employee_id", "employees", "employee"),
        _fk("wage_inputs", "pay_group_id", "pay_groups", "pay_group"),
        _fk("wage_inputs", "component_id", "pay_components", "component"),
        _fk("wage_inputs", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("wage_inputs", "recurrence_parent_id", "wage_inputs", "recurrence_parent"),
        _fk("wage_inputs", "import_id", "wage_input_imports", "import"),
        _fk("wage_inputs", "approval_request_id", "approval_requests", "approval_request"),
        _fk("wage_inputs", "created_by_user_id", "users", "created_by"),
        CheckConstraint(f"status IN {WAGE_INPUT_STATUSES}", name="ck_wage_inputs_status"),
        CheckConstraint(f"source IN {WAGE_INPUT_SOURCES}", name="ck_wage_inputs_source"),
        _currency("wage_inputs"),
        CheckConstraint("(amount IS NULL) <> (units IS NULL)", name="ck_wage_inputs_amount_or_units"),
        CheckConstraint("amount IS NULL OR amount <> 0", name="ck_wage_inputs_amount"),
        CheckConstraint("units IS NULL OR units > 0", name="ck_wage_inputs_units"),
        CheckConstraint("rate IS NULL OR (units IS NOT NULL AND rate >= 0)", name="ck_wage_inputs_rate"),
        CheckConstraint("target_period_end >= target_period_start", name="ck_wage_inputs_period"),
        CheckConstraint("fx_rule IS NULL OR jsonb_typeof(fx_rule) = 'object'", name="ck_wage_inputs_fx_rule"),
        CheckConstraint("recurrence IS NULL OR jsonb_typeof(recurrence) = 'object'", name="ck_wage_inputs_recurrence"),
        CheckConstraint("jsonb_typeof(attachments) = 'array'", name="ck_wage_inputs_attachments"),
        CheckConstraint("jsonb_typeof(flags) = 'array'", name="ck_wage_inputs_flags"),
        CheckConstraint("(import_id IS NULL) = (import_row IS NULL)", name="ck_wage_inputs_import_row"),
        CheckConstraint(
            "(status = 'CONSUMED') = (consumed_by_run_employee_id IS NOT NULL)", name="ck_wage_inputs_consumed"
        ),
        CheckConstraint(
            "reserved_by_run_employee_id IS NULL OR status IN ('APPROVED', 'CONSUMED')", name="ck_wage_inputs_reserved"
        ),
        CheckConstraint("row_version >= 1", name="ck_wage_inputs_row_version"),
        Index(
            "uq_wage_inputs_external_ref", "company_id", "employee_id", "component_id", "target_period_start",
            "external_ref", unique=True,
            postgresql_where=text(f"external_ref IS NOT NULL AND status IN {LIVE_WAGE_INPUT_STATUSES}"),
        ),
        Index("uq_wage_inputs_import_row", "import_id", "import_row", unique=True,
              postgresql_where=text("import_id IS NOT NULL")),
        Index("ix_wage_inputs_period", "company_id", "employee_id", "target_period_start"),
        Index("ix_wage_inputs_status", "company_id", "status"),
    )


class WageInputImport(TenantMixin, BaseModel):
    """A CSV import of wage inputs: the staged file, its dry-run result and the commit job (3.4 §13)."""

    __tablename__ = "wage_input_imports"

    file_name = Column(String(200), nullable=False)
    storage_path = Column(String(300), nullable=False)
    sha256 = Column(CHAR(64), nullable=False)
    bytes = Column(BigInteger, nullable=False)
    status = Column(String(12), nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    # Dry-run outcome per row: {"row": 2, "ok": true, "errors": [...], "values": {...}}.
    rows = Column(JSONB, nullable=False, default=list)
    result = Column(JSONB(none_as_null=True), nullable=True)
    job_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    committed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_wage_input_imports_company_id_id"),
        _fk("wage_input_imports", "created_by_user_id", "users", "created_by"),
        CheckConstraint(f"status IN {IMPORT_STATUSES}", name="ck_wage_input_imports_status"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_wage_input_imports_sha256"),
        CheckConstraint("bytes > 0 AND row_count >= 0 AND error_count >= 0", name="ck_wage_input_imports_counts"),
        CheckConstraint("jsonb_typeof(rows) = 'array'", name="ck_wage_input_imports_rows"),
    )


class ReimbursementClaim(TenantMixin, BaseModel):
    """An expense claim (3.4 §14). amount/currency are as claimed; payout_amount is in the pay currency."""

    __tablename__ = "reimbursement_claims"

    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    pay_group_id = Column(Integer, nullable=False)
    policy_version_id = Column(Integer, nullable=False)
    claim_type = Column(String(40), nullable=False)
    claim_type_name = Column(String(100), nullable=False)
    taxability = Column(String(12), nullable=False, default="UNCLASSIFIED")
    expense_date = Column(Date, nullable=False)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    merchant = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    payout_route = Column(String(20), nullable=False)
    payout_amount = Column(Numeric(19, 4), nullable=False)
    payout_currency = Column(CHAR(3), nullable=False)
    approved_amount = Column(Numeric(19, 4), nullable=True)
    fx_conversion_id = Column(Integer, nullable=True)
    status = Column(String(12), nullable=False, default="SUBMITTED")
    flags = Column(JSONB, nullable=False, default=list)
    approval_request_id = Column(Integer, nullable=True)
    scheduled_ref = Column(String(100), nullable=True)
    paid_ref = Column(String(100), nullable=True)
    submitted_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    status_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    receipts = relationship(
        "ReimbursementReceipt",
        primaryjoin="ReimbursementClaim.id == ReimbursementReceipt.claim_id",
        foreign_keys="ReimbursementReceipt.claim_id",
        order_by="ReimbursementReceipt.id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_reimbursement_claims_company_id_id"),
        _fk("reimbursement_claims", "employee_id", "employees", "employee"),
        _fk("reimbursement_claims", "pay_group_id", "pay_groups", "pay_group"),
        _fk("reimbursement_claims", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("reimbursement_claims", "policy_version_id", "policy_versions", "policy_version"),
        _fk("reimbursement_claims", "fx_conversion_id", "fx_conversions", "fx_conversion"),
        _fk("reimbursement_claims", "approval_request_id", "approval_requests", "approval_request"),
        _fk("reimbursement_claims", "submitted_by_user_id", "users", "submitted_by"),
        CheckConstraint(f"status IN {CLAIM_STATUSES}", name="ck_reimbursement_claims_status"),
        CheckConstraint(f"payout_route IN {PAYOUT_ROUTES}", name="ck_reimbursement_claims_payout_route"),
        CheckConstraint(f"taxability IN {TAXABILITY}", name="ck_reimbursement_claims_taxability"),
        _currency("reimbursement_claims"),
        _currency("reimbursement_claims", "payout_currency"),
        CheckConstraint("amount > 0 AND payout_amount > 0", name="ck_reimbursement_claims_amount"),
        CheckConstraint(
            "approved_amount IS NULL OR (approved_amount > 0 AND approved_amount <= payout_amount)",
            name="ck_reimbursement_claims_approved_amount",
        ),
        CheckConstraint(
            "(currency = payout_currency) = (fx_conversion_id IS NULL)", name="ck_reimbursement_claims_fx"
        ),
        CheckConstraint(
            "status NOT IN ('APPROVED', 'SCHEDULED', 'PAID') OR approved_amount IS NOT NULL",
            name="ck_reimbursement_claims_approved",
        ),
        CheckConstraint("(status = 'SCHEDULED') = (scheduled_ref IS NOT NULL)", name="ck_reimbursement_claims_scheduled"),
        CheckConstraint("(status = 'PAID') = (paid_ref IS NOT NULL)", name="ck_reimbursement_claims_paid"),
        CheckConstraint("jsonb_typeof(flags) = 'array'", name="ck_reimbursement_claims_flags"),
        CheckConstraint("row_version >= 1", name="ck_reimbursement_claims_row_version"),
        Index("ix_reimbursement_claims_employee_type", "company_id", "employee_id", "claim_type", "expense_date"),
        Index("ix_reimbursement_claims_status", "company_id", "status"),
    )


class ReimbursementReceipt(TenantMixin, BaseModel):
    """A receipt file of a claim in private storage (app.core.storage, category claim-receipts)."""

    __tablename__ = "reimbursement_receipts"

    claim_id = Column(Integer, nullable=False, index=True)
    file_name = Column(String(200), nullable=False)
    storage_path = Column(String(300), nullable=False)
    sha256 = Column(CHAR(64), nullable=False)
    # claim_fingerprint(receipt, amount, currency, expense date, merchant) for duplicate detection.
    fingerprint = Column(CHAR(64), nullable=False)
    mime = Column(String(100), nullable=False)
    bytes = Column(BigInteger, nullable=False)
    uploaded_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_reimbursement_receipts_company_id_id"),
        UniqueConstraint("claim_id", "sha256", name="uq_reimbursement_receipts_file"),
        _fk("reimbursement_receipts", "claim_id", "reimbursement_claims", "claim", ondelete="CASCADE"),
        _fk("reimbursement_receipts", "uploaded_by_user_id", "users", "uploaded_by"),
        CheckConstraint("bytes > 0", name="ck_reimbursement_receipts_bytes"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$' AND fingerprint ~ '^[0-9a-f]{64}$'",
                        name="ck_reimbursement_receipts_hashes"),
        Index("ix_reimbursement_receipts_sha256", "company_id", "sha256"),
        Index("ix_reimbursement_receipts_fingerprint", "company_id", "fingerprint"),
    )


class Loan(TenantMixin, BaseModel):
    """A loan or salary advance with its current repayment schedule (3.4 §15).

    tenure is months for a loan and runs for an advance. schedule is the RepaymentSchedule of
    app.payroll.time_pay_loans as JSON; after a restructure its principal is the balance carried
    into the new schedule (opening_balance) and schedule_version counts the schedules.
    """

    __tablename__ = "loans"

    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    pay_group_id = Column(Integer, nullable=False)
    kind = Column(String(8), nullable=False)
    principal = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    purpose = Column(Text, nullable=True)
    tenure = Column(Integer, nullable=False)
    interest = Column(JSONB, nullable=False, default=dict)
    schedule = Column(JSONB, nullable=False)
    schedule_version = Column(Integer, nullable=False, default=1)
    opening_balance = Column(Numeric(19, 4), nullable=True)
    recover_balance_on_exit = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="PENDING_APPROVAL")
    policy_version_id = Column(Integer, nullable=False)
    application_issues = Column(JSONB, nullable=False, default=list)
    approval_request_id = Column(Integer, nullable=True)
    applied_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    disbursed_on = Column(Date, nullable=True)
    disbursement_ref = Column(String(100), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    status_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_loans_company_id_id"),
        _fk("loans", "employee_id", "employees", "employee"),
        _fk("loans", "pay_group_id", "pay_groups", "pay_group"),
        _fk("loans", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("loans", "policy_version_id", "policy_versions", "policy_version"),
        _fk("loans", "approval_request_id", "approval_requests", "approval_request"),
        _fk("loans", "applied_by_user_id", "users", "applied_by"),
        CheckConstraint(f"kind IN {LOAN_KINDS}", name="ck_loans_kind"),
        CheckConstraint(f"status IN {LOAN_STATUSES}", name="ck_loans_status"),
        _currency("loans"),
        CheckConstraint("principal > 0", name="ck_loans_principal"),
        CheckConstraint("tenure >= 1 AND (kind = 'LOAN' OR tenure <= 3)", name="ck_loans_tenure"),
        CheckConstraint("schedule_version >= 1 AND (schedule_version = 1) = (opening_balance IS NULL)",
                        name="ck_loans_schedule_version"),
        CheckConstraint("jsonb_typeof(schedule) = 'object' AND jsonb_typeof(interest) = 'object'",
                        name="ck_loans_json"),
        CheckConstraint(
            "status NOT IN ('ACTIVE', 'PAUSED', 'CLOSED') OR disbursed_on IS NOT NULL", name="ck_loans_disbursed"
        ),
        CheckConstraint("row_version >= 1", name="ck_loans_row_version"),
        Index("ix_loans_employee_status", "company_id", "employee_id", "status"),
    )


class LoanAction(TenantMixin, BaseModel):
    """A requested pause, resume, restructure, prepayment or waiver; applied when approved (EC-VAR-06)."""

    __tablename__ = "loan_actions"

    loan_id = Column(Integer, nullable=False, index=True)
    action = Column(String(12), nullable=False)
    # RESTRUCTURE: {"tenure": n}; PREPAY / WAIVE: {"amount": "...", "reference": "..."}.
    params = Column(JSONB, nullable=False, default=dict)
    reason = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="PENDING_APPROVAL")
    approval_request_id = Column(Integer, nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    status_reason = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_loan_actions_company_id_id"),
        _fk("loan_actions", "loan_id", "loans", "loan"),
        _fk("loan_actions", "approval_request_id", "approval_requests", "approval_request"),
        _fk("loan_actions", "requested_by_user_id", "users", "requested_by"),
        CheckConstraint(f"action IN {LOAN_ACTIONS}", name="ck_loan_actions_action"),
        CheckConstraint(f"status IN {LOAN_ACTION_STATUSES}", name="ck_loan_actions_status"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="ck_loan_actions_params"),
        CheckConstraint("(status = 'PENDING_APPROVAL') = (decided_at IS NULL)", name="ck_loan_actions_decided"),
        Index("uq_loan_actions_one_pending", "company_id", "loan_id", unique=True,
              postgresql_where=text("status = 'PENDING_APPROVAL'")),
    )


class LoanTransaction(TenantMixin, BaseModel):
    """A row of the loan ledger (append-only). Amounts are positive; an ADJUSTMENT is signed (positive adds
    to the balance). An INSTALMENT names its run employee: at most one per (loan, run employee) (EC-VAR-08)."""

    __tablename__ = "loan_transactions"

    loan_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(12), nullable=False)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    schedule_version = Column(Integer, nullable=False, default=1)
    run_employee_id = Column(Integer, nullable=True)
    # The scheduled part of an instalment (what was due before protected-net carry-forward).
    scheduled_amount = Column(Numeric(19, 4), nullable=True)
    effective_on = Column(Date, nullable=False)
    reference = Column(String(100), nullable=True)
    note = Column(Text, nullable=True)
    action_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_loan_transactions_company_id_id"),
        _fk("loan_transactions", "loan_id", "loans", "loan"),
        _fk("loan_transactions", "action_id", "loan_actions", "action"),
        _fk("loan_transactions", "created_by_user_id", "users", "created_by"),
        CheckConstraint(f"kind IN {LOAN_TRANSACTION_KINDS}", name="ck_loan_transactions_kind"),
        _currency("loan_transactions"),
        CheckConstraint(
            "(kind = 'ADJUSTMENT' AND amount <> 0) OR (kind = 'INSTALMENT' AND amount >= 0) "
            "OR (kind IN ('DISBURSEMENT', 'PREPAYMENT', 'WAIVER') AND amount > 0)",
            name="ck_loan_transactions_amount",
        ),
        CheckConstraint("(kind = 'INSTALMENT') = (run_employee_id IS NOT NULL)", name="ck_loan_transactions_run"),
        CheckConstraint("scheduled_amount IS NULL OR (kind = 'INSTALMENT' AND scheduled_amount >= 0)",
                        name="ck_loan_transactions_scheduled"),
        CheckConstraint("schedule_version >= 1", name="ck_loan_transactions_schedule_version"),
        Index("uq_loan_transactions_instalment", "loan_id", "run_employee_id", unique=True,
              postgresql_where=text("kind = 'INSTALMENT'")),
        Index("uq_loan_transactions_disbursement", "loan_id", unique=True,
              postgresql_where=text("kind = 'DISBURSEMENT'")),
    )
