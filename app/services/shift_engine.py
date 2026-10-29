"""Shift engine: concrete shift instances, scheduled minutes and half-day boundaries (AS-M1, WI-AS-1.2).

Owner doc ``docs/attendance-shift/03-shifts/01-shift-definitions.md``: kinds §2.2,
version columns §3.2, legacy mirrors §3.8, segments §4, break rules §5,
half-day boundary §6, anchor time zone §8, instance construction §9, scheduled
minutes §10 and the golden vectors ``GV-AS-SHF-*`` of §21. The zone-change
switch rule (EC AS-TZ-02) is ``02-system-design.md`` §7.2 item 4.

Pure (spine §1.4 L0, implementation guide IG-AS-02): no database, no clock, no
settings reads. Callers pass the resolved version (or a roster entry's custom
times), the employee-local shift date, the work zone and any policy value the
rule needs (``open_day_start_time``, the standard-hours fallback). The preview
path and the real path run the same code.

Conventions (guide §2 C-6): dates are employee-local, instants are aware UTC
datetimes, durations are whole seconds and minutes are ``floor(seconds / 60)``;
an unknown value is ``None``, never 0.

Local time to instant (§9.2 step 4): a non-existent local time (spring-forward
gap) moves forward by the gap; an ambiguous one (fall-back overlap) takes the
first instant (``fold = 0``). The virtualenv has no ``tzdata`` for ``zoneinfo``
on Windows, so zones come from ``pytz``; resolution is computed from the zone's
UTC offsets rather than pytz's ``is_dst`` flag, which gives exactly the
``zoneinfo`` ``fold = 0`` result.

Clock times are placed on a timeline of minutes from 00:00 of the anchor date
in the clock zone (anchor zone or work zone): segment day carry follows §4.1,
break ``next_day`` adds one day, and a ``FIXED_WINDOW`` break is its start
instant plus ``minutes`` so a DST change never alters its length (EC AS-SHF-11).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytz

SHIFT_KINDS = ("FIXED", "FLEXIBLE", "OPEN", "SPLIT")
SEGMENT_KINDS = ("WORK", "ON_CALL")
BREAK_KINDS = ("LUNCH", "REST", "PERSONAL", "OTHER")
BREAK_MODES = ("PUNCHED", "FIXED_WINDOW", "FLOATING_WINDOW", "AUTO_DEDUCT")
FIXED, FLEXIBLE, OPEN, SPLIT = SHIFT_KINDS
WORK, ON_CALL = SEGMENT_KINDS
PUNCHED, FIXED_WINDOW, FLOATING_WINDOW, AUTO_DEDUCT = BREAK_MODES

DEFAULT_EARLY_IN_WINDOW_MINUTES = 120
DEFAULT_LATE_OUT_WINDOW_MINUTES = 240
DEFAULT_OPEN_DAY_START_TIME = "04:00"
MAX_SEGMENTS = 4
MAX_BREAK_RULES = 4
MIN_SPLIT_GAP_MINUTES = 30
MIN_CORE_MINUTES = 30
DAY_MINUTES = 1440

# ``explain[]`` codes (§9.4). ``BREAK_WINDOW_CLIPPED`` is registered by SP-AS-205; the rest name the rule that fired.
HALVES_SPLIT_TIME = "HALVES_SPLIT_TIME"  # §6.1 rule 1
HALVES_FIXED_BREAK = "HALVES_FIXED_BREAK"  # §6.1 rule 2
HALVES_CENTRED = "HALVES_CENTRED"  # §6.1 rule 3 (binding fallback, LH-WWK-01)
HALVES_SPLIT_SEGMENTS = "HALVES_SPLIT_SEGMENTS"  # §6.1 SPLIT row
HALVES_CORE_MIDPOINT = "HALVES_CORE_MIDPOINT"  # §6.1 FLEXIBLE row
HALVES_BAND_MIDPOINT = "HALVES_BAND_MIDPOINT"  # FLEXIBLE without a core window (refused at save)
HALVES_OPEN_WORKED = "HALVES_OPEN_WORKED"  # §6.1 OPEN row: no clock boundary
HALF_SPLIT_IGNORED = "HALF_SPLIT_IGNORED"
DST_GAP_FORWARD = "DST_GAP_FORWARD"
DST_AMBIGUOUS_FIRST = "DST_AMBIGUOUS_FIRST"
BREAK_WINDOW_CLIPPED = "BREAK_WINDOW_CLIPPED"
BREAK_AS_PUNCHED = "BREAK_AS_PUNCHED"  # §5.4: a window outside a custom-time entry is expected, not placed
ANCHOR_DATE_PREVIOUS = "ANCHOR_DATE_PREVIOUS"
ANCHOR_DATE_NEXT = "ANCHOR_DATE_NEXT"
ANCHOR_DATE_UNRESOLVED = "ANCHOR_DATE_UNRESOLVED"

UTC = dt.timezone.utc
_ONE_DAY = dt.timedelta(days=1)
_OFFSET_PROBE = dt.timedelta(hours=30)
_HOURS_2DP = Decimal("0.01")


class ShiftEngineError(ValueError):
    """Malformed engine input; ``code`` is stable for API error mapping, ``field`` names the input."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.field = field


# --------------------------------------------------------------------------- inputs (mirror the columns, no ORM)


@dataclass(frozen=True)
class SegmentData:
    """One ``shift_versions.segments[]`` item (§4.1); ``next_day`` is the stored flag, ``None`` = not given."""

    kind: str
    start: str | dt.time
    end: str | dt.time
    next_day: bool | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> SegmentData:
        return cls(kind=raw.get("kind", WORK), start=raw["start"], end=raw["end"], next_day=raw.get("next_day"))


@dataclass(frozen=True)
class BreakRuleData:
    """One BreakRule of ``shift_versions.break_rules[]`` (§5.1)."""

    kind: str
    mode: str
    minutes: int
    paid: bool
    start: str | dt.time | None = None  # FIXED_WINDOW
    end: str | dt.time | None = None  # FIXED_WINDOW; end - start = minutes
    window_start: str | dt.time | None = None  # FLOATING_WINDOW
    window_end: str | dt.time | None = None
    next_day: bool = False  # the break's times are on the day after the anchor date
    auto_deduct_after_minutes: int | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> BreakRuleData:
        return cls(
            kind=raw["kind"], mode=raw["mode"], minutes=raw["minutes"], paid=bool(raw["paid"]),
            start=raw.get("start"), end=raw.get("end"), window_start=raw.get("window_start"),
            window_end=raw.get("window_end"), next_day=bool(raw.get("next_day", False)),
            auto_deduct_after_minutes=raw.get("auto_deduct_after_minutes"),
        )


def _segments_tuple(items: Iterable[Any]) -> tuple[SegmentData, ...]:
    return tuple(SegmentData.from_json(item) if isinstance(item, Mapping) else item for item in items or ())


def _rules_tuple(items: Iterable[Any]) -> tuple[BreakRuleData, ...]:
    return tuple(BreakRuleData.from_json(item) if isinstance(item, Mapping) else item for item in items or ())


@dataclass(frozen=True)
class ShiftVersionData:
    """The business columns of one ``shift_versions`` row plus the shift's ``kind`` (spine §3.4, AS 11 §4.4).

    ``segments`` and ``break_rules`` accept the stored JSON dicts and are kept as tuples of dataclasses.
    """

    kind: str = FIXED
    start_time: str | dt.time | None = None
    end_time: str | dt.time | None = None
    segments: tuple[SegmentData, ...] = ()
    break_rules: tuple[BreakRuleData, ...] = ()
    unpaid_break_minutes: int | None = None  # legacy value, used only when break_rules is empty
    half_split_time: str | dt.time | None = None
    core_start_time: str | dt.time | None = None
    core_end_time: str | dt.time | None = None
    expected_net_minutes: int | None = None
    grace_in_minutes: int | None = None
    grace_out_minutes: int | None = None
    early_in_window_minutes: int = DEFAULT_EARLY_IN_WINDOW_MINUTES
    late_out_window_minutes: int = DEFAULT_LATE_OUT_WINDOW_MINUTES
    anchor_timezone: str | None = None
    long_duty: bool = False
    safety_critical: bool = False
    outdoor: bool = False
    shift_id: int | None = None
    shift_version_id: int | None = None
    version_no: int | None = None
    effective_from: dt.date | None = None
    effective_to: dt.date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", _segments_tuple(self.segments))
        object.__setattr__(self, "break_rules", _rules_tuple(self.break_rules))

    @property
    def crosses_midnight(self) -> bool | None:
        """Stored CHECK value ``end_time <= start_time``; ``None`` when a time is missing (OPEN)."""
        if self.start_time is None or self.end_time is None:
            return None
        return _clock(self.end_time, "end_time") <= _clock(self.start_time, "start_time")


@dataclass(frozen=True)
class RosterEntryData:
    """The instance-relevant columns of a published ``roster_entries`` row (AS 11 §4.9).

    ``break_defaults`` are the resolved ``ATTENDANCE_RULES_POLICY.break_defaults`` for a custom-time
    entry without ``shift_id`` (§5.4, §9.1); the engine never reads policies itself.
    """

    roster_entry_id: int | None = None
    version: int | None = None
    shift_id: int | None = None
    shift_version_id: int | None = None
    custom_start_time: str | dt.time | None = None
    custom_end_time: str | dt.time | None = None
    custom_segments: tuple[SegmentData, ...] = ()
    break_defaults: tuple[BreakRuleData, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "custom_segments", _segments_tuple(self.custom_segments))
        object.__setattr__(self, "break_defaults", _rules_tuple(self.break_defaults))

    @property
    def has_custom_times(self) -> bool:
        return bool(self.custom_segments) or self.custom_start_time is not None


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True)
class InstanceSegment:
    kind: str
    start_at: dt.datetime
    end_at: dt.datetime


@dataclass(frozen=True)
class PlacedBreak:
    """A break rule placed on the instance (§5.2): FIXED_WINDOW has start/end, FLOATING_WINDOW its window."""

    index: int  # position in the rules the instance used
    kind: str
    mode: str
    paid: bool
    minutes: int
    start_at: dt.datetime | None = None
    end_at: dt.datetime | None = None
    window_start_at: dt.datetime | None = None
    window_end_at: dt.datetime | None = None
    auto_deduct_after_minutes: int | None = None


@dataclass(frozen=True)
class HalfData:
    """One half of the day (§6.1); OPEN halves have no instants ("first N worked minutes")."""

    start_at: dt.datetime | None
    end_at: dt.datetime | None
    minutes: int | None  # before FTE; None when the scheduled net is unknown (standard-hours fallback)


@dataclass(frozen=True)
class ShiftInstance:
    """One concrete occurrence of a version on a shift date (§9.4; field names SP-AS-035)."""

    shift_id: int | None
    shift_version_id: int | None
    roster_entry_id: int | None
    roster_version: int | None
    kind: str
    shift_date: dt.date
    work_timezone: str
    clock_timezone: str
    anchor_date: dt.date
    start_at: dt.datetime | None  # None for OPEN (no clock times)
    end_at: dt.datetime | None
    crosses_midnight: bool
    segments: tuple[InstanceSegment, ...]
    breaks: tuple[PlacedBreak, ...]
    core_start_at: dt.datetime | None
    core_end_at: dt.datetime | None
    first_half: HalfData
    second_half: HalfData
    scheduled_net_minutes: int | None  # before FTE; None = standard-hours fallback applies
    gross_minutes: int | None  # WORK segments only; None for FLEXIBLE / OPEN
    match_start_at: dt.datetime
    match_end_at: dt.datetime
    explain: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegacyMirror:
    """The ``shifts`` mirror columns for legacy readers (§3.8)."""

    start_time: str
    end_time: str
    duration_hours: Decimal | None
    unpaid_break_minutes: int | None
    is_flexible: bool


@dataclass(frozen=True)
class VersionIssue:
    """A field error of :func:`validate_version` (422 ``VALIDATION_ERROR`` at the API)."""

    field: str
    code: str
    message: str


@dataclass(frozen=True)
class ZoneSwitch:
    """Which placement owns an instance across a dated work-zone change (EC AS-TZ-02)."""

    placement: str  # "OLD" | "NEW"
    work_timezone: str
    shift_date: dt.date


# --------------------------------------------------------------------------- parsing and zones


def _clock(value: object, what: str) -> int:
    """``"HH:MM"`` (00:00-23:59) or a ``time`` -> minutes after midnight."""
    if isinstance(value, dt.time):
        return value.hour * 60 + value.minute
    if isinstance(value, str):
        parts = value.strip().split(":")
        if len(parts) == 2 and all(len(p) == 2 and p.isdigit() for p in parts):
            hour, minute = int(parts[0]), int(parts[1])
            if hour < 24 and minute < 60:
                return hour * 60 + minute
    raise ShiftEngineError("INVALID_TIME", f"{what} must be HH:MM between 00:00 and 23:59, got {value!r}", what)


def _zone(name: object, what: str) -> dt.tzinfo:
    if not isinstance(name, str) or not name:
        raise ShiftEngineError("INVALID_TIMEZONE", f"{what} must be an IANA zone name", what)
    try:
        return pytz.timezone(name)
    except pytz.UnknownTimeZoneError:
        raise ShiftEngineError("INVALID_TIMEZONE", f"{what} {name!r} is not a known IANA zone", what) from None


def _require_date(value: object, what: str) -> dt.date:
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        raise ShiftEngineError("INVALID_DATE", f"{what} must be a date, got {type(value).__name__}", what)
    return value


def _wall_of(zone: dt.tzinfo, naive_utc: dt.datetime) -> dt.datetime:
    return pytz.utc.localize(naive_utc).astimezone(zone).replace(tzinfo=None)


def _resolve(zone: dt.tzinfo, wall: dt.datetime) -> tuple[dt.datetime, str | None]:
    """Local wall time -> UTC instant; the note says which DST rule fired (§9.2 step 4)."""
    offsets: list[dt.timedelta] = []
    for probe in (wall - _OFFSET_PROBE, wall + _OFFSET_PROBE):
        offset = pytz.utc.localize(probe).astimezone(zone).utcoffset()
        if offset not in offsets:
            offsets.append(offset)
    hits = sorted({wall - offset for offset in offsets if _wall_of(zone, wall - offset) == wall})
    if hits:
        return hits[0].replace(tzinfo=UTC), (DST_AMBIGUOUS_FIRST if len(hits) > 1 else None)
    # Gap: the offset in force before the transition moves the wall time forward by the gap.
    return (wall - offsets[0]).replace(tzinfo=UTC), DST_GAP_FORWARD


def resolve_local_time(timezone_name: str, local: dt.datetime) -> dt.datetime:
    """Naive local wall time in ``timezone_name`` -> aware UTC instant (gap forward, overlap first instant)."""
    if local.tzinfo is not None:
        raise ShiftEngineError("INVALID_INSTANT", "local time must be naive (a wall-clock time)", "local")
    return _resolve(_zone(timezone_name, "timezone"), local)[0]


def _local(instant: dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    return instant.astimezone(zone)


def _seconds(start: dt.datetime, end: dt.datetime) -> int:
    return int((end - start).total_seconds())


def _overlap_seconds(a_start: dt.datetime, a_end: dt.datetime, b_start: dt.datetime, b_end: dt.datetime) -> int:
    return max(0, _seconds(max(a_start, b_start), min(a_end, b_end)))


# --------------------------------------------------------------------------- clock timeline (minutes from anchor 00:00)


@dataclass(frozen=True)
class _Span:
    kind: str
    start: int
    end: int


def _timeline(segments: Sequence[SegmentData], prefix: str = "segments") -> list[_Span]:
    """Day carry of §4.1: each segment starts on the day of the previous end, +1 when its clock start is earlier."""
    spans: list[_Span] = []
    for i, seg in enumerate(segments):
        start_clock = _clock(seg.start, f"{prefix}[{i}].start")
        end_clock = _clock(seg.end, f"{prefix}[{i}].end")
        if not spans:
            start = start_clock
        else:
            previous_end = spans[-1].end
            start = (previous_end // DAY_MINUTES) * DAY_MINUTES + start_clock
            if start < previous_end:
                start += DAY_MINUTES
        end = start - start_clock + end_clock
        if end_clock <= start_clock:
            end += DAY_MINUTES
        spans.append(_Span(seg.kind, start, end))
    return spans


def _break_span(rule: BreakRuleData, what: str) -> _Span | None:
    offset = DAY_MINUTES if rule.next_day else 0
    if rule.mode == FIXED_WINDOW:
        if rule.start is None:
            raise ShiftEngineError("INVALID_BREAK", "a FIXED_WINDOW break needs start", f"{what}.start")
        start = offset + _clock(rule.start, f"{what}.start")
        return _Span(rule.mode, start, start + int(rule.minutes))
    if rule.mode == FLOATING_WINDOW:
        if rule.window_start is None or rule.window_end is None:
            raise ShiftEngineError("INVALID_BREAK", "a FLOATING_WINDOW break needs window_start and window_end", what)
        start = offset + _clock(rule.window_start, f"{what}.window_start")
        end = offset + _clock(rule.window_end, f"{what}.window_end")
        if end <= start:
            end += DAY_MINUTES
        return _Span(rule.mode, start, end)
    return None


def _inside_any(span: _Span, containers: Iterable[_Span]) -> bool:
    return any(c.start <= span.start and span.end <= c.end for c in containers)


def _version_segments(version: ShiftVersionData) -> tuple[SegmentData, ...]:
    """Explicit segments, or one WORK segment ``start_time -> end_time`` (§4.1); FLEXIBLE / OPEN have none."""
    if version.kind in (FLEXIBLE, OPEN):
        return ()
    if version.segments:
        return version.segments
    if version.start_time is None or version.end_time is None:
        raise ShiftEngineError("INVALID_VERSION", f"a {version.kind} version needs start_time and end_time", "start_time")
    return (SegmentData(WORK, version.start_time, version.end_time),)


def _entry_segments(entry: RosterEntryData) -> tuple[SegmentData, ...]:
    if entry.custom_segments:
        return entry.custom_segments
    if entry.custom_end_time is None:
        raise ShiftEngineError("INVALID_ENTRY", "custom_start_time needs custom_end_time", "custom_end_time")
    return (SegmentData(WORK, entry.custom_start_time, entry.custom_end_time),)


def _unpaid_minutes(rules: Sequence[BreakRuleData], legacy: int | None) -> int:
    """Σ unpaid break-rule minutes; with no rules the legacy ``unpaid_break_minutes`` (zero diff, §10)."""
    if rules:
        return sum(int(rule.minutes) for rule in rules if not rule.paid)
    return int(legacy or 0)


# --------------------------------------------------------------------------- instance construction (§9)


def build_instance(
    version: ShiftVersionData | None,
    shift_date: dt.date,
    work_tz: str,
    entry: RosterEntryData | None = None,
    *,
    open_day_start_time: str = DEFAULT_OPEN_DAY_START_TIME,
) -> ShiftInstance:
    """Build the instance of ``version`` (or of ``entry``'s custom times) starting on ``shift_date`` (§9.2).

    ``version`` may be ``None`` only for a custom-time entry without a catalogue shift.
    ``open_day_start_time`` is ``ATTENDANCE_RULES_POLICY.open_day_start_time`` (OPEN matching window).
    """
    _require_date(shift_date, "shift_date")
    work_zone = _zone(work_tz, "work_tz")
    custom = entry is not None and entry.has_custom_times
    if version is None and not custom:
        raise ShiftEngineError("SHIFT_REQUIRED", "a version is required unless the entry has custom times", "version")
    if version is not None and version.kind not in SHIFT_KINDS:
        raise ShiftEngineError("INVALID_KIND", f"kind must be one of {SHIFT_KINDS}, got {version.kind!r}", "kind")
    explain: list[str] = []

    if custom:
        segments = _entry_segments(entry)
        kind = SPLIT if sum(1 for s in segments if s.kind == WORK) > 1 else FIXED
        rules = version.break_rules if version is not None else entry.break_defaults
        legacy_unpaid = version.unpaid_break_minutes if version is not None else None
        clock_tz = work_tz  # custom times are always work-zone local (§8)
    else:
        kind = version.kind
        if kind == OPEN:
            return _open_instance(version, shift_date, work_tz, work_zone, entry, open_day_start_time)
        segments = _version_segments(version)
        rules = version.break_rules
        legacy_unpaid = version.unpaid_break_minutes
        clock_tz = version.anchor_timezone or work_tz
    clock_zone = _zone(clock_tz, "anchor_timezone")

    if kind == FLEXIBLE:
        if version.start_time is None or version.end_time is None:
            raise ShiftEngineError("INVALID_VERSION", "a FLEXIBLE version needs its nominal band", "start_time")
        spans = _timeline([SegmentData("BAND", version.start_time, version.end_time)], "band")
    else:
        spans = _timeline(segments)
    if not spans:
        raise ShiftEngineError("INVALID_VERSION", "the version has no segment", "segments")

    anchor = shift_date if clock_tz == work_tz else _anchor_date(spans[0].start, shift_date, clock_zone, work_zone, explain)
    midnight = dt.datetime.combine(anchor, dt.time())

    def at(minutes: int, what: str) -> dt.datetime:
        instant, note = _resolve(clock_zone, midnight + dt.timedelta(minutes=minutes))
        if note:
            explain.append(f"{note}:{what}")
        return instant

    if kind == FLEXIBLE:
        start_at, end_at = at(spans[0].start, "band.start"), at(spans[0].end, "band.end")
        placed_segments: tuple[InstanceSegment, ...] = ()
    else:
        placed_segments = tuple(
            InstanceSegment(span.kind, at(span.start, f"segments[{i}].start"), at(span.end, f"segments[{i}].end"))
            for i, span in enumerate(spans)
        )
        start_at, end_at = placed_segments[0].start_at, placed_segments[-1].end_at
    work_segments = [seg for seg in placed_segments if seg.kind == WORK]

    breaks = _place_breaks(rules, spans, custom, at, work_segments, explain)

    core_start_at = core_end_at = None
    if kind == FLEXIBLE and version.core_start_time is not None and version.core_end_time is not None:
        core_start = _clock(version.core_start_time, "core_start_time")
        while core_start < spans[0].start:
            core_start += DAY_MINUTES
        core_end = core_start - core_start % DAY_MINUTES + _clock(version.core_end_time, "core_end_time")
        if core_end <= core_start:
            core_end += DAY_MINUTES
        core_start_at, core_end_at = at(core_start, "core_start"), at(core_end, "core_end")

    if kind == FLEXIBLE:
        gross_minutes = None
        net = version.expected_net_minutes
    else:
        gross_minutes = sum(_seconds(seg.start_at, seg.end_at) for seg in work_segments) // 60
        net = max(0, gross_minutes - _unpaid_minutes(rules, legacy_unpaid))

    split_minutes = None
    if version is not None and version.half_split_time is not None and not custom:
        split_minutes = _clock(version.half_split_time, "half_split_time")
        while split_minutes <= spans[0].start:
            split_minutes += DAY_MINUTES
        if split_minutes >= spans[-1].end:
            explain.append(HALF_SPLIT_IGNORED)
            split_minutes = None
    split_at = at(split_minutes, "half_split_time") if split_minutes is not None else None

    first_half, second_half, rule_code = _halves(
        kind, start_at, end_at, work_segments, breaks, rules, legacy_unpaid, net, split_at, core_start_at, core_end_at,
    )
    explain.append(rule_code)

    early = version.early_in_window_minutes if version is not None else DEFAULT_EARLY_IN_WINDOW_MINUTES
    late = version.late_out_window_minutes if version is not None else DEFAULT_LATE_OUT_WINDOW_MINUTES
    return ShiftInstance(
        shift_id=_first(entry.shift_id if entry else None, version.shift_id if version else None),
        shift_version_id=_first(entry.shift_version_id if entry else None, version.shift_version_id if version else None),
        roster_entry_id=entry.roster_entry_id if entry else None,
        roster_version=entry.version if entry else None,
        kind=kind,
        shift_date=shift_date,
        work_timezone=work_tz,
        clock_timezone=clock_tz,
        anchor_date=anchor,
        start_at=start_at,
        end_at=end_at,
        crosses_midnight=_local(end_at, work_zone).date() > shift_date,
        segments=placed_segments,
        breaks=breaks,
        core_start_at=core_start_at,
        core_end_at=core_end_at,
        first_half=first_half,
        second_half=second_half,
        scheduled_net_minutes=net,
        gross_minutes=gross_minutes,
        match_start_at=start_at - dt.timedelta(minutes=int(early)),
        match_end_at=end_at + dt.timedelta(minutes=int(late)),
        explain=tuple(explain),
    )


def _first(*values: int | None) -> int | None:
    return next((value for value in values if value is not None), None)


def _anchor_date(first_start: int, shift_date: dt.date, clock_zone: dt.tzinfo, work_zone: dt.tzinfo,
                 explain: list[str]) -> dt.date:
    """§8: the first of D, D-1, D+1 whose first start, built in the clock zone, falls on D in the work zone."""
    for candidate, note in ((shift_date, None), (shift_date - _ONE_DAY, ANCHOR_DATE_PREVIOUS),
                            (shift_date + _ONE_DAY, ANCHOR_DATE_NEXT)):
        wall = dt.datetime.combine(candidate, dt.time()) + dt.timedelta(minutes=first_start)
        if _local(_resolve(clock_zone, wall)[0], work_zone).date() == shift_date:
            if note:
                explain.append(note)
            return candidate
    explain.append(ANCHOR_DATE_UNRESOLVED)
    return shift_date


def _place_breaks(rules: Sequence[BreakRuleData], spans: Sequence[_Span], custom: bool, at: Any,
                  work_segments: Sequence[InstanceSegment], explain: list[str]) -> tuple[PlacedBreak, ...]:
    """§9.2 step 6 with the §5.4 conversion for custom-time entries."""
    containers = [s for s in spans if s.kind in (WORK, "BAND")]
    placed: list[PlacedBreak] = []
    for i, rule in enumerate(rules):
        what = f"break_rules[{i}]"
        mode = rule.mode
        span = _break_span(rule, what)
        if custom and span is not None and not _inside_any(span, containers):
            explain.append(f"{BREAK_AS_PUNCHED}:{i}")
            mode, span = PUNCHED, None
        common = dict(index=i, kind=rule.kind, mode=mode, paid=bool(rule.paid), minutes=int(rule.minutes),
                      auto_deduct_after_minutes=rule.auto_deduct_after_minutes)
        if mode == FIXED_WINDOW:
            start_at = at(span.start, f"{what}.start")
            end_at = start_at + dt.timedelta(minutes=int(rule.minutes))
            holder = next((s for s in work_segments if s.start_at <= start_at < s.end_at), None)
            if holder is not None and end_at > holder.end_at:
                end_at = holder.end_at
                explain.append(f"{BREAK_WINDOW_CLIPPED}:{i}")
            placed.append(PlacedBreak(start_at=start_at, end_at=end_at, **common))
        elif mode == FLOATING_WINDOW:
            placed.append(PlacedBreak(window_start_at=at(span.start, f"{what}.window_start"),
                                      window_end_at=at(span.end, f"{what}.window_end"), **common))
        else:
            placed.append(PlacedBreak(**common))
    return tuple(placed)


def _open_instance(version: ShiftVersionData, shift_date: dt.date, work_tz: str, work_zone: dt.tzinfo,
                   entry: RosterEntryData | None, open_day_start_time: str) -> ShiftInstance:
    """OPEN: no clock times; window ``[D + open_day_start, D+1 + open_day_start)`` in the work zone (§9.2 step 10)."""
    explain: list[str] = []
    day_start = _clock(open_day_start_time, "open_day_start_time")

    def at(day: dt.date, what: str) -> dt.datetime:
        instant, note = _resolve(work_zone, dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=day_start))
        if note:
            explain.append(f"{note}:{what}")
        return instant

    match_start, match_end = at(shift_date, "match_start"), at(shift_date + _ONE_DAY, "match_end")
    expected = version.expected_net_minutes
    first_minutes = expected // 2 if expected is not None else None
    second_minutes = expected - first_minutes if expected is not None else None
    explain.append(HALVES_OPEN_WORKED)
    return ShiftInstance(
        shift_id=_first(entry.shift_id if entry else None, version.shift_id),
        shift_version_id=_first(entry.shift_version_id if entry else None, version.shift_version_id),
        roster_entry_id=entry.roster_entry_id if entry else None,
        roster_version=entry.version if entry else None,
        kind=OPEN, shift_date=shift_date, work_timezone=work_tz, clock_timezone=work_tz, anchor_date=shift_date,
        start_at=None, end_at=None, crosses_midnight=False, segments=(), breaks=(),
        core_start_at=None, core_end_at=None,
        first_half=HalfData(None, None, first_minutes), second_half=HalfData(None, None, second_minutes),
        scheduled_net_minutes=expected, gross_minutes=None,
        match_start_at=match_start, match_end_at=match_end, explain=tuple(explain),
    )


# --------------------------------------------------------------------------- halves (§6)


def _halves(kind: str, start_at: dt.datetime, end_at: dt.datetime, work_segments: Sequence[InstanceSegment],
            breaks: Sequence[PlacedBreak], rules: Sequence[BreakRuleData], legacy_unpaid: int | None,
            net: int | None, split_at: dt.datetime | None, core_start_at: dt.datetime | None,
            core_end_at: dt.datetime | None) -> tuple[HalfData, HalfData, str]:
    if kind == FLEXIBLE:
        if split_at is not None:
            boundary, code = split_at, HALVES_SPLIT_TIME
        elif core_start_at is not None and core_end_at is not None:
            boundary, code = core_start_at + dt.timedelta(seconds=_seconds(core_start_at, core_end_at) // 2), HALVES_CORE_MIDPOINT
        else:
            boundary, code = start_at + dt.timedelta(seconds=_seconds(start_at, end_at) // 2), HALVES_BAND_MIDPOINT
        first = net // 2 if net is not None else None
        second = net - first if net is not None else None
        return HalfData(start_at, boundary, first), HalfData(boundary, end_at, second), code

    # FIXED / SPLIT: halves run over the WORK span (the instance span when every segment is ON_CALL).
    span_start = work_segments[0].start_at if work_segments else start_at
    span_end = work_segments[-1].end_at if work_segments else end_at
    fixed_unpaid = sorted((b for b in breaks if b.mode == FIXED_WINDOW and not b.paid), key=lambda b: b.start_at)
    unplaced_unpaid = _unpaid_minutes(rules, legacy_unpaid) - sum(b.minutes for b in fixed_unpaid)

    def net_minutes_in(a: dt.datetime, b: dt.datetime) -> int:
        worked = sum(_overlap_seconds(s.start_at, s.end_at, a, b) for s in work_segments)
        unpaid = sum(_overlap_seconds(x.start_at, x.end_at, a, b) for x in fixed_unpaid)
        return max(0, (worked - unpaid) // 60 - unplaced_unpaid // 2)

    if split_at is not None:  # rule 1
        first = min(net, net_minutes_in(span_start, split_at))
        return HalfData(span_start, split_at, first), HalfData(split_at, span_end, net - first), HALVES_SPLIT_TIME
    if kind == SPLIT and work_segments:
        return _split_halves(work_segments, fixed_unpaid, net)
    if fixed_unpaid:  # rule 2: the first unpaid FIXED_WINDOW break
        lunch = fixed_unpaid[0]
        first = min(net, net_minutes_in(span_start, lunch.start_at))
        return HalfData(span_start, lunch.start_at, first), HalfData(lunch.end_at, span_end, net - first), HALVES_FIXED_BREAK
    # rule 3: the unpaid minutes centred on the gross midpoint (UTC instants)
    unpaid_seconds = _unpaid_minutes(rules, legacy_unpaid) * 60
    break_start = span_start + dt.timedelta(seconds=_seconds(span_start, span_end) // 2 - unpaid_seconds // 2)
    break_end = break_start + dt.timedelta(seconds=unpaid_seconds)
    first = net // 2
    return HalfData(span_start, break_start, first), HalfData(break_end, span_end, net - first), HALVES_CENTRED


def _split_halves(work_segments: Sequence[InstanceSegment], fixed_unpaid: Sequence[PlacedBreak],
                  net: int) -> tuple[HalfData, HalfData, str]:
    """SPLIT row of §6.1: segments fill H1 while the cumulative minutes stay <= half of net."""
    seg_net = [
        max(0, (_seconds(s.start_at, s.end_at)
                - sum(_overlap_seconds(b.start_at, b.end_at, s.start_at, s.end_at) for b in fixed_unpaid)) // 60)
        for s in work_segments
    ]
    filled = cumulative = 0
    for minutes in seg_net:
        if 2 * (cumulative + minutes) > net:
            break
        cumulative += minutes
        filled += 1
    if filled == len(work_segments) and filled > 1:  # degenerate (net 0): keep the last segment for H2
        filled -= 1
        cumulative -= seg_net[-1]
    if filled == 0:  # the first segment alone exceeds half: the boundary lies inside it
        boundary = work_segments[0].start_at + dt.timedelta(minutes=net // 2)
        first = net // 2
        return (HalfData(work_segments[0].start_at, boundary, first),
                HalfData(boundary, work_segments[-1].end_at, net - first), HALVES_SPLIT_SEGMENTS)
    cumulative = min(cumulative, net)
    return (HalfData(work_segments[0].start_at, work_segments[filled - 1].end_at, cumulative),
            HalfData(work_segments[filled].start_at if filled < len(work_segments) else work_segments[-1].end_at,
                     work_segments[-1].end_at, net - cumulative),
            HALVES_SPLIT_SEGMENTS)


def half_boundary(instance: ShiftInstance) -> dt.datetime:
    """The instant where the first half ends (§6.1: split time, else the first unpaid fixed break, else the
    centred break); OPEN shifts have no clock boundary and raise ``NO_CLOCK_BOUNDARY``."""
    if instance.first_half.end_at is None:
        raise ShiftEngineError("NO_CLOCK_BOUNDARY", "an OPEN shift's halves are the first N worked minutes", "kind")
    return instance.first_half.end_at


# --------------------------------------------------------------------------- scheduled minutes (§10)


def _fte(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ShiftEngineError("INVALID_FTE", f"fte must be a Decimal or int, got {type(value).__name__}", "fte")
    fte = Decimal(value)
    if not fte.is_finite() or fte < 0:
        raise ShiftEngineError("INVALID_FTE", f"fte must be a finite non-negative number, got {value!r}", "fte")
    return fte


def scheduled_net_minutes(
    version: ShiftVersionData | None,
    shift_date: dt.date,
    work_tz: str,
    *,
    fte: Decimal = Decimal(1),
    standard_minutes: int | None = None,
    entry: RosterEntryData | None = None,
) -> int:
    """``ROUND_HALF_UP(net x FTE)`` for the full day (§10).

    FIXED / SPLIT net = Σ WORK instant minutes − Σ unpaid break-rule minutes (ON_CALL excluded);
    FLEXIBLE / OPEN = ``expected_net_minutes``. ``standard_minutes`` (``payroll.standard_hours_per_day``
    × 60) is the fallback for no shift (``version`` None, no custom entry) and for FLEXIBLE / OPEN without
    ``expected_net_minutes`` (D-AS-37); it is required only when that fallback applies.
    """
    multiplier = _fte(fte)
    custom = entry is not None and entry.has_custom_times
    net: int | None
    if version is None and not custom:
        net = None
    else:
        net = build_instance(version, shift_date, work_tz, entry).scheduled_net_minutes
    if net is None:
        if standard_minutes is None:
            raise ShiftEngineError("STANDARD_MINUTES_REQUIRED",
                                   "no scheduled net: pass standard_minutes (standard-hours fallback)", "standard_minutes")
        if isinstance(standard_minutes, bool) or not isinstance(standard_minutes, int) or standard_minutes < 0:
            raise ShiftEngineError("INVALID_STANDARD_MINUTES", "standard_minutes must be a non-negative int",
                                   "standard_minutes")
        net = standard_minutes
    return int((Decimal(net) * multiplier).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- legacy mirrors (§3.8)


def _hhmm(minutes: int) -> str:
    minutes %= DAY_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _hours(minutes: int) -> Decimal:
    return (Decimal(min(minutes, DAY_MINUTES)) / 60).quantize(_HOURS_2DP, rounding=ROUND_HALF_UP)


def legacy_mirror(version: ShiftVersionData, on_date: dt.date, work_tz: str, *,
                  open_day_start_time: str = DEFAULT_OPEN_DAY_START_TIME) -> LegacyMirror:
    """Mirror columns of ``shifts`` for the version valid on the owner's local ``on_date`` (§3.8).

    Durations are clock (wall) minutes of the version times, so a DST night does not move the mirror;
    an anchored version mirrors its instance on ``on_date`` converted to the work zone.
    """
    unpaid = sum(int(r.minutes) for r in version.break_rules if not r.paid) if version.break_rules \
        else version.unpaid_break_minutes
    if version.kind == OPEN:
        day_start = _hhmm(_clock(open_day_start_time, "open_day_start_time"))
        expected = version.expected_net_minutes
        return LegacyMirror(day_start, day_start, _hours(expected) if expected is not None else None, 0, True)
    if version.kind == FLEXIBLE:
        band = _timeline([SegmentData("BAND", version.start_time, version.end_time)], "band")[0]
        minutes = (version.expected_net_minutes + (unpaid or 0) if version.expected_net_minutes is not None
                   else band.end - band.start)
        return LegacyMirror(_hhmm(band.start), _hhmm(band.end), _hours(minutes), unpaid, True)
    spans = _timeline(_version_segments(version))
    work = [s for s in spans if s.kind == WORK]
    measured = work or spans  # an all-ON_CALL shift mirrors its span so legacy hours stay non-NULL
    minutes = sum(s.end - s.start for s in measured)
    if version.kind == SPLIT:
        start, end = _hhmm(work[0].start), _hhmm(work[-1].end)
    elif version.anchor_timezone:
        instance = build_instance(version, on_date, work_tz)
        zone = _zone(work_tz, "work_tz")
        start, end = (_local(instance.start_at, zone).strftime("%H:%M"), _local(instance.end_at, zone).strftime("%H:%M"))
    else:
        start = _hhmm(_clock(version.start_time, "start_time")) if version.start_time is not None else _hhmm(spans[0].start)
        end = _hhmm(_clock(version.end_time, "end_time")) if version.end_time is not None else _hhmm(spans[-1].end)
    return LegacyMirror(start, end, _hours(minutes), unpaid, False)


# --------------------------------------------------------------------------- versions and zones


def version_on(versions: Iterable[ShiftVersionData], day: dt.date) -> ShiftVersionData | None:
    """The version with ``effective_from <= day <= COALESCE(effective_to, ∞)`` (§3.1); ``None`` when none."""
    _require_date(day, "day")
    for version in versions:
        if version.effective_from is None:
            raise ShiftEngineError("INVALID_VERSION", "versions passed to version_on need effective_from", "effective_from")
        if version.effective_from <= day and (version.effective_to is None or day <= version.effective_to):
            return version
    return None


def zone_change_shift_date(start_at: dt.datetime, effective_from: dt.date, old_timezone: str,
                           new_timezone: str) -> ZoneSwitch:
    """Switch rule of 02-system-design §7.2 item 4 (EC AS-TZ-02) for a placement change effective E.

    The instance belongs to the OLD placement when the local date of its start in the OLD zone is earlier
    than E, else to the NEW one; its shift date is the start's local date in the chosen zone, raised to E
    for the new placement.
    """
    if start_at.tzinfo is None:
        raise ShiftEngineError("INVALID_INSTANT", "start_at must be timezone-aware", "start_at")
    _require_date(effective_from, "effective_from")
    old_date = _local(start_at, _zone(old_timezone, "old_timezone")).date()
    if old_date < effective_from:
        return ZoneSwitch("OLD", old_timezone, old_date)
    new_date = _local(start_at, _zone(new_timezone, "new_timezone")).date()
    return ZoneSwitch("NEW", new_timezone, max(new_date, effective_from))


# --------------------------------------------------------------------------- validation (§3.2, §4.1, §5.1)


def _check_range(issues: list[VersionIssue], field: str, value: int | None, low: int, high: int) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        issues.append(VersionIssue(field, "OUT_OF_RANGE", f"{field} must be an integer {low}-{high}"))


def validate_version(version: ShiftVersionData) -> tuple[VersionIssue, ...]:
    """Field errors of a version body (§3.2, §4.1, §5.1); empty when the body is valid.

    The checks need no database: catalogue locks (kind, code, archive) and the guard stay in the service.
    """
    issues: list[VersionIssue] = []

    def add(field: str, code: str, message: str) -> None:
        issues.append(VersionIssue(field, code, message))

    kind = version.kind
    if kind not in SHIFT_KINDS:
        add("kind", "INVALID_KIND", f"kind must be one of {SHIFT_KINDS}")
        return tuple(issues)

    def clock_or_issue(value: object, field: str) -> int | None:
        if value is None:
            return None
        try:
            return _clock(value, field)
        except ShiftEngineError as exc:
            add(field, exc.code, exc.message)
            return None

    start = clock_or_issue(version.start_time, "start_time")
    end = clock_or_issue(version.end_time, "end_time")
    if kind == OPEN:
        for field, value in (("start_time", version.start_time), ("end_time", version.end_time)):
            if value is not None:
                add(field, "NOT_ALLOWED", "an OPEN shift has no clock times")
        if version.anchor_timezone is not None:
            add("anchor_timezone", "NOT_ALLOWED", "an OPEN shift cannot be anchored")
    else:
        for field, value in (("start_time", version.start_time), ("end_time", version.end_time)):
            if value is None:
                add(field, "REQUIRED", f"{field} is required for a {kind} shift")
    if start is not None and end is not None and start == end and not (kind == FIXED and version.long_duty):
        add("end_time", "EQUALS_START_TIME", "end_time equals start_time: only a FIXED 24-hour duty with long_duty")
    if version.long_duty and (kind != FIXED or start is None or start != end):
        add("long_duty", "LONG_DUTY_INVALID", "long_duty needs a FIXED shift with start_time = end_time")

    if version.anchor_timezone is not None and kind != OPEN:
        try:
            _zone(version.anchor_timezone, "anchor_timezone")
        except ShiftEngineError as exc:
            add("anchor_timezone", exc.code, exc.message)

    spans: list[_Span] = []
    if kind in (FLEXIBLE, OPEN):
        if version.segments:
            add("segments", "NOT_ALLOWED", f"a {kind} shift has no segments")
    elif start is not None and end is not None:
        segments = version.segments or (SegmentData(WORK, version.start_time, version.end_time),)
        spans = _validate_segments(version, segments, start, end, add)

    containers = spans
    if kind == FLEXIBLE and start is not None and end is not None:
        containers = _timeline([SegmentData("BAND", version.start_time, version.end_time)], "band")
        _validate_core(version, containers[0], add)
    elif version.core_start_time is not None or version.core_end_time is not None:
        add("core_start_time", "NOT_ALLOWED", "core hours belong to FLEXIBLE shifts only")

    fixed_spans = _validate_breaks(version, containers, add)
    work_minutes = sum(s.end - s.start for s in spans if s.kind == WORK)
    unpaid = sum(int(r.minutes) for r in version.break_rules if not r.paid and isinstance(r.minutes, int))
    if version.break_rules and kind in (FIXED, SPLIT) and spans and unpaid >= work_minutes:
        add("break_rules", "UNPAID_NOT_LESS_THAN_WORK", "Σ unpaid break minutes must be less than Σ WORK minutes")

    if version.half_split_time is not None:
        split = clock_or_issue(version.half_split_time, "half_split_time")
        if kind == OPEN:
            add("half_split_time", "NOT_ALLOWED", "an OPEN shift has no clock half boundary")
        elif split is not None and containers:
            while split <= containers[0].start:
                split += DAY_MINUTES
            if split >= containers[-1].end:
                add("half_split_time", "OUTSIDE_INSTANCE", "half_split_time must lie strictly inside the shift")
            elif any(s.start < split < s.end for s, rule in fixed_spans if not rule.paid):
                add("half_split_time", "INSIDE_FIXED_BREAK", "half_split_time cannot fall inside an unpaid fixed break")

    if version.expected_net_minutes is not None:
        if kind not in (FLEXIBLE, OPEN):
            add("expected_net_minutes", "NOT_ALLOWED", "expected_net_minutes belongs to FLEXIBLE and OPEN shifts")
        else:
            _check_range(issues, "expected_net_minutes", version.expected_net_minutes, 1, DAY_MINUTES)
            if kind == FLEXIBLE and containers and isinstance(version.expected_net_minutes, int):
                band = containers[0].end - containers[0].start
                if version.expected_net_minutes > band - unpaid:
                    add("expected_net_minutes", "EXCEEDS_BAND", "expected_net_minutes exceeds the band less unpaid breaks")
    _check_range(issues, "grace_in_minutes", version.grace_in_minutes, 0, 240)
    _check_range(issues, "grace_out_minutes", version.grace_out_minutes, 0, 240)
    _check_range(issues, "early_in_window_minutes", version.early_in_window_minutes, 0, 720)
    _check_range(issues, "late_out_window_minutes", version.late_out_window_minutes, 0, 720)
    return tuple(issues)


def _validate_segments(version: ShiftVersionData, segments: Sequence[SegmentData], start: int, end: int,
                       add: Any) -> list[_Span]:
    kind = version.kind
    if len(segments) > MAX_SEGMENTS:
        add("segments", "TOO_MANY", f"at most {MAX_SEGMENTS} segments")
    for i, seg in enumerate(segments):
        if seg.kind not in SEGMENT_KINDS:
            add(f"segments[{i}].kind", "INVALID_KIND", f"segment kind must be one of {SEGMENT_KINDS}")
    try:
        spans = _timeline(segments)
    except ShiftEngineError as exc:
        add(exc.field or "segments", exc.code, exc.message)
        return []
    work = [s for s in spans if s.kind == WORK]
    if kind == SPLIT and not 2 <= len(work) <= MAX_SEGMENTS:
        add("segments", "SPLIT_WORK_SEGMENTS", "a SPLIT shift has 2-4 WORK segments")
    if kind == FIXED and len(work) > 1:
        add("segments", "FIXED_ONE_WORK_SEGMENT", "a FIXED shift has one WORK segment (plus optional ON_CALL)")
    for i, (seg, span) in enumerate(zip(segments, spans)):
        inferred = span.end >= DAY_MINUTES  # the segment ends on a day after the anchor date
        if seg.next_day is not None and bool(seg.next_day) != inferred:
            add(f"segments[{i}].next_day", "NEXT_DAY_MISMATCH", "next_day differs from the inferred day (order?)")
        if i and kind == SPLIT and spans[i - 1].kind == WORK and span.kind == WORK \
                and span.start - spans[i - 1].end < MIN_SPLIT_GAP_MINUTES:
            add(f"segments[{i}].start", "SPLIT_GAP_TOO_SHORT",
                f"gaps between split segments are at least {MIN_SPLIT_GAP_MINUTES} minutes (shorter gaps are breaks)")
    if spans and spans[-1].end - spans[0].start > DAY_MINUTES:
        add("segments", "SPAN_OVER_24H", "first start to last end must be at most 24 hours")
    if version.segments and spans:
        first_start = work[0].start if (kind == SPLIT and work) else spans[0].start
        last_end = work[-1].end if (kind == SPLIT and work) else spans[-1].end
        if first_start % DAY_MINUTES != start:
            add("start_time", "SEGMENTS_MISMATCH", "start_time must equal the first segment start")
        if last_end % DAY_MINUTES != end:
            add("end_time", "SEGMENTS_MISMATCH", "end_time must equal the last segment end")
    return spans


def _validate_core(version: ShiftVersionData, band: _Span, add: Any) -> None:
    if version.core_start_time is None or version.core_end_time is None:
        add("core_start_time", "REQUIRED", "a FLEXIBLE shift needs core_start_time and core_end_time")
        return
    try:
        core_start = _clock(version.core_start_time, "core_start_time")
        core_end = _clock(version.core_end_time, "core_end_time")
    except ShiftEngineError as exc:
        add(exc.field or "core_start_time", exc.code, exc.message)
        return
    while core_start < band.start:
        core_start += DAY_MINUTES
    core_end = core_start - core_start % DAY_MINUTES + core_end
    if core_end <= core_start:
        core_end += DAY_MINUTES
    if core_end > band.end:
        add("core_end_time", "OUTSIDE_BAND", "the core window must lie inside the nominal band")
    elif core_end - core_start < MIN_CORE_MINUTES:
        add("core_end_time", "CORE_TOO_SHORT", f"the core window is at least {MIN_CORE_MINUTES} minutes")


def custom_entry(version: ShiftVersionData, *, roster_entry_id: int | None = None,
                 break_defaults: Sequence[Any] = ()) -> RosterEntryData:
    """The roster entry behind a resolved custom-time block without a shift (``shift_resolution`` gives it as a FIXED
    version), with the resolved ``break_defaults`` (§5.4)."""
    return RosterEntryData(roster_entry_id=roster_entry_id, custom_start_time=version.start_time,
                           custom_end_time=version.end_time, custom_segments=version.segments,
                           break_defaults=_rules_tuple(break_defaults))


def validate_break_defaults(rules: Sequence[Any]) -> tuple[VersionIssue, ...]:
    """Field errors of ``ATTENDANCE_RULES_POLICY.break_defaults`` (§5.1 rule checks). There is no shift to hold the
    windows, so any clock window is accepted; one outside a custom-time block becomes punched (§5.4)."""
    issues: list[VersionIssue] = []
    data = ShiftVersionData(break_rules=_rules_tuple(rules))
    _validate_breaks(data, [_Span(WORK, 0, 2 * DAY_MINUTES)],
                     lambda field, code, message: issues.append(VersionIssue(field, code, message)))
    return tuple(issues)


def _validate_breaks(version: ShiftVersionData, containers: Sequence[_Span],
                     add: Any) -> list[tuple[_Span, BreakRuleData]]:
    rules = version.break_rules
    if len(rules) > MAX_BREAK_RULES:
        add("break_rules", "TOO_MANY", f"at most {MAX_BREAK_RULES} break rules")
    work = [s for s in containers if s.kind in (WORK, "BAND")]
    fixed: list[tuple[_Span, BreakRuleData]] = []
    auto_kinds: set[str] = set()
    for i, rule in enumerate(rules):
        field = f"break_rules[{i}]"
        if rule.kind not in BREAK_KINDS:
            add(f"{field}.kind", "INVALID_KIND", f"break kind must be one of {BREAK_KINDS}")
        if rule.mode not in BREAK_MODES:
            add(f"{field}.mode", "INVALID_MODE", f"break mode must be one of {BREAK_MODES}")
            continue
        if isinstance(rule.minutes, bool) or not isinstance(rule.minutes, int) or not 1 <= rule.minutes <= 240:
            add(f"{field}.minutes", "OUT_OF_RANGE", "minutes must be an integer 1-240")
            continue
        if rule.mode == AUTO_DEDUCT:
            if rule.auto_deduct_after_minutes is None:
                add(f"{field}.auto_deduct_after_minutes", "REQUIRED", "AUTO_DEDUCT needs auto_deduct_after_minutes")
            if rule.kind in auto_kinds:
                add(field, "DUPLICATE_AUTO_DEDUCT", "at most one AUTO_DEDUCT rule per kind")
            auto_kinds.add(rule.kind)
        try:
            span = _break_span(rule, field)
        except ShiftEngineError as exc:
            add(exc.field or field, exc.code, exc.message)
            continue
        if span is None:
            continue
        if rule.mode == FIXED_WINDOW and rule.end is not None:
            try:
                length = (_clock(rule.end, f"{field}.end") - span.start) % DAY_MINUTES
            except ShiftEngineError as exc:
                add(f"{field}.end", exc.code, exc.message)
            else:
                if length != rule.minutes:
                    add(f"{field}.end", "WINDOW_LENGTH", "a fixed window's end - start must equal minutes")
        if rule.mode == FLOATING_WINDOW and span.end - span.start < rule.minutes:
            add(f"{field}.window_end", "WINDOW_TOO_SHORT", "the floating window must be at least minutes long")
        if not _inside_any(span, work):
            add(field, "BREAK_OUTSIDE_WORK", "the break window must lie inside a WORK segment")
        if rule.mode == FIXED_WINDOW:
            if any(other.start < span.end and span.start < other.end for other, _ in fixed):
                add(field, "FIXED_WINDOWS_OVERLAP", "two fixed-window breaks overlap")
            fixed.append((span, rule))
    return fixed
