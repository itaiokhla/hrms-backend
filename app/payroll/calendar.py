"""Pay calendars: periods, pay dates, attendance cycles, cutoffs and tax years (3.5 §2, 3.1 §5.1).

A pay calendar turns a frequency and its rules into consecutive periods with no gap and no overlap:

    MONTHLY       monthly_start_day .. the day before the same day next month; a start day beyond
                  a month's length clamps to that month's last day (31 -> 28/29 Feb, 30 Apr)  EC-CAL-01
    SEMI_MONTHLY  1st .. semi_monthly_split_day, then the next day .. the month end
    BI_WEEKLY     14 days from biweekly_anchor_date (which must fall on week_start_day)
    WEEKLY        7 days starting on week_start_day
    DAILY         one day

Each period carries:

* its pay date: ``pay_day_rule`` gives a nominal date (LAST_DAY and LAST_WORKING_DAY of the period,
  DAY_OF_MONTH = day N of the month the period ends in, clamped, monthly calendars only, or
  DAYS_AFTER_PERIOD_END), then ``pay_day_shift`` moves a weekend or bank holiday of the banking
  calendar to the previous, next or nearest business day (a tie goes to the previous day, so pay is
  never late) or leaves it (EC-CAL-03);
* the attendance window that feeds time-based inputs: the period itself, the previous period, or a
  custom start-day cycle such as 26 Oct-25 Nov feeding November (the cycle ending on or before the
  period end). Fixed components are always paid for the period itself;
* the input cutoff and approval deadline (calendar days before the pay date) and the latest bank
  submission date (``payment_lead_business_days`` business days before the value date);
* its tax year, decided by the pay date unless the pack says period end (EC-CAL-05), and its number
  in that tax year. Weekly and bi-weekly calendars therefore have 52/53 or 26/27 periods in a tax
  year (one more when a holiday shift moves a pay date across the boundary), and annual amounts are
  divided by that actual count unless the pack fixes a divisor (EC-CAL-04).

Every date is a local calendar date in the legal entity's timezone; leap days are ordinary days
(EC-CAL-02). Converting instants is the caller's job and is done once, with ``local_date``;
datetimes are rejected everywhere else, so no server timezone or DST change can move a date
(EC-CAL-09, EC-CAL-10).

Changing frequency ends the old calendar on a chosen date: the old period that straddles it is
shortened into a closing transition period, the new calendar starts the next day (with an opening
transition period when that day is not one of its natural period starts), and attendance windows
are chained across the change, so no pay date or attendance date is left out or counted twice
(EC-CAL-07).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence, TypeVar

MAX_RANGE_DAYS = 3660
MAX_DAYS_AFTER_PERIOD_END = 90
MAX_SHIFT_SEARCH_DAYS = 31
MIN_YEAR, MAX_YEAR = 1900, 2200

_ONE_DAY = timedelta(days=1)
_SAFETY = timedelta(days=200)  # > forward pay-date lag (90 + 31) + longest period (31)
_CALC = Context(prec=28, rounding=ROUND_HALF_EVEN)  # the engine's context (money.CALC_CONTEXT)

ERROR_CODES = frozenset({
    "INVALID_FREQUENCY", "INVALID_RULES", "INVALID_WEEKDAY", "INVALID_MONTHLY_START_DAY", "INVALID_SPLIT_DAY",
    "BIWEEKLY_ANCHOR_REQUIRED", "BIWEEKLY_ANCHOR_WEEKDAY", "INVALID_PAY_DAY_RULE", "INVALID_PAY_DAY_VALUE",
    "PAY_DAY_VALUE_REQUIRED", "PAY_DAY_RULE_UNSUPPORTED", "INVALID_PAY_DAY_SHIFT", "INVALID_ATTENDANCE_MODE",
    "INVALID_ATTENDANCE_START_DAY", "ATTENDANCE_START_DAY_REQUIRED", "ATTENDANCE_CYCLE_UNSUPPORTED",
    "INVALID_CUTOFF", "CUTOFF_ORDER", "INVALID_TAX_YEAR_START", "INVALID_TAX_YEAR_BASIS", "INVALID_TAX_YEAR",
    "INVALID_BANKING_CALENDAR", "NO_BUSINESS_DAY", "NOT_A_LOCAL_DATE", "NAIVE_DATETIME", "INVALID_RANGE",
    "RANGE_TOO_LARGE", "INVALID_PERIOD", "INVALID_AMOUNT", "PERIOD_COUNT_REQUIRED", "INVALID_PERIOD_COUNT",
    "NO_PERIODS", "PERIODS_NOT_CONTIGUOUS", "ATTENDANCE_NOT_CONTIGUOUS", "CHANGE_BEFORE_CALENDAR_START",
    "CHANGE_LEAVES_GAP", "CHANGE_IN_LOCKED_PERIOD", "PAY_DATES_OUT_OF_ORDER", "INVALID_LATE_INPUT_POLICY",
})


class CalendarError(ValueError):
    """A calendar rule or request that cannot be honoured; ``code`` is stable (see ERROR_CODES)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Frequency(StrEnum):
    MONTHLY = "MONTHLY"
    SEMI_MONTHLY = "SEMI_MONTHLY"
    BI_WEEKLY = "BI_WEEKLY"
    WEEKLY = "WEEKLY"
    DAILY = "DAILY"


class Weekday(StrEnum):
    MON = "MON"
    TUE = "TUE"
    WED = "WED"
    THU = "THU"
    FRI = "FRI"
    SAT = "SAT"
    SUN = "SUN"

    @property
    def number(self) -> int:
        """Python weekday number (Mon=0 … Sun=6)."""
        return _WEEKDAY_NUMBERS[self]


_WEEKDAY_NUMBERS: Mapping[Weekday, int] = MappingProxyType({day: number for number, day in enumerate(Weekday)})


class PayDayRule(StrEnum):
    LAST_DAY = "LAST_DAY"
    LAST_WORKING_DAY = "LAST_WORKING_DAY"
    DAY_OF_MONTH = "DAY_OF_MONTH"
    DAYS_AFTER_PERIOD_END = "DAYS_AFTER_PERIOD_END"


class PayDayShift(StrEnum):
    PREVIOUS_BUSINESS_DAY = "PREVIOUS_BUSINESS_DAY"
    NEXT_BUSINESS_DAY = "NEXT_BUSINESS_DAY"
    NEAREST_BUSINESS_DAY = "NEAREST_BUSINESS_DAY"
    NO_SHIFT = "NO_SHIFT"


class AttendanceCycleMode(StrEnum):
    SAME_AS_PERIOD = "SAME_AS_PERIOD"
    PREVIOUS_PERIOD = "PREVIOUS_PERIOD"
    CUSTOM_START_DAY = "CUSTOM_START_DAY"


class TaxYearBasis(StrEnum):
    PAY_DATE = "PAY_DATE"
    PERIOD_END = "PERIOD_END"


class LateInputPolicy(StrEnum):
    NEXT_PERIOD = "NEXT_PERIOD"
    EXCEPTION = "EXCEPTION"


NOMINAL_PERIODS_PER_YEAR: Mapping[Frequency, int] = MappingProxyType({
    Frequency.MONTHLY: 12, Frequency.SEMI_MONTHLY: 24, Frequency.BI_WEEKLY: 26, Frequency.WEEKLY: 52,
    Frequency.DAILY: 365,
})

# Settings registry keys (3.1 §5.1) -> CalendarRules fields.
SETTING_FIELDS: Mapping[str, str] = MappingProxyType({
    "payroll.calendar.monthly_start_day": "monthly_start_day",
    "payroll.calendar.semi_monthly_split_day": "semi_monthly_split_day",
    "payroll.calendar.week_start_day": "week_start_day",
    "payroll.calendar.biweekly_anchor_date": "biweekly_anchor_date",
    "payroll.calendar.pay_day_rule": "pay_day_rule",
    "payroll.calendar.pay_day_value": "pay_day_value",
    "payroll.calendar.pay_day_shift": "pay_day_shift",
    "payroll.attendance_cycle.mode": "attendance_cycle_mode",
    "payroll.attendance_cycle.start_day": "attendance_cycle_start_day",
    "payroll.cutoff.input_days_before_pay_day": "input_cutoff_days_before_pay_day",
    "payroll.cutoff.approval_days_before_pay_day": "approval_days_before_pay_day",
    "payroll.cutoff.payment_lead_business_days": "payment_lead_business_days",
})
FREQUENCY_SETTING = "payroll.pay_frequency"

_E = TypeVar("_E", bound=StrEnum)


def _enum(kind: type[_E], value: Any, code: str) -> _E:
    try:
        return kind(value)
    except ValueError:
        raise CalendarError(code, f"{value!r} is not one of {', '.join(kind)}") from None


def _int(value: Any, name: str, low: int, high: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalendarError(code, f"{name} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise CalendarError(code, f"{name} must be between {low} and {high}, got {value}")
    return value


def _date(value: Any, name: str) -> date:
    """A local date: ``datetime`` (a ``date`` subclass) is refused, so no instant slips in."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise CalendarError("NOT_A_LOCAL_DATE",
                            f"{name} must be a local date (datetime.date), got {type(value).__name__}")
    if not 1 < value.year < 9999:
        raise CalendarError("INVALID_RANGE", f"{name} {value} is out of range")
    return value


def _supported(value: Any, name: str) -> date:
    day = _date(value, name)
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise CalendarError("INVALID_RANGE", f"{name} {day} is outside {MIN_YEAR}-{MAX_YEAR}")
    return day


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def _days_in_month(year: int, month: int) -> int:
    next_year, next_month = _add_months(year, month, 1)
    return (date(next_year, next_month, 1) - date(year, month, 1)).days


def _clamped(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, _days_in_month(year, month)))


def _month_end(day: date) -> date:
    return date(day.year, day.month, _days_in_month(day.year, day.month))


# --------------------------------------------------------------------------- banking calendar


@dataclass(frozen=True)
class BankingCalendar:
    """Business days for pay-date shifting and payment lead times: every day that is neither a
    weekend day (Python weekday numbers, Mon=0 … Sun=6, as ``countries.default_weekend``) nor a
    bank holiday of the resolved ``payroll.calendar.banking_calendar_id``."""

    weekend: frozenset[int]
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        try:
            weekend, holidays = frozenset(self.weekend), frozenset(self.holidays)
        except TypeError:
            raise CalendarError("INVALID_BANKING_CALENDAR", "weekend and holidays must be collections") from None
        for day in weekend:
            if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
                raise CalendarError("INVALID_BANKING_CALENDAR", f"weekend day {day!r} is not a weekday number 0-6")
        if len(weekend) == 7:
            raise CalendarError("INVALID_BANKING_CALENDAR", "a banking calendar needs at least one business weekday")
        for holiday in holidays:
            if isinstance(holiday, datetime) or not isinstance(holiday, date):
                raise CalendarError("INVALID_BANKING_CALENDAR", f"holiday {holiday!r} is not a local date")
        object.__setattr__(self, "weekend", weekend)
        object.__setattr__(self, "holidays", holidays)

    def is_business_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend and day not in self.holidays

    def _search(self, day: date, step: int) -> date:
        for offset in range(MAX_SHIFT_SEARCH_DAYS + 1):
            candidate = day + timedelta(days=step * offset)
            if self.is_business_day(candidate):
                return candidate
        raise CalendarError("NO_BUSINESS_DAY",
                            f"no business day within {MAX_SHIFT_SEARCH_DAYS} days of {day.isoformat()}")

    def on_or_before(self, day: date) -> date:
        return self._search(day, -1)

    def on_or_after(self, day: date) -> date:
        return self._search(day, 1)

    def previous_business_day(self, day: date) -> date:
        return self._search(day - _ONE_DAY, -1)

    def next_business_day(self, day: date) -> date:
        return self._search(day + _ONE_DAY, 1)

    def business_days_before(self, day: date, count: int) -> date:
        """The ``count``-th business day strictly before ``day``; with 0, the latest business day on
        or before it (a bank cannot take a value date that is not a business day)."""
        _int(count, "count", 0, 366, "INVALID_CUTOFF")
        if count == 0:
            return self.on_or_before(day)
        for _ in range(count):
            day = self.previous_business_day(day)
        return day

    def with_holidays(self, extra: Iterable[date]) -> BankingCalendar:
        return BankingCalendar(self.weekend, self.holidays | frozenset(extra))


def _shift(day: date, shift: PayDayShift, banking: BankingCalendar) -> date:
    if shift is PayDayShift.NO_SHIFT or banking.is_business_day(day):
        return day
    if shift is PayDayShift.PREVIOUS_BUSINESS_DAY:
        return banking.previous_business_day(day)
    if shift is PayDayShift.NEXT_BUSINESS_DAY:
        return banking.next_business_day(day)
    before, after = banking.previous_business_day(day), banking.next_business_day(day)
    return before if day - before <= after - day else after


def shift_to_business_day(day: date, shift: PayDayShift | str, banking: BankingCalendar) -> date:
    """Move a nominal pay date off a weekend or bank holiday per ``pay_day_shift`` (EC-CAL-03)."""
    return _shift(_date(day, "day"), _enum(PayDayShift, shift, "INVALID_PAY_DAY_SHIFT"), banking)


# --------------------------------------------------------------------------- tax years


def parse_tax_year_start(start: str) -> tuple[int, int]:
    """``"MM-DD"`` (for example ``"04-01"`` India, ``"04-06"`` UK, ``"01-01"`` US) -> (month, day)."""
    if not isinstance(start, str) or len(start) != 5 or start[2] != "-" or not (start[:2] + start[3:]).isdigit():
        raise CalendarError("INVALID_TAX_YEAR_START", f"tax year start must be 'MM-DD', got {start!r}")
    month, day = int(start[:2]), int(start[3:])
    if not 1 <= month <= 12 or not 1 <= day <= _days_in_month(2001, month):
        raise CalendarError("INVALID_TAX_YEAR_START", f"{start!r} is not a day that exists every year")
    return month, day


def tax_year_bounds(day: date, start: str) -> tuple[date, date]:
    """First and last date of the tax year that contains ``day``."""
    month, first = parse_tax_year_start(start)
    day = _date(day, "day")
    begin = date(day.year, month, first)
    if day < begin:
        begin = date(day.year - 1, month, first)
    return begin, date(begin.year + 1, month, first) - _ONE_DAY


def tax_year_label(day: date, start: str) -> str:
    """``"2026"`` for a calendar tax year, ``"2026-27"`` when it spans two calendar years."""
    begin, _ = tax_year_bounds(day, start)
    if (begin.month, begin.day) == (1, 1):
        return f"{begin.year}"
    return f"{begin.year}-{(begin.year + 1) % 100:02d}"


def tax_year_from_label(label: str, start: str) -> tuple[date, date]:
    """Bounds of the tax year named ``label`` (the inverse of ``tax_year_label``)."""
    month, first = parse_tax_year_start(start)
    if not isinstance(label, str) or len(label) < 4 or not label[:4].isdigit():
        raise CalendarError("INVALID_TAX_YEAR", f"{label!r} is not a tax year label")
    begin = _supported(date(int(label[:4]), month, first), "tax year")
    if tax_year_label(begin, start) != label:
        raise CalendarError("INVALID_TAX_YEAR", f"{label!r} is not a tax year starting {start}")
    return tax_year_bounds(begin, start)


# --------------------------------------------------------------------------- rules


@dataclass(frozen=True, kw_only=True)
class CalendarRules:
    """Resolved calendar settings of one pay group (3.1 §5.1) plus the facts they depend on.

    ``banking_calendar`` is the resolved ``payroll.calendar.banking_calendar_id``; ``tax_year_start``
    and ``tax_year_basis`` come from the country pack. Defaults are the registry's platform
    defaults. A value the frequency or rule does not use (the split day of a weekly calendar, the
    custom start day of a SAME_AS_PERIOD cycle) is range-checked and otherwise ignored.
    """

    banking_calendar: BankingCalendar
    tax_year_start: str
    tax_year_basis: TaxYearBasis = TaxYearBasis.PAY_DATE
    monthly_start_day: int = 1
    semi_monthly_split_day: int = 15
    week_start_day: Weekday = Weekday.MON
    biweekly_anchor_date: date | None = None
    pay_day_rule: PayDayRule = PayDayRule.LAST_WORKING_DAY
    pay_day_value: int | None = None
    pay_day_shift: PayDayShift = PayDayShift.PREVIOUS_BUSINESS_DAY
    attendance_cycle_mode: AttendanceCycleMode = AttendanceCycleMode.SAME_AS_PERIOD
    attendance_cycle_start_day: int | None = 26
    input_cutoff_days_before_pay_day: int = 5
    approval_days_before_pay_day: int = 2
    payment_lead_business_days: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.banking_calendar, BankingCalendar):
            raise CalendarError("INVALID_BANKING_CALENDAR", "banking_calendar must be a BankingCalendar")
        parse_tax_year_start(self.tax_year_start)
        for name, kind, code in (("tax_year_basis", TaxYearBasis, "INVALID_TAX_YEAR_BASIS"),
                                 ("week_start_day", Weekday, "INVALID_WEEKDAY"),
                                 ("pay_day_rule", PayDayRule, "INVALID_PAY_DAY_RULE"),
                                 ("pay_day_shift", PayDayShift, "INVALID_PAY_DAY_SHIFT"),
                                 ("attendance_cycle_mode", AttendanceCycleMode, "INVALID_ATTENDANCE_MODE")):
            object.__setattr__(self, name, _enum(kind, getattr(self, name), code))
        _int(self.monthly_start_day, "monthly_start_day", 1, 31, "INVALID_MONTHLY_START_DAY")
        _int(self.semi_monthly_split_day, "semi_monthly_split_day", 1, 27, "INVALID_SPLIT_DAY")
        if self.biweekly_anchor_date is not None:
            _supported(self.biweekly_anchor_date, "biweekly_anchor_date")
        if self.pay_day_value is not None:
            _int(self.pay_day_value, "pay_day_value", 0, MAX_DAYS_AFTER_PERIOD_END, "INVALID_PAY_DAY_VALUE")
        if self.attendance_cycle_start_day is not None:
            _int(self.attendance_cycle_start_day, "attendance_cycle_start_day", 1, 28, "INVALID_ATTENDANCE_START_DAY")
        _int(self.input_cutoff_days_before_pay_day, "input_cutoff_days_before_pay_day", 0, 31, "INVALID_CUTOFF")
        _int(self.approval_days_before_pay_day, "approval_days_before_pay_day", 0, 31, "INVALID_CUTOFF")
        _int(self.payment_lead_business_days, "payment_lead_business_days", 0, 10, "INVALID_CUTOFF")
        if not (self.input_cutoff_days_before_pay_day > self.approval_days_before_pay_day
                > self.payment_lead_business_days):
            raise CalendarError("CUTOFF_ORDER", "input cutoff > approval deadline > payment lead is required "
                                                "(days before the pay day, V-07)")

    @property
    def week_start_index(self) -> int:
        return Weekday(self.week_start_day).number

    @classmethod
    def from_settings(cls, values: Mapping[str, Any], *, banking_calendar: BankingCalendar, tax_year_start: str,
                      tax_year_basis: TaxYearBasis | str = TaxYearBasis.PAY_DATE) -> CalendarRules:
        """Rules from resolved setting values keyed by registry key. An absent key takes the platform
        default; a key present with ``None`` stays ``None`` (so a required value that resolved to
        nothing fails validation instead of picking up a default). ``payroll.calendar.biweekly_anchor_date``
        may be an ISO date string (JSON)."""
        fields: dict[str, Any] = {name: values[key] for key, name in SETTING_FIELDS.items() if key in values}
        anchor = fields.get("biweekly_anchor_date")
        if isinstance(anchor, str):
            try:
                fields["biweekly_anchor_date"] = date.fromisoformat(anchor)
            except ValueError:
                raise CalendarError("NOT_A_LOCAL_DATE", f"biweekly_anchor_date {anchor!r} is not an ISO date") from None
        return cls(banking_calendar=banking_calendar, tax_year_start=tax_year_start,
                   tax_year_basis=tax_year_basis, **fields)


def validate_rules(frequency: Frequency | str, rules: CalendarRules) -> Frequency:
    """Check the combinations that depend on the frequency; returns the frequency as an enum."""
    freq = _enum(Frequency, frequency, "INVALID_FREQUENCY")
    if not isinstance(rules, CalendarRules):
        raise CalendarError("INVALID_RULES", "rules must be CalendarRules")
    if freq is Frequency.BI_WEEKLY:
        anchor = rules.biweekly_anchor_date
        if anchor is None:
            raise CalendarError("BIWEEKLY_ANCHOR_REQUIRED", "a bi-weekly calendar needs biweekly_anchor_date")
        if anchor.weekday() != rules.week_start_index:
            raise CalendarError("BIWEEKLY_ANCHOR_WEEKDAY",
                                f"anchor {anchor.isoformat()} is not a {rules.week_start_day} (week_start_day)")
    if rules.pay_day_rule is PayDayRule.DAY_OF_MONTH:
        if freq is not Frequency.MONTHLY:
            raise CalendarError("PAY_DAY_RULE_UNSUPPORTED",
                                "DAY_OF_MONTH would give several periods one pay date; it is for monthly calendars")
        if rules.pay_day_value is None:
            raise CalendarError("PAY_DAY_VALUE_REQUIRED", "DAY_OF_MONTH needs pay_day_value 1-31")
        _int(rules.pay_day_value, "pay_day_value", 1, 31, "INVALID_PAY_DAY_VALUE")
    if rules.pay_day_rule is PayDayRule.DAYS_AFTER_PERIOD_END and rules.pay_day_value is None:
        raise CalendarError("PAY_DAY_VALUE_REQUIRED", "DAYS_AFTER_PERIOD_END needs pay_day_value")
    if rules.attendance_cycle_mode is AttendanceCycleMode.CUSTOM_START_DAY:
        if freq is not Frequency.MONTHLY:
            raise CalendarError("ATTENDANCE_CYCLE_UNSUPPORTED",
                                "a custom start-day attendance cycle is monthly; use SAME_AS_PERIOD or PREVIOUS_PERIOD")
        if rules.attendance_cycle_start_day is None:
            raise CalendarError("ATTENDANCE_START_DAY_REQUIRED", "CUSTOM_START_DAY needs attendance_cycle_start_day")
    return freq


# --------------------------------------------------------------------------- periods


@dataclass(frozen=True, kw_only=True)
class Period:
    """One pay period with every date runs, inputs and payments need (local dates, inclusive).

    ``attendance_end == attendance_start - 1`` is an empty attendance window; it only occurs right
    after a calendar change, when earlier periods already covered those dates. ``input_cutoff`` and
    ``approval_deadline`` are the last local dates on which inputs and approvals are on time;
    ``payment_submit_by`` is the latest business day to submit payments to the bank.
    """

    start: date
    end: date
    attendance_start: date
    attendance_end: date
    pay_date: date
    nominal_pay_date: date
    input_cutoff: date
    approval_deadline: date
    payment_submit_by: date
    tax_year: str
    number_in_tax_year: int
    is_last_in_tax_year: bool
    frequency: Frequency
    is_transition: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "frequency", _enum(Frequency, self.frequency, "INVALID_FREQUENCY"))
        for name in ("start", "end", "attendance_start", "attendance_end", "pay_date", "nominal_pay_date",
                     "input_cutoff", "approval_deadline", "payment_submit_by"):
            _date(getattr(self, name), name)
        if self.end < self.start:
            raise CalendarError("INVALID_PERIOD", f"period ends {self.end} before it starts {self.start}")
        if not self.attendance_start - _ONE_DAY <= self.attendance_end <= self.end:
            raise CalendarError("INVALID_PERIOD", "the attendance window must end on or before the period end")
        if isinstance(self.number_in_tax_year, bool) or not isinstance(self.number_in_tax_year, int) \
                or self.number_in_tax_year < 1:
            raise CalendarError("INVALID_PERIOD", "number_in_tax_year must be a positive integer")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def attendance_days(self) -> int:
        return (self.attendance_end - self.attendance_start).days + 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def in_attendance(self, day: date) -> bool:
        return self.attendance_start <= day <= self.attendance_end

    def attendance_dates(self) -> Iterator[date]:
        day = self.attendance_start
        while day <= self.attendance_end:
            yield day
            day += _ONE_DAY

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for snapshots and the API (ISO dates)."""
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days,
            "attendance_start": self.attendance_start.isoformat(), "attendance_end": self.attendance_end.isoformat(),
            "pay_date": self.pay_date.isoformat(), "nominal_pay_date": self.nominal_pay_date.isoformat(),
            "input_cutoff": self.input_cutoff.isoformat(), "approval_deadline": self.approval_deadline.isoformat(),
            "payment_submit_by": self.payment_submit_by.isoformat(), "tax_year": self.tax_year,
            "number_in_tax_year": self.number_in_tax_year, "is_last_in_tax_year": self.is_last_in_tax_year,
            "frequency": self.frequency.value, "is_transition": self.is_transition,
        }


@dataclass(frozen=True)
class _Slot:
    """A period before it is numbered within its tax year."""

    start: date
    end: date
    attendance_start: date
    attendance_end: date
    pay_date: date
    nominal_pay_date: date
    input_cutoff: date
    approval_deadline: date
    payment_submit_by: date
    tax_year: str
    frequency: Frequency
    is_transition: bool

    def period(self, number: int, is_last: bool) -> Period:
        return Period(start=self.start, end=self.end, attendance_start=self.attendance_start,
                      attendance_end=self.attendance_end, pay_date=self.pay_date,
                      nominal_pay_date=self.nominal_pay_date, input_cutoff=self.input_cutoff,
                      approval_deadline=self.approval_deadline, payment_submit_by=self.payment_submit_by,
                      tax_year=self.tax_year, number_in_tax_year=number, is_last_in_tax_year=is_last,
                      frequency=self.frequency, is_transition=self.is_transition)


def _bounds(freq: Frequency, rules: CalendarRules, day: date) -> tuple[date, date]:
    """The natural period of the calendar that contains ``day``."""
    if freq is Frequency.MONTHLY:
        start = _clamped(day.year, day.month, rules.monthly_start_day)
        if start > day:
            year, month = _add_months(day.year, day.month, -1)
            return _clamped(year, month, rules.monthly_start_day), start - _ONE_DAY
        year, month = _add_months(day.year, day.month, 1)
        return start, _clamped(year, month, rules.monthly_start_day) - _ONE_DAY
    if freq is Frequency.SEMI_MONTHLY:
        split = rules.semi_monthly_split_day
        if day.day <= split:
            return date(day.year, day.month, 1), date(day.year, day.month, split)
        return date(day.year, day.month, split + 1), _month_end(day)
    if freq is Frequency.BI_WEEKLY:
        anchor = rules.biweekly_anchor_date
        assert anchor is not None
        start = anchor + timedelta(days=(day - anchor).days // 14 * 14)
        return start, start + timedelta(days=13)
    if freq is Frequency.WEEKLY:
        start = day - timedelta(days=(day.weekday() - rules.week_start_index) % 7)
        return start, start + timedelta(days=6)
    return day, day


def _nominal(rules: CalendarRules, end: date) -> date:
    rule = rules.pay_day_rule
    if rule is PayDayRule.LAST_DAY:
        return end
    if rule is PayDayRule.LAST_WORKING_DAY:
        return rules.banking_calendar.on_or_before(end)
    assert rules.pay_day_value is not None
    if rule is PayDayRule.DAY_OF_MONTH:
        return _clamped(end.year, end.month, rules.pay_day_value)
    return end + timedelta(days=rules.pay_day_value)


def nominal_pay_date(frequency: Frequency | str, rules: CalendarRules, start: date, end: date) -> date:
    """The pay date that ``pay_day_rule`` gives for the period ``start``..``end``, before any shift."""
    validate_rules(frequency, rules)
    if _date(end, "end") < _date(start, "start"):
        raise CalendarError("INVALID_PERIOD", f"period ends {end} before it starts {start}")
    return _nominal(rules, end)


def pay_date(frequency: Frequency | str, rules: CalendarRules, start: date, end: date) -> date:
    """The pay date of the period ``start``..``end``: the nominal date, then the business-day shift."""
    return _shift(nominal_pay_date(frequency, rules, start, end), rules.pay_day_shift, rules.banking_calendar)


def _custom_cycle(end: date, start_day: int) -> tuple[date, date]:
    """The ``start_day`` .. ``start_day - 1`` cycle that ends on or before ``end``."""
    if start_day == 1:
        last = end if end == _month_end(end) else date(end.year, end.month, 1) - _ONE_DAY
        return date(last.year, last.month, 1), last
    close = start_day - 1
    if end.day >= close:
        cycle_end = date(end.year, end.month, close)
    else:
        year, month = _add_months(end.year, end.month, -1)
        cycle_end = date(year, month, close)
    year, month = _add_months(cycle_end.year, cycle_end.month, -1)
    return date(year, month, start_day), cycle_end


def _attendance(rules: CalendarRules, start: date, end: date, previous: tuple[date, date]) -> tuple[date, date]:
    mode = rules.attendance_cycle_mode
    if mode is AttendanceCycleMode.SAME_AS_PERIOD:
        return start, end
    if mode is AttendanceCycleMode.PREVIOUS_PERIOD:
        return previous
    assert rules.attendance_cycle_start_day is not None
    return _custom_cycle(end, rules.attendance_cycle_start_day)


def _slot(freq: Frequency, rules: CalendarRules, start: date, end: date, previous: tuple[date, date], *,
          transition: bool = False) -> _Slot:
    banking = rules.banking_calendar
    nominal = _nominal(rules, end)
    paid = _shift(nominal, rules.pay_day_shift, banking)
    attendance = _attendance(rules, start, end, previous)
    basis = paid if rules.tax_year_basis is TaxYearBasis.PAY_DATE else end
    return _Slot(start=start, end=end, attendance_start=attendance[0], attendance_end=attendance[1],
                 pay_date=paid, nominal_pay_date=nominal,
                 input_cutoff=paid - timedelta(days=rules.input_cutoff_days_before_pay_day),
                 approval_deadline=paid - timedelta(days=rules.approval_days_before_pay_day),
                 payment_submit_by=banking.business_days_before(paid, rules.payment_lead_business_days),
                 tax_year=tax_year_label(basis, rules.tax_year_start), frequency=freq, is_transition=transition)


def _numbering(slots: Sequence[_Slot], following: str, seed: Mapping[str, int] | None = None) -> list[tuple[int, bool]]:
    """(number in tax year, is last in tax year) for slots whose tax years never decrease."""
    counts = dict(seed or {})
    result: list[tuple[int, bool]] = []
    for index, slot in enumerate(slots):
        counts[slot.tax_year] = counts.get(slot.tax_year, 0) + 1
        next_label = slots[index + 1].tax_year if index + 1 < len(slots) else following
        result.append((counts[slot.tax_year], next_label != slot.tax_year))
    return result


def _check_range(from_date: date, until: date) -> tuple[date, date]:
    from_date, until = _supported(from_date, "from_date"), _supported(until, "until")
    if until < from_date:
        raise CalendarError("INVALID_RANGE", f"until {until} is before from_date {from_date}")
    if (until - from_date).days > MAX_RANGE_DAYS:
        raise CalendarError("RANGE_TOO_LARGE", f"at most {MAX_RANGE_DAYS} days per request")
    return from_date, until


def _generate(freq: Frequency, rules: CalendarRules, from_date: date, until: date) -> list[Period]:
    """Periods overlapping ``from_date``..``until``, numbered over whole tax years around them."""
    first = tax_year_bounds(from_date - _SAFETY, rules.tax_year_start)[0] - _SAFETY
    last = tax_year_bounds(until + _SAFETY, rules.tax_year_start)[1] + _SAFETY
    bounds = _bounds(freq, rules, first)
    previous = _bounds(freq, rules, bounds[0] - _ONE_DAY)
    slots = [_slot(freq, rules, bounds[0], bounds[1], previous)]
    while bounds[1] <= last:
        previous, bounds = bounds, _bounds(freq, rules, bounds[1] + _ONE_DAY)
        slots.append(_slot(freq, rules, bounds[0], bounds[1], previous))
    numbers = _numbering(slots[:-1], slots[-1].tax_year)
    return [slot.period(*number) for slot, number in zip(slots, numbers)
            if slot.end >= from_date and slot.start <= until]


def generate_periods(frequency: Frequency | str, rules: CalendarRules, from_date: date, until: date) -> list[Period]:
    """Every natural period of the calendar that overlaps ``from_date``..``until``, in order.

    Numbering within a tax year assumes the calendar ran unchanged through that tax year;
    ``plan_calendar_change`` renumbers across a change.
    """
    freq = validate_rules(frequency, rules)
    from_date, until = _check_range(from_date, until)
    return _generate(freq, rules, from_date, until)


@dataclass(frozen=True)
class PayCalendar:
    """A frequency with its rules: the generator of one pay group's periods."""

    frequency: Frequency
    rules: CalendarRules

    def __post_init__(self) -> None:
        object.__setattr__(self, "frequency", validate_rules(self.frequency, self.rules))

    def periods(self, from_date: date, until: date) -> list[Period]:
        return generate_periods(self.frequency, self.rules, from_date, until)

    def period_containing(self, day: date) -> Period:
        return self.periods(day, day)[0]

    @classmethod
    def from_settings(cls, values: Mapping[str, Any], *, banking_calendar: BankingCalendar, tax_year_start: str,
                      tax_year_basis: TaxYearBasis | str = TaxYearBasis.PAY_DATE) -> PayCalendar:
        rules = CalendarRules.from_settings(values, banking_calendar=banking_calendar,
                                            tax_year_start=tax_year_start, tax_year_basis=tax_year_basis)
        return cls(values.get(FREQUENCY_SETTING) or Frequency.MONTHLY, rules)


def periods_of_tax_year(frequency: Frequency | str, rules: CalendarRules, tax_year: str | date) -> list[Period]:
    """The periods whose tax year (by pay date, or period end per the pack) is ``tax_year``: a label
    such as ``"2026-27"`` or any date inside it."""
    freq = validate_rules(frequency, rules)
    if isinstance(tax_year, str):
        begin, end = tax_year_from_label(tax_year, rules.tax_year_start)
    else:
        begin, end = tax_year_bounds(_supported(tax_year, "tax_year"), rules.tax_year_start)
    label = tax_year_label(begin, rules.tax_year_start)
    return [p for p in _generate(freq, rules, begin - _SAFETY, end + _SAFETY) if p.tax_year == label]


def periods_in_tax_year(frequency: Frequency | str, rules: CalendarRules, tax_year: str | date) -> int:
    """Actual number of periods in the tax year: 12, 24, 365/366, 52/53 or 26/27 (EC-CAL-04)."""
    return len(periods_of_tax_year(frequency, rules, tax_year))


def _decimal(value: Any) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, int, str)):
        raise CalendarError("INVALID_AMOUNT", f"amount must be a Decimal, int or decimal string, got {value!r}")
    try:
        result = Decimal(value.strip()) if isinstance(value, str) else Decimal(value)
    except InvalidOperation:
        raise CalendarError("INVALID_AMOUNT", f"{value!r} is not a decimal number") from None
    if not result.is_finite():
        raise CalendarError("INVALID_AMOUNT", f"{value!r} is not a finite number")
    return result


def period_divisor(frequency: Frequency | str, periods_in_year: int | None = None, *,
                   fixed_divisor: int | None = None) -> int:
    """Divisor that turns an annual amount into one period's amount.

    A pack's fixed divisor wins. Monthly and semi-monthly calendars always use 12 and 24. Weekly,
    bi-weekly and daily calendars use the actual number of periods in the tax year (52/53, 26/27,
    365/366) from ``periods_in_tax_year`` (EC-CAL-04).
    """
    freq = _enum(Frequency, frequency, "INVALID_FREQUENCY")
    if fixed_divisor is not None:
        return _int(fixed_divisor, "fixed_divisor", 1, 366, "INVALID_PERIOD_COUNT")
    if freq in (Frequency.MONTHLY, Frequency.SEMI_MONTHLY):
        nominal = NOMINAL_PERIODS_PER_YEAR[freq]
        if periods_in_year is not None and periods_in_year != nominal:
            raise CalendarError("INVALID_PERIOD_COUNT", f"{freq} amounts are annual / {nominal}, "
                                                        f"not / {periods_in_year}")
        return nominal
    if periods_in_year is None:
        raise CalendarError("PERIOD_COUNT_REQUIRED", f"{freq} needs the actual number of periods in the tax year")
    return _int(periods_in_year, "periods_in_year", 1, 366, "INVALID_PERIOD_COUNT")


def annual_to_period(amount: Decimal | int | str, frequency: Frequency | str, periods_in_year: int | None = None, *,
                     fixed_divisor: int | None = None) -> Decimal:
    """One period's share of an annual amount, unrounded (rounding happens once, at the line stage)."""
    divisor = period_divisor(frequency, periods_in_year, fixed_divisor=fixed_divisor)
    return _CALC.divide(_decimal(amount), Decimal(divisor))


# --------------------------------------------------------------------------- contiguity and lookups


def check_contiguous(periods: Sequence[Period]) -> None:
    """Raise ``PERIODS_NOT_CONTIGUOUS`` unless each period starts the day after the previous one
    ends: no gap and no overlap (the rule the database guard enforces, EC-CAL-12)."""
    for earlier, later in zip(periods, periods[1:]):
        if later.start != earlier.end + _ONE_DAY:
            kind = "gap" if later.start > earlier.end + _ONE_DAY else "overlap"
            raise CalendarError("PERIODS_NOT_CONTIGUOUS",
                                f"{kind} between the period ending {earlier.end} and the period starting {later.start}")


def check_attendance_contiguous(periods: Sequence[Period]) -> None:
    """Raise ``ATTENDANCE_NOT_CONTIGUOUS`` unless every attendance date feeds exactly one period."""
    for earlier, later in zip(periods, periods[1:]):
        if later.attendance_start != earlier.attendance_end + _ONE_DAY:
            raise CalendarError("ATTENDANCE_NOT_CONTIGUOUS",
                                f"attendance ending {earlier.attendance_end} is followed by attendance "
                                f"starting {later.attendance_start}")


def period_for_date(periods: Sequence[Period], day: date) -> Period | None:
    """The period of an ordered, contiguous sequence that contains the local ``day``."""
    day = _date(day, "day")
    low, high = 0, len(periods)
    while low < high:
        middle = (low + high) // 2
        if periods[middle].end < day:
            low = middle + 1
        else:
            high = middle
    if low < len(periods) and periods[low].contains(day):
        return periods[low]
    return None


def local_date(instant: datetime, tz: tzinfo) -> date:
    """The local date of an aware instant in the legal entity's (or branch's) timezone."""
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise CalendarError("NAIVE_DATETIME", "instants must be timezone-aware datetimes")
    return instant.astimezone(tz).date()


def period_for_instant(periods: Sequence[Period], instant: datetime, tz: tzinfo) -> Period | None:
    return period_for_date(periods, local_date(instant, tz))


# --------------------------------------------------------------------------- warnings and re-checks


@dataclass(frozen=True)
class CalendarIssue:
    """A data-dependent calendar finding for review (returned, never raised)."""

    code: str
    severity: str
    message: str
    period_start: date | None = None
    rule: str | None = None


def pay_date_warnings(periods: Iterable[Period]) -> tuple[CalendarIssue, ...]:
    """V-14: periods paid before their attendance window closes (typically SAME_AS_PERIOD with a pay
    date before the period end). Later attendance is corrected next period through retro (EC-CAL-06)."""
    return tuple(
        CalendarIssue("PAY_DATE_BEFORE_ATTENDANCE_END", "WARNING",
                      f"Period {p.start}–{p.end} is paid on {p.pay_date}, before attendance closes on "
                      f"{p.attendance_end}. Attendance after the pay date will be corrected next period. "
                      "Consider a 26th–25th attendance cycle.", p.start, "V-14")
        for p in periods if p.attendance_end > p.pay_date
    )


@dataclass(frozen=True)
class PayDateCheck:
    """A stored pay date re-validated against the current banking calendar (EC-CAL-08)."""

    scheduled: date
    current: date
    payment_submit_by: date
    changed: bool
    issues: tuple[CalendarIssue, ...]


def recheck_pay_date(period: Period, rules: CalendarRules) -> PayDateCheck:
    """Re-validate a period's pay date when a payment batch is created: a bank holiday added since
    the period was generated moves the value date by the shift rule, and the move is reported."""
    banking = rules.banking_calendar
    last_working = rules.pay_day_rule is PayDayRule.LAST_WORKING_DAY
    nominal = banking.on_or_before(period.end) if last_working else period.nominal_pay_date
    current = _shift(nominal, rules.pay_day_shift, banking)
    issues: list[CalendarIssue] = []
    if current != period.pay_date:
        issues.append(CalendarIssue("PAY_DATE_SHIFTED", "WARNING",
                                    f"Pay date {period.pay_date} no longer fits the banking calendar; "
                                    f"the value date moves to {current}.", period.start))
    if current > period.pay_date:
        issues.append(CalendarIssue("VALUE_DATE_AFTER_PAY_DATE", "WARNING",
                                    f"The value date {current} is after the scheduled pay date {period.pay_date}.",
                                    period.start))
    if not banking.is_business_day(current):
        issues.append(CalendarIssue("PAY_DATE_NOT_BUSINESS_DAY", "WARNING",
                                    f"{current} is not a business day; the bank settles on "
                                    f"{banking.next_business_day(current)}.", period.start))
    return PayDateCheck(scheduled=period.pay_date, current=current,
                        payment_submit_by=banking.business_days_before(current, rules.payment_lead_business_days),
                        changed=current != period.pay_date, issues=tuple(issues))


@dataclass(frozen=True)
class InputRouting:
    """Where an input lands: its target period when on time; when late, the next period still open
    for inputs (``NEXT_PERIOD``) or the target period with a review exception (``EXCEPTION``)."""

    period: Period
    late: bool
    exception: str | None = None


def route_input(periods: Sequence[Period], target: Period, submitted_on: date, *,
                late_policy: LateInputPolicy | str) -> InputRouting:
    """Apply the input cutoff of ``target`` to an input submitted on the local date ``submitted_on``
    (EC-CAL-13). ``late_policy`` comes from the input type's setting."""
    policy = _enum(LateInputPolicy, late_policy, "INVALID_LATE_INPUT_POLICY")
    submitted_on = _date(submitted_on, "submitted_on")
    if submitted_on <= target.input_cutoff:
        return InputRouting(target, late=False)
    if policy is LateInputPolicy.EXCEPTION:
        return InputRouting(target, late=True, exception="LATE_INPUT")
    for candidate in periods:
        if candidate.start > target.end and submitted_on <= candidate.input_cutoff:
            return InputRouting(candidate, late=True)
    return InputRouting(target, late=True, exception="LATE_INPUT_NO_OPEN_PERIOD")


# --------------------------------------------------------------------------- frequency change


@dataclass(frozen=True)
class CalendarChangeCheck:
    """What a calendar change starting on ``new_start`` does to the stored periods."""

    new_start: date
    kept: tuple[Period, ...]
    truncated: Period | None
    dropped: tuple[Period, ...]


def validate_calendar_change(old_periods: Sequence[Period], new_start: date, *,
                             locked_until: date | None = None) -> CalendarChangeCheck:
    """Validate ending the stored calendar on ``new_start - 1`` (EC-CAL-07).

    ``old_periods`` are the stored periods in order; ``locked_until`` is the last date covered by a
    PROCESSING or CLOSED period, which can never be shortened or removed. The old period straddling
    the change is ``truncated`` (it becomes the closing transition period); it and every later
    period are ``dropped``.
    """
    if not old_periods:
        raise CalendarError("NO_PERIODS", "there is no calendar to change")
    old = list(old_periods)
    check_contiguous(old)
    check_attendance_contiguous(old)
    new_start = _supported(new_start, "new_start")
    if new_start < old[0].start:
        raise CalendarError("CHANGE_BEFORE_CALENDAR_START",
                            f"{new_start} is before the first stored period ({old[0].start})")
    if new_start > old[-1].end + _ONE_DAY:
        raise CalendarError("CHANGE_LEAVES_GAP", f"{new_start} would leave {old[-1].end + _ONE_DAY}–"
                                                 f"{new_start - _ONE_DAY} without a period; generate the old "
                                                 "calendar further first")
    truncated = next((p for p in old if p.start < new_start <= p.end), None)
    if locked_until is not None:
        first_changed = truncated.start if truncated is not None else new_start
        if first_changed <= _date(locked_until, "locked_until"):
            raise CalendarError("CHANGE_IN_LOCKED_PERIOD",
                                f"periods up to {locked_until} have runs and cannot change; start the new "
                                f"calendar after {locked_until}")
    return CalendarChangeCheck(new_start=new_start, kept=tuple(p for p in old if p.end < new_start),
                               truncated=truncated, dropped=tuple(p for p in old if p.end >= new_start))


@dataclass(frozen=True)
class CalendarChange:
    """The calendar after a change: ``periods`` = kept + closing transition + opening transition +
    new regular periods, contiguous in dates and attendance, numbered across the change."""

    kept: tuple[Period, ...]
    dropped: tuple[Period, ...]
    closing_transition: Period | None
    opening_transition: Period | None
    new_periods: tuple[Period, ...]

    @property
    def periods(self) -> tuple[Period, ...]:
        middle = tuple(p for p in (self.closing_transition, self.opening_transition) if p is not None)
        return self.kept + middle + self.new_periods


def plan_calendar_change(old: PayCalendar, old_periods: Sequence[Period], new: PayCalendar, new_start: date,
                         until: date, *, locked_until: date | None = None) -> CalendarChange:
    """End ``old`` (the calendar that produced ``old_periods``) on ``new_start - 1`` and generate
    ``new`` from ``new_start`` through the period containing ``until``.

    Attendance windows are chained: each starts the day after the previous one ends and ends where
    its own rule says (never before the previous end), so switching attendance modes neither skips
    nor repeats a date; one window may come out longer, or empty. Pay dates must not go backwards
    across the change, so tax-year numbering stays well defined. The last kept period keeps its
    dates; only its ``is_last_in_tax_year`` is recomputed against the new successor.
    """
    if not isinstance(old, PayCalendar) or not isinstance(new, PayCalendar):
        raise CalendarError("INVALID_RULES", "old and new must be PayCalendar")
    check = validate_calendar_change(old_periods, new_start, locked_until=locked_until)
    new_start, until = _check_range(new_start, until)
    first_old = old_periods[0]
    kept = list(check.kept)
    if kept:
        previous: tuple[date, date] = (kept[-1].start, kept[-1].end)
        chain_end = kept[-1].attendance_end
    else:
        previous = _bounds(old.frequency, old.rules, first_old.start - _ONE_DAY)
        chain_end = first_old.attendance_start - _ONE_DAY

    slots: list[_Slot] = []
    if check.truncated is not None:
        slots.append(_slot(old.frequency, old.rules, check.truncated.start, new_start - _ONE_DAY, previous,
                           transition=True))
        previous = (check.truncated.start, new_start - _ONE_DAY)
    natural = _bounds(new.frequency, new.rules, new_start)
    opening = natural[0] < new_start
    if opening:
        slots.append(_slot(new.frequency, new.rules, new_start, natural[1], previous, transition=True))
        previous = (new_start, natural[1])
    cursor = natural[1] + _ONE_DAY if opening else new_start
    while not slots or slots[-1].end < until:
        bounds = _bounds(new.frequency, new.rules, cursor)
        slots.append(_slot(new.frequency, new.rules, bounds[0], bounds[1], previous))
        previous, cursor = bounds, bounds[1] + _ONE_DAY
    lookahead = _slot(new.frequency, new.rules, *_bounds(new.frequency, new.rules, cursor), previous)

    chained: list[_Slot] = []
    for slot in slots:
        start, chain_end = chain_end + _ONE_DAY, max(slot.attendance_end, chain_end)
        chained.append(replace(slot, attendance_start=start, attendance_end=chain_end))

    pay_dates = [p.pay_date for p in kept[-1:]] + [s.pay_date for s in chained] + [lookahead.pay_date]
    for earlier, later in zip(pay_dates, pay_dates[1:]):
        if later < earlier:
            raise CalendarError("PAY_DATES_OUT_OF_ORDER", f"a later period would be paid on {later}, before an "
                                                          f"earlier one ({earlier}); adjust the new pay-day rule "
                                                          "or the change date")

    if kept:
        seed = {kept[-1].tax_year: kept[-1].number_in_tax_year}
        kept[-1] = replace(kept[-1], is_last_in_tax_year=chained[0].tax_year != kept[-1].tax_year)
    else:
        seed = {first_old.tax_year: first_old.number_in_tax_year - 1}
    periods = [slot.period(*number) for slot, number in zip(chained, _numbering(chained, lookahead.tax_year, seed))]

    closing = periods.pop(0) if check.truncated is not None else None
    opening_period = periods.pop(0) if opening else None
    change = CalendarChange(kept=tuple(kept), dropped=check.dropped, closing_transition=closing,
                            opening_transition=opening_period, new_periods=tuple(periods))
    check_contiguous(change.periods)
    check_attendance_contiguous(change.periods)
    return change
