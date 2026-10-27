"""Holiday recurrence: rule validation, expansion, observed dates and year generation (04-holiday/02 §3-§7).

A holiday rule is one of a closed set of kinds (§3.3), expanded into dated rows per **rule-year**:

    FIXED_DATE        date(Y, month, day) + offset_days; 29 Feb in a common year follows missing_date
                      (SKIP -> no row, LAST_DAY_OF_MONTH -> 28 Feb, NEXT_DAY -> 1 Mar)       LH-REC-01
    NTH_WEEKDAY       nth weekday of the month (-1 = last) + offset_days; a missing 5th -> no row  LH-REC-02
    WEEKDAY_RELATIVE  the weekday on/after, after, on/before or before date(Y, month, day)
    EASTER_OFFSET     Easter Sunday (Western or Orthodox, §5) + offset_days                    LH-REC-16
    ANNUAL_CONFIRM    no computed date: duration_range[0] PROVISIONAL days from an expected start (§6.2)
    ONE_OFF           its date, only in its own year
    RRULE             the RFC 5545 subset of §7.1, through the ``rrule_*`` helpers

Rows of a multi-day rule are consecutive calendar days with ``sequence_index`` 1..n. Observed rules
(§3.5, §4.3.1) add an OBSERVED partner for a date on a trigger weekday (``applies_on``, default the
calendar's effective weekend), searching at most 14 days, in ascending date order, never onto a weekend
day or a date that already holds a holiday (G-01..G-07). ``params.observe_if_coincides`` also moves a
date held by an earlier holiday of equal or higher rank.

``generate_year`` builds calendar year Y from rule-years Y-1, Y and Y+1 and keeps the rows dated in Y,
so offsets and observed dates may cross 1 January in both directions (§4.2, §4.7); observed rows of
rule-year Y dated in another year that the stored rows lack come back as cross-year items.

Pure: no database, clock, settings or network. Every caller passes the calendar's effective weekend
(``holiday_calendar_service.effective_weekend``) and, where a rule needs it, the calendar-local today.
Weekdays are Python's (Monday = 0 ... Sunday = 6).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal

MIN_YEAR, MAX_YEAR = 2000, 2100
OBSERVED_HORIZON_DAYS = 14
MAX_ACTIVE_RULES = 200
MAX_PREVIEW_YEARS = 10
MAX_DURATION_DAYS = 31
MAX_NAME_LENGTH = 150

KINDS = ("FIXED_DATE", "NTH_WEEKDAY", "WEEKDAY_RELATIVE", "EASTER_OFFSET", "ANNUAL_CONFIRM", "ONE_OFF", "RRULE")
OBSERVED_RULES = ("NONE", "NEAREST_WORKDAY", "NEXT_WORKDAY", "PREVIOUS_WORKDAY")
PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF")
HOLIDAY_TYPES = ("PUBLIC", "COMPANY", "OPTIONAL")
HOLIDAY_RANK = {"PUBLIC": 0, "COMPANY": 1, "OPTIONAL": 2}
HOLIDAY_CATEGORIES = (
    "NATIONAL", "REGIONAL", "FESTIVAL", "ELECTION", "COMPANY_EVENT", "SHUTDOWN", "BRIDGE", "EMERGENCY", "BANK", "OTHER",
)
RULE_STATUSES = ("ACTIVE", "INACTIVE")
MISSING_DATES = ("SKIP", "LAST_DAY_OF_MONTH", "NEXT_DAY")
DIRECTIONS = ("ON_OR_AFTER", "AFTER", "ON_OR_BEFORE", "BEFORE")
TRADITIONS = ("WESTERN", "ORTHODOX")
NTH_VALUES = (1, 2, 3, 4, 5, -1)
EXPECTED_SOURCES = ("TENANT", "IMPORT", "CONNECTOR", "PACK")
_EXPECTED_PRIORITY = {"TENANT": 0, "IMPORT": 1, "CONNECTOR": 1, "PACK": 2}

ORIGINAL, OBSERVED = "ORIGINAL", "OBSERVED"
CONFIRMED, PROVISIONAL, CANCELLED = "CONFIRMED", "PROVISIONAL", "CANCELLED"

_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", -1: "last"}
_RRULE_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_COMMON_PARAMS = frozenset({"observe_if_coincides"})
_KIND_PARAMS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = MappingProxyType({
    "FIXED_DATE": (("month", "day"), ("offset_days", "missing_date")),
    "NTH_WEEKDAY": (("month", "weekday", "nth"), ("offset_days",)),
    "WEEKDAY_RELATIVE": (("month", "day", "weekday", "direction"), ()),
    "EASTER_OFFSET": (("offset_days", "tradition"), ()),
    "ANNUAL_CONFIRM": ((), ("expected_month", "hint")),
    "ONE_OFF": (("date",), ()),
    "RRULE": (("rrule", "dtstart"), ()),
})
_OFFSET_LIMIT = {"FIXED_DATE": 31, "NTH_WEEKDAY": 31, "EASTER_OFFSET": 70}

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Issue:
    """A validation failure or a generation note; ``code`` is a catalogue code (11 §10)."""

    code: str
    message: str
    severity: Severity = "error"
    field: str | None = None
    reason: str | None = None
    rule_id: int | None = None
    year: int | None = None


class RecurrenceError(ValueError):
    """Input the engine cannot expand; ``code`` is stable for API error mapping."""

    def __init__(self, code: str, message: str, issues: Sequence[Issue] = ()) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.issues = tuple(issues)


@dataclass(frozen=True)
class RuleSpec:
    """One ``holiday_rules`` row (§3.2) as plain data; ``params`` is the kind-specific JSON of §3.4."""

    kind: str
    params: Mapping[str, Any]
    name: str
    id: int | None = None
    holiday_type: str = "COMPANY"
    category: str = "OTHER"
    portion: str = "FULL"
    duration_days: int = 1
    duration_range: tuple[int, int] | None = None
    observed_rule: str = "NONE"
    applies_on: frozenset[int] | None = None
    valid_from_year: int = MIN_YEAR
    valid_to_year: int | None = None
    status: str = "ACTIVE"
    version: int = 1
    name_i18n: Mapping[str, str] = dataclasses.field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if isinstance(self.params, Mapping):
            object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        if isinstance(self.name_i18n, Mapping):
            object.__setattr__(self, "name_i18n", MappingProxyType(dict(self.name_i18n)))
        if self.applies_on is not None and _is_collection(self.applies_on):
            object.__setattr__(self, "applies_on", frozenset(self.applies_on))
        if self.duration_range is not None and _is_collection(self.duration_range):
            object.__setattr__(self, "duration_range", tuple(self.duration_range))

    def valid_in(self, year: int) -> bool:
        """Rule-year ``year`` lies in ``valid_from_year .. COALESCE(valid_to_year, 2100)`` (§4.2)."""
        upper = MAX_YEAR if self.valid_to_year is None else self.valid_to_year
        return self.valid_from_year <= year <= upper


@dataclass(frozen=True)
class Occurrence:
    """One dated holiday row (§4.1): generated by a rule, or a fixed row the generator must respect."""

    date: dt.date
    name: str
    holiday_type: str = "COMPANY"
    category: str = "OTHER"
    portion: str = "FULL"
    status: str = CONFIRMED
    observance: str = ORIGINAL
    observed_from_date: dt.date | None = None
    sequence_index: int = 1
    rule_id: int | None = None
    rule_year: int | None = None
    rule_version: int | None = None
    occurrence_key: str | None = None
    name_i18n: Mapping[str, str] = dataclasses.field(default_factory=lambda: MappingProxyType({}))
    rule: RuleSpec | None = dataclasses.field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class GenerationResult:
    """Rows dated in the target year, observed rows of its rule-year that belong elsewhere, and notes."""

    rows: tuple[Occurrence, ...]
    cross_year_items: tuple[Occurrence, ...]
    notes: tuple[Issue, ...]


@dataclass(frozen=True)
class RRuleParts:
    """A parsed RRULE inside the accepted subset (§7.1)."""

    bymonth: int | None = None
    bymonthday: tuple[int, ...] = ()
    weekday: int | None = None
    ordinal: int | None = None
    bysetpos: int | None = None
    count: int | None = None
    until: dt.date | None = None
    byeaster: int | None = None


@dataclass(frozen=True)
class NativeRule:
    """The native kind an RRULE maps to (§7.2), with the validity years its COUNT/UNTIL imply."""

    kind: str
    params: Mapping[str, Any]
    valid_from_year: int
    valid_to_year: int | None


# --------------------------------------------------------------------------- small helpers


def _is_collection(value: object) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_date(value: object) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and len(value) == 10:
        return dt.date.fromisoformat(value)
    raise ValueError(f"not a YYYY-MM-DD date: {value!r}")


def _weekend(weekend_days: Collection[int]) -> frozenset[int]:
    if not _is_collection(weekend_days):
        raise RecurrenceError("INVALID_WEEKEND", "weekend_days must be a collection of weekdays 0-6")
    days = frozenset(weekend_days)
    if any(not _is_int(d) or not 0 <= d <= 6 for d in days) or len(days) == 7:
        raise RecurrenceError("INVALID_WEEKEND", f"weekend_days must be weekdays 0-6 leaving a workday, got {sorted(days)}")
    return days


def _check_year(year: int) -> None:
    if not _is_int(year) or not MIN_YEAR <= year <= MAX_YEAR:
        raise RecurrenceError("RULE_INVALID", f"year must be {MIN_YEAR}-{MAX_YEAR}, got {year!r}")


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> dt.date | None:
    if nth == -1:
        last = _month_end(year, month)
        return last - dt.timedelta(days=(last.weekday() - weekday) % 7)
    first = dt.date(year, month, 1)
    day = first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))
    return day if day.month == month else None


def _relative_weekday(anchor: dt.date, weekday: int, direction: str) -> dt.date:
    if direction == "ON_OR_AFTER":
        return anchor + dt.timedelta(days=(weekday - anchor.weekday()) % 7)
    if direction == "AFTER":
        return anchor + dt.timedelta(days=(weekday - anchor.weekday() - 1) % 7 + 1)
    if direction == "ON_OR_BEFORE":
        return anchor - dt.timedelta(days=(anchor.weekday() - weekday) % 7)
    return anchor - dt.timedelta(days=(anchor.weekday() - weekday - 1) % 7 + 1)


def _occurrence_key(rule_year: int, sequence_index: int, observance: str) -> str:
    return f"{rule_year}:{sequence_index}:{'O' if observance == ORIGINAL else 'B'}"


def _fmt(day: dt.date) -> str:
    return f"{_WEEKDAY_NAMES[day.weekday()][:3]} {day.isoformat()}"


# --------------------------------------------------------------------------- Easter (§5)


def _computus(year: int, tradition: str) -> dt.date:
    if tradition == "WESTERN":
        a, b, c = year % 19, year // 100, year % 100
        d, e = b // 4, b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = c // 4, c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - Meeus/Jones/Butcher names
        m = (a + 11 * h + 22 * l) // 451
        n = h + l - 7 * m + 114
        return dt.date(year, n // 31, n % 31 + 1)
    if tradition == "ORTHODOX":
        a, b, c = year % 4, year % 7, year % 19
        d = (19 * c + 15) % 30
        e = (2 * a + 4 * b - d + 34) % 7
        n = d + e + 114
        julian = dt.date(year, n // 31, n % 31 + 1)
        return julian + dt.timedelta(days=year // 100 - year // 400 - 2)
    raise ValueError(f"unknown tradition {tradition!r}")


def _easter(year: int, tradition: Literal["WESTERN", "ORTHODOX"] = "WESTERN") -> dt.date:
    """Easter Sunday by the Anonymous Gregorian (Western) or Meeus Julian (Orthodox) computus, 2000-2100 (§5)."""
    if not _is_int(year) or not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"year out of range {MIN_YEAR}-{MAX_YEAR}: {year!r}")
    return _computus(year, tradition)


# --------------------------------------------------------------------------- base dates (§3.3)


def _base_date(kind: str, params: Mapping[str, Any], year: int, *, easter=_easter) -> dt.date | None:
    offset = dt.timedelta(days=params.get("offset_days", 0))
    if kind == "FIXED_DATE":
        try:
            base = dt.date(year, params["month"], params["day"])
        except ValueError:
            missing = params.get("missing_date", "SKIP")
            if missing == "SKIP":
                return None
            base = dt.date(year, 2, 28) if missing == "LAST_DAY_OF_MONTH" else dt.date(year, 3, 1)
        return base + offset
    if kind == "NTH_WEEKDAY":
        base = _nth_weekday(year, params["month"], params["weekday"], params["nth"])
        return None if base is None else base + offset
    if kind == "WEEKDAY_RELATIVE":
        try:
            anchor = dt.date(year, params["month"], params["day"])
        except ValueError:
            return None
        return _relative_weekday(anchor, params["weekday"], params["direction"])
    if kind == "EASTER_OFFSET":
        return easter(year, params.get("tradition", "WESTERN")) + offset
    if kind == "ONE_OFF":
        day = _as_date(params["date"])
        return day if day.year == year else None
    raise RecurrenceError("RULE_INVALID", f"kind {kind} has no base date")


def _start_dates(rule: RuleSpec, year: int) -> list[dt.date]:
    if rule.kind == "RRULE":
        return rrule_expand(rrule_parse(rule.params["rrule"]), _as_date(rule.params["dtstart"]), year)
    base = _base_date(rule.kind, rule.params, year)
    return [] if base is None else [base]


def _skip_note(rule: RuleSpec, year: int) -> Issue | None:
    params = rule.params
    if rule.kind == "FIXED_DATE":
        message = f"No {params['day']} {_MONTH_NAMES[params['month'] - 1]} in {year}"
    elif rule.kind == "NTH_WEEKDAY":
        message = (
            f"No {_ORDINALS[params['nth']]} {_WEEKDAY_NAMES[params['weekday']]} in "
            f"{_MONTH_NAMES[params['month'] - 1]} {year}"
        )
    elif rule.kind in ("WEEKDAY_RELATIVE", "RRULE"):
        message = f"{rule.name} has no date in {year}"
    else:
        return None
    return Issue("DATE_SKIPPED", message, "info", rule_id=rule.id, year=year)


# --------------------------------------------------------------------------- validation (§3.4)


def _param_problem(kind: str, key: str, value: Any) -> str | None:
    if key in ("month", "expected_month"):
        return None if _is_int(value) and 1 <= value <= 12 else "must be an integer 1-12"
    if key == "day":
        return None if _is_int(value) and 1 <= value <= 31 else "must be an integer 1-31"
    if key == "weekday":
        return None if _is_int(value) and 0 <= value <= 6 else "must be an integer 0 (Monday) to 6 (Sunday)"
    if key == "nth":
        return None if _is_int(value) and value in NTH_VALUES else "must be 1-5 or -1 (last)"
    if key == "offset_days":
        limit = _OFFSET_LIMIT.get(kind, 0)
        return None if _is_int(value) and -limit <= value <= limit else f"must be an integer from -{limit} to {limit}"
    if key == "missing_date":
        return None if value in MISSING_DATES else f"must be one of {', '.join(MISSING_DATES)}"
    if key == "direction":
        return None if value in DIRECTIONS else f"must be one of {', '.join(DIRECTIONS)}"
    if key == "tradition":
        return None if value in TRADITIONS else f"must be one of {', '.join(TRADITIONS)}"
    if key == "hint":
        return None if isinstance(value, str) and len(value) <= 200 else "must be text of at most 200 characters"
    if key == "rrule":
        return None if isinstance(value, str) and len(value) <= 500 else "must be text of at most 500 characters"
    if key in ("date", "dtstart"):
        try:
            _as_date(value)
        except ValueError:
            return "must be a date YYYY-MM-DD"
        return None
    if key == "observe_if_coincides":
        return None if isinstance(value, bool) else "must be true or false"
    return "is not a rule parameter"


def _rule_issues(rule: RuleSpec) -> list[Issue]:
    """Checks that need nothing but the rule; errors here make the rule unexpandable."""
    issues: list[Issue] = []

    def bad(field: str, message: str, code: str = "RULE_INVALID", **extra: Any) -> None:
        issues.append(Issue(code, message, "error", field=field, rule_id=rule.id, **extra))

    if not isinstance(rule.name, str) or not 1 <= len(rule.name.strip()) <= MAX_NAME_LENGTH:
        bad("name", f"Name must be 1-{MAX_NAME_LENGTH} characters")
    if rule.holiday_type not in HOLIDAY_TYPES:
        bad("holiday_type", f"Type must be one of {', '.join(HOLIDAY_TYPES)}")
    if rule.category not in HOLIDAY_CATEGORIES:
        bad("category", f"Category must be one of {', '.join(HOLIDAY_CATEGORIES)}")
    if rule.portion not in PORTIONS:
        bad("portion", f"Portion must be one of {', '.join(PORTIONS)}", "INVALID_PORTION")
    if rule.status not in RULE_STATUSES:
        bad("status", "Status must be ACTIVE or INACTIVE")
    if rule.observed_rule not in OBSERVED_RULES:
        bad("observed_rule", f"Observed rule must be one of {', '.join(OBSERVED_RULES)}")

    from_ok = _is_int(rule.valid_from_year) and MIN_YEAR <= rule.valid_from_year <= MAX_YEAR
    to_ok = rule.valid_to_year is None or (_is_int(rule.valid_to_year) and MIN_YEAR <= rule.valid_to_year <= MAX_YEAR)
    if not from_ok:
        bad("valid_from_year", f"From year must be {MIN_YEAR}-{MAX_YEAR}")
    if not to_ok:
        bad("valid_to_year", f"Until year must be {MIN_YEAR}-{MAX_YEAR}")
    if from_ok and to_ok and rule.valid_to_year is not None and rule.valid_to_year < rule.valid_from_year:
        bad("valid_to_year", "Until year must not be before the from year")

    if not _is_int(rule.duration_days) or not 1 <= rule.duration_days <= MAX_DURATION_DAYS:
        bad("duration_days", f"A holiday lasts 1-{MAX_DURATION_DAYS} days")
    if rule.kind == "ANNUAL_CONFIRM" and rule.duration_range is not None:
        rng = rule.duration_range
        if not (isinstance(rng, tuple) and len(rng) == 2 and all(_is_int(v) for v in rng)
                and 1 <= rng[0] <= rng[1] <= MAX_DURATION_DAYS):
            bad("duration_range", f"Length range must be [min, max] with 1 <= min <= max <= {MAX_DURATION_DAYS}")
        elif rule.duration_days != rng[0]:
            bad("duration_days", "For an announced holiday the length must equal the shortest length of the range")
    elif rule.duration_range is not None:
        bad("duration_range", "A length range is only for holidays announced every year")

    applies_on = rule.applies_on
    if applies_on is not None:
        if rule.observed_rule == "NONE":
            bad("applies_on", "Trigger days need an observed rule")
        elif not isinstance(applies_on, frozenset) or not applies_on or any(
            not _is_int(d) or not 0 <= d <= 6 for d in applies_on
        ):
            bad("applies_on", "Trigger days must be weekdays 0 (Monday) to 6 (Sunday)")

    if rule.kind not in KINDS:
        bad("kind", f"Kind must be one of {', '.join(KINDS)}")
        return issues
    params = rule.params
    if not isinstance(params, Mapping):
        bad("params", "Parameters must be an object")
        return issues
    required, optional = _KIND_PARAMS[rule.kind]
    allowed = set(required) | set(optional) | _COMMON_PARAMS
    for key in sorted(map(str, params)):
        if key not in allowed:
            bad(f"params.{key}", f"{key} is not used by {rule.kind} rules")
        else:
            problem = _param_problem(rule.kind, key, params[key])
            if problem:
                bad(f"params.{key}", f"{key} {problem}")
    for key in required:
        if key not in params:
            bad(f"params.{key}", f"{key} is required for {rule.kind} rules")
    if any(i.field and i.field.startswith("params.") for i in issues):
        return issues

    if rule.kind in ("FIXED_DATE", "WEEKDAY_RELATIVE"):
        try:
            dt.date(2000, params["month"], params["day"])
        except ValueError:
            bad("params.day", f"{_MONTH_NAMES[params['month'] - 1]} has no day {params['day']}")
    if rule.kind == "FIXED_DATE" and params.get("missing_date", "SKIP") != "SKIP" and (
        params["month"], params["day"]) != (2, 29):
        bad("params.missing_date", "A fallback for a missing date is only for 29 February")
    if rule.kind == "NTH_WEEKDAY" and params["nth"] == 5:
        issues.append(Issue(
            "RULE_NTH_5", f"No 5th {_WEEKDAY_NAMES[params['weekday']]} in some years; use -1 for the last one",
            "info", field="params.nth", rule_id=rule.id,
        ))
    if rule.kind == "ONE_OFF":
        year = _as_date(params["date"]).year
        if not (rule.valid_from_year == year and rule.valid_to_year == year):
            bad("valid_from_year", f"A one-off holiday is valid only in its own year: from and until year must be {year}")
    if rule.kind == "RRULE":
        try:
            rrule_parse(params["rrule"])
        except RecurrenceError as exc:
            bad("params.rrule", exc.message, reason="RRULE_UNSUPPORTED")
    return issues


def _normal_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def validate(
    rule: RuleSpec,
    *,
    weekend_days: Collection[int] | None = None,
    today: dt.date | None = None,
    half_days_enabled: bool = False,
    allow_rrule: bool = False,
    other_rules: Sequence[RuleSpec] = (),
) -> list[Issue]:
    """RULE_INVALID / INVALID_PORTION / HOLIDAY_IN_PAST issues and RV-02 info (§3.4 RV-01..RV-09).

    Context checks run only when their input is given: RV-04 needs the effective ``weekend_days``,
    RV-06 the calendar-local ``today``, RV-08 and the per-calendar name and count limits the calendar's
    ``other_rules``. ``half_days_enabled`` is LH-M5b; ``allow_rrule`` is the import path (RV-07).
    """
    issues = _rule_issues(rule)

    def bad(field: str, message: str, code: str = "RULE_INVALID") -> None:
        issues.append(Issue(code, message, "error", field=field, rule_id=rule.id))

    if rule.portion in PORTIONS and rule.portion != "FULL":
        if rule.holiday_type == "OPTIONAL":
            bad("portion", "Optional holidays are full days", "INVALID_PORTION")
        elif not half_days_enabled:
            bad("portion", "Half-day holidays are not available yet", "INVALID_PORTION")
    if rule.kind == "RRULE" and not allow_rrule:
        bad("kind", "Repeating rules in iCalendar format can only be imported.")
    if weekend_days is not None and isinstance(rule.applies_on, frozenset) and rule.observed_rule != "NONE":
        outside = rule.applies_on - _weekend(weekend_days)
        if outside:
            days = ", ".join(_WEEKDAY_NAMES[d] for d in sorted(outside) if _is_int(d) and 0 <= d <= 6)
            bad("applies_on", f"Trigger days must be weekend days of the calendar ({days} is a workday)")
    errors = any(i.severity == "error" for i in issues)
    if today is not None and rule.kind == "ONE_OFF" and not errors and _as_date(rule.params["date"]) <= today:
        bad("params.date", "The date has passed; record a past closure as a backdated closure", "HOLIDAY_IN_PAST")
    if other_rules and rule.status == "ACTIVE":
        issues.extend(_calendar_issues(rule, other_rules, errors))
    return issues


def _calendar_issues(rule: RuleSpec, other_rules: Sequence[RuleSpec], rule_has_errors: bool) -> list[Issue]:
    others = [o for o in other_rules if o.status == "ACTIVE" and (rule.id is None or o.id != rule.id)]
    issues: list[Issue] = []
    if len(others) >= MAX_ACTIVE_RULES:
        issues.append(Issue(
            "RULE_INVALID", f"A calendar can hold at most {MAX_ACTIVE_RULES} repeating holidays", field="status",
            rule_id=rule.id,
        ))
    name = _normal_name(rule.name) if isinstance(rule.name, str) else None
    for other in others:
        if not isinstance(other.name, str) or _normal_name(other.name) != name:
            continue
        shared = None if rule_has_errors else _first_shared_date(rule, other)
        message = (
            f"{other.name} already gives this holiday on {_fmt(shared)}" if shared
            else f"Another repeating holiday is already called {other.name}"
        )
        issues.append(Issue("RULE_INVALID", message, field="name", reason="DUPLICATE", rule_id=rule.id))
    return issues


def _first_shared_date(rule: RuleSpec, other: RuleSpec) -> dt.date | None:
    first = max(rule.valid_from_year, MIN_YEAR)
    for year in range(first, min(first + 5, MAX_YEAR + 1)):
        try:
            mine = {o.date for o in expand(rule, year, frozenset())}
            theirs = {o.date for o in expand(other, year, frozenset())}
        except RecurrenceError:
            return None
        shared = sorted(mine & theirs)
        if shared:
            return shared[0]
    return None


def _require_valid(rule: RuleSpec) -> None:
    errors = [i for i in _rule_issues(rule) if i.severity == "error"]
    if errors:
        raise RecurrenceError("RULE_INVALID", f"rule {rule.name!r} is invalid: {errors[0].message}", errors)


# --------------------------------------------------------------------------- expansion (§4.1)


def expand(
    rule: RuleSpec, year: int, weekend_days: Collection[int], *, expected_start: dt.date | None = None
) -> list[Occurrence]:
    """ORIGINAL rows of rule-year ``year`` (no observed rule applied); multi-day blocks get sequence_index 1..n.

    ANNUAL_CONFIRM rows need ``expected_start`` and are PROVISIONAL, ``duration_range[0]`` days long;
    ONE_OFF yields only in its own year. A rule outside its validity years yields nothing.
    """
    _weekend(weekend_days)
    _require_valid(rule)
    if rule.kind == "ANNUAL_CONFIRM":
        if expected_start is not None:
            _as_date(expected_start)
        starts = [] if expected_start is None else [expected_start]
        length = rule.duration_range[0] if rule.duration_range else rule.duration_days
        status = PROVISIONAL
    elif expected_start is not None:
        raise RecurrenceError("RULE_INVALID", "an expected start date is only for ANNUAL_CONFIRM rules")
    else:
        starts, length, status = None, rule.duration_days, CONFIRMED
    if not rule.valid_in(year):
        return []
    if starts is None:
        starts = _start_dates(rule, year)
    rows: list[Occurrence] = []
    for start in starts:
        for i in range(length):
            seq = i + 1
            rows.append(Occurrence(
                date=start + dt.timedelta(days=i),
                name=rule.name.strip(),
                holiday_type=rule.holiday_type,
                category=rule.category,
                portion=rule.portion,
                status=status,
                observance=ORIGINAL,
                sequence_index=seq,
                rule_id=rule.id,
                rule_year=year,
                rule_version=rule.version,
                occurrence_key=_occurrence_key(year, seq, ORIGINAL),
                name_i18n=rule.name_i18n,
                rule=rule,
            ))
    return rows


# --------------------------------------------------------------------------- observance (§3.5, §4.3.1)

_Owner = tuple[Any, Any]
_ANONYMOUS: _Owner = (None, None)


def _owner(row: Occurrence) -> _Owner:
    return (row.rule_id, row.occurrence_key) if row.rule_id is not None or row.rule is not None else _ANONYMOUS


def _row_order(row: Occurrence) -> tuple:
    return (row.date, HOLIDAY_RANK.get(row.holiday_type, len(HOLIDAY_RANK)), row.rule_id is None,
            row.rule_id or 0, row.sequence_index)


def _rule_order(rule: RuleSpec) -> tuple:
    return (rule.id is None, rule.id or 0)


def _direction(observed_rule: str, day: dt.date, weekend: frozenset[int]) -> int:
    if observed_rule == "NEXT_WORKDAY":
        return 1
    if observed_rule == "PREVIOUS_WORKDAY":
        return -1
    if len(weekend) <= 1:
        return 1
    first_of_run = (day.weekday() - 1) % 7 not in weekend
    return -1 if first_of_run else 1


def _search(day: dt.date, step: int, weekend: frozenset[int], blockers: Mapping[dt.date, set], own: _Owner):
    for i in range(1, OBSERVED_HORIZON_DAYS + 1):
        candidate = day + dt.timedelta(days=i * step)
        if candidate.weekday() in weekend:
            continue
        owners = blockers.get(candidate)
        if owners and owners - {own}:
            continue
        return candidate
    return None


def _observe(
    rows: Sequence[Occurrence],
    weekend: frozenset[int],
    blockers: dict[dt.date, set],
    holders: dict[dt.date, list[tuple[int, _Owner]]],
) -> tuple[list[Occurrence], list[tuple[Issue, dt.date, int]]]:
    out: list[Occurrence] = []
    misses: list[tuple[Issue, dt.date, int]] = []
    for row in rows:
        out.append(row)
        rank = HOLIDAY_RANK.get(row.holiday_type, len(HOLIDAY_RANK))
        held = holders.setdefault(row.date, [])
        holder = min((r for r, owner in held if owner != _owner(row)), default=None)
        held.append((rank, _owner(row)))
        rule = row.rule
        if rule is None or row.observance != ORIGINAL or row.status == CANCELLED:
            continue
        trigger = rule.applies_on or weekend
        if rule.observed_rule != "NONE" and row.date.weekday() in trigger:
            step = _direction(rule.observed_rule, row.date, weekend)
        elif rule.params.get("observe_if_coincides") and holder is not None and holder <= rank:
            step = 1
        else:
            continue
        key = _occurrence_key(row.rule_year, row.sequence_index, OBSERVED) if row.rule_year is not None else None
        own: _Owner = (row.rule_id, key)
        target = _search(row.date, step, weekend, blockers, own)
        if target is None:
            note = Issue(
                "NO_OBSERVED_WORKDAY", f"No working day found for the observed date of {row.name}", "warning",
                rule_id=row.rule_id, year=row.rule_year,
            )
            misses.append((note, row.date, step))
            continue
        out.append(replace(row, date=target, observance=OBSERVED, observed_from_date=row.date, occurrence_key=key))
        blockers.setdefault(target, set()).add(own)
    return out, misses


def apply_observance(
    rows: Sequence[Occurrence],
    weekend_days: Collection[int],
    taken: Collection[dt.date],
    *,
    fixed_rows: Sequence[Occurrence] = (),
) -> list[Occurrence]:
    """ORIGINAL rows plus OBSERVED partners per observed_rule (NONE, NEAREST_WORKDAY, NEXT_WORKDAY,
    PREVIOUS_WORKDAY), chain-safe in date order, skipping weekend days and dates in ``taken`` or held by
    ``rows``, horizon 14 days (none found -> no partner; ``generate_year`` adds the note, LH-REC-10).

    ``fixed_rows`` (non-cancelled) give the rank holders for ``observe_if_coincides`` (G-07).
    """
    weekend = _weekend(weekend_days)
    ordered = sorted(rows, key=_row_order)
    blockers: dict[dt.date, set] = {day: {_ANONYMOUS} for day in taken}
    holders: dict[dt.date, list[tuple[int, _Owner]]] = {}
    for fixed in fixed_rows:
        if fixed.status != CANCELLED:
            blockers.setdefault(fixed.date, set()).add(_owner(fixed))
            holders.setdefault(fixed.date, []).append((HOLIDAY_RANK.get(fixed.holiday_type, 3), _owner(fixed)))
    for row in ordered:
        blockers.setdefault(row.date, set()).add(_owner(row))
    return _observe(ordered, weekend, blockers, holders)[0]


# --------------------------------------------------------------------------- generation (§4.2-§4.7)


def _touches(day: dt.date, step: int, year: int) -> bool:
    if day.year == year:
        return True
    return any((day + dt.timedelta(days=i * step)).year == year for i in range(1, OBSERVED_HORIZON_DAYS + 1))


def generate_year(
    rules: Sequence[RuleSpec],
    year: int,
    weekend_days: Collection[int],
    fixed_rows: Sequence[Occurrence] = (),
    expected_dates: Mapping[tuple[int | None, int], dt.date] | None = None,
    *,
    weekly_off_days: Collection[int] = (),
) -> GenerationResult:
    """Calendar year ``year`` from rule-years Y-1..Y+1 (V3-059): ORIGINAL and OBSERVED rows dated in Y,
    cross-year items, and notes.

    ``fixed_rows`` are rows the generator respects but never rewrites (manual and imported rows, overridden
    or skipped rule rows, rows of PUBLISHED years and of included calendars, years Y-1..Y+1); non-cancelled
    ones are taken dates (G-02). ``expected_dates`` maps ``(rule_id, rule_year)`` to the expected start of an
    ANNUAL_CONFIRM rule (§6.2; see ``select_expected_start``). ``weekly_off_days`` only widens the
    "falls on a weekend" note to weekdays that are off in a work week of the calendar's employees (§8.5).
    """
    _check_year(year)
    weekend = _weekend(weekend_days)
    expected = expected_dates or {}
    notes: list[Issue] = []
    base: list[Occurrence] = []
    for rule in sorted((r for r in rules if r.status == "ACTIVE"), key=_rule_order):
        for rule_year in (year - 1, year, year + 1):
            if not rule.valid_in(rule_year):
                continue
            start = None
            if rule.kind == "ANNUAL_CONFIRM":
                start = expected.get((rule.id, rule_year))
                if start is None:
                    if rule_year == year:
                        notes.append(Issue(
                            "HOLIDAY_DATE_NEEDED",
                            f"{rule.name} has no date for {year}. Employees will not get this holiday until you add it.",
                            "warning", rule_id=rule.id, year=year,
                        ))
                    continue
            rows = expand(rule, rule_year, weekend, expected_start=start)
            if not rows and rule_year == year:
                note = _skip_note(rule, year)
                if note:
                    notes.append(note)
            base.extend(rows)
    base.sort(key=_row_order)

    blockers: dict[dt.date, set] = {}
    holders: dict[dt.date, list[tuple[int, _Owner]]] = {}
    for fixed in fixed_rows:
        if fixed.status != CANCELLED:
            blockers.setdefault(fixed.date, set()).add(_owner(fixed))
            holders.setdefault(fixed.date, []).append((HOLIDAY_RANK.get(fixed.holiday_type, 3), _owner(fixed)))
    for row in base:
        blockers.setdefault(row.date, set()).add(_owner(row))
    rows, misses = _observe(base, weekend, blockers, holders)
    notes.extend(note for note, day, step in misses if _touches(day, step, year))

    keep = sorted((r for r in rows if r.date.year == year), key=_row_order)
    stored = {(f.rule_id, f.occurrence_key) for f in fixed_rows if f.rule_id is not None}
    cross = sorted((
        r for r in rows
        if r.date.year != year and r.observance == OBSERVED and r.rule_year == year
        and (r.rule_id, r.occurrence_key) not in stored
    ), key=_row_order)
    notes.extend(_weekend_notes(keep, rows, weekend | frozenset(weekly_off_days)))
    notes.extend(_duplicate_notes(keep, fixed_rows))
    return GenerationResult(rows=tuple(keep), cross_year_items=tuple(cross), notes=tuple(notes))


def _weekend_notes(keep: Sequence[Occurrence], rows: Sequence[Occurrence], off_days: frozenset[int]) -> list[Issue]:
    observed = {
        (r.rule_id, r.rule_year, r.sequence_index, r.observed_from_date): r.date for r in rows if r.observance == OBSERVED
    }
    notes = []
    for row in keep:
        if row.observance != ORIGINAL or row.date.weekday() not in off_days:
            continue
        moved = observed.get((row.rule_id, row.rule_year, row.sequence_index, row.date))
        outcome = f"observed {_fmt(moved)}" if moved else "not moved"
        notes.append(Issue(
            "HOLIDAY_ON_WEEKEND", f"{row.name} falls on {_fmt(row.date)}; {outcome}", "info",
            rule_id=row.rule_id, year=row.date.year,
        ))
    return notes


def _duplicate_notes(keep: Sequence[Occurrence], fixed_rows: Sequence[Occurrence]) -> list[Issue]:
    existing = {
        (f.date, _normal_name(f.name)): _owner(f) for f in fixed_rows if f.status != CANCELLED
    }
    notes = []
    for row in keep:
        owner = existing.get((row.date, _normal_name(row.name)))
        if owner is not None and owner != _owner(row):
            notes.append(Issue(
                "DUPLICATE_HOLIDAY", f"{row.name} on {_fmt(row.date)} is already on the calendar", "info",
                rule_id=row.rule_id, year=row.date.year,
            ))
    return notes


def select_expected_start(candidates: Mapping[str, dt.date]) -> tuple[str, dt.date] | None:
    """The expected start of an ANNUAL_CONFIRM rule-year by source priority TENANT > IMPORT/CONNECTOR > PACK (§6.2)."""
    known = [(source, day) for source, day in candidates.items() if day is not None]
    unknown = [source for source, _ in known if source not in _EXPECTED_PRIORITY]
    if unknown:
        raise RecurrenceError("INVALID_EXPECTED_SOURCE", f"unknown expected-date source {unknown[0]!r}")
    if not known:
        return None
    return min(known, key=lambda item: (_EXPECTED_PRIORITY[item[0]], EXPECTED_SOURCES.index(item[0])))


def preview(
    rule: RuleSpec,
    other_rules: Sequence[RuleSpec],
    weekend_days: Collection[int],
    *,
    years: int = 5,
    from_year: int | None = None,
    fixed_rows: Sequence[Occurrence] = (),
    expected_dates: Mapping[tuple[int | None, int], dt.date] | None = None,
) -> list[Occurrence]:
    """Rows of ``rule`` for ``years`` calendar years from ``from_year`` (default ``valid_from_year``), generated
    with the calendar's other ACTIVE rules so observed chains are realistic (§3.7 ``POST /rules/preview``)."""
    if not _is_int(years) or not 1 <= years <= MAX_PREVIEW_YEARS:
        raise RecurrenceError("RULE_INVALID", f"years must be 1-{MAX_PREVIEW_YEARS}, got {years!r}")
    start = rule.valid_from_year if from_year is None else from_year
    _check_year(start)
    candidate = replace(rule, status="ACTIVE")
    others = [o for o in other_rules if rule.id is None or o.id != rule.id]
    out: list[Occurrence] = []
    for year in range(start, min(start + years, MAX_YEAR + 1)):
        result = generate_year([candidate, *others], year, weekend_days, fixed_rows, expected_dates)
        out.extend(r for r in result.rows if r.rule is candidate)
    return out


# --------------------------------------------------------------------------- RRULE subset (§7)

_REJECTED_PARTS = frozenset({"RSCALE", "SKIP", "BYYEARDAY", "BYWEEKNO", "BYHOUR", "BYMINUTE", "BYSECOND"})
_ACCEPTED_PARTS = frozenset({
    "FREQ", "INTERVAL", "BYMONTH", "BYMONTHDAY", "BYDAY", "BYSETPOS", "COUNT", "UNTIL", "BYEASTER", "WKST",
})
_BYDAY = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")


def _unsupported(message: str) -> RecurrenceError:
    return RecurrenceError("RULE_INVALID", message, [Issue("RULE_INVALID", message, reason="RRULE_UNSUPPORTED")])


def _rrule_int(value: str, what: str) -> int:
    if not re.fullmatch(r"[+-]?\d{1,3}", value):
        raise _unsupported(f"{what} must be a whole number, got {value!r}")
    return int(value)


def rrule_parse(text: str) -> RRuleParts:
    """Parse an RRULE value inside the accepted subset (§7.1); anything else raises RRULE_UNSUPPORTED."""
    if not isinstance(text, str) or not text.strip():
        raise _unsupported("An iCalendar rule is required")
    body = text.strip()
    if body.upper().startswith("RRULE:"):
        body = body[6:]
    raw: dict[str, str] = {}
    for part in filter(None, body.split(";")):
        key, sep, value = part.partition("=")
        key = key.strip().upper()
        if not sep or not value.strip():
            raise _unsupported(f"Malformed rule part {part!r}")
        if key in _REJECTED_PARTS:
            raise _unsupported(
                "Non-Gregorian repeating rules are not supported; use an announced-every-year holiday"
                if key in ("RSCALE", "SKIP") else f"{key} is not supported in holiday rules"
            )
        if key not in _ACCEPTED_PARTS:
            raise _unsupported(f"{key} is not supported in holiday rules")
        if key in raw:
            raise _unsupported(f"{key} appears twice")
        raw[key] = value.strip().upper()
    if raw.get("FREQ") != "YEARLY":
        raise _unsupported("Only yearly rules are holidays; repeating weekly or monthly days off are work-week patterns")
    if "INTERVAL" in raw and _rrule_int(raw["INTERVAL"], "INTERVAL") != 1:
        raise _unsupported("Rules that repeat every few years are not supported")
    if "COUNT" in raw and "UNTIL" in raw:
        raise _unsupported("COUNT and UNTIL cannot both be set")

    bymonth = None
    if "BYMONTH" in raw:
        if "," in raw["BYMONTH"] or raw["BYMONTH"].endswith("L"):
            raise _unsupported("BYMONTH must be exactly one month 1-12")
        bymonth = _rrule_int(raw["BYMONTH"], "BYMONTH")
        if not 1 <= bymonth <= 12:
            raise _unsupported("BYMONTH must be exactly one month 1-12")
    monthdays: tuple[int, ...] = ()
    if "BYMONTHDAY" in raw:
        monthdays = tuple(_rrule_int(v, "BYMONTHDAY") for v in raw["BYMONTHDAY"].split(","))
        run = len(monthdays) == 7 and monthdays == tuple(range(monthdays[0], monthdays[0] + 7))
        if any(not 1 <= d <= 31 for d in monthdays) or not (len(monthdays) == 1 or run):
            raise _unsupported("BYMONTHDAY must be one day 1-31 or a run of seven consecutive days")
    weekday = ordinal = None
    if "BYDAY" in raw:
        match = _BYDAY.match(raw["BYDAY"])
        if match is None:
            raise _unsupported("BYDAY must be exactly one weekday, optionally with 1-5 or -1")
        weekday = _RRULE_DAYS.index(match.group(2))
        if match.group(1) is not None:
            ordinal = int(match.group(1))
            if ordinal not in NTH_VALUES:
                raise _unsupported("A BYDAY ordinal must be 1-5 or -1")
    bysetpos = None
    if "BYSETPOS" in raw:
        bysetpos = _rrule_int(raw["BYSETPOS"], "BYSETPOS")
        if bysetpos not in NTH_VALUES:
            raise _unsupported("BYSETPOS must be 1-5 or -1")
    byeaster = None
    if "BYEASTER" in raw:
        byeaster = _rrule_int(raw["BYEASTER"], "BYEASTER")
        if not -70 <= byeaster <= 70:
            raise _unsupported("BYEASTER must be -70 to 70 days")
        if bymonth or monthdays or weekday is not None or bysetpos is not None:
            raise _unsupported("BYEASTER cannot be combined with month or weekday parts")
    if weekday is not None:
        if bymonth is None:
            raise _unsupported("A weekday rule needs BYMONTH (otherwise it counts within the year)")
        if ordinal is not None and (monthdays or bysetpos is not None):
            raise _unsupported("A numbered weekday cannot be combined with BYMONTHDAY or BYSETPOS")
        if ordinal is None and (len(monthdays) == 7) == (bysetpos is not None):
            raise _unsupported("A weekday needs a number, BYSETPOS, or a seven-day BYMONTHDAY run")
    elif bysetpos is not None or len(monthdays) == 7:
        raise _unsupported("BYSETPOS and a seven-day BYMONTHDAY run need one BYDAY weekday")

    count = until = None
    if "COUNT" in raw:
        count = _rrule_int(raw["COUNT"], "COUNT")
        if count < 1:
            raise _unsupported("COUNT must be at least 1")
    if "UNTIL" in raw:
        stamp = raw["UNTIL"]
        try:
            until = dt.date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
        except ValueError:
            raise _unsupported(f"UNTIL must be a date YYYYMMDD, got {stamp!r}") from None
        if len(stamp) != 8 and not re.fullmatch(r"\d{8}T\d{6}Z?", stamp):
            raise _unsupported(f"UNTIL must be a date YYYYMMDD, got {stamp!r}")
    return RRuleParts(
        bymonth=bymonth, bymonthday=monthdays, weekday=weekday, ordinal=ordinal, bysetpos=bysetpos,
        count=count, until=until, byeaster=byeaster,
    )


def _native_shape(parts: RRuleParts, dtstart: dt.date) -> tuple[str, dict[str, Any]] | None:
    if parts.byeaster is not None:
        return "EASTER_OFFSET", {"offset_days": parts.byeaster, "tradition": "WESTERN"}
    month = parts.bymonth or dtstart.month
    if parts.weekday is not None:
        if parts.ordinal is not None or parts.bysetpos is not None:
            nth = parts.ordinal if parts.ordinal is not None else parts.bysetpos
            return "NTH_WEEKDAY", {"month": month, "weekday": parts.weekday, "nth": nth}
        return "WEEKDAY_RELATIVE", {
            "month": month, "day": parts.bymonthday[0], "weekday": parts.weekday, "direction": "ON_OR_AFTER",
        }
    day = parts.bymonthday[0] if parts.bymonthday else dtstart.day
    try:
        dt.date(2000, month, day)
    except ValueError:
        return None
    return "FIXED_DATE", {"month": month, "day": day}


def _rrule_instance(kind: str, params: Mapping[str, Any], year: int, dtstart: dt.date) -> dt.date | None:
    day = _base_date(kind, params, year, easter=_computus)
    return day if day is not None and day >= dtstart else None


def rrule_to_native(parts: RRuleParts, dtstart: dt.date) -> NativeRule | None:
    """The native rule of an accepted RRULE (§7.2): 7-day BYMONTHDAY runs -> WEEKDAY_RELATIVE ON_OR_AFTER,
    numbered or BYSETPOS weekdays -> NTH_WEEKDAY, BYEASTER -> EASTER_OFFSET, else FIXED_DATE; None when it
    has no instance. ``valid_to_year`` is the year of the last instance under COUNT or UNTIL."""
    dtstart = _as_date(dtstart)
    shape = _native_shape(parts, dtstart)
    if shape is None:
        return None
    kind, params = shape
    first = last = None
    seen = 0
    for year in range(dtstart.year, MAX_YEAR + 1):
        day = _rrule_instance(kind, params, year, dtstart)
        if day is None:
            continue
        if parts.until is not None and day > parts.until:
            break
        first = first or year
        last = year
        seen += 1
        if parts.count is not None and seen == parts.count:
            break
    if first is None:
        return None
    bounded = parts.count is not None or parts.until is not None
    return NativeRule(kind, MappingProxyType(params), dtstart.year, last if bounded else None)


def rrule_expand(parts: RRuleParts, dtstart: dt.date, year: int) -> list[dt.date]:
    """Instance dates of an accepted RRULE in ``year``; invalid instances are skipped, never moved (§7.1)."""
    native = rrule_to_native(parts, dtstart)
    if native is None or year < native.valid_from_year:
        return []
    if native.valid_to_year is not None and year > native.valid_to_year:
        return []
    day = _rrule_instance(native.kind, native.params, year, _as_date(dtstart))
    if day is None or (parts.until is not None and day > parts.until):
        return []
    return [day]
