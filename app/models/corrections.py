"""Corrections of finalized payroll: retro arrears, reversals and final settlements (3.5 §12, §13, §15;
3.3 §8.3; 3.4 §16; migration 0025_corrections).

Finalized runs are never edited (EC-RUN-17); every correction is a new record linked to the original:

* ``arrears_proposals``: the retro delta of one finalized run employee. The affected period is
  recalculated by the engine from the original frozen snapshot with ONLY the changed sources replaced
  (``snapshot`` keeps that recalculated input, ``changed_sources`` says what was replaced) and compared
  per component with the corrected view (the stored lines plus earlier approved deltas of the period).
  ``trigger`` names the source kind; ``base_proposal_id`` is the earlier approved delta whose snapshot
  was the starting point; a newer change of a period with a pending proposal supersedes it
  (``superseded_by_id``). Status PENDING_APPROVAL -> APPROVED -> SETTLED, or REJECTED / CANCELLED /
  SUPERSEDED. One pending proposal per original run employee (partial unique index).
* ``arrears_proposal_lines``: one component delta (ARREARS when positive, RECOVERY when negative) with
  the original and recalculated amounts, the component's tags (arrears lines inherit them) and the
  original period reference.
* ``arrears_instalments``: what a paying run actually pays or recovers. A positive delta has one
  instalment; a recovery larger than one run can absorb within the protected net pay is split into
  several (EC-COR-05). The snapshot arrears hook offers the next unsettled instalment of each approved
  line to the paying run (item id = instalment id); finalization consumes it exactly once through
  ``payroll_line_sources`` (source type ARREARS, EC-VAR-01).
* ``payroll_reversals``: the request to reverse a finalized run (all or selected employees), approved
  by two different approvers other than the requester (3.5 §4.1 REVERSE, guard TWO_APPROVERS), which
  creates the REVERSAL run of exact negative lines; its finalization cancels unsubmitted payment
  instructions (EC-COR-04) and turns paid ones into recovery obligations, never a bank debit (EC-COR-03).
* ``final_settlement_cases``: one per employment period end (EC-COR-09: a rehire is a new employment
  period, keyed by ``employment_start``), with the termination type, payee (DEATH: nominee or legal
  heir), hold (ABSCONDING), due date and the FINAL_SETTLEMENT run.
* ``final_settlement_items``: the checklist (salary to the last day, notice, leave encashment, negative
  leave balance, gratuity, pending arrears/claims/inputs, loans, assets) with signed amounts
  (positive = paid to the payee, negative = recovered) and the issues that block confirmation.
* ``final_settlement_deadlines``: the configurable company (or legal-entity) deadline used when no
  verified pack rule gives the due date (shown with a "not verified" note, EC-COR-08).
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

ARREARS_TRIGGERS = (
    "COMPENSATION", "ATTENDANCE", "LEAVE", "OVERTIME", "TASK", "SETTING", "POLICY", "INPUT", "REVERSAL", "MANUAL",
)
ARREARS_STATUSES = ("PENDING_APPROVAL", "APPROVED", "SETTLED", "REJECTED", "CANCELLED", "SUPERSEDED")
LIVE_ARREARS_STATUSES = ("PENDING_APPROVAL", "APPROVED", "SETTLED")
ARREARS_DIRECTIONS = ("ARREARS", "RECOVERY")
ARREARS_PAYMENT_MODES = ("NEXT_REGULAR_RUN", "OFF_CYCLE")
ARREARS_RECOVERY_MODES = ("NEXT_RUN", "INSTALMENTS", "MANUAL")
INSTALMENT_STATUSES = ("PLANNED", "SETTLED", "CANCELLED")

REVERSAL_STATUSES = ("PENDING_APPROVAL", "APPROVED", "FINALIZED", "REJECTED", "CANCELLED")

TERMINATION_TYPES = (
    "RESIGNATION", "TERMINATION_WITH_CAUSE", "TERMINATION_WITHOUT_CAUSE", "REDUNDANCY", "RETIREMENT",
    "CONTRACT_END", "DEATH", "ABSCONDING",
)
SETTLEMENT_STATUSES = ("OPEN", "READY", "IN_RUN", "SETTLED", "CANCELLED")
DUE_BASES = ("PACK_VERIFIED", "COMPANY_DEADLINE", "NOT_CONFIGURED")
SETTLEMENT_ITEM_TYPES = (
    "SALARY_TO_LAST_DAY", "NOTICE_PAY", "LEAVE_ENCASHMENT", "NEGATIVE_LEAVE_BALANCE", "GRATUITY",
    "STATUTORY_BONUS", "PENDING_ARREARS", "PENDING_CLAIMS", "PENDING_INPUTS", "LOAN_RECOVERY", "ASSET_RECOVERY",
    "TAX",
)
SETTLEMENT_ITEM_STATUSES = ("PENDING", "CONFIRMED", "WAIVED", "NOT_APPLICABLE", "BLOCKED")
DEADLINE_BASES = ("CALENDAR_DAYS", "WORKING_DAYS")


def _fk(table: str, columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    remote = [f"{target}.company_id", f"{target}.id"]
    return ForeignKeyConstraint(local, remote, name=f"fk_{table}_{name}", ondelete=ondelete)


def _uq(table: str) -> UniqueConstraint:
    return UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id")


def _currency(table: str, column: str = "currency") -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^[A-Z]{{3}}$'", name=f"ck_{table}_{column}")


def _row_version(table: str) -> CheckConstraint:
    return CheckConstraint("row_version >= 1", name=f"ck_{table}_row_version")


class ArrearsProposal(TenantMixin, BaseModel):
    """The retro delta of one finalized run employee, proposed for approval (3.3 §8.3, 3.5 §12)."""

    __tablename__ = "arrears_proposals"

    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    pay_group_id = Column(Integer, nullable=False)
    original_run_id = Column(Integer, nullable=False, index=True)
    original_run_employee_id = Column(Integer, nullable=False, index=True)
    original_period_id = Column(Integer, nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    currency = Column(CHAR(3), nullable=False)
    trigger = Column(String(20), nullable=False)
    # {"type": "compensation_assignment", "id": 12} or {"type": "source_change_log", "ids": [...]}.
    trigger_ref = Column(JSONB, nullable=False, default=dict)
    # What replaced the original frozen input: [{"type": "COMPENSATION", "assignment_ids": [...]},
    # {"type": "ATTENDANCE", "dates": [...]}, {"type": "SETTING", "keys": [...]}, ...].
    changed_sources = Column(JSONB, nullable=False, default=list)
    base_proposal_id = Column(Integer, nullable=True)
    superseded_by_id = Column(Integer, nullable=True)
    snapshot = Column(JSONB(none_as_null=True), nullable=True)
    snapshot_hash = Column(CHAR(64), nullable=True)
    calc_hash = Column(CHAR(64), nullable=True)
    engine_version = Column(String(20), nullable=True)
    # {"arrears": "…", "recovery": "…", "net_change": "…"} in ``currency`` (never across currencies).
    totals = Column(JSONB, nullable=False, default=dict)
    issues = Column(JSONB, nullable=False, default=list)
    status = Column(String(20), nullable=False, default="PENDING_APPROVAL")
    payment_mode = Column(String(20), nullable=True)
    recovery_mode = Column(String(20), nullable=True)
    retro_run_id = Column(Integer, nullable=True)
    approval_request_id = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("arrears_proposals"),
        _fk("arrears_proposals", "employee_id", "employees", "employee"),
        _fk("arrears_proposals", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("arrears_proposals", "pay_group_id", "pay_groups", "pay_group"),
        _fk("arrears_proposals", "original_run_id", "payroll_runs", "original_run"),
        _fk("arrears_proposals", "original_run_employee_id", "payroll_run_employees", "original_run_employee"),
        _fk("arrears_proposals", "original_period_id", "pay_periods", "original_period"),
        _fk("arrears_proposals", "base_proposal_id", "arrears_proposals", "base_proposal"),
        _fk("arrears_proposals", "superseded_by_id", "arrears_proposals", "superseded_by"),
        _fk("arrears_proposals", "retro_run_id", "payroll_runs", "retro_run"),
        _fk("arrears_proposals", "approval_request_id", "approval_requests", "approval_request"),
        _fk("arrears_proposals", "created_by_user_id", "users", "created_by"),
        _fk("arrears_proposals", "decided_by_user_id", "users", "decided_by"),
        _currency("arrears_proposals"),
        CheckConstraint(f"trigger IN {ARREARS_TRIGGERS}", name="ck_arrears_proposals_trigger"),
        CheckConstraint(f"status IN {ARREARS_STATUSES}", name="ck_arrears_proposals_status"),
        CheckConstraint(f"payment_mode IS NULL OR payment_mode IN {ARREARS_PAYMENT_MODES}",
                        name="ck_arrears_proposals_payment_mode"),
        CheckConstraint(f"recovery_mode IS NULL OR recovery_mode IN {ARREARS_RECOVERY_MODES}",
                        name="ck_arrears_proposals_recovery_mode"),
        CheckConstraint("period_end >= period_start", name="ck_arrears_proposals_period"),
        CheckConstraint("jsonb_typeof(trigger_ref) = 'object'", name="ck_arrears_proposals_trigger_ref"),
        CheckConstraint("jsonb_typeof(changed_sources) = 'array'", name="ck_arrears_proposals_changed"),
        CheckConstraint("jsonb_typeof(totals) = 'object'", name="ck_arrears_proposals_totals"),
        CheckConstraint("jsonb_typeof(issues) = 'array'", name="ck_arrears_proposals_issues"),
        CheckConstraint("snapshot IS NULL OR jsonb_typeof(snapshot) = 'object'", name="ck_arrears_proposals_snapshot"),
        CheckConstraint("(snapshot IS NULL) = (snapshot_hash IS NULL)", name="ck_arrears_proposals_snapshot_hash"),
        CheckConstraint("snapshot_hash IS NULL OR snapshot_hash ~ '^[0-9a-f]{64}$'",
                        name="ck_arrears_proposals_snapshot_hash_hex"),
        CheckConstraint("calc_hash IS NULL OR calc_hash ~ '^[0-9a-f]{64}$'", name="ck_arrears_proposals_calc_hash"),
        CheckConstraint("status NOT IN ('APPROVED', 'SETTLED') OR payment_mode IS NOT NULL",
                        name="ck_arrears_proposals_approved_mode"),
        CheckConstraint("status <> 'REJECTED' OR decision_note IS NOT NULL", name="ck_arrears_proposals_reject_note"),
        CheckConstraint("(status = 'SUPERSEDED') = (superseded_by_id IS NOT NULL)",
                        name="ck_arrears_proposals_superseded"),
        CheckConstraint("(status = 'SETTLED') = (settled_at IS NOT NULL)", name="ck_arrears_proposals_settled"),
        CheckConstraint("base_proposal_id IS NULL OR base_proposal_id <> id", name="ck_arrears_proposals_base"),
        _row_version("arrears_proposals"),
        # One pending proposal per finalized run employee: a newer change supersedes it.
        Index("uq_arrears_proposals_pending", "company_id", "original_run_employee_id", unique=True,
              postgresql_where=text("status = 'PENDING_APPROVAL'")),
        Index("ix_arrears_proposals_employee_status", "company_id", "employee_id", "status"),
    )


class ArrearsProposalLine(TenantMixin, BaseModel):
    """One component delta of a proposal: ARREARS (positive) or RECOVERY (negative)."""

    __tablename__ = "arrears_proposal_lines"

    proposal_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False)
    component_code = Column(String(40), nullable=False)
    component_id = Column(Integer, nullable=True)
    kind = Column(String(25), nullable=False)
    category = Column(String(40), nullable=True)
    tags = Column(JSONB, nullable=False, default=list)
    original_amount = Column(Numeric(19, 4), nullable=False)
    recalculated_amount = Column(Numeric(19, 4), nullable=False)
    delta = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    direction = Column(String(10), nullable=False)
    # {"period_id", "start", "end", "run_id", "run_employee_id"} of the original period.
    period_ref = Column(JSONB, nullable=False, default=dict)
    explanation = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        _uq("arrears_proposal_lines"),
        UniqueConstraint("proposal_id", "component_code", name="uq_arrears_proposal_lines_component"),
        _fk("arrears_proposal_lines", "proposal_id", "arrears_proposals", "proposal", ondelete="CASCADE"),
        _fk("arrears_proposal_lines", "employee_id", "employees", "employee"),
        _fk("arrears_proposal_lines", "component_id", "pay_components", "component"),
        _currency("arrears_proposal_lines"),
        CheckConstraint(f"direction IN {ARREARS_DIRECTIONS}", name="ck_arrears_proposal_lines_direction"),
        CheckConstraint("delta <> 0", name="ck_arrears_proposal_lines_delta"),
        CheckConstraint("(direction = 'ARREARS') = (delta > 0)", name="ck_arrears_proposal_lines_sign"),
        CheckConstraint("jsonb_typeof(tags) = 'array'", name="ck_arrears_proposal_lines_tags"),
        CheckConstraint("jsonb_typeof(period_ref) = 'object'", name="ck_arrears_proposal_lines_period_ref"),
    )


class ArrearsInstalment(TenantMixin, BaseModel):
    """What one paying run pays (or recovers) of a proposal line (EC-COR-05: recoveries split)."""

    __tablename__ = "arrears_instalments"

    proposal_line_id = Column(Integer, nullable=False, index=True)
    proposal_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    number = Column(Integer, nullable=False)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    status = Column(String(10), nullable=False, default="PLANNED")
    settled_run_id = Column(Integer, nullable=True)
    settled_amount = Column(Numeric(19, 4), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("arrears_instalments"),
        UniqueConstraint("proposal_line_id", "number", name="uq_arrears_instalments_number"),
        _fk("arrears_instalments", "proposal_line_id", "arrears_proposal_lines", "proposal_line", ondelete="CASCADE"),
        _fk("arrears_instalments", "proposal_id", "arrears_proposals", "proposal", ondelete="CASCADE"),
        _fk("arrears_instalments", "employee_id", "employees", "employee"),
        _fk("arrears_instalments", "settled_run_id", "payroll_runs", "settled_run"),
        _currency("arrears_instalments"),
        CheckConstraint(f"status IN {INSTALMENT_STATUSES}", name="ck_arrears_instalments_status"),
        CheckConstraint("number >= 1", name="ck_arrears_instalments_number_positive"),
        CheckConstraint("amount <> 0", name="ck_arrears_instalments_amount"),
        CheckConstraint("(status = 'SETTLED') = (settled_at IS NOT NULL AND settled_run_id IS NOT NULL "
                        "AND settled_amount IS NOT NULL)", name="ck_arrears_instalments_settled"),
        _row_version("arrears_instalments"),
    )


class PayrollReversal(TenantMixin, BaseModel):
    """A request to reverse a finalized run, its two approvals and the REVERSAL run it creates (3.5 §12)."""

    __tablename__ = "payroll_reversals"

    original_run_id = Column(Integer, nullable=False, index=True)
    reversal_run_id = Column(Integer, nullable=True)
    legal_entity_id = Column(Integer, nullable=False)
    pay_group_id = Column(Integer, nullable=False)
    # Original run employees being reversed (all calculated ones when none were selected).
    run_employee_ids = Column(JSONB, nullable=False, default=list)
    employee_ids = Column(JSONB, nullable=False, default=list)
    selected = Column(Boolean, nullable=False, default=False)
    reason = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="PENDING_APPROVAL")
    # {"INR": {"gross": "-…", "net": "-…", "employees": n}} per currency (never summed across currencies).
    preview = Column(JSONB, nullable=False, default=dict)
    # Finalization effects: {"instructions_cancelled": [...], "recoveries": [...], "outbox_event_id": n}.
    effects = Column(JSONB, nullable=False, default=dict)
    requested_by_user_id = Column(Integer, nullable=False)
    first_approver_user_id = Column(Integer, nullable=True)
    first_approved_at = Column(DateTime(timezone=True), nullable=True)
    first_comment = Column(Text, nullable=True)
    second_approver_user_id = Column(Integer, nullable=True)
    second_approved_at = Column(DateTime(timezone=True), nullable=True)
    second_comment = Column(Text, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)
    finalized_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payroll_reversals"),
        UniqueConstraint("company_id", "reversal_run_id", name="uq_payroll_reversals_reversal_run"),
        _fk("payroll_reversals", "original_run_id", "payroll_runs", "original_run"),
        _fk("payroll_reversals", "reversal_run_id", "payroll_runs", "reversal_run"),
        _fk("payroll_reversals", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("payroll_reversals", "pay_group_id", "pay_groups", "pay_group"),
        _fk("payroll_reversals", "requested_by_user_id", "users", "requested_by"),
        _fk("payroll_reversals", "first_approver_user_id", "users", "first_approver"),
        _fk("payroll_reversals", "second_approver_user_id", "users", "second_approver"),
        _fk("payroll_reversals", "decided_by_user_id", "users", "decided_by"),
        CheckConstraint(f"status IN {REVERSAL_STATUSES}", name="ck_payroll_reversals_status"),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_payroll_reversals_reason"),
        CheckConstraint("jsonb_typeof(run_employee_ids) = 'array' AND jsonb_array_length(run_employee_ids) > 0",
                        name="ck_payroll_reversals_run_employees"),
        CheckConstraint("jsonb_typeof(employee_ids) = 'array'", name="ck_payroll_reversals_employees"),
        CheckConstraint("jsonb_typeof(preview) = 'object'", name="ck_payroll_reversals_preview"),
        CheckConstraint("jsonb_typeof(effects) = 'object'", name="ck_payroll_reversals_effects"),
        # Two approvers, neither of them the requester (3.5 §4.1 REVERSE: reason; two approvers).
        CheckConstraint("first_approver_user_id IS NULL OR first_approver_user_id <> requested_by_user_id",
                        name="ck_payroll_reversals_first_not_requester"),
        CheckConstraint("second_approver_user_id IS NULL OR (second_approver_user_id <> requested_by_user_id "
                        "AND second_approver_user_id <> first_approver_user_id)",
                        name="ck_payroll_reversals_second_distinct"),
        CheckConstraint("(first_approver_user_id IS NULL) = (first_approved_at IS NULL)",
                        name="ck_payroll_reversals_first_at"),
        CheckConstraint("(second_approver_user_id IS NULL) = (second_approved_at IS NULL)",
                        name="ck_payroll_reversals_second_at"),
        CheckConstraint("second_approver_user_id IS NULL OR first_approver_user_id IS NOT NULL",
                        name="ck_payroll_reversals_order"),
        CheckConstraint("status NOT IN ('APPROVED', 'FINALIZED') OR (second_approver_user_id IS NOT NULL "
                        "AND reversal_run_id IS NOT NULL)", name="ck_payroll_reversals_approved"),
        CheckConstraint("(status = 'FINALIZED') = (finalized_at IS NOT NULL)", name="ck_payroll_reversals_finalized"),
        CheckConstraint("status <> 'REJECTED' OR decision_note IS NOT NULL", name="ck_payroll_reversals_reject_note"),
        _row_version("payroll_reversals"),
        Index("uq_payroll_reversals_pending", "company_id", "original_run_id", unique=True,
              postgresql_where=text("status = 'PENDING_APPROVAL'")),
    )


class FinalSettlementCase(TenantMixin, BaseModel):
    """The full and final settlement of one employment period (3.5 §15)."""

    __tablename__ = "final_settlement_cases"

    employee_id = Column(Integer, nullable=False, index=True)
    employee_assignment_id = Column(Integer, nullable=False)
    legal_entity_id = Column(Integer, nullable=False)
    pay_group_id = Column(Integer, nullable=False)
    pay_period_id = Column(Integer, nullable=True)
    employment_start = Column(Date, nullable=False)
    last_working_day = Column(Date, nullable=False)
    notice_given_on = Column(Date, nullable=True)
    termination_type = Column(String(30), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    status = Column(String(10), nullable=False, default="OPEN")
    held = Column(Boolean, nullable=False, default=False)
    hold_reason = Column(String(500), nullable=True)
    # DEATH: {"type": "NOMINEE" | "LEGAL_HEIR", "name", "relationship", "evidence_ref"} — never bank numbers
    # (the payee's bank account goes through the bank-account workflow).
    payee = Column(JSONB(none_as_null=True), nullable=True)
    due_date = Column(Date, nullable=True)
    due_basis = Column(String(20), nullable=False, default="NOT_CONFIGURED")
    due_note = Column(Text, nullable=True)
    run_id = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancel_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("final_settlement_cases"),
        _fk("final_settlement_cases", "employee_id", "employees", "employee"),
        # The service also checks that the assignment belongs to the same employee.
        ForeignKeyConstraint(["company_id", "employee_assignment_id"],
                             ["employee_assignments.company_id", "employee_assignments.id"],
                             name="fk_final_settlement_cases_assignment"),
        _fk("final_settlement_cases", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("final_settlement_cases", "pay_group_id", "pay_groups", "pay_group"),
        _fk("final_settlement_cases", "pay_period_id", "pay_periods", "pay_period"),
        _fk("final_settlement_cases", "run_id", "payroll_runs", "run"),
        _fk("final_settlement_cases", "created_by_user_id", "users", "created_by"),
        _currency("final_settlement_cases"),
        CheckConstraint(f"termination_type IN {TERMINATION_TYPES}", name="ck_final_settlement_cases_type"),
        CheckConstraint(f"status IN {SETTLEMENT_STATUSES}", name="ck_final_settlement_cases_status"),
        CheckConstraint(f"due_basis IN {DUE_BASES}", name="ck_final_settlement_cases_due_basis"),
        CheckConstraint("last_working_day >= employment_start", name="ck_final_settlement_cases_window"),
        CheckConstraint("notice_given_on IS NULL OR notice_given_on <= last_working_day",
                        name="ck_final_settlement_cases_notice"),
        CheckConstraint("payee IS NULL OR jsonb_typeof(payee) = 'object'", name="ck_final_settlement_cases_payee"),
        CheckConstraint("termination_type <> 'DEATH' OR payee IS NOT NULL", name="ck_final_settlement_cases_death_payee"),
        CheckConstraint("held = (hold_reason IS NOT NULL)", name="ck_final_settlement_cases_hold"),
        CheckConstraint("(due_basis = 'NOT_CONFIGURED') = (due_date IS NULL)", name="ck_final_settlement_cases_due"),
        CheckConstraint("status NOT IN ('IN_RUN', 'SETTLED') OR run_id IS NOT NULL",
                        name="ck_final_settlement_cases_run"),
        CheckConstraint("(status = 'SETTLED') = (settled_at IS NOT NULL)", name="ck_final_settlement_cases_settled"),
        CheckConstraint("(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
                        name="ck_final_settlement_cases_cancelled"),
        _row_version("final_settlement_cases"),
        # One live case per employment period; a rehire starts a new one (EC-COR-09).
        Index("uq_final_settlement_cases_employment", "company_id", "employee_id", "employment_start", unique=True,
              postgresql_where=text("status <> 'CANCELLED'")),
        Index("ix_final_settlement_cases_status_due", "company_id", "status", "due_date"),
    )


class FinalSettlementItem(TenantMixin, BaseModel):
    """One checklist item of a final settlement (signed amount: positive paid, negative recovered)."""

    __tablename__ = "final_settlement_items"

    case_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False)
    item_type = Column(String(30), nullable=False)
    source_key = Column(String(80), nullable=False, default="")
    status = Column(String(15), nullable=False, default="PENDING")
    component_code = Column(String(40), nullable=True)
    amount = Column(Numeric(19, 4), nullable=True)
    currency = Column(CHAR(3), nullable=False)
    units = Column(Numeric(10, 4), nullable=True)
    rate = Column(Numeric(24, 10), nullable=True)
    details = Column(JSONB, nullable=False, default=dict)
    issues = Column(JSONB, nullable=False, default=list)
    requires_consent = Column(Boolean, nullable=False, default=False)
    consent_ref = Column(String(300), nullable=True)
    consent_recorded_by_user_id = Column(Integer, nullable=True)
    consent_recorded_at = Column(DateTime(timezone=True), nullable=True)
    wage_input_id = Column(Integer, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("final_settlement_items"),
        UniqueConstraint("case_id", "item_type", "source_key", name="uq_final_settlement_items_key"),
        _fk("final_settlement_items", "case_id", "final_settlement_cases", "case", ondelete="CASCADE"),
        _fk("final_settlement_items", "employee_id", "employees", "employee"),
        _fk("final_settlement_items", "consent_recorded_by_user_id", "users", "consent_recorded_by"),
        _fk("final_settlement_items", "wage_input_id", "wage_inputs", "wage_input"),
        _fk("final_settlement_items", "decided_by_user_id", "users", "decided_by"),
        _currency("final_settlement_items"),
        CheckConstraint(f"item_type IN {SETTLEMENT_ITEM_TYPES}", name="ck_final_settlement_items_type"),
        CheckConstraint(f"status IN {SETTLEMENT_ITEM_STATUSES}", name="ck_final_settlement_items_status"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_final_settlement_items_details"),
        CheckConstraint("jsonb_typeof(issues) = 'array'", name="ck_final_settlement_items_issues"),
        CheckConstraint("status <> 'CONFIRMED' OR (amount IS NOT NULL OR item_type IN ('SALARY_TO_LAST_DAY', 'TAX', "
                        "'PENDING_ARREARS', 'PENDING_CLAIMS', 'PENDING_INPUTS', 'LOAN_RECOVERY'))",
                        name="ck_final_settlement_items_confirmed_amount"),
        # A deduction for unreturned assets needs recorded consent/approval (EC-COR-14).
        CheckConstraint("NOT (status = 'CONFIRMED' AND requires_consent) OR (consent_ref IS NOT NULL "
                        "AND consent_recorded_by_user_id IS NOT NULL)", name="ck_final_settlement_items_consent"),
        CheckConstraint("status NOT IN ('CONFIRMED', 'WAIVED') OR decided_by_user_id IS NOT NULL",
                        name="ck_final_settlement_items_decided"),
        CheckConstraint("status <> 'WAIVED' OR note IS NOT NULL", name="ck_final_settlement_items_waive_note"),
        _row_version("final_settlement_items"),
    )


class FinalSettlementDeadline(TenantMixin, BaseModel):
    """The company's own final-settlement deadline (NULL legal entity = every entity of the company)."""

    __tablename__ = "final_settlement_deadlines"

    legal_entity_id = Column(Integer, nullable=True)
    days = Column(Integer, nullable=False)
    day_basis = Column(String(15), nullable=False, default="CALENDAR_DAYS")
    note = Column(String(500), nullable=True)
    updated_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("final_settlement_deadlines"),
        _fk("final_settlement_deadlines", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("final_settlement_deadlines", "updated_by_user_id", "users", "updated_by"),
        CheckConstraint(f"day_basis IN {DEADLINE_BASES}", name="ck_final_settlement_deadlines_basis"),
        CheckConstraint("days BETWEEN 0 AND 365", name="ck_final_settlement_deadlines_days"),
        _row_version("final_settlement_deadlines"),
        Index("uq_final_settlement_deadlines_scope", "company_id", text("COALESCE(legal_entity_id, 0)"), unique=True),
    )
