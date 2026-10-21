"""Work-week patterns: which dates are weekly offs for an employee (3.4 §3).

A pattern is a set of weekdays that are always off (Monday = 0 ... Sunday = 6)
plus nth-weekday rules such as "2nd and 4th Saturday off". Occurrences count
within the calendar month (days 1-7 are the 1st occurrence, 8-14 the 2nd, ...)
and ``LAST`` (-1) is the last occurrence in the month, so a rule never depends
on how pay periods are cut.

A scheduled working day is a date that is neither a weekly off in the
employee's pattern nor a paid holiday on their branch calendar. Callers pass
the paid-holiday dates, already filtered by holiday type, because which types
are paid is a policy (``HOLIDAY_PAY_POLICY.paid_holiday_types``).

Patterns are canonical values: the same rules compare and hash equal whatever
order they were given in, and ``to_json``/``from_json`` round-trip the stored
JSON form exactly.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)
WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
LAST = -1
NTH_VALUES = frozenset({1, 2, 3, 4, 5, LAST})
_EVERY_OCCURRENCE = frozenset({1, 2, 3, 4, 5})
_ONE_DAY = dt.timedelta(days=1)
_ONE_WEEK = dt.timedelta(days=7)


class WorkWeekError(ValueError):
    """Invalid pattern, date or range; ``code`` is stable for API error mapping."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _weekday(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MONDAY <= value <= SUNDAY:
        raise WorkWeekError("INVALID_WEEKDAY", f"{what} must be an integer from 0 (Monday) to 6 (Sunday), got {value!r}")
    return value


def _occurrence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in NTH_VALUES:
        raise WorkWeekError("INVALID_OCCURRENCE", f"an occurrence must be 1-5 or -1 (last), got {value!r}")
    return value


def _require_date(value: object, what: str) -> dt.date:
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise WorkWeekError("INVALID_DATE", f"{what} must be a date, got {type(value).__name__}")
    return value


def _items(value: object, what: str) -> list[object]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise WorkWeekError("INVALID_PATTERN", f"{what} must be a collection")
    return list(value)


@dataclass(frozen=True)
class WorkWeekPattern:
    """Weekly offs: ``weekly_off_days`` every week, plus ``(weekday, occurrences)`` rules per month."""

    weekly_off_days: frozenset[int] = frozenset()
    nth_weekday_offs: tuple[tuple[int, frozenset[int]], ...] = ()

    def __post_init__(self) -> None:
        off = frozenset(_weekday(day, "a weekly off day") for day in _items(self.weekly_off_days, "weekly_off_days"))
        rules: dict[int, frozenset[int]] = {}
        for rule in _items(self.nth_weekday_offs, "nth_weekday_offs"):
            parts = _items(rule, "an nth-weekday rule")
            if len(parts) != 2:
                raise WorkWeekError("INVALID_PATTERN", f"an nth-weekday rule is (weekday, occurrences), got {rule!r}")
            weekday = _weekday(parts[0], "an nth-weekday rule's weekday")
            occurrences = frozenset(_occurrence(n) for n in _items(parts[1], "occurrences"))
            if not occurrences:
                raise WorkWeekError("EMPTY_RULE", f"the rule for {WEEKDAY_CODES[weekday]} lists no occurrence")
            if weekday in off:
                raise WorkWeekError("REDUNDANT_RULE", f"{WEEKDAY_CODES[weekday]} is already off every week")
            if weekday in rules:
                raise WorkWeekError("DUPLICATE_RULE", f"{WEEKDAY_CODES[weekday]} has more than one nth-weekday rule")
            if _EVERY_OCCURRENCE <= occurrences:
                raise WorkWeekError(
                    "EVERY_OCCURRENCE", f"every {WEEKDAY_CODES[weekday]} is off; list it in weekly_off_days instead"
                )
            rules[weekday] = occurrences
        if len(off) == 7:
            raise WorkWeekError("NO_WORKING_DAYS", "a work week needs at least one working weekday")
        object.__setattr__(self, "weekly_off_days", off)
        object.__setattr__(self, "nth_weekday_offs", tuple(sorted(rules.items())))

    def to_json(self) -> dict[str, Any]:
        return {
            "weekly_off_days": sorted(self.weekly_off_days),
            "nth_weekday_offs": [{"weekday": day, "nth": sorted(nths)} for day, nths in self.nth_weekday_offs],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> WorkWeekPattern:
        """Parse the stored JSON form (``work_week_patterns.pattern``); unknown keys are refused."""
        if not isinstance(data, Mapping):
            raise WorkWeekError("INVALID_PATTERN", "a pattern must be a JSON object")
        unknown = set(data) - {"weekly_off_days", "nth_weekday_offs"}
        if unknown:
            raise WorkWeekError("INVALID_PATTERN", f"unknown pattern keys: {', '.join(sorted(map(str, unknown)))}")
        off = data.get("weekly_off_days", [])
        raw_rules = data.get("nth_weekday_offs", [])
        if not isinstance(off, list) or not isinstance(raw_rules, list):
            raise WorkWeekError("INVALID_PATTERN", "weekly_off_days and nth_weekday_offs must be lists")
        rules: list[tuple[int, frozenset[int]]] = []
        for item in raw_rules:
            if not isinstance(item, Mapping) or set(item) != {"weekday", "nth"} or not isinstance(item["nth"], list):
                raise WorkWeekError("INVALID_PATTERN", 'each nth-weekday rule is {"weekday": int, "nth": [int, ...]}')
            nths = [_occurrence(n) for n in item["nth"]]
            if len(set(nths)) != len(nths):
                raise WorkWeekError("INVALID_PATTERN", "an nth-weekday rule repeats an occurrence")
            rules.append((_weekday(item["weekday"], "an nth-weekday rule's weekday"), frozenset(nths)))
        days = [_weekday(day, "a weekly off day") for day in off]
        if len(set(days)) != len(days):
            raise WorkWeekError("INVALID_PATTERN", "weekly_off_days repeats a weekday")
        return cls(frozenset(days), tuple(rules))


MON_FRI = WorkWeekPattern(frozenset({SATURDAY, SUNDAY}))
MON_SAT = WorkWeekPattern(frozenset({SUNDAY}))
SUN_THU = WorkWeekPattern(frozenset({FRIDAY, SATURDAY}))
SAT_THU = WorkWeekPattern(frozenset({FRIDAY}))
MON_SAT_2ND_4TH_SAT_OFF = WorkWeekPattern(frozenset({SUNDAY}), ((SATURDAY, frozenset({2, 4})),))

PRESETS: Mapping[str, WorkWeekPattern] = MappingProxyType(
    {
        "MON_FRI": MON_FRI,
        "MON_SAT": MON_SAT,
        "SUN_THU": SUN_THU,
        "SAT_THU": SAT_THU,
        "MON_SAT_2ND_4TH_SAT_OFF": MON_SAT_2ND_4TH_SAT_OFF,
    }
)


def occurrence_in_month(day: dt.date) -> int:
    """1 for the first such weekday of the month (days 1-7), 2 for days 8-14, ... up to 5."""
    return (_require_date(day, "day").day - 1) // 7 + 1


def is_last_occurrence_in_month(day: dt.date) -> bool:
    day = _require_date(day, "day")
    return (day + _ONE_WEEK).month != day.month


def is_weekly_off(pattern: WorkWeekPattern, day: dt.date) -> bool:
    weekday = _require_date(day, "day").weekday()
    if weekday in pattern.weekly_off_days:
        return True
    for rule_weekday, occurrences in pattern.nth_weekday_offs:
        if rule_weekday == weekday:
            return occurrence_in_month(day) in occurrences or (
                LAST in occurrences and is_last_occurrence_in_month(day)
            )
    return False


def dates_between(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    """Every date from ``start`` to ``end``, both included."""
    current = _require_date(start, "start")
    if _require_date(end, "end") < current:
        raise WorkWeekError("INVALID_RANGE", f"end {end.isoformat()} is before start {start.isoformat()}")
    while current <= end:
        yield current
        current += _ONE_DAY


def _holiday_set(holidays: Collection[dt.date]) -> frozenset[dt.date]:
    return frozenset(_require_date(day, "a holiday") for day in _items(holidays, "holidays"))


def is_scheduled_working_day(
    pattern: WorkWeekPattern, day: dt.date, holidays: Collection[dt.date] = frozenset()
) -> bool:
    """Neither a weekly off nor one of ``holidays`` (paid holiday dates on the branch calendar)."""
    return not is_weekly_off(pattern, day) and day not in _holiday_set(holidays)


def weekly_off_dates(pattern: WorkWeekPattern, start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
    return tuple(day for day in dates_between(start, end) if is_weekly_off(pattern, day))


def scheduled_working_dates(
    pattern: WorkWeekPattern, start: dt.date, end: dt.date, holidays: Collection[dt.date] = frozenset()
) -> tuple[dt.date, ...]:
    paid_holidays = _holiday_set(holidays)
    return tuple(
        day for day in dates_between(start, end) if not is_weekly_off(pattern, day) and day not in paid_holidays
    )


def scheduled_working_days(
    pattern: WorkWeekPattern, start: dt.date, end: dt.date, holidays: Collection[dt.date] = frozenset()
) -> int:
    """Number of scheduled working days from ``start`` to ``end`` inclusive."""
    return len(scheduled_working_dates(pattern, start, end, holidays))
