"""Bank business days: value dates, settlement windows and overdue counts.

A ``BusinessCalendar`` is a weekend (weekday numbers, Monday = 0, as ``countries.default_weekend``)
plus bank holidays; both come from the caller, so results never depend on the clock. Distances
count the business days moved over, ``distance(a, b) = #{business days d : a < d <= b}`` (negative
when b is before a), so Friday to Monday is 1 and Friday to Saturday is 0.

``payment_value_date`` rolls a requested value date that falls on a weekend or bank holiday to the
next business day (or the previous one), and warns when the result is after the pay date (EC-PAY-10).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.payroll.banking.errors import BankValidationError

DEFAULT_WEEKEND: frozenset[int] = frozenset({5, 6})
ROLL_CONVENTIONS: tuple[str, ...] = ("FOLLOWING", "PRECEDING")
_DAY = timedelta(days=1)


@dataclass(frozen=True)
class BusinessCalendar:
    weekend: frozenset[int] = DEFAULT_WEEKEND
    holidays: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        weekend = frozenset(self.weekend)
        if not weekend <= frozenset(range(7)) or len(weekend) == 7:
            raise BankValidationError("INVALID_CALENDAR", "the weekend must be weekday numbers 0-6, leaving at least one business day")
        if not all(isinstance(day, date) for day in self.holidays):
            raise BankValidationError("INVALID_CALENDAR", "holidays must be dates")
        object.__setattr__(self, "weekend", weekend)
        object.__setattr__(self, "holidays", frozenset(self.holidays))

    def is_business_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend and day not in self.holidays

    def roll(self, day: date, convention: str = "FOLLOWING") -> date:
        """The day itself when it is a business day, else the next (FOLLOWING) or previous (PRECEDING) one."""
        if convention not in ROLL_CONVENTIONS:
            raise BankValidationError("INVALID_ARGUMENT", f"convention must be one of {', '.join(ROLL_CONVENTIONS)}")
        step = _DAY if convention == "FOLLOWING" else -_DAY
        while not self.is_business_day(day):
            day += step
        return day

    def add(self, day: date, business_days: int) -> date:
        """Move by a number of business days (negative moves back); the start day itself is not counted."""
        step = _DAY if business_days >= 0 else -_DAY
        remaining = abs(business_days)
        while remaining:
            day += step
            if self.is_business_day(day):
                remaining -= 1
        return day

    def distance(self, start: date, end: date) -> int:
        """Signed number of business days in (start, end] (or -(end, start] when end is earlier)."""
        sign, low, high = (1, start, end) if end >= start else (-1, end, start)
        count, day = 0, low
        while day < high:
            day += _DAY
            if self.is_business_day(day):
                count += 1
        return sign * count


@dataclass(frozen=True)
class ValueDate:
    requested: date
    value_date: date
    shifted: bool
    warnings: tuple[str, ...]


def payment_value_date(
    requested: date, pay_date: date, calendar: BusinessCalendar, *, convention: str = "FOLLOWING"
) -> ValueDate:
    """Roll a requested value date off weekends and bank holidays (EC-PAY-10).

    Warnings: ``VALUE_DATE_SHIFTED`` whenever the date moved, ``VALUE_DATE_AFTER_PAY_DATE`` when the
    money will arrive after the pay date.
    """
    value = calendar.roll(requested, convention)
    warnings: list[str] = []
    if value != requested:
        warnings.append("VALUE_DATE_SHIFTED")
    if value > pay_date:
        warnings.append("VALUE_DATE_AFTER_PAY_DATE")
    return ValueDate(requested, value, value != requested, tuple(warnings))
