"""Publish validation of a candidate roster: ``validate(schedule, limits, context) -> list[RuleResult]`` (AS-M5).

Owner doc ``docs/attendance-shift/03-shifts/02-rosters-rotations-assignment.md`` §6 (contract §6.1, rules §6.2,
IN illustrative values §6.3, coverage §6.4), §4.3 / §4.4 (locks, substitution), §5.4 (``notice_hours``), §8
(holiday modes), worked examples §14 and the golden vectors ``GV-AS-ROS-*`` of §15; pack fields
``appendix-a-country-packs.md`` §5 and the IN seeds §8.5. Pure (IG-AS-02): standard library, ``pytz`` and the
pure engines :mod:`app.services.shift_engine` / :mod:`app.services.rotation_engine`; no database, no clock,
no settings. The caller resolves everything (packs, policy, consents, day facts) and passes plain dataclasses.

Inputs (all frozen dataclasses below)
-------------------------------------
``ScheduleData.employees``: one :class:`EmployeeSchedule` per employee = ``employee_id``, ``work_timezone``
(IANA), employment window (``employment_start`` / ``employment_end``, inclusive, ``None`` = open),
``protected_groups`` (e.g. ``{"WOMEN"}``; the caller derives them, no date of birth or gender here),
``joiner_backfill`` and the candidate ``instances`` (published entries ⊕ drafts over the checked span —
changed dates ± 14 days). Each :class:`PlannedInstance`: ``shift_date`` (employee-local), ``kind`` WORK / OFF,
``entry_seq``, ``start_at`` / ``end_at`` (aware UTC, from ``shift_engine.build_instance``; ``None`` for OFF and
OPEN shifts), ``net_minutes`` (scheduled net), ``shift_id``, ``archived``, ``has_on_call``, ``changed`` (this
publication creates / changes / withdraws it), ``withdrawn``, the ``previous_*`` values of an UPDATED entry
(notice, late addition, cancellation), ``safety_critical`` / ``long_duty`` (lints), ``holiday_skeleton`` and
``holiday_unreviewed`` (a published WORK entry whose holiday arrived after its publication, §8).
:func:`from_shift_instance` builds one from a ``shift_engine.ShiftInstance``.

``LimitsData``: an :class:`EmployeeLimits` default plus per-employee overrides — every limit is a
:class:`Limit` (value, severity, source_ref, verified) or ``None`` (no limit); the week definition
(``ROLLING`` / ``FIXED``) and ``week_start_day`` (0 = Monday); protected-group night windows; notice;
``backdate_days``; ``holiday_mode``; Ramadan ``reduced_hours``; OT caps; lint switches; raise-only
``rule_severity``. The caller applies
§6.1 "Severity" (pack field severity, ``VERIFIED_FOR_PERIOD`` + ``enforce`` else at most WARN — except where
the rule table itself says REQUIRE_REASON under DRAFT, e.g. ``NIGHT_WORK_CONSENT_MISSING``) when it fills
``Limit.severity``; ``verified = False`` only changes the message key ("Compliance not verified").

``RuleContext``: employee-local ``today`` (per employee or one date), the publication instant and kind (notice),
full non-working holidays (global or per employee), consents, coverage rules, rotation coverage, tags,
payroll period states, leave impacts (computed by the leave service), projected OT minutes, pattern-level lint
results to merge and ``check_span_days``.

Semantics kept from §6.1: integrity rules (``SHIFT_OVERLAP``, ``NOT_EMPLOYED``, ``SHIFT_ARCHIVED``) only on
changed entries (overlap: any pair with a changed member, which covers the direct neighbours); touching
instances do not overlap; durations on UTC instants (a DST night of an 8-h shift is 7 or 9 h); weeks per
``week_definition``; when an employee has changed entries, working-time results are kept only if they touch
the checked span (changed dates ± ``check_span_days``). OPEN instances (no clock times) count for hours and
days but not for time-based rules.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytz

from app.services import rotation_engine as rot
from app.services import shift_engine as se
from app.services.rotation_engine import (  # re-exported: one RuleResult type for lints and publish rules
    BLOCK,
    REQUIRE_REASON,
    SEVERITIES,
    SEVERITY_RANK,
    WARN,
    NightWorkerRule,
    RuleResult,
)

__all__ = [
    "BLOCK", "REQUIRE_REASON", "WARN", "SEVERITIES", "RuleResult", "NightWorkerRule",
    "PlannedInstance", "EmployeeSchedule", "ScheduleData", "Limit", "NightWorkRule", "OtCap", "EmployeeLimits",
    "LimitsData", "Consent", "TagHolding", "CoverageRule", "RotationCoverage", "LeaveImpact", "RuleContext",
    "validate", "summarize", "notice_hours", "affected_start", "min_notice_hours", "from_shift_instance",
    "substitution_window", "substitution_issue", "ReducedHoursWindow", "entry_approves_holiday_work",
]

WORK, OFF = "WORK", "OFF"
ROLLING, FIXED = "ROLLING", "FIXED"
WEEK_DEFINITIONS = (ROLLING, FIXED)
FROM_END_OF_LAST_INSTANCE, ANY_FREE_INTERVAL = "FROM_END_OF_LAST_INSTANCE", "ANY_FREE_INTERVAL"
GRANTED = "GRANTED"
STANDING, PER_INSTANCE = "STANDING", "PER_INSTANCE"
NIGHT_WORK, COMPRESSED_WEEK, REST_WAIVER, SCHEDULE_CHANGE_ACCEPTED = (
    "NIGHT_WORK", "COMPRESSED_WEEK", "REST_WAIVER", "SCHEDULE_CHANGE_ACCEPTED")
OPEN_STATE, INPUT_LOCKED, FINALIZED = "OPEN", "INPUT_LOCKED", "FINALIZED"
EMPLOYER, EMPLOYEE, SYSTEM = "EMPLOYER", "EMPLOYEE", "SYSTEM"
KIND_REQUEST = "REQUEST"
REASON_EMPLOYMENT_ENDED = "EMPLOYMENT_ENDED"
HOLIDAY_OFF, WORK_THROUGH, HOLIDAY_SKELETON = rot.HOLIDAY_OFF, rot.WORK_THROUGH, rot.HOLIDAY_SKELETON

# rule keys (§6.2)
SHIFT_OVERLAP = "SHIFT_OVERLAP"
NOT_EMPLOYED = "NOT_EMPLOYED"
SHIFT_ARCHIVED = "SHIFT_ARCHIVED"
BACKDATE_LIMIT = "BACKDATE_LIMIT"
PERIOD_LOCKED = "PERIOD_LOCKED"
ROSTER_AFFECTS_APPROVED_LEAVE = "ROSTER_AFFECTS_APPROVED_LEAVE"
HOLIDAY_WORK = "HOLIDAY_WORK"
WEEKLY_REST_EXCEEDED = "WEEKLY_REST_EXCEEDED"
WEEKLY_REST_HOURS = "WEEKLY_REST_HOURS"
MIN_REST_VIOLATION = "MIN_REST_VIOLATION"
DAILY_HOURS_EXCEEDED = "DAILY_HOURS_EXCEEDED"
SPREAD_OVER_EXCEEDED = "SPREAD_OVER_EXCEEDED"
WEEKLY_HOURS_EXCEEDED = "WEEKLY_HOURS_EXCEEDED"
OT_CAP_PROJECTED = "OT_CAP_PROJECTED"
NIGHT_WORK_CONSENT_MISSING = "NIGHT_WORK_CONSENT_MISSING"
NOTICE_PERIOD_SHORT = "NOTICE_PERIOD_SHORT"
LATE_ADDITION_NO_CONSENT = "LATE_ADDITION_NO_CONSENT"
CANCELLATION_INSIDE_NOTICE = "CANCELLATION_INSIDE_NOTICE"
ON_CALL_NOT_ALLOWED = "ON_CALL_NOT_ALLOWED"
COVERAGE_BELOW_MINIMUM = "COVERAGE_BELOW_MINIMUM"
DOUBLE_SHIFT = "DOUBLE_SHIFT"
SUBSTITUTION_INVALID = "SUBSTITUTION_INVALID"
OUTDOOR_MIDDAY_BAN = "OUTDOOR_MIDDAY_BAN"  # pack seasonal.outdoor_midday_ban (EC AS-CTY-08)
ADOLESCENT_LIMITS = "ADOLESCENT_LIMITS"  # pack night_work.adolescent (EC AS-CTY-07)
RAMADAN_HOURS = "RAMADAN_HOURS"  # pack seasonal.ramadan_reduction_hours (EC X-INT-58)
INTEGRITY_RULES = frozenset({SHIFT_OVERLAP, NOT_EMPLOYED, SHIFT_ARCHIVED})

# SUBSTITUTION_INVALID details.rule (§4.4, SP-AS-040)
SUB_WORK_DATE_NOT_REST_DAY = "WORK_DATE_NOT_REST_DAY"
SUB_OFF_DATE_NOT_WORKING = "OFF_DATE_NOT_WORKING"
SUB_OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
SUB_MAX_CONSECUTIVE = "MAX_CONSECUTIVE"

_ONE_DAY = dt.timedelta(days=1)
_WEEK = dt.timedelta(days=7)
_HOURS_2DP = Decimal("0.01")
_SECONDS_PER_HOUR = Decimal(3600)


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class PlannedInstance:
    """One candidate entry of an employee (published ⊕ draft), with its instance from ``shift_engine``."""

    shift_date: dt.date
    kind: str = WORK  # WORK | OFF
    entry_seq: int = 1
    start_at: dt.datetime | None = None  # aware UTC; None for OFF and OPEN shifts
    end_at: dt.datetime | None = None
    net_minutes: int | None = None  # scheduled net minutes (OFF: None)
    shift_id: Any = None
    shift_version_id: int | None = None
    archived: bool = False  # references an archived shift
    has_on_call: bool = False  # an ON_CALL segment
    changed: bool = False  # this publication creates, changes or withdraws the entry
    withdrawn: bool = False  # the entry is withdrawn by this publication (changed must be true)
    previous_kind: str | None = None  # UPDATED: the published entry's kind before the change (None = CREATED)
    previous_start_at: dt.datetime | None = None
    previous_net_minutes: int | None = None
    safety_critical: bool = False
    long_duty: bool = False
    holiday_skeleton: bool = False  # kept WORK by HOLIDAY_SKELETON (no HOLIDAY_WORK result)
    # A published (unchanged) WORK entry whose date became a full holiday after its publication (§8, EC AS-INT-18):
    # nobody decided that holiday work yet, so the preview shows HOLIDAY_WORK for it (WARN) until the planner does.
    holiday_unreviewed: bool = False
    entry_id: int | None = None
    outdoor: bool = False  # shift_versions.outdoor or attendance_sites.outdoor (C-35)
    adolescent: bool = False  # under 18 at 00:00 local of shift_date (derived by the caller; no date of birth here)

    @property
    def active(self) -> bool:
        return not self.withdrawn

    @property
    def is_work(self) -> bool:
        return self.kind == WORK and not self.withdrawn

    @property
    def timed(self) -> bool:
        return self.start_at is not None and self.end_at is not None

    @property
    def gross_minutes(self) -> int | None:
        if not self.timed:
            return None
        return int((self.end_at - self.start_at).total_seconds()) // 60


@dataclass(frozen=True)
class EmployeeSchedule:
    employee_id: int
    work_timezone: str
    instances: tuple[PlannedInstance, ...] = ()
    employment_start: dt.date | None = None
    employment_end: dt.date | None = None  # last working day
    protected_groups: frozenset[str] = frozenset()
    joiner_backfill: bool = False  # late-entered joiner: BACKDATE_LIMIT is REQUIRE_REASON from employment_start

    def __post_init__(self) -> None:
        object.__setattr__(self, "instances", tuple(self.instances))
        object.__setattr__(self, "protected_groups", frozenset(self.protected_groups))


@dataclass(frozen=True)
class ScheduleData:
    employees: tuple[EmployeeSchedule, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "employees", tuple(self.employees))


@dataclass(frozen=True)
class Limit:
    """A resolved limit: the value with its severity and where it came from (pack field, policy, setting)."""

    value: Any
    severity: str = WARN
    source_ref: str | None = None
    verified: bool = True  # False -> message key suffix ".not_verified" ("Compliance not verified")


@dataclass(frozen=True)
class NightWorkRule:
    """Pack ``night_work.protected_groups[]`` row resolved for the employee (IN women 19:00–06:00)."""

    window: tuple[str, str]
    requires_consent: bool = True
    severity: str = REQUIRE_REASON
    source_ref: str | None = None
    verified: bool = True


@dataclass(frozen=True)
class OutdoorBan:
    """Pack ``seasonal.outdoor_midday_ban`` (appendix-a §5.7): no outdoor work in ``window`` from ``season_from`` to
    ``season_to`` (``MM-DD``, both inclusive, work zone; wraps the year end; a ``02-28`` end includes 29 February)."""

    window: tuple[str, str]
    season_from: str
    season_to: str
    severity: str = WARN
    source_ref: str | None = None
    verified: bool = True


@dataclass(frozen=True)
class AdolescentRule:
    """Pack ``night_work.adolescent`` (appendix-a §5.5) for instances whose ``adolescent`` flag is set."""

    forbidden_window: tuple[str, str] | None = None
    max_hours: Decimal | None = None  # net hours of one instance
    max_stretch_hours: Decimal | None = None  # continuous work before an interval
    interval_minutes: int | None = None
    severity: str = BLOCK
    source_ref: str | None = None
    verified: bool = True


@dataclass(frozen=True)
class Waiver:
    """A pack ``exemptions[]`` entry the establishment holds (appendix-a §3.3, §5.10): ``fields`` (``hours.daily_max``,
    ``hours.spread_over``, ``night_work.protected_groups.<GROUP>``) are waived for shift dates from ``valid_from`` to
    ``valid_to``, both inclusive; outside them the field applies again (EC AS-CTY-17)."""

    code: str
    fields: frozenset[str]
    valid_from: dt.date
    valid_to: dt.date | None = None

    def covers(self, field_path: str, day: dt.date) -> bool:
        return (field_path in self.fields and self.valid_from <= day
                and (self.valid_to is None or day <= self.valid_to))


@dataclass(frozen=True)
class ReducedHoursWindow:
    """A pack reduced-hours period such as Ramadan (appendix-a ``seasonal.ramadan_periods[]`` with
    ``ramadan_reduction_hours``; §6.2 ``RAMADAN_HOURS``): inside ``from_date..to_date`` (both inclusive) the normal day
    is ``normal_hours`` (AE 8 − 2 = 6 h)."""

    code: str
    from_date: dt.date
    to_date: dt.date
    normal_hours: Decimal
    source_ref: str = "WORKING_TIME pack seasonal.ramadan_reduction_hours"
    verified: bool = True

    def covers(self, day: dt.date) -> bool:
        return self.from_date <= day <= self.to_date


@dataclass(frozen=True)
class OtCap:
    """Pack ``overtime.caps[]`` entry (appendix-a §5.4). ``window`` DAY | WEEK | MONTH | QUARTER | YEAR."""

    window: str
    hours: Decimal
    length: int = 1
    window_kind: str = "CALENDAR_QUARTER"  # QUARTER only: CALENDAR_QUARTER | ROLLING_3_MONTHS | FINANCIAL_QUARTER
    source_ref: str | None = None
    severity: str = WARN


@dataclass(frozen=True)
class EmployeeLimits:
    min_rest_hours: Limit | None = None  # rest.min_between_shifts_hours / policy min_rest_hours
    rest_waivable_with_consent: bool = False  # rest.waivable_with_consent (clopening)
    daily_max_hours: Limit | None = None  # hours.daily_max_including_ot_hours
    compressed_week_allowed: bool = False  # lifts DAILY_HOURS_EXCEEDED with a COMPRESSED_WEEK consent
    spread_over_max_hours: Limit | None = None  # hours.spread_over_max_hours
    weekly_max_hours: Limit | None = None  # hours.weekly_max_including_ot_hours / policy max_hours_per_week
    max_days_per_week: Limit | None = None  # LH weekly_rest.max_days_per_week / policy
    max_consecutive_days: Limit | None = None  # LH weekly_rest.max_consecutive_days / policy
    weekly_rest_min_hours: Limit | None = None  # rest.weekly_min_hours
    weekly_rest_basis: str = FROM_END_OF_LAST_INSTANCE  # rest.weekly_min_basis
    week_definition: str = ROLLING  # hours.week_definition
    weekly_rest_week_definition: str | None = None  # LH weekly_rest's own week, when it carries one
    week_start_day: int = 0  # payroll.calendar.week_start_day, 0 = Monday
    night_work_groups: Mapping[str, NightWorkRule] = field(default_factory=dict)
    advance_notice_hours: Limit | None = None  # scheduling.advance_notice_hours
    advance_notice_days: Limit | None = None  # scheduling.advance_notice_days (wins when set)
    employee_initiated_exempt: bool = True  # scheduling.employee_initiated_exempt
    late_additions_require_consent: Limit | None = None  # value True
    cancellation_notice_hours: Limit | None = None
    on_call_allowed: bool = True
    on_call_severity: str = WARN
    backdate_days: int | None = 7  # shift.roster.backdate_days (BLOCK)
    holiday_mode: str = HOLIDAY_OFF  # SCHEDULING_POLICY.holiday_mode
    ot_caps: tuple[OtCap, ...] = ()
    lints_enabled: bool = True
    lints_muted: frozenset[str] = frozenset()
    night_window: tuple[str, str] = rot.DEFAULT_NIGHT_WINDOW  # attendance.night_window (lints)
    night_threshold_hours: Decimal = rot.DEFAULT_NIGHT_THRESHOLD_HOURS
    night_worker: NightWorkerRule | None = None
    outdoor_midday_ban: OutdoorBan | None = None
    adolescent: AdolescentRule | None = None
    reduced_hours: tuple[ReducedHoursWindow, ...] = ()  # pack Ramadan periods (RAMADAN_HOURS, EC X-INT-58)
    rule_severity: Mapping[str, str] = field(default_factory=dict)  # SCHEDULING_POLICY.rule_severity (raise only)
    waivers: tuple[Waiver, ...] = ()  # pack exemptions the establishment holds (EC AS-CTY-17)

    def waived(self, field_path: str, day: dt.date) -> bool:
        return any(w.covers(field_path, day) for w in self.waivers)


@dataclass(frozen=True)
class LimitsData:
    default: EmployeeLimits = field(default_factory=EmployeeLimits)
    per_employee: Mapping[int, EmployeeLimits] = field(default_factory=dict)

    def for_employee(self, employee_id: int) -> EmployeeLimits:
        return self.per_employee.get(employee_id, self.default)


@dataclass(frozen=True)
class Consent:
    """An ``attendance_consents`` row (C-22). ``PER_INSTANCE`` consents cover ``subject_date`` only."""

    employee_id: int
    kind: str
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    status: str = GRANTED
    scope: str = STANDING
    subject_date: dt.date | None = None
    consent_id: int | None = None

    def valid_on(self, day: dt.date) -> bool:
        if self.status != GRANTED:
            return False
        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return self.scope != PER_INSTANCE or self.subject_date == day


@dataclass(frozen=True)
class TagHolding:
    tag: str
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None

    def valid_on(self, day: dt.date) -> bool:
        return (self.valid_from is None or self.valid_from <= day) and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True)
class CoverageRule:
    """``SCHEDULING_POLICY.coverage_rules[]`` with team / branch / site resolved to ``employee_ids`` (None = all)."""

    shift_id: Any
    min_count: int
    weekdays: frozenset[int] | None = None  # 0 = Monday; None = every day
    employee_ids: frozenset[int] | None = None
    required_tags: tuple[str, ...] = ()
    from_date: dt.date | None = None
    to_date: dt.date | None = None
    rule_ref: str | None = None


@dataclass(frozen=True)
class RotationCoverage:
    """A rotation's structural ``coverage``: per date, crews with >= 1 member on the token's shift (§6.4)."""

    pattern_ref: str
    crews: Mapping[str, frozenset[int]]  # crew_code -> employee ids
    required: Mapping[Any, int]  # shift_id -> crews
    from_date: dt.date | None = None
    to_date: dt.date | None = None


@dataclass(frozen=True)
class LeaveImpact:
    """An APPROVED / PENDING leave whose charged units the change alters (computed by the leave service)."""

    employee_id: int
    leave_ref: Any
    dates: tuple[dt.date, ...]
    old_units: Decimal
    new_units: Decimal


@dataclass(frozen=True)
class RuleContext:
    today: dt.date | Mapping[int, dt.date] | None = None  # employee-local today
    publish_at: dt.datetime | None = None  # publication instant (aware)
    publication_kind: str = "PERIOD"  # PERIOD | DIRECT | REQUEST | SUBSTITUTION | ROTATION_AUTO
    initiated_by: str = EMPLOYER  # EMPLOYER | EMPLOYEE | SYSTEM
    reason_code: str | None = None
    holidays: Collection[dt.date] | Mapping[int, Collection[dt.date]] = ()  # full non-working holidays
    consents: Sequence[Consent] = ()
    coverage_rules: Sequence[CoverageRule] = ()
    rotation_coverage: Sequence[RotationCoverage] = ()
    employee_tags: Mapping[int, Sequence[TagHolding]] = field(default_factory=dict)
    period_states: Mapping[dt.date, str] = field(default_factory=dict)  # calendars.period_state_on
    affected_leave: Sequence[LeaveImpact] = ()
    ot_minutes: Mapping[int, Mapping[dt.date, int]] = field(default_factory=dict)  # projected + prior OT minutes
    pattern_lints: Sequence[RuleResult] = ()  # rotation_engine.lint of the patterns behind the entries
    check_span_days: int | None = 14

    def today_for(self, employee_id: int) -> dt.date | None:
        if isinstance(self.today, Mapping):
            return self.today.get(employee_id)
        return self.today

    def holidays_for(self, employee_id: int) -> frozenset[dt.date]:
        if isinstance(self.holidays, Mapping):
            return frozenset(self.holidays.get(employee_id, ()))
        return frozenset(self.holidays)

    def consent(self, employee_id: int, kind: str, day: dt.date) -> Consent | None:
        for consent in self.consents:
            if consent.employee_id == employee_id and consent.kind == kind and consent.valid_on(day):
                return consent
        return None


# --------------------------------------------------------------------------- helpers


def _hours(minutes: int | Decimal) -> Decimal:
    return (Decimal(minutes) / 60).quantize(_HOURS_2DP, rounding=ROUND_HALF_UP)


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _local_midnight(day: dt.date, tz: str) -> dt.datetime:
    return se.resolve_local_time(tz, dt.datetime.combine(day, dt.time()))


def _wall(instant: dt.datetime, tz: str) -> dt.datetime:
    return instant.astimezone(pytz.timezone(tz)).replace(tzinfo=None)


def _severity(key: str, base: str, limits: EmployeeLimits, cap: str | None = None) -> str:
    """Raise-only ``rule_severity`` override (§6.1), optionally capped (coverage never BLOCKs in v1)."""
    severity = base
    override = limits.rule_severity.get(key)
    if override in SEVERITY_RANK and SEVERITY_RANK[override] > SEVERITY_RANK[severity]:
        severity = override
    if cap is not None and SEVERITY_RANK[severity] > SEVERITY_RANK[cap]:
        severity = cap
    return severity


def _message(key: str, verified: bool = True, variant: str | None = None) -> str:
    text = f"roster.rule.{key.lower()}" + (f".{variant}" if variant else "")
    return text if verified else text + ".not_verified"


def _windows(dates: Iterable[dt.date], definition: str, week_start: int) -> list[tuple[dt.date, dt.date]]:
    """7-date windows touching ``dates``: every rolling window, or the fixed weeks starting on ``week_start``."""
    days = sorted(set(dates))
    if not days:
        return []
    if definition == FIXED:
        first = days[0] - dt.timedelta(days=(days[0].weekday() - week_start) % 7)
        out, start = [], first
        while start <= days[-1]:
            out.append((start, start + dt.timedelta(days=6)))
            start += _WEEK
        return out
    if definition != ROLLING:
        raise ValueError(f"week_definition must be one of {WEEK_DEFINITIONS}")
    out, start = [], days[0] - dt.timedelta(days=6)
    while start <= days[-1]:
        out.append((start, start + dt.timedelta(days=6)))
        start += _ONE_DAY
    return out


def _merge(failing: list[tuple[dt.date, dt.date, Any]], definition: str,
           pick: Any = max) -> list[tuple[dt.date, dt.date, Any]]:
    """Merge overlapping rolling windows into one finding (value = ``pick`` of the merged values)."""
    if definition == FIXED:
        return failing
    merged: list[tuple[dt.date, dt.date, list[Any]]] = []
    for lo, hi, value in failing:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]), merged[-1][2] + [value])
        else:
            merged.append((lo, hi, [value]))
    return [(lo, hi, pick(values)) for lo, hi, values in merged]


def _in(day: dt.date, lo: dt.date, hi: dt.date) -> bool:
    return lo <= day <= hi


# --------------------------------------------------------------------------- notice (§5.4)


def notice_hours(start_at: dt.datetime, publish_at: dt.datetime) -> Decimal:
    """``(affected instance start − publication instant) / 3600``, exact seconds, half-up to 2 decimals."""
    seconds = Decimal(int((start_at - publish_at).total_seconds()))
    return (seconds / _SECONDS_PER_HOUR).quantize(_HOURS_2DP, rounding=ROUND_HALF_UP)


def _start_of(kind: str | None, start_at: dt.datetime | None, day: dt.date, tz: str) -> dt.datetime:
    """Instance start; for OFF (and OPEN) entries 00:00 local of the shift date (§5.4)."""
    if kind == WORK and start_at is not None:
        return start_at
    return _local_midnight(day, tz)


def affected_start(instance: PlannedInstance, work_tz: str) -> dt.datetime:
    """CREATED -> the new instance; WITHDRAWN -> the old one (``instance`` is the withdrawn entry); UPDATED ->
    the earlier of the old and new starts (§5.4)."""
    new = _start_of(instance.kind, instance.start_at, instance.shift_date, work_tz)
    if instance.withdrawn or instance.previous_kind is None:
        return new
    old = _start_of(instance.previous_kind, instance.previous_start_at, instance.shift_date, work_tz)
    return min(old, new)


def min_notice_hours(schedule: ScheduleData, publish_at: dt.datetime, *, work_only: bool = False) -> Decimal | None:
    """Minimum ``notice_hours`` over the changed entries (``roster_publications.min_notice_hours``)."""
    values = [notice_hours(affected_start(i, e.work_timezone), publish_at)
              for e in schedule.employees for i in e.instances
              if i.changed and (not work_only or i.kind == WORK or i.previous_kind == WORK)]
    return min(values) if values else None


def _notice_short(instance: PlannedInstance, tz: str, limits: EmployeeLimits,
                  publish_at: dt.datetime) -> tuple[bool, Any, Any, str, Limit] | None:
    start = affected_start(instance, tz)
    if limits.advance_notice_days is not None:
        days = int(limits.advance_notice_days.value)
        start_wall, publish_wall = _wall(start, tz), _wall(publish_at, tz)
        short = start_wall - dt.timedelta(days=days) > publish_wall  # wall-clock arithmetic, equality passes
        measured = (Decimal(int((start_wall - publish_wall).total_seconds())) / Decimal(86400)).quantize(_HOURS_2DP)
        return short, measured, days, "days", limits.advance_notice_days
    if limits.advance_notice_hours is not None:
        hours = notice_hours(start, publish_at)
        return hours < _dec(limits.advance_notice_hours.value), hours, _dec(limits.advance_notice_hours.value), "h", \
            limits.advance_notice_hours
    return None


# --------------------------------------------------------------------------- per-employee rules


def _integrity(emp: EmployeeSchedule, lim: EmployeeLimits, out: list[RuleResult]) -> None:
    eid = emp.employee_id
    timed = sorted((i for i in emp.instances if i.is_work and i.timed), key=lambda i: (i.start_at, i.end_at))
    for a_pos, a in enumerate(timed):
        for b in timed[a_pos + 1:]:
            if b.start_at >= a.end_at:
                break
            if (a.changed or b.changed) and b.start_at < a.end_at and a.start_at < b.end_at:
                overlap = se._overlap_seconds(a.start_at, a.end_at, b.start_at, b.end_at) // 60
                out.append(RuleResult(SHIFT_OVERLAP, _severity(SHIFT_OVERLAP, BLOCK, lim), eid,
                                      tuple(sorted({a.shift_date, b.shift_date})), overlap, 0, "min", "integrity",
                                      _message(SHIFT_OVERLAP),
                                      {"entry_seqs": [a.entry_seq, b.entry_seq]}))
    for i in emp.instances:
        if not i.changed or i.withdrawn:
            continue
        outside = (emp.employment_start is not None and i.shift_date < emp.employment_start) or \
                  (emp.employment_end is not None and i.shift_date > emp.employment_end)
        if outside:
            out.append(RuleResult(NOT_EMPLOYED, _severity(NOT_EMPLOYED, BLOCK, lim), eid, (i.shift_date,),
                                  i.shift_date.isoformat(), f"{emp.employment_start}..{emp.employment_end}", "date",
                                  "integrity", _message(NOT_EMPLOYED)))
        if i.archived and i.kind == WORK:
            out.append(RuleResult(SHIFT_ARCHIVED, _severity(SHIFT_ARCHIVED, BLOCK, lim), eid, (i.shift_date,),
                                  i.shift_id, None, None, "integrity", _message(SHIFT_ARCHIVED)))


def _backdate_and_locks(emp: EmployeeSchedule, lim: EmployeeLimits, ctx: RuleContext, out: list[RuleResult]) -> None:
    eid, today = emp.employee_id, ctx.today_for(emp.employee_id)
    changed = [i for i in emp.instances if i.changed]
    if today is not None and lim.backdate_days is not None:
        floor = today - dt.timedelta(days=lim.backdate_days)
        groups: dict[str, list[dt.date]] = {}
        for i in changed:
            if i.shift_date < floor:
                backfill = emp.joiner_backfill and emp.employment_start is not None and \
                    i.shift_date >= emp.employment_start
                groups.setdefault(REQUIRE_REASON if backfill else BLOCK, []).append(i.shift_date)
        for base, days in groups.items():
            days = sorted(set(days))
            out.append(RuleResult(BACKDATE_LIMIT, _severity(BACKDATE_LIMIT, base, lim), eid, tuple(days),
                                  (today - days[0]).days, lim.backdate_days, "days", "shift.roster.backdate_days",
                                  _message(BACKDATE_LIMIT, variant="joiner_backfill" if base == REQUIRE_REASON else None)))
    locked: dict[str, list[dt.date]] = {}
    for i in changed:
        state = ctx.period_states.get(i.shift_date, OPEN_STATE)
        if state in (INPUT_LOCKED, FINALIZED):
            locked.setdefault(state, []).append(i.shift_date)
    for state, days in locked.items():
        base = WARN if state == INPUT_LOCKED else REQUIRE_REASON
        out.append(RuleResult(PERIOD_LOCKED, _severity(PERIOD_LOCKED, base, lim), eid, tuple(sorted(set(days))),
                              state, OPEN_STATE, "state", "calendars.period_state_on",
                              _message(PERIOD_LOCKED, variant=state.lower())))


def _day_rules(emp: EmployeeSchedule, lim: EmployeeLimits, ctx: RuleContext, out: list[RuleResult]) -> None:
    eid = emp.employee_id
    by_date: dict[dt.date, list[PlannedInstance]] = {}
    for i in emp.instances:
        if i.is_work:
            by_date.setdefault(i.shift_date, []).append(i)
    holidays = ctx.holidays_for(eid)
    for day, items in sorted(by_date.items()):
        if len(items) > 1:
            out.append(RuleResult(DOUBLE_SHIFT, _severity(DOUBLE_SHIFT, REQUIRE_REASON, lim), eid, (day,), len(items),
                                  1, "entries", "SCHEDULING_POLICY", _message(DOUBLE_SHIFT)))
        if day in holidays and lim.holiday_mode in (HOLIDAY_OFF, HOLIDAY_SKELETON):
            if not (lim.holiday_mode == HOLIDAY_SKELETON and all(i.holiday_skeleton for i in items)):
                # §8: holiday work planned by this publication needs a reason; a published entry the holiday arrived
                # after is shown (WARN) until the planner decides (EC AS-INT-18); an entry published once the
                # holiday was known was decided then and is not asked again.
                base = (REQUIRE_REASON if any(i.changed for i in items) else
                        WARN if any(i.holiday_unreviewed for i in items) else None)
                if base is not None:
                    out.append(RuleResult(HOLIDAY_WORK, _severity(HOLIDAY_WORK, base, lim), eid, (day,), None, None,
                                          None, "SCHEDULING_POLICY.holiday_mode",
                                          _message(HOLIDAY_WORK, variant=None if base == REQUIRE_REASON
                                                   else "unreviewed")))
        net = sum(i.net_minutes or 0 for i in items)
        if (lim.daily_max_hours is not None and Decimal(net) > _dec(lim.daily_max_hours.value) * 60
                and not lim.waived("hours.daily_max", day)):
            lifted = lim.compressed_week_allowed and ctx.consent(eid, COMPRESSED_WEEK, day) is not None
            if not lifted:
                out.append(RuleResult(DAILY_HOURS_EXCEEDED, _severity(DAILY_HOURS_EXCEEDED, lim.daily_max_hours.severity,
                                                                      lim), eid, (day,), _hours(net),
                                      _dec(lim.daily_max_hours.value), "h",
                                      lim.daily_max_hours.source_ref or "hours.daily_max_including_ot_hours",
                                      _message(DAILY_HOURS_EXCEEDED, lim.daily_max_hours.verified)))
        window = next((w for w in lim.reduced_hours if w.covers(day)), None)
        if window is not None and Decimal(net) > _dec(window.normal_hours) * 60:
            out.append(RuleResult(RAMADAN_HOURS, _severity(RAMADAN_HOURS, WARN, lim), eid, (day,), _hours(net),
                                  _dec(window.normal_hours), "h", window.source_ref,
                                  _message(RAMADAN_HOURS, window.verified), {"window": window.code}))
        timed = [i for i in items if i.timed]
        if lim.spread_over_max_hours is not None and timed and not lim.waived("hours.spread_over", day):
            spread = int((max(i.end_at for i in timed) - min(i.start_at for i in timed)).total_seconds()) // 60
            if Decimal(spread) > _dec(lim.spread_over_max_hours.value) * 60:
                out.append(RuleResult(SPREAD_OVER_EXCEEDED, _severity(SPREAD_OVER_EXCEEDED,
                                                                      lim.spread_over_max_hours.severity, lim),
                                      eid, (day,), _hours(spread), _dec(lim.spread_over_max_hours.value), "h",
                                      lim.spread_over_max_hours.source_ref or "hours.spread_over_max_hours",
                                      _message(SPREAD_OVER_EXCEEDED, lim.spread_over_max_hours.verified)))
        if not lim.on_call_allowed and any(i.has_on_call for i in items):
            out.append(RuleResult(ON_CALL_NOT_ALLOWED, _severity(ON_CALL_NOT_ALLOWED, lim.on_call_severity, lim), eid,
                                  (day,), True, False, None, "scheduling.on_call_allowed",
                                  _message(ON_CALL_NOT_ALLOWED)))


def _rest_rules(emp: EmployeeSchedule, lim: EmployeeLimits, ctx: RuleContext, out: list[RuleResult]) -> None:
    eid, tz = emp.employee_id, emp.work_timezone
    timed = sorted((i for i in emp.instances if i.is_work and i.timed), key=lambda i: (i.start_at, i.end_at))
    if lim.min_rest_hours is not None:
        minimum = _dec(lim.min_rest_hours.value) * 60
        for prev, nxt in zip(timed, timed[1:]):
            rest = int((nxt.start_at - prev.end_at).total_seconds()) // 60
            if rest < 0 or Decimal(rest) >= minimum:
                continue  # overlaps are SHIFT_OVERLAP
            base, details = lim.min_rest_hours.severity, {}
            if lim.rest_waivable_with_consent:
                consent = ctx.consent(eid, REST_WAIVER, nxt.shift_date)
                if consent is not None:
                    base, details = REQUIRE_REASON, {"consent_id": consent.consent_id}
            out.append(RuleResult(MIN_REST_VIOLATION, _severity(MIN_REST_VIOLATION, base, lim), eid,
                                  tuple(sorted({prev.shift_date, nxt.shift_date})), _hours(rest),
                                  _dec(lim.min_rest_hours.value), "h",
                                  lim.min_rest_hours.source_ref or "rest.min_between_shifts_hours",
                                  _message(MIN_REST_VIOLATION, lim.min_rest_hours.verified), details))

    limit = lim.weekly_rest_min_hours
    if limit is None or not timed:
        return
    minimum = _dec(limit.value) * 3600
    # free intervals between the end of one instance and the start of the next; open ends are unbounded
    gaps: list[tuple[dt.datetime | None, dt.datetime | None]] = [(None, timed[0].start_at)]
    gaps += [(a.end_at, b.start_at) for a, b in zip(timed, timed[1:]) if b.start_at > a.end_at]
    gaps.append((timed[-1].end_at, None))
    definition = lim.week_definition
    failing: list[tuple[dt.date, dt.date, Decimal]] = []
    for lo, hi in _windows((i.shift_date for i in timed), definition, lim.week_start_day):
        w_start, w_end = _local_midnight(lo, tz), _local_midnight(hi + _ONE_DAY, tz)
        best: Decimal | None = None
        satisfied = False
        for g_start, g_end in gaps:
            if (g_end is not None and g_end <= w_start) or (g_start is not None and g_start >= w_end):
                continue
            if g_start is None or g_end is None:
                satisfied = True
                break
            seconds = Decimal(int((g_end - g_start).total_seconds()))
            best = seconds if best is None else max(best, seconds)
            if seconds >= minimum:
                satisfied = True
                break
        if not satisfied:
            failing.append((lo, hi, best if best is not None else Decimal(0)))
    for lo, hi, seconds in _merge(failing, definition, pick=min):
        dates = tuple(sorted({i.shift_date for i in timed if _in(i.shift_date, lo, hi)}))
        out.append(RuleResult(WEEKLY_REST_HOURS, _severity(WEEKLY_REST_HOURS, limit.severity, lim), eid, dates,
                              (seconds / _SECONDS_PER_HOUR).quantize(_HOURS_2DP, rounding=ROUND_HALF_UP),
                              _dec(limit.value), "h", limit.source_ref or "rest.weekly_min_hours",
                              _message(WEEKLY_REST_HOURS, limit.verified),
                              {"window": [lo.isoformat(), hi.isoformat()], "basis": lim.weekly_rest_basis,
                               "week_definition": definition}))


def _week_rules(emp: EmployeeSchedule, lim: EmployeeLimits, out: list[RuleResult]) -> None:
    eid = emp.employee_id
    minutes: dict[dt.date, int] = {}
    for i in emp.instances:
        if i.is_work:
            minutes[i.shift_date] = minutes.get(i.shift_date, 0) + (i.net_minutes or 0)
    if not minutes:
        return
    if lim.weekly_max_hours is not None:
        limit_minutes = _dec(lim.weekly_max_hours.value) * 60
        failing = []
        for lo, hi in _windows(minutes, lim.week_definition, lim.week_start_day):
            total = sum(m for d, m in minutes.items() if _in(d, lo, hi))
            if Decimal(total) > limit_minutes:
                failing.append((lo, hi, total))
        for lo, hi, total in _merge(failing, lim.week_definition):
            out.append(RuleResult(WEEKLY_HOURS_EXCEEDED, _severity(WEEKLY_HOURS_EXCEEDED, lim.weekly_max_hours.severity,
                                                                   lim), eid,
                                  tuple(sorted(d for d in minutes if _in(d, lo, hi))), _hours(total),
                                  _dec(lim.weekly_max_hours.value), "h",
                                  lim.weekly_max_hours.source_ref or "hours.weekly_max_including_ot_hours",
                                  _message(WEEKLY_HOURS_EXCEEDED, lim.weekly_max_hours.verified),
                                  {"window": [lo.isoformat(), hi.isoformat()], "week_definition": lim.week_definition}))
    rest_definition = lim.weekly_rest_week_definition or lim.week_definition
    if lim.max_days_per_week is not None:
        failing = []
        for lo, hi in _windows(minutes, rest_definition, lim.week_start_day):
            count = sum(1 for d in minutes if _in(d, lo, hi))
            if count > int(lim.max_days_per_week.value):
                failing.append((lo, hi, count))
        for lo, hi, count in _merge(failing, rest_definition):
            out.append(RuleResult(WEEKLY_REST_EXCEEDED, _severity(WEEKLY_REST_EXCEEDED, lim.max_days_per_week.severity,
                                                                  lim), eid,
                                  tuple(sorted(d for d in minutes if _in(d, lo, hi))), count,
                                  int(lim.max_days_per_week.value), "days",
                                  lim.max_days_per_week.source_ref or "weekly_rest.max_days_per_week",
                                  _message(WEEKLY_REST_EXCEEDED, lim.max_days_per_week.verified, "days_per_week"),
                                  {"check": "DAYS_PER_WEEK", "window": [lo.isoformat(), hi.isoformat()],
                                   "week_definition": rest_definition}))
    if lim.max_consecutive_days is not None:
        for run in rot._runs({d: True for d in minutes}):
            if len(run) > int(lim.max_consecutive_days.value):
                out.append(RuleResult(WEEKLY_REST_EXCEEDED, _severity(WEEKLY_REST_EXCEEDED,
                                                                      lim.max_consecutive_days.severity, lim), eid,
                                      tuple(run), len(run), int(lim.max_consecutive_days.value), "days",
                                      lim.max_consecutive_days.source_ref or "weekly_rest.max_consecutive_days",
                                      _message(WEEKLY_REST_EXCEEDED, lim.max_consecutive_days.verified,
                                               "consecutive_days"), {"check": "CONSECUTIVE_DAYS"}))


def _night_work(emp: EmployeeSchedule, lim: EmployeeLimits, ctx: RuleContext, out: list[RuleResult]) -> None:
    eid = emp.employee_id
    for group in sorted(emp.protected_groups):
        rule = lim.night_work_groups.get(group)
        if rule is None or not rule.requires_consent:
            continue
        for i in emp.instances:
            if not (i.is_work and i.timed) or lim.waived(f"night_work.protected_groups.{group}", i.shift_date):
                continue
            inside = rot.window_overlap_minutes(i.start_at, i.end_at, emp.work_timezone, rule.window)
            if inside > 0 and ctx.consent(eid, NIGHT_WORK, i.shift_date) is None:
                out.append(RuleResult(NIGHT_WORK_CONSENT_MISSING, _severity(NIGHT_WORK_CONSENT_MISSING, rule.severity,
                                                                            lim), eid, (i.shift_date,),
                                      _hours(inside), "-".join(rule.window), "h in window",
                                      rule.source_ref or "night_work.protected_groups[]",
                                      _message(NIGHT_WORK_CONSENT_MISSING, rule.verified), {"group": group}))


def season_contains(season_from: str, season_to: str, day: dt.date) -> bool:
    """appendix-a §4.2 season dates: ``MM-DD``, both inclusive; ``from`` later than ``to`` wraps the year end; a ``to``
    of ``02-28`` includes 29 February in a leap year (EC AS-CTY-08)."""
    start = tuple(int(x) for x in season_from.split("-"))
    end = tuple(int(x) for x in season_to.split("-"))
    if end == (2, 28):
        end = (2, 29)
    md = (day.month, day.day)
    return start <= md <= end if start <= end else (md >= start or md <= end)


def season_window_minutes(start_at: dt.datetime, end_at: dt.datetime, zone: str, window: tuple[str, str],
                          season_from: str, season_to: str) -> int:
    """Minutes of ``[start_at, end_at)`` inside the daily local ``window`` on the dates of the season only."""
    total = 0
    day = rot._local_date(start_at, zone) - _ONE_DAY
    last = rot._local_date(end_at, zone)
    while day <= last:
        if season_contains(season_from, season_to, day):
            w_start, w_end = rot._window_bounds(day, window, zone)
            total += se._overlap_seconds(start_at, end_at, w_start, w_end)
        day += _ONE_DAY
    return total // 60


def _outdoor_ban(emp: EmployeeSchedule, lim: EmployeeLimits, out: list[RuleResult]) -> None:
    """``OUTDOOR_MIDDAY_BAN``: outdoor work overlapping the pack's midday window on a date of its season (WARN;
    BLOCK when the pack is VERIFIED)."""
    ban = lim.outdoor_midday_ban
    if ban is None:
        return
    for i in emp.instances:
        if not (i.is_work and i.timed and i.outdoor):
            continue
        inside = season_window_minutes(i.start_at, i.end_at, emp.work_timezone, ban.window, ban.season_from,
                                       ban.season_to)
        if inside > 0:
            out.append(RuleResult(OUTDOOR_MIDDAY_BAN, _severity(OUTDOOR_MIDDAY_BAN, ban.severity, lim), emp.employee_id,
                                  (i.shift_date,), _hours(inside), "-".join(ban.window), "h in window",
                                  ban.source_ref or "seasonal.outdoor_midday_ban",
                                  _message(OUTDOOR_MIDDAY_BAN, ban.verified),
                                  {"season": f"{ban.season_from}..{ban.season_to}"}))


def _adolescent_limits(emp: EmployeeSchedule, lim: EmployeeLimits, out: list[RuleResult]) -> None:
    """``ADOLESCENT_LIMITS`` for instances of an under-18 employee (evaluated once per instance by its shift date,
    so a night that starts the day before the 18th birthday stays under the limits for its whole length). The
    stretch limit is read from the instance: longer than ``max_stretch_hours`` with an unpaid break shorter than
    ``interval_minutes``. The message never shows the date of birth ("under 18 on this date")."""
    rule = lim.adolescent
    if rule is None:
        return
    for i in emp.instances:
        if not (i.is_work and i.timed and i.adolescent):
            continue
        breaches: list[tuple[str, Any, Any, str]] = []
        if rule.forbidden_window is not None:
            inside = rot.window_overlap_minutes(i.start_at, i.end_at, emp.work_timezone, rule.forbidden_window)
            if inside > 0:
                breaches.append(("FORBIDDEN_WINDOW", _hours(inside), "-".join(rule.forbidden_window), "h in window"))
        if rule.max_hours is not None and i.net_minutes is not None and Decimal(i.net_minutes) / 60 > rule.max_hours:
            breaches.append(("MAX_HOURS", _hours(i.net_minutes), rule.max_hours, "h"))
        gross = i.gross_minutes
        if rule.max_stretch_hours is not None and gross is not None and Decimal(gross) / 60 > rule.max_stretch_hours:
            unpaid = gross - (i.net_minutes if i.net_minutes is not None else gross)
            if rule.interval_minutes is None or unpaid < rule.interval_minutes:
                breaches.append(("MAX_STRETCH", _hours(gross), rule.max_stretch_hours, "h without an interval"))
        for kind, measured, limit, unit in breaches:
            out.append(RuleResult(ADOLESCENT_LIMITS, _severity(ADOLESCENT_LIMITS, rule.severity, lim), emp.employee_id,
                                  (i.shift_date,), measured, limit, unit, rule.source_ref or "night_work.adolescent",
                                  _message(ADOLESCENT_LIMITS, rule.verified), {"limit": kind}))


def _notice_rules(emp: EmployeeSchedule, lim: EmployeeLimits, ctx: RuleContext, out: list[RuleResult]) -> None:
    if ctx.publish_at is None:
        return
    eid, tz = emp.employee_id, emp.work_timezone
    employee_request = ctx.publication_kind == KIND_REQUEST or ctx.initiated_by == EMPLOYEE
    exempt = (employee_request and lim.employee_initiated_exempt) or \
        (ctx.initiated_by == SYSTEM and ctx.reason_code == REASON_EMPLOYMENT_ENDED)
    if exempt:
        return
    for i in emp.instances:
        if not i.changed:
            continue
        check = _notice_short(i, tz, lim, ctx.publish_at)
        hours = notice_hours(affected_start(i, tz), ctx.publish_at)
        inside_notice = bool(check and check[0])
        if inside_notice:
            _, measured, limit, unit, src = check
            out.append(RuleResult(NOTICE_PERIOD_SHORT, _severity(NOTICE_PERIOD_SHORT, src.severity, lim), eid,
                                  (i.shift_date,), measured, limit, unit,
                                  src.source_ref or ("scheduling.advance_notice_days" if unit == "days"
                                                     else "scheduling.advance_notice_hours"),
                                  _message(NOTICE_PERIOD_SHORT, src.verified), {"notice_hours": str(hours)}))
        adds_hours = i.is_work and (i.previous_kind in (None, OFF) or (
            i.previous_net_minutes is not None and (i.net_minutes or 0) > i.previous_net_minutes))
        late = lim.late_additions_require_consent
        if late is not None and late.value and inside_notice and adds_hours:
            if ctx.consent(eid, SCHEDULE_CHANGE_ACCEPTED, i.shift_date) is None:
                out.append(RuleResult(LATE_ADDITION_NO_CONSENT, _severity(LATE_ADDITION_NO_CONSENT, late.severity, lim),
                                      eid, (i.shift_date,), hours, None, "h",
                                      late.source_ref or "scheduling.late_additions_require_consent",
                                      _message(LATE_ADDITION_NO_CONSENT, late.verified)))
        cancel = lim.cancellation_notice_hours
        removes_hours = (i.withdrawn and i.kind == WORK) or (i.previous_kind == WORK and not i.is_work) or (
            i.is_work and i.previous_kind == WORK and i.previous_net_minutes is not None
            and (i.net_minutes or 0) < i.previous_net_minutes)
        if cancel is not None and removes_hours and hours < _dec(cancel.value):
            out.append(RuleResult(CANCELLATION_INSIDE_NOTICE, _severity(CANCELLATION_INSIDE_NOTICE, cancel.severity,
                                                                        lim), eid, (i.shift_date,), hours,
                                  _dec(cancel.value), "h", cancel.source_ref or "scheduling.cancellation_notice_hours",
                                  _message(CANCELLATION_INSIDE_NOTICE, cancel.verified)))


def _add_months(day: dt.date, months: int) -> dt.date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    last = (dt.date(year + (month == 12), month % 12 + 1, 1) - _ONE_DAY).day
    return dt.date(year, month, min(day.day, last))


def _ot_caps(emp: EmployeeSchedule, lim: EmployeeLimits, ctx: RuleContext, out: list[RuleResult]) -> None:
    eid = emp.employee_id
    usage = {d: m for d, m in ctx.ot_minutes.get(eid, {}).items() if m}
    if not usage or not lim.ot_caps:
        return
    days = sorted(usage)
    for cap in lim.ot_caps:
        cap_minutes = _dec(cap.hours) * 60
        windows: list[tuple[dt.date, dt.date, str]] = []
        if cap.window == "DAY":
            windows = [(d, d, "DAY") for d in days]
        elif cap.window == "WEEK":
            windows = [(lo, hi, "FIXED_WEEK") for lo, hi in _windows(days, FIXED, lim.week_start_day)]
        elif cap.window in ("MONTH", "QUARTER", "YEAR"):
            months = {"MONTH": cap.length, "QUARTER": 3 * cap.length, "YEAR": 12 * cap.length}[cap.window]
            if cap.window == "MONTH" and cap.length == 1 or cap.window in ("QUARTER", "YEAR"):
                step = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}[cap.window]
                starts = sorted({dt.date(d.year, ((d.month - 1) // step) * step + 1, 1) for d in days})
                windows += [(s, _add_months(s, months) - _ONE_DAY, "CALENDAR") for s in starts]
            if cap.window == "QUARTER" or (cap.window == "MONTH" and cap.length > 1):
                # rolling windows ([d, d + n months)); for QUARTER always shown next to the calendar quarter (EC AS-OT-08)
                windows += [(d, _add_months(d, months) - _ONE_DAY, "ROLLING") for d in days]
        else:
            continue
        exceeded = [(lo, hi, kind, total) for lo, hi, kind in windows
                    if Decimal(total := sum(m for d, m in usage.items() if _in(d, lo, hi))) > cap_minutes]
        fixed = [w for w in exceeded if w[2] != "ROLLING"]
        # one result per exceeded calendar window; the rolling usage only when no calendar window exceeds
        # (EC AS-OT-08: "WARN is also raised when only the rolling usage exceeds the cap")
        report = fixed or ([max((w for w in exceeded), key=lambda w: w[3])] if exceeded else [])
        for lo, hi, kind, total in report:
            out.append(RuleResult(OT_CAP_PROJECTED, _severity(OT_CAP_PROJECTED, cap.severity, lim, cap=WARN), eid,
                                  tuple(d for d in days if _in(d, lo, hi)), _hours(total), _dec(cap.hours), "h",
                                  cap.source_ref or "overtime.caps[]", _message(OT_CAP_PROJECTED),
                                  {"window": cap.window, "window_kind": kind, "from": lo.isoformat(),
                                   "to": hi.isoformat()}))


def _lints(emp: EmployeeSchedule, lim: EmployeeLimits, out: list[RuleResult]) -> None:
    if not lim.lints_enabled:
        return
    tz = emp.work_timezone
    timed = sorted((i for i in emp.instances if i.is_work and i.timed), key=lambda i: i.start_at)
    type_start: dict[Any, int] = {}
    items = []
    for i in timed:
        wall = _wall(i.start_at, tz)
        local_start = wall.hour * 60 + wall.minute
        type_ref = i.shift_id if i.shift_id is not None else ("CUSTOM", local_start)
        type_start.setdefault(type_ref, local_start)
        items.append(rot.LintInstance(
            i.shift_date, i.start_at, i.end_at, type_ref, type_start[type_ref], local_start, i.net_minutes or 0,
            rot.is_night(i.start_at, i.end_at, tz, lim.night_window, lim.night_threshold_hours),
            i.safety_critical, i.long_duty))
    out.extend(rot.lint_instances(items, employee_id=emp.employee_id, zone=tz, night_window=lim.night_window,
                                  muted=lim.lints_muted, night_worker=lim.night_worker))


def _coverage(schedule: ScheduleData, limits: LimitsData, ctx: RuleContext, out: list[RuleResult]) -> None:
    holding: dict[tuple[dt.date, Any], set[int]] = {}
    all_dates: set[dt.date] = set()
    for emp in schedule.employees:
        for i in emp.instances:
            if not i.active:
                continue
            all_dates.add(i.shift_date)
            employed = (emp.employment_start is None or i.shift_date >= emp.employment_start) and \
                (emp.employment_end is None or i.shift_date <= emp.employment_end)
            if i.is_work and employed:  # entries after the last working day are not counted (§3.4)
                holding.setdefault((i.shift_date, i.shift_id), set()).add(emp.employee_id)
    if not all_dates:
        return
    lo_all, hi_all = min(all_dates), max(all_dates)
    lim = limits.default
    for rule in ctx.coverage_rules:
        lo, hi = rule.from_date or lo_all, rule.to_date or hi_all
        for day in rot._days(lo, hi):
            if rule.weekdays is not None and day.weekday() not in rule.weekdays:
                continue
            people = holding.get((day, rule.shift_id), set())
            if rule.employee_ids is not None:
                people = people & set(rule.employee_ids)
            if rule.required_tags:
                people = {e for e in people if all(any(t.tag == tag and t.valid_on(day)
                                                       for t in ctx.employee_tags.get(e, ()))
                                                   for tag in rule.required_tags)}
            if len(people) < rule.min_count:
                out.append(RuleResult(COVERAGE_BELOW_MINIMUM, _severity(COVERAGE_BELOW_MINIMUM, WARN, lim,
                                                                        cap=REQUIRE_REASON),
                                      None, (day,), len(people), rule.min_count, "employees",
                                      rule.rule_ref or "SCHEDULING_POLICY.coverage_rules",
                                      _message(COVERAGE_BELOW_MINIMUM), {"shift_id": rule.shift_id}))
    for rc in ctx.rotation_coverage:
        lo, hi = rc.from_date or lo_all, rc.to_date or hi_all
        for day in rot._days(lo, hi):
            for shift_id, needed in rc.required.items():
                people = holding.get((day, shift_id), set())
                crews = sum(1 for members in rc.crews.values() if people & set(members))
                if crews < needed:
                    out.append(RuleResult(COVERAGE_BELOW_MINIMUM, _severity(COVERAGE_BELOW_MINIMUM, WARN, lim,
                                                                            cap=REQUIRE_REASON),
                                          None, (day,), crews, needed, "crews", f"rotation {rc.pattern_ref} coverage",
                                          _message(COVERAGE_BELOW_MINIMUM, variant="rotation"),
                                          {"shift_id": shift_id, "pattern": rc.pattern_ref}))


# --------------------------------------------------------------------------- validate / summarize


def validate(schedule: ScheduleData, limits: LimitsData | EmployeeLimits, context: RuleContext) -> list[RuleResult]:
    """All publish results for the candidate schedule (§6.1, §6.2). Order: per employee, then coverage, leave
    impacts and the pattern-level lints passed in ``context.pattern_lints``."""
    if isinstance(limits, EmployeeLimits):
        limits = LimitsData(default=limits)
    results: list[RuleResult] = []
    for emp in schedule.employees:
        lim = limits.for_employee(emp.employee_id)
        found: list[RuleResult] = []
        _integrity(emp, lim, found)
        _backdate_and_locks(emp, lim, context, found)
        _notice_rules(emp, lim, context, found)
        working: list[RuleResult] = []
        _day_rules(emp, lim, context, working)
        _rest_rules(emp, lim, context, working)
        _week_rules(emp, lim, working)
        _night_work(emp, lim, context, working)
        _outdoor_ban(emp, lim, working)
        _adolescent_limits(emp, lim, working)
        _ot_caps(emp, lim, context, working)
        _lints(emp, lim, working)
        changed_days = [i.shift_date for i in emp.instances if i.changed]
        if context.check_span_days is not None and changed_days:
            lo = min(changed_days) - dt.timedelta(days=context.check_span_days)
            hi = max(changed_days) + dt.timedelta(days=context.check_span_days)
            working = [r for r in working if not r.dates or any(_in(d, lo, hi) for d in r.dates)]
        results.extend(found)
        results.extend(working)
    _coverage(schedule, limits, context, results)
    for impact in context.affected_leave:
        lim = limits.for_employee(impact.employee_id)
        results.append(RuleResult(ROSTER_AFFECTS_APPROVED_LEAVE, _severity(ROSTER_AFFECTS_APPROVED_LEAVE, WARN, lim),
                                  impact.employee_id, tuple(impact.dates), _dec(impact.new_units),
                                  _dec(impact.old_units), "days", "LH README §9.2",
                                  _message(ROSTER_AFFECTS_APPROVED_LEAVE), {"leave_ref": impact.leave_ref}))
    results.extend(context.pattern_lints)
    return results


def summarize(results: Iterable[RuleResult], acknowledged_warnings: Collection[str] = (),
              reasons: Mapping[str, str] | None = None,
              exemptions: Mapping[str, Any] | None = None) -> tuple[list[RuleResult], list[RuleResult], list[RuleResult]]:
    """Resolution of §6.1 / §5.1 step 4 -> ``(blocking, unresolved_warnings, missing_reasons)``.

    WARN needs its key in ``acknowledged_warnings`` (one per key); REQUIRE_REASON needs a non-blank
    ``reasons[rule_key]``; BLOCK needs ``exemptions[rule_key]`` (``{exemption_ref, reason}``; a mapping must
    carry a non-blank ``exemption_ref``). Publishing is allowed when all three lists are empty.
    """
    reasons, exemptions = reasons or {}, exemptions or {}
    acknowledged = set(acknowledged_warnings)
    blocking, warnings, missing = [], [], []
    for r in results:
        if r.severity == BLOCK:
            exemption = exemptions.get(r.rule_key)
            ok = bool(exemption) and (not isinstance(exemption, Mapping) or
                                      bool(str(exemption.get("exemption_ref") or "").strip()))
            if not ok:
                blocking.append(r)
        elif r.severity == REQUIRE_REASON:
            if not str(reasons.get(r.rule_key) or "").strip():
                missing.append(r)
        elif r.rule_key not in acknowledged:
            warnings.append(r)
    return blocking, warnings, missing


# --------------------------------------------------------------------------- adapters and substitution (§4.4)


def from_shift_instance(instance: se.ShiftInstance, *, changed: bool = False, entry_seq: int = 1,
                        archived: bool = False, safety_critical: bool = False, long_duty: bool = False,
                        withdrawn: bool = False, previous_kind: str | None = None,
                        previous_start_at: dt.datetime | None = None, previous_net_minutes: int | None = None,
                        net_minutes: int | None = None, entry_id: int | None = None) -> PlannedInstance:
    """A WORK :class:`PlannedInstance` from ``shift_engine.build_instance`` output (UTC start / end, scheduled
    net minutes, ON_CALL segments). ``net_minutes`` overrides the instance's value (FTE or the standard-hours
    fallback of ``shift_engine.scheduled_net_minutes``)."""
    return PlannedInstance(
        shift_date=instance.shift_date, kind=WORK, entry_seq=entry_seq, start_at=instance.start_at,
        end_at=instance.end_at,
        net_minutes=net_minutes if net_minutes is not None else instance.scheduled_net_minutes,
        shift_id=instance.shift_id, shift_version_id=instance.shift_version_id, archived=archived,
        has_on_call=any(s.kind == se.ON_CALL for s in instance.segments), changed=changed, withdrawn=withdrawn,
        previous_kind=previous_kind, previous_start_at=previous_start_at, previous_net_minutes=previous_net_minutes,
        safety_critical=safety_critical, long_duty=long_duty, entry_id=entry_id or instance.roster_entry_id)


def substitution_window(work_date: dt.date) -> tuple[dt.date, dt.date]:
    """IN ``WEEK_BEFORE_OR_AFTER``: Monday of the ISO week before the ``work_date`` week to Sunday of the ISO
    week after it (Wages (Central) Rules r.6; §4.4)."""
    monday = work_date - dt.timedelta(days=work_date.weekday())
    return monday - _WEEK, monday + dt.timedelta(days=13)


def substitution_issue(work_date: dt.date, off_date: dt.date, *, work_date_is_rest_day: bool,
                       off_date_is_working: bool, window: str | None = "WEEK_BEFORE_OR_AFTER",
                       longest_run_after: int | None = None, max_consecutive_days: int | None = None,
                       verified: bool = False) -> str | None:
    """``details.rule`` of 422 ``SUBSTITUTION_INVALID`` for a rest-day substitution pair, or ``None`` (§4.4).
    ``MAX_CONSECUTIVE`` applies only with a VERIFIED pack (below it, publish raises WEEKLY_REST_EXCEEDED WARN)."""
    if not work_date_is_rest_day:
        return SUB_WORK_DATE_NOT_REST_DAY
    if not off_date_is_working:
        return SUB_OFF_DATE_NOT_WORKING
    if window == "WEEK_BEFORE_OR_AFTER":
        lo, hi = substitution_window(work_date)
        if not lo <= off_date <= hi:
            return SUB_OUTSIDE_WINDOW
    if verified and longest_run_after is not None and max_consecutive_days is not None \
            and longest_run_after > max_consecutive_days:
        return SUB_MAX_CONSECUTIVE
    return None


# --------------------------------------------------------------------------- caller helpers
FEMALE_VALUES = frozenset({"F", "FEMALE", "WOMAN", "W"})


def protected_groups_for(gender: str | None, pack_groups: Collection[str]) -> frozenset[str]:
    """The protected groups an employee belongs to for the pack's night-work rules (EC AS-ROS-10).

    Appendix-a: unknown inputs make the field apply ("covered - verify"), never silently exempt, so an employee
    without a recorded gender is in ``WOMEN`` until a gender is recorded (or a consent is granted)."""
    groups: set[str] = set()
    if "WOMEN" in pack_groups:
        value = (gender or "").strip().upper()
        if not value or value in FEMALE_VALUES:
            groups.add("WOMEN")
    return frozenset(groups)


def entry_approves_holiday_work(published_at: dt.datetime | None, holiday_known_at: dt.datetime | None) -> bool:
    """JC-AS-08 as amended (§8; AS 05 JC-AS-08 box; EC AS-INT-18): a published WORK entry is the approval of holiday
    work only when it was published on or after the holiday became known (``holidays.confirmed_at``). For a holiday
    that arrived after the publication nobody decided holiday work: the entry stays ``holiday_work_unreviewed`` until
    the planner confirms or withdraws it. No publication instant = not approved; no known instant = known before."""
    if published_at is None:
        return False
    return holiday_known_at is None or published_at >= holiday_known_at
