"""Day evaluation (AS-M3, WI-AS-3.x): one pure evaluator per employee and shift date.

Owner doc ``docs/attendance-shift/04-attendance/03-day-evaluation-calculations.md``: contract §2, sessions §4,
shift date §5, the 19 STANDARD steps §6.2, late / early §7, hashes §8.2, golden vectors §15. Vocabularies are the
spine constants of AS 11 §5 (``EVALUATION_STATES``, ``DAY_RESULTS`` ...); ``attendance_days`` stores the outputs.

Pure (spine §1.4 L0): no database, no clock, no settings reads. ``now`` is an input; the batch loader
``attendance_days.load_inputs_many`` resolves policies, settings, instances and sessions and the write path
persists the result. The preview path and the real path run the same code.

Attendance publishes physical facts only (net seconds, marks, night minutes, OT-eligible seconds). It never
prices time: payroll turns a day into money through ``DAY_RESULT_TO_LEDGER`` (JC-AS-01).

Conventions (AS 14 C-6): instants are aware UTC datetimes, durations are whole seconds, ``*_minutes =
floor(seconds / 60)``, an unknown value is ``None`` (never 0), thresholds are ``Decimal`` and never rounded (A-04).

Scope of this revision (IG-AS-63): steps 1–19 except 5b (statutory pack breaks: "no pack" until packs resolve, so
``meal_violations`` / ``rest_violations`` stay 0) and the break attestation of step 5 (no attestation input yet).
Step 16 applies the daily part of payroll's ``threshold_overtime`` arithmetic to one date (IG-AS-64).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal
from typing import Any, Optional

import pytz

from app.services import shift_engine as engine

EVALUATION_ENGINE_VERSION = "2026.10.1"

# Spine vocabularies (AS 11 §5).
EVALUATION_STATES = ("OPEN", "PROVISIONAL", "FINAL")
OPEN, PROVISIONAL, FINAL = EVALUATION_STATES
RULES_MODES = ("LEGACY_V1", "STANDARD")
LEGACY_V1, STANDARD = RULES_MODES
PORTION_RESULTS = ("PRESENT", "ABSENT", "MISSING_PUNCH", "UNMARKED", "PENDING", "OFF")
PRESENT, ABSENT, MISSING_PUNCH, UNMARKED, PENDING, OFF = PORTION_RESULTS
HALF_DAY = "HALF_DAY"
DAY_RESULTS = ("PRESENT", "HALF_DAY", "ABSENT", "MISSING_PUNCH", "UNMARKED", "PENDING", "OFF")
PRESENCE_BASES = ("SESSION", "ON_DUTY", "REMOTE_WORK", "OVERRIDE", "PRESUMED")
SESSION, ON_DUTY, REMOTE_WORK, OVERRIDE, PRESUMED = PRESENCE_BASES
_BASIS_PRIORITY = (OVERRIDE, SESSION, ON_DUTY, REMOTE_WORK, PRESUMED)  # step 19
PENDING_REASONS = ("SHIFT_IN_PROGRESS", "DEVICE_SYNC", "REQUEST_PENDING", "LEAVE_PENDING", "NOT_EVALUATED")
SHIFT_IN_PROGRESS, DEVICE_SYNC, REQUEST_PENDING, LEAVE_PENDING, NOT_EVALUATED = PENDING_REASONS
EVALUATION_WRITE_MODES = ("NORMAL", "SHADOW")
EXCUSE_SOURCES = ("REQUEST", "POLICY")
WAIVER_MODES = ("NONE", "NET_GE_EXPECTED", "GROSS_GE_EXPECTED")
INPUTS_HASH_PHASES = ("BEFORE_CLOSE", "AFTER_CLOSE")
EVALUATION_TRIGGERS = (
    "PUNCH", "SESSION_CLOSED", "PAIRING", "REQUEST_DECIDED", "ON_DUTY_DECIDED", "LEAVE_CHANGED", "HOLIDAY_CHANGED",
    "WORKWEEK_CHANGED", "ROSTER_PUBLISHED", "SHIFT_VERSION", "POLICY_CHANGED", "SETTING_CHANGED",
    "PLACEMENT_CHANGED", "MAPPING_CHANGED", "CONSENT_CHANGED", "DAY_CLOSE", "BACKFILL", "MANUAL_RECOMPUTE",
)
EXCEPTION_CODES = (
    # The six leave-holiday kinds, names verbatim (LH 05 §4.3; B-07).
    "WORKED_ON_LEAVE", "UNMARKED_DAY", "MISSING_PUNCH_OUT", "NON_WORKING_DAY_WORKED", "LEAVE_PENDING_PAST_DATE",
    "LEAVE_ON_PUNCHED_DATE",
    # Attendance kinds (§12.2).
    "MISSING_PUNCH_IN", "LATE_IN", "EARLY_OUT", "SHORT_HOURS", "ABSENT", "NO_SHIFT_ASSIGNED", "SHIFT_MISMATCH",
    "ROSTER_UNPUBLISHED", "UNSCHEDULED_WORK", "OUTSIDE_GEOFENCE", "EVIDENCE_FLAGGED", "OVERTIME_UNAPPROVED",
    "OVERTIME_CONSENT_MISSING", "BREAK_MISSED", "BREAK_OVERSTAY", "MEAL_BREAK_LATE", "MEAL_BREAK_SHORT",
    "MEAL_BREAK_MISSED", "REST_BREAK_MISSED", "DEVICE_SYNC_PENDING", "REQUEST_PENDING", "REST_VIOLATION",
    "CONSECUTIVE_ABSENCE", "BREAK_OFF_WINDOW", "BREAK_NOT_TAKEN", "SHORT_VISIT", "OVERTIME_ON_ABSENT_DAY",
    "NIGHT_WORK_WITHOUT_CONSENT", "NO_PLACEMENT",
)
EXPECTED_PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF", "NONE")
FIRST_HALF, SECOND_HALF = "FIRST_HALF", "SECOND_HALF"
HALVES = (FIRST_HALF, SECOND_HALF)
_PORTION_HALVES = {"FULL": HALVES, FIRST_HALF: (FIRST_HALF,), SECOND_HALF: (SECOND_HALF,), "NONE": ()}
INTERACTIVE_SOURCES = ("WEB", "MOBILE", "KIOSK")

UTC = dt.timezone.utc
Span = tuple  # (start: datetime, end: datetime), start < end


class DayEvaluationError(ValueError):
    """Malformed evaluator input (a programming error of the caller, never a user error)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- inputs

_NULLABLE_RULES = ("grace_in_minutes", "late_half_day_after_minutes", "early_half_day_before_minutes",
                   "non_working_day_min_minutes")


@dataclass(frozen=True)
class RulesParams:
    """The resolved ``ATTENDANCE_RULES_POLICY`` fields (02 §4.3); defaults are the STANDARD defaults."""

    day_basis: str = "HOURS"  # PRESENCE | HOURS
    hours_basis: str = "NET"  # NET | GROSS
    threshold_mode: str = "PERCENT_OF_EXPECTED"  # ABSOLUTE | PERCENT_OF_EXPECTED
    full_day_min_minutes: Decimal = Decimal(480)
    half_day_min_minutes: Decimal = Decimal(240)
    full_day_min_percent: int = 90
    half_day_min_percent: int = 50
    work_time_basis: str = "SUM_OF_SESSIONS"  # SUM_OF_SESSIONS | FIRST_IN_LAST_OUT
    absent_marking: str = "AUTO_ABSENT"  # AUTO_ABSENT | LEAVE_UNMARKED
    non_working_day_min_minutes: Optional[Decimal] = None
    grace_in_minutes: Optional[int] = None  # NULL = setting attendance.grace_minutes
    grace_out_minutes: int = 0
    flexible_late_basis: str = "NONE"  # NONE | CORE_START
    late_half_day_after_minutes: Optional[int] = None
    early_half_day_before_minutes: Optional[int] = None
    late_waiver: str = "NONE"
    early_waiver: str = "NONE"
    short_hours_enabled: bool = False
    short_tolerance_minutes: int = 0
    break_attestation: bool = False
    rounding_mode: str = "NONE"  # NONE | NEAREST | UP | DOWN
    rounding_increment_minutes: int = 1
    open_day_start_time: str = engine.DEFAULT_OPEN_DAY_START_TIME
    ot_early_arrival: str = "NEVER"  # NEVER | WITH_APPROVAL | ALWAYS
    ot_post_shift_buffer_minutes: int = 0
    ot_requires_pre_approval: bool = False

    @classmethod
    def from_fields(cls, raw: Optional[Mapping[str, Any]]) -> "RulesParams":
        """A policy version's ``fields`` JSON -> params; unknown keys are ignored, missing keys keep the default."""
        raw = dict(raw or {})
        rounding = raw.pop("rounding", None) or {}
        if "mode" in rounding:
            raw["rounding_mode"] = rounding["mode"]
        if "increment_minutes" in rounding:
            raw["rounding_increment_minutes"] = rounding["increment_minutes"]
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in raw.items() if k in known and (v is not None or k in _NULLABLE_RULES)}
        for name in ("full_day_min_minutes", "half_day_min_minutes", "non_working_day_min_minutes"):
            if values.get(name) is not None:
                values[name] = Decimal(str(values[name]))
        return cls(**values)


@dataclass(frozen=True)
class LegacyPayParams:
    """Payroll's own fields a LEGACY_V1 day reads per period (read, never copied; 02 §6.3, JC-AS-04/-05)."""

    derive_status_from_hours: bool = False
    full_day_min_hours: Decimal = Decimal(8)
    half_day_min_hours: Decimal = Decimal(4)
    early_exit_grace_minutes: int = 0


@dataclass(frozen=True)
class EvalSettings:
    """Settings used by the evaluation (§2.2), resolved per employee and date."""

    grace_minutes: int = 15
    night_window_start: str = "22:00"
    night_window_end: str = "06:00"
    day_close_buffer_minutes: int = 240
    capture_enabled: bool = True


@dataclass(frozen=True)
class OvertimeView:
    """Payroll's OT switches and daily threshold for E on D (read-only; step 16).

    ``daily_threshold_minutes`` None = ``SHIFT_DURATION`` (the day's ``scheduled_minutes``); ``daily_applies``
    is false for the WEEKLY combination, whose weekly part only the proposal job decides.
    """

    enabled: bool = False
    daily_applies: bool = True
    daily_threshold_minutes: Optional[int] = None
    holiday_work_premium: bool = False
    weekly_off_pay: bool = False


@dataclass(frozen=True)
class ExpectationData:
    """``time_inputs.expected_presence`` for (E, D) plus the work source (LH 05 §4.2)."""

    expected_portion: str = "NONE"
    expected_minutes: int = 0
    reasons: tuple[str, ...] = ()
    calendar_version: Optional[str] = None
    work_source: Optional[str] = None


@dataclass(frozen=True)
class BreakData:
    """One ``attendance_breaks`` row; ``ended_at`` None = still open."""

    id: int
    kind: str
    started_at: dt.datetime
    ended_at: Optional[dt.datetime]
    is_paid: bool


@dataclass(frozen=True)
class SessionData:
    """One non-voided ``attendance`` row of D.

    ``closed_at`` set with no ``check_out_time`` = auto-closed without a punch-out. ``legacy_status``,
    ``early_exit``, ``early_exit_minutes`` and ``late_minutes`` are the stored v1 marks (LEGACY_V1 only).
    """

    id: int
    check_in_at: dt.datetime
    check_out_at: Optional[dt.datetime]
    closed_at: Optional[dt.datetime] = None
    breaks: tuple[BreakData, ...] = ()
    content_hash: Optional[str] = None
    capture_source: str = "WEB"
    work_mode: str = "ON_SITE"
    legacy_status: str = "present"
    late_minutes: Optional[int] = None
    early_exit: bool = False
    early_exit_minutes: Optional[int] = None


@dataclass(frozen=True)
class OnDutyData:
    """An APPROVED ``on_duty_requests`` row covering D (step 11)."""

    id: int
    kind: str = "ON_DUTY"  # ON_DUTY | REMOTE_WORK
    portion: str = "FULL"
    requires_punch: bool = False
    row_version: int = 1


@dataclass(frozen=True)
class OverrideData:
    """An applied ``STATUS_OVERRIDE`` (AS-M4): the halves it names and their results."""

    request_id: int
    version: int = 1
    first_half: Optional[str] = None  # PRESENT | ABSENT | UNMARKED
    second_half: Optional[str] = None


@dataclass(frozen=True)
class ExcuseData:
    """An approved ``MARK_EXCUSE`` for D (AS-M4): excuses a mark only up to ``minutes`` (SP-AS-067)."""

    request_id: int
    kind: str  # LATE | EARLY
    minutes: int
    version: int = 1


@dataclass(frozen=True)
class DayInputs:
    """Everything ``evaluate_day`` reads (§2.2), resolved by the caller in the spine §5.1 order."""

    employee_id: int
    shift_date: dt.date
    work_timezone: str
    now: dt.datetime
    expectation: ExpectationData = ExpectationData()
    instances: tuple[engine.ShiftInstance, ...] = ()
    shift_source: str = "NONE"
    shift_grace_in_minutes: Optional[int] = None  # shift_versions.grace_in_minutes (first wins, §6.1)
    shift_grace_out_minutes: Optional[int] = None
    scheduled_minutes: int = 0  # day facts net x FTE; the SHIFT_DURATION threshold (step 16)
    legal_entity_id: Optional[int] = None
    branch_id: Optional[int] = None
    sessions: tuple[SessionData, ...] = ()
    on_duty: tuple[OnDutyData, ...] = ()
    overrides: tuple[OverrideData, ...] = ()
    excuses: tuple[ExcuseData, ...] = ()
    rules_mode: str = STANDARD
    rules: RulesParams = RulesParams()
    legacy: LegacyPayParams = LegacyPayParams()
    settings: EvalSettings = EvalSettings()
    ot_view: OvertimeView = OvertimeView()
    capture_required: bool = True
    shift_required: bool = False
    policy_version_ids: Mapping[str, int] = field(default_factory=dict)
    request_pending: bool = False
    device_ids_behind: tuple[int, ...] = ()
    late_log_grace_elapsed: bool = False
    not_evaluated: bool = False  # runtime policy tie (SP-AS-085)
    early_arrival_approved: bool = False  # WITH_APPROVAL: an approved overtime entry covers the early minutes
    previous_last_out_at: Optional[dt.datetime] = None
    rest_min_between_shifts_hours: Optional[Decimal] = None  # pack ``rest``; None = no pack
    punch_ids: tuple[int, ...] = ()


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True)
class DayEvaluation:
    """The output columns of ``attendance_days`` (§2.3) and the explain lines of the day Sheet."""

    employee_id: int
    shift_date: dt.date
    work_timezone: str
    legal_entity_id: Optional[int]
    branch_id: Optional[int]
    evaluation_state: str
    rules_mode: str
    inputs_hash: str
    content_hash: str
    policy_version_ids: dict
    calendar_version: Optional[str]
    work_source: Optional[str]
    shift_source: Optional[str]
    shift_id: Optional[int]
    shift_version_id: Optional[int]
    roster_entry_id: Optional[int]
    roster_version: Optional[int]
    expected_portion: str
    expected_minutes: Optional[int]
    expected_start_at: Optional[dt.datetime]
    expected_end_at: Optional[dt.datetime]
    session_count: int
    first_in_at: Optional[dt.datetime]
    last_out_at: Optional[dt.datetime]
    gross_seconds: Optional[int]
    net_seconds: Optional[int]
    gross_minutes: Optional[int]
    break_minutes: Optional[int]
    unpaid_break_minutes: Optional[int]
    net_minutes: Optional[int]
    auto_deducted_minutes: int
    rounding_delta_minutes: int
    late_minutes: Optional[int]
    early_minutes: Optional[int]
    short_minutes: Optional[int]
    late_mark: bool
    late_excused: bool
    late_excuse_source: Optional[str]
    early_mark: bool
    early_excused: bool
    early_excuse_source: Optional[str]
    short_mark: bool
    late_converted: bool
    early_converted: bool
    night_minutes: int
    ot_eligible_seconds: int
    ot_eligible_minutes: int
    ot_candidate_minutes: int
    meal_violations: int
    rest_violations: int
    first_half_result: str
    second_half_result: str
    day_result: str
    presence_basis: Optional[str]
    work_mode: Optional[str]
    exception_codes: tuple[str, ...]
    pending_reasons: tuple[str, ...]
    override_request_id: Optional[int]
    net_closed_seconds: Optional[int] = None  # explain only (AS-EVL-05)
    explain: tuple[str, ...] = ()

    def outputs(self) -> dict:
        """Every stored output column (version rows; ``attendance_day_versions.outputs``)."""
        return {name: _plain(getattr(self, name)) for name in OUTPUT_COLUMNS}


OUTPUT_COLUMNS = tuple(f.name for f in fields(DayEvaluation) if f.name not in (
    "employee_id", "shift_date", "inputs_hash", "net_closed_seconds", "explain"))

# Pay-relevant outputs hashed into ``content_hash`` (§8.2).
CONTENT_FIELDS = (
    "day_result", "first_half_result", "second_half_result", "pending_reasons", "presence_basis", "net_seconds",
    "ot_eligible_seconds", "late_mark", "early_mark", "short_mark", "late_excused", "late_excuse_source",
    "early_excused", "early_excuse_source", "late_converted", "early_converted", "meal_violations",
    "rest_violations", "night_minutes", "ot_candidate_minutes", "override_request_id",
)


# --------------------------------------------------------------------------- interval helpers


def _union(spans: Sequence[Span]) -> list[Span]:
    out: list[Span] = []
    for start, end in sorted(s for s in spans if s[1] > s[0]):
        if out and start <= out[-1][1]:
            if end > out[-1][1]:
                out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def _length(spans: Sequence[Span]) -> int:
    return sum(int((end - start).total_seconds()) for start, end in _union(spans))


def _intersect(a: Sequence[Span], b: Sequence[Span]) -> list[Span]:
    out: list[Span] = []
    for a_start, a_end in _union(a):
        for b_start, b_end in _union(b):
            start, end = max(a_start, b_start), min(a_end, b_end)
            if end > start:
                out.append((start, end))
    return _union(out)


def _before(spans: Sequence[Span], at: dt.datetime) -> list[Span]:
    return [(s, min(e, at)) for s, e in _union(spans) if s < at]


def _after(spans: Sequence[Span], at: dt.datetime) -> list[Span]:
    return [(max(s, at), e) for s, e in _union(spans) if e > at]


def _minutes(seconds: Optional[int]) -> Optional[int]:
    return None if seconds is None else seconds // 60


def _plain(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def _sha(payload: Any) -> str:
    text = json.dumps(_plain(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- day-level halves (§4, §6.1)


@dataclass(frozen=True)
class _Half:
    name: str
    spans: tuple[Span, ...]  # empty = no clock boundary (OPEN shift, no shift)
    minutes: Optional[int]


def _day_halves(instances: Sequence[engine.ShiftInstance], expected_minutes: int) -> dict[str, _Half]:
    clocked = [i for i in instances if i.first_half.start_at is not None]
    if len(instances) == 1 and clocked:
        i = clocked[0]
        return {FIRST_HALF: _Half(FIRST_HALF, ((i.first_half.start_at, i.first_half.end_at),), i.first_half.minutes),
                SECOND_HALF: _Half(SECOND_HALF, ((i.second_half.start_at, i.second_half.end_at),),
                                   i.second_half.minutes)}
    if len(instances) > 1 and len(clocked) == len(instances):
        # Double shifts (AS-EVL-08): concatenate the instances' work intervals; H1 = the first ceil(50 %) minutes.
        spans: list[Span] = []
        for i in sorted(clocked, key=lambda x: x.start_at):
            work = [(s.start_at, s.end_at) for s in i.segments if s.kind == engine.WORK]
            spans.extend(work or [(i.start_at, i.end_at)])
        spans = _union(spans)
        total = _length(spans) // 60
        first_minutes = -(-total // 2)
        remaining = first_minutes * 60
        h1: list[Span] = []
        h2: list[Span] = []
        for start, end in spans:
            length = int((end - start).total_seconds())
            if remaining >= length:
                h1.append((start, end))
                remaining -= length
            elif remaining > 0:
                cut = start + dt.timedelta(seconds=remaining)
                h1.append((start, cut))
                h2.append((cut, end))
                remaining = 0
            else:
                h2.append((start, end))
        return {FIRST_HALF: _Half(FIRST_HALF, tuple(h1), first_minutes),
                SECOND_HALF: _Half(SECOND_HALF, tuple(h2), total - first_minutes)}
    if len(instances) == 1:  # OPEN: the first N worked minutes are H1
        i = instances[0]
        return {FIRST_HALF: _Half(FIRST_HALF, (), i.first_half.minutes),
                SECOND_HALF: _Half(SECOND_HALF, (), i.second_half.minutes)}
    first = expected_minutes // 2
    return {FIRST_HALF: _Half(FIRST_HALF, (), first), SECOND_HALF: _Half(SECOND_HALF, (), expected_minutes - first)}


# --------------------------------------------------------------------------- evaluation


def _close_point(inputs: DayInputs) -> dt.datetime:
    """Last instance end (no shift / OPEN: D+1 + ``open_day_start_time``) + the day-close buffer (§6.1)."""
    ends = [i.end_at for i in inputs.instances if i.end_at is not None]
    if ends:
        end = max(ends)
    else:
        hour, minute = (int(x) for x in inputs.rules.open_day_start_time.split(":"))
        end = engine.resolve_local_time(
            inputs.work_timezone,
            dt.datetime.combine(inputs.shift_date + dt.timedelta(days=1), dt.time(hour, minute)))
    return end + dt.timedelta(minutes=inputs.settings.day_close_buffer_minutes)


def close_point(inputs: DayInputs) -> dt.datetime:
    """Public: the instant after which D is decided (day close, the job's selection rule)."""
    return _close_point(inputs)


def _round_instant(at: dt.datetime, zone: Any, mode: str, increment: int) -> dt.datetime:
    """Step 3: round on the local time axis (``UP`` ceiling, ``DOWN`` floor, ``NEAREST`` half up)."""
    if mode == "NONE" or increment <= 1 and at.second == 0 and at.microsecond == 0:
        return at
    local = at.astimezone(zone)
    since = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1_000_000
    step = increment * 60
    if mode == "UP":
        target = -(-since // step) * step
    elif mode == "DOWN":
        target = (since // step) * step
    else:
        target = ((since + step / 2) // step) * step
    return at + dt.timedelta(seconds=target - since)


def _night_spans(inputs: DayInputs, last: dt.datetime) -> list[Span]:
    """N = the night windows opening on every local date from D-1 to the last local date W touches (step 14)."""
    zone = pytz.timezone(inputs.work_timezone)
    sh, sm = (int(x) for x in inputs.settings.night_window_start.split(":"))
    eh, em = (int(x) for x in inputs.settings.night_window_end.split(":"))
    last_date = last.astimezone(zone).date()
    out: list[Span] = []
    day = inputs.shift_date - dt.timedelta(days=1)
    while day <= last_date:
        start = engine.resolve_local_time(inputs.work_timezone, dt.datetime.combine(day, dt.time(sh, sm)))
        end_day = day + dt.timedelta(days=1) if (eh, em) <= (sh, sm) else day
        end = engine.resolve_local_time(inputs.work_timezone, dt.datetime.combine(end_day, dt.time(eh, em)))
        out.append((start, end))
        day += dt.timedelta(days=1)
    return _union(out)


def _decimal_minutes(seconds: int) -> Decimal:
    return Decimal(seconds) / Decimal(60)


def evaluate_day(inputs: DayInputs) -> DayEvaluation:
    """Evaluate one employee and shift date (§6). LEGACY_V1 days follow the exact port of §2.4."""
    if inputs.now.tzinfo is None:
        raise DayEvaluationError("INVALID_INSTANT", "now must be an aware instant")
    if inputs.rules_mode not in RULES_MODES:
        raise DayEvaluationError("INVALID_RULES_MODE", f"rules_mode must be one of {RULES_MODES}")
    if inputs.expectation.expected_portion not in EXPECTED_PORTIONS:
        raise DayEvaluationError("INVALID_PORTION", f"expected_portion must be one of {EXPECTED_PORTIONS}")
    state = _Run(inputs)
    if inputs.rules_mode == LEGACY_V1:
        state.legacy()
    else:
        state.standard()
    return state.result()


class _Run:
    """One evaluation; the steps fill these fields in order (§6.2)."""

    def __init__(self, inputs: DayInputs) -> None:
        self.i = inputs
        self.r = inputs.rules
        self.zone = pytz.timezone(inputs.work_timezone)
        self.exceptions: set[str] = set()
        self.explain: list[str] = []
        self.close_at = _close_point(inputs)
        self.after_close = inputs.now >= self.close_at
        exp = inputs.expectation
        self.not_employed = "NOT_EMPLOYED" in exp.reasons
        self.halves = _day_halves(inputs.instances, exp.expected_minutes)
        self.expected = list(_PORTION_HALVES[exp.expected_portion]) if not self.not_employed else []
        self.result_of: dict[str, str] = {h: OFF for h in HALVES}
        self.basis_of: dict[str, Optional[str]] = {h: None for h in HALVES}
        self.pending_of: dict[str, list[str]] = {h: [] for h in HALVES}
        # time
        self.work: list[Span] = []
        self.unpaid: list[Span] = []
        self.auto_seconds = 0
        self.break_spans: list[Span] = []
        self.gross: Optional[int] = None
        self.gross_known = True
        self.net: Optional[int] = None
        self.net_closed: Optional[int] = None
        self.rounding_delta = 0
        self.first_in: Optional[dt.datetime] = None
        self.last_out: Optional[dt.datetime] = None
        self.open_after_close: list[SessionData] = []
        # marks
        self.late_seconds: Optional[int] = None
        self.early_seconds: Optional[int] = None
        self.late_mark = self.early_mark = False
        self.late_excused = self.early_excused = False
        self.late_source: Optional[str] = None
        self.early_source: Optional[str] = None
        self.late_converted = self.early_converted = False
        self.short_minutes: Optional[int] = None
        self.short_mark = False
        self.night_seconds = 0
        self.ot_eligible = 0
        self.ot_candidate = 0
        self.override_request_id: Optional[int] = None
        self.state = OPEN
        self.state_reason: Optional[str] = None
        self.expected_minutes: Optional[int] = exp.expected_minutes if not self.not_employed else 0
        self.expected_start: Optional[dt.datetime] = None
        self.expected_end: Optional[dt.datetime] = None
        self.covered: dict[str, str] = {}  # half -> ON_DUTY | REMOTE_WORK
        self.unworked_by_rule: set[str] = set()  # halves made unworked by steps 8 / 9

    # ----------------------------------------------------------------- shared steps

    def _state(self) -> None:
        """Step 1."""
        if not self.after_close:
            self.state = OPEN
        elif self.i.device_ids_behind and not self.i.late_log_grace_elapsed:
            self.state, self.state_reason = PROVISIONAL, DEVICE_SYNC
        elif self.i.request_pending:
            self.state, self.state_reason = PROVISIONAL, REQUEST_PENDING
        else:
            self.state = FINAL
        if self.state == FINAL and self.i.device_ids_behind:
            self.exceptions.add("DEVICE_SYNC_PENDING")
        if self.state == PROVISIONAL and self.state_reason == REQUEST_PENDING:
            self.exceptions.add("REQUEST_PENDING")

    def _capture_off(self) -> bool:
        return not self.i.settings.capture_enabled or not self.i.capture_required

    def _sessions_window(self, rounding: bool) -> tuple[list[Span], list[Span], list[Span]]:
        """Step 2 (+ step 3 rounding of IN / OUT): W, the raw spans, and the break spans clipped to W."""
        rounded: list[Span] = []
        raw: list[Span] = []
        breaks: list[tuple[BreakData, Span]] = []
        self.open_after_close = []
        for s in sorted(self.i.sessions, key=lambda x: x.check_in_at):
            out = s.check_out_at
            if out is None:
                if self.after_close or s.closed_at is not None:
                    self.open_after_close.append(s)
                    continue
                out = self.i.now  # live session: provisional figures up to now
            if out <= s.check_in_at:
                continue
            raw.append((s.check_in_at, out))
            if rounding and self.r.rounding_mode != "NONE":
                start = _round_instant(s.check_in_at, self.zone, self.r.rounding_mode, self.r.rounding_increment_minutes)
                end = _round_instant(out, self.zone, self.r.rounding_mode, self.r.rounding_increment_minutes)
                if end > start:
                    rounded.append((start, end))
            else:
                rounded.append((s.check_in_at, out))
            for b in s.breaks:
                b_end = b.ended_at or out
                if b_end > b.started_at:
                    breaks.append((b, (b.started_at, b_end)))
        if self.r.work_time_basis == "FIRST_IN_LAST_OUT" and rounded and not self.open_after_close:
            rounded = [(min(s for s, _ in rounded), max(e for _, e in rounded))]
        self._breaks = breaks
        return _union(rounded), _union(raw), [span for _, span in breaks]

    def _expected_window(self) -> None:
        """§2.2: ``expected_start`` / ``expected_end`` from the expected halves not covered by ``ON_DUTY``."""
        if self.not_employed:
            return
        duty_halves: set[str] = set()
        for row in self.i.on_duty:
            if row.kind == "ON_DUTY":
                duty_halves |= set(_PORTION_HALVES.get(row.portion, ()))
        edge = [h for h in self.expected if h not in duty_halves] or []
        spans = [span for h in edge for span in self.halves[h].spans]
        if spans:
            self.expected_start = min(s for s, _ in spans)
            self.expected_end = max(e for _, e in spans)
        removed = [h for h in self.expected if h in duty_halves]
        if removed and self.expected_minutes is not None:
            minutes = sum(self.halves[h].minutes or 0 for h in removed)
            self.expected_minutes = max(0, self.expected_minutes - minutes)

    def _worked_in(self, half: str, work: Sequence[Span], unpaid: Sequence[Span]) -> int:
        h = self.halves[half]
        if h.spans:
            return max(0, _length(_intersect(work, h.spans)) - _length(_intersect(unpaid, h.spans)))
        total = max(0, _length(work) - _length(unpaid) - self.auto_seconds)
        first = (self.halves[FIRST_HALF].minutes or 0) * 60
        return min(total, first) if half == FIRST_HALF else max(0, total - first)

    # ----------------------------------------------------------------- STANDARD

    def standard(self) -> None:
        i, r = self.i, self.r
        self._state()
        self._expected_window()
        if i.not_evaluated:
            for h in self.expected:
                self.result_of[h], self.pending_of[h] = PENDING, [NOT_EVALUATED]
            self.state = PROVISIONAL if self.after_close else OPEN
            return
        capture_off = self._capture_off()

        # Steps 2-6: work set, rounding, gross, breaks, net.
        raw_work_unrounded = self._sessions_window(rounding=False)[0]
        self.work, raw_work, _ = self._sessions_window(rounding=True)
        if capture_off and self.open_after_close:
            self.open_after_close = []  # punches are optional: an open session contributes nothing (step 11)
        self.gross = _length(self.work)
        self._breaks_step()
        unpaid_total = _length(self.unpaid) + self.auto_seconds
        self.net_closed = max(0, self.gross - unpaid_total)
        if not i.sessions:  # no time recorded: unknown, never 0 (D-AS-80; GV-AS-EVL-13)
            self.gross = self.net_closed = self.net = None
        elif self.open_after_close:
            self.gross_known = False
            self.net = None
        else:
            self.net = self.net_closed
        if r.rounding_mode != "NONE" and self.net is not None:
            raw_net = max(0, _length(raw_work_unrounded) - unpaid_total)
            self.rounding_delta = self.net // 60 - raw_net // 60
        if raw_work:
            self.first_in = min(s.check_in_at for s in i.sessions)
            closed_outs = [s.check_out_at for s in i.sessions if s.check_out_at is not None]
            live = any(s.check_out_at is None for s in i.sessions)
            self.last_out = max(closed_outs) if closed_outs and not live else None
        elif i.sessions:
            self.first_in = min(s.check_in_at for s in i.sessions)

        has_work = bool(self.work) or bool(self.open_after_close)

        # Expected NONE (holiday, weekly off, leave, rostered OFF) or not employed.
        if not self.expected:
            self._non_working_day(has_work)
            self._night()
            self._ot(capture_off)
            self._rest()
            return

        # Step 11 (coverage first, AS-EVL-03): on-duty / remote work.
        for row in i.on_duty:
            kind = REMOTE_WORK if row.kind == "REMOTE_WORK" else ON_DUTY
            for h in _PORTION_HALVES.get(row.portion, ()):
                if h not in self.expected:
                    continue
                if row.requires_punch and not self._half_touched(h):
                    continue
                self.covered.setdefault(h, kind)
        all_on_call = bool(i.instances) and all(
            inst.segments and all(s.kind == engine.ON_CALL for s in inst.segments) for inst in i.instances)
        # An all-ON_CALL instance is presumed (step 11): nothing is judged, no marks (§6.4).
        judged = [] if all_on_call else [h for h in self.expected if h not in self.covered]

        # Step 7: marks.
        if not capture_off and not all_on_call:
            self._marks()

        # Step 8: portions.
        present: set[str] = set()
        if self.net is not None:
            present = self._portions(judged)
        elif self.open_after_close:
            present = self._portions_with_open_session(judged)

        # Step 9: late / early beyond.
        if not capture_off:
            self._beyond(judged, present)

        # Step 10: short hours.
        if not capture_off and self.net is not None and self.expected_minutes is not None:
            self.short_minutes = max(0, self.expected_minutes - self.net // 60)
            if r.short_hours_enabled and self.short_minutes > r.short_tolerance_minutes and not self.unworked_by_rule:
                self.short_mark = True
                self.exceptions.add("SHORT_HOURS")

        for h in present:
            self.result_of[h], self.basis_of[h] = PRESENT, SESSION
        for h, kind in self.covered.items():
            self.result_of[h], self.basis_of[h] = PRESENT, kind

        # Step 11 (rest): all-ON_CALL instances and capture off / not required -> PRESUMED.
        if all_on_call or capture_off:
            for h in self.expected:
                if self.result_of[h] != PRESENT:
                    self.result_of[h], self.basis_of[h] = PRESENT, PRESUMED

        # Step 12: missing punch.
        if self.open_after_close and not capture_off:
            for h in self._missing_halves(judged):
                if self.result_of[h] != PRESENT or self.basis_of[h] == SESSION:
                    self.result_of[h], self.basis_of[h] = MISSING_PUNCH, None
            self.exceptions.add("MISSING_PUNCH_OUT")

        # Step 13: unworked halves.
        leave_pending = LEAVE_PENDING in i.expectation.reasons
        for h in self.expected:
            if self.result_of[h] in (PRESENT, MISSING_PUNCH):
                continue
            if leave_pending:
                self.result_of[h], self.pending_of[h] = PENDING, [LEAVE_PENDING]
            elif self.state == OPEN:
                self.result_of[h], self.pending_of[h] = PENDING, [SHIFT_IN_PROGRESS]
            elif self.state == PROVISIONAL:
                self.result_of[h], self.pending_of[h] = PENDING, [self.state_reason]
            elif r.absent_marking == "LEAVE_UNMARKED":
                self.result_of[h] = UNMARKED
            else:
                self.result_of[h] = ABSENT

        # Step 11 (last word): applied STATUS_OVERRIDE.
        for o in i.overrides:
            for h, value in ((FIRST_HALF, o.first_half), (SECOND_HALF, o.second_half)):
                if value is not None and h in self.expected:
                    self.result_of[h] = value
                    self.basis_of[h] = OVERRIDE if value == PRESENT else None
                    self.pending_of[h] = []
            self.override_request_id = o.request_id

        if leave_pending and self.state != OPEN and any(PENDING == self.result_of[h] and LEAVE_PENDING in
                                                       self.pending_of[h] for h in self.expected):
            self.exceptions.add("LEAVE_PENDING_PAST_DATE")
        if any(self.result_of[h] == ABSENT for h in self.expected):
            self.exceptions.add("ABSENT")
        if (self.state == FINAL and not i.sessions and not self.covered and not i.overrides
                and any(self.result_of[h] in (ABSENT, UNMARKED) for h in self.expected)):
            self.exceptions.add("UNMARKED_DAY")
        if i.shift_required and not i.instances:
            self.exceptions.add("NO_SHIFT_ASSIGNED")

        self._night()
        self._ot(capture_off)
        self._rest()

    def _half_touched(self, half: str) -> bool:
        h = self.halves[half]
        if not h.spans:
            return bool(self.work)
        return bool(_intersect(self.work, h.spans))

    def _rules_on_day(self) -> list[engine.PlacedBreak]:
        return [b for inst in self.i.instances for b in inst.breaks]

    def _breaks_step(self) -> None:
        """Step 5: punched breaks matched to rules, fixed windows, floating windows, auto-deduct (D-AS-40)."""
        rules = self._rules_on_day()
        matched: set[int] = set()
        unpaid: list[Span] = []
        punched_union: list[Span] = []
        for b, span in self._breaks:
            clipped = _intersect([span], self.work)
            if not clipped:
                continue
            punched_union.extend(clipped)
            rule = self._match(b, span, rules)
            if rule is None:
                if not b.is_paid:
                    unpaid.extend(clipped)
                continue
            matched.add(id(rule))
            covered_end = span[0] + dt.timedelta(minutes=rule.minutes)
            if not b.is_paid:
                unpaid.extend(clipped)
            elif span[1] > covered_end:
                unpaid.extend(_intersect([(covered_end, span[1])], self.work))  # UNPAID_EXCESS (AS-BRK-05)
            if span[1] > covered_end:
                self.exceptions.add("BREAK_OVERSTAY")
            window = self._window(rule)
            if window is not None and not _intersect([span], [window]):
                self.exceptions.add("BREAK_OFF_WINDOW")
        for rule in rules:
            if rule.mode == engine.FIXED_WINDOW and rule.start_at is not None:
                inside = _intersect([(rule.start_at, rule.end_at)], self.work)
                if not rule.paid:
                    unpaid.extend(inside)
                if id(rule) not in matched and inside and not rule.paid and any(
                        s.check_in_at <= rule.start_at and (s.check_out_at or self.i.now) >= rule.end_at
                        and s.capture_source in INTERACTIVE_SOURCES for s in self.i.sessions):
                    self.exceptions.add("BREAK_NOT_TAKEN")
        gross_minutes = self.gross // 60 if self.gross is not None else 0
        for rule in rules:
            if rule.mode == engine.FLOATING_WINDOW and id(rule) not in matched:
                window = self._window(rule)
                if window is None or not _intersect([window], self.work):
                    continue
                if rule.auto_deduct_after_minutes is not None and gross_minutes >= rule.auto_deduct_after_minutes:
                    if not rule.paid:
                        self.auto_seconds += max(0, rule.minutes * 60 - _length(unpaid) - self.auto_seconds)
                elif rule.auto_deduct_after_minutes is None:
                    self.exceptions.add("BREAK_MISSED")
        for rule in rules:
            if rule.mode == engine.AUTO_DEDUCT and rule.auto_deduct_after_minutes is not None:
                if gross_minutes >= rule.auto_deduct_after_minutes and not rule.paid:
                    self.auto_seconds += max(0, rule.minutes * 60 - _length(unpaid) - self.auto_seconds)
        self.unpaid = _union(unpaid)
        self.break_spans = _union(list(punched_union) + list(self.unpaid))

    @staticmethod
    def _window(rule: engine.PlacedBreak) -> Optional[Span]:
        if rule.mode == engine.FIXED_WINDOW and rule.start_at is not None:
            return rule.start_at, rule.end_at
        if rule.mode == engine.FLOATING_WINDOW and rule.window_start_at is not None:
            return rule.window_start_at, rule.window_end_at
        return None

    def _match(self, b: BreakData, span: Span, rules: Sequence[engine.PlacedBreak]) -> Optional[engine.PlacedBreak]:
        """AS-BRK-02: the rule of the break's kind whose window it overlaps, else the nearest of that kind."""
        same = [r for r in rules if r.kind == b.kind]
        if not same:
            return None
        for rule in same:
            window = self._window(rule)
            if window is not None and _intersect([span], [window]):
                return rule

        def distance(rule: engine.PlacedBreak) -> float:
            window = self._window(rule)
            if window is None:
                return 0.0
            return min(abs((span[0] - window[0]).total_seconds()), abs((span[0] - window[1]).total_seconds()))

        return min(same, key=distance)

    def _marks(self) -> None:
        """Step 7: late / early on raw instants against the expectation; excuses and waivers keep the mark."""
        i, r = self.i, self.r
        kinds = {inst.kind for inst in i.instances}
        if not i.instances or engine.OPEN in kinds:
            return
        start, end = self.expected_start, self.expected_end
        if engine.FLEXIBLE in kinds:
            if r.flexible_late_basis != "CORE_START":
                return
            core = [inst for inst in i.instances if inst.core_start_at is not None]
            if not core:
                return
            start, end = core[0].core_start_at, core[-1].core_end_at
        grace_in = next(v for v in (i.shift_grace_in_minutes, r.grace_in_minutes, i.settings.grace_minutes)
                        if v is not None)
        grace_out = next(v for v in (i.shift_grace_out_minutes, r.grace_out_minutes, 0) if v is not None)
        if start is not None and self.first_in is not None:
            self.late_seconds = max(0, int((self.first_in - start).total_seconds()))
            self.late_mark = self.late_seconds > grace_in * 60
        if end is not None and self.last_out is not None:
            self.early_seconds = max(0, int((end - self.last_out).total_seconds()))
            self.early_mark = self.early_seconds > grace_out * 60
        for e in i.excuses:
            if e.kind == "LATE" and self.late_mark and self.late_seconds // 60 <= e.minutes:
                self.late_excused, self.late_source = True, "REQUEST"
            if e.kind == "EARLY" and self.early_mark and self.early_seconds // 60 <= e.minutes:
                self.early_excused, self.early_source = True, "REQUEST"
        if self.late_mark and not self.late_excused and self._waived(r.late_waiver):
            self.late_excused, self.late_source = True, "POLICY"
        if self.early_mark and not self.early_excused and self._waived(r.early_waiver):
            self.early_excused, self.early_source = True, "POLICY"

    def _waived(self, mode: str) -> bool:
        expected = (self.expected_minutes or 0) * 60
        if mode == "NET_GE_EXPECTED":
            return self.net is not None and self.net >= expected
        if mode == "GROSS_GE_EXPECTED":
            return self.gross is not None and self.gross_known and self.gross >= expected
        return False

    def _thresholds(self) -> tuple[Decimal, Decimal]:
        r = self.r
        if r.threshold_mode == "ABSOLUTE":
            return Decimal(r.full_day_min_minutes), Decimal(r.half_day_min_minutes)
        expected = Decimal(self.expected_minutes or 0)
        return (Decimal(r.full_day_min_percent) * expected / 100, Decimal(r.half_day_min_percent) * expected / 100)

    def _flexible(self) -> bool:
        return any(inst.kind in (engine.FLEXIBLE, engine.OPEN) for inst in self.i.instances)

    def _portions(self, judged: Sequence[str]) -> set[str]:
        """Step 8 on the day's value (no rounding of the comparison)."""
        if not judged:
            return set()
        r = self.r
        if r.day_basis == "PRESENCE":
            if self._flexible():
                return set(judged) if self.work else set()
            return {h for h in judged if self._worked_in(h, self.work, self.unpaid) > 0}
        seconds = self.net if r.hours_basis == "NET" else self.gross
        value = _decimal_minutes(seconds)
        full, half = self._thresholds()
        if len(judged) == 2:
            if value >= full:
                return set(judged)
            if value >= half:
                w1 = self._worked_in(FIRST_HALF, self.work, self.unpaid)
                w2 = self._worked_in(SECOND_HALF, self.work, self.unpaid)
                chosen = FIRST_HALF if w1 >= w2 else SECOND_HALF
                self.unworked_by_rule.add(SECOND_HALF if chosen == FIRST_HALF else FIRST_HALF)
                return {chosen}
            self.unworked_by_rule |= set(judged)
            return set()
        if value >= half:
            return set(judged)
        self.unworked_by_rule |= set(judged)
        return set()

    def _portions_with_open_session(self, judged: Sequence[str]) -> set[str]:
        """Step 12 (AS-EVL-05): halves before the open IN are judged from the closed sessions only."""
        missing = set(self._missing_halves(judged))
        present: set[str] = set()
        _, half = self._thresholds()
        expected = self.expected_minutes or 0
        for h in judged:
            if h in missing:
                continue
            worked = self._worked_in(h, self.work, self.unpaid)
            if self.r.day_basis == "PRESENCE":
                ok = worked > 0
            else:
                minutes = Decimal(self.halves[h].minutes or 0)
                needed = half * minutes / Decimal(expected) if expected else half
                ok = _decimal_minutes(worked) >= needed
            if ok:
                present.add(h)
        return present

    def _missing_halves(self, judged: Sequence[str]) -> list[str]:
        if not self.open_after_close:
            return []
        first_open = min(s.check_in_at for s in self.open_after_close)
        order = [h for h in HALVES if h in judged]
        if not order:
            return []
        if not all(self.halves[h].spans for h in order):
            return order
        start_index = 0
        for index, h in enumerate(order):
            if any(s <= first_open < e for s, e in self.halves[h].spans) or first_open >= self.halves[h].spans[0][0]:
                start_index = index
        return order[start_index:]

    def _beyond(self, judged: Sequence[str], present: set[str]) -> None:
        """Step 9: lateness / earliness beyond the half-day limits converts the mark (D-AS-78, INV-18)."""
        r = self.r
        if not judged:
            return
        if (r.late_half_day_after_minutes is not None and self.late_seconds is not None and self.late_mark
                and not self.late_excused and self.late_seconds > r.late_half_day_after_minutes * 60):
            present.discard(judged[0])
            self.unworked_by_rule.add(judged[0])
            self.late_mark, self.late_converted = False, True
        if (r.early_half_day_before_minutes is not None and self.early_seconds is not None and self.early_mark
                and not self.early_excused and self.early_seconds > r.early_half_day_before_minutes * 60):
            present.discard(judged[-1])
            self.unworked_by_rule.add(judged[-1])
            self.early_mark, self.early_converted = False, True

    def _non_working_day(self, has_work: bool) -> None:
        """Expected NONE: portions OFF; worked -> PRESENT unless below the non-working-day minimum (step 18)."""
        reasons = self.i.expectation.reasons
        if self.not_employed or not has_work:
            return
        if self.open_after_close:
            self.exceptions.add("MISSING_PUNCH_OUT")
        if "LEAVE" in reasons:
            self.exceptions.add("WORKED_ON_LEAVE")
        elif "HOLIDAY" in reasons or "WEEKLY_OFF" in reasons:
            self.exceptions.add("NON_WORKING_DAY_WORKED")
        else:
            self.exceptions.add("UNSCHEDULED_WORK")

    def _non_working_minimum(self) -> Decimal:
        r = self.r
        if r.non_working_day_min_minutes is not None:
            return Decimal(r.non_working_day_min_minutes)
        if r.threshold_mode == "ABSOLUTE":
            return Decimal(r.half_day_min_minutes)
        scheduled = sum(inst.scheduled_net_minutes or 0 for inst in self.i.instances)
        return Decimal(r.half_day_min_percent) * Decimal(scheduled) / 100

    def _night(self) -> None:
        """Step 14: |W ∩ N| − |U ∩ N| in the work zone (JC-AS-06)."""
        if not self.work:
            return
        night = _night_spans(self.i, max(e for _, e in self.work))
        self.night_seconds = max(0, _length(_intersect(self.work, night)) - _length(_intersect(self.unpaid, night)))

    def _ot(self, capture_off: bool) -> None:
        """Steps 15-16: OT-eligible seconds and the daily OT candidate (minutes, never money)."""
        i, r = self.i, self.r
        if self.net is None:
            return
        if not self.expected or self.expected_start is None:
            eligible = self.net
        else:
            pre_work = _before(self.work, self.expected_start)
            pre = max(0, _length(pre_work) - _length(_intersect(self.unpaid, pre_work)))
            post_work = _after(self.work, self.expected_end)
            post = max(0, _length(post_work) - _length(_intersect(self.unpaid, post_work)))
            if r.ot_early_arrival == "ALWAYS" or (r.ot_early_arrival == "WITH_APPROVAL" and i.early_arrival_approved):
                pre = 0
            if post > r.ot_post_shift_buffer_minutes * 60:
                post = 0
            eligible = max(0, self.net - pre - post)
        # AS-OT-11: worked time inside halves an override set ABSENT / UNMARKED is not eligible; when the override
        # makes every expected half not worked, nothing of the day is.
        overridden = {h for o in i.overrides for h, value in ((FIRST_HALF, o.first_half), (SECOND_HALF, o.second_half))
                      if value in (ABSENT, UNMARKED) and h in self.expected}
        if overridden and eligible:
            if overridden == set(self.expected):
                eligible = 0
            else:
                eligible = max(0, eligible - sum(self._worked_in(h, self.work, self.unpaid) for h in overridden))
            self.exceptions.add("OVERTIME_ON_ABSENT_DAY")
        self.ot_eligible = eligible
        view = i.ot_view
        if not view.enabled or not view.daily_applies:
            return
        reasons = i.expectation.reasons
        if not self.expected and ("HOLIDAY" in reasons or "WEEKLY_OFF" in reasons or "LEAVE" not in reasons):
            switch = view.holiday_work_premium if "HOLIDAY" in reasons else view.weekly_off_pay
            scheduled = sum(inst.scheduled_net_minutes or 0 for inst in i.instances)
            threshold = scheduled * 60 if switch else 0
        else:
            minutes = view.daily_threshold_minutes if view.daily_threshold_minutes is not None else i.scheduled_minutes
            threshold = minutes * 60
        self.ot_candidate = max(0, eligible - threshold) // 60
        if self.ot_candidate and r.ot_requires_pre_approval:
            self.exceptions.add("OVERTIME_UNAPPROVED")

    def _rest(self) -> None:
        """Step 17: rest from the previous shift date's last OUT (pack ``rest``; no pack = no check)."""
        i = self.i
        if i.rest_min_between_shifts_hours is None or i.previous_last_out_at is None or self.first_in is None:
            return
        rest = Decimal(int((self.first_in - i.previous_last_out_at).total_seconds())) / 3600
        if rest < Decimal(i.rest_min_between_shifts_hours):
            self.exceptions.add("REST_VIOLATION")

    # ----------------------------------------------------------------- LEGACY_V1 (exact port, §2.4)

    def legacy(self) -> None:
        """Today's outcome: one session, ``attendance_hours`` arithmetic, payroll's own policy per period."""
        i, lp = self.i, self.i.legacy
        self._state()
        self._expected_window()
        sessions = sorted(i.sessions, key=lambda s: s.check_in_at)
        session = sessions[-1] if sessions else None  # last row wins (time_inputs._attendance_inputs)
        if session is not None:
            out = session.check_out_at
            closed_without_out = out is None and (session.closed_at is not None or self.after_close)
            if out is None and not closed_without_out:
                out = i.now
            self.first_in = session.check_in_at
            if out is not None and out > session.check_in_at:
                self.work = [(session.check_in_at, out)]
                unpaid = []
                for b in session.breaks:
                    end = b.ended_at or out
                    if not b.is_paid and end > b.started_at:
                        unpaid.append((b.started_at, end))
                self.unpaid = _intersect(unpaid, self.work)
                self.gross = _length(self.work)
                self.net = self.net_closed = max(0, self.gross - _length(self.unpaid))
                self.last_out = session.check_out_at
            if closed_without_out:
                self.open_after_close = [session]
                self.gross = self.net = None
            self.late_mark = session.legacy_status == "late"
            self.late_seconds = (session.late_minutes or 0) * 60 if session.late_minutes is not None else None
            self.early_mark = bool(session.early_exit) and (session.early_exit_minutes or 0) > lp.early_exit_grace_minutes
            self.early_seconds = ((session.early_exit_minutes or 0) * 60
                                  if session.early_exit or session.early_exit_minutes else None)
        if not self.expected:
            self._non_working_day(session is not None)
            self._night()
            return
        live = session is not None and session.check_out_at is None and not self.open_after_close
        if session is None:
            outcome = UNMARKED if self.state != OPEN else PENDING
        elif live:
            outcome = PENDING
        elif session.legacy_status == "absent":
            outcome = ABSENT
        elif self.open_after_close:
            outcome = MISSING_PUNCH
        elif lp.derive_status_from_hours and self.net is not None:
            worked = Decimal(self.net)
            if worked >= lp.full_day_min_hours * 3600:
                outcome = PRESENT
            elif worked >= lp.half_day_min_hours * 3600:
                outcome = HALF_DAY
            else:
                outcome = ABSENT
        elif session.legacy_status == "half-day":
            outcome = HALF_DAY
        else:
            outcome = PRESENT
        first, second = self.expected[0], self.expected[-1]
        if outcome == HALF_DAY and len(self.expected) == 2:
            self.result_of[first], self.basis_of[first] = PRESENT, SESSION
            self.result_of[second] = ABSENT
        else:
            for h in self.expected:
                if outcome == PENDING:
                    self.result_of[h] = PENDING
                    self.pending_of[h] = [SHIFT_IN_PROGRESS]
                else:
                    self.result_of[h] = PRESENT if outcome == HALF_DAY else outcome
                    self.basis_of[h] = SESSION if outcome in (PRESENT, HALF_DAY) else None
        if outcome == MISSING_PUNCH:
            self.exceptions.add("MISSING_PUNCH_OUT")
        if outcome == UNMARKED and self.state == FINAL:
            self.exceptions.add("UNMARKED_DAY")
        if any(self.result_of[h] == ABSENT for h in self.expected):
            self.exceptions.add("ABSENT")
        self._night()  # display only; the payroll input stays 0 until STANDARD (JC-AS-06)

    # ----------------------------------------------------------------- result

    def _day_result(self) -> str:
        """Step 18."""
        results = [self.result_of[h] for h in self.expected]
        if not self.expected:
            if self.work and self.net is not None and not self.not_employed:
                if self.rules_mode_is_standard() and _decimal_minutes(self.net) < self._non_working_minimum():
                    self.exceptions.add("SHORT_VISIT")
                    return OFF
                return PRESENT
            if self.open_after_close and not self.not_employed:
                return MISSING_PUNCH
            return OFF
        if MISSING_PUNCH in results:
            return MISSING_PUNCH
        if PENDING in results:
            return PENDING
        if len(results) == 1:
            return results[0]
        a, b = results
        if a == PRESENT and b == PRESENT:
            return PRESENT
        if PRESENT in (a, b):
            return HALF_DAY
        if a == b:
            return a
        return ABSENT

    def rules_mode_is_standard(self) -> bool:
        return self.i.rules_mode == STANDARD

    def result(self) -> DayEvaluation:
        i = self.i
        day_result = self._day_result()
        if self.late_mark and not self.late_excused:
            self.exceptions.add("LATE_IN")
        if self.early_mark and not self.early_excused:
            self.exceptions.add("EARLY_OUT")
        bases = [self.basis_of[h] for h in self.expected if self.result_of[h] == PRESENT and self.basis_of[h]]
        presence_basis = next((b for b in _BASIS_PRIORITY if b in bases), None)
        work_mode = None
        if i.sessions:
            by_mode: dict[str, int] = {}
            for s in i.sessions:
                end = s.check_out_at or (i.now if not self.after_close else s.check_in_at)
                by_mode[s.work_mode] = by_mode.get(s.work_mode, 0) + max(0, int((end - s.check_in_at).total_seconds()))
            work_mode = max(sorted(by_mode), key=lambda m: by_mode[m])
        pending = tuple(dict.fromkeys(reason for h in HALVES for reason in self.pending_of[h]))
        first_instance = i.instances[0] if i.instances else None
        unpaid_seconds = _length(self.unpaid) + self.auto_seconds
        gross = self.gross
        known = self.net is not None
        values = dict(
            employee_id=i.employee_id, shift_date=i.shift_date, work_timezone=i.work_timezone,
            legal_entity_id=i.legal_entity_id, branch_id=i.branch_id,
            evaluation_state=self.state, rules_mode=i.rules_mode,
            policy_version_ids=dict(sorted(i.policy_version_ids.items())),
            calendar_version=i.expectation.calendar_version, work_source=i.expectation.work_source,
            shift_source=i.shift_source,
            shift_id=first_instance.shift_id if first_instance else None,
            shift_version_id=first_instance.shift_version_id if first_instance else None,
            roster_entry_id=first_instance.roster_entry_id if first_instance else None,
            roster_version=first_instance.roster_version if first_instance else None,
            expected_portion=i.expectation.expected_portion if not self.not_employed else "NONE",
            expected_minutes=self.expected_minutes,
            expected_start_at=self.expected_start, expected_end_at=self.expected_end,
            session_count=len(i.sessions), first_in_at=self.first_in, last_out_at=self.last_out,
            gross_seconds=gross if known else None, net_seconds=self.net,
            gross_minutes=_minutes(gross) if known else None,
            break_minutes=(_length(self.break_spans) + self.auto_seconds) // 60 if known else None,
            unpaid_break_minutes=unpaid_seconds // 60 if known else None,
            net_minutes=_minutes(self.net),
            auto_deducted_minutes=self.auto_seconds // 60, rounding_delta_minutes=self.rounding_delta,
            late_minutes=_minutes(self.late_seconds), early_minutes=_minutes(self.early_seconds),
            short_minutes=self.short_minutes,
            late_mark=self.late_mark, late_excused=self.late_excused and self.late_mark,
            late_excuse_source=self.late_source if self.late_excused and self.late_mark else None,
            early_mark=self.early_mark, early_excused=self.early_excused and self.early_mark,
            early_excuse_source=self.early_source if self.early_excused and self.early_mark else None,
            short_mark=self.short_mark, late_converted=self.late_converted, early_converted=self.early_converted,
            night_minutes=self.night_seconds // 60, ot_eligible_seconds=self.ot_eligible,
            ot_eligible_minutes=self.ot_eligible // 60, ot_candidate_minutes=self.ot_candidate,
            meal_violations=0, rest_violations=0,
            first_half_result=self.result_of[FIRST_HALF], second_half_result=self.result_of[SECOND_HALF],
            day_result=day_result, presence_basis=presence_basis, work_mode=work_mode,
            exception_codes=tuple(code for code in EXCEPTION_CODES if code in self.exceptions),
            pending_reasons=pending, override_request_id=self.override_request_id,
            net_closed_seconds=self.net_closed if self.net is None else None,
            explain=tuple(self.explain),
        )
        values["content_hash"] = _sha({k: values[k] for k in CONTENT_FIELDS})
        values["inputs_hash"] = inputs_hash(i)
        return DayEvaluation(**values)


# --------------------------------------------------------------------------- hashes (§8.2)


def phase(inputs: DayInputs) -> str:
    return "AFTER_CLOSE" if inputs.now >= _close_point(inputs) else "BEFORE_CLOSE"


def inputs_hash(inputs: DayInputs) -> str:
    """SHA-256 of the canonical inputs (§8.2). ``now`` is not hashed, only the phase: a live day is re-evaluated by
    its punches and by day close, never by a timer."""
    exp = inputs.expectation
    payload = {
        "engine": EVALUATION_ENGINE_VERSION,
        "employee_id": inputs.employee_id,
        "shift_date": inputs.shift_date,
        "placement": {"legal_entity_id": inputs.legal_entity_id, "branch_id": inputs.branch_id,
                      "work_timezone": inputs.work_timezone},
        "expectation": {"expected_portion": exp.expected_portion, "expected_minutes": exp.expected_minutes,
                        "reasons": sorted(exp.reasons), "calendar_version": exp.calendar_version,
                        "work_source": exp.work_source},
        "instances": [{"shift_version_id": x.shift_version_id, "roster_entry_id": x.roster_entry_id,
                       "roster_version": x.roster_version, "start": x.start_at, "end": x.end_at,
                       "kind": x.kind} for x in inputs.instances],
        "shift_source": inputs.shift_source,
        "grace": [inputs.shift_grace_in_minutes, inputs.shift_grace_out_minutes],
        "scheduled_minutes": inputs.scheduled_minutes,
        "sessions": sorted(({"id": s.id, "content_hash": s.content_hash or _sha(_session_plain(s))}
                            for s in inputs.sessions), key=lambda x: x["id"]),
        "punch_ids": sorted(inputs.punch_ids),
        "on_duty": sorted(({"id": d.id, "row_version": d.row_version, "kind": d.kind, "portion": d.portion}
                           for d in inputs.on_duty), key=lambda x: x["id"]),
        "requests": sorted([{"id": o.request_id, "version": o.version} for o in inputs.overrides]
                           + [{"id": e.request_id, "version": e.version} for e in inputs.excuses],
                           key=lambda x: x["id"]),
        "policy_version_ids": dict(sorted(inputs.policy_version_ids.items())),
        "rules": {f.name: getattr(inputs.rules, f.name) for f in fields(RulesParams)},
        "legacy": {f.name: getattr(inputs.legacy, f.name) for f in fields(LegacyPayParams)},
        "settings": {f.name: getattr(inputs.settings, f.name) for f in fields(EvalSettings)},
        "capture": {"required": inputs.capture_required, "shift_required": inputs.shift_required},
        "pack": {"rest": inputs.rest_min_between_shifts_hours, "previous_last_out": inputs.previous_last_out_at},
        "payroll_ot_view": {f.name: getattr(inputs.ot_view, f.name) for f in fields(OvertimeView)},
        "watermarks": {"device_ids_behind": sorted(inputs.device_ids_behind),
                       "late_log_grace_elapsed": inputs.late_log_grace_elapsed},
        "pending": {"request": inputs.request_pending, "leave": LEAVE_PENDING in exp.reasons,
                    "not_evaluated": inputs.not_evaluated},
        "early_arrival_approved": inputs.early_arrival_approved,
        "rules_mode": inputs.rules_mode,
        "phase": phase(inputs),
    }
    return _sha(payload)


def _session_plain(s: SessionData) -> dict:
    return {"in": s.check_in_at, "out": s.check_out_at, "closed": s.closed_at,
            "breaks": [(b.kind, b.started_at, b.ended_at, b.is_paid) for b in s.breaks],
            "source": s.capture_source, "mode": s.work_mode, "status": s.legacy_status}


def changed_outputs(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict:
    """Output columns whose value differs (later ``attendance_day_versions`` rows store only these)."""
    return {k: v for k, v in new.items() if old.get(k) != v}


def with_now(inputs: DayInputs, now: dt.datetime) -> DayInputs:
    return replace(inputs, now=now)
