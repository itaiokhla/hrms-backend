"""Pay calendars, periods, runs and their results (3.5, 3.9 §3.6; migration 0019_payroll_runs).

* ``pay_calendars``: one frequency + rules per pay group and date range. A frequency change ends
  the current calendar (``effective_to``) and starts a new one the next day (EC-CAL-07); at most
  one open-ended calendar per pay group.
* ``pay_periods``: generated from a calendar (``app.payroll.calendar``), local dates in the legal
  entity's timezone. Periods of one pay group never overlap (trigger ``pay_periods_guard``, as this
  PostgreSQL build has no btree_gist, EC-CAL-12); the calendar service keeps them contiguous.
* ``payroll_runs``: one pay group, one period, one run type; ``status`` uses the contract
  vocabulary (``state_machines.RUN``) and changes only through ``services.runs.transition``.
  ``regular_sequence`` makes the "one non-cancelled regular run per pay group and period" rule a
  partial unique index (EC-RUN-01): it is always 1, unless ``payroll.run.allow_multiple_regular_runs``
  lets the service number further regular runs.
* ``payroll_run_employees``: one row per (run, employee, assignment). ``status`` PENDING |
  CALCULATED | ERROR | ON_HOLD | EXCLUDED. A FULL hold sets ON_HOLD (calculated, never finalized
  for that employee); a PAYMENT hold keeps the status and sets ``hold_type = 'PAYMENT'`` (finalized,
  instruction held). One regular payment per (assignment, period) across non-cancelled regular
  runs is enforced by trigger ``payroll_run_employees_guard`` (EC-RUN-02).
* ``payroll_input_snapshots`` are immutable (UPDATE refused by trigger); a re-snapshot is a new version.
* ``payroll_lines`` are the engine's lines (``engine.lines.Line``); ``payroll_line_sources`` link a
  line to the consumable record it pays. A source is consumed exactly once: the partial unique index
  ``uq_payroll_line_sources_consumed`` covers rows whose ``consumed`` flag finalization sets
  (EC-VAR-01, EC-VAR-08). ``source_key`` distinguishes recurring sources (a loan's instalment number).
* ``payroll_exceptions``: pre-check, calculation and validation findings with their resolution
  (FIXED | WAIVED | DEFERRED); ``dedupe_key`` lets a re-check update instead of duplicating.
* ``payroll_approvals``: the decisions recorded on a run (decision documented in services.runs):
  the generic approvals engine (``approval_requests``) routes the chain, and every decision is also
  written here with the run, step, actor and capacity, so the run's approval history survives
  reopening (``cleared_at``, EC-RUN-16). ``run_employee_id`` records a self-pay line approval (EC-RUN-06).
* ``ytd_balances``, ``statutory_liabilities``: written by finalization (idempotent keys).
* ``statutory_attestations``: maker-checker attestation that statutory obligations of a legal
  entity are handled outside the system (decision I-15); approver ≠ attester (CHECK).
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
from app.models.pay_org import PAY_FREQUENCIES

CALENDAR_STATUSES = ("ACTIVE", "ENDED")
PERIOD_STATUSES = ("OPEN", "PROCESSING", "CLOSED")
RUN_TYPES = ("REGULAR", "OFF_CYCLE", "RETRO_ADJUSTMENT", "FINAL_SETTLEMENT", "REVERSAL")
RUN_STATUSES = (
    "DRAFT", "INPUTS_PENDING", "READY", "CALCULATING", "CALCULATED", "VALIDATION_FAILED", "REVIEW_REQUIRED",
    "APPROVAL_PENDING", "APPROVED", "REJECTED", "REOPENED_FOR_CORRECTION", "CANCELLED", "FINALIZED",
    "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED",
)
RUN_ENVIRONMENTS = ("development", "test", "staging", "production")
RUN_EMPLOYEE_STATUSES = ("PENDING", "CALCULATED", "ERROR", "ON_HOLD", "EXCLUDED")
EXCLUSION_REASONS = (
    "NO_SALARY", "INVALID_ASSIGNMENT", "JOINED_AFTER_PERIOD", "LEFT_BEFORE_PERIOD", "SUSPENDED_ALL_PERIOD",
    "INELIGIBLE", "REMOVED", "NOT_SELECTED",
)
HOLD_TYPES = ("PAYMENT", "FULL")
HOLD_REASONS = ("ABSCONDING", "DISPUTE", "BANK_ISSUE", "INVESTIGATION", "OTHER")
LINE_KINDS = (
    "EARNING", "DEDUCTION", "EMPLOYER_CONTRIBUTION", "REIMBURSEMENT", "BENEFIT_IN_KIND", "INFORMATIONAL", "ROUNDING",
)
LINE_VISIBILITIES = ("EMPLOYEE_VISIBLE", "HR_ONLY")
LINE_SOURCE_TYPES = ("WAGE_INPUT", "OVERTIME", "TASK", "LOAN", "CLAIM", "ARREARS", "LEAVE_ENCASHMENT")
EXCEPTION_SEVERITIES = ("BLOCKING", "WARNING", "FLAG")
EXCEPTION_STATUSES = ("OPEN", "FIXED", "WAIVED", "DEFERRED")
EXCEPTION_SOURCES = ("PRECHECK", "CALCULATION", "VALIDATION", "SYSTEM")
DEFER_TARGETS = ("NEXT_PERIOD", "RETRO")
APPROVAL_DECISIONS = ("APPROVED", "REJECTED")
APPROVAL_ACTED_AS = ("approver", "delegate", "override")
LIABILITY_STATUSES = ("OPEN", "REMITTED", "RECONCILED")
ATTESTATION_STATUSES = ("PENDING", "ACTIVE", "REVOKED")


def _fk(table: str, columns, target: str, name: str, ondelete=None, use_alter: bool = False) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    remote = [f"{target}.company_id", f"{target}.id"]
    return ForeignKeyConstraint(local, remote, name=f"fk_{table}_{name}", ondelete=ondelete, use_alter=use_alter)


def _uq(table: str) -> UniqueConstraint:
    return UniqueConstraint("company_id", "id", name=f"uq_{table}_company_id_id")


def _currency(table: str, column: str = "currency") -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^[A-Z]{{3}}$'", name=f"ck_{table}_{column}")


class PayCalendar(TenantMixin, BaseModel):
    """The calendar of one pay group from ``effective_from`` (to ``effective_to``).

    ``rules`` holds the complete ``calendar.CalendarRules`` fields except the banking calendar
    (resolved when periods are generated): monthly_start_day, semi_monthly_split_day,
    week_start_day, biweekly_anchor_date, pay_day_rule, pay_day_value, pay_day_shift,
    attendance_cycle_mode, attendance_cycle_start_day, the three cutoffs, tax_year_start and
    tax_year_basis.
    """

    __tablename__ = "pay_calendars"

    pay_group_id = Column(Integer, nullable=False, index=True)
    frequency = Column(String(15), nullable=False)
    rules = Column(JSONB, nullable=False, default=dict)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    generated_until = Column(Date, nullable=True)
    banking_calendar_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("pay_calendars"),
        _fk("pay_calendars", "pay_group_id", "pay_groups", "pay_group"),
        _fk("pay_calendars", "banking_calendar_id", "holiday_calendars", "banking_calendar"),
        _fk("pay_calendars", "created_by_user_id", "users", "created_by"),
        CheckConstraint(f"frequency IN {PAY_FREQUENCIES}", name="ck_pay_calendars_frequency"),
        CheckConstraint(f"status IN {CALENDAR_STATUSES}", name="ck_pay_calendars_status"),
        CheckConstraint("jsonb_typeof(rules) = 'object'", name="ck_pay_calendars_rules"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_pay_calendars_window"),
        CheckConstraint("(status = 'ENDED') = (effective_to IS NOT NULL)", name="ck_pay_calendars_ended"),
        CheckConstraint("row_version >= 1", name="ck_pay_calendars_row_version"),
        Index("uq_pay_calendars_open", "company_id", "pay_group_id", unique=True,
              postgresql_where=text("effective_to IS NULL")),
    )


class PayPeriod(TenantMixin, BaseModel):
    """One generated pay period (``calendar.Period``) of a pay group."""

    __tablename__ = "pay_periods"

    pay_calendar_id = Column(Integer, nullable=False, index=True)
    pay_group_id = Column(Integer, nullable=False, index=True)
    frequency = Column(String(15), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    attendance_start = Column(Date, nullable=False)
    attendance_end = Column(Date, nullable=False)
    pay_date = Column(Date, nullable=False)
    nominal_pay_date = Column(Date, nullable=False)
    input_cutoff_on = Column(Date, nullable=False)
    approval_deadline_on = Column(Date, nullable=False)
    payment_submit_by = Column(Date, nullable=False)
    tax_year = Column(String(9), nullable=False)
    number_in_tax_year = Column(Integer, nullable=False)
    is_last_in_tax_year = Column(Boolean, nullable=False, default=False)
    is_transition = Column(Boolean, nullable=False, default=False)
    status = Column(String(12), nullable=False, default="OPEN")
    pay_date_warnings = Column(JSONB, nullable=False, default=list)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("pay_periods"),
        _fk("pay_periods", "pay_calendar_id", "pay_calendars", "calendar"),
        _fk("pay_periods", "pay_group_id", "pay_groups", "pay_group"),
        UniqueConstraint("company_id", "pay_group_id", "period_start", name="uq_pay_periods_group_start"),
        CheckConstraint(f"frequency IN {PAY_FREQUENCIES}", name="ck_pay_periods_frequency"),
        CheckConstraint(f"status IN {PERIOD_STATUSES}", name="ck_pay_periods_status"),
        CheckConstraint("period_end >= period_start", name="ck_pay_periods_window"),
        CheckConstraint("attendance_end >= attendance_start - 1 AND attendance_end <= period_end",
                        name="ck_pay_periods_attendance"),
        CheckConstraint("input_cutoff_on <= approval_deadline_on AND approval_deadline_on <= pay_date",
                        name="ck_pay_periods_cutoffs"),
        CheckConstraint("number_in_tax_year >= 1", name="ck_pay_periods_number"),
        CheckConstraint("jsonb_typeof(pay_date_warnings) = 'array'", name="ck_pay_periods_warnings"),
        CheckConstraint("row_version >= 1", name="ck_pay_periods_row_version"),
        Index("ix_pay_periods_group_dates", "company_id", "pay_group_id", "period_start", "period_end"),
    )


class PayrollRun(TenantMixin, BaseModel):
    """One pay run (3.5 §3-§5). Server-owned columns only: nothing here is client input but the
    label, type, pay group and period chosen at creation."""

    __tablename__ = "payroll_runs"

    number = Column(String(20), nullable=False)
    label = Column(String(150), nullable=False)
    legal_entity_id = Column(Integer, nullable=False, index=True)
    pay_group_id = Column(Integer, nullable=False, index=True)
    pay_period_id = Column(Integer, nullable=False, index=True)
    run_type = Column(String(20), nullable=False)
    status = Column(String(25), nullable=False, default="DRAFT")
    regular_sequence = Column(Integer, nullable=True)
    currency = Column(CHAR(3), nullable=False)
    reverses_run_id = Column(Integer, nullable=True)
    # Off-cycle / final-settlement selection, retro original periods: {"employee_ids": [...],
    # "component_codes": [...], "original_period_ids": [...], "employment": {...}}.
    selection = Column(JSONB, nullable=False, default=dict)
    pack_version_ids = Column(JSONB, nullable=False, default=list)
    fx_snapshot_id = Column(Integer, nullable=True)
    # {"INR": {"gross": "…", "deductions": "…", "reimbursements": "…", "net": "…", "employer_cost": "…",
    #          "employees": n}} — never summed across currencies (EC-RUN-19).
    totals = Column(JSONB, nullable=False, default=dict)
    # {"eligible": n, "included": n, "excluded": n, "on_hold": n, "excluded_by_reason": {...}} (EC-RUN-11)
    population = Column(JSONB, nullable=False, default=dict)
    stale = Column(Boolean, nullable=False, default=False)
    stale_reasons = Column(JSONB, nullable=False, default=list)
    environment = Column(String(12), nullable=False)
    engine_version = Column(String(20), nullable=True)
    calculation_job_id = Column(Integer, nullable=True)
    approval_request_id = Column(Integer, nullable=True)
    prechecked_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    calculated_by_user_id = Column(Integer, nullable=True)
    calculated_at = Column(DateTime(timezone=True), nullable=True)
    submitted_by_user_id = Column(Integer, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    finalized_by_user_id = Column(Integer, nullable=True)
    finalized_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by_user_id = Column(Integer, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    status_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payroll_runs"),
        UniqueConstraint("company_id", "number", name="uq_payroll_runs_number"),
        _fk("payroll_runs", "legal_entity_id", "legal_entities", "legal_entity"),
        ForeignKeyConstraint(
            ["company_id", "legal_entity_id", "pay_group_id"],
            ["pay_groups.company_id", "pay_groups.legal_entity_id", "pay_groups.id"],
            name="fk_payroll_runs_pay_group",
        ),
        _fk("payroll_runs", "pay_period_id", "pay_periods", "pay_period"),
        _fk("payroll_runs", "reverses_run_id", "payroll_runs", "reverses"),
        _fk("payroll_runs", "fx_snapshot_id", "fx_snapshots", "fx_snapshot"),
        # jobs is platform-owned (nullable company_id): a plain foreign key.
        ForeignKeyConstraint(["calculation_job_id"], ["jobs.id"], name="fk_payroll_runs_calculation_job",
                             ondelete="SET NULL"),
        _fk("payroll_runs", "approval_request_id", "approval_requests", "approval_request"),
        _fk("payroll_runs", "created_by_user_id", "users", "created_by"),
        _fk("payroll_runs", "calculated_by_user_id", "users", "calculated_by"),
        _fk("payroll_runs", "submitted_by_user_id", "users", "submitted_by"),
        _fk("payroll_runs", "finalized_by_user_id", "users", "finalized_by"),
        _fk("payroll_runs", "cancelled_by_user_id", "users", "cancelled_by"),
        CheckConstraint(f"run_type IN {RUN_TYPES}", name="ck_payroll_runs_type"),
        CheckConstraint(f"status IN {RUN_STATUSES}", name="ck_payroll_runs_status"),
        CheckConstraint(f"environment IN {RUN_ENVIRONMENTS}", name="ck_payroll_runs_environment"),
        _currency("payroll_runs"),
        CheckConstraint("number ~ '^PR-[0-9]{4}-[0-9]{6}$'", name="ck_payroll_runs_number"),
        CheckConstraint("(run_type = 'REGULAR') = (regular_sequence IS NOT NULL)", name="ck_payroll_runs_regular_seq"),
        CheckConstraint("regular_sequence IS NULL OR regular_sequence >= 1", name="ck_payroll_runs_regular_seq_pos"),
        CheckConstraint("(run_type = 'REVERSAL') = (reverses_run_id IS NOT NULL)", name="ck_payroll_runs_reversal"),
        CheckConstraint("reverses_run_id IS NULL OR reverses_run_id <> id", name="ck_payroll_runs_not_self"),
        CheckConstraint("jsonb_typeof(selection) = 'object'", name="ck_payroll_runs_selection"),
        CheckConstraint("jsonb_typeof(pack_version_ids) = 'array'", name="ck_payroll_runs_packs"),
        CheckConstraint("jsonb_typeof(totals) = 'object'", name="ck_payroll_runs_totals"),
        CheckConstraint("jsonb_typeof(population) = 'object'", name="ck_payroll_runs_population"),
        CheckConstraint("jsonb_typeof(stale_reasons) = 'array'", name="ck_payroll_runs_stale_reasons"),
        CheckConstraint("stale OR stale_reasons = '[]'::jsonb", name="ck_payroll_runs_stale"),
        CheckConstraint("(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name="ck_payroll_runs_cancelled"),
        CheckConstraint(
            "(finalized_at IS NULL) = (status NOT IN ('FINALIZED', 'PAYMENT_READY', 'SUBMITTED', 'PARTIALLY_PAID', "
            "'PAID', 'RECONCILED', 'CLOSED'))", name="ck_payroll_runs_finalized",
        ),
        CheckConstraint("row_version >= 1", name="ck_payroll_runs_row_version"),
        # EC-RUN-01: one live regular run per pay group and period (regular_sequence is 1 unless the
        # pay group allows several regular runs).
        Index("uq_payroll_runs_regular", "company_id", "pay_group_id", "pay_period_id", "regular_sequence",
              unique=True, postgresql_where=text("run_type = 'REGULAR' AND status <> 'CANCELLED'")),
        Index("ix_payroll_runs_filters", "company_id", "pay_period_id", "status"),
    )


class PayrollRunEmployee(TenantMixin, BaseModel):
    """One employee assignment inside a run, with its totals once calculated."""

    __tablename__ = "payroll_run_employees"

    run_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    employee_assignment_id = Column(Integer, nullable=False, index=True)
    status = Column(String(12), nullable=False, default="PENDING")
    exclusion_reason = Column(String(30), nullable=True)
    exclusion_note = Column(String(500), nullable=True)
    hold_type = Column(String(10), nullable=True)
    # {"type", "reason_code", "note", "by_user_id", "at", "status_before"}; history is in the audit log.
    hold = Column(JSONB(none_as_null=True), nullable=True)
    gross = Column(Numeric(19, 4), nullable=True)
    deductions = Column(Numeric(19, 4), nullable=True)
    reimbursements = Column(Numeric(19, 4), nullable=True)
    net = Column(Numeric(19, 4), nullable=True)
    employer_cost = Column(Numeric(19, 4), nullable=True)
    currency = Column(CHAR(3), nullable=False)
    snapshot_id = Column(Integer, nullable=True)
    content_hash = Column(CHAR(64), nullable=True)
    calc_hash = Column(CHAR(64), nullable=True)
    calculated_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(JSONB(none_as_null=True), nullable=True)
    flags = Column(JSONB, nullable=False, default=list)
    # Engine side results besides lines (migration 0020, services.calculation): {"engine_version", "calc_hash",
    # "statutory": {liabilities, ytd_increments, pack_state, bases, taxable}, "deductions": {carry_forwards,
    # protected_net, skipped}, "payments": [...], "ledger": {...}, "rates": {...}} — read by finalization (F-15).
    stage_outputs = Column(JSONB, nullable=False, default=dict)
    self_line_approved_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payroll_run_employees"),
        UniqueConstraint("run_id", "employee_id", "employee_assignment_id", name="uq_payroll_run_employees_identity"),
        _fk("payroll_run_employees", "run_id", "payroll_runs", "run", ondelete="CASCADE"),
        _fk("payroll_run_employees", "employee_id", "employees", "employee"),
        # The guard trigger also checks that the assignment belongs to the same employee.
        ForeignKeyConstraint(["company_id", "employee_assignment_id"],
                             ["employee_assignments.company_id", "employee_assignments.id"],
                             name="fk_payroll_run_employees_assignment"),
        _fk("payroll_run_employees", "snapshot_id", "payroll_input_snapshots", "snapshot", use_alter=True),
        _fk("payroll_run_employees", "self_line_approved_by_user_id", "users", "self_line_approver"),
        CheckConstraint(f"status IN {RUN_EMPLOYEE_STATUSES}", name="ck_payroll_run_employees_status"),
        CheckConstraint(f"exclusion_reason IS NULL OR exclusion_reason IN {EXCLUSION_REASONS}",
                        name="ck_payroll_run_employees_exclusion_reason"),
        CheckConstraint("(status = 'EXCLUDED') = (exclusion_reason IS NOT NULL)",
                        name="ck_payroll_run_employees_excluded"),
        CheckConstraint(f"hold_type IS NULL OR hold_type IN {HOLD_TYPES}", name="ck_payroll_run_employees_hold_type"),
        CheckConstraint("(hold_type IS NULL) = (hold IS NULL)", name="ck_payroll_run_employees_hold"),
        CheckConstraint("status <> 'ON_HOLD' OR hold_type = 'FULL'", name="ck_payroll_run_employees_on_hold"),
        CheckConstraint("hold IS NULL OR jsonb_typeof(hold) = 'object'", name="ck_payroll_run_employees_hold_shape"),
        CheckConstraint("jsonb_typeof(flags) = 'array'", name="ck_payroll_run_employees_flags"),
        CheckConstraint("jsonb_typeof(stage_outputs) = 'object'", name="ck_payroll_run_employees_stage_outputs"),
        _currency("payroll_run_employees"),
        CheckConstraint("content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'",
                        name="ck_payroll_run_employees_content_hash"),
        CheckConstraint("calc_hash IS NULL OR calc_hash ~ '^[0-9a-f]{64}$'", name="ck_payroll_run_employees_calc_hash"),
        CheckConstraint("row_version >= 1", name="ck_payroll_run_employees_row_version"),
        Index("ix_payroll_run_employees_run_status", "company_id", "run_id", "status"),
    )


class PayrollInputSnapshot(TenantMixin, BaseModel):
    """The frozen calculation input of one run employee (``engine.snapshot``); immutable."""

    __tablename__ = "payroll_input_snapshots"

    run_employee_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    content = Column(JSONB, nullable=False)
    content_hash = Column(CHAR(64), nullable=False)
    source_hashes = Column(JSONB, nullable=False, default=dict)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        _uq("payroll_input_snapshots"),
        UniqueConstraint("run_employee_id", "version", name="uq_payroll_input_snapshots_version"),
        _fk("payroll_input_snapshots", "run_employee_id", "payroll_run_employees", "run_employee", ondelete="CASCADE"),
        _fk("payroll_input_snapshots", "created_by_user_id", "users", "created_by"),
        CheckConstraint("version >= 1", name="ck_payroll_input_snapshots_version"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="ck_payroll_input_snapshots_content"),
        CheckConstraint("jsonb_typeof(source_hashes) = 'object'", name="ck_payroll_input_snapshots_sources"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_payroll_input_snapshots_hash"),
    )


class PayrollLine(TenantMixin, BaseModel):
    """One calculated line (``engine.lines.Line``) of a run employee."""

    __tablename__ = "payroll_lines"

    run_id = Column(Integer, nullable=False, index=True)
    run_employee_id = Column(Integer, nullable=False, index=True)
    component_code = Column(String(40), nullable=False)
    component_id = Column(Integer, nullable=True)
    component_version_id = Column(Integer, nullable=True)
    kind = Column(String(25), nullable=False)
    category = Column(String(40), nullable=True)
    stage = Column(Integer, nullable=False)
    amount = Column(Numeric(19, 4), nullable=False)
    currency = Column(CHAR(3), nullable=False)
    units = Column(Numeric(14, 6), nullable=True)
    rate = Column(Numeric(24, 10), nullable=True)
    # Arrears and recoveries point at the original period: {"period_id", "start", "end", "run_id"}.
    period_ref = Column(JSONB(none_as_null=True), nullable=True)
    segment = Column(JSONB(none_as_null=True), nullable=True)
    tags = Column(JSONB, nullable=False, default=list)
    visibility = Column(String(20), nullable=False, default="EMPLOYEE_VISIBLE")
    sources = Column(JSONB, nullable=False, default=list)
    trace = Column(JSONB, nullable=False, default=dict)
    explanation = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    statutory = Column(Boolean, nullable=False, default=False)
    fixture = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        _uq("payroll_lines"),
        _fk("payroll_lines", "run_id", "payroll_runs", "run", ondelete="CASCADE"),
        _fk("payroll_lines", "run_employee_id", "payroll_run_employees", "run_employee", ondelete="CASCADE"),
        _fk("payroll_lines", "component_id", "pay_components", "component"),
        _fk("payroll_lines", "component_version_id", "pay_component_versions", "component_version"),
        CheckConstraint(f"kind IN {LINE_KINDS}", name="ck_payroll_lines_kind"),
        CheckConstraint(f"visibility IN {LINE_VISIBILITIES}", name="ck_payroll_lines_visibility"),
        CheckConstraint("stage BETWEEN 1 AND 12", name="ck_payroll_lines_stage"),
        _currency("payroll_lines"),
        CheckConstraint("jsonb_typeof(tags) = 'array'", name="ck_payroll_lines_tags"),
        CheckConstraint("jsonb_typeof(sources) = 'array'", name="ck_payroll_lines_sources"),
        CheckConstraint("jsonb_typeof(trace) = 'object'", name="ck_payroll_lines_trace"),
        CheckConstraint("period_ref IS NULL OR jsonb_typeof(period_ref) = 'object'", name="ck_payroll_lines_period_ref"),
        CheckConstraint("segment IS NULL OR jsonb_typeof(segment) = 'object'", name="ck_payroll_lines_segment"),
        Index("ix_payroll_lines_run_employee_order", "company_id", "run_employee_id", "sort_order"),
    )


class PayrollLineSource(TenantMixin, BaseModel):
    """A consumable record paid by a line; ``consumed`` is set when the run is finalized."""

    __tablename__ = "payroll_line_sources"

    line_id = Column(Integer, nullable=False, index=True)
    run_id = Column(Integer, nullable=False, index=True)
    run_employee_id = Column(Integer, nullable=False, index=True)
    source_type = Column(String(20), nullable=False)
    source_id = Column(Integer, nullable=False)
    source_key = Column(String(80), nullable=False, default="")
    consumed = Column(Boolean, nullable=False, default=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        _uq("payroll_line_sources"),
        UniqueConstraint("line_id", "source_type", "source_id", "source_key", name="uq_payroll_line_sources_line"),
        _fk("payroll_line_sources", "line_id", "payroll_lines", "line", ondelete="CASCADE"),
        _fk("payroll_line_sources", "run_id", "payroll_runs", "run", ondelete="CASCADE"),
        _fk("payroll_line_sources", "run_employee_id", "payroll_run_employees", "run_employee", ondelete="CASCADE"),
        CheckConstraint(f"source_type IN {LINE_SOURCE_TYPES}", name="ck_payroll_line_sources_type"),
        CheckConstraint("consumed = (consumed_at IS NOT NULL)", name="ck_payroll_line_sources_consumed"),
        # Exactly once (EC-VAR-01, EC-VAR-08): a source is consumed by at most one finalized line.
        Index("uq_payroll_line_sources_consumed", "company_id", "source_type", "source_id", "source_key",
              unique=True, postgresql_where=text("consumed")),
        Index("ix_payroll_line_sources_source", "company_id", "source_type", "source_id"),
    )


class PayrollException(TenantMixin, BaseModel):
    """A pre-check, calculation or validation finding of a run (3.5 §5.2, §10)."""

    __tablename__ = "payroll_exceptions"

    run_id = Column(Integer, nullable=False, index=True)
    run_employee_id = Column(Integer, nullable=True, index=True)
    employee_id = Column(Integer, nullable=True)
    code = Column(String(40), nullable=False)
    severity = Column(String(10), nullable=False)
    source = Column(String(12), nullable=False, default="PRECHECK")
    message = Column(String(1000), nullable=False)
    details = Column(JSONB, nullable=False, default=dict)
    # Allowed resolutions besides re-checking: ["FIX", "WAIVE", "DEFER"].
    resolutions = Column(JSONB, nullable=False, default=list)
    fix_link = Column(String(300), nullable=True)
    waivable = Column(Boolean, nullable=False, default=False)
    blocks_finalization = Column(Boolean, nullable=False, default=False)
    dedupe_key = Column(String(200), nullable=False)
    status = Column(String(10), nullable=False, default="OPEN")
    resolved_by_user_id = Column(Integer, nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolution_note = Column(Text, nullable=True)
    deferred_to = Column(String(12), nullable=True)
    deferred_to_period_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("payroll_exceptions"),
        _fk("payroll_exceptions", "run_id", "payroll_runs", "run", ondelete="CASCADE"),
        _fk("payroll_exceptions", "run_employee_id", "payroll_run_employees", "run_employee", ondelete="CASCADE"),
        _fk("payroll_exceptions", "employee_id", "employees", "employee"),
        _fk("payroll_exceptions", "resolved_by_user_id", "users", "resolved_by"),
        _fk("payroll_exceptions", "deferred_to_period_id", "pay_periods", "deferred_to_period"),
        CheckConstraint(f"severity IN {EXCEPTION_SEVERITIES}", name="ck_payroll_exceptions_severity"),
        CheckConstraint(f"status IN {EXCEPTION_STATUSES}", name="ck_payroll_exceptions_status"),
        CheckConstraint(f"source IN {EXCEPTION_SOURCES}", name="ck_payroll_exceptions_source"),
        CheckConstraint(f"deferred_to IS NULL OR deferred_to IN {DEFER_TARGETS}", name="ck_payroll_exceptions_defer"),
        CheckConstraint("(status = 'DEFERRED') = (deferred_to IS NOT NULL)", name="ck_payroll_exceptions_deferred"),
        CheckConstraint("status <> 'WAIVED' OR waivable", name="ck_payroll_exceptions_waivable"),
        CheckConstraint("status NOT IN ('WAIVED', 'DEFERRED') OR (resolved_by_user_id IS NOT NULL "
                        "AND resolution_note IS NOT NULL)", name="ck_payroll_exceptions_resolved_by"),
        CheckConstraint("(status = 'OPEN') = (resolved_at IS NULL)", name="ck_payroll_exceptions_resolved_at"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_payroll_exceptions_details"),
        CheckConstraint("jsonb_typeof(resolutions) = 'array'", name="ck_payroll_exceptions_resolutions"),
        CheckConstraint("row_version >= 1", name="ck_payroll_exceptions_row_version"),
        Index("uq_payroll_exceptions_open", "company_id", "run_id", "dedupe_key", unique=True,
              postgresql_where=text("status = 'OPEN'")),
        Index("ix_payroll_exceptions_run_status", "company_id", "run_id", "status", "severity"),
    )


class PayrollApproval(TenantMixin, BaseModel):
    """One approval decision on a run (or on a self-pay line of it, EC-RUN-06)."""

    __tablename__ = "payroll_approvals"

    run_id = Column(Integer, nullable=False, index=True)
    run_employee_id = Column(Integer, nullable=True)
    approval_request_id = Column(Integer, nullable=True)
    step_order = Column(Integer, nullable=False)
    approver_user_id = Column(Integer, nullable=False)
    acted_as = Column(String(10), nullable=False, default="approver")
    on_behalf_of_user_id = Column(Integer, nullable=True)
    decision = Column(String(10), nullable=False)
    comment = Column(Text, nullable=True)
    acted_at = Column(DateTime(timezone=True), nullable=False)
    cleared_at = Column(DateTime(timezone=True), nullable=True)
    cleared_reason = Column(Text, nullable=True)

    __table_args__ = (
        _uq("payroll_approvals"),
        _fk("payroll_approvals", "run_id", "payroll_runs", "run", ondelete="CASCADE"),
        _fk("payroll_approvals", "run_employee_id", "payroll_run_employees", "run_employee", ondelete="CASCADE"),
        _fk("payroll_approvals", "approval_request_id", "approval_requests", "approval_request"),
        _fk("payroll_approvals", "approver_user_id", "users", "approver"),
        _fk("payroll_approvals", "on_behalf_of_user_id", "users", "on_behalf_of"),
        CheckConstraint(f"decision IN {APPROVAL_DECISIONS}", name="ck_payroll_approvals_decision"),
        CheckConstraint(f"acted_as IN {APPROVAL_ACTED_AS}", name="ck_payroll_approvals_acted_as"),
        CheckConstraint("step_order >= 1", name="ck_payroll_approvals_step"),
        CheckConstraint("(acted_as = 'delegate') = (on_behalf_of_user_id IS NOT NULL)",
                        name="ck_payroll_approvals_delegate"),
        CheckConstraint("decision <> 'REJECTED' OR comment IS NOT NULL", name="ck_payroll_approvals_reject_reason"),
        CheckConstraint("cleared_reason IS NULL OR cleared_at IS NOT NULL", name="ck_payroll_approvals_cleared"),
        # One live decision per approver and step (two clicks or two tabs record one decision, EC-RUN-08).
        Index("uq_payroll_approvals_live", "company_id", "run_id", text("COALESCE(run_employee_id, 0)"), "step_order",
              "approver_user_id", unique=True, postgresql_where=text("cleared_at IS NULL")),
    )


class YtdBalance(TenantMixin, BaseModel):
    """Year-to-date total of one component or bucket per employee, legal entity and tax year (EC-STA-08)."""

    __tablename__ = "ytd_balances"

    employee_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    tax_year = Column(String(9), nullable=False)
    bucket = Column(String(60), nullable=False)
    component_id = Column(Integer, nullable=True)
    amount = Column(Numeric(19, 4), nullable=False, default=0)
    currency = Column(CHAR(3), nullable=False)
    last_run_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("ytd_balances"),
        UniqueConstraint("company_id", "employee_id", "legal_entity_id", "tax_year", "bucket", "currency",
                         name="uq_ytd_balances_key"),
        _fk("ytd_balances", "employee_id", "employees", "employee"),
        _fk("ytd_balances", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("ytd_balances", "component_id", "pay_components", "component"),
        _fk("ytd_balances", "last_run_id", "payroll_runs", "last_run"),
        _currency("ytd_balances"),
        CheckConstraint("tax_year ~ '^[0-9]{4}(-[0-9]{2,4})?$'", name="ck_ytd_balances_tax_year"),
        CheckConstraint("row_version >= 1", name="ck_ytd_balances_row_version"),
    )


class StatutoryLiability(TenantMixin, BaseModel):
    """What a finalized run owes an authority for one scheme (employee and employer portions, EC-STA-10)."""

    __tablename__ = "statutory_liabilities"

    run_id = Column(Integer, nullable=False, index=True)
    legal_entity_id = Column(Integer, nullable=False)
    scheme = Column(String(40), nullable=False)
    payee_authority = Column(String(150), nullable=False)
    employee_amount = Column(Numeric(19, 4), nullable=False, default=0)
    employer_amount = Column(Numeric(19, 4), nullable=False, default=0)
    currency = Column(CHAR(3), nullable=False)
    due_date = Column(Date, nullable=True)
    status = Column(String(12), nullable=False, default="OPEN")
    remittance_ref = Column(String(100), nullable=True)
    fixture = Column(Boolean, nullable=False, default=False)
    details = Column(JSONB, nullable=False, default=dict)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("statutory_liabilities"),
        UniqueConstraint("company_id", "run_id", "scheme", "payee_authority", "currency",
                         name="uq_statutory_liabilities_key"),
        _fk("statutory_liabilities", "run_id", "payroll_runs", "run"),
        _fk("statutory_liabilities", "legal_entity_id", "legal_entities", "legal_entity"),
        CheckConstraint(f"status IN {LIABILITY_STATUSES}", name="ck_statutory_liabilities_status"),
        _currency("statutory_liabilities"),
        CheckConstraint("status = 'OPEN' OR remittance_ref IS NOT NULL", name="ck_statutory_liabilities_remitted"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_statutory_liabilities_details"),
        CheckConstraint("row_version >= 1", name="ck_statutory_liabilities_row_version"),
    )


class StatutoryAttestation(TenantMixin, BaseModel):
    """"Statutory obligations of this legal entity are handled outside this system" (decision I-15).

    Made by one administrator (``attested_by``) and approved by another (``approved_by``); only an
    ACTIVE attestation covering a run's period lets a run of an entity without a verified pack finalize.
    """

    __tablename__ = "statutory_attestations"

    legal_entity_id = Column(Integer, nullable=False, index=True)
    schemes = Column(JSONB, nullable=False, default=list)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    reason = Column(Text, nullable=False)
    evidence_ref = Column(String(300), nullable=True)
    status = Column(String(10), nullable=False, default="PENDING")
    attested_by_user_id = Column(Integer, nullable=False)
    approved_by_user_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(Integer, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoke_reason = Column(Text, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        _uq("statutory_attestations"),
        _fk("statutory_attestations", "legal_entity_id", "legal_entities", "legal_entity"),
        _fk("statutory_attestations", "attested_by_user_id", "users", "attested_by"),
        _fk("statutory_attestations", "approved_by_user_id", "users", "approved_by"),
        _fk("statutory_attestations", "revoked_by_user_id", "users", "revoked_by"),
        CheckConstraint(f"status IN {ATTESTATION_STATUSES}", name="ck_statutory_attestations_status"),
        CheckConstraint("approved_by_user_id IS NULL OR approved_by_user_id <> attested_by_user_id",
                        name="ck_statutory_attestations_maker_checker"),
        CheckConstraint("status <> 'ACTIVE' OR (approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
                        name="ck_statutory_attestations_active"),
        CheckConstraint("(status = 'REVOKED') = (revoked_at IS NOT NULL)", name="ck_statutory_attestations_revoked"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                        name="ck_statutory_attestations_window"),
        CheckConstraint("jsonb_typeof(schemes) = 'array'", name="ck_statutory_attestations_schemes"),
        CheckConstraint("row_version >= 1", name="ck_statutory_attestations_row_version"),
    )
