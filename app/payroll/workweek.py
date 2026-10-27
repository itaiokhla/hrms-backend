"""Work-week patterns: which dates are weekly offs for an employee (3.4 §3; LH 04-holiday/03 §3, §6.3).

A WEEKLY pattern is a set of weekdays that are always off (Monday = 0 ... Sunday = 6)
plus nth-weekday rules such as "2nd and 4th Saturday off". Occurrences count
within the calendar month (days 1-7 are the 1st occurrence, 8-14 the 2nd, ...)
and ``LAST`` (-1) is the last occurrence in the month, so a rule never depends
on how pay periods are cut. A WEEKLY pattern may also give half-day offs
("Saturday afternoon off", optionally on some occurrences only).

A CYCLE pattern repeats every ``cycle_length_days`` days from ``anchor_date``
(cycle day 0); ``cycle_off_days`` are its full offs. It expresses schedules that
do not repeat every calendar week (alternate Saturdays, 4-on-4-off, 2-2-3,
DuPont). The anchor never moves: a parity change is a new pattern version.

``rest_days`` (optional) names which full offs are rest days; absent means every
full off is a rest day (today's behaviour, LH-WWK-04).

A scheduled working day is a date that is neither a (full) weekly off in the
employee's pattern nor a paid holiday on their branch calendar. Callers pass
the paid-holiday dates, already filtered by holiday type, because which types
are paid is a policy (``HOLIDAY_PAY_POLICY.paid_holiday_types``).

Patterns are canonical values: the same rules compare and hash equal whatever
order they were given in, and ``to_json``/``from_json`` round-trip the stored
JSON form exactly. A WEEKLY pattern without half-day offs or rest days
serialises exactly as before kinds existed, so stored hashes stay stable.

Everything here is pure: no database, no clock, no settings.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal, Optional

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)
WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
LAST = -1
NTH_VALUES = frozenset({1, 2, 3, 4, 5, LAST})
_EVERY_OCCURRENCE = frozenset({1, 2, 3, 4, 5})
# The 5th occurrence is always the last one, so {1, 2, 3, 4, -1} also covers every occurrence (V-4).
_EVERY_OCCURRENCE_WITH_LAST = frozenset({1, 2, 3, 4, LAST})
_ONE_DAY = dt.timedelta(days=1)
_ONE_WEEK = dt.timedelta(days=7)

WEEKLY, CYCLE = "WEEKLY", "CYCLE"
KINDS = (WEEKLY, CYCLE)
FULL, FIRST_HALF, SECOND_HALF, NONE = "FULL", "FIRST_HALF", "SECOND_HALF", "NONE"
HALF_PORTIONS = (FIRST_HALF, SECOND_HALF)
MIN_CYCLE_DAYS, MAX_CYCLE_DAYS = 2, 56
# Longest range one evaluation call expands (preview, junction checks); mirrors the day-facts horizon.
MAX_RANGE_DAYS = 400

Portion = Literal["FULL", "FIRST_HALF", "SECOND_HALF"]
WorkingPortion = Literal["FULL", "FIRST_HALF", "SECOND_HALF", "NONE"]


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


def _portion(value: object) -> str:
    if value not in HALF_PORTIONS:
        raise WorkWeekError("INVALID_PORTION", f"a half-day off is FIRST_HALF or SECOND_HALF, got {value!r}")
    return str(value)


def _cycle_day(value: object, length: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < length:
        raise WorkWeekError("INVALID_PATTERN", f"{what} must be a cycle day from 0 to {length - 1}, got {value!r}")
    return value


def occurrences_can_meet(a: Collection[int], b: Collection[int]) -> bool:
    """Whether two nth-occurrence sets can match the same date (LAST meets the 4th or 5th occurrence)."""
    a, b = frozenset(a), frozenset(b)
    if a & b:
        return True
    return (LAST in a and bool(b & {4, 5})) or (LAST in b and bool(a & {4, 5}))


@dataclass(frozen=True)
class WorkWeekPattern:
    """Weekly offs of one pattern version (04-holiday/03 §3.2).

    WEEKLY: ``weekly_off_days`` every week, ``(weekday, occurrences)`` rules per month, and half-day offs
    ``(weekday, portion, occurrences or None)`` (None = every week).
    CYCLE: ``cycle_off_days`` of a ``cycle_length_days`` loop starting at ``anchor_date``, and half-day offs
    ``(cycle_day, portion, None)``.
    """

    weekly_off_days: frozenset[int] = frozenset()
    nth_weekday_offs: tuple[tuple[int, frozenset[int]], ...] = ()
    kind: str = WEEKLY
    half_day_offs: tuple[tuple[int, str, Optional[frozenset[int]]], ...] = ()
    cycle_length_days: Optional[int] = None
    cycle_off_days: frozenset[int] = frozenset()
    anchor_date: Optional[dt.date] = None
    rest_days: Optional[frozenset[int]] = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise WorkWeekError("INVALID_PATTERN", f"kind must be WEEKLY or CYCLE, got {self.kind!r}")
        if self.kind == WEEKLY:
            self._validate_weekly()
        else:
            self._validate_cycle()

    # ------------------------------------------------------------------ validation (V-1..V-8)
    def _validate_weekly(self) -> None:
        if self.cycle_length_days is not None or self.anchor_date is not None or _items(self.cycle_off_days, "cycle_off_days"):
            raise WorkWeekError("KIND_MISMATCH", "a WEEKLY pattern has no cycle length, cycle offs or anchor date")
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
            if _EVERY_OCCURRENCE <= occurrences or _EVERY_OCCURRENCE_WITH_LAST <= occurrences:
                raise WorkWeekError(
                    "EVERY_OCCURRENCE", f"every {WEEKDAY_CODES[weekday]} is off; list it in weekly_off_days instead"
                )
            rules[weekday] = occurrences
        halves: dict[int, tuple[int, str, Optional[frozenset[int]]]] = {}
        for item in _items(self.half_day_offs, "half_day_offs"):
            parts = _items(item, "a half-day off")
            if len(parts) != 3:
                raise WorkWeekError("INVALID_PATTERN", f"a half-day off is (weekday, portion, occurrences), got {item!r}")
            weekday = _weekday(parts[0], "a half-day off's weekday")
            portion = _portion(parts[1])
            nth = None
            if parts[2] is not None:
                nth = frozenset(_occurrence(n) for n in _items(parts[2], "occurrences"))
                if not nth:
                    raise WorkWeekError("EMPTY_RULE", f"the half-day rule for {WEEKDAY_CODES[weekday]} lists no occurrence")
            if weekday in off:
                raise WorkWeekError("REDUNDANT_RULE", f"{WEEKDAY_CODES[weekday]} is already off every week")
            if weekday in halves:
                raise WorkWeekError("DUPLICATE_RULE", f"{WEEKDAY_CODES[weekday]} has more than one half-day rule")
            if weekday in rules and (nth is None or occurrences_can_meet(nth, rules[weekday])):
                raise WorkWeekError(
                    "OVERLAPPING_RULE",
                    f"the half-day and full-day rules for {WEEKDAY_CODES[weekday]} must list different occurrences",
                )
            halves[weekday] = (weekday, portion, nth)
        if len(off) == 7:
            raise WorkWeekError("NO_WORKING_DAYS", "a work week needs at least one working weekday")
        rest = self._rest(off | frozenset(rules), "a weekly off or nth-rule weekday")
        object.__setattr__(self, "weekly_off_days", off)
        object.__setattr__(self, "nth_weekday_offs", tuple(sorted(rules.items())))
        object.__setattr__(self, "half_day_offs", tuple(halves[k] for k in sorted(halves)))
        object.__setattr__(self, "cycle_off_days", frozenset())
        object.__setattr__(self, "rest_days", rest)

    def _validate_cycle(self) -> None:
        if _items(self.weekly_off_days, "weekly_off_days") or _items(self.nth_weekday_offs, "nth_weekday_offs"):
            raise WorkWeekError("KIND_MISMATCH", "a CYCLE pattern has no weekly_off_days or nth_weekday_offs")
        length = self.cycle_length_days
        if isinstance(length, bool) or not isinstance(length, int) or not MIN_CYCLE_DAYS <= length <= MAX_CYCLE_DAYS:
            raise WorkWeekError("INVALID_CYCLE", f"a cycle is {MIN_CYCLE_DAYS}-{MAX_CYCLE_DAYS} days long, got {length!r}")
        if self.anchor_date is None:
            raise WorkWeekError("ANCHOR_REQUIRED", "a CYCLE pattern needs the anchor date of cycle day 0")
        _require_date(self.anchor_date, "anchor_date")
        days = [_cycle_day(d, length, "a cycle off day") for d in _items(self.cycle_off_days, "cycle_off_days")]
        if len(set(days)) != len(days):
            raise WorkWeekError("INVALID_PATTERN", "cycle_off_days repeats a day")
        off = frozenset(days)
        if len(off) == length:
            raise WorkWeekError("NO_WORKING_DAYS", "a cycle needs at least one working day")
        halves: dict[int, tuple[int, str, None]] = {}
        for item in _items(self.half_day_offs, "half_day_offs"):
            parts = _items(item, "a half-day off")
            if len(parts) != 3 or parts[2] is not None:
                raise WorkWeekError("INVALID_PATTERN", "a cycle half-day off is (cycle_day, portion) without occurrences")
            day = _cycle_day(parts[0], length, "a half-day off's cycle day")
            portion = _portion(parts[1])
            if day in off:
                raise WorkWeekError("REDUNDANT_RULE", f"cycle day {day} is already a full off")
            if day in halves:
                raise WorkWeekError("DUPLICATE_RULE", f"cycle day {day} has more than one half-day rule")
            halves[day] = (day, portion, None)
        rest = self._rest(off, "a cycle off day")
        object.__setattr__(self, "cycle_off_days", off)
        object.__setattr__(self, "half_day_offs", tuple(halves[k] for k in sorted(halves)))
        object.__setattr__(self, "weekly_off_days", frozenset())
        object.__setattr__(self, "nth_weekday_offs", ())
        object.__setattr__(self, "rest_days", rest)

    def _rest(self, allowed: frozenset[int], what: str) -> Optional[frozenset[int]]:
        if self.rest_days is None:
            return None
        values = _items(self.rest_days, "rest_days")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in values) or len(set(values)) != len(values):
            raise WorkWeekError("INVALID_REST_DAYS", "rest_days are distinct integers")
        rest = frozenset(values)  # type: ignore[arg-type]
        outside = sorted(rest - allowed)
        if outside:
            raise WorkWeekError("INVALID_REST_DAYS", f"rest days must be full offs; {outside} is not {what}")
        return rest or None  # empty = absent = every full off is a rest day

    # ------------------------------------------------------------------ JSON
    def to_json(self) -> dict[str, Any]:
        """Canonical rules JSON (``work_week_pattern_versions.rules``); keys absent when empty."""
        if self.kind == CYCLE:
            data: dict[str, Any] = {
                "kind": CYCLE,
                "cycle_length_days": self.cycle_length_days,
                "anchor_date": self.anchor_date.isoformat() if self.anchor_date else None,
                "cycle_off_days": sorted(self.cycle_off_days),
            }
            if self.half_day_offs:
                data["half_day_offs"] = [{"cycle_day": d, "portion": p} for d, p, _ in self.half_day_offs]
        else:
            data = {
                "weekly_off_days": sorted(self.weekly_off_days),
                "nth_weekday_offs": [{"weekday": day, "nth": sorted(nths)} for day, nths in self.nth_weekday_offs],
            }
            if self.half_day_offs or self.rest_days:
                data["kind"] = WEEKLY
            if self.half_day_offs:
                data["half_day_offs"] = [
                    {"weekday": d, "portion": p, **({"nth": sorted(n)} if n is not None else {})}
                    for d, p, n in self.half_day_offs
                ]
        if self.rest_days:
            data["rest_days"] = sorted(self.rest_days)
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> WorkWeekPattern:
        """Parse a stored or posted rules object; unknown keys are refused."""
        if not isinstance(data, Mapping):
            raise WorkWeekError("INVALID_PATTERN", "a pattern must be a JSON object")
        kind = data.get("kind", WEEKLY)
        if kind not in KINDS:
            raise WorkWeekError("INVALID_PATTERN", f"kind must be WEEKLY or CYCLE, got {kind!r}")
        weekly_keys = {"weekly_off_days", "nth_weekday_offs"}
        cycle_keys = {"cycle_length_days", "anchor_date", "cycle_off_days"}
        common = {"kind", "half_day_offs", "rest_days"}
        unknown = set(data) - weekly_keys - cycle_keys - common
        if unknown:
            raise WorkWeekError("INVALID_PATTERN", f"unknown pattern keys: {', '.join(sorted(map(str, unknown)))}")
        if (kind == WEEKLY and set(data) & cycle_keys) or (kind == CYCLE and set(data) & weekly_keys):
            raise WorkWeekError("KIND_MISMATCH", f"{kind} rules cannot use {', '.join(sorted(set(data) & (cycle_keys if kind == WEEKLY else weekly_keys)))}")
        raw_half = data.get("half_day_offs", [])
        raw_rest = data.get("rest_days")
        if not isinstance(raw_half, list) or (raw_rest is not None and not isinstance(raw_rest, list)):
            raise WorkWeekError("INVALID_PATTERN", "half_day_offs and rest_days must be lists")
        if kind == CYCLE:
            return cls._cycle_from_json(data, raw_half, raw_rest)
        off = data.get("weekly_off_days", [])
        raw_rules = data.get("nth_weekday_offs", [])
        if not isinstance(off, list) or not isinstance(raw_rules, list):
            raise WorkWeekError("INVALID_PATTERN", "weekly_off_days and nth_weekday_offs must be lists")
        rules: list[tuple[int, frozenset[int]]] = []
        for item in raw_rules:
            if not isinstance(item, Mapping) or set(item) != {"weekday", "nth"} or not isinstance(item["nth"], list):
                raise WorkWeekError("INVALID_PATTERN", 'each nth-weekday rule is {"weekday": int, "nth": [int, ...]}')
            rules.append((_weekday(item["weekday"], "an nth-weekday rule's weekday"), _nth_list(item["nth"])))
        halves: list[tuple[int, str, Optional[frozenset[int]]]] = []
        for item in raw_half:
            if not isinstance(item, Mapping) or not {"weekday", "portion"} <= set(item) <= {"weekday", "portion", "nth"}:
                raise WorkWeekError("INVALID_PATTERN", 'each half-day off is {"weekday": int, "portion": str, "nth"?: [int]}')
            nth = None
            if "nth" in item:
                if not isinstance(item["nth"], list):
                    raise WorkWeekError("INVALID_PATTERN", "a half-day rule's nth must be a list")
                nth = _nth_list(item["nth"])
            halves.append((_weekday(item["weekday"], "a half-day off's weekday"), item["portion"], nth))
        days = [_weekday(day, "a weekly off day") for day in off]
        if len(set(days)) != len(days):
            raise WorkWeekError("INVALID_PATTERN", "weekly_off_days repeats a weekday")
        return cls(frozenset(days), tuple(rules), WEEKLY, tuple(halves),
                   rest_days=None if raw_rest is None else frozenset(_distinct_ints(raw_rest, "rest_days")))

    @classmethod
    def _cycle_from_json(cls, data: Mapping[str, Any], raw_half: list, raw_rest: Optional[list]) -> WorkWeekPattern:
        missing = {"cycle_length_days", "cycle_off_days"} - set(data)
        if missing:
            raise WorkWeekError("INVALID_CYCLE", f"a CYCLE pattern needs {', '.join(sorted(missing))}")
        anchor_raw = data.get("anchor_date")
        if anchor_raw in (None, ""):
            raise WorkWeekError("ANCHOR_REQUIRED", "a CYCLE pattern needs the anchor date of cycle day 0")
        try:
            anchor = anchor_raw if isinstance(anchor_raw, dt.date) else dt.date.fromisoformat(str(anchor_raw))
        except ValueError:
            raise WorkWeekError("INVALID_DATE", f"anchor_date must be YYYY-MM-DD, got {anchor_raw!r}") from None
        off = data["cycle_off_days"]
        if not isinstance(off, list):
            raise WorkWeekError("INVALID_PATTERN", "cycle_off_days must be a list")
        halves: list[tuple[int, str, None]] = []
        for item in raw_half:
            if not isinstance(item, Mapping) or set(item) != {"cycle_day", "portion"}:
                raise WorkWeekError("INVALID_PATTERN", 'each cycle half-day off is {"cycle_day": int, "portion": str}')
            halves.append((item["cycle_day"], item["portion"], None))
        return cls(kind=CYCLE, cycle_length_days=data["cycle_length_days"], anchor_date=anchor,
                   cycle_off_days=frozenset(_distinct_ints(off, "cycle_off_days")), half_day_offs=tuple(halves),
                   rest_days=None if raw_rest is None else frozenset(_distinct_ints(raw_rest, "rest_days")))

    # ------------------------------------------------------------------ helpers
    def content_hash(self) -> str:
        """sha256 of the canonical rules JSON (``work_week_pattern_versions.content_hash``)."""
        return rules_hash(self.to_json())

    @property
    def has_half_day_offs(self) -> bool:
        return bool(self.half_day_offs)

    def summary(self) -> str:
        """Short human description, e.g. "Sat, Sun off" or "14-day cycle, 3 days off"."""
        if self.kind == CYCLE:
            text = f"{self.cycle_length_days}-day cycle from {self.anchor_date.isoformat()}, {len(self.cycle_off_days)} off"
        else:
            parts = [", ".join(WEEKDAY_CODES[d].title() for d in sorted(self.weekly_off_days)) + " off"] \
                if self.weekly_off_days else ["No weekly offs"]
            for day, nths in self.nth_weekday_offs:
                parts.append(f"{WEEKDAY_CODES[day].title()} {'/'.join(_ordinal(n) for n in sorted(nths, key=_nth_sort))} off")
            text = "; ".join(parts)
        for day, portion, nths in self.half_day_offs:
            label = f"day {day + 1}" if self.kind == CYCLE else WEEKDAY_CODES[day].title()
            when = "" if nths is None else " " + "/".join(_ordinal(n) for n in sorted(nths, key=_nth_sort))
            text += f"; {label}{when} {'morning' if portion == FIRST_HALF else 'afternoon'} off"
        return text


def _nth_sort(n: int) -> int:
    return 6 if n == LAST else n


def _ordinal(n: int) -> str:
    return "last" if n == LAST else {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def _nth_list(values: list) -> frozenset[int]:
    nths = [_occurrence(n) for n in values]
    if len(set(nths)) != len(nths):
        raise WorkWeekError("INVALID_PATTERN", "a rule repeats an occurrence")
    return frozenset(nths)


def _distinct_ints(values: list, what: str) -> list[int]:
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        raise WorkWeekError("INVALID_PATTERN", f"{what} must list integers")
    if len(set(values)) != len(values):
        raise WorkWeekError("INVALID_PATTERN", f"{what} repeats a value")
    return list(values)


def rules_hash(rules: Mapping[str, Any]) -> str:
    """sha256 of canonical JSON (sorted keys, no whitespace), the same canonical form as day-fact hashes."""
    text = json.dumps(rules, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


# ---------------------------------------------------------------------------------------------- templates (§3.6)
@dataclass(frozen=True)
class Template:
    """A starter rule set offered by the "Start from template" picker; choosing one copies the rules."""

    code: str
    name: str
    kind: str
    typical_use: str
    # WEEKLY: the full rules. CYCLE: the rules without the anchor (``rules_for`` adds "next Monday").
    rules: Mapping[str, Any]
    # Half-day weekly offs are charged by payroll from LH-M5b (V-9); until then the template is shown, not savable.
    needs_half_day_offs: bool = False

    def rules_for(self, today: dt.date) -> dict[str, Any]:
        rules = dict(self.rules)
        if self.kind == CYCLE:
            rules["anchor_date"] = next_monday(today).isoformat()
        return WorkWeekPattern.from_json(rules).to_json()


def next_monday(day: dt.date) -> dt.date:
    """The first Monday on or after ``day`` (default cycle anchor)."""
    day = _require_date(day, "day")
    return day + dt.timedelta(days=(MONDAY - day.weekday()) % 7)


def _template(code: str, name: str, use: str, rules: Mapping[str, Any], *, half: bool = False) -> Template:
    kind = rules.get("kind", WEEKLY)
    probe = dict(rules)
    if kind == CYCLE:
        probe["anchor_date"] = "2027-01-04"
    WorkWeekPattern.from_json(probe)  # templates are valid by construction
    return Template(code, name, kind, use, MappingProxyType(dict(rules)), half)


TEMPLATES: Mapping[str, Template] = MappingProxyType({
    t.code: t
    for t in (
        _template("MON_FRI", "Monday to Friday", "Offices, IT", {"weekly_off_days": [5, 6], "nth_weekday_offs": []}),
        _template("MON_SAT", "Monday to Saturday", "Factories, shops (India)",
                  {"weekly_off_days": [6], "nth_weekday_offs": []}),
        _template("MON_SAT_2ND_4TH_SAT_OFF", "Monday to Saturday, 2nd and 4th Saturday off",
                  "Banks and many firms (India)",
                  {"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [2, 4]}]}),
        _template("MON_SAT_HALF_SAT", "Monday to Saturday, Saturday afternoon off", "SMEs, retail",
                  {"kind": WEEKLY, "weekly_off_days": [6], "nth_weekday_offs": [],
                   "half_day_offs": [{"weekday": 5, "portion": SECOND_HALF}]}, half=True),
        _template("SUN_THU", "Sunday to Thursday", "Saudi Arabia and other Gulf states",
                  {"weekly_off_days": [4, 5], "nth_weekday_offs": []}),
        _template("SAT_THU", "Saturday to Thursday", "Six-day Gulf week", {"weekly_off_days": [4], "nth_weekday_offs": []}),
        _template("FRI_HALF", "Monday to Friday, Friday afternoon off", "UAE government-style week",
                  {"kind": WEEKLY, "weekly_off_days": [5, 6], "nth_weekday_offs": [],
                   "half_day_offs": [{"weekday": 4, "portion": SECOND_HALF}]}, half=True),
        _template("MON_THU", "Monday to Thursday", "Four-day week (4x10)",
                  {"weekly_off_days": [4, 5, 6], "nth_weekday_offs": []}),
        _template("ALT_SAT_OFF", "Alternate Saturdays off", "Alternate Saturdays (14-day cycle)",
                  {"kind": CYCLE, "cycle_length_days": 14, "cycle_off_days": [6, 12, 13]}),
        _template("FOUR_ON_FOUR_OFF", "4 on, 4 off", "12-hour continuous operations",
                  {"kind": CYCLE, "cycle_length_days": 8, "cycle_off_days": [4, 5, 6, 7]}),
        _template("PANAMA_2_2_3", "2-2-3 (Panama)", "24x7 plants, security",
                  {"kind": CYCLE, "cycle_length_days": 14, "cycle_off_days": [2, 3, 7, 8, 11, 12, 13]}),
        _template("DUPONT", "DuPont (28 days)", "Continuous process",
                  {"kind": CYCLE, "cycle_length_days": 28,
                   "cycle_off_days": [4, 5, 6, 10, 14, 15, 16, 21, 22, 23, 24, 25, 26, 27]}),
    )
})


# ---------------------------------------------------------------------------------------------- evaluation
def occurrence_in_month(day: dt.date) -> int:
    """1 for the first such weekday of the month (days 1-7), 2 for days 8-14, ... up to 5."""
    return (_require_date(day, "day").day - 1) // 7 + 1


def is_last_occurrence_in_month(day: dt.date) -> bool:
    day = _require_date(day, "day")
    return (day + _ONE_WEEK).month != day.month


def _occurs(occurrences: frozenset[int], day: dt.date) -> bool:
    return occurrence_in_month(day) in occurrences or (LAST in occurrences and is_last_occurrence_in_month(day))


def cycle_day(pattern: WorkWeekPattern, day: dt.date) -> Optional[int]:
    """Position of ``day`` in a CYCLE pattern (0..N-1; dates before the anchor work too), None for WEEKLY."""
    day = _require_date(day, "day")
    if pattern.kind != CYCLE:
        return None
    return (day - pattern.anchor_date).days % pattern.cycle_length_days


def weekly_off_portion(pattern: WorkWeekPattern, day: dt.date) -> Optional[Portion]:
    """'FULL', 'FIRST_HALF' or 'SECOND_HALF' when ``day`` is (partly) a weekly off, None on a working day (§3.3)."""
    day = _require_date(day, "day")
    if pattern.kind == CYCLE:
        index = cycle_day(pattern, day)
        if index in pattern.cycle_off_days:
            return FULL
        for half_day, portion, _ in pattern.half_day_offs:
            if half_day == index:
                return portion  # type: ignore[return-value]
        return None
    weekday = day.weekday()
    if weekday in pattern.weekly_off_days:
        return FULL
    for rule_weekday, occurrences in pattern.nth_weekday_offs:
        if rule_weekday == weekday and _occurs(occurrences, day):
            return FULL
    for half_weekday, portion, occurrences in pattern.half_day_offs:
        if half_weekday == weekday and (occurrences is None or _occurs(occurrences, day)):
            return portion  # type: ignore[return-value]
    return None


def is_weekly_off(pattern: WorkWeekPattern, day: dt.date) -> bool:
    """The whole day is a weekly off (half-day offs are working days for this test)."""
    return weekly_off_portion(pattern, day) == FULL


def is_rest_day(pattern: WorkWeekPattern, day: dt.date) -> bool:
    """A full weekly off that is a rest day (LH-WWK-04): every full off unless ``rest_days`` narrows it."""
    if not is_weekly_off(pattern, day):
        return False
    if pattern.rest_days is None:
        return True
    key = cycle_day(pattern, day) if pattern.kind == CYCLE else day.weekday()
    return key in pattern.rest_days


def working_portion(weekly_off: Optional[str], holiday: Optional[str]) -> WorkingPortion:
    """What is left to work after a weekly-off portion and a holiday portion (SPINE §6.1 truth table, LH-WWK-14).

    FULL on either side leaves nothing; two different halves leave nothing; one half (or the same half twice)
    leaves the other half.
    """
    for value in (weekly_off, holiday):
        if value not in (None, FULL, FIRST_HALF, SECOND_HALF):
            raise WorkWeekError("INVALID_PORTION", f"unknown portion {value!r}")
    if FULL in (weekly_off, holiday):
        return NONE
    off = {p for p in (weekly_off, holiday) if p is not None}
    if not off:
        return FULL
    if off == {FIRST_HALF, SECOND_HALF}:
        return NONE
    return SECOND_HALF if off == {FIRST_HALF} else FIRST_HALF


def apply_declared_working_day(pattern: WorkWeekPattern, off: Optional[str], declared: Optional[str]) -> Optional[str]:
    """The weekly-off portion left after a declared (make-up) working day (SPINE §7 step 4; 04-holiday/03 §6.1).

    Declared working days apply to WEEKLY patterns only: a CYCLE keeps its offs (LH-WWK-10). A FULL declaration
    makes a full or half off fully working (LH-WWK-03 store staff); a half declaration works only that half.
    Removing the declaration (``declared`` None) restores the pattern's portion (LH-WWK-15).
    """
    for value in (off, declared):
        if value not in (None, FULL, FIRST_HALF, SECOND_HALF):
            raise WorkWeekError("INVALID_PORTION", f"unknown portion {value!r}")
    if declared is None or off is None or pattern.kind == CYCLE:
        return off
    if declared == FULL or declared == off:
        return None
    if off == FULL:
        return SECOND_HALF if declared == FIRST_HALF else FIRST_HALF
    return off  # a half off and the other half declared: nothing changes


def portion_units(portion: str) -> Decimal:
    """Day units of a working portion: FULL 1.00, a half 0.50, NONE 0."""
    return {FULL: Decimal("1.00"), FIRST_HALF: Decimal("0.50"), SECOND_HALF: Decimal("0.50"), NONE: Decimal("0")}[portion]


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


@dataclass(frozen=True)
class DayMark:
    """One date of a preview (04-holiday/03 §5.7; 09-ui-ux/02 §19 month grid)."""

    date: dt.date
    portion: Optional[str]  # FULL / FIRST_HALF / SECOND_HALF / None (working)
    rest_day: bool
    cycle_day: Optional[int]
    occurrence: int  # nth occurrence of the weekday in its month (1-5)

    def to_json(self) -> dict[str, Any]:
        return {"date": self.date.isoformat(), "weekday": self.date.weekday(), "portion": self.portion,
                "rest_day": self.rest_day, "cycle_day": self.cycle_day, "occurrence": self.occurrence}


def _check_range(start: dt.date, end: dt.date) -> None:
    _require_date(start, "start")
    _require_date(end, "end")
    if end < start:
        raise WorkWeekError("INVALID_RANGE", f"end {end.isoformat()} is before start {start.isoformat()}")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise WorkWeekError("RANGE_TOO_LONG", f"a range covers at most {MAX_RANGE_DAYS} days")


def day_marks(pattern: WorkWeekPattern, start: dt.date, end: dt.date) -> list[DayMark]:
    """Per-date weekly-off marks of one pattern (no holidays: they belong to calendars)."""
    _check_range(start, end)
    return [DayMark(day, weekly_off_portion(pattern, day), is_rest_day(pattern, day), cycle_day(pattern, day),
                    occurrence_in_month(day))
            for day in dates_between(start, end)]


# ---------------------------------------------------------------------------------------------- versions (§5.1)
@dataclass(frozen=True)
class PatternVersion:
    """One effective-dated version of a pattern: rules valid from ``effective_from`` to ``effective_to`` (inclusive)."""

    version: int
    effective_from: dt.date
    effective_to: Optional[dt.date]
    pattern: WorkWeekPattern

    def covers(self, day: dt.date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)


def version_on(versions: Sequence[PatternVersion], day: dt.date) -> Optional[PatternVersion]:
    """The version valid on ``day`` (every date resolves on its own, so a range may cross a boundary)."""
    _require_date(day, "day")
    for version in versions:
        if version.covers(day):
            return version
    return None


def check_tiling(versions: Sequence[PatternVersion]) -> None:
    """Versions never overlap and leave no gap from version 1 on; only the last one is open-ended."""
    ordered = sorted(versions, key=lambda v: v.effective_from)
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.effective_to is None or earlier.effective_to + _ONE_DAY != later.effective_from:
            raise WorkWeekError("INVALID_RANGE", f"version {earlier.version} and {later.version} do not tile")
    for version in ordered:
        if version.effective_to is not None and version.effective_to < version.effective_from:
            raise WorkWeekError("INVALID_RANGE", f"version {version.version} ends before it starts")


@dataclass(frozen=True)
class VersionPlan:
    """What posting a version effective ``effective_from`` does to the existing ones (§5.2 step 6)."""

    close: Optional[tuple[int, dt.date]]  # (version, new effective_to) of the version covering effective_from
    delete: tuple[int, ...]  # future versions starting on or after effective_from (they never applied)
    new_version: int
    new_effective_to: Optional[dt.date]


def plan_version(versions: Sequence[PatternVersion], effective_from: dt.date) -> VersionPlan:
    """Tile a new version into ``versions``; earlier dates keep their versions untouched (no history rewrite)."""
    _require_date(effective_from, "effective_from")
    covering = version_on(versions, effective_from)
    close = None
    if covering is not None and covering.effective_from < effective_from:
        close = (covering.version, effective_from - _ONE_DAY)
    delete = tuple(sorted(v.version for v in versions if v.effective_from >= effective_from))
    earliest = min((v.effective_from for v in versions), default=None)
    # A version posted before every existing one runs until the first of them starts (never overlaps).
    new_to = (earliest - _ONE_DAY) if earliest is not None and effective_from < earliest and not delete else None
    return VersionPlan(close, delete, max((v.version for v in versions), default=0) + 1, new_to)


def apply_plan(versions: Sequence[PatternVersion], plan: VersionPlan, effective_from: dt.date,
               pattern: WorkWeekPattern) -> list[PatternVersion]:
    """The versions after ``plan`` (pure mirror of what the service writes)."""
    out = []
    for version in versions:
        if version.version in plan.delete:
            continue
        if plan.close and version.version == plan.close[0]:
            version = PatternVersion(version.version, version.effective_from, plan.close[1], version.pattern)
        out.append(version)
    out.append(PatternVersion(plan.new_version, effective_from, plan.new_effective_to, pattern))
    out.sort(key=lambda v: v.effective_from)
    check_tiling(out)
    return out


def class_changes(before: WorkWeekPattern, after: WorkWeekPattern, start: dt.date, end: dt.date) -> dict[str, list[dt.date]]:
    """Dates whose weekly-off class differs between two rule sets (§5.7 ``date_changes``)."""
    _check_range(start, end)
    out: dict[str, list[dt.date]] = {"working_to_off": [], "off_to_working": [], "full_to_half": [], "half_change": []}
    for day in dates_between(start, end):
        a, b = weekly_off_portion(before, day), weekly_off_portion(after, day)
        if a == b:
            continue
        if a is None and b == FULL:
            out["working_to_off"].append(day)
        elif a == FULL and b is None:
            out["off_to_working"].append(day)
        elif a == FULL and b in HALF_PORTIONS:
            out["full_to_half"].append(day)
        else:
            out["half_change"].append(day)
    return out


# ---------------------------------------------------------------------------------------------- weekly rest (§6.3)
@dataclass(frozen=True)
class RestLimits:
    """Pack ``weekly_rest`` limits (appendix-a §2.6); None = not limited."""

    max_days_per_week: Optional[int] = None
    max_consecutive_days: Optional[int] = None
    verified: bool = False  # capability VERIFIED -> violations block (422), else warnings


@dataclass(frozen=True)
class RestViolation:
    rule: str  # "R-1" days per week, "R-2" consecutive days
    start: dt.date
    end: dt.date
    count: int
    limit: int
    code: str = "WEEKLY_REST_EXCEEDED"

    def message(self) -> str:
        if self.rule == "R-1":
            return (f"Employees would work {self.count} days in a week ({self.start.isoformat()} to "
                    f"{self.end.isoformat()}; limit {self.limit}).")
        return (f"Employees would work {self.count} days in a row ({self.start.isoformat()} to "
                f"{self.end.isoformat()}; limit {self.limit}).")

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "rule": self.rule, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "count": self.count, "limit": self.limit, "message": self.message()}


def rest_check(schedule: Sequence[tuple[dt.date, bool]], limits: RestLimits) -> list[RestViolation]:
    """R-1 and R-2 over consecutive dates; ``schedule`` = (date, works that day: a half day counts as work)."""
    days = sorted(schedule, key=lambda item: item[0])
    for (a, _), (b, _) in zip(days, days[1:]):
        if b - a != _ONE_DAY:
            raise WorkWeekError("INVALID_RANGE", "a rest check needs consecutive dates")
    found: list[RestViolation] = []
    week_limit = limits.max_days_per_week
    if week_limit is not None and len(days) >= 7:
        merged: Optional[list] = None
        for index in range(len(days) - 6):
            window = days[index:index + 7]
            count = sum(1 for _, works in window if works)
            if count > week_limit:
                if merged and window[0][0] <= merged[1]:
                    merged[1], merged[2] = window[-1][0], max(merged[2], count)
                else:
                    if merged:
                        found.append(RestViolation("R-1", merged[0], merged[1], merged[2], week_limit))
                    merged = [window[0][0], window[-1][0], count]
        if merged:
            found.append(RestViolation("R-1", merged[0], merged[1], merged[2], week_limit))
    run_limit = limits.max_consecutive_days
    if run_limit is not None:
        run: list[dt.date] = []
        for day, works in days + [(None, False)]:  # type: ignore[list-item]
            if works:
                run.append(day)
                continue
            if len(run) > run_limit:
                found.append(RestViolation("R-2", run[0], run[-1], len(run), run_limit))
            run = []
    return found


def junction_window(pattern: WorkWeekPattern, effective_from: dt.date, limits: RestLimits) -> tuple[dt.date, dt.date]:
    """[effective_from - L, effective_from + S + L] of the version-junction check (LH-WWK-17)."""
    lead = limits.max_consecutive_days or 7
    if pattern.kind == CYCLE:
        span = pattern.cycle_length_days
    elif pattern.nth_weekday_offs or pattern.half_day_offs:
        span = 365
    else:
        span = 7
    return effective_from - dt.timedelta(days=lead), effective_from + dt.timedelta(days=span + lead)


def junction_schedule(previous: Optional[WorkWeekPattern], new: WorkWeekPattern, effective_from: dt.date,
                      start: dt.date, end: dt.date) -> list[tuple[dt.date, bool]]:
    """Dates before ``effective_from`` from the previous version (if any), the rest from the new one."""
    out = []
    for day in dates_between(start, end):
        if day < effective_from and previous is None:
            continue
        rules = previous if day < effective_from else new
        out.append((day, weekly_off_portion(rules, day) != FULL))  # type: ignore[arg-type]
    return out


def check_version(previous: Optional[WorkWeekPattern], new: WorkWeekPattern, effective_from: dt.date,
                  limits: RestLimits) -> list[RestViolation]:
    """Weekly-rest check of a version post: the junction window (previous + new), or the new rules alone.

    The pattern check ignores holidays (Q-2). A cycle is checked over two joined cycles so a run that wraps around
    the end of the cycle is seen.
    """
    if limits.max_days_per_week is None and limits.max_consecutive_days is None:
        return []
    start, end = junction_window(new, effective_from, limits)
    if previous is None:
        start = effective_from
        if new.kind == CYCLE:
            end = max(end, effective_from + dt.timedelta(days=2 * new.cycle_length_days - 1))
    return rest_check(junction_schedule(previous, new, effective_from, start, end), limits)
