"""Day ledger: exactly one classification per half-day slot (3.4 §4-§7 and §11).

Every date of the attendance cycle gets two slots, AM and PM, and each slot
takes the classification of the first precedence rule that matches (3.4 §4.2):

    1   NOT_EMPLOYED       before hire, after exit, outside the segment     neither paid nor LOP
    2   SUSPENDED_UNPAID   unpaid suspension                                 LOP
    3   LEAVE              approved leave, leave integration ON              per pay treatment (§7)
    3b  LEAVE_EXCUSED      approved leave, leave OFF, attendance ON          paid (ABSENT if so configured)
    4   HOLIDAY            paid holiday type on the branch calendar          paid (salaried)
    5   WEEKLY_OFF         weekly off in the work pattern                    paid (salaried)
    6   MANUAL_LOP         approved manual LOP input                         LOP
    7   PRESENT, HALF_DAY, ABSENT; UNMARKED and MISSING_PUNCH_OUT per ATTENDANCE_PAY_POLICY (attendance ON)
    8   PRESUMED_PRESENT   attendance OFF (paid), or the uncovered half of a half day (LOP unless half_day_value = 1)

A slot holds one thing, so the same time is never both leave and absence and
is never deducted twice (contract §21.2). The sandwich post-pass (§4.3) turns
weekly offs or holidays between two full LOP days into SANDWICH_LOP. Totals
(§4.4) are slot sums ÷ 2; penalty LOP for late marks (§11) is an input added to
LOP and capped by the paid working days, and late marks never reclassify a
slot.

Whatever the policy sends to review (unmarked days, missing punch-outs, zero
paid working days, ignored manual LOP, ...) is returned as LedgerException
data and the slot stays pending: nothing is deducted silently. Malformed or
contradictory input raises LedgerInputError. There is deliberately no GPS or
location input: location never affects pay, status or LOP (EC-ATT-08).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypeVar

from app.payroll.proration import CALC_CONTEXT, PeriodFacts, UnitFacts, canonical, decimal_value, plain

ZERO = Decimal(0)
HALF = Decimal("0.5")
ONE = Decimal(1)
TWO = Decimal(2)
HUNDRED = Decimal(100)
SECONDS_PER_HOUR = Decimal(3600)
HOURS_QUANTUM = Decimal("1E-10")


def hours_of(seconds: int) -> Decimal:
    """Hours of one day's seconds, rounded half-even to 10 decimal places, so per-day hours add up exactly."""
    with localcontext(CALC_CONTEXT):
        return canonical((Decimal(seconds) / SECONDS_PER_HOUR).quantize(HOURS_QUANTUM, rounding=ROUND_HALF_EVEN))

SourceId = int | str


class LedgerError(ValueError):
    """Base of ledger errors; ``code`` is a stable identifier for API mapping and tests."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class LedgerConfigError(LedgerError):
    """A LedgerConfig value is outside what the settings registry allows."""


class LedgerInputError(LedgerError):
    """Day input is malformed or contradicts itself (for example two leaves on one slot)."""


class Slot(StrEnum):
    AM = "AM"
    PM = "PM"


SLOTS: tuple[Slot, Slot] = (Slot.AM, Slot.PM)


class Portion(StrEnum):
    FULL = "FULL"
    FIRST_HALF = "FIRST_HALF"
    SECOND_HALF = "SECOND_HALF"

    @property
    def slots(self) -> tuple[Slot, ...]:
        return _PORTION_SLOTS[self]


_PORTION_SLOTS: dict[Portion, tuple[Slot, ...]] = {
    Portion.FULL: SLOTS,
    Portion.FIRST_HALF: (Slot.AM,),
    Portion.SECOND_HALF: (Slot.PM,),
}


class PayTreatment(StrEnum):
    FULL = "FULL"
    UNPAID = "UNPAID"
    PARTIAL = "PARTIAL"
    TIERED = "TIERED"
    STATUTORY = "STATUTORY"


class HolidayType(StrEnum):
    PUBLIC = "PUBLIC"
    COMPANY = "COMPANY"
    OPTIONAL_AVAILED = "OPTIONAL_AVAILED"
    OPTIONAL_NOT_AVAILED = "OPTIONAL_NOT_AVAILED"


class AttendanceStatus(StrEnum):
    PRESENT = "PRESENT"
    LATE = "LATE"
    HALF_DAY = "HALF_DAY"
    ABSENT = "ABSENT"


class Classification(StrEnum):
    NOT_EMPLOYED = "NOT_EMPLOYED"
    SUSPENDED_UNPAID = "SUSPENDED_UNPAID"
    LEAVE = "LEAVE"
    LEAVE_EXCUSED = "LEAVE_EXCUSED"
    HOLIDAY = "HOLIDAY"
    WEEKLY_OFF = "WEEKLY_OFF"
    MANUAL_LOP = "MANUAL_LOP"
    PRESENT = "PRESENT"
    HALF_DAY = "HALF_DAY"
    ABSENT = "ABSENT"
    UNMARKED = "UNMARKED"
    MISSING_PUNCH_OUT = "MISSING_PUNCH_OUT"
    PRESUMED_PRESENT = "PRESUMED_PRESENT"
    SANDWICH_LOP = "SANDWICH_LOP"


class OverrideAction(StrEnum):
    TREAT_AS_PRESENT = "TREAT_AS_PRESENT"
    TREAT_AS_ABSENT = "TREAT_AS_ABSENT"
    TREAT_AS_HALF_DAY = "TREAT_AS_HALF_DAY"


class Severity(StrEnum):
    BLOCKING = "BLOCKING"
    WARNING = "WARNING"
    INFO = "INFO"


class ExceptionCode(StrEnum):
    UNMARKED_DAY = "UNMARKED_DAY"
    MISSING_PUNCH_OUT = "MISSING_PUNCH_OUT"
    ZERO_PAID_WORKING_DAYS = "ZERO_PAID_WORKING_DAYS"
    MANUAL_LOP_CONFLICT = "MANUAL_LOP_CONFLICT"
    MANUAL_LOP_IGNORED = "MANUAL_LOP_IGNORED"
    LEAVE_ON_NON_WORKING_DAY = "LEAVE_ON_NON_WORKING_DAY"
    WORKED_ON_LEAVE = "WORKED_ON_LEAVE"
    ABSENT_WITH_HOURS = "ABSENT_WITH_HOURS"
    OVERRIDE_NOT_APPLIED = "OVERRIDE_NOT_APPLIED"
    INPUT_OUTSIDE_EMPLOYMENT = "INPUT_OUTSIDE_EMPLOYMENT"
    STATUTORY_LEAVE = "STATUTORY_LEAVE"
    PENALTY_LOP_CAPPED = "PENALTY_LOP_CAPPED"
    HOLIDAY_ELIGIBILITY_ASSUMED = "HOLIDAY_ELIGIBILITY_ASSUMED"


class ZeroPayReason(StrEnum):
    NO_PAID_WORKING_DAYS = "NO_PAID_WORKING_DAYS"
    HOLIDAYS_COVER_ALL_WORKING_DAYS = "HOLIDAYS_COVER_ALL_WORKING_DAYS"
    NO_WORKING_DAYS_IN_EMPLOYMENT = "NO_WORKING_DAYS_IN_EMPLOYMENT"


class LeaveDisabledTreatment(StrEnum):
    PAID_EXCUSED = "PAID_EXCUSED"
    ABSENT = "ABSENT"


class UnmarkedDayPolicy(StrEnum):
    EXCEPTION = "EXCEPTION"
    ABSENT = "ABSENT"
    PRESENT = "PRESENT"


class MissingPunchOutPolicy(StrEnum):
    EXCEPTION = "EXCEPTION"
    HALF_DAY = "HALF_DAY"
    FULL_DAY = "FULL_DAY"
    ABSENT = "ABSENT"


class SandwichRule(StrEnum):
    OFF = "OFF"
    WEEKLY_OFFS = "WEEKLY_OFFS"
    HOLIDAYS = "HOLIDAYS"
    BOTH = "BOTH"


class ZeroPaidDaysPolicy(StrEnum):
    EXCEPTION = "EXCEPTION"
    PAY_ZERO = "PAY_ZERO"


class NonSalariedHolidayPay(StrEnum):
    OFF = "OFF"
    STANDARD_HOURS = "STANDARD_HOURS"
    AVERAGE_DAILY_EARNINGS = "AVERAGE_DAILY_EARNINGS"


class AdjacentDays(StrEnum):
    NONE = "NONE"
    DAY_BEFORE = "DAY_BEFORE"
    DAY_AFTER = "DAY_AFTER"
    BOTH = "BOTH"


class HolidayOnWeeklyOff(StrEnum):
    NONE = "NONE"
    SUBSTITUTE_DAY = "SUBSTITUTE_DAY"
    EXTRA_DAY_PAY = "EXTRA_DAY_PAY"


_E = TypeVar("_E", bound=StrEnum)


def _choice(
    enum: type[_E], value: object, what: str, *, code: str = "INVALID_CHOICE", error: type[LedgerError] = LedgerInputError
) -> _E:
    try:
        return enum(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise error(code, f"{what} must be one of {allowed}, got {value!r}") from None


def _flag(value: object, what: str, error: type[LedgerError] = LedgerInputError) -> bool:
    if not isinstance(value, bool):
        raise error("INVALID_FLAG", f"{what} must be a bool, got {type(value).__name__}")
    return value


def _date(value: object, what: str) -> dt.date:
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise LedgerInputError("INVALID_DATE", f"{what} must be a date, got {type(value).__name__}")
    return value


def _source_id(value: object, what: str, *, optional: bool = False) -> SourceId | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or value == "":
        raise LedgerInputError("INVALID_SOURCE_ID", f"{what} must be an int or a non-empty string, got {value!r}")
    return value


def _content_hash(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise LedgerInputError("INVALID_SOURCE_ID", "content_hash must be a string")
    return value


def _seconds(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerInputError("INVALID_SECONDS", f"{what} must be a non-negative integer, got {value!r}")
    return value


def _number(value: object, what: str, error: type[LedgerError] = LedgerInputError) -> Decimal:
    return decimal_value(value, what, error)


def _percent(value: object, what: str) -> Decimal:
    result = _number(value, what)
    if not ZERO <= result <= HUNDRED:
        raise LedgerInputError("INVALID_PERCENT", f"{what} must be between 0 and 100, got {value!r}")
    return result


def _half_days(value: object, what: str) -> Decimal:
    result = _number(value, what)
    if result < 0 or result * TWO != (result * TWO).to_integral_value():
        raise LedgerInputError("INVALID_DAYS", f"{what} must be a non-negative multiple of 0.5, got {value!r}")
    return result


def _hours(value: object, what: str, *, low: Decimal, error: type[LedgerError]) -> Decimal:
    result = _number(value, what, error)
    if not low <= result <= Decimal(24):
        raise error("INVALID_HOURS", f"{what} must be between {plain(low)} and 24, got {value!r}")
    return result


def _items(value: object, item_type: type, what: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise LedgerInputError("INVALID_INPUT", f"{what} must be a sequence of {item_type.__name__}")
    items = tuple(value)
    for item in items:
        if not isinstance(item, item_type):
            raise LedgerInputError("INVALID_INPUT", f"{what} must contain {item_type.__name__}, got {type(item).__name__}")
    return items


def _no_overlap(items: Iterable[Any], code: str, when: dt.date) -> None:
    seen: set[Slot] = set()
    for item in items:
        for slot in item.portion.slots:
            if slot in seen:
                raise LedgerInputError(code, f"two inputs cover the {slot.value} slot of {when.isoformat()}")
            seen.add(slot)


@dataclass(frozen=True)
class SourceRef:
    """The record behind a slot or a day (its table, id and version hash), for "Why this amount?"."""

    type: str
    id: SourceId | None = None
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "content_hash": self.content_hash}


@dataclass(frozen=True)
class Tier:
    """Leave days ``from_day`` to ``to_day`` (inclusive; None = no end) of the rolling window, paid at ``percent``."""

    from_day: int
    to_day: int | None
    percent: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.from_day, bool) or not isinstance(self.from_day, int) or self.from_day < 1:
            raise LedgerInputError("INVALID_TIERS", f"from_day must be an integer >= 1, got {self.from_day!r}")
        if self.to_day is not None and (
            isinstance(self.to_day, bool) or not isinstance(self.to_day, int) or self.to_day < self.from_day
        ):
            raise LedgerInputError("INVALID_TIERS", f"to_day must be an integer >= from_day, got {self.to_day!r}")
        object.__setattr__(self, "percent", _percent(self.percent, "tier percent"))

    def covers(self, day_number: int) -> bool:
        return self.from_day <= day_number and (self.to_day is None or day_number <= self.to_day)


@dataclass(frozen=True)
class TieredPay:
    """TIERED pay (3.4 §7.2): the paid percent follows the day's position in a rolling window.

    ``days_before_period`` counts days of the leave type taken in the rolling
    window that ends the day before the period starts. ``rolls_off`` lists
    ``(date, days)`` for those earlier days that leave the window during the
    period, so the position is exact on every date (EC-LV-05). Days beyond every
    tier are unpaid.
    """

    tiers: tuple[Tier, ...]
    days_before_period: Decimal = ZERO
    rolls_off: tuple[tuple[dt.date, Decimal], ...] = ()

    def __post_init__(self) -> None:
        tiers = _items(self.tiers, Tier, "tiers")
        if not tiers:
            raise LedgerInputError("INVALID_TIERS", "tiered pay needs at least one tier")
        for earlier, later in zip(tiers, tiers[1:]):
            if earlier.to_day is None or later.from_day <= earlier.to_day:
                raise LedgerInputError("INVALID_TIERS", "tiers must be in order and must not overlap")
        before = _half_days(self.days_before_period, "days_before_period")
        if isinstance(self.rolls_off, (str, bytes, Mapping)) or not isinstance(self.rolls_off, Iterable):
            raise LedgerInputError("INVALID_TIERED_HISTORY", "rolls_off must be a sequence of (date, days)")
        rolls: list[tuple[dt.date, Decimal]] = []
        for item in self.rolls_off:
            if not isinstance(item, tuple) or len(item) != 2:
                raise LedgerInputError("INVALID_TIERED_HISTORY", f"a roll-off is (date, days), got {item!r}")
            days = _half_days(item[1], "roll-off days")
            if days == 0:
                raise LedgerInputError("INVALID_TIERED_HISTORY", "a roll-off must remove a positive number of days")
            rolls.append((_date(item[0], "roll-off date"), days))
        if sum((days for _, days in rolls), ZERO) > before:
            raise LedgerInputError("INVALID_TIERED_HISTORY", "more days roll off than were taken before the period")
        object.__setattr__(self, "tiers", tiers)
        object.__setattr__(self, "days_before_period", before)
        object.__setattr__(self, "rolls_off", tuple(sorted(rolls)))

    def days_in_window_before(self, when: dt.date) -> Decimal:
        """Earlier days of this leave type still inside the rolling window on ``when``."""
        return self.days_before_period - sum((days for day, days in self.rolls_off if day <= when), ZERO)

    def percent_for_day(self, day_number: int) -> Decimal:
        for tier in self.tiers:
            if tier.covers(day_number):
                return tier.percent
        return ZERO


@dataclass(frozen=True)
class LeavePortion:
    """An approved leave on one date (a ``leave_days`` row) with its pay treatment snapshot."""

    leave_id: SourceId
    leave_type_code: str
    portion: Portion = Portion.FULL
    pay_treatment: PayTreatment = PayTreatment.FULL
    partial_percent: Decimal | None = None
    tiered: TieredPay | None = None
    counts_non_working_days: bool = False
    content_hash: str | None = None

    def __post_init__(self) -> None:
        _source_id(self.leave_id, "leave_id")
        if not isinstance(self.leave_type_code, str) or not self.leave_type_code:
            raise LedgerInputError("INVALID_INPUT", "leave_type_code must be a non-empty string")
        object.__setattr__(self, "portion", _choice(Portion, self.portion, "portion", code="INVALID_PORTION"))
        treatment = _choice(PayTreatment, self.pay_treatment, "pay_treatment", code="INVALID_PAY_TREATMENT")
        object.__setattr__(self, "pay_treatment", treatment)
        if treatment is PayTreatment.PARTIAL:
            if self.partial_percent is None:
                raise LedgerInputError("INVALID_LEAVE_PAY", "PARTIAL leave needs partial_percent")
            object.__setattr__(self, "partial_percent", _percent(self.partial_percent, "partial_percent"))
        elif self.partial_percent is not None:
            raise LedgerInputError("INVALID_LEAVE_PAY", f"partial_percent is only valid for PARTIAL, not {treatment}")
        if treatment is PayTreatment.TIERED:
            if not isinstance(self.tiered, TieredPay):
                raise LedgerInputError("INVALID_LEAVE_PAY", "TIERED leave needs tiered pay information")
        elif self.tiered is not None:
            raise LedgerInputError("INVALID_LEAVE_PAY", f"tiered information is only valid for TIERED, not {treatment}")
        _flag(self.counts_non_working_days, "counts_non_working_days")
        _content_hash(self.content_hash)


@dataclass(frozen=True)
class HolidayInput:
    """A holiday on the branch calendar of the employee's branch on that date."""

    holiday_id: SourceId
    type: HolidayType
    name: str = ""
    content_hash: str | None = None

    def __post_init__(self) -> None:
        _source_id(self.holiday_id, "holiday_id")
        object.__setattr__(self, "type", _choice(HolidayType, self.type, "holiday type"))
        if not isinstance(self.name, str):
            raise LedgerInputError("INVALID_INPUT", "holiday name must be a string")
        _content_hash(self.content_hash)

    def to_dict(self) -> dict[str, Any]:
        return {"holiday_id": self.holiday_id, "type": self.type.value, "name": self.name, "content_hash": self.content_hash}


@dataclass(frozen=True)
class ManualLop:
    """An approved manual LOP input naming this date."""

    source_id: SourceId
    portion: Portion = Portion.FULL
    content_hash: str | None = None

    def __post_init__(self) -> None:
        _source_id(self.source_id, "manual LOP source_id")
        object.__setattr__(self, "portion", _choice(Portion, self.portion, "portion", code="INVALID_PORTION"))
        _content_hash(self.content_hash)


@dataclass(frozen=True)
class OnDuty:
    """Approved on-duty or field work for this date."""

    source_id: SourceId
    portion: Portion = Portion.FULL
    content_hash: str | None = None

    def __post_init__(self) -> None:
        _source_id(self.source_id, "on-duty source_id")
        object.__setattr__(self, "portion", _choice(Portion, self.portion, "portion", code="INVALID_PORTION"))
        _content_hash(self.content_hash)


@dataclass(frozen=True)
class AttendanceInput:
    """The approved attendance session dated on this day (its local start date, EC-ATT-07).

    ``worked_seconds`` is canonical net time from ``attendance_hours.compute``
    (gross minus unpaid breaks); it is None when the punch-out is missing,
    because an end time is never invented (EC-ATT-02).
    """

    status: AttendanceStatus
    session_id: SourceId | None = None
    closed_without_punch_out: bool = False
    worked_seconds: int | None = None
    late: bool = False
    early_exit: bool = False
    regularised: bool = False
    night_seconds: int = 0
    content_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _choice(AttendanceStatus, self.status, "attendance status"))
        _source_id(self.session_id, "session_id", optional=True)
        for name in ("closed_without_punch_out", "late", "early_exit", "regularised"):
            _flag(getattr(self, name), name)
        if self.worked_seconds is not None:
            _seconds(self.worked_seconds, "worked_seconds")
        _seconds(self.night_seconds, "night_seconds")
        if self.closed_without_punch_out and self.worked_seconds is not None:
            raise LedgerInputError(
                "INCONSISTENT_ATTENDANCE", "a session closed without a punch-out cannot carry worked time"
            )
        if self.worked_seconds is None and self.night_seconds:
            raise LedgerInputError("INCONSISTENT_ATTENDANCE", "night time needs a complete session")
        _content_hash(self.content_hash)


@dataclass(frozen=True)
class DayInput:
    """Everything known about one employee-date, already approved and resolved to that date.

    ``weekly_off`` and ``holiday`` must be filled for every date of the period,
    employed or not, because WORKING_DAYS and HOURS bases count the whole
    period. ``standard_hours`` overrides the configured hours for the date (FTE
    changes); ``segment`` names the assignment segment the date belongs to.
    """

    date: dt.date
    employed: bool = True
    suspended_unpaid: bool = False
    leaves: tuple[LeavePortion, ...] = ()
    holiday: HolidayInput | None = None
    weekly_off: bool = False
    manual_lop: tuple[ManualLop, ...] = ()
    attendance: AttendanceInput | None = None
    on_duty: tuple[OnDuty, ...] = ()
    segment: str = ""
    standard_hours: Decimal | None = None
    assignment_id: SourceId | None = None
    suspension_id: SourceId | None = None
    work_week_id: SourceId | None = None

    def __post_init__(self) -> None:
        when = _date(self.date, "date")
        for name in ("employed", "suspended_unpaid", "weekly_off"):
            _flag(getattr(self, name), name)
        object.__setattr__(self, "leaves", _items(self.leaves, LeavePortion, "leaves"))
        object.__setattr__(self, "manual_lop", _items(self.manual_lop, ManualLop, "manual_lop"))
        object.__setattr__(self, "on_duty", _items(self.on_duty, OnDuty, "on_duty"))
        if self.holiday is not None and not isinstance(self.holiday, HolidayInput):
            raise LedgerInputError("INVALID_INPUT", "holiday must be a HolidayInput")
        if self.attendance is not None and not isinstance(self.attendance, AttendanceInput):
            raise LedgerInputError("INVALID_INPUT", "attendance must be an AttendanceInput")
        if not isinstance(self.segment, str):
            raise LedgerInputError("INVALID_INPUT", "segment must be a string")
        if self.standard_hours is not None:
            hours = _hours(self.standard_hours, "standard_hours", low=ZERO, error=LedgerInputError)
            if hours == 0:
                raise LedgerInputError("INVALID_HOURS", "standard_hours must be positive")
            object.__setattr__(self, "standard_hours", hours)
        for name in ("assignment_id", "suspension_id", "work_week_id"):
            _source_id(getattr(self, name), name, optional=True)
        _no_overlap(self.leaves, "OVERLAPPING_LEAVE", when)
        _no_overlap(self.manual_lop, "OVERLAPPING_MANUAL_LOP", when)
        _no_overlap(self.on_duty, "OVERLAPPING_ON_DUTY", when)


_PAYABLE_HOLIDAY_TYPES = frozenset({HolidayType.PUBLIC, HolidayType.COMPANY, HolidayType.OPTIONAL_AVAILED})
_HALF_DAY_VALUES = (ZERO, HALF, ONE)


@dataclass(frozen=True)
class LedgerConfig:
    """Resolved switches and ATTENDANCE/LEAVE/HOLIDAY_PAY policy fields for one employee and period.

    The defaults are the platform defaults of 3.1 §5.2-§5.4 with every switch
    OFF, so a default config pays a salaried employee for every employed day.
    ``non_salaried_holiday_pay`` and ``holiday_on_weekly_off`` are the policy
    values when ``payroll.holiday_pay.enabled`` is ON; pass OFF/NONE otherwise.
    """

    attendance_enabled: bool = False
    leave_enabled: bool = False
    when_disabled_treat_approved_leave_as: LeaveDisabledTreatment = LeaveDisabledTreatment.PAID_EXCUSED
    half_day_value: Decimal = HALF
    unmarked_day: UnmarkedDayPolicy = UnmarkedDayPolicy.EXCEPTION
    missing_punch_out: MissingPunchOutPolicy = MissingPunchOutPolicy.EXCEPTION
    derive_status_from_hours: bool = False
    full_day_min_hours: Decimal = Decimal("8.0")
    half_day_min_hours: Decimal = Decimal("4.0")
    count_on_duty_as_present: bool = True
    sandwich_rule: SandwichRule = SandwichRule.OFF
    zero_paid_days: ZeroPaidDaysPolicy = ZeroPaidDaysPolicy.EXCEPTION
    paid_holiday_types: frozenset[HolidayType] = _PAYABLE_HOLIDAY_TYPES
    standard_hours_per_day: Decimal = Decimal("8.0")
    employee_is_salaried: bool = True
    non_salaried_holiday_pay: NonSalariedHolidayPay = NonSalariedHolidayPay.OFF
    holiday_eligibility_adjacent_days: AdjacentDays = AdjacentDays.NONE
    holiday_on_weekly_off: HolidayOnWeeklyOff = HolidayOnWeeklyOff.NONE

    def __post_init__(self) -> None:
        error = LedgerConfigError
        for name in (
            "attendance_enabled",
            "leave_enabled",
            "derive_status_from_hours",
            "count_on_duty_as_present",
            "employee_is_salaried",
        ):
            _flag(getattr(self, name), name, error)
        for name, enum in (
            ("when_disabled_treat_approved_leave_as", LeaveDisabledTreatment),
            ("unmarked_day", UnmarkedDayPolicy),
            ("missing_punch_out", MissingPunchOutPolicy),
            ("sandwich_rule", SandwichRule),
            ("zero_paid_days", ZeroPaidDaysPolicy),
            ("non_salaried_holiday_pay", NonSalariedHolidayPay),
            ("holiday_eligibility_adjacent_days", AdjacentDays),
            ("holiday_on_weekly_off", HolidayOnWeeklyOff),
        ):
            object.__setattr__(self, name, _choice(enum, getattr(self, name), name, error=error))
        half_day = _number(self.half_day_value, "half_day_value", error)
        if half_day not in _HALF_DAY_VALUES:
            raise error("INVALID_HALF_DAY_VALUE", f"half_day_value must be 0, 0.5 or 1, got {self.half_day_value!r}")
        object.__setattr__(self, "half_day_value", canonical(half_day))
        standard = _hours(self.standard_hours_per_day, "standard_hours_per_day", low=ZERO, error=error)
        if standard == 0:
            raise error("INVALID_HOURS", "standard_hours_per_day must be positive")
        object.__setattr__(self, "standard_hours_per_day", standard)
        full = _hours(self.full_day_min_hours, "full_day_min_hours", low=ZERO, error=error)
        half = _hours(self.half_day_min_hours, "half_day_min_hours", low=ZERO, error=error)
        if half > full:
            raise error("INVALID_HOURS", "half_day_min_hours must not exceed full_day_min_hours")
        object.__setattr__(self, "full_day_min_hours", full)
        object.__setattr__(self, "half_day_min_hours", half)
        if isinstance(self.paid_holiday_types, (str, bytes)) or not isinstance(self.paid_holiday_types, Iterable):
            raise error("INVALID_CHOICE", "paid_holiday_types must be a set of holiday types")
        paid_types = frozenset(_choice(HolidayType, t, "paid holiday type", error=error) for t in self.paid_holiday_types)
        if not paid_types <= _PAYABLE_HOLIDAY_TYPES:
            raise error("INVALID_CHOICE", "paid_holiday_types may contain PUBLIC, COMPANY and OPTIONAL_AVAILED only")
        object.__setattr__(self, "paid_holiday_types", paid_types)
        if self.holiday_eligibility_adjacent_days is not AdjacentDays.NONE and not self.attendance_enabled:
            raise error("ADJACENT_DAYS_NEED_ATTENDANCE", "adjacent-day holiday eligibility requires attendance ON (V-03)")


@dataclass(frozen=True)
class LedgerOverride:
    """A reviewer's resolution of a day held for review; recorded as the source of the slots it decides."""

    action: OverrideAction
    source_id: SourceId | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _choice(OverrideAction, self.action, "override action", code="INVALID_OVERRIDE"))
        _source_id(self.source_id, "override source_id", optional=True)


@dataclass(frozen=True)
class SlotResult:
    """One half-day slot: its classification, the rule and record that decided it, and its pay.

    ``paid`` and ``lop`` are fractions of the slot. Employed slots have
    ``paid + lop == 1`` except pending slots (held for review) and unpaid rest
    days of non-salaried employees, which are neither paid nor LOP.
    """

    slot: Slot
    classification: Classification
    rule: str
    paid: Decimal
    lop: Decimal
    pending: bool = False
    source: SourceRef | None = None
    detail: str | None = None
    leave_type_code: str | None = None
    pay_treatment: PayTreatment | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot.value,
            "classification": self.classification.value,
            "rule": self.rule,
            "paid": plain(self.paid),
            "lop": plain(self.lop),
            "pending": self.pending,
            "source": self.source.to_dict() if self.source is not None else None,
            "detail": self.detail,
            "leave_type_code": self.leave_type_code,
            "pay_treatment": self.pay_treatment.value if self.pay_treatment is not None else None,
        }


@dataclass(frozen=True)
class DayRecord:
    """One date of the ledger: both slots plus the hours and marks recorded on it.

    Session time and marks are taken only when attendance is ON, because punches
    never change pay when it is OFF (3.4 §6). ``hours_recorded`` says a complete
    session's net time was taken (possibly zero); otherwise paid presence is
    presumed at standard hours for payable hours.
    """

    date: dt.date
    segment: str
    employed: bool
    scheduled_working_day: bool
    weekly_off: bool
    paid_holiday: bool
    holiday: HolidayInput | None
    am: SlotResult
    pm: SlotResult
    standard_hours: Decimal
    worked_seconds: int = 0
    night_seconds: int = 0
    hours_recorded: bool = False
    late_mark: bool = False
    early_exit: bool = False
    late_mark_regularised: bool = False
    early_exit_regularised: bool = False
    half_day: bool = False
    holiday_pay_eligible: bool | None = None
    inputs: tuple[SourceRef, ...] = ()

    @property
    def slots(self) -> tuple[SlotResult, SlotResult]:
        return (self.am, self.pm)

    @property
    def paid(self) -> Decimal:
        return canonical((self.am.paid + self.pm.paid) / TWO)

    @property
    def lop(self) -> Decimal:
        return canonical((self.am.lop + self.pm.lop) / TWO)

    @property
    def holiday_on_weekly_off(self) -> bool:
        return (
            self.employed
            and self.weekly_off
            and self.paid_holiday
            and all(s.classification is Classification.HOLIDAY for s in self.slots)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "segment": self.segment,
            "employed": self.employed,
            "scheduled_working_day": self.scheduled_working_day,
            "weekly_off": self.weekly_off,
            "paid_holiday": self.paid_holiday,
            "holiday": self.holiday.to_dict() if self.holiday is not None else None,
            "slots": [self.am.to_dict(), self.pm.to_dict()],
            "paid": plain(self.paid),
            "lop": plain(self.lop),
            "standard_hours": plain(self.standard_hours),
            "worked_seconds": self.worked_seconds,
            "night_seconds": self.night_seconds,
            "hours_recorded": self.hours_recorded,
            "late_mark": self.late_mark,
            "early_exit": self.early_exit,
            "late_mark_regularised": self.late_mark_regularised,
            "early_exit_regularised": self.early_exit_regularised,
            "half_day": self.half_day,
            "holiday_on_weekly_off": self.holiday_on_weekly_off,
            "holiday_pay_eligible": self.holiday_pay_eligible,
            "inputs": [ref.to_dict() for ref in self.inputs],
        }


@dataclass(frozen=True)
class LedgerException:
    """A review item: data, never raised. BLOCKING items stop approval until resolved."""

    code: ExceptionCode
    severity: Severity
    message: str
    day: dt.date | None = None
    slots: tuple[Slot, ...] = ()
    sources: tuple[SourceRef, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "day": self.day.isoformat() if self.day is not None else None,
            "slots": [slot.value for slot in self.slots],
            "sources": [ref.to_dict() for ref in self.sources],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ZeroPayGuard:
    """3.4 §5.3: an employed employee without a paid scheduled working day (EC-ATT-09, EC-ATT-10)."""

    triggered: bool
    reason: ZeroPayReason | None = None
    pay_zero: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "reason": self.reason.value if self.reason is not None else None,
            "pay_zero": self.pay_zero,
        }


@dataclass(frozen=True)
class LeaveTypeDays:
    """Leave days of one leave type: paid, unpaid (LOP), statutory and excused (leave OFF, attendance ON)."""

    leave_type_code: str
    paid_days: Decimal
    unpaid_days: Decimal
    statutory_days: Decimal
    excused_days: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "leave_type_code": self.leave_type_code,
            "paid_days": plain(self.paid_days),
            "unpaid_days": plain(self.unpaid_days),
            "statutory_days": plain(self.statutory_days),
            "excused_days": plain(self.excused_days),
        }


@dataclass(frozen=True)
class LedgerTotals:
    """3.4 §4.4 outputs for the whole ledger or one segment; day counts are slot sums ÷ 2.

    ``paid_days + slot_lop_days + pending_days + unpaid_days == employment_days``.
    ``lop_days`` and ``lop_days_on_working_days`` include ``penalty_lop_days``
    (the applied, capped penalty), which ``paid_days`` does not.
    ``payable_hours`` is worked hours plus paid leave, paid holiday and
    ``presumed_hours`` (standard hours of paid presence on a day without
    session time, such as attendance OFF); ``worked_hours`` is never invented.
    Hours are per-day ``hours_of(seconds)`` summed; the exact seconds are kept too.
    """

    days: int
    employment_days: Decimal
    scheduled_working_days: Decimal
    non_weekly_off_days: Decimal
    standard_hours: Decimal
    paid_days: Decimal
    lop_days: Decimal
    lop_days_on_working_days: Decimal
    slot_lop_days: Decimal
    penalty_lop_days: Decimal
    paid_days_after_penalty: Decimal
    paid_working_days: Decimal
    pending_days: Decimal
    unpaid_days: Decimal
    present_days: Decimal
    absent_days: Decimal
    half_days: Decimal
    presumed_present_days: Decimal
    leave_days_paid: Decimal
    leave_days_unpaid: Decimal
    leave_days_statutory: Decimal
    leave_days_excused: Decimal
    holidays: Decimal
    weekly_offs: Decimal
    holidays_on_weekly_off: Decimal
    extra_day_pay_days: Decimal
    substitute_days: Decimal
    manual_lop_days: Decimal
    suspended_days: Decimal
    sandwich_lop_days: Decimal
    worked_hours: Decimal
    paid_leave_hours: Decimal
    paid_holiday_hours: Decimal
    presumed_hours: Decimal
    payable_hours: Decimal
    night_hours: Decimal
    worked_seconds: int
    night_seconds: int
    late_marks: int
    early_exits: int
    regularised_late_marks: int
    regularised_early_exits: int
    leave_by_type: tuple[LeaveTypeDays, ...] = ()

    def unit_facts(self) -> UnitFacts:
        return UnitFacts(
            employment_days=self.employment_days,
            scheduled_working_days=self.scheduled_working_days,
            non_weekly_off_days=self.non_weekly_off_days,
            lop_days=self.lop_days,
            lop_days_on_working_days=self.lop_days_on_working_days,
            payable_hours=self.payable_hours,
            standard_hours=self.standard_hours,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, Decimal):
                result[item.name] = plain(value)
            elif item.name == "leave_by_type":
                result[item.name] = [entry.to_dict() for entry in value]
            else:
                result[item.name] = value
        return result


@dataclass(frozen=True)
class DayLedger:
    """The classified period for one employee: days, totals (overall and per segment), review items."""

    days: tuple[DayRecord, ...]
    totals: LedgerTotals
    segments: Mapping[str, LedgerTotals]
    exceptions: tuple[LedgerException, ...]
    guard: ZeroPayGuard
    period: PeriodFacts
    config: LedgerConfig
    penalty_lop_days_requested: Decimal = ZERO

    @property
    def blocking(self) -> tuple[LedgerException, ...]:
        return tuple(item for item in self.exceptions if item.severity is Severity.BLOCKING)

    @property
    def segment_keys(self) -> tuple[str, ...]:
        return tuple(self.segments)

    def segment(self, key: str) -> LedgerTotals:
        try:
            return self.segments[key]
        except KeyError:
            raise LedgerInputError("UNKNOWN_SEGMENT", f"no segment {key!r} in this ledger") from None

    def unit_facts(self, segment: str | None = None) -> UnitFacts:
        """Proration facts of the whole employment (None) or of one segment."""
        return (self.totals if segment is None else self.segment(segment)).unit_facts()

    def segment_facts(self) -> tuple[UnitFacts, ...]:
        """Proration facts of every segment, in date order, for ``proration.prorate_segments``."""
        return tuple(totals.unit_facts() for totals in self.segments.values())

    def day(self, when: dt.date) -> DayRecord:
        for record in self.days:
            if record.date == when:
                return record
        raise LedgerInputError("UNKNOWN_DATE", f"{when!r} is not in this ledger")

    def lop_dates(self, segment: str | None = None) -> tuple[dt.date, ...]:
        """Dates with any LOP slot, for the line trace ("2 LOP days: 12-13 Nov")."""
        return tuple(
            record.date
            for record in self.days
            if (segment is None or record.segment == segment) and record.lop > 0
        )

    def with_penalty_lop(self, days: Decimal | int | str) -> DayLedger:
        """The same classification with a different penalty LOP (from ``time_pay.late_penalty``)."""
        kept = [item for item in self.exceptions if item.code not in _PERIOD_CODES]
        with localcontext(CALC_CONTEXT):
            return _assemble(self.days, self.config, _penalty(days), kept)

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": [record.to_dict() for record in self.days],
            "totals": self.totals.to_dict(),
            "segments": {key: totals.to_dict() for key, totals in self.segments.items()},
            "exceptions": [item.to_dict() for item in self.exceptions],
            "guard": self.guard.to_dict(),
            "period": self.period.to_dict(),
            "penalty_lop_days_requested": plain(self.penalty_lop_days_requested),
        }


# --- classification ---------------------------------------------------------------------------------------------

LeavePay = Callable[[LeavePortion, dt.date], tuple[Decimal, Decimal, str | None]]

_OVERRIDE_OUTCOME = {
    OverrideAction.TREAT_AS_PRESENT: Classification.PRESENT,
    OverrideAction.TREAT_AS_ABSENT: Classification.ABSENT,
    OverrideAction.TREAT_AS_HALF_DAY: Classification.HALF_DAY,
}
_LEAVE_RULES = frozenset({"3", "3b"})
_PERIOD_CODES = frozenset(
    {ExceptionCode.ZERO_PAID_WORKING_DAYS, ExceptionCode.PENALTY_LOP_CAPPED, ExceptionCode.STATUTORY_LEAVE}
)
_SANDWICH_TARGETS = {
    SandwichRule.WEEKLY_OFFS: frozenset({Classification.WEEKLY_OFF}),
    SandwichRule.HOLIDAYS: frozenset({Classification.HOLIDAY}),
    SandwichRule.BOTH: frozenset({Classification.WEEKLY_OFF, Classification.HOLIDAY}),
}


class _TieredPositions:
    """Running position of TIERED leave days per leave type, in date and slot order."""

    def __init__(self) -> None:
        self._taken: dict[str, Decimal] = {}
        self._pay: dict[str, TieredPay] = {}

    def percent(self, leave: LeavePortion, when: dt.date) -> tuple[int, Decimal]:
        tiered = leave.tiered
        assert tiered is not None
        known = self._pay.setdefault(leave.leave_type_code, tiered)
        if known != tiered:
            raise LedgerInputError(
                "INCONSISTENT_TIERED_INFO", f"leave type {leave.leave_type_code!r} has two different tiered histories"
            )
        taken = self._taken.get(leave.leave_type_code, ZERO) + HALF
        self._taken[leave.leave_type_code] = taken
        position = tiered.days_in_window_before(when) + taken
        day_number = int(position.to_integral_value(rounding=ROUND_CEILING))
        return day_number, tiered.percent_for_day(day_number)


def _leave_pay(positions: _TieredPositions) -> LeavePay:
    def pay(leave: LeavePortion, when: dt.date) -> tuple[Decimal, Decimal, str | None]:
        treatment = leave.pay_treatment
        if treatment is PayTreatment.FULL:
            return ONE, ZERO, None
        if treatment is PayTreatment.UNPAID:
            return ZERO, ONE, None
        if treatment is PayTreatment.STATUTORY:
            return ZERO, ONE, "Statutory pay comes from the country pack"
        if treatment is PayTreatment.PARTIAL:
            assert leave.partial_percent is not None
            share = leave.partial_percent / HUNDRED
            return share, ONE - share, f"PARTIAL at {plain(leave.partial_percent)}%"
        day_number, percent = positions.percent(leave, when)
        share = percent / HUNDRED
        return share, ONE - share, f"TIERED day {day_number} of the rolling window at {plain(percent)}%"

    return pay


def _no_pay(leave: LeavePortion, when: dt.date) -> tuple[Decimal, Decimal, str | None]:
    return ONE, ZERO, None


def _is_paid_holiday(day: DayInput, cfg: LedgerConfig) -> bool:
    return day.holiday is not None and day.holiday.type in cfg.paid_holiday_types


def _rest_pay(cfg: LedgerConfig) -> Decimal:
    return ONE if cfg.employee_is_salaried else ZERO


def _leave_slot(slot: Slot, leave: LeavePortion, day: DayInput, cfg: LedgerConfig, pay: LeavePay) -> SlotResult | None:
    source = SourceRef("leave", leave.leave_id, leave.content_hash)
    if cfg.leave_enabled:
        paid, lop, detail = pay(leave, day.date)
        return SlotResult(
            slot, Classification.LEAVE, "3", canonical(paid), canonical(lop), source=source, detail=detail,
            leave_type_code=leave.leave_type_code, pay_treatment=leave.pay_treatment,
        )
    if not cfg.attendance_enabled:
        return None
    if cfg.when_disabled_treat_approved_leave_as is LeaveDisabledTreatment.PAID_EXCUSED:
        return SlotResult(
            slot, Classification.LEAVE_EXCUSED, "3b", ONE, ZERO, source=source, detail="Approved leave, paid and excused",
            leave_type_code=leave.leave_type_code,
        )
    return SlotResult(
        slot, Classification.ABSENT, "3b", ZERO, ONE, source=source, detail="Approved leave treated as absent",
        leave_type_code=leave.leave_type_code,
    )


@dataclass
class _Claims:
    slots: dict[Slot, SlotResult] = field(default_factory=dict)
    skipped_leaves: list[LeavePortion] = field(default_factory=list)
    ignored_lop: list[tuple[ManualLop, list[Slot], list[SlotResult]]] = field(default_factory=list)


def _claims(day: DayInput, cfg: LedgerConfig, pay: LeavePay) -> _Claims:
    """Rules 1-6 of 3.4 §4.2."""
    result = _Claims()
    slots = result.slots
    if not day.employed:
        source = SourceRef("assignment", day.assignment_id)
        for slot in SLOTS:
            slots[slot] = SlotResult(slot, Classification.NOT_EMPLOYED, "1", ZERO, ZERO, source=source)
    elif day.suspended_unpaid:
        source = SourceRef("suspension", day.suspension_id)
        for slot in SLOTS:
            slots[slot] = SlotResult(slot, Classification.SUSPENDED_UNPAID, "2", ZERO, ONE, source=source)
    else:
        paid_holiday = _is_paid_holiday(day, cfg)
        working = not (day.weekly_off or paid_holiday)
        for leave in day.leaves:
            if not (working or leave.counts_non_working_days):
                result.skipped_leaves.append(leave)
                continue
            for slot in leave.portion.slots:
                decided = _leave_slot(slot, leave, day, cfg, pay)
                if decided is not None:
                    slots[slot] = decided
        if paid_holiday:
            assert day.holiday is not None
            source = SourceRef("holiday", day.holiday.holiday_id, day.holiday.content_hash)
            for slot in SLOTS:
                if slot not in slots:
                    slots[slot] = SlotResult(
                        slot, Classification.HOLIDAY, "4", _rest_pay(cfg), ZERO, source=source,
                        detail=day.holiday.name or day.holiday.type.value,
                    )
        if day.weekly_off:
            source = SourceRef("work_week", day.work_week_id)
            for slot in SLOTS:
                if slot not in slots:
                    slots[slot] = SlotResult(slot, Classification.WEEKLY_OFF, "5", _rest_pay(cfg), ZERO, source=source)
    for lop in day.manual_lop:
        source = SourceRef("manual_lop", lop.source_id, lop.content_hash)
        taken = [slot for slot in lop.portion.slots if slot in slots]
        if taken:
            result.ignored_lop.append((lop, taken, [slots[slot] for slot in taken]))
        for slot in lop.portion.slots:
            if slot not in slots:
                slots[slot] = SlotResult(slot, Classification.MANUAL_LOP, "6", ZERO, ONE, source=source)
    return result


def _attendance_outcome(
    attendance: AttendanceInput | None, cfg: LedgerConfig
) -> tuple[Classification, SourceRef, str | None]:
    """The rule-7 outcome of the day; UNMARKED and MISSING_PUNCH_OUT come back only when held for review."""
    if attendance is None:
        policy = SourceRef("policy", "ATTENDANCE_PAY_POLICY.unmarked_day")
        if cfg.unmarked_day is UnmarkedDayPolicy.ABSENT:
            return Classification.ABSENT, policy, "Unmarked day treated as absent by policy"
        if cfg.unmarked_day is UnmarkedDayPolicy.PRESENT:
            return Classification.PRESENT, policy, "Unmarked day treated as present by policy"
        return Classification.UNMARKED, policy, "No attendance and no leave: awaiting review"
    source = SourceRef("attendance", attendance.session_id, attendance.content_hash)
    if attendance.status is AttendanceStatus.ABSENT:
        return Classification.ABSENT, source, None
    if attendance.closed_without_punch_out:
        return _missing_punch_out(cfg, source)
    if cfg.derive_status_from_hours and attendance.worked_seconds is not None:
        worked = Decimal(attendance.worked_seconds)
        if worked >= cfg.full_day_min_hours * SECONDS_PER_HOUR:
            return Classification.PRESENT, source, "Present from worked hours"
        if worked >= cfg.half_day_min_hours * SECONDS_PER_HOUR:
            return Classification.HALF_DAY, source, "Half day from worked hours"
        return Classification.ABSENT, source, "Absent from worked hours"
    if attendance.status is AttendanceStatus.HALF_DAY:
        return Classification.HALF_DAY, source, None
    if attendance.worked_seconds is None:
        return _missing_punch_out(cfg, source)
    return Classification.PRESENT, source, "Late" if attendance.status is AttendanceStatus.LATE else None


def _missing_punch_out(cfg: LedgerConfig, source: SourceRef) -> tuple[Classification, SourceRef, str | None]:
    """ATTENDANCE_PAY_POLICY.missing_punch_out for a session without an end, whatever status it carries (EC-ATT-02)."""
    policy = cfg.missing_punch_out
    if policy is MissingPunchOutPolicy.HALF_DAY:
        return Classification.HALF_DAY, source, "Missing punch-out treated as a half day by policy"
    if policy is MissingPunchOutPolicy.FULL_DAY:
        return Classification.PRESENT, source, "Missing punch-out treated as a full day by policy"
    if policy is MissingPunchOutPolicy.ABSENT:
        return Classification.ABSENT, source, "Missing punch-out treated as absent by policy"
    return Classification.MISSING_PUNCH_OUT, source, "Closed without a punch-out: awaiting review"


def _apply_outcome(
    decided: dict[Slot, SlotResult],
    slots: Sequence[Slot],
    outcome: Classification,
    source: SourceRef,
    detail: str | None,
    cfg: LedgerConfig,
) -> None:
    if outcome is Classification.PRESENT:
        for slot in slots:
            decided[slot] = SlotResult(slot, Classification.PRESENT, "7", ONE, ZERO, source=source, detail=detail)
    elif outcome is Classification.ABSENT:
        for slot in slots:
            decided[slot] = SlotResult(slot, Classification.ABSENT, "7", ZERO, ONE, source=source, detail=detail)
    else:
        covered, *uncovered = slots
        if not uncovered:
            # The other half is already leave, on-duty or manual LOP: the attended half is plain presence (EC-ATT-03).
            note = "Half day; the other half is decided by an earlier rule"
            decided[covered] = SlotResult(
                covered, Classification.HALF_DAY, "7", ONE, ZERO, source=source, detail=detail or note
            )
            return
        paid = ONE if cfg.half_day_value >= HALF else ZERO
        decided[covered] = SlotResult(covered, Classification.HALF_DAY, "7", paid, ONE - paid, source=source, detail=detail)
        rest = ONE if cfg.half_day_value == ONE else ZERO
        for slot in uncovered:
            decided[slot] = SlotResult(
                slot, Classification.PRESUMED_PRESENT, "8", rest, ONE - rest, source=source,
                detail="Uncovered half of a half day",
            )


def _rules_7_and_8(
    day: DayInput, cfg: LedgerConfig, free: Sequence[Slot], override: LedgerOverride | None
) -> tuple[dict[Slot, SlotResult], Classification | None, bool]:
    """Decide the free slots; returns them, the pending classification if held for review, and override use."""
    decided: dict[Slot, SlotResult] = {}
    if not cfg.attendance_enabled:
        source = SourceRef("policy", "payroll.attendance.enabled")
        for slot in free:
            decided[slot] = SlotResult(
                slot, Classification.PRESUMED_PRESENT, "8", ONE, ZERO, source=source, detail="Attendance is OFF"
            )
        return decided, None, False
    if override is not None:
        _apply_outcome(
            decided, free, _OVERRIDE_OUTCOME[override.action], SourceRef("override", override.source_id),
            f"Resolved by review: {override.action.value}", cfg,
        )
        return decided, None, True
    remaining = list(free)
    if cfg.count_on_duty_as_present:
        for duty in day.on_duty:
            source = SourceRef("on_duty", duty.source_id, duty.content_hash)
            for slot in duty.portion.slots:
                if slot in remaining:
                    decided[slot] = SlotResult(slot, Classification.PRESENT, "7", ONE, ZERO, source=source, detail="On duty")
                    remaining.remove(slot)
    if not remaining:
        return decided, None, False
    outcome, source, detail = _attendance_outcome(day.attendance, cfg)
    if outcome in (Classification.UNMARKED, Classification.MISSING_PUNCH_OUT):
        for slot in remaining:
            decided[slot] = SlotResult(slot, outcome, "7", ZERO, ZERO, pending=True, source=source, detail=detail)
        return decided, outcome, False
    _apply_outcome(decided, remaining, outcome, source, detail, cfg)
    return decided, None, False


def _input_refs(day: DayInput) -> tuple[SourceRef, ...]:
    refs: list[SourceRef] = []
    if day.assignment_id is not None:
        refs.append(SourceRef("assignment", day.assignment_id))
    if day.suspended_unpaid:
        refs.append(SourceRef("suspension", day.suspension_id))
    refs.extend(SourceRef("leave", leave.leave_id, leave.content_hash) for leave in day.leaves)
    if day.holiday is not None:
        refs.append(SourceRef("holiday", day.holiday.holiday_id, day.holiday.content_hash))
    if day.weekly_off:
        refs.append(SourceRef("work_week", day.work_week_id))
    refs.extend(SourceRef("manual_lop", lop.source_id, lop.content_hash) for lop in day.manual_lop)
    if day.attendance is not None:
        refs.append(SourceRef("attendance", day.attendance.session_id, day.attendance.content_hash))
    refs.extend(SourceRef("on_duty", duty.source_id, duty.content_hash) for duty in day.on_duty)
    return tuple(refs)


def _counts_mark(slot: SlotResult) -> bool:
    """A late or early mark counts only on a paid, present slot the session decided (no double deduction)."""
    return (
        slot.classification in (Classification.PRESENT, Classification.PRESUMED_PRESENT)
        and slot.paid > 0
        and (slot.source is None or slot.source.type != "on_duty")
    )


def _classify_day(
    day: DayInput,
    cfg: LedgerConfig,
    override: LedgerOverride | None,
    pay: LeavePay,
    issues: list[LedgerException],
) -> tuple[DayRecord, bool]:
    claims = _claims(day, cfg, pay)
    slots = dict(claims.slots)
    paid_holiday = _is_paid_holiday(day, cfg)
    scheduled = not (day.weekly_off or paid_holiday)
    if cfg.leave_enabled or cfg.attendance_enabled:
        kind = "holiday" if paid_holiday else "weekly off"
        for leave in claims.skipped_leaves:
            issues.append(LedgerException(
                ExceptionCode.LEAVE_ON_NON_WORKING_DAY, Severity.WARNING,
                f"Approved {leave.leave_type_code} leave falls on a {kind} and its leave type does not count "
                f"non-working days: the day stays a {kind} and should not use leave balance.",
                day.date, leave.portion.slots, (SourceRef("leave", leave.leave_id, leave.content_hash),), kind,
            ))
    for lop, taken, deciders in claims.ignored_lop:
        names = ", ".join(dict.fromkeys(d.classification.value for d in deciders))
        issues.append(LedgerException(
            ExceptionCode.MANUAL_LOP_IGNORED, Severity.WARNING,
            f"Manual LOP ignored: the slot is already {names}.",
            day.date, tuple(taken), (SourceRef("manual_lop", lop.source_id, lop.content_hash),), names,
        ))
    free = [slot for slot in SLOTS if slot not in slots]
    used_override = False
    if free:
        decided, pending, used_override = _rules_7_and_8(day, cfg, free, override)
        slots.update(decided)
        if pending is not None:
            code = ExceptionCode.UNMARKED_DAY if pending is Classification.UNMARKED else ExceptionCode.MISSING_PUNCH_OUT
            message = (
                "No attendance and no leave on a working day: mark the day present or absent."
                if pending is Classification.UNMARKED
                else "Session closed without a punch-out (hours are never invented): correct it or resolve the day."
            )
            pending_slots = tuple(slot for slot in SLOTS if slots[slot].pending)
            source = slots[pending_slots[0]].source
            issues.append(LedgerException(
                code, Severity.BLOCKING, message, day.date, pending_slots, (source,) if source is not None else ()
            ))
    am, pm = slots[Slot.AM], slots[Slot.PM]
    attendance = day.attendance
    worked = night = 0
    late = early = recorded = False
    if not day.employed:
        kinds = [name for name, present in (("attendance", attendance), ("leave", day.leaves), ("on-duty", day.on_duty))
                 if present]
        if kinds:
            issues.append(LedgerException(
                ExceptionCode.INPUT_OUTSIDE_EMPLOYMENT, Severity.WARNING,
                f"{', '.join(kinds).capitalize()} recorded on a date outside employment; it does not affect pay.",
                day.date, (), _input_refs(day),
            ))
    elif attendance is not None and cfg.attendance_enabled:
        source = SourceRef("attendance", attendance.session_id, attendance.content_hash)
        if attendance.status is AttendanceStatus.ABSENT:
            if attendance.worked_seconds or attendance.night_seconds:
                issues.append(LedgerException(
                    ExceptionCode.ABSENT_WITH_HOURS, Severity.WARNING,
                    "Attendance is marked absent but carries worked time; the time is ignored.",
                    day.date, (), (source,),
                ))
        else:
            if attendance.worked_seconds is not None:
                worked, night = attendance.worked_seconds, attendance.night_seconds
                recorded = True
            late = (attendance.status is AttendanceStatus.LATE or attendance.late) and _counts_mark(am)
            early = attendance.early_exit and _counts_mark(pm)
        if worked and am.rule in _LEAVE_RULES and pm.rule in _LEAVE_RULES:
            issues.append(LedgerException(
                ExceptionCode.WORKED_ON_LEAVE, Severity.WARNING,
                f"{plain(hours_of(worked))} hours worked on a day of approved full-day leave; "
                "pay follows the leave.",
                day.date, SLOTS, (source,),
            ))
    record = DayRecord(
        date=day.date,
        segment=day.segment,
        employed=day.employed,
        scheduled_working_day=scheduled,
        weekly_off=day.weekly_off,
        paid_holiday=paid_holiday,
        holiday=day.holiday,
        am=am,
        pm=pm,
        standard_hours=day.standard_hours if day.standard_hours is not None else cfg.standard_hours_per_day,
        worked_seconds=worked,
        night_seconds=night,
        hours_recorded=recorded,
        late_mark=late,
        early_exit=early,
        late_mark_regularised=late and attendance is not None and attendance.regularised,
        early_exit_regularised=early and attendance is not None and attendance.regularised,
        half_day=any(slot.classification is Classification.HALF_DAY for slot in (am, pm)),
        inputs=_input_refs(day),
    )
    return record, used_override


def _full_lop_day(record: DayRecord) -> bool:
    return (
        record.employed
        and record.scheduled_working_day
        and all(slot.lop == ONE and slot.pay_treatment is not PayTreatment.STATUTORY for slot in record.slots)
    )


def _apply_sandwich(records: list[DayRecord], rule: SandwichRule) -> list[DayRecord]:
    """3.4 §4.3: non-working days between two full LOP scheduled working days become SANDWICH_LOP."""
    if rule is SandwichRule.OFF:
        return records
    targets = _SANDWICH_TARGETS[rule]
    out = list(records)
    start = 0
    while start < len(out):
        if not out[start].employed or out[start].scheduled_working_day:
            start += 1
            continue
        end = start
        while end + 1 < len(out) and out[end + 1].employed and not out[end + 1].scheduled_working_day:
            end += 1
        before = out[start - 1] if start > 0 else None
        after = out[end + 1] if end + 1 < len(out) else None
        if before is not None and after is not None and _full_lop_day(before) and _full_lop_day(after):
            source = SourceRef("sandwich", f"{before.date.isoformat()}/{after.date.isoformat()}")
            for index in range(start, end + 1):
                record = out[index]
                am, pm = (
                    SlotResult(s.slot, Classification.SANDWICH_LOP, "S", ZERO, ONE, source=source,
                               detail=f"{s.classification.value} between two LOP days")
                    if s.classification in targets else s
                    for s in record.slots
                )
                out[index] = replace(record, am=am, pm=pm)
        start = end + 1
    return out


def _nearest_working_day(records: Sequence[DayRecord], index: int, step: int) -> DayRecord | None:
    cursor = index + step
    while 0 <= cursor < len(records):
        record = records[cursor]
        if not record.employed:
            return None
        if record.scheduled_working_day:
            return record
        cursor += step
    return None


def _apply_non_salaried_holidays(
    records: list[DayRecord], cfg: LedgerConfig, issues: list[LedgerException]
) -> list[DayRecord]:
    """Holiday pay for hourly and daily workers (3.4 §8): per policy and the adjacent-day rule (EC-HOL-03)."""
    if cfg.employee_is_salaried:
        return records
    rule = cfg.holiday_eligibility_adjacent_days
    out = list(records)
    for index, record in enumerate(records):
        if not record.employed or not any(s.classification is Classification.HOLIDAY for s in record.slots):
            continue
        eligible = not record.weekly_off and cfg.non_salaried_holiday_pay is not NonSalariedHolidayPay.OFF
        assumed: list[str] = []
        if eligible:
            for step, needed, label in ((-1, (AdjacentDays.DAY_BEFORE, AdjacentDays.BOTH), "before"),
                                        (1, (AdjacentDays.DAY_AFTER, AdjacentDays.BOTH), "after")):
                if rule not in needed:
                    continue
                neighbour = _nearest_working_day(records, index, step)
                if neighbour is None:
                    assumed.append(label)
                elif not any(s.classification in (Classification.PRESENT, Classification.HALF_DAY)
                             for s in neighbour.slots):
                    eligible = False
        if eligible and assumed:
            issues.append(LedgerException(
                ExceptionCode.HOLIDAY_ELIGIBILITY_ASSUMED, Severity.INFO,
                f"The working day {' and '.join(assumed)} the holiday is outside this ledger or employment; "
                "holiday pay eligibility is assumed.",
                record.date, SLOTS, record.inputs,
            ))
        am, pm = (
            replace(s, paid=ONE) if eligible and s.classification is Classification.HOLIDAY else s
            for s in record.slots
        )
        out[index] = replace(record, am=am, pm=pm, holiday_pay_eligible=eligible)
    return out


# --- totals -----------------------------------------------------------------------------------------------------


_PRESENCE = frozenset({Classification.PRESENT, Classification.HALF_DAY, Classification.PRESUMED_PRESENT})


@dataclass
class _Sums:
    days: int = 0
    employment: Decimal = ZERO
    working: Decimal = ZERO
    non_weekly_off: Decimal = ZERO
    standard_hours: Decimal = ZERO
    paid: Decimal = ZERO
    lop: Decimal = ZERO
    lop_working: Decimal = ZERO
    paid_working: Decimal = ZERO
    pending: Decimal = ZERO
    unpaid: Decimal = ZERO
    present: Decimal = ZERO
    absent: Decimal = ZERO
    half_days: Decimal = ZERO
    presumed: Decimal = ZERO
    leave_paid: Decimal = ZERO
    leave_unpaid: Decimal = ZERO
    leave_statutory: Decimal = ZERO
    leave_excused: Decimal = ZERO
    holidays: Decimal = ZERO
    weekly_offs: Decimal = ZERO
    holidays_on_weekly_off: Decimal = ZERO
    manual_lop: Decimal = ZERO
    suspended: Decimal = ZERO
    sandwich: Decimal = ZERO
    leave_hours: Decimal = ZERO
    holiday_hours: Decimal = ZERO
    presumed_hours: Decimal = ZERO
    worked_seconds: int = 0
    night_seconds: int = 0
    worked_hours: Decimal = ZERO
    night_hours: Decimal = ZERO
    late: int = 0
    early: int = 0
    late_regularised: int = 0
    early_regularised: int = 0
    by_type: dict[str, list[Decimal]] = field(default_factory=dict)


def _add_slot(sums: _Sums, record: DayRecord, slot: SlotResult, cfg: LedgerConfig) -> None:
    sums.paid += slot.paid
    sums.lop += slot.lop
    if slot.pending:
        sums.pending += ONE
    else:
        sums.unpaid += ONE - slot.paid - slot.lop
    if record.scheduled_working_day:
        sums.lop_working += slot.lop
        sums.paid_working += slot.paid
    half_hours = record.standard_hours / TWO
    kind = slot.classification
    if kind in _PRESENCE and not record.hours_recorded:
        sums.presumed_hours += half_hours * slot.paid
    if kind in (Classification.PRESENT, Classification.HALF_DAY):
        sums.present += ONE
    elif kind is Classification.ABSENT:
        sums.absent += ONE
    elif kind is Classification.PRESUMED_PRESENT:
        sums.presumed += slot.paid
    elif kind is Classification.HOLIDAY:
        sums.holidays += ONE
        if not record.weekly_off and (
            cfg.employee_is_salaried or cfg.non_salaried_holiday_pay is NonSalariedHolidayPay.STANDARD_HOURS
        ):
            sums.holiday_hours += half_hours * slot.paid
    elif kind is Classification.WEEKLY_OFF:
        sums.weekly_offs += ONE
    elif kind is Classification.MANUAL_LOP:
        sums.manual_lop += ONE
    elif kind is Classification.SUSPENDED_UNPAID:
        sums.suspended += ONE
    elif kind is Classification.SANDWICH_LOP:
        sums.sandwich += ONE
    if kind in (Classification.LEAVE, Classification.LEAVE_EXCUSED):
        assert slot.leave_type_code is not None
        entry = sums.by_type.setdefault(slot.leave_type_code, [ZERO, ZERO, ZERO, ZERO])
        sums.leave_hours += half_hours * slot.paid
        sums.leave_paid += slot.paid
        entry[0] += slot.paid
        if kind is Classification.LEAVE_EXCUSED:
            sums.leave_excused += ONE
            entry[3] += ONE
        elif slot.pay_treatment is PayTreatment.STATUTORY:
            sums.leave_statutory += ONE
            entry[2] += ONE
        else:
            sums.leave_unpaid += slot.lop
            entry[1] += slot.lop


def _sums(records: Iterable[DayRecord], cfg: LedgerConfig) -> _Sums:
    sums = _Sums()
    for record in records:
        sums.days += 1
        if not record.employed:
            continue
        sums.employment += ONE
        if record.scheduled_working_day:
            sums.working += ONE
        if not record.weekly_off:
            sums.non_weekly_off += ONE
            sums.standard_hours += record.standard_hours
        for slot in record.slots:
            _add_slot(sums, record, slot, cfg)
        if record.half_day:
            sums.half_days += ONE
        if record.holiday_on_weekly_off:
            sums.holidays_on_weekly_off += ONE
        sums.worked_seconds += record.worked_seconds
        sums.night_seconds += record.night_seconds
        sums.worked_hours += hours_of(record.worked_seconds)
        sums.night_hours += hours_of(record.night_seconds)
        sums.late += record.late_mark
        sums.early += record.early_exit
        sums.late_regularised += record.late_mark_regularised
        sums.early_regularised += record.early_exit_regularised
    return sums


def _totals(sums: _Sums, cfg: LedgerConfig, penalty: Decimal) -> LedgerTotals:
    def days(slot_units: Decimal) -> Decimal:
        return canonical(slot_units / TWO)

    worked_hours = sums.worked_hours
    on_weekly_off = canonical(sums.holidays_on_weekly_off)
    return LedgerTotals(
        days=sums.days,
        employment_days=canonical(sums.employment),
        scheduled_working_days=canonical(sums.working),
        non_weekly_off_days=canonical(sums.non_weekly_off),
        standard_hours=canonical(sums.standard_hours),
        paid_days=days(sums.paid),
        lop_days=canonical(days(sums.lop) + penalty),
        lop_days_on_working_days=canonical(days(sums.lop_working) + penalty),
        slot_lop_days=days(sums.lop),
        penalty_lop_days=canonical(penalty),
        paid_days_after_penalty=canonical(days(sums.paid) - penalty),
        paid_working_days=days(sums.paid_working),
        pending_days=days(sums.pending),
        unpaid_days=days(sums.unpaid),
        present_days=days(sums.present),
        absent_days=days(sums.absent),
        half_days=canonical(sums.half_days),
        presumed_present_days=days(sums.presumed),
        leave_days_paid=days(sums.leave_paid),
        leave_days_unpaid=days(sums.leave_unpaid),
        leave_days_statutory=days(sums.leave_statutory),
        leave_days_excused=days(sums.leave_excused),
        holidays=days(sums.holidays),
        weekly_offs=days(sums.weekly_offs),
        holidays_on_weekly_off=on_weekly_off,
        extra_day_pay_days=on_weekly_off if cfg.holiday_on_weekly_off is HolidayOnWeeklyOff.EXTRA_DAY_PAY else ZERO,
        substitute_days=on_weekly_off if cfg.holiday_on_weekly_off is HolidayOnWeeklyOff.SUBSTITUTE_DAY else ZERO,
        manual_lop_days=days(sums.manual_lop),
        suspended_days=days(sums.suspended),
        sandwich_lop_days=days(sums.sandwich),
        worked_hours=canonical(worked_hours),
        paid_leave_hours=canonical(sums.leave_hours),
        paid_holiday_hours=canonical(sums.holiday_hours),
        presumed_hours=canonical(sums.presumed_hours),
        payable_hours=canonical(worked_hours + sums.leave_hours + sums.holiday_hours + sums.presumed_hours),
        night_hours=canonical(sums.night_hours),
        worked_seconds=sums.worked_seconds,
        night_seconds=sums.night_seconds,
        late_marks=sums.late,
        early_exits=sums.early,
        regularised_late_marks=sums.late_regularised,
        regularised_early_exits=sums.early_regularised,
        leave_by_type=tuple(
            LeaveTypeDays(code, days(entry[0]), days(entry[1]), days(entry[2]), days(entry[3]))
            for code, entry in sorted(sums.by_type.items())
        ),
    )


def _zero_pay_guard(
    records: Sequence[DayRecord], totals: LedgerTotals, cfg: LedgerConfig
) -> tuple[ZeroPayGuard, LedgerException | None]:
    if totals.employment_days == 0:
        return ZeroPayGuard(False), None
    pending_on_working_days = any(
        slot.pending for record in records if record.employed and record.scheduled_working_day for slot in record.slots
    )
    if totals.scheduled_working_days == 0:
        if totals.non_weekly_off_days > 0:
            reason = ZeroPayReason.HOLIDAYS_COVER_ALL_WORKING_DAYS
            message = "No paid working days: the holiday calendar covers every working day. Check the calendar."
        else:
            reason = ZeroPayReason.NO_WORKING_DAYS_IN_EMPLOYMENT
            message = "No scheduled working day during employment: confirm before paying."
    elif totals.paid_working_days == 0 and not pending_on_working_days:
        reason = ZeroPayReason.NO_PAID_WORKING_DAYS
        message = "No paid working days: confirm before paying."
    else:
        return ZeroPayGuard(False), None
    pay_zero = cfg.zero_paid_days is ZeroPaidDaysPolicy.PAY_ZERO
    if pay_zero:
        message += " Policy PAY_ZERO: LOP-applicable components are zero; weekly offs and holidays are not paid."
    item = LedgerException(
        ExceptionCode.ZERO_PAID_WORKING_DAYS, Severity.INFO if pay_zero else Severity.BLOCKING, message, detail=reason.value
    )
    return ZeroPayGuard(True, reason, pay_zero), item


def _statutory_items(records: Sequence[DayRecord]) -> list[LedgerException]:
    found: dict[str, tuple[Decimal, dict[SourceRef, None]]] = {}
    for record in records:
        for slot in record.slots:
            if slot.classification is Classification.LEAVE and slot.pay_treatment is PayTreatment.STATUTORY:
                assert slot.leave_type_code is not None and slot.source is not None
                count, refs = found.get(slot.leave_type_code, (ZERO, {}))
                refs[slot.source] = None
                found[slot.leave_type_code] = (count + HALF, refs)
    return [
        LedgerException(
            ExceptionCode.STATUTORY_LEAVE, Severity.INFO,
            f"{plain(count)} days of statutory {code} leave: salary is not paid for them and statutory pay "
            "(with any company top-up) comes from the country pack.",
            sources=tuple(refs), detail=code,
        )
        for code, (count, refs) in sorted(found.items())
    ]


def _sort_key(item: LedgerException) -> tuple[Any, ...]:
    return (item.day is None, item.day or dt.date.min, item.code.value, tuple(slot.value for slot in item.slots))


def _assemble(
    records: Sequence[DayRecord], cfg: LedgerConfig, penalty: Decimal, issues: list[LedgerException]
) -> DayLedger:
    keys = list(dict.fromkeys(record.segment for record in records))
    sums = {key: _sums((r for r in records if r.segment == key), cfg) for key in keys}
    remaining = penalty
    applied: dict[str, Decimal] = {}
    for key in reversed(keys):
        take = min(remaining, canonical(sums[key].paid_working / TWO))
        applied[key] = take
        remaining -= take
    items = list(issues)
    if remaining > 0:
        items.append(LedgerException(
            ExceptionCode.PENALTY_LOP_CAPPED, Severity.WARNING,
            f"Penalty LOP of {plain(penalty)} days capped at {plain(penalty - remaining)}: penalty and ordinary LOP "
            "never exceed the paid working days.",
            detail=plain(penalty),
        ))
    segments = {key: _totals(sums[key], cfg, applied[key]) for key in keys}
    totals = _totals(_sums(records, cfg), cfg, penalty - remaining)
    guard, guard_item = _zero_pay_guard(records, totals, cfg)
    items.extend(_statutory_items(records))
    if guard_item is not None:
        items.append(guard_item)
    period = PeriodFacts(
        days=len(records),
        working_days=sum(1 for r in records if r.scheduled_working_day),
        non_weekly_off_days=sum(1 for r in records if not r.weekly_off),
        standard_hours=canonical(sum((r.standard_hours for r in records if not r.weekly_off), ZERO)),
        employed_all_period=all(r.employed for r in records),
    )
    return DayLedger(
        days=tuple(records),
        totals=totals,
        segments=MappingProxyType(segments),
        exceptions=tuple(sorted(items, key=_sort_key)),
        guard=guard,
        period=period,
        config=cfg,
        penalty_lop_days_requested=canonical(penalty),
    )


def _penalty(value: Decimal | int | str) -> Decimal:
    penalty = _number(value, "penalty_lop_days")
    if penalty < 0:
        raise LedgerInputError("INVALID_DAYS", "penalty_lop_days must not be negative")
    return penalty


def _ordered(days: Sequence[DayInput]) -> list[DayInput]:
    ordered = sorted(_items(days, DayInput, "days"), key=lambda day: day.date)
    if not ordered:
        raise LedgerInputError("EMPTY_LEDGER", "a ledger needs at least one date")
    for earlier, later in zip(ordered, ordered[1:]):
        if later.date == earlier.date:
            raise LedgerInputError("DUPLICATE_DATE", f"{later.date.isoformat()} appears twice")
        if later.date - earlier.date != dt.timedelta(days=1):
            raise LedgerInputError(
                "NON_CONTIGUOUS_DATES", f"dates jump from {earlier.date.isoformat()} to {later.date.isoformat()}"
            )
    closed: set[str] = set()
    for earlier, later in zip(ordered, ordered[1:]):
        if later.segment != earlier.segment:
            closed.add(earlier.segment)
            if later.segment in closed:
                raise LedgerInputError(
                    "NON_CONTIGUOUS_SEGMENT",
                    f"segment {later.segment!r} resumes on {later.date.isoformat()} after another segment",
                )
    return ordered


def _resolve_overrides(overrides: Mapping[dt.date, str | LedgerOverride] | None) -> dict[dt.date, LedgerOverride]:
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise LedgerInputError("INVALID_OVERRIDE", "overrides must map dates to override actions")
    resolved: dict[dt.date, LedgerOverride] = {}
    for when, value in overrides.items():
        resolved[_date(when, "override date")] = value if isinstance(value, LedgerOverride) else LedgerOverride(value)
    return resolved


def build_ledger(
    days: Sequence[DayInput],
    cfg: LedgerConfig,
    overrides: Mapping[dt.date, str | LedgerOverride] | None = None,
    *,
    penalty_lop_days: Decimal | int | str = ZERO,
) -> DayLedger:
    """Classify every slot of ``days`` (one contiguous range), then total it (3.4 §4).

    ``overrides`` resolves days held for review (``TREAT_AS_PRESENT``,
    ``TREAT_AS_ABSENT``, ``TREAT_AS_HALF_DAY``) wherever attendance decides a
    slot; unused overrides are reported. ``penalty_lop_days`` is the penalty
    LOP from late marks (§11), allocated to the last segments first and capped
    at the paid working days.
    """
    if not isinstance(cfg, LedgerConfig):
        raise LedgerConfigError("INVALID_CONFIG", "cfg must be a LedgerConfig")
    ordered = _ordered(days)
    pending_overrides = _resolve_overrides(overrides)
    penalty = _penalty(penalty_lop_days)
    issues: list[LedgerException] = []
    with localcontext(CALC_CONTEXT):
        pay = _leave_pay(_TieredPositions())
        records: list[DayRecord] = []
        for day in ordered:
            override = pending_overrides.pop(day.date, None)
            record, used = _classify_day(day, cfg, override, pay, issues)
            if override is not None and not used:
                issues.append(LedgerException(
                    ExceptionCode.OVERRIDE_NOT_APPLIED, Severity.WARNING,
                    f"Override {override.action.value} not applied: attendance decides no slot of this day.",
                    day.date, (), (SourceRef("override", override.source_id),), override.action.value,
                ))
            records.append(record)
        for when, override in sorted(pending_overrides.items()):
            issues.append(LedgerException(
                ExceptionCode.OVERRIDE_NOT_APPLIED, Severity.WARNING,
                f"Override {override.action.value} not applied: the date is outside this ledger.",
                when, (), (SourceRef("override", override.source_id),), override.action.value,
            ))
        records = _apply_sandwich(records, cfg.sandwich_rule)
        records = _apply_non_salaried_holidays(records, cfg, issues)
        return _assemble(records, cfg, penalty, issues)


def manual_lop_conflicts(day: DayInput, portion: Portion | str, cfg: LedgerConfig) -> tuple[LedgerException, ...]:
    """Why a new manual LOP input for ``portion`` of ``day`` must be rejected on entry (EC-ATT-16).

    A manual LOP only works on a slot that no earlier precedence rule
    (employment, suspension, leave, holiday, weekly off) or existing manual LOP
    already decides; the LOP-input service rejects the input when this returns
    anything.
    """
    if not isinstance(day, DayInput):
        raise LedgerInputError("INVALID_INPUT", "day must be a DayInput")
    if not isinstance(cfg, LedgerConfig):
        raise LedgerConfigError("INVALID_CONFIG", "cfg must be a LedgerConfig")
    wanted = _choice(Portion, portion, "portion", code="INVALID_PORTION")
    with localcontext(CALC_CONTEXT):
        claimed = _claims(day, cfg, _no_pay).slots
    grouped: dict[Classification, list[SlotResult]] = {}
    for slot in wanted.slots:
        if slot in claimed:
            grouped.setdefault(claimed[slot].classification, []).append(claimed[slot])
    return tuple(
        LedgerException(
            ExceptionCode.MANUAL_LOP_CONFLICT, Severity.BLOCKING,
            f"Manual LOP cannot be entered: the slot is already {kind.value}.",
            day.date, tuple(s.slot for s in decided), tuple(s.source for s in decided if s.source is not None),
            kind.value,
        )
        for kind, decided in grouped.items()
    )
