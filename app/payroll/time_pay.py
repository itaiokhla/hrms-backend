"""Time-based pay rules (3.4 §8–§12, §14, §15; ARCHITECTURE §4.6): the public facade.

Pure and deterministic: Decimal arithmetic in a fixed 28-digit context, no
floats, clock, randomness or environment. Policies are plain mappings (or
attribute objects) with the field names of 3.1 §5; missing fields take their
documented defaults, except the overtime, holiday-work and weekly-off multipliers,
which are never assumed (contract K:157); invalid ones raise
``TimePayError(INVALID_POLICY)``.
Impossible input raises ``TimePayError`` with a stable ``code``; problems the
spec turns into review items come back as ``Issue`` records. Every money result
is a ``TimePayLine`` rounded once to the currency's ISO 4217 minor unit, with
its unrounded value, sources and an explanation for the payroll trace.

    overtime            threshold_overtime, overtime_hours, overtime_pay, default_hourly_rate
    non-working days    holiday_premiums, holiday_on_weekly_off, non_salaried_holiday_pay
    attendance          late_penalty, shift_allowance
    tasks               task_pay, minimum_wage_check, task_clawback, split_amount
    loans and advances  loan_schedule, advance_schedule, loan_recovery, protected_net_room
    reimbursements      check_claim, claim_fingerprint, reimbursement_lines

Implementation lives in the ``time_pay_*`` modules; import from here.
"""

from __future__ import annotations

from app.payroll.time_pay_claims import (
    Claim,
    ClaimCheck,
    ClaimType,
    FxSnapshot,
    check_claim,
    claim_fingerprint,
    claim_type,
    reimbursement_lines,
)
from app.payroll.time_pay_common import (
    BLOCKING,
    DAY_TYPES,
    HOLIDAY,
    INFO,
    WARNING,
    WEEKLY_OFF,
    WORKDAY,
    DayRanges,
    Issue,
    TimePayError,
    TimePayLine,
    switch_on,
)
from app.payroll.time_pay_loans import (
    AdvanceRules,
    Instalment,
    LoanAccount,
    LoanRecovery,
    LoanRules,
    LoanTransaction,
    ProtectedNet,
    ProtectedRecovery,
    RepaymentSchedule,
    advance_schedule,
    check_advance_request,
    check_loan_application,
    loan_balance,
    loan_recoveries,
    loan_recovery,
    loan_schedule,
    loan_schedule_from_policy,
    parse_advance_policy,
    parse_loan_policy,
    protected_net_room,
    protected_recovery,
)
from app.payroll.time_pay_overtime import (
    CompOffCredit,
    OvertimeBucket,
    OvertimeEntry,
    OvertimeHours,
    OvertimePay,
    OvertimeRate,
    OvertimeRules,
    ThresholdOvertime,
    WorkedDay,
    default_hourly_rate,
    overtime_hours,
    overtime_pay,
    parse_overtime_policy,
    resolve_overtime_rate,
    round_minutes,
    threshold_overtime,
    validate_overtime_entries,
)
from app.payroll.time_pay_penalties import (
    LateMark,
    LatePenalty,
    LatePenaltyRules,
    is_early_exit,
    is_late,
    late_penalty,
    parse_late_penalty_policy,
)
from app.payroll.time_pay_premiums import (
    CompOffGrant,
    HolidayDay,
    HolidayOnWeeklyOff,
    HolidayPremiums,
    NonSalariedHolidayPay,
    NonWorkingDayWork,
    holiday_on_weekly_off,
    holiday_premiums,
    non_salaried_holiday_pay,
)
from app.payroll.time_pay_shift import (
    ShiftAllowance,
    ShiftWorked,
    night_minutes,
    parse_night_window,
    parse_shift_allowance_policy,
    shift_allowance,
)
from app.payroll.time_pay_tasks import (
    EmployeeAttributes,
    MinimumWageCheck,
    MinimumWageInput,
    RateCardRow,
    TaskAssignee,
    TaskClawback,
    TaskItem,
    TaskPay,
    TaskRecord,
    minimum_wage_check,
    parse_task_pay_policy,
    resolve_task_rate,
    split_amount,
    task_clawback,
    task_pay,
    validate_split,
)

__all__ = [
    "AdvanceRules", "BLOCKING", "Claim", "ClaimCheck", "ClaimType", "CompOffCredit", "CompOffGrant", "DAY_TYPES",
    "DayRanges", "EmployeeAttributes", "FxSnapshot", "HOLIDAY", "HolidayDay", "HolidayOnWeeklyOff", "HolidayPremiums",
    "INFO", "Instalment", "Issue", "LateMark", "LatePenalty", "LatePenaltyRules", "LoanAccount", "LoanRecovery",
    "LoanRules", "LoanTransaction", "MinimumWageCheck", "MinimumWageInput", "NonSalariedHolidayPay",
    "NonWorkingDayWork", "OvertimeBucket", "OvertimeEntry", "OvertimeHours", "OvertimePay", "OvertimeRate",
    "OvertimeRules", "ProtectedNet", "ProtectedRecovery", "RateCardRow", "RepaymentSchedule", "ShiftAllowance",
    "ShiftWorked", "TaskAssignee", "TaskClawback", "TaskItem", "TaskPay", "TaskRecord", "ThresholdOvertime",
    "TimePayError", "TimePayLine", "WARNING", "WEEKLY_OFF", "WORKDAY", "WorkedDay", "advance_schedule",
    "check_advance_request", "check_claim", "check_loan_application", "claim_fingerprint", "claim_type",
    "default_hourly_rate", "holiday_on_weekly_off", "holiday_premiums", "is_early_exit", "is_late", "late_penalty",
    "loan_balance", "loan_recoveries", "loan_recovery", "loan_schedule", "loan_schedule_from_policy",
    "minimum_wage_check", "night_minutes", "non_salaried_holiday_pay", "overtime_hours", "overtime_pay",
    "parse_advance_policy", "parse_late_penalty_policy", "parse_loan_policy", "parse_night_window",
    "parse_overtime_policy", "parse_shift_allowance_policy", "parse_task_pay_policy", "protected_net_room",
    "protected_recovery", "reimbursement_lines", "resolve_overtime_rate", "resolve_task_rate", "round_minutes",
    "shift_allowance", "split_amount", "switch_on", "task_clawback", "task_pay", "threshold_overtime",
    "validate_overtime_entries", "validate_split",
]
