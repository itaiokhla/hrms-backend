"""Overtime: thresholds, buckets and tiers, minimums, rounding, caps and comp-off (3.4 §9, 3.1 §5.5).

Three steps, each usable on its own:

    threshold_overtime   worked hours -> proposed entries. Each hour is counted once (§9.3,
                         EC-OT-03); weekly overtime is allocated to the latest days of the week
                         first, which picks its bucket.
    overtime_hours       entries -> paid minutes per bucket and tier. Only APPROVED entries count
                         (EC-OT-02); entries on dates where the DAY-grain switch is OFF are recorded
                         but unpaid (§9.9, EC-OT-01); per entry the minimum is applied before the
                         rounding, then caps per day, week, month and quarter (§9.6, EC-OT-04);
                         comp-off entries become a credit instead of pay (§9.7, EC-OT-09); a shift
                         crossing midnight stays on its start date or is split (§9.8, EC-OT-05).
    overtime_pay         buckets -> OT_WORKDAY / OT_WEEKLY_OFF / OT_HOLIDAY lines, where
                         amount = hourly_rate × multiplier × hours and the multiplier is the total
                         rate for the hour (§9.4). The hourly rate is never rounded; the line is.

Work on weekly offs and holidays is split between premiums and overtime by
``time_pay_premiums.holiday_premiums`` (§9.5), whose overtime entries feed
``overtime_hours``; mixing them with requests for the same date is refused, so
no hour is paid twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.payroll.time_pay_common import (
    BLOCKING,
    DAY_TYPES,
    DOUBLE_COUNTED_HOURS,
    EARNING,
    EXEMPT_NOT_ALLOWED,
    HOLIDAY,
    INVALID_INPUT,
    INVALID_MINUTES,
    INVALID_POLICY,
    INVALID_RATE,
    MAX_MINUTES_PER_DAY,
    MAX_SECONDS_PER_DAY,
    OVERLAPPING_ENTRIES,
    WARNING,
    WEEKLY_OFF,
    WORKDAY,
    DayRanges,
    Issue,
    TimePayError,
    TimePayLine,
    check_ranges,
    check_rounding_mode,
    field_value,
    hours,
    in_calc_context,
    make_line,
    policy_bool,
    policy_choice,
    policy_decimal,
    policy_int,
    require_decimal,
    require_int,
    resolve_minor_unit,
    show,
    show_hours,
    switch_on,
    units_4dp,
)

APPROVED = "APPROVED"
PROPOSED = "PROPOSED"
INACTIVE_STATUSES = frozenset({"REJECTED", "CANCELLED"})

ORIGIN_REQUEST = "REQUEST"
ORIGIN_THRESHOLD = "THRESHOLD"
ORIGIN_HOLIDAY_WORK = "HOLIDAY_WORK"
ORIGIN_WEEKLY_OFF_WORK = "WEEKLY_OFF_WORK"

SOURCES = ("APPROVED_REQUESTS", "ATTENDANCE_WITH_APPROVAL", "ATTENDANCE_AUTO")
COMBINATIONS = ("DAILY", "WEEKLY", "DAILY_THEN_WEEKLY")
ROUNDING_DIRECTIONS = ("DOWN", "NEAREST", "UP")
CAP_ACTIONS = ("EXCEPTION", "TRUNCATE")
CROSS_MIDNIGHT = ("SHIFT_START_DATE", "SPLIT_AT_MIDNIGHT")
CAP_WINDOWS = ("per_day", "per_week", "per_month", "per_quarter")
SHIFT_DURATION = "SHIFT_DURATION"

OT_COMPONENTS = {WORKDAY: "OT_WORKDAY", WEEKLY_OFF: "OT_WEEKLY_OFF", HOLIDAY: "OT_HOLIDAY"}

# Skip reasons
NOT_APPROVED = "NOT_APPROVED"
SWITCH_OFF = "SWITCH_OFF"
EXEMPT = "EXEMPT"
BELOW_MIN_MINUTES = "BELOW_MIN_MINUTES"
ROUNDED_TO_ZERO = "ROUNDED_TO_ZERO"
CAP_TRUNCATED = "CAP_TRUNCATED"
COMP_OFF_NOT_ALLOWED = "COMP_OFF_NOT_ALLOWED"


@dataclass(frozen=True)
class Tier:
    day_type: str
    after_hours: Decimal
    multiplier: Decimal

    @property
    def after_minutes(self) -> int:
        return int(self.after_hours * 60)


@dataclass(frozen=True)
class OvertimeRules:
    """A parsed OVERTIME_POLICY with every documented default filled in."""

    exempt: bool
    source: str
    daily_threshold: Optional[Decimal]  # None = SHIFT_DURATION
    weekly_threshold_hours: Optional[Decimal]
    threshold_combination: str
    min_minutes: int
    rounding_increment: int
    rounding_mode: str
    tiers: Tuple[Tier, ...]
    caps: Mapping[str, Decimal]
    cap_action: str
    cross_midnight: str
    comp_off_allowed: bool
    comp_off_ratio: Decimal

    def tiers_for(self, day_type: str) -> Tuple[Tier, ...]:
        return tuple(tier for tier in self.tiers if tier.day_type == day_type)


def _parse_tiers(raw: Any) -> Tuple[Tier, ...]:
    """Buckets have no default: the admin or a verified pack supplies every multiplier (contract K:157)."""
    if raw is None:
        raise TimePayError(INVALID_POLICY, "buckets are required: no overtime multiplier is assumed", "buckets")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or not raw:
        raise TimePayError(INVALID_POLICY, "buckets must be a non-empty list", "buckets")
    tiers: List[Tier] = []
    seen = set()
    for index, item in enumerate(raw):
        name = f"buckets[{index}]"
        day_type = field_value(item, "day_type")
        if day_type not in DAY_TYPES:
            raise TimePayError(INVALID_POLICY, f"{name}.day_type must be one of {', '.join(DAY_TYPES)}", name)
        after = require_decimal(field_value(item, "after_hours", "0"), f"{name}.after_hours",
                                minimum=Decimal(0), maximum=Decimal(24), code=INVALID_POLICY)
        if after * 60 != (after * 60).to_integral_value():
            raise TimePayError(INVALID_POLICY, f"{name}.after_hours must be a whole number of minutes", name)
        raw_multiplier = field_value(item, "multiplier")
        if raw_multiplier is None:
            raise TimePayError(INVALID_POLICY, f"{name}.multiplier is required: no overtime multiplier is assumed",
                               f"{name}.multiplier")
        multiplier = require_decimal(raw_multiplier, f"{name}.multiplier",
                                     minimum=Decimal(0), maximum=Decimal(10), code=INVALID_POLICY)
        if (day_type, after) in seen:
            raise TimePayError(INVALID_POLICY, f"{name} repeats {day_type} after {after} hours", name)
        seen.add((day_type, after))
        tiers.append(Tier(day_type, after, multiplier))
    for day_type in DAY_TYPES:
        if (day_type, Decimal(0)) not in {(t.day_type, t.after_hours) for t in tiers}:
            raise TimePayError(INVALID_POLICY, f"buckets need a {day_type} tier starting after 0 hours", "buckets")
    return tuple(sorted(tiers, key=lambda t: (DAY_TYPES.index(t.day_type), t.after_hours)))


def parse_overtime_policy(policy: Any) -> OvertimeRules:
    """Read an OVERTIME_POLICY (3.1 §5.5); raises ``TimePayError(INVALID_POLICY)`` on bad values."""
    daily_raw = field_value(policy, "daily_threshold", SHIFT_DURATION)
    daily = None if daily_raw == SHIFT_DURATION else require_decimal(
        daily_raw, "daily_threshold", minimum=Decimal(0), maximum=Decimal(24), code=INVALID_POLICY)
    combination = policy_choice(policy, "threshold_combination", "DAILY", COMBINATIONS)
    weekly = policy_decimal(policy, "weekly_threshold_hours", None, minimum=Decimal(0), maximum=Decimal(168))
    if combination != "DAILY" and weekly is None:
        raise TimePayError(INVALID_POLICY, f"{combination} needs weekly_threshold_hours", "weekly_threshold_hours")
    rounding = field_value(policy, "rounding", {"increment_minutes": 15, "mode": "DOWN"})
    increment = policy_int(rounding, "increment_minutes", 15, minimum=0, maximum=MAX_MINUTES_PER_DAY)
    direction = policy_choice(rounding, "mode", "DOWN", ROUNDING_DIRECTIONS)
    exempt = policy_bool(policy, "exempt", False)
    caps_raw = field_value(policy, "caps", {}) or {}
    caps: Dict[str, Decimal] = {}
    for key in CAP_WINDOWS:
        cap = policy_decimal(caps_raw, key, None, minimum=Decimal(0))
        if cap is not None:
            caps[key] = cap
    return OvertimeRules(
        exempt=exempt,
        source=policy_choice(policy, "source", "APPROVED_REQUESTS", SOURCES),
        daily_threshold=daily,
        weekly_threshold_hours=weekly,
        threshold_combination=combination,
        min_minutes=policy_int(policy, "min_minutes", 30, minimum=0, maximum=MAX_MINUTES_PER_DAY) or 0,
        rounding_increment=increment or 0,
        rounding_mode=direction,
        tiers=() if exempt else _parse_tiers(field_value(policy, "buckets")),
        caps=caps,
        cap_action=policy_choice(policy, "cap_action", "EXCEPTION", CAP_ACTIONS),
        cross_midnight=policy_choice(policy, "cross_midnight", "SHIFT_START_DATE", CROSS_MIDNIGHT),
        comp_off_allowed=policy_bool(policy, "comp_off_allowed", False),
        comp_off_ratio=policy_decimal(policy, "comp_off_ratio", "1.0", minimum=Decimal(0)) or Decimal(0),
    )


# --------------------------------------------------------------------------- hourly rate


@in_calc_context
def default_hourly_rate(sum_tag_rate_ot_base: Decimal, basis_days: Decimal | int,
                        standard_hours_per_day: Decimal | int) -> Decimal:
    """``SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day`` (3.1 §5.5), unrounded.

    Full-period rates are used, so LOP days never lower the overtime rate (§9.4).
    """
    base = require_decimal(sum_tag_rate_ot_base, "sum_tag_rate_ot_base", minimum=Decimal(0))
    days = require_decimal(basis_days, "basis_days", positive=True)
    std = require_decimal(standard_hours_per_day, "standard_hours_per_day", positive=True, maximum=Decimal(24))
    return base / days / std


@dataclass(frozen=True)
class OvertimeRate:
    rate: Decimal
    source: str  # POLICY_FORMULA | PACK_REGULAR_RATE
    explanation: str


@in_calc_context
def resolve_overtime_rate(policy_rate: Decimal, *, pack_regular_rate: Optional[Decimal] = None) -> OvertimeRate:
    """The hourly base for overtime. A pack's legal regular rate overrides the policy formula (EC-OT-08)."""
    policy_value = require_decimal(policy_rate, "policy_rate", positive=True, code=INVALID_RATE)
    if pack_regular_rate is None:
        return OvertimeRate(policy_value, "POLICY_FORMULA", f"Policy rate formula: {show(policy_value)} per hour")
    pack_value = require_decimal(pack_regular_rate, "pack_regular_rate", positive=True, code=INVALID_RATE)
    return OvertimeRate(pack_value, "PACK_REGULAR_RATE",
                        f"Country pack regular rate {show(pack_value)} per hour overrides the policy formula "
                        f"({show(policy_value)})")


# --------------------------------------------------------------------------- thresholds


@dataclass(frozen=True)
class OvertimeEntry:
    """An overtime entry (3.9 ``overtime_entries``). ``date`` is the shift start date.

    ``start``/``end`` (optional) give the overtime span; approved minutes shorter than the
    span are taken from its start (an approver edits the end down).
    """

    entry_id: str
    date: date
    minutes: int
    status: str = APPROVED
    comp_off: bool = False
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    origin: str = ORIGIN_REQUEST


@dataclass(frozen=True)
class WorkedDay:
    """Canonical net worked time on one date (``attendance_hours.compute``)."""

    date: date
    worked_seconds: int
    day_type: str = WORKDAY
    shift_hours: Optional[Decimal] = None  # scheduled duration, used when daily_threshold = SHIFT_DURATION
    leave_seconds_toward_threshold: int = 0  # paid leave whose type has counts_toward_ot_threshold


@dataclass(frozen=True)
class ThresholdDay:
    date: date
    day_type: str
    worked_seconds: int
    daily_ot_seconds: Decimal
    weekly_ot_seconds: Decimal

    @property
    def ot_seconds(self) -> Decimal:
        return self.daily_ot_seconds + self.weekly_ot_seconds

    @property
    def ot_minutes(self) -> int:
        return int(self.ot_seconds // 60)


@dataclass(frozen=True)
class ThresholdOvertime:
    combination: str
    days: Tuple[ThresholdDay, ...]
    entries: Tuple[OvertimeEntry, ...]
    explanations: Tuple[str, ...]

    @property
    def daily_ot_seconds(self) -> Decimal:
        return sum((day.daily_ot_seconds for day in self.days), Decimal(0))

    @property
    def weekly_ot_seconds(self) -> Decimal:
        return sum((day.weekly_ot_seconds for day in self.days), Decimal(0))

    @property
    def total_ot_seconds(self) -> Decimal:
        return self.daily_ot_seconds + self.weekly_ot_seconds


def week_start_of(day: date, week_start: int) -> date:
    return day - timedelta(days=(day.weekday() - week_start) % 7)


def _check_worked_days(days: Sequence[WorkedDay]) -> None:
    seen = set()
    for day in days:
        if day.date in seen:
            raise TimePayError(INVALID_INPUT, f"worked hours given twice for {day.date}", "days")
        seen.add(day.date)
        require_int(day.worked_seconds, f"worked_seconds on {day.date}", minimum=0, maximum=MAX_SECONDS_PER_DAY)
        require_int(day.leave_seconds_toward_threshold, f"leave_seconds_toward_threshold on {day.date}",
                    minimum=0, maximum=MAX_SECONDS_PER_DAY)
        if day.day_type not in DAY_TYPES:
            raise TimePayError(INVALID_INPUT, f"day_type on {day.date} must be one of {', '.join(DAY_TYPES)}", "days")


@in_calc_context
def threshold_overtime(days: Sequence[WorkedDay], policy: Any, *, week_start: int = 0) -> ThresholdOvertime:
    """Overtime proposed from worked hours (3.4 §9.3); each hour is counted once.

    Only WORKDAY dates are measured against thresholds: hours on weekly offs and
    holidays are already premium or overtime hours (§9.5) and are neither regular
    hours nor counted again. Weeks start on ``week_start`` (Mon=0); pass whole weeks.
    Proposals carry whole minutes (seconds are truncated) and are APPROVED only for
    ``ATTENDANCE_AUTO``; otherwise they are PROPOSED for a manager to approve.
    """
    rules = parse_overtime_policy(policy)
    require_int(week_start, "week_start", minimum=0, maximum=6)
    _check_worked_days(days)
    weeks: Dict[date, List[WorkedDay]] = {}
    for day in sorted(days, key=lambda d: d.date):
        weeks.setdefault(week_start_of(day.date, week_start), []).append(day)

    result_days: List[ThresholdDay] = []
    explanations: List[str] = []
    use_daily = rules.threshold_combination in ("DAILY", "DAILY_THEN_WEEKLY")
    use_weekly = rules.threshold_combination in ("WEEKLY", "DAILY_THEN_WEEKLY")
    for monday, week in sorted(weeks.items()):
        daily: Dict[date, Decimal] = {}
        regular: Dict[date, Decimal] = {}
        workdays = [d for d in week if d.day_type == WORKDAY]
        for day in workdays:
            worked = Decimal(day.worked_seconds)
            daily[day.date] = max(Decimal(0), worked - _daily_threshold_seconds(day, rules)) if use_daily else Decimal(0)
            regular[day.date] = worked - daily[day.date]
        weekly: Dict[date, Decimal] = {d.date: Decimal(0) for d in workdays}
        if use_weekly and rules.weekly_threshold_hours is not None:
            total_regular = sum(regular.values(), Decimal(0))
            credit = Decimal(sum(d.leave_seconds_toward_threshold for d in week))
            excess = max(Decimal(0), total_regular + credit - rules.weekly_threshold_hours * 3600)
            remaining = min(excess, total_regular)
            for day in reversed(workdays):
                take = min(remaining, regular[day.date])
                weekly[day.date] = take
                remaining -= take
            if excess:
                explanations.append(
                    f"Week of {monday.isoformat()}: regular {show(hours(total_regular / 60))} h"
                    + (f" + leave {show(hours(credit / 60))} h" if credit else "")
                    + f" − threshold {show(rules.weekly_threshold_hours)} h = {show(hours(excess / 60))} h weekly overtime,"
                    " allocated to the latest days first")
        for day in week:
            result_days.append(ThresholdDay(day.date, day.day_type, day.worked_seconds,
                                            daily.get(day.date, Decimal(0)), weekly.get(day.date, Decimal(0))))

    status = APPROVED if rules.source == "ATTENDANCE_AUTO" else PROPOSED
    entries = tuple(
        OvertimeEntry(f"threshold:{day.date.isoformat()}", day.date, day.ot_minutes, status, origin=ORIGIN_THRESHOLD)
        for day in result_days if day.ot_minutes > 0
    )
    return ThresholdOvertime(rules.threshold_combination, tuple(result_days), entries, tuple(explanations))


def _daily_threshold_seconds(day: WorkedDay, rules: OvertimeRules) -> Decimal:
    if rules.daily_threshold is not None:
        return rules.daily_threshold * 3600
    if day.shift_hours is None:
        raise TimePayError(INVALID_INPUT, f"daily_threshold is SHIFT_DURATION but {day.date} has no shift duration",
                           "shift_hours")
    return require_decimal(day.shift_hours, f"shift_hours on {day.date}", positive=True, maximum=Decimal(24)) * 3600


# --------------------------------------------------------------------------- entry validation (EC-OT-10)


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo is not None else moment


def _minutes_between(start: datetime, end: datetime) -> Decimal:
    return Decimal(int((_utc(end) - _utc(start)).total_seconds())) / 60


def validate_overtime_entries(entries: Sequence[OvertimeEntry]) -> None:
    """Reject impossible entries (EC-OT-10): negative minutes, more than 24 hours in a day,
    spans shorter than their minutes, and overlapping spans. Raised, because such an
    entry must be refused when entered, never paid."""
    ids = set()
    spans: List[Tuple[datetime, datetime, str]] = []
    per_day: Dict[date, int] = {}
    aware: Optional[bool] = None
    for entry in entries:
        if not isinstance(entry.entry_id, str) or not entry.entry_id or entry.entry_id in ids:
            raise TimePayError(INVALID_INPUT, f"entry_id must be unique and non-empty: {entry.entry_id!r}", "entry_id")
        ids.add(entry.entry_id)
        if isinstance(entry.minutes, bool) or not isinstance(entry.minutes, int):
            raise TimePayError(INVALID_MINUTES, f"{entry.entry_id}: minutes must be an integer", "minutes")
        if entry.minutes < 0:
            raise TimePayError(INVALID_MINUTES, f"{entry.entry_id}: minutes cannot be negative", "minutes")
        if entry.minutes > MAX_MINUTES_PER_DAY:
            raise TimePayError(INVALID_MINUTES, f"{entry.entry_id}: more than 24 hours of overtime", "minutes")
        if (entry.start is None) != (entry.end is None):
            raise TimePayError(INVALID_INPUT, f"{entry.entry_id}: give both start and end, or neither", "start")
        active = entry.status not in INACTIVE_STATUSES
        if entry.start is not None and entry.end is not None:
            is_aware = entry.start.tzinfo is not None
            if is_aware != (entry.end.tzinfo is not None) or (aware is not None and aware != is_aware):
                raise TimePayError(INVALID_INPUT, "overtime spans must all be timezone-aware or all naive", "start")
            aware = is_aware
            span = _minutes_between(entry.start, entry.end)
            if span <= 0 or span > MAX_MINUTES_PER_DAY:
                raise TimePayError(INVALID_MINUTES, f"{entry.entry_id}: the span must be between 1 minute and 24 hours",
                                   "end")
            if entry.minutes > span:
                raise TimePayError(INVALID_MINUTES, f"{entry.entry_id}: {entry.minutes} minutes exceed the span", "minutes")
            if entry.start.date() not in (entry.date, entry.date + timedelta(days=1)):
                raise TimePayError(INVALID_INPUT, f"{entry.entry_id}: the span must start on the shift date", "start")
            if active:
                spans.append((_utc(entry.start), _utc(entry.end), entry.entry_id))
        if active:
            per_day[entry.date] = per_day.get(entry.date, 0) + entry.minutes
            if per_day[entry.date] > MAX_MINUTES_PER_DAY:
                raise TimePayError(INVALID_MINUTES, f"more than 24 hours of overtime on {entry.date}", "minutes")
    spans.sort()
    for (_, earlier_end, earlier_id), (later_start, _, later_id) in zip(spans, spans[1:]):
        if later_start < earlier_end:
            raise TimePayError(OVERLAPPING_ENTRIES, f"{later_id} overlaps {earlier_id}", "start")


def round_minutes(minutes: int, increment: int, mode: str) -> int:
    """Round per entry to the policy increment (3.1 §5.5 ``rounding``); NEAREST rounds halves up."""
    if increment <= 0:
        return minutes
    whole, rest = divmod(minutes, increment)
    if mode == "UP" and rest:
        whole += 1
    elif mode == "NEAREST" and rest * 2 >= increment:
        whole += 1
    return whole * increment


# --------------------------------------------------------------------------- hours per bucket


@dataclass(frozen=True)
class NormalizedEntry:
    entry_id: str
    date: date
    minutes_approved: int
    minutes_after_rounding: int


@dataclass(frozen=True)
class SkippedEntry:
    entry_id: str
    date: date
    minutes: int
    reason: str


@dataclass(frozen=True)
class TierAllocation:
    entry_id: str
    date: date
    day_type: str
    after_hours: Decimal
    multiplier: Decimal
    minutes: int

    @property
    def component(self) -> str:
        return OT_COMPONENTS[self.day_type]


@dataclass(frozen=True)
class OvertimeBucket:
    day_type: str
    after_hours: Decimal
    multiplier: Decimal
    minutes: int

    @property
    def component(self) -> str:
        return OT_COMPONENTS[self.day_type]

    @property
    def hours(self) -> Decimal:
        return hours(self.minutes)


@dataclass(frozen=True)
class CompOffCredit:
    """Overtime taken as time off: credited as a COMP_OFF leave balance, never paid (EC-OT-09)."""

    entry_id: str
    date: date
    minutes: int
    ratio: Decimal

    @property
    def credit_hours(self) -> Decimal:
        return Decimal(self.minutes) * self.ratio / 60


@dataclass(frozen=True)
class OvertimeHours:
    exempt: bool
    buckets: Tuple[OvertimeBucket, ...]
    allocations: Tuple[TierAllocation, ...]
    comp_off: Tuple[CompOffCredit, ...]
    normalized: Tuple[NormalizedEntry, ...]
    recorded_only: Tuple[SkippedEntry, ...]  # approved but unpaid by rule (switch OFF, exempt); still reported
    excluded: Tuple[SkippedEntry, ...]
    issues: Tuple[Issue, ...]
    # Entries whose minutes are recorded worked time (THRESHOLD, HOLIDAY_WORK, WEEKLY_OFF_WORK origins), so they
    # are also in the ledger's worked/payable hours; a REQUEST's hours need not be in attendance.
    from_worked_time: Tuple[str, ...] = ()

    def paid_minutes_from_worked_time(self, dates: Optional[frozenset] = None) -> int:
        """Paid overtime minutes taken from recorded worked time (on `dates` when given): payable hours exclude
        them, because an overtime multiplier is the total rate for the hour (3.4 §9.4-§9.5)."""
        ids = set(self.from_worked_time)
        return sum(a.minutes for a in self.allocations
                   if a.entry_id in ids and (dates is None or a.date in dates))

    def minutes_for(self, day_type: str) -> int:
        return sum(bucket.minutes for bucket in self.buckets if bucket.day_type == day_type)

    @property
    def paid_minutes(self) -> int:
        return sum(bucket.minutes for bucket in self.buckets)

    @property
    def hours_workday(self) -> Decimal:
        return hours(self.minutes_for(WORKDAY))

    @property
    def hours_weekly_off(self) -> Decimal:
        return hours(self.minutes_for(WEEKLY_OFF))

    @property
    def hours_holiday(self) -> Decimal:
        return hours(self.minutes_for(HOLIDAY))

    @property
    def hours_total(self) -> Decimal:
        return hours(self.paid_minutes)

    @property
    def comp_off_credit_hours(self) -> Decimal:
        return sum((credit.credit_hours for credit in self.comp_off), Decimal(0))


@dataclass
class _Segment:
    entry_id: str
    index: int
    date: date
    minutes: int
    comp_off: bool
    wall_minute: Optional[int]

    def key(self) -> Tuple[date, int, int, int]:
        return (self.date, 0 if self.wall_minute is None else 1, self.wall_minute or 0, self.index)


_WINDOW_LABELS = {"per_day": "daily", "per_week": "weekly", "per_month": "monthly", "per_quarter": "quarterly"}


def _window_start(window: str, day: date, week_start: int) -> date:
    if window == "per_day":
        return day
    if window == "per_week":
        return week_start_of(day, week_start)
    if window == "per_month":
        return day.replace(day=1)
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _split(entry: OvertimeEntry, minutes: int, index: int, rules: OvertimeRules) -> List[_Segment]:
    if entry.start is None:
        return [_Segment(entry.entry_id, index, entry.date, minutes, entry.comp_off, None)]
    start_day = entry.start.date()
    wall = entry.start.hour * 60 + entry.start.minute
    if rules.cross_midnight == "SHIFT_START_DATE":
        offset = MAX_MINUTES_PER_DAY if start_day > entry.date else 0
        return [_Segment(entry.entry_id, index, entry.date, minutes, entry.comp_off, wall + offset)]
    midnight = datetime.combine(start_day + timedelta(days=1), time(0), tzinfo=entry.start.tzinfo)
    before = min(minutes, int(_minutes_between(entry.start, midnight)))
    segments = [_Segment(entry.entry_id, index, start_day, before, entry.comp_off, wall)] if before else []
    if minutes > before:
        segments.append(_Segment(entry.entry_id, index, start_day + timedelta(days=1), minutes - before,
                                 entry.comp_off, 0))
    return segments


def _check_origins(entries: Sequence[OvertimeEntry]) -> None:
    by_date: Dict[date, set] = {}
    for entry in entries:
        if entry.origin not in (ORIGIN_REQUEST, ORIGIN_THRESHOLD, ORIGIN_HOLIDAY_WORK, ORIGIN_WEEKLY_OFF_WORK):
            raise TimePayError(INVALID_INPUT, f"{entry.entry_id}: unknown origin {entry.origin!r}", "origin")
        if entry.status not in INACTIVE_STATUSES:
            by_date.setdefault(entry.date, set()).add(entry.origin)
    for day, origins in sorted(by_date.items()):
        if len(origins) > 1:
            raise TimePayError(DOUBLE_COUNTED_HOURS, f"{day}: overtime from {', '.join(sorted(origins))} would count the "
                               "same hours twice; use one source per date", "origin")


def _apply_caps(segments: List[_Segment], rules: OvertimeRules, week_start: int,
                prior: Mapping[Tuple[str, date], int]) -> Tuple[List[SkippedEntry], List[Issue]]:
    usage: Dict[Tuple[str, date], int] = {}
    truncated: Dict[Tuple[str, date], int] = {}
    skipped: List[SkippedEntry] = []
    limits = {window: int(cap * 60) for window, cap in rules.caps.items()}
    for segment in segments:
        keys = [(window, _window_start(window, segment.date, week_start)) for window in CAP_WINDOWS if window in limits]
        for key in keys:
            usage.setdefault(key, prior.get(key, 0))
        if rules.cap_action == "TRUNCATE" and keys:
            rooms = [(limits[key[0]] - usage[key], key) for key in keys]
            room, binding = min(rooms, key=lambda item: (item[0], CAP_WINDOWS.index(item[1][0])))
            allowed = max(0, min(segment.minutes, room))
            cut = segment.minutes - allowed
            if cut:
                truncated[binding] = truncated.get(binding, 0) + cut
                skipped.append(SkippedEntry(segment.entry_id, segment.date, cut, CAP_TRUNCATED))
                segment.minutes = allowed
        for key in keys:
            usage[key] += segment.minutes
    issues: List[Issue] = []
    if rules.cap_action == "TRUNCATE":
        for (window, start), cut in sorted(truncated.items()):
            issues.append(Issue("OT_CAP_TRUNCATED", WARNING,
                                f"Overtime truncated by {show_hours(cut)} at the {_WINDOW_LABELS[window]} cap of "
                                f"{show(rules.caps[window])} h", start,
                                details={"window": window, "cap_hours": str(rules.caps[window]),
                                         "truncated_minutes": str(cut)}))
    else:
        for (window, start), used in sorted(usage.items()):
            if used > limits[window]:
                issues.append(Issue("OT_CAP_EXCEEDED", BLOCKING,
                                    f"Overtime of {show_hours(used)} exceeds the {_WINDOW_LABELS[window]} cap of "
                                    f"{show(rules.caps[window])} h; resolve before approval", start,
                                    details={"window": window, "cap_hours": str(rules.caps[window]),
                                             "minutes": str(used)}))
    return skipped, issues


def _allocate_tiers(segments: Sequence[_Segment], rules: OvertimeRules,
                    day_types: Mapping[date, str]) -> List[TierAllocation]:
    used: Dict[date, int] = {}
    allocations: List[TierAllocation] = []
    for segment in segments:
        day_type = day_types[segment.date]
        tiers = rules.tiers_for(day_type)
        done = used.get(segment.date, 0)
        remaining = segment.minutes
        for position, tier in enumerate(tiers):
            upper = tiers[position + 1].after_minutes if position + 1 < len(tiers) else None
            if upper is not None and done >= upper:
                continue
            take = remaining if upper is None else min(remaining, upper - done)
            if take > 0:
                allocations.append(TierAllocation(segment.entry_id, segment.date, day_type, tier.after_hours,
                                                  tier.multiplier, take))
                done += take
                remaining -= take
            if not remaining:
                break
        used[segment.date] = done
    return allocations


@in_calc_context
def overtime_hours(
    entries: Sequence[OvertimeEntry],
    policy: Any,
    *,
    day_types: Mapping[date, str],
    enabled: Optional[DayRanges] = None,
    legally_covered: bool = False,
    week_start: int = 0,
    prior_minutes: Optional[Mapping[Tuple[str, date], int]] = None,
) -> OvertimeHours:
    """Paid overtime minutes per bucket and tier for one employee and period (3.4 §9.2–§9.9).

    ``day_types`` maps every date an entry (or its after-midnight part) falls on to
    WORKDAY, WEEKLY_OFF or HOLIDAY; a missing date raises (fail closed). ``enabled``
    lists the dates on which ``payroll.overtime.enabled`` is ON (DAY grain); with
    SPLIT_AT_MIDNIGHT each part is paid only if the switch is ON on its own date. Caps
    count approved paid and comp-off minutes; ``prior_minutes`` carries minutes
    already used in a cap window by earlier periods, keyed ``("per_week", start)``.
    Tiers fill chronologically per date; comp-off minutes do not consume tiers.
    """
    rules = parse_overtime_policy(policy)
    check_ranges(enabled, "enabled")
    require_int(week_start, "week_start", minimum=0, maximum=6)
    validate_overtime_entries(entries)
    _check_origins(entries)
    for day, day_type in day_types.items():
        if day_type not in DAY_TYPES:
            raise TimePayError(INVALID_INPUT, f"day type on {day} must be one of {', '.join(DAY_TYPES)}", "day_types")
    prior = dict(prior_minutes or {})
    for (window, start), used in prior.items():
        if window not in CAP_WINDOWS or not isinstance(start, date):
            raise TimePayError(INVALID_INPUT, f"prior_minutes key {(window, start)!r} is not a cap window", "prior_minutes")
        require_int(used, f"prior_minutes[{window}]", minimum=0)
    if rules.exempt and legally_covered:
        raise TimePayError(EXEMPT_NOT_ALLOWED, "an exempt overtime policy cannot apply to legally covered workers",
                           "exempt")

    normalized: List[NormalizedEntry] = []
    recorded: List[SkippedEntry] = []
    excluded: List[SkippedEntry] = []
    issues: List[Issue] = []
    segments: List[_Segment] = []
    for index, entry in enumerate(entries):
        if entry.status != APPROVED:
            excluded.append(SkippedEntry(entry.entry_id, entry.date, entry.minutes, NOT_APPROVED))
            continue
        if rules.exempt:
            recorded.append(SkippedEntry(entry.entry_id, entry.date, entry.minutes, EXEMPT))
            continue
        split = entry.start is not None and rules.cross_midnight == "SPLIT_AT_MIDNIGHT"
        if not split and not switch_on(entry.date, enabled):
            recorded.append(SkippedEntry(entry.entry_id, entry.date, entry.minutes, SWITCH_OFF))
            continue
        if entry.minutes < rules.min_minutes:
            excluded.append(SkippedEntry(entry.entry_id, entry.date, entry.minutes, BELOW_MIN_MINUTES))
            continue
        rounded = round_minutes(entry.minutes, rules.rounding_increment, rules.rounding_mode)
        normalized.append(NormalizedEntry(entry.entry_id, entry.date, entry.minutes, rounded))
        if rounded == 0:
            excluded.append(SkippedEntry(entry.entry_id, entry.date, entry.minutes, ROUNDED_TO_ZERO))
            continue
        if entry.comp_off and not rules.comp_off_allowed:
            excluded.append(SkippedEntry(entry.entry_id, entry.date, rounded, COMP_OFF_NOT_ALLOWED))
            issues.append(Issue("OT_COMP_OFF_NOT_ALLOWED", BLOCKING,
                                "Comp-off was chosen but the overtime policy does not allow it; neither paid nor "
                                "credited until resolved", entry.date, entry.entry_id))
            continue
        for segment in _split(entry, rounded, index, rules):
            if switch_on(segment.date, enabled):
                segments.append(segment)
            else:
                recorded.append(SkippedEntry(entry.entry_id, segment.date, segment.minutes, SWITCH_OFF))

    for segment in segments:
        if segment.date not in day_types:
            raise TimePayError(INVALID_INPUT, f"no day type given for {segment.date}", "day_types")
    segments.sort(key=_Segment.key)
    truncated, cap_issues = _apply_caps(segments, rules, week_start, prior)
    excluded.extend(truncated)
    issues.extend(cap_issues)

    credits: Dict[str, CompOffCredit] = {}
    paid: List[_Segment] = []
    entry_dates = {entry.entry_id: entry.date for entry in entries}
    for segment in segments:
        if not segment.minutes:
            continue
        if segment.comp_off:
            previous = credits.get(segment.entry_id)
            minutes = segment.minutes + (previous.minutes if previous else 0)
            credits[segment.entry_id] = CompOffCredit(segment.entry_id, entry_dates[segment.entry_id], minutes,
                                                      rules.comp_off_ratio)
        else:
            paid.append(segment)
    allocations = _allocate_tiers(paid, rules, day_types)
    totals: Dict[Tuple[str, Decimal, Decimal], int] = {}
    for allocation in allocations:
        key = (allocation.day_type, allocation.after_hours, allocation.multiplier)
        totals[key] = totals.get(key, 0) + allocation.minutes
    buckets = tuple(
        OvertimeBucket(day_type, after, multiplier, minutes)
        for (day_type, after, multiplier), minutes in sorted(
            totals.items(), key=lambda item: (DAY_TYPES.index(item[0][0]), item[0][1]))
    )
    worked_ids = tuple(sorted({e.entry_id for e in entries if e.origin != ORIGIN_REQUEST}))
    return OvertimeHours(rules.exempt, buckets, tuple(allocations), tuple(credits.values()), tuple(normalized),
                         tuple(recorded), tuple(excluded), tuple(issues), from_worked_time=worked_ids)


# --------------------------------------------------------------------------- pay


@dataclass(frozen=True)
class OvertimePay:
    currency: str
    hourly_rate: Optional[Decimal]
    lines: Tuple[TimePayLine, ...]

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))


@in_calc_context
def overtime_pay(ot: OvertimeHours, hourly_rate: Decimal, currency: str, *, minor_unit: Optional[int] = None,
                 rounding: str = "HALF_UP") -> OvertimePay:
    """OT_WORKDAY / OT_WEEKLY_OFF / OT_HOLIDAY lines: Σ hourly_rate × multiplier × hours per bucket.

    The hourly rate stays unrounded (§9.4: 50,000 ÷ 26 ÷ 8 = 240.3846… × 1.5 × 4 = 1,442.31);
    each line is rounded once to the currency's minor unit.
    """
    minor_unit = resolve_minor_unit(currency, minor_unit)
    check_rounding_mode(rounding)
    if not ot.paid_minutes:
        return OvertimePay(currency, None, ())
    rate = require_decimal(hourly_rate, "hourly_rate", positive=True, code=INVALID_RATE)
    lines: List[TimePayLine] = []
    for day_type in DAY_TYPES:
        buckets = [bucket for bucket in ot.buckets if bucket.day_type == day_type and bucket.minutes]
        if not buckets:
            continue
        weighted = sum((bucket.multiplier * bucket.minutes for bucket in buckets), Decimal(0))
        minutes = sum(bucket.minutes for bucket in buckets)
        per_entry: Dict[str, int] = {}
        for allocation in ot.allocations:
            if allocation.day_type == day_type:
                per_entry[allocation.entry_id] = per_entry.get(allocation.entry_id, 0) + allocation.minutes
        explanation = " + ".join(
            f"{show(rate)} × {bucket.multiplier} × {show_hours(bucket.minutes)}"
            + (f" (after {show(bucket.after_hours)} h)" if bucket.after_hours else "")
            for bucket in buckets
        )
        lines.append(make_line(
            OT_COMPONENTS[day_type], EARNING, "OVERTIME", rate * weighted / 60, currency,
            minor_unit=minor_unit, rounding=rounding, method="RATE_X_UNITS", explanation=explanation,
            units=units_4dp(minutes), rate=rate,
            sources=[{"type": "overtime_entry", "id": entry_id, "minutes": str(used)}
                     for entry_id, used in per_entry.items()],
        ))
    return OvertimePay(currency, rate, tuple(lines))
