"""Rotation engine: templates, the token resolver, expansion, anchor alignment and fatigue lints (AS-M5, WI-AS-5.2).

Owner doc ``docs/attendance-shift/03-shifts/02-rosters-rotations-assignment.md``: model §2.1, structural
validation §2.2, the template catalogue §2.3 (22 entries, D-AS-18 as amended by SP-AS-218), copy on use §2.4,
``suggest_anchor`` §2.6, fatigue lints §2.7, resolver ``token_on`` §3.2, assignments §3.3, draft generation
§3.5 / §8, worked examples §14 and the golden vectors ``GV-AS-ROT-*`` of §15.

Pure (spine §1.4 L0, implementation guide IG-AS-02): no database, no clock, no settings reads. It imports only
the standard library and the pure :mod:`app.services.shift_engine` (time-zone arithmetic for publish lints).
:class:`RuleResult` is defined here (the lower layer) and re-exported by :mod:`app.services.roster_rules`.

Conventions (guide §2 C-6): dates are employee-local shift dates, instants are aware UTC datetimes, minutes are
whole minutes; an unknown value is ``None``.

Shift references. ``token_map`` maps a token to a shift reference (a shift code or id; the stored form
``{"shift_id": n}`` is accepted and read as ``n``) or to ``"OFF"``. Lints and ``suggest_anchor`` need the clock
times and minutes of each mapped shift: pass ``shifts = {shift_ref: ShiftTimes | net_minutes}``. For a pattern
copied from a template (``template_key`` set) whose shifts are not given, the template's research default times
(§2.3: ``D`` 07–19, ``N`` 19–07 for 12-h templates; ``M`` 06–14, ``A`` 14–22, ``N`` 22–06 for 8-h templates)
are used, which is what :meth:`RotationTemplate.pattern` sets up (each token mapped to itself).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections.abc import Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Any, Final

from app.services import shift_engine as se

# --------------------------------------------------------------------------- constants

WARN, REQUIRE_REASON, BLOCK = "WARN", "REQUIRE_REASON", "BLOCK"
SEVERITIES: Final = (WARN, REQUIRE_REASON, BLOCK)
SEVERITY_RANK: Final = MappingProxyType({WARN: 0, REQUIRE_REASON: 1, BLOCK: 2})

OFF = "OFF"
WORK = "WORK"
OFF_TOKEN = "O"  # reserved, always rostered OFF (§2.1)
TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
MAX_CYCLE_DAYS = 366

SOURCE_ROTATION = "ROTATION"
SOURCE_HOLIDAY_RULE = "HOLIDAY_RULE"
HOLIDAY_OFF, WORK_THROUGH, HOLIDAY_SKELETON = "HOLIDAY_OFF", "WORK_THROUGH", "HOLIDAY_SKELETON"
HOLIDAY_MODES: Final = (HOLIDAY_OFF, WORK_THROUGH, HOLIDAY_SKELETON)

# Structural validation (§2.2): 422 ROTATION_PATTERN_INVALID with details.rule, or its own code.
ROTATION_PATTERN_INVALID = "ROTATION_PATTERN_INVALID"
SHIFT_ARCHIVED = "SHIFT_ARCHIVED"
INVALID_FIELDS = "INVALID_FIELDS"
RULE_ROW_LENGTH = "ROW_LENGTH"
RULE_MODE = "MODE"
RULE_TOKEN_UNMAPPED = "TOKEN_UNMAPPED"
RULE_O_REMAPPED = "O_REMAPPED"
RULE_OFFSET_RANGE = "OFFSET_RANGE"
RULE_COVERAGE_MISMATCH = "COVERAGE_MISMATCH"

# Fatigue lints (§2.7) — WARN only, never BLOCK.
LINT_BACKWARD_ROTATION = "LINT_BACKWARD_ROTATION"
LINT_WEEKLY_ROTATION_SPEED = "LINT_WEEKLY_ROTATION_SPEED"
LINT_SHIFT_OVER_12H = "LINT_SHIFT_OVER_12H"
LINT_SAFETY_CRITICAL_OVER_8H = "LINT_SAFETY_CRITICAL_OVER_8H"
LINT_CONSECUTIVE_DAYS = "LINT_CONSECUTIVE_DAYS"
LINT_CONSECUTIVE_LONG_OR_NIGHT = "LINT_CONSECUTIVE_LONG_OR_NIGHT"
LINT_DAY_NIGHT_SWITCH_REST = "LINT_DAY_NIGHT_SWITCH_REST"
LINT_NO_FREE_WEEKEND = "LINT_NO_FREE_WEEKEND"
LINT_PERMANENT_NIGHTS = "LINT_PERMANENT_NIGHTS"
LINT_EARLY_START = "LINT_EARLY_START"
LINT_REST_BELOW_10H = "LINT_REST_BELOW_10H"
LINT_NIGHT_WORKER_AVERAGE = "LINT_NIGHT_WORKER_AVERAGE"
LINT_KEYS: Final = (
    LINT_BACKWARD_ROTATION, LINT_WEEKLY_ROTATION_SPEED, LINT_SHIFT_OVER_12H, LINT_SAFETY_CRITICAL_OVER_8H,
    LINT_CONSECUTIVE_DAYS, LINT_CONSECUTIVE_LONG_OR_NIGHT, LINT_DAY_NIGHT_SWITCH_REST, LINT_NO_FREE_WEEKEND,
    LINT_PERMANENT_NIGHTS, LINT_EARLY_START, LINT_REST_BELOW_10H, LINT_NIGHT_WORKER_AVERAGE,
)
# "pattern + publish" lints run again on each employee's resolved schedule; the others are cycle-level only.
PUBLISH_LINT_KEYS: Final = frozenset({
    LINT_BACKWARD_ROTATION, LINT_SHIFT_OVER_12H, LINT_SAFETY_CRITICAL_OVER_8H, LINT_CONSECUTIVE_DAYS,
    LINT_CONSECUTIVE_LONG_OR_NIGHT, LINT_DAY_NIGHT_SWITCH_REST, LINT_EARLY_START, LINT_REST_BELOW_10H,
    LINT_NIGHT_WORKER_AVERAGE,
})
# For a long-duty (24-h) shift these two fire by design and are shown, never muted (§2.3 note on KELLY_24_48).
UNMUTABLE_FOR_LONG_DUTY: Final = frozenset({LINT_SHIFT_OVER_12H, LINT_CONSECUTIVE_LONG_OR_NIGHT})

_LINT_SOURCE = {
    LINT_BACKWARD_ROTATION: "HSE HSG256 Table 11 G1",
    LINT_WEEKLY_ROTATION_SPEED: "HSE HSG256 Table 11 G2",
    LINT_SHIFT_OVER_12H: "HSE HSG256 Table 11 G3",
    LINT_SAFETY_CRITICAL_OVER_8H: "HSE HSG256 Table 11 G3",
    LINT_CONSECUTIVE_DAYS: "HSE HSG256 Table 11 G4",
    LINT_CONSECUTIVE_LONG_OR_NIGHT: "HSE HSG256 Table 11 G4",
    LINT_DAY_NIGHT_SWITCH_REST: "HSE HSG256 Table 11 G5",
    LINT_NO_FREE_WEEKEND: "HSE HSG256 Table 11 G6",
    LINT_PERMANENT_NIGHTS: "HSE HSG256 Table 11 G6",
    LINT_EARLY_START: "HSE HSG256 Table 11 G6",
    LINT_REST_BELOW_10H: "HSE HSG256 Table 11 G8 (NIOSH summary)",
    LINT_NIGHT_WORKER_AVERAGE: "pack night_work.night_worker",
}

# Lint thresholds (§2.7 design values derived from the HSE wording).
LINT_SPEED_MIN_RUN, LINT_SPEED_MAX_RUN = 5, 14
LINT_MAX_SHIFT_MINUTES = 720
LINT_SAFETY_CRITICAL_MINUTES = 480
LINT_MAX_CONSECUTIVE_DAYS = 7
LINT_LONG_MINUTES = 480
LINT_MAX_LONG_OR_NIGHT_RUN = 3
LINT_MIN_FREE_NIGHTS = 2
LINT_EARLY_START_MINUTES = 7 * 60
LINT_MIN_REST_MINUTES = 10 * 60
DEFAULT_NIGHT_WINDOW: Final = ("22:00", "06:00")  # attendance.night_window default
DEFAULT_NIGHT_THRESHOLD_HOURS = Decimal(3)  # night_work.night_worker.threshold_hours_in_window when the pack is silent

WEEKDAY_CODES: Final = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_SAT, _SUN = 5, 6
_HOURS_2DP = Decimal("0.01")
_ONE_DAY = dt.timedelta(days=1)
_SYNTHETIC_MONDAY = dt.date(2001, 1, 1)  # a Monday: index 0 of a pattern-level expansion


class RotationEngineError(ValueError):
    """Invalid input to the engine (programming error of the caller, not a validation result)."""

    def __init__(self, code: str, message: str, field_name: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field_name


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class RuleResult:
    """One publish-rule or lint result (spine §5.4; owner doc §6.1).

    ``details`` is an extension for pattern-level results (``crew_code``, cycle ``indices``) and rule specifics;
    it never carries reasons or leave types.
    """

    rule_key: str
    severity: str
    employee_id: int | None
    dates: tuple[dt.date, ...] = ()
    measured: Any = None
    limit: Any = None
    unit: str | None = None
    source_ref: str | None = None
    message_key: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def to_dict(self) -> dict[str, Any]:
        def plain(value: Any) -> Any:
            return str(value) if isinstance(value, Decimal) else value

        return {
            "rule_key": self.rule_key,
            "severity": self.severity,
            "employee_id": self.employee_id,
            "dates": [d.isoformat() for d in self.dates],
            "measured": plain(self.measured),
            "limit": plain(self.limit),
            "unit": self.unit,
            "source_ref": self.source_ref,
            "message_key": self.message_key,
            "details": dict(self.details),
        }


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class ShiftTimes:
    """What the engine needs of a mapped shift: clock times, minutes and flags (from ``shift_versions``).

    ``end <= start`` crosses midnight; ``end == start`` is a 24-h duty (``long_duty``, §2.3 #22).
    ``net_minutes`` defaults to the gross span (``shift_engine.scheduled_net_minutes`` in real use).
    """

    start: str
    end: str
    net_minutes: int | None = None
    safety_critical: bool = False
    long_duty: bool = False
    archived: bool = False

    @property
    def start_minutes(self) -> int:
        return se._clock(self.start, "start")

    @property
    def gross_minutes(self) -> int:
        start, end = self.start_minutes, se._clock(self.end, "end")
        return end - start if end > start else end - start + se.DAY_MINUTES

    @property
    def net(self) -> int:
        return self.gross_minutes if self.net_minutes is None else self.net_minutes


@dataclass(frozen=True)
class PatternVersionData:
    """The business columns of one ``rotation_pattern_versions`` row (§2.1).

    Exactly one of ``tokens`` (base row + ``crew_offsets``) or ``crew_rows`` (``{crew_code: tokens}``).
    ``token_map``: ``{token: shift_ref | {"shift_id": n} | "OFF"}``; ``O`` is always OFF and may not be mapped.
    ``coverage``: ``{token: crews}`` held on every cycle index. ``week_anchor_weekday``: 0 = Monday.
    """

    cycle_days: int
    tokens: str | None = None
    crew_rows: Mapping[str, str] | None = None
    crew_offsets: Mapping[str, int] = field(default_factory=dict)
    token_map: Mapping[str, Any] = field(default_factory=dict)
    coverage: Mapping[str, int] = field(default_factory=dict)
    week_anchor_weekday: int | None = None
    lints_muted: frozenset[str] = frozenset()
    template_key: str | None = None
    code: str | None = None
    version_no: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lints_muted", frozenset(self.lints_muted or ()))
        if self.crew_rows is not None:
            object.__setattr__(self, "crew_rows", dict(self.crew_rows))
        object.__setattr__(self, "crew_offsets", dict(self.crew_offsets or {}))
        object.__setattr__(self, "token_map", dict(self.token_map or {}))
        object.__setattr__(self, "coverage", dict(self.coverage or {}))

    @property
    def crews(self) -> tuple[str | None, ...]:
        """Crew codes in declaration order; ``(None,)`` for a single-row pattern without crews."""
        if self.crew_rows:
            return tuple(self.crew_rows)
        if self.crew_offsets:
            return tuple(self.crew_offsets)
        return (None,)


@dataclass(frozen=True)
class AssignmentData:
    """The resolver columns of one ``rotation_assignments`` row (§3.3)."""

    anchor_date: dt.date
    offset_days: int = 0
    crew_code: str | None = None
    effective_from: dt.date | None = None
    effective_to: dt.date | None = None


@dataclass(frozen=True)
class PlannedDay:
    """One generated (employee, date) cell (§3.5 steps 2–3)."""

    date: dt.date
    index: int  # cycle index
    token: str
    kind: str  # WORK | OFF
    shift: Hashable | None  # mapped shift reference, None when OFF
    source: str = SOURCE_ROTATION  # ROTATION | HOLIDAY_RULE
    planned_shift: Hashable | None = None  # HOLIDAY_RULE: the shift of the token it replaced (§8)


@dataclass(frozen=True)
class PatternIssue:
    """A structural validation failure (§2.2). ``code`` is the API error; ``rule`` is ``details.rule``."""

    code: str
    rule: str | None
    message: str
    details: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class AnchorSuggestion:
    """Output of :func:`suggest_anchor` (§2.6 step 4/5). Minutes are scheduled net minutes."""

    k: int | None
    anchor_weekday: int | None
    worst_week_minutes: int
    worst_week_count: int | None  # crew-weeks at the worst value for the chosen k
    table: Mapping[int, int]  # k -> worst fixed-week minutes
    counts: Mapping[int, int]  # k -> crew-weeks at that k's worst value
    min_week_minutes: int  # lowest fixed-week minutes (alignment-free range "between x and y")

    @property
    def anchor_weekday_code(self) -> str | None:
        return None if self.anchor_weekday is None else WEEKDAY_CODES[self.anchor_weekday]


@dataclass(frozen=True)
class CycleStats:
    """Per-pattern figures of the §2.3 table (worst over crews; ``minutes_basis`` gross or net)."""

    avg_week_minutes: Decimal  # minutes per 7 days averaged over the cycle
    max_run: int  # longest run of consecutive work dates (wrapped); cycle_days * 2 when a row has no OFF
    max_night_run: int  # longest run of consecutive night instances (dates)
    worst_7d_days: int  # most work dates in any 7 consecutive dates (wrapped)
    worst_7d_minutes: int
    coverage_by_index: tuple[Mapping[str, int], ...]  # token -> crews holding it, per cycle index


@dataclass(frozen=True)
class NightWorkerRule:
    """Pack ``night_work.night_worker`` (C-35) for ``LINT_NIGHT_WORKER_AVERAGE``."""

    threshold_hours_in_window: Decimal
    average_max_hours: Decimal
    reference_weeks: int


@dataclass(frozen=True)
class LintInstance:
    """A work instance as the lint core sees it (pattern-level synthetic or a published instance)."""

    shift_date: dt.date
    start_at: dt.datetime
    end_at: dt.datetime
    type_ref: Hashable  # the mapped shift (type) — backward rotation and rotation speed
    type_start_minutes: int  # local clock start of the type, orders M -> A -> N
    local_start_minutes: int  # local clock start of this instance (early start)
    net_minutes: int
    night: bool
    safety_critical: bool = False
    long_duty: bool = False
    index: int | None = None  # cycle index (pattern level)

    @property
    def gross_minutes(self) -> int:
        return int((self.end_at - self.start_at).total_seconds()) // 60


# --------------------------------------------------------------------------- templates (§2.3)


def _shift(start: str, end: str, net: int | None = None, *, long_duty: bool = False) -> ShiftTimes:
    return ShiftTimes(start=start, end=end, net_minutes=net, long_duty=long_duty)


_TIMES_12H = MappingProxyType({"D": _shift("07:00", "19:00"), "N": _shift("19:00", "07:00")})
_TIMES_8H = MappingProxyType({"M": _shift("06:00", "14:00"), "A": _shift("14:00", "22:00"), "N": _shift("22:00", "06:00")})
_TIMES_10H = MappingProxyType({"D": _shift("08:00", "18:00")})  # 4x10 daytime (illustrative clock)
_TIMES_GENERAL = MappingProxyType({"G": _shift("09:00", "18:00", 480)})  # 9 h gross, 1 h unpaid lunch, 8 h net
_TIMES_W = MappingProxyType({"W": _shift("09:00", "18:00", 480)})  # per mapped shift; illustrative
_TIMES_KELLY = MappingProxyType({"K": _shift("08:00", "08:00", long_duty=True)})  # 24-h duty, 08:00 -> 08:00


@dataclass(frozen=True)
class RotationTemplate:
    """One platform template (§2.3). ``expected_*`` are the table's figures (checked by the tests)."""

    number: int
    key: str
    name: str
    cycle_days: int
    tokens: str | None
    crew_rows: Mapping[str, str] | None
    crew_offsets: Mapping[str, int]
    coverage: Mapping[str, int]
    shift_hours: Decimal | None
    default_shifts: Mapping[str, ShiftTimes]
    week_anchor_weekday: int | None = None
    long_duty_tokens: frozenset[str] = frozenset()
    expected_avg_week_hours: Decimal | None = None
    expected_max_run: int | None = None
    expected_max_night_run: int | None = None
    expected_worst_7d_days: int | None = None
    expected_worst_7d_hours: Decimal | None = None
    note: str = ""

    @property
    def crews(self) -> tuple[str, ...]:
        return tuple(self.crew_rows) if self.crew_rows else tuple(self.crew_offsets)

    @property
    def work_tokens(self) -> tuple[str, ...]:
        rows = self.crew_rows.values() if self.crew_rows else (self.tokens or "",)
        return tuple(sorted({t for row in rows for t in row if t != OFF_TOKEN}))

    def pattern(self, token_map: Mapping[str, Any] | None = None, **overrides: Any) -> PatternVersionData:
        """Copy on use (§2.4). Without ``token_map`` each work token is mapped to itself, so the template's
        ``default_shifts`` serve as ``shifts`` for lints and ``suggest_anchor``."""
        values: dict[str, Any] = dict(
            cycle_days=self.cycle_days,
            tokens=self.tokens,
            crew_rows=dict(self.crew_rows) if self.crew_rows else None,
            crew_offsets=dict(self.crew_offsets),
            token_map=dict(token_map) if token_map is not None else {t: t for t in self.work_tokens},
            coverage=dict(self.coverage),
            week_anchor_weekday=self.week_anchor_weekday,
            template_key=self.key,
        )
        values.update(overrides)
        return PatternVersionData(**values)


def _offsets(**offsets: int) -> Mapping[str, int]:
    return MappingProxyType(dict(offsets))


def _staggered_rows() -> Mapping[str, str]:
    """``ABC_WEEKLY_STAGGERED_OFF`` crew rows from its two layers (§2.3 #17).

    Shift layer ``[M, A, N]`` per week with crew week offsets A0 B2 C1 (week w of crew c works
    ``layer[(w + offset_c) mod 3]``); off layer of 7 slots with base Sunday (slot s is OFF on weekday
    ``(SUN + s) mod 7``), anchor weekday MON so cycle index i falls on weekday ``i mod 7``. Row codes are
    ``<crew>-<off weekday>``, e.g. ``A-SUN`` (21 rows: 3 crews x 7 off slots; relievers fill the off days).
    """
    layer, week_offsets = "MAN", {"A": 0, "B": 2, "C": 1}
    rows: dict[str, str] = {}
    for crew, crew_offset in week_offsets.items():
        for slot in range(7):
            off_weekday = (_SUN + slot) % 7
            rows[f"{crew}-{WEEKDAY_CODES[off_weekday]}"] = "".join(
                OFF_TOKEN if i % 7 == off_weekday else layer[(i // 7 + crew_offset) % 3] for i in range(21))
    return MappingProxyType(rows)


def _template(number: int, key: str, name: str, cycle_days: int, *, tokens: str | None = None,
              rows: Mapping[str, str] | None = None, offsets: Mapping[str, int] | None = None,
              coverage: Mapping[str, int] | None = None, hours: str | None, shifts: Mapping[str, ShiftTimes],
              expect: tuple[str | None, int, int, int, str], anchor: int | None = None,
              long_duty_tokens: Iterable[str] = (), note: str = "") -> RotationTemplate:
    avg, max_run, night_run, worst_days, worst_hours = expect
    return RotationTemplate(
        number=number, key=key, name=name, cycle_days=cycle_days, tokens=tokens,
        crew_rows=MappingProxyType(dict(rows)) if rows is not None else None,
        crew_offsets=offsets if offsets is not None else MappingProxyType({}),
        coverage=MappingProxyType(dict(coverage or {})),
        shift_hours=Decimal(hours) if hours is not None else None,
        default_shifts=shifts, week_anchor_weekday=anchor, long_duty_tokens=frozenset(long_duty_tokens),
        expected_avg_week_hours=Decimal(avg) if avg is not None else None, expected_max_run=max_run,
        expected_max_night_run=night_run, expected_worst_7d_days=worst_days,
        expected_worst_7d_hours=Decimal(worst_hours), note=note,
    )


_DN = {"D": 1, "N": 1}
_MAN = {"M": 1, "A": 1, "N": 1}

_TEMPLATE_LIST: Final = (
    _template(1, "PANAMA_223_ROT", "2-2-3 Panama, rotating day/night", 28, tokens="DDOODDDOODDOOONNOONNNOONNOOO",
              offsets=_offsets(A=0, B=7, C=14, D=21), coverage=_DN, hours="12", shifts=_TIMES_12H,
              expect=("42", 3, 3, 5, "60")),
    _template(2, "PITMAN_FIXED", "Pitman, fixed 2-2-3", 14,
              rows={"A": "DDOODDDOODDOOO", "B": "OODDOOODDOODDD", "C": "NNOONNNOONNOOO", "D": "OONNOOONNOONNN"},
              coverage=_DN, hours="12", shifts=_TIMES_12H, expect=("42", 3, 3, 5, "60")),
    _template(3, "DUPONT", "DuPont 4-week", 28, tokens="NNNNOOODDDONNNOOODDDDOOOOOOO",
              offsets=_offsets(A=0, B=7, C=14, D=21), coverage=_DN, hours="12", shifts=_TIMES_12H,
              expect=("42", 4, 4, 6, "72")),
    _template(4, "FOUR_ON_FOUR_OFF_ROT", "4-on-4-off rotating", 16, tokens="DDDDOOOONNNNOOOO",
              offsets=_offsets(A=0, B=4, C=8, D=12), coverage=_DN, hours="12", shifts=_TIMES_12H,
              expect=("42", 4, 4, 4, "48")),
    _template(5, "FOUR_ON_FOUR_OFF_FIXED", "4-on-4-off fixed day/night", 8,
              rows={"A": "DDDDOOOO", "B": "OOOODDDD", "C": "NNNNOOOO", "D": "OOOONNNN"},
              coverage=_DN, hours="12", shifts=_TIMES_12H, expect=("42", 4, 4, 4, "48")),
    _template(6, "DDNNOOOO_4C", "2 days, 2 nights, 4 off", 8, tokens="DDNNOOOO",
              offsets=_offsets(A=0, B=2, C=4, D=6), coverage=_DN, hours="12", shifts=_TIMES_12H,
              expect=("42", 4, 2, 4, "48")),
    _template(7, "SEVEN_ON_SEVEN_OFF", "7-on-7-off fixed", 14,
              rows={"A": "DDDDDDDOOOOOOO", "B": "OOOOOOODDDDDDD", "C": "NNNNNNNOOOOOOO", "D": "OOOOOOONNNNNNN"},
              coverage=_DN, hours="12", shifts=_TIMES_12H, expect=("42", 7, 7, 7, "84")),
    _template(8, "FOUR_THREE_ALT_12H", "4-on-3-off / 3-on-4-off alternating", 14,
              rows={"A": "DDDDOOODDDOOOO", "B": "OOOODDDOOODDDD", "C": "NNNNOOONNNOOOO", "D": "OOOONNNOOONNNN"},
              coverage=_DN, hours="12", shifts=_TIMES_12H, expect=("42", 4, 4, 4, "48")),
    _template(9, "DDNNOO_3C", "3-crew 12 h (2-2-2)", 6, tokens="DDNNOO", offsets=_offsets(A=0, B=2, C=4),
              coverage=_DN, hours="12", shifts=_TIMES_12H, expect=("56", 4, 2, 5, "60")),
    _template(10, "FIVE_CREW_12H", "5-crew 12 h (2-2-6)", 10, tokens="DDNNOOOOOO",
              offsets=_offsets(A=0, B=2, C=4, D=6, E=8), coverage=_DN, hours="12", shifts=_TIMES_12H,
              expect=("33.6", 4, 2, 4, "48")),
    _template(11, "CONTINENTAL_28", "Continental, 8 h (2-2-3 style)", 28, tokens="MMAANNNOOMMAAANNOOMMMAANNOOO",
              offsets=_offsets(A=0, B=21, C=14, D=7), coverage=_MAN, hours="8", shifts=_TIMES_8H,
              expect=("42", 7, 3, 7, "56")),
    _template(12, "CONTINENTAL_222_8D", "Fast continental 2-2-2", 8, tokens="MMAANNOO",
              offsets=_offsets(A=0, B=2, C=4, D=6), coverage=_MAN, hours="8", shifts=_TIMES_8H,
              expect=("42", 6, 2, 6, "48")),
    _template(13, "SIX_ON_TWO_OFF_SLOW", "6-on-2-off block rotation", 24, tokens="MMMMMMOOAAAAAAOONNNNNNOO",
              offsets=_offsets(A=0, B=6, C=12, D=18), coverage=_MAN, hours="8", shifts=_TIMES_8H,
              expect=("42", 6, 6, 6, "48")),
    _template(14, "SOUTHERN_SWING", "Southern swing (weekly)", 28, tokens="MMMMMMMOOAAAAAAAOONNNNNNNOOO",
              offsets=_offsets(A=0, B=21, C=14, D=7), coverage=_MAN, hours="8", shifts=_TIMES_8H,
              expect=("42", 7, 7, 7, "56")),
    _template(15, "FIVE_CREW_8H", "5-crew 8 h (2-2-2-4)", 10, tokens="MMAANNOOOO",
              offsets=_offsets(A=0, B=2, C=4, D=6, E=8), coverage=_MAN, hours="8", shifts=_TIMES_8H,
              expect=("33.6", 6, 2, 6, "48")),
    _template(16, "ABC_WEEKLY_24X6", "A/B/C weekly, common weekly off (24x6)", 21, tokens="MMMMMMOAAAAAAONNNNNNO",
              offsets=_offsets(A=0, B=14, C=7), hours="8", shifts=_TIMES_8H, anchor=0,
              expect=("48", 6, 6, 6, "48"), note="common weekly off: no constant per-index coverage"),
    _template(17, "ABC_WEEKLY_STAGGERED_OFF", "A/B/C weekly, staggered weekly off + relievers (24x7)", 21,
              rows=_staggered_rows(), hours="8", shifts=_TIMES_8H, anchor=0, expect=("48", 6, 6, 6, "48"),
              note="relievers sized headcount x 7/6; an off day that moves later week-to-week allows 12 consecutive "
                   "days (WEEKLY_REST_EXCEEDED against IN's 10)"),
    _template(18, "FOUR_ON_THREE_OFF_10H", "4x10 compressed week, two squads (daytime)", 7,
              rows={"A": "DDDDOOO", "B": "OOODDDD"}, hours="10", shifts=_TIMES_10H, expect=("40", 4, 0, 4, "40"),
              note="declares no coverage: index 3 holds both squads"),
    _template(19, "FOUR_ON_THREE_OFF_12H_IN", "4x12 compressed week (India 48-h option)", 7, tokens="DDDDOOO",
              offsets=_offsets(A=0), hours="12", shifts=MappingProxyType({"D": _TIMES_12H["D"]}),
              expect=("48", 4, 0, 4, "48"), note="needs compressed_week_allowed + COMPRESSED_WEEK consent"),
    _template(20, "GENERAL_MON_SAT", "General shift, Sunday off", 7, tokens="GGGGGGO", offsets=_offsets(A=0),
              hours="9", shifts=_TIMES_GENERAL, anchor=0, expect=("54", 6, 0, 6, "54"),
              note="54 gross / 48 net hours a week"),
    _template(21, "FIVE_TWO_ROTATING_OFF", "5 + 2 rotating weekly offs (BPO)", 7, tokens="WWWWWOO",
              offsets=_offsets(A=0, B=1, C=2, D=3, E=4, F=5, G=6), coverage={"W": 5}, hours=None, shifts=_TIMES_W,
              expect=(None, 5, 0, 5, "45"), note="one token W mapped per crew to its shift; re-anchor monthly"),
    _template(22, "KELLY_24_48", "Kelly 24/48 (24 h on, 48 h off; SP-AS-218)", 3, tokens="KOO",
              offsets=_offsets(A=0, B=1, C=2), coverage={"K": 1}, hours="24", shifts=_TIMES_KELLY,
              long_duty_tokens=("K",), expect=("56", 1, 1, 3, "72"),
              note="K maps to a long_duty shift; LINT_SHIFT_OVER_12H and LINT_CONSECUTIVE_LONG_OR_NIGHT by design"),
)

TEMPLATES: Final[Mapping[str, RotationTemplate]] = MappingProxyType({t.key: t for t in _TEMPLATE_LIST})
ROTATION_TEMPLATES: Final = TEMPLATES  # the spec's constant name (§2.3)


# --------------------------------------------------------------------------- helpers


def _ref(value: Any) -> Any:
    """Normalise a ``token_map`` value: ``{"shift_id": n}`` -> ``n``; ``"OFF"`` and refs unchanged."""
    if isinstance(value, Mapping):
        if "shift_id" not in value:
            raise RotationEngineError("INVALID_TOKEN_MAP", f"token_map value {value!r} has no shift_id", "token_map")
        return value["shift_id"]
    return value


def _require_date(value: object, what: str) -> dt.date:
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise RotationEngineError("INVALID_DATE", f"{what} must be a date, got {type(value).__name__}", what)
    return value


def _days(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    day = start
    while day <= end:
        yield day
        day += _ONE_DAY


def _row(pattern: PatternVersionData, crew_code: str | None) -> str:
    if pattern.crew_rows:
        if crew_code not in pattern.crew_rows:
            raise RotationEngineError("UNKNOWN_CREW", f"crew {crew_code!r} has no row in the pattern", "crew_code")
        return pattern.crew_rows[crew_code]
    if pattern.tokens is None:
        raise RotationEngineError("INVALID_PATTERN", "pattern has neither tokens nor crew_rows", "tokens")
    return pattern.tokens


def _crew_offset(pattern: PatternVersionData, crew_code: str | None) -> int:
    if pattern.crew_rows or crew_code is None:
        return 0
    if crew_code not in pattern.crew_offsets:
        raise RotationEngineError("UNKNOWN_CREW", f"crew {crew_code!r} is not in crew_offsets", "crew_code")
    return pattern.crew_offsets[crew_code]


def crew_sequence(pattern: PatternVersionData, crew_code: str | None) -> str:
    """The L tokens crew ``crew_code`` holds on cycle indices 0..L-1 (§2.1 table: rows, or the base row
    shifted by the crew offset)."""
    row, offset, length = _row(pattern, crew_code), _crew_offset(pattern, crew_code), pattern.cycle_days
    return "".join(row[(i + offset) % length] for i in range(length))


def shift_for_token(pattern: PatternVersionData, token: str) -> Any | None:
    """The mapped shift reference of ``token``, or ``None`` when it is rostered OFF (§3.2 ``result``)."""
    if token == OFF_TOKEN:
        return None
    if token not in pattern.token_map:
        raise RotationEngineError("TOKEN_UNMAPPED", f"token {token!r} is not in token_map", "token_map")
    ref = _ref(pattern.token_map[token])
    return None if ref == OFF else ref


def _shift_catalogue(pattern: PatternVersionData,
                     shifts: Mapping[Any, ShiftTimes | int] | None) -> Mapping[Any, ShiftTimes | int]:
    if shifts is not None:
        return shifts
    template = TEMPLATES.get(pattern.template_key or "")
    if template is None:
        raise RotationEngineError("SHIFTS_REQUIRED", "pass shifts={shift_ref: ShiftTimes | net minutes}", "shifts")
    # Template defaults are keyed by token; resolve through the pattern's token_map.
    out: dict[Any, ShiftTimes] = {}
    for token, value in pattern.token_map.items():
        ref = _ref(value)
        if ref != OFF and token in template.default_shifts:
            out[ref] = template.default_shifts[token]
    return out


def _net(shifts: Mapping[Any, ShiftTimes | int], ref: Any) -> int:
    if ref not in shifts:
        raise RotationEngineError("SHIFT_UNKNOWN", f"no minutes for mapped shift {ref!r}", "shifts")
    value = shifts[ref]
    return value.net if isinstance(value, ShiftTimes) else int(value)


def _times(shifts: Mapping[Any, ShiftTimes | int], ref: Any) -> ShiftTimes:
    value = shifts.get(ref)
    if not isinstance(value, ShiftTimes):
        raise RotationEngineError("SHIFT_TIMES_REQUIRED", f"lints need ShiftTimes for mapped shift {ref!r}", "shifts")
    return value


def _hours(minutes: int | Decimal) -> Decimal:
    return (Decimal(minutes) / 60).quantize(_HOURS_2DP, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- resolver (§3.2) and expansion (§3.5)


def token_on(pattern: PatternVersionData, anchor_date: dt.date, offset_days: int, d: dt.date,
             crew_code: str | None = None) -> str:
    """The token on shift date ``d``: ``idx = ((d - anchor).days + offset_days) mod L`` (floor modulo, valid
    before the anchor, GV-AS-ROT-04); ``crew_rows[crew_code][idx]`` in row mode, else ``tokens[idx]``."""
    return _row(pattern, crew_code)[cycle_index(pattern, anchor_date, offset_days, d)]


def cycle_index(pattern: PatternVersionData, anchor_date: dt.date, offset_days: int, d: dt.date) -> int:
    _require_date(anchor_date, "anchor_date")
    _require_date(d, "d")
    if pattern.cycle_days < 1:
        raise RotationEngineError("INVALID_PATTERN", "cycle_days must be >= 1", "cycle_days")
    return ((d - anchor_date).days + offset_days) % pattern.cycle_days  # Python % is floor modulo


def assignment_for_crew(pattern: PatternVersionData, crew_code: str, anchor_date: dt.date,
                        effective_from: dt.date | None = None,
                        effective_to: dt.date | None = None) -> AssignmentData:
    """A TEAM assignment: ``offset_days`` copied from ``crew_offsets[crew_code]`` (0 in row mode) (§3.2, §3.3)."""
    if pattern.crew_rows:
        _row(pattern, crew_code)
        offset = 0
    else:
        offset = _crew_offset(pattern, crew_code)
    return AssignmentData(anchor_date, offset, crew_code, effective_from, effective_to)


def offset_for_index(pattern: PatternVersionData, anchor_date: dt.date, effective_from: dt.date,
                     desired_index: int) -> int:
    """EMPLOYEE assignment: ``(desired_index - (effective_from - anchor_date).days) mod L`` (§3.3, GV-AS-ROT-05)."""
    return (desired_index - (effective_from - anchor_date).days) % pattern.cycle_days


def expand(pattern: PatternVersionData, assignment: AssignmentData, start: dt.date, end: dt.date, *,
           holidays: Collection[dt.date] = (), holiday_mode: str = HOLIDAY_OFF) -> list[PlannedDay]:
    """One :class:`PlannedDay` per date of ``[start, end]`` inside the assignment's effective dates.

    Calendar-anchored, never pauses (M-3). Holiday mode (§3.5 step 3, §8): under ``HOLIDAY_OFF`` a WORK token
    on a date in ``holidays`` (full non-working holidays of the employee) becomes OFF with source
    ``HOLIDAY_RULE`` and ``planned_shift`` = the replaced shift; ``WORK_THROUGH`` keeps it; ``HOLIDAY_SKELETON``
    also keeps it here — the skeleton choice is cross-employee and belongs to ``generate_drafts``.
    """
    _require_date(start, "start")
    _require_date(end, "end")
    if holiday_mode not in HOLIDAY_MODES:
        raise RotationEngineError("INVALID_HOLIDAY_MODE", f"holiday_mode must be one of {HOLIDAY_MODES}", "holiday_mode")
    lo = max(start, assignment.effective_from) if assignment.effective_from else start
    hi = min(end, assignment.effective_to) if assignment.effective_to else end
    row = _row(pattern, assignment.crew_code if pattern.crew_rows else None)
    holiday_set = frozenset(holidays)
    out: list[PlannedDay] = []
    for day in _days(lo, hi):
        idx = cycle_index(pattern, assignment.anchor_date, assignment.offset_days, day)
        token = row[idx]
        ref = shift_for_token(pattern, token)
        if ref is None:
            out.append(PlannedDay(day, idx, token, OFF, None))
        elif day in holiday_set and holiday_mode == HOLIDAY_OFF:
            out.append(PlannedDay(day, idx, token, OFF, None, SOURCE_HOLIDAY_RULE, ref))
        else:
            out.append(PlannedDay(day, idx, token, WORK, ref))
    return out


def content_hash(pattern: PatternVersionData) -> str:
    """SHA-256 of the canonical JSON of the hashed version columns (§2.1)."""
    payload = {
        "cycle_days": pattern.cycle_days,
        "tokens": pattern.tokens,
        "crew_rows": dict(pattern.crew_rows) if pattern.crew_rows is not None else None,
        "crew_offsets": dict(pattern.crew_offsets),
        "token_map": dict(pattern.token_map),
        "coverage": dict(pattern.coverage),
        "week_anchor_weekday": pattern.week_anchor_weekday,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- structural validation (§2.2)


def coverage_by_index(pattern: PatternVersionData) -> tuple[dict[str, int], ...]:
    """Per cycle index, how many crews hold each token (all crews of the pattern)."""
    sequences = [crew_sequence(pattern, crew) for crew in pattern.crews]
    out: list[dict[str, int]] = []
    for i in range(pattern.cycle_days):
        counts: dict[str, int] = {}
        for seq in sequences:
            counts[seq[i]] = counts.get(seq[i], 0) + 1
        out.append(counts)
    return tuple(out)


def validate_pattern(pattern: PatternVersionData,
                     shifts: Mapping[Any, ShiftTimes | int] | None = None) -> list[PatternIssue]:
    """Structural checks of a version save / preview (§2.2). Empty list = valid.

    ``shifts`` (optional) lets the check see archived shifts (``SHIFT_ARCHIVED``) and the long-duty flag a
    template requires (``KELLY_24_48``: ``K`` on a shift without ``long_duty`` -> ``TOKEN_UNMAPPED``).
    Lints are not issues (see :func:`lint`); a crew row without OFF is valid.
    """
    issues: list[PatternIssue] = []

    def add(code: str, rule: str | None, message: str, **details: Any) -> None:
        issues.append(PatternIssue(code, rule, message, details))

    length = pattern.cycle_days
    if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= MAX_CYCLE_DAYS:
        add(INVALID_FIELDS, None, f"cycle_days must be 1..{MAX_CYCLE_DAYS}", field="cycle_days")
        return issues
    weekday = pattern.week_anchor_weekday
    if weekday is not None and (isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6):
        add(INVALID_FIELDS, None, "week_anchor_weekday must be 0 (Mon) .. 6 (Sun)", field="week_anchor_weekday")
    has_tokens, has_rows = pattern.tokens is not None, pattern.crew_rows is not None
    if has_tokens == has_rows:
        add(ROTATION_PATTERN_INVALID, RULE_MODE, "exactly one of tokens or crew_rows is required")
        return issues
    rows = {None: pattern.tokens} if has_tokens else dict(pattern.crew_rows or {})
    if has_rows and not rows:
        add(ROTATION_PATTERN_INVALID, RULE_MODE, "crew_rows is empty")
        return issues
    lengths_ok = True
    for crew, row in rows.items():
        if not isinstance(row, str) or len(row) != length:
            lengths_ok = False
            add(ROTATION_PATTERN_INVALID, RULE_ROW_LENGTH, f"row {crew or 'tokens'} length != cycle_days {length}",
                crew_code=crew, length=len(row) if isinstance(row, str) else None)
        elif not set(row) <= TOKEN_CHARS:
            add(INVALID_FIELDS, None, "tokens are one character [A-Z0-9] per cycle day", field="tokens", crew_code=crew)
    if has_rows and pattern.crew_offsets and any(pattern.crew_offsets.values()):
        add(ROTATION_PATTERN_INVALID, RULE_MODE, "crew_offsets apply to base-row mode only")
    for crew, offset in pattern.crew_offsets.items():
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < length:
            add(ROTATION_PATTERN_INVALID, RULE_OFFSET_RANGE, f"offset of crew {crew} outside [0, {length})",
                crew_code=crew, offset=offset)
    if OFF_TOKEN in pattern.token_map:
        add(ROTATION_PATTERN_INVALID, RULE_O_REMAPPED, "token O is reserved for OFF and may not be mapped")
    used = sorted({t for row in rows.values() if isinstance(row, str) for t in row if t != OFF_TOKEN})
    template = TEMPLATES.get(pattern.template_key or "")
    for token in used:
        if token not in pattern.token_map:
            add(ROTATION_PATTERN_INVALID, RULE_TOKEN_UNMAPPED, f"token {token} is not mapped", token=token)
            continue
        ref = _ref(pattern.token_map[token])
        if ref == OFF or shifts is None:
            continue
        times = shifts.get(ref)
        if isinstance(times, ShiftTimes):
            if times.archived:
                add(SHIFT_ARCHIVED, None, f"token {token} maps to an archived shift", token=token, shift=ref)
            if template is not None and token in template.long_duty_tokens and not times.long_duty:
                add(ROTATION_PATTERN_INVALID, RULE_TOKEN_UNMAPPED,
                    f"token {token} of {template.key} needs a shift with long_duty = true", token=token, shift=ref)
    if lengths_ok and pattern.coverage and not any(i.rule == RULE_OFFSET_RANGE for i in issues):
        for idx, counts in enumerate(coverage_by_index(pattern)):
            for token, needed in pattern.coverage.items():
                if counts.get(token, 0) != needed:
                    add(ROTATION_PATTERN_INVALID, RULE_COVERAGE_MISMATCH,
                        f"index {idx}: {counts.get(token, 0)} crews hold {token}, coverage says {needed}",
                        index=idx, token=token, measured=counts.get(token, 0), expected=needed)
                    break
            else:
                continue
            break
    return issues


# --------------------------------------------------------------------------- anchor alignment (§2.6)


def suggest_anchor(pattern: PatternVersionData, week_start: int = 0,
                   shifts: Mapping[Any, ShiftTimes | int] | None = None) -> AnchorSuggestion:
    """Choose the cycle index ``k`` on which a fixed week should start (§2.6).

    ``week_start`` = ``payroll.calendar.week_start_day`` (0 = Monday). For each k in 0..6 and each crew the
    scheduled net minutes of every fixed week ``[k + 7j, k + 7j + 7)`` over one cycle (wrapping) are computed;
    the k with the smallest worst week wins, ties by the fewest crew-weeks at that value, then the smallest k.
    ``anchor_weekday = (week_start - k) mod 7``. When L is not a multiple of 7 every alignment occurs over time:
    ``k`` and ``anchor_weekday`` are ``None`` and the min / max fixed-week minutes come from LCM(L, 7) days.
    """
    if not 0 <= week_start <= 6:
        raise RotationEngineError("INVALID_WEEK_START", "week_start must be 0..6", "week_start")
    catalogue = _shift_catalogue(pattern, shifts)
    length = pattern.cycle_days
    minutes_rows: list[list[int]] = []
    for crew in pattern.crews:
        seq = crew_sequence(pattern, crew)
        row = []
        for token in seq:
            ref = shift_for_token(pattern, token)
            row.append(0 if ref is None else _net(catalogue, ref))
        minutes_rows.append(row)

    if length % 7:
        span = length * 7 // math.gcd(length, 7)
        weeks = [sum(row[(s + i) % length] for i in range(7)) for row in minutes_rows for s in range(span)]
        return AnchorSuggestion(None, None, max(weeks), None, {}, {}, min(weeks))

    table: dict[int, int] = {}
    counts: dict[int, int] = {}
    lowest = None
    for k in range(7):
        weeks = [sum(row[(k + 7 * j + i) % length] for i in range(7)) for row in minutes_rows
                 for j in range(length // 7)]
        table[k] = max(weeks)
        counts[k] = weeks.count(table[k])
        lowest = min(weeks) if lowest is None else min(lowest, min(weeks))
    best = min(range(7), key=lambda k: (table[k], counts[k], k))
    return AnchorSuggestion(best, (week_start - best) % 7, table[best], counts[best], table, counts, lowest or 0)


def alignment_k(anchor_weekday: int, week_start: int) -> int:
    """The cycle index on which a fixed week starts for an anchor on ``anchor_weekday`` (inverse of §2.6 step 4);
    ``suggest_anchor(...).table[alignment_k(...)]`` is the live assignment's worst fixed week (EC AS-ROT-07)."""
    return (week_start - anchor_weekday) % 7


def next_anchor_date(on_or_after: dt.date, anchor_weekday: int) -> dt.date:
    """The editor's proposal: the next date with ``anchor_weekday`` on or after the planned start (§2.6 step 4)."""
    return on_or_after + dt.timedelta(days=(anchor_weekday - on_or_after.weekday()) % 7)


# --------------------------------------------------------------------------- night windows (shared with roster_rules)


def _window_bounds(day: dt.date, window: tuple[str, str], zone: str) -> tuple[dt.datetime, dt.datetime]:
    start_min, end_min = se._clock(window[0], "night_window"), se._clock(window[1], "night_window")
    start_local = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=start_min)
    end_local = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=end_min)
    if end_min <= start_min:
        end_local += _ONE_DAY
    if zone == "UTC":
        return start_local.replace(tzinfo=se.UTC), end_local.replace(tzinfo=se.UTC)
    return se.resolve_local_time(zone, start_local), se.resolve_local_time(zone, end_local)


def _local_date(instant: dt.datetime, zone: str) -> dt.date:
    if zone == "UTC":
        return instant.astimezone(se.UTC).date()
    return se._local(instant, se._zone(zone, "zone")).date()


def window_overlap_minutes(start_at: dt.datetime, end_at: dt.datetime, zone: str,
                           window: tuple[str, str] = DEFAULT_NIGHT_WINDOW) -> int:
    """Minutes of ``[start_at, end_at)`` inside the daily local ``window`` (e.g. 22:00–06:00) in ``zone``.
    Touching is not overlapping (a 14:00–22:00 shift has 0 minutes in 22:00–06:00)."""
    total = 0
    day = _local_date(start_at, zone) - _ONE_DAY
    last = _local_date(end_at, zone)
    while day <= last:
        w_start, w_end = _window_bounds(day, window, zone)
        total += se._overlap_seconds(start_at, end_at, w_start, w_end)
        day += _ONE_DAY
    return total // 60


def full_windows_between(after: dt.datetime, before: dt.datetime, zone: str,
                         window: tuple[str, str] = DEFAULT_NIGHT_WINDOW) -> int:
    """How many whole daily ``window`` occurrences lie inside ``[after, before]`` (free nights, §2.7 G5)."""
    if before <= after:
        return 0
    count = 0
    day = _local_date(after, zone) - _ONE_DAY
    last = _local_date(before, zone)
    while day <= last:
        w_start, w_end = _window_bounds(day, window, zone)
        if w_start >= after and w_end <= before:
            count += 1
        day += _ONE_DAY
    return count


def is_night(start_at: dt.datetime, end_at: dt.datetime, zone: str,
             window: tuple[str, str] = DEFAULT_NIGHT_WINDOW,
             threshold_hours: Decimal | int = DEFAULT_NIGHT_THRESHOLD_HOURS) -> bool:
    """"Night instance" = at least ``threshold_hours`` inside the night window (§2.7)."""
    return Decimal(window_overlap_minutes(start_at, end_at, zone, window)) >= Decimal(threshold_hours) * 60


# --------------------------------------------------------------------------- lint core (§2.7)


@dataclass(frozen=True)
class _Finding:
    key: str
    items: tuple[LintInstance, ...]
    measured: Any = None
    limit: Any = None
    unit: str | None = None


def _runs(flags_by_date: Mapping[dt.date, bool]) -> list[list[dt.date]]:
    """Maximal runs of consecutive dates whose flag is true."""
    runs: list[list[dt.date]] = []
    for day in sorted(d for d, flag in flags_by_date.items() if flag):
        if runs and runs[-1][-1] + _ONE_DAY == day:
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def _lint_findings(items: Sequence[LintInstance], keys: Collection[str], zone: str,
                   night_window: tuple[str, str], types: Sequence[tuple[int, Any]] | None = None,
                   night_worker: NightWorkerRule | None = None) -> list[_Finding]:
    """The sequence lints over ``items`` (sorted by start). Cycle-level lints are done by :func:`lint`."""
    seq = sorted(items, key=lambda x: (x.start_at, x.end_at))
    found: list[_Finding] = []
    if not seq:
        return found

    if LINT_BACKWARD_ROTATION in keys:
        order = [ref for _, ref in sorted(types)] if types is not None else \
            [ref for _, ref in sorted({(x.type_start_minutes, x.type_ref) for x in seq}, key=lambda p: (p[0], str(p[1])))]
        if len(order) > 2:  # with two types (D <-> N) the lint never fires
            successor = {ref: order[(i + 1) % len(order)] for i, ref in enumerate(order)}
            for prev, cur in zip(seq, seq[1:]):
                if cur.type_ref != prev.type_ref and cur.type_ref != successor.get(prev.type_ref):
                    found.append(_Finding(LINT_BACKWARD_ROTATION, (prev, cur)))

    if LINT_WEEKLY_ROTATION_SPEED in keys:
        blocks: list[list[LintInstance]] = []
        for x in seq:  # OFF days skipped: consecutive work instances of the same type
            if blocks and blocks[-1][-1].type_ref == x.type_ref:
                blocks[-1].append(x)
            else:
                blocks.append([x])
        for block in blocks[1:-1]:  # bounded on both sides by a different type
            if LINT_SPEED_MIN_RUN <= len(block) <= LINT_SPEED_MAX_RUN:
                found.append(_Finding(LINT_WEEKLY_ROTATION_SPEED, tuple(block), len(block),
                                      f"{LINT_SPEED_MIN_RUN}-{LINT_SPEED_MAX_RUN}", "instances"))

    for x in seq:
        gross = x.gross_minutes
        if LINT_SHIFT_OVER_12H in keys and gross > LINT_MAX_SHIFT_MINUTES:
            found.append(_Finding(LINT_SHIFT_OVER_12H, (x,), _hours(gross), _hours(LINT_MAX_SHIFT_MINUTES), "h"))
        if LINT_SAFETY_CRITICAL_OVER_8H in keys and x.safety_critical and gross > LINT_SAFETY_CRITICAL_MINUTES:
            found.append(_Finding(LINT_SAFETY_CRITICAL_OVER_8H, (x,), _hours(gross),
                                  _hours(LINT_SAFETY_CRITICAL_MINUTES), "h"))
        if LINT_EARLY_START in keys and x.local_start_minutes < LINT_EARLY_START_MINUTES:
            found.append(_Finding(LINT_EARLY_START, (x,), f"{x.local_start_minutes // 60:02d}:"
                                  f"{x.local_start_minutes % 60:02d}", "07:00", "time"))

    by_date: dict[dt.date, list[LintInstance]] = {}
    for x in seq:
        by_date.setdefault(x.shift_date, []).append(x)

    if LINT_CONSECUTIVE_DAYS in keys:
        for run in _runs({d: True for d in by_date}):
            if len(run) > LINT_MAX_CONSECUTIVE_DAYS:
                found.append(_Finding(LINT_CONSECUTIVE_DAYS, tuple(by_date[run[0]] + by_date[run[-1]]), len(run),
                                      LINT_MAX_CONSECUTIVE_DAYS, "days"))

    if LINT_CONSECUTIVE_LONG_OR_NIGHT in keys:
        flags = {d: any(x.night or x.gross_minutes > LINT_LONG_MINUTES for x in xs) for d, xs in by_date.items()}
        for run in _runs(flags):
            if len(run) > LINT_MAX_LONG_OR_NIGHT_RUN:
                found.append(_Finding(LINT_CONSECUTIVE_LONG_OR_NIGHT, tuple(by_date[run[0]] + by_date[run[-1]]),
                                      len(run), LINT_MAX_LONG_OR_NIGHT_RUN, "days"))
        # A single duty longer than 12 h (24-h Kelly duty) is itself more consecutive long / night work than the
        # guidance allows; it fires by design (§2.3 note on KELLY_24_48).
        for x in seq:
            if x.gross_minutes > LINT_MAX_SHIFT_MINUTES:
                found.append(_Finding(LINT_CONSECUTIVE_LONG_OR_NIGHT, (x,), _hours(x.gross_minutes),
                                      _hours(LINT_MAX_SHIFT_MINUTES), "h"))

    if LINT_DAY_NIGHT_SWITCH_REST in keys:
        for prev, cur in zip(seq, seq[1:]):
            if cur.night and not prev.night:
                free = full_windows_between(prev.end_at, cur.start_at, zone, night_window)
                if free < LINT_MIN_FREE_NIGHTS:
                    found.append(_Finding(LINT_DAY_NIGHT_SWITCH_REST, (prev, cur), free, LINT_MIN_FREE_NIGHTS,
                                          "nights"))

    if LINT_REST_BELOW_10H in keys:
        for prev, cur in zip(seq, seq[1:]):
            rest = int((cur.start_at - prev.end_at).total_seconds()) // 60
            if rest < LINT_MIN_REST_MINUTES:
                found.append(_Finding(LINT_REST_BELOW_10H, (prev, cur), _hours(rest), _hours(LINT_MIN_REST_MINUTES), "h"))

    if LINT_NIGHT_WORKER_AVERAGE in keys and night_worker is not None:
        threshold = Decimal(night_worker.threshold_hours_in_window) * 60
        is_night_worker = any(Decimal(window_overlap_minutes(x.start_at, x.end_at, zone, night_window)) >= threshold
                              for x in seq)
        if is_night_worker:
            span_days = night_worker.reference_weeks * 7
            first = seq[0].shift_date
            total = sum(x.net_minutes for x in seq if (x.shift_date - first).days < span_days)
            average = Decimal(total) / 60 / span_days
            if average > Decimal(night_worker.average_max_hours):
                found.append(_Finding(LINT_NIGHT_WORKER_AVERAGE, tuple(seq[:1]),
                                      average.quantize(_HOURS_2DP, rounding=ROUND_HALF_UP),
                                      Decimal(night_worker.average_max_hours), "h/24h"))
    return found


def lint_instances(items: Sequence[LintInstance], *, employee_id: int | None, zone: str,
                   night_window: tuple[str, str] = DEFAULT_NIGHT_WINDOW, muted: Collection[str] = (),
                   night_worker: NightWorkerRule | None = None) -> list[RuleResult]:
    """Publish-level lints (the "pattern + publish" rows of §2.7) on one employee's resolved instances.
    One WARN result per finding, ``dates`` = the shift dates involved."""
    keys = PUBLISH_LINT_KEYS - frozenset(muted) | (UNMUTABLE_FOR_LONG_DUTY if any(x.long_duty for x in items)
                                                   else frozenset())
    keys = keys & PUBLISH_LINT_KEYS
    results: list[RuleResult] = []
    for f in _lint_findings(items, keys, zone, night_window, night_worker=night_worker):
        results.append(RuleResult(
            f.key, WARN, employee_id, tuple(sorted({x.shift_date for x in f.items})), f.measured, f.limit, f.unit,
            _LINT_SOURCE[f.key], f"rotation.lint.{f.key[5:].lower()}"))
    return results


def _synthetic_instances(pattern: PatternVersionData, crew: str | None, catalogue: Mapping[Any, ShiftTimes | int],
                         first: int, last: int, base: dt.date) -> list[LintInstance]:
    seq = crew_sequence(pattern, crew)
    out: list[LintInstance] = []
    for n in range(first, last):
        idx = n % pattern.cycle_days
        ref = shift_for_token(pattern, seq[idx])
        if ref is None:
            continue
        times = _times(catalogue, ref)
        day = base + dt.timedelta(days=n)
        start = dt.datetime.combine(day, dt.time(), tzinfo=se.UTC) + dt.timedelta(minutes=times.start_minutes)
        end = start + dt.timedelta(minutes=times.gross_minutes)
        out.append(LintInstance(day, start, end, ref, times.start_minutes, times.start_minutes, times.net,
                                False, times.safety_critical, times.long_duty, idx))
    return out


def _with_night(items: list[LintInstance], window: tuple[str, str], threshold: Decimal) -> list[LintInstance]:
    return [LintInstance(x.shift_date, x.start_at, x.end_at, x.type_ref, x.type_start_minutes, x.local_start_minutes,
                         x.net_minutes, is_night(x.start_at, x.end_at, "UTC", window, threshold), x.safety_critical,
                         x.long_duty, x.index) for x in items]


def lint(pattern: PatternVersionData, shifts: Mapping[Any, ShiftTimes] | None = None, *,
         night_window: tuple[str, str] = DEFAULT_NIGHT_WINDOW,
         night_threshold_hours: Decimal | int = DEFAULT_NIGHT_THRESHOLD_HOURS,
         anchor_weekday: int | None = None, night_worker: NightWorkerRule | None = None,
         include_muted: bool = False) -> list[RuleResult]:
    """Pattern-level fatigue lints (§2.7): WARN only, one result per (lint key, crew).

    Each crew row is expanded wrapped (``-L .. LCM(L, 7) + L`` days) so runs and transitions across the cycle
    boundary are seen; weekday lints use ``LCM(L, 7)`` days with cycle index 0 on ``anchor_weekday`` (default
    the version's ``week_anchor_weekday``, else Monday). Clock times are the mapped shifts' local times
    (``shifts``; template defaults for a template copy). Muted keys are skipped (``include_muted`` lists them
    anyway), except ``LINT_SHIFT_OVER_12H`` / ``LINT_CONSECUTIVE_LONG_OR_NIGHT`` for long-duty shifts.
    ``details`` carries ``crew_code`` and up to 10 cycle ``indices``.
    """
    catalogue = _shift_catalogue(pattern, shifts)
    threshold = Decimal(night_threshold_hours)
    length = pattern.cycle_days
    span = length * 7 // math.gcd(length, 7)
    weekday0 = anchor_weekday if anchor_weekday is not None else (pattern.week_anchor_weekday or 0)
    base = _SYNTHETIC_MONDAY + dt.timedelta(days=weekday0)
    long_duty = any(_times(catalogue, ref).long_duty for ref in
                    {_ref(v) for v in pattern.token_map.values()} - {OFF} if ref in catalogue)
    muted = frozenset() if include_muted else pattern.lints_muted - (UNMUTABLE_FOR_LONG_DUTY if long_duty else frozenset())
    keys = frozenset(LINT_KEYS) - muted
    types = sorted({(_times(catalogue, _ref(v)).start_minutes, _ref(v)) for v in pattern.token_map.values()
                    if _ref(v) != OFF and _ref(v) in catalogue}, key=lambda p: (p[0], str(p[1])))
    results: list[RuleResult] = []

    def emit(key: str, crew: str | None, indices: Iterable[int], measured: Any = None, limit: Any = None,
             unit: str | None = None) -> None:
        results.append(RuleResult(key, WARN, None, (), measured, limit, unit, _LINT_SOURCE[key],
                                  f"rotation.lint.{key[5:].lower()}",
                                  {"crew_code": crew, "indices": sorted(set(indices))[:10]}))

    for crew in pattern.crews:
        wrapped = _with_night(_synthetic_instances(pattern, crew, catalogue, -length, span + length, base),
                              night_window, threshold)
        by_key: dict[str, list[_Finding]] = {}
        for f in _lint_findings(wrapped, keys - {LINT_NIGHT_WORKER_AVERAGE}, "UTC", night_window, types):
            by_key.setdefault(f.key, []).append(f)
        for key in LINT_KEYS:
            if key not in by_key:
                continue
            findings = by_key[key]
            worst = max(findings, key=lambda f: (Decimal(str(f.measured)) if isinstance(f.measured, (int, Decimal))
                                                 else Decimal(0)))
            emit(key, crew, (x.index for f in findings for x in f.items if x.index is not None),
                 worst.measured, worst.limit, worst.unit)

        cycle = [x for x in wrapped if 0 <= (x.shift_date - base).days < span]
        if LINT_NO_FREE_WEEKEND in keys:
            work_days = {(x.shift_date - base).days for x in cycle}
            free = any((base + dt.timedelta(days=n)).weekday() == _SAT and n not in work_days
                       and (n + 1) % span not in work_days for n in range(span))
            if not free:
                emit(LINT_NO_FREE_WEEKEND, crew, (), 0, 1, "weekends")
        if LINT_PERMANENT_NIGHTS in keys and cycle and all(x.night for x in cycle):
            emit(LINT_PERMANENT_NIGHTS, crew, (x.index for x in cycle if x.index is not None))
        if LINT_NIGHT_WORKER_AVERAGE in keys and night_worker is not None:
            days = night_worker.reference_weeks * 7
            sample = _with_night(_synthetic_instances(pattern, crew, catalogue, 0, days, base), night_window, threshold)
            for f in _lint_findings(sample, {LINT_NIGHT_WORKER_AVERAGE}, "UTC", night_window,
                                    night_worker=night_worker):
                emit(f.key, crew, (), f.measured, f.limit, f.unit)
    return results


def lint_keys(results: Iterable[RuleResult]) -> frozenset[str]:
    return frozenset(r.rule_key for r in results)


# --------------------------------------------------------------------------- cycle figures (§2.3 table, §2.4 editor)


def cycle_stats(pattern: PatternVersionData, shifts: Mapping[Any, ShiftTimes] | None = None, *,
                minutes_basis: str = "net", night_window: tuple[str, str] = DEFAULT_NIGHT_WINDOW,
                night_threshold_hours: Decimal | int = DEFAULT_NIGHT_THRESHOLD_HOURS) -> CycleStats:
    """Average and worst-window figures over one wrapped cycle, worst over crews (§2.3 columns)."""
    if minutes_basis not in ("net", "gross"):
        raise RotationEngineError("INVALID_BASIS", "minutes_basis is 'net' or 'gross'", "minutes_basis")
    catalogue = _shift_catalogue(pattern, shifts)
    length = pattern.cycle_days
    threshold = Decimal(night_threshold_hours) * 60
    avg = Decimal(0)
    max_run = max_night_run = worst_days = worst_minutes = 0
    for crew in pattern.crews:
        seq = crew_sequence(pattern, crew)
        minutes, work, night = [], [], []
        for token in seq:
            ref = shift_for_token(pattern, token)
            if ref is None:
                minutes.append(0)
                work.append(False)
                night.append(False)
                continue
            times = _times(catalogue, ref)
            minutes.append(times.gross_minutes if minutes_basis == "gross" else times.net)
            work.append(True)
            start = dt.datetime.combine(_SYNTHETIC_MONDAY, dt.time(), tzinfo=se.UTC) + \
                dt.timedelta(minutes=times.start_minutes)
            end = start + dt.timedelta(minutes=times.gross_minutes)
            night.append(Decimal(window_overlap_minutes(start, end, "UTC", night_window)) >= threshold)
        avg = max(avg, Decimal(sum(minutes)) * 7 / length)
        max_run = max(max_run, _wrapped_run(work))
        max_night_run = max(max_night_run, _wrapped_run(night))
        for s in range(length):
            window = [(s + i) % length for i in range(7)]
            worst_days = max(worst_days, sum(1 for i in window if work[i]))
            worst_minutes = max(worst_minutes, sum(minutes[i] for i in window))
    return CycleStats(avg, max_run, max_night_run, worst_days, worst_minutes, coverage_by_index(pattern))


def _wrapped_run(flags: Sequence[bool]) -> int:
    if flags and all(flags):
        return 2 * len(flags)  # no OFF: unbounded (reported as twice the cycle)
    best = run = 0
    for flag in list(flags) * 2:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best
