"""Time inputs that feed the payroll day ledger (3.4 §2-§8, §16; audit 03 §8 items 3, 5, 10, 13).

leave_days (one row per date of an approved leave), leave_balance_transactions (append-only balance
ledger), lop_inputs (manual LOP), employee_suspensions, on_duty_requests, employee_optional_holidays
and source_change_log (append-only version hashes of every source record a pay run may read).
"""

from sqlalchemy import (
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
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF")
BALANCE_REASONS = ("ACCRUAL", "USE", "ENCASH", "ADJUST", "EXPIRE",
                   # Leave v2 (SPINE §6.4, migration 0063): credits OPENING, GRANT, CARRY_FORWARD, COMP_OFF; PRORATE
                   # either way; debits LAPSE, PENALTY.
                   "OPENING", "GRANT", "PRORATE", "CARRY_FORWARD", "COMP_OFF", "LAPSE", "PENALTY")
LEDGER_SOURCES = ("LEAVE", "ACCRUAL_JOB", "YEAR_END", "ADJUSTMENT", "IMPORT", "MIGRATION", "COMP_OFF", "ENCASHMENT",
                  "PAYROLL", "SYSTEM")
INPUT_STATUSES = ("PENDING", "APPROVED", "REJECTED", "CANCELLED")
ACTIVE_INPUT_STATUSES = ("PENDING", "APPROVED")
SUSPENSION_STATUSES = ("ACTIVE", "CANCELLED")
OPTIONAL_HOLIDAY_STATUSES = ("CHOSEN", "CANCELLED")
CHANGE_RECORD_TYPES = (
    "attendance", "leave", "holiday", "lop_input", "suspension", "on_duty", "optional_holiday",
    "overtime", "task", "wage_input", "assignment",
    # LH-M1 (migration lh_m1_work_weeks_links): work-week pattern versions and effective-dated calendar links.
    "work_week_version", "calendar_link",
    # AS-M1 (as_m1_shift_versions) and LH-M1 calendar structure (lh_m1_calendar_structure).
    "shift_version", "shift_rule", "holiday_year", "working_day",
    # AS-M3 (as_m3_days_policies, JC-AS-11b): evaluations, attendance requests, roster entries.
    "attendance_day", "attendance_request", "roster_entry",
)
CHANGE_KINDS = ("CREATED", "UPDATED", "VOIDED", "DELETED")
ACTIVE_LEAVE_DAY = "voided_at IS NULL"
ACTIVE_INPUT = "status IN ('PENDING', 'APPROVED')"


def _employee_fk(table: str):
    return ForeignKeyConstraint(
        ["company_id", "employee_id"], ["employees.company_id", "employees.id"], name=f"fk_{table}_employee"
    )


def _approval_fk(table: str):
    return ForeignKeyConstraint(
        ["company_id", "approval_request_id"], ["approval_requests.company_id", "approval_requests.id"],
        name=f"fk_{table}_approval_request",
    )


class LeaveDay(TenantMixin, BaseModel):
    """One date of an approved leave, generated at approval from the work pattern and holiday calendar.

    Every date of the request gets a row; ``counts_against_balance`` is false on weekly offs and
    holidays unless the leave type counts non-working days. Cancellation voids the rows.
    """

    __tablename__ = "leave_days"

    leave_id = Column(Integer, nullable=False, index=True)
    employee_id = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    portion = Column(String(12), nullable=False, default="FULL")
    units = Column(Numeric(3, 2), nullable=False)
    counts_against_balance = Column(Boolean, nullable=False, default=True)
    pay_treatment_snapshot = Column(JSONB, nullable=False, default=dict)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    void_reason = Column(String(30), nullable=True)
    # Leave v2 (migration 0063): why a date is charged (WORKING, BASIS = the counting basis charges a non-working
    # date, SANDWICH) and the type rule's counting basis; NULL on legacy rows.
    charge_reason = Column(String(12), nullable=True)
    counting_basis = Column(String(24), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_days_company_id_id"),
        ForeignKeyConstraint(["company_id", "leave_id"], ["leaves.company_id", "leaves.id"], name="fk_leave_days_leave"),
        _employee_fk("leave_days"),
        CheckConstraint(f"portion IN {PORTIONS}", name="ck_leave_days_portion"),
        CheckConstraint(
            "units IN (0.5, 1) AND (portion = 'FULL') = (units = 1)", name="ck_leave_days_units"
        ),
        Index("uq_leave_days_active_slot", "company_id", "employee_id", "date", "portion", unique=True,
              postgresql_where=text(ACTIVE_LEAVE_DAY)),
        Index("ix_leave_days_employee_date", "company_id", "employee_id", "date"),
    )


class LeaveBalanceTransaction(TenantMixin, BaseModel):
    """An append-only change of one leave balance; ``idempotency_key`` makes each effect happen once (EC-LV-07)."""

    __tablename__ = "leave_balance_transactions"

    employee_id = Column(Integer, nullable=False)
    leave_type = Column(String(30), nullable=False)
    delta = Column(Numeric(6, 2), nullable=False)
    reason = Column(String(20), nullable=False)
    ref_type = Column(String(30), nullable=True)
    ref_id = Column(Integer, nullable=True)
    idempotency_key = Column(String(120), nullable=False)
    effective_date = Column(Date, nullable=False)
    expires_on = Column(Date, nullable=True)
    note = Column(String(500), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    # Leave v2 (migration 0063): the balance scope (leave year) the row belongs to, its writer and the plan version
    # that produced it. NULL on legacy rows (their cycle is the calendar year of effective_date).
    cycle_start = Column(Date, nullable=True)
    cycle_end = Column(Date, nullable=True)
    source = Column(String(12), nullable=True)
    policy_version_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_leave_balance_transactions_company_id_id"),
        UniqueConstraint("company_id", "idempotency_key", name="uq_leave_balance_transactions_key"),
        _employee_fk("leave_balance_transactions"),
        CheckConstraint(f"reason IN {BALANCE_REASONS}", name="ck_leave_balance_transactions_reason"),
        CheckConstraint("delta <> 0", name="ck_leave_balance_transactions_delta"),
        Index("ix_leave_balance_transactions_balance", "company_id", "employee_id", "leave_type", "effective_date"),
    )


class LopInput(TenantMixin, BaseModel):
    """A manual loss-of-pay input for one date or half day (3.4 §4.2 rule 6), approved through the engine (module lop)."""

    __tablename__ = "lop_inputs"

    employee_id = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    portion = Column(String(12), nullable=False, default="FULL")
    reason = Column(Text, nullable=False)
    status = Column(String(10), nullable=False, default="PENDING")
    approval_request_id = Column(Integer, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_lop_inputs_company_id_id"),
        _employee_fk("lop_inputs"),
        _approval_fk("lop_inputs"),
        CheckConstraint(f"portion IN {PORTIONS}", name="ck_lop_inputs_portion"),
        CheckConstraint(f"status IN {INPUT_STATUSES}", name="ck_lop_inputs_status"),
        Index("uq_lop_inputs_active_slot", "company_id", "employee_id", "date", "portion", unique=True,
              postgresql_where=text(ACTIVE_INPUT)),
        Index("ix_lop_inputs_employee_date", "company_id", "employee_id", "date"),
    )


class EmployeeSuspension(TenantMixin, BaseModel):
    """A suspension from ``start_date`` to ``end_date``; unpaid ones are SUSPENDED_UNPAID in the ledger (rule 2)."""

    __tablename__ = "employee_suspensions"

    employee_id = Column(Integer, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    paid = Column(Boolean, nullable=False, default=False)
    reason = Column(Text, nullable=False)
    status = Column(String(10), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_employee_suspensions_company_id_id"),
        _employee_fk("employee_suspensions"),
        CheckConstraint("end_date >= start_date", name="ck_employee_suspensions_dates"),
        CheckConstraint(f"status IN {SUSPENSION_STATUSES}", name="ck_employee_suspensions_status"),
        Index("ix_employee_suspensions_employee", "company_id", "employee_id", "start_date"),
    )


class OnDutyRequest(TenantMixin, BaseModel):
    """Approved on-duty or field work for a date (counts as present per ATTENDANCE_PAY_POLICY), module attendance."""

    __tablename__ = "on_duty_requests"

    employee_id = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    portion = Column(String(12), nullable=False, default="FULL")
    reason = Column(Text, nullable=False)
    status = Column(String(10), nullable=False, default="PENDING")
    approval_request_id = Column(Integer, nullable=True)
    requested_by_user_id = Column(Integer, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)
    # AS-M4 (migration 0052; 04-attendance/04 §5.2, JC-AS-37): kind, partial window, trip batch, punch snapshot.
    kind = Column(String(12), nullable=False, default="ON_DUTY", server_default="ON_DUTY")
    start_time = Column(Time, nullable=True)
    end_time = Column(Time, nullable=True)
    batch_id = Column(String(36), nullable=True)
    requires_punch = Column(Boolean, nullable=False, default=False, server_default="false")
    reason_code = Column(String(40), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_on_duty_requests_company_id_id"),
        CheckConstraint("kind IN ('ON_DUTY', 'REMOTE_WORK')", name="ck_on_duty_requests_kind"),
        CheckConstraint("(start_time IS NULL) = (end_time IS NULL) AND (end_time IS NULL OR end_time > start_time)",
                        name="ck_on_duty_requests_window"),
        Index("ix_on_duty_requests_batch", "company_id", "batch_id", postgresql_where=text("batch_id IS NOT NULL")),
        _employee_fk("on_duty_requests"),
        _approval_fk("on_duty_requests"),
        CheckConstraint(f"portion IN {PORTIONS}", name="ck_on_duty_requests_portion"),
        CheckConstraint(f"status IN {INPUT_STATUSES}", name="ck_on_duty_requests_status"),
        Index("uq_on_duty_requests_active_slot", "company_id", "employee_id", "date", "portion", unique=True,
              postgresql_where=text(ACTIVE_INPUT)),
    )


class EmployeeOptionalHoliday(TenantMixin, BaseModel):
    """An employee's choice of an OPTIONAL holiday (OPTIONAL_AVAILED in the ledger), within the yearly quota (EC-HOL-05)."""

    __tablename__ = "employee_optional_holidays"

    employee_id = Column(Integer, nullable=False)
    holiday_id = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)
    status = Column(String(10), nullable=False, default="CHOSEN")
    chosen_by_user_id = Column(Integer, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_employee_optional_holidays_company_id_id"),
        _employee_fk("employee_optional_holidays"),
        ForeignKeyConstraint(
            ["company_id", "holiday_id"], ["holidays.company_id", "holidays.id"], ondelete="CASCADE",
            name="fk_employee_optional_holidays_holiday",
        ),
        CheckConstraint(f"status IN {OPTIONAL_HOLIDAY_STATUSES}", name="ck_employee_optional_holidays_status"),
        Index("uq_employee_optional_holidays_chosen", "company_id", "employee_id", "holiday_id", unique=True,
              postgresql_where=text("status = 'CHOSEN'")),
        Index("ix_employee_optional_holidays_year", "company_id", "employee_id", "year"),
    )


class SourceChangeLog(TenantMixin, BaseModel):
    """Append-only: a source record changed and now has ``version_hash`` (3.4 §16, change detection).

    ``employee_id`` is NULL for records that reach many employees (holidays); ``date_from``/``date_to``
    bound the dates whose ledger the change can affect.
    """

    __tablename__ = "source_change_log"

    record_type = Column(String(20), nullable=False)
    record_id = Column(BigInteger, nullable=False)  # BIGINT since as_m3_days_policies (attendance_days, A-07)
    employee_id = Column(Integer, nullable=True)
    date_from = Column(Date, nullable=True)
    date_to = Column(Date, nullable=True)
    change = Column(String(10), nullable=False)
    version_hash = Column(String(64), nullable=False)
    changed_at = Column(DateTime(timezone=True), nullable=False)
    actor_user_id = Column(Integer, nullable=True)
    details = Column(JSONB, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_source_change_log_company_id_id"),
        _employee_fk("source_change_log"),
        CheckConstraint(f"record_type IN {CHANGE_RECORD_TYPES}", name="ck_source_change_log_record_type"),
        CheckConstraint(f"change IN {CHANGE_KINDS}", name="ck_source_change_log_change"),
        CheckConstraint("version_hash ~ '^[0-9a-f]{64}$'", name="ck_source_change_log_hash"),
        CheckConstraint("date_to IS NULL OR date_from IS NULL OR date_to >= date_from", name="ck_source_change_log_dates"),
        Index("ix_source_change_log_employee", "company_id", "employee_id", "date_from"),
        Index("ix_source_change_log_changed", "company_id", "changed_at"),
        Index("ix_source_change_log_record", "company_id", "record_type", "record_id"),
    )
