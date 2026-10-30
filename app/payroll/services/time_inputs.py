"""Time inputs of the day ledger (3.4 §2-§8, §16; 3.1 §5.2-§5.4; audit 03 §6-§8; FOLLOW-UPS F-02..F-04).

* Day facts: per date the primary assignment (employment window, branch, work week, shift, FTE), the
  weekly off of the work pattern (assignment, then branch, then legal entity, then the country's
  weekend) and the holiday of the branch's calendar lineage on that date (EC-HOL-04, EC-ATT-21).
* Leave days: generated at approval for every date of a leave (a half day on the first or last date);
  weekly offs and holidays are charged only when the leave type counts non-working days (EC-LV-06,
  EC-LV-10). Balances move through append-only leave_balance_transactions with idempotency keys.
* Manual LOP inputs (approvals module ``lop``), unpaid suspensions, on-duty requests (module
  ``attendance``) and optional holiday choices within the yearly quota (EC-HOL-05).
* source_change_log: every writer appends the record's new version hash (3.4 §16).
* ledger_inputs(): every date of a range as ``ledger.DayInput`` for the snapshot builder.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import calendar as _calendar
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.attendance import Attendance
from app.models.employee import Employee, Shift
from app.models.holiday import Holiday
from app.models.leave import Leave, LeaveType
from app.models.org import EmployeeAssignment
from app.models.time_inputs import (
    EmployeeOptionalHoliday,
    EmployeeSuspension,
    LeaveBalanceTransaction,
    LeaveDay,
    LopInput,
    OnDutyRequest,
    SourceChangeLog,
)
from app.payroll import audit as payroll_audit
from app.payroll import ledger, policy_types, workweek
from app.payroll.services import access, approvals
from app.services import attendance_policy, attendance_service, holiday_service
from app.services.audit_log_service import AuditLogService

ONE, HALF, ZERO = Decimal(1), Decimal("0.5"), Decimal(0)
PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF")
MAX_RANGE_DAYS = 400
INPUTS_READ, INPUTS_EDIT = "payroll:inputs:read", "payroll:inputs:edit"
LOP_MODULE, LOP_SUBJECT = "lop", "lop_input"
ON_DUTY_MODULE, ON_DUTY_SUBJECT = "attendance", "on_duty"
HOLIDAY_RANK = {"PUBLIC": 0, "COMPANY": 1, "OPTIONAL": 2}
STATUS_MAP = {"present": "PRESENT", "late": "LATE", "half-day": "HALF_DAY", "absent": "ABSENT"}
AUDIT_MODULE = payroll_audit.PAYROLL_INPUTS


# ---------------------------------------------------------------------------
# Version hashes and the source change log
# ---------------------------------------------------------------------------
def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"cannot hash {type(value).__name__}")


def content_hash(value: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace, decimals and dates as strings)."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log_change(
    db: Session, company_id: int, *, record_type: str, record_id: int, change: str, content: Any,
    employee_id: Optional[int] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
    actor_user_id: Optional[int] = None, details: Optional[Mapping[str, Any]] = None,
) -> SourceChangeLog:
    """Append the record's new version to source_change_log (3.4 §16)."""
    row = SourceChangeLog(
        company_id=company_id, record_type=record_type, record_id=record_id, employee_id=employee_id,
        date_from=date_from, date_to=date_to if date_to is not None else date_from, change=change,
        version_hash=content_hash(content), changed_at=utcnow(), actor_user_id=actor_user_id,
        details=json.loads(json.dumps(dict(details), default=_json_default)) if details else None,
    )
    db.add(row)
    db.flush()
    # AS-M3: attendance v2 tenants re-evaluate the days this change touches (job attendance.recompute_scope).
    from app.services import attendance_days

    attendance_days.schedule_recompute(db, company_id, row)
    # G-16 (X 03 §3, JC-AS-11): changes reaching an input-locked or finalized period go to stale detection / retro.
    from app.services import payroll_sync

    payroll_sync.on_change(db, company_id, row)
    return row


def changes_since(db: Session, company_id: int, since: datetime, *, employee_ids: Optional[Iterable[int]] = None,
                  start: Optional[date] = None, end: Optional[date] = None) -> List[SourceChangeLog]:
    """Change-log rows after ``since`` that can touch ``start..end`` (employee-less rows reach everyone)."""
    query = db.query(SourceChangeLog).filter(SourceChangeLog.company_id == company_id, SourceChangeLog.changed_at > since)
    if employee_ids is not None:
        query = query.filter(or_(SourceChangeLog.employee_id.is_(None), SourceChangeLog.employee_id.in_(list(employee_ids))))
    if end is not None:
        query = query.filter(or_(SourceChangeLog.date_from.is_(None), SourceChangeLog.date_from <= end))
    if start is not None:
        query = query.filter(or_(SourceChangeLog.date_to.is_(None), SourceChangeLog.date_to >= start))
    return query.order_by(SourceChangeLog.changed_at, SourceChangeLog.id).all()


# ---------------------------------------------------------------------------
# Day facts: employment window, branch, work week, holiday per date
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HolidayFact:
    id: int
    name: str
    type: str  # PUBLIC | COMPANY | OPTIONAL_AVAILED | OPTIONAL_NOT_AVAILED
    content_hash: str
    # LH-M1 (LH 05 §3.2): reporting family, the holiday part of the day (union of the date's rows), status and why
    # the holiday is on this date (ORIGINAL / OBSERVED / EMPLOYEE_SUBSTITUTE, never stored for a substitute).
    category: str = "OTHER"
    portion: str = "FULL"
    status: str = "CONFIRMED"
    observance: str = "ORIGINAL"
    observed_from_date: Optional[date] = None

    @property
    def non_working(self) -> bool:
        """Day facts decide only working / non-working (D-LH-11, BUG-11); payroll decides pay with
        ``HOLIDAY_PAY_POLICY.paid_holiday_types``."""
        return self.type != "OPTIONAL_NOT_AVAILED"

    @property
    def paid(self) -> bool:
        """Deprecated alias of ``non_working`` (SPINE V3-062): it never meant "paid"."""
        return self.non_working


@dataclass(frozen=True)
class DayFacts:
    date: date
    employed: bool
    assignment: Optional[EmployeeAssignment]
    branch_id: Optional[int]
    work_week_id: Optional[int]
    weekly_off: bool
    holiday: Optional[HolidayFact]
    # LH-M1 (04-holiday/03 §5.4, SPINE §7): the version of the pattern valid on the date, where the pattern came
    # from, the weekly-off portion (half-day offs are working days for ``weekly_off``) and whether it is a rest day.
    pattern_version: Optional[int] = None
    work_source: str = "PATTERN"
    weekly_off_portion: Optional[str] = None
    rest_day: bool = False
    # LH-M1 (04-holiday/01 §4.1): the holiday calendar resolved for the date and how (OVERRIDE / CRITERIA /
    # BRANCH_LINK; NONE = the legacy location rule of a branch without a calendar).
    calendar_id: Optional[int] = None
    calendar_source: str = "NONE"
    # LH-M1 (LH 05 §3.2, SPINE §7 step 4): a declared working day on the resolved calendar cancelled a WEEKLY off.
    declared_working_day: bool = False
    declared_working_portion: Optional[str] = None
    legal_entity_id: Optional[int] = None
    timezone: Optional[str] = None
    # The part of the date the employee is scheduled to work (truth table LH 05 §3.4) and its net minutes x FTE.
    working_portion: str = "NONE"
    scheduled_minutes: int = 0
    calendar_version: Optional[str] = None
    # Attendance-shift fields (JC-AS-30): which step decided the shift and the built instance(s).
    shift_source: str = "NONE"
    shift_ids: Tuple[int, ...] = ()
    shift_version_ids: Tuple[int, ...] = ()
    roster_entry_ids: Tuple[int, ...] = ()
    instances: Tuple[Tuple[datetime, datetime], ...] = ()
    # An ORIGINAL holiday shown elsewhere because its OBSERVED partner counts (display only, LH 05 X-5).
    observed_elsewhere: Optional[str] = None

    @property
    def working(self) -> bool:
        """A scheduled working day: neither a full weekly off nor a non-working holiday (3.4 §3)."""
        return not self.weekly_off and not (self.holiday is not None and self.holiday.non_working)


def _dates(start: date, end: date) -> List[date]:
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise bad_request(f"A range may cover at most {MAX_RANGE_DAYS} days.", "RANGE_TOO_LONG")
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def _employee(db: Session, company_id: int, employee_id: int) -> Employee:
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    return employee


def primary_assignments(db: Session, company_id: int, employee_id: int) -> List[EmployeeAssignment]:
    return (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                EmployeeAssignment.is_primary.is_(True))
        .order_by(EmployeeAssignment.effective_from, EmployeeAssignment.id)
        .all()
    )


def _covering(assignments: Sequence[EmployeeAssignment], day: date) -> Optional[EmployeeAssignment]:
    for row in assignments:
        if row.effective_from <= day and (row.effective_to is None or day <= row.effective_to):
            return row
    return None


def _nearest(assignments: Sequence[EmployeeAssignment], day: date) -> Optional[EmployeeAssignment]:
    """The placement that decides weekly offs and holidays of a date outside employment (F-04)."""
    before = [a for a in assignments if a.effective_from <= day]
    return before[-1] if before else (assignments[0] if assignments else None)


def employed_on(employee: Employee, assignment: Optional[EmployeeAssignment], day: date) -> bool:
    if assignment is None:
        return False
    if employee.hire_date is not None and day < employee.hire_date:
        return False
    return employee.termination_date is None or day <= employee.termination_date


def _resolver(db: Session, company_id: int):
    """The effective-dated work-week resolver (LH-M1: roster > placement > criteria > branch link > entity >
    country weekend > MON_FRI, rules of the version valid on each date; app.services.holiday_assignments)."""
    from app.services.holiday_assignments import WorkWeekResolver

    return WorkWeekResolver(db, company_id)


def _holiday_fact(holiday: Holiday, chosen: set, *, observance: Optional[str] = None,
                  observed_from: Optional[date] = None, portion: Optional[str] = None) -> HolidayFact:
    kind = holiday.holiday_type or "COMPANY"
    if kind == "OPTIONAL":
        kind = "OPTIONAL_AVAILED" if holiday.id in chosen else "OPTIONAL_NOT_AVAILED"
    content = holiday_service.content(holiday)
    if observance == "EMPLOYEE_SUBSTITUTE":
        content = {**content, "substitute_for": holiday.date.isoformat()}
    return HolidayFact(holiday.id, holiday.name, kind, content_hash(content),
                       category=getattr(holiday, "category", None) or "OTHER",
                       portion=portion or getattr(holiday, "portion", None) or "FULL",
                       status=getattr(holiday, "status", None) or "CONFIRMED",
                       observance=observance or getattr(holiday, "observance", None) or "ORIGINAL",
                       observed_from_date=observed_from if observance == "EMPLOYEE_SUBSTITUTE"
                       else getattr(holiday, "observed_from_date", None))


_PORTION_SIZE = {"FULL": 0, "FIRST_HALF": 1, "SECOND_HALF": 1}


def _union(portions: Iterable[str]) -> str:
    """FIRST_HALF ∪ SECOND_HALF = FULL (SPINE V3-063 step 5)."""
    seen = set(portions)
    if "FULL" in seen or {"FIRST_HALF", "SECOND_HALF"} <= seen:
        return "FULL"
    return next(iter(seen)) if seen else "FULL"


def pick_holiday(items: Sequence[Holiday], chosen: set) -> Optional[HolidayFact]:
    """One holiday per date (SPINE V3-063 step 5): non-working rows first, then HOLIDAY_RANK (PUBLIC > COMPANY >
    OPTIONAL), larger portion, lower id; the fact's portion is the union of the non-working rows' portions."""
    if not items:
        return None
    facts = [(h, _holiday_fact(h, chosen)) for h in items]
    ordered = sorted(facts, key=lambda pair: (not pair[1].non_working, HOLIDAY_RANK.get(pair[0].holiday_type, 3),
                                              _PORTION_SIZE.get(pair[1].portion, 0), pair[0].id))
    winner, fact = ordered[0]
    counting = [f.portion for _, f in facts if f.non_working]
    if fact.non_working and len(counting) > 1:
        fact = _holiday_fact(winner, chosen, portion=_union(counting))
    return fact


# LH 05 §3.4 truth table: (weekly-off portion, holiday portion) -> working portion.
_WORKING = {
    (None, None): "FULL", (None, "FULL"): "NONE", (None, "FIRST_HALF"): "SECOND_HALF",
    (None, "SECOND_HALF"): "FIRST_HALF",
    ("FULL", None): "NONE", ("FULL", "FULL"): "NONE", ("FULL", "FIRST_HALF"): "NONE", ("FULL", "SECOND_HALF"): "NONE",
    ("FIRST_HALF", None): "SECOND_HALF", ("FIRST_HALF", "FULL"): "NONE", ("FIRST_HALF", "FIRST_HALF"): "SECOND_HALF",
    ("FIRST_HALF", "SECOND_HALF"): "NONE",
    ("SECOND_HALF", None): "FIRST_HALF", ("SECOND_HALF", "FULL"): "NONE", ("SECOND_HALF", "FIRST_HALF"): "NONE",
    ("SECOND_HALF", "SECOND_HALF"): "FIRST_HALF",
}


def working_portion(employed: bool, weekly_off_portion: Optional[str], holiday: Optional[HolidayFact]) -> str:
    """Scheduled day − weekly-off portion − holiday portion (OPTIONAL only when availed); not employed -> NONE."""
    if not employed:
        return "NONE"
    holiday_portion = holiday.portion if holiday is not None and holiday.non_working else None
    return _WORKING[(weekly_off_portion, holiday_portion)]


def _chosen_optional(db: Session, company_id: int, employee_id: int, holiday_ids: Iterable[int]) -> set:
    ids = list(holiday_ids)
    if not ids:
        return set()
    return {h for (h,) in db.query(EmployeeOptionalHoliday.holiday_id).filter(
        EmployeeOptionalHoliday.company_id == company_id, EmployeeOptionalHoliday.employee_id == employee_id,
        EmployeeOptionalHoliday.holiday_id.in_(ids), EmployeeOptionalHoliday.status == "CHOSEN")}


SUBSTITUTE_HORIZON_DAYS = 14  # LH 04-holiday/02 §3.6: a substitute is searched at most 14 days after the holiday
SUBSTITUTE_LOOKBACK_DAYS = 28  # two horizons, so the chain does not depend on the queried range
MAX_EMPLOYEE_DAYS = 200_000  # day_facts_many cap (SPINE V3-062)


@dataclass
class _Day:
    date: date
    placement: Optional[EmployeeAssignment]
    employed: bool
    calendar_id: Optional[int]
    calendar_source: str
    week: Any = None
    holidays: List[Holiday] = None  # type: ignore[assignment]
    fact: Optional[HolidayFact] = None
    observed_elsewhere: Optional[str] = None


def _standard_minutes(db: Session, company_id: int, employee_id: int, on: date) -> int:
    """``payroll.standard_hours_per_day`` x 60 for the no-shift fallback (SPINE V3-062), default 8 h."""
    try:
        from app.payroll.services import settings as settings_service

        value = settings_service.resolve(db, company_id, "payroll.standard_hours_per_day", employee_id=employee_id,
                                         on=on).value
        return int((Decimal(str(value)) * 60).to_integral_value())
    except Exception:  # noqa: BLE001 - an unreadable setting falls back to the documented default
        return 480


def _dst_difference(start_at: datetime, end_at: datetime, zone: str) -> Decimal:
    """Elapsed minutes minus wall-clock minutes of a shift (LH-TZ-10): +60 on a fall-back night, −60 on a
    spring-forward night, 0 otherwise; the wall-clock formula stays for every other date (zero diff)."""
    import pytz  # zone data on Windows comes from pytz (shift_engine)

    try:
        tz = pytz.timezone(zone)
    except pytz.UnknownTimeZoneError:  # an unknown zone keeps the wall-clock minutes
        return Decimal(0)
    elapsed = (end_at - start_at).total_seconds()
    wall = (end_at.astimezone(tz).replace(tzinfo=None) - start_at.astimezone(tz).replace(tzinfo=None)).total_seconds()
    return Decimal(int(elapsed - wall) // 60)


def _shift_minutes(resolved, day: date, portion: str, fte: Decimal, standard) -> Tuple[int, tuple]:
    """Net scheduled minutes inside ``portion`` x FTE (LH 05 §3.4) and the built instance; the legacy-shaped
    full day keeps payroll's own helper (``shift_resolution.scheduled_hours``) so both agree."""
    from decimal import ROUND_HALF_UP

    from app.services import shift_engine, shift_resolution

    if portion == "NONE":
        return 0, ()
    instance = None
    if resolved is not None:
        try:
            instance = shift_engine.build_instance(resolved.version, day, resolved.work_timezone)
        except Exception:  # noqa: BLE001 - an unbuildable version uses the standard-hours fallback
            instance = None
    if portion == "FULL":
        hours = shift_resolution.scheduled_hours(resolved)
        net = (hours * 60) if hours is not None else Decimal(_value(standard))
        if hours is not None and instance is not None and instance.start_at and instance.end_at:
            net += _dst_difference(instance.start_at, instance.end_at, resolved.work_timezone)
    else:
        half = None
        if instance is not None:
            half = (instance.first_half if portion == "FIRST_HALF" else instance.second_half).minutes
        net = Decimal(half) if half is not None else Decimal(_value(standard)) / 2
    minutes = int((net * fte).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    spans = ((instance.start_at, instance.end_at),) if instance is not None and instance.start_at else ()
    return minutes, spans


def _observance_of(db: Session, company_id: int, calendar_ids: Iterable[int]) -> Dict[int, str]:
    from app.models.pay_org import HolidayCalendar

    ids = sorted({c for c in calendar_ids if c})
    if not ids:
        return {}
    return {c: (o or "NONE") for c, o in db.query(HolidayCalendar.id, HolidayCalendar.employee_observance).filter(
        HolidayCalendar.company_id == company_id, HolidayCalendar.id.in_(ids))}


def _calendar_version(db: Session, company_id: int, calendar_ids: Iterable[int], patterns: Iterable[Tuple[Any, Any]],
                      branch_ids: Iterable[int], start: date, end: date, *, roster: Iterable[Tuple[int, int]] = (),
                      shift_versions: Iterable[int] = (), shift_rules: Iterable[Tuple[int, int]] = ()) -> str:
    """content_hash of the calendar years, pattern versions and assignment rows consulted (LH 05 §3.4) and, JC-AS-30,
    the roster entries (``[roster_entry_id, version]``, OFF included), shift versions and shift-assignment rules
    (``[rule_id, row_version]``) that decided the shift. Empty keys are omitted, so a tenant without rosters or rules
    keeps its hash (zero diff); the same inputs always give the same hash (AS-INT-08, X-INT-56, X-INT-58)."""
    return content_hash({**_calendar_parts(db, company_id, calendar_ids, patterns, branch_ids, start, end),
                         **_shift_parts(roster, shift_versions, shift_rules)})


def _shift_parts(roster: Iterable[Tuple[int, int]] = (), shift_versions: Iterable[int] = (),
                 shift_rules: Iterable[Tuple[int, int]] = ()) -> Dict[str, list]:
    """The JC-AS-30 keys of ``calendar_version``, sorted; a key without values is left out."""
    out: Dict[str, list] = {}
    if roster:
        out["roster"] = sorted([int(i), int(v)] for i, v in set(roster))
    if shift_versions:
        out["shift_versions"] = sorted([int(v)] for v in set(shift_versions))
    if shift_rules:
        out["shift_rules"] = sorted([int(i), int(v)] for i, v in set(shift_rules))
    return out


def _calendar_parts(db: Session, company_id: int, calendar_ids: Iterable[int], patterns: Iterable[Tuple[Any, Any]],
                    branch_ids: Iterable[int], start: date, end: date) -> Dict[str, list]:
    """The pre-JC-AS-30 payload of ``calendar_version``: calendar years, pattern versions, assignment rows."""
    from app.models.holiday import HolidayCalendarYear
    from app.models.pay_org import BranchCalendarLink, HolidayCalendarAssignment
    from app.services import holiday_calendar_service

    graph = holiday_calendar_service._graph(db, company_id)  # noqa: SLF001
    cals = sorted({c for cal in set(calendar_ids) if cal for c in holiday_calendar_service.reachable(graph, cal)})
    years: List[list] = []
    if cals:
        stored = {(c, y): v for c, y, v in db.query(HolidayCalendarYear.calendar_id, HolidayCalendarYear.year,
                                                     HolidayCalendarYear.version).filter(
            HolidayCalendarYear.company_id == company_id, HolidayCalendarYear.calendar_id.in_(cals),
            HolidayCalendarYear.year >= start.year, HolidayCalendarYear.year <= end.year)}
        for c in cals:
            for y in range(start.year, end.year + 1):
                if (c, y) in stored:
                    years.append([c, y, stored[(c, y)]])
                else:
                    latest = db.query(func.max(Holiday.updated_at)).filter(
                        Holiday.company_id == company_id, Holiday.calendar_id == c,
                        Holiday.date >= date(y, 1, 1), Holiday.date <= date(y, 12, 31)).scalar()
                    if latest is not None:
                        years.append([c, 0, latest.isoformat()])
    assignments: List[list] = []
    branches = sorted({b for b in branch_ids if b})
    if branches:
        assignments += [["link", i, rv] for i, rv in db.query(BranchCalendarLink.id, BranchCalendarLink.row_version)
                        .filter(BranchCalendarLink.company_id == company_id, BranchCalendarLink.branch_id.in_(branches),
                                BranchCalendarLink.effective_from <= end,
                                or_(BranchCalendarLink.effective_to.is_(None), BranchCalendarLink.effective_to >= start))]
    assignments += [["criteria", i, rv] for i, rv in db.query(HolidayCalendarAssignment.id,
                                                              HolidayCalendarAssignment.row_version).filter(
        HolidayCalendarAssignment.company_id == company_id, HolidayCalendarAssignment.effective_from <= end,
        or_(HolidayCalendarAssignment.effective_to.is_(None), HolidayCalendarAssignment.effective_to >= start))]
    return {"years": sorted(years), "patterns": sorted([list(p) for p in set(patterns) if p[0]]),
            "assignments": sorted(assignments)}


def _value(standard) -> int:
    return standard() if callable(standard) else standard


class _Batch:
    """Per-call caches of ``day_facts_many``: employees, placements and shifts are loaded once for every employee,
    and calendar-level data (holidays, declared working days, observance, calendar version, zones, the
    standard-hours setting) once per calendar or key — the statements of a dashboard do not grow with its people
    (attendance view budget, tests/test_attendance_performance.py)."""

    def __init__(self, db: Session, company_id: int, ids: Sequence[int], start: date, end: date, *,
                 light: bool = False) -> None:
        from app.services import shift_resolution

        self.db, self.company_id, self.light = db, company_id, light
        self.employees = {e.id: e for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                                    Employee.id.in_(list(ids) or [0]))}
        self.assignments: Dict[int, List[EmployeeAssignment]] = defaultdict(list)
        for row in (db.query(EmployeeAssignment)
                    .filter(EmployeeAssignment.company_id == company_id,
                            EmployeeAssignment.employee_id.in_(list(ids) or [0]),
                            EmployeeAssignment.is_primary.is_(True))
                    .order_by(EmployeeAssignment.employee_id, EmployeeAssignment.effective_from,
                              EmployeeAssignment.id)):
            self.assignments[row.employee_id].append(row)
        from app.services import holiday_rosters

        roster = holiday_rosters.provider()
        if hasattr(roster, "prefetch"):  # one roster read per batch; the null provider has nothing to load
            lookback = start - timedelta(days=SUBSTITUTE_LOOKBACK_DAYS)
            roster.prefetch(db, company_id, list(ids), lookback, end)
        try:
            self.shifts = {} if light else shift_resolution.shifts_for_many(db, company_id, list(ids), start, end)
        except Exception:  # noqa: BLE001 - shift data that cannot be resolved leaves the standard-hours fallback
            self.shifts = {}
        # JC-AS-30 inputs of calendar_version: the published entries of every (E, D) (OFF included) and the row
        # versions of the shift rules that resolved a shift; nothing is read for a tenant without either.
        self.roster_marks: Dict[Tuple[int, date], Tuple[Tuple[int, int], ...]] = {}
        if not light and hasattr(roster, "entries_batch"):
            for employee_id, by_day in roster.entries_batch(db, company_id, list(ids), start, end).items():
                for day, entries in by_day.items():
                    marks = tuple((e.roster_entry_id, e.roster_version) for e in entries
                                  if getattr(e, "roster_entry_id", None) is not None)
                    if marks:
                        self.roster_marks[(employee_id, day)] = marks
        self.rule_versions: Dict[int, int] = {}
        rule_ids = sorted({r.rule_id for r in self.shifts.values() if r is not None and r.rule_id})
        if rule_ids:
            from app.models.shifts import ShiftAssignmentRule

            self.rule_versions = dict(db.query(ShiftAssignmentRule.id, ShiftAssignmentRule.row_version).filter(
                ShiftAssignmentRule.company_id == company_id, ShiftAssignmentRule.id.in_(rule_ids)))
        self._holidays: Dict[Tuple[str, Any, Tuple[date, ...]], Dict[date, List[Holiday]]] = {}
        self._declared: Dict[Tuple[int, Tuple[date, ...]], Dict[date, str]] = {}
        self._observance: Dict[int, str] = {}
        self._versions: Dict[Any, str] = {}
        self._parts: Dict[str, Dict[str, list]] = {}
        self._zones: Dict[Any, str] = {}
        self._standard: Dict[Any, int] = {}

    def employee(self, employee_id: int) -> Employee:
        employee = self.employees.get(employee_id)
        if employee is None:
            raise not_found("Employee")
        return employee

    def holidays(self, by_calendar: Mapping[int, List[date]],
                 by_branch: Mapping[Optional[int], List[date]]) -> Dict[date, List[Holiday]]:
        out: Dict[date, List[Holiday]] = defaultdict(list)
        for kind, groups in (("calendar", by_calendar), ("branch", by_branch)):
            for owner, days in groups.items():
                key = (kind, owner, tuple(sorted(days)))
                if key not in self._holidays:
                    reader = (holiday_service.holidays_by_calendar_date if kind == "calendar"
                              else holiday_service.holidays_by_branch_date)
                    self._holidays[key] = reader(self.db, self.company_id, {owner: list(days)})
                for day, items in self._holidays[key].items():
                    out[day].extend(items)
        return out

    def observance(self, calendar_ids: Iterable[int]) -> Dict[int, str]:
        wanted = {c for c in calendar_ids if c}
        missing = wanted - set(self._observance)
        if missing:
            self._observance.update(_observance_of(self.db, self.company_id, missing))
            for c in missing:
                self._observance.setdefault(c, "NONE")
        return {c: self._observance[c] for c in wanted}

    def declared(self, calendar_id: int, days: Sequence[date]) -> Dict[date, str]:
        from app.services import holiday_calendar_service

        key = (calendar_id, tuple(sorted(days)))
        if key not in self._declared:
            self._declared[key] = holiday_calendar_service.declared_working_days(self.db, self.company_id,
                                                                                calendar_id, list(days))
        return self._declared[key]

    def version(self, calendar_ids, patterns, branch_ids, start: date, end: date) -> str:
        if self.light:
            return ""
        key = (tuple(sorted({c for c in calendar_ids if c})), tuple(sorted({tuple(p) for p in patterns if p[0]}, key=repr)),
               tuple(sorted({b for b in branch_ids if b})), start, end)
        if key not in self._versions:
            parts = _calendar_parts(self.db, self.company_id, key[0], key[1], key[2], start, end)
            digest = content_hash(parts)
            self._versions[key], self._parts[digest] = digest, parts
        return self._versions[key]

    def day_version(self, base: str, employee_id: int, day: date) -> str:
        """``calendar_version`` of one date (JC-AS-30): the range version plus the roster entries of (E, D) and the
        shift version / rule that decided the shift when a roster entry or a shift rule decided it (a placement shift
        adds nothing, zero diff). Per date, so the hash of a date never depends on the range asked (attendance
        evaluates one date on a punch and three at day close)."""
        if not base or base not in self._parts:
            return base
        resolved = self.shifts.get((employee_id, day))
        decided = resolved is not None and resolved.source in ("ROSTER", "RULE")
        extras = _shift_parts(
            self.roster_marks.get((employee_id, day), ()),
            (resolved.shift_version_id,) if decided and resolved.shift_version_id else (),
            ((resolved.rule_id, self.rule_versions[resolved.rule_id]),)
            if decided and resolved.rule_id in self.rule_versions else ())
        return content_hash({**self._parts[base], **extras}) if extras else base

    def zone(self, placement: EmployeeAssignment) -> str:
        from app.services import org_structure_service

        key = (placement.branch_id, placement.legal_entity_id, getattr(placement, "work_timezone", None))
        if key not in self._zones:
            self._zones[key] = org_structure_service.placement_zone(self.db, placement)
        return self._zones[key]

    def standard(self, employee_id: int, placement: Optional[EmployeeAssignment], on: date) -> int:
        if self.light:
            return 480
        key = (getattr(placement, "legal_entity_id", None), getattr(placement, "pay_group_id", None))
        if key not in self._standard:
            self._standard[key] = _standard_minutes(self.db, self.company_id, employee_id, on)
        return self._standard[key]


def _resolve_days(db: Session, company_id: int, employee: Employee, assignments: Sequence[EmployeeAssignment],
                  patterns, days: Sequence[date], batch: Optional["_Batch"] = None) -> List[_Day]:
    """Steps 1-3 and the holiday rows of step 5 (before employee observance) for every date."""
    out: List[_Day] = []
    by_calendar: Dict[int, List[date]] = defaultdict(list)
    by_branch: Dict[Optional[int], List[date]] = defaultdict(list)
    for day in days:
        covering = _covering(assignments, day)
        placement = covering or _nearest(assignments, day)
        calendar_id, calendar_source = patterns.calendar_on(placement, day)
        out.append(_Day(day, placement, employed_on(employee, covering, day), calendar_id, calendar_source))
        if calendar_id:
            by_calendar[calendar_id].append(day)
        else:  # a branch without a calendar: the legacy location rule
            by_branch[placement.branch_id if placement else None].append(day)
    if batch is not None:
        holidays = batch.holidays(by_calendar, by_branch)
    else:
        holidays = holiday_service.holidays_by_calendar_date(db, company_id, by_calendar)
        for day, items in holiday_service.holidays_by_branch_date(db, company_id, by_branch).items():
            holidays.setdefault(day, []).extend(items)
    for d in out:
        d.week = patterns.resolve(d.placement, d.date, employee.id)
        d.holidays = list(holidays.get(d.date, ()))
    return out


def _apply_observance(db: Session, company_id: int, resolved: List[_Day], chosen: set,
                      batch: Optional["_Batch"] = None) -> None:
    """Step 5 per employee (LH 04-holiday/02 §3.6): pick the date's holiday, then OBSERVED_IF_OFF (an ORIGINAL date the
    employee works keeps the holiday there) and NEXT_WORKING_DAY (a holiday on a rest day moves to the employee's next
    scheduled working day that is neither a holiday nor an assigned substitute; ascending order, 14-day horizon)."""
    from app.services import holiday_calendar_service

    index = {d.date: d for d in resolved}
    observance = (batch.observance([d.calendar_id for d in resolved]) if batch is not None
                  else _observance_of(db, company_id, [d.calendar_id for d in resolved]))
    for d in resolved:
        d.fact = pick_holiday(d.holidays, chosen)
    # OBSERVED_IF_OFF: the employee scheduled on the ORIGINAL date gets the holiday there, not on the observed date.
    if_off = [d for d in resolved if d.fact is not None and d.fact.observance == "OBSERVED"
              and observance.get(d.calendar_id) == "OBSERVED_IF_OFF"]
    if if_off:
        partners = holiday_calendar_service.original_partners(
            db, company_id, [h for d in if_off for h in d.holidays if h.id == d.fact.id])
        for d in if_off:
            original = partners.get(d.fact.id)
            target = index.get(original.date) if original is not None else None
            if target is None or target.week.portion(target.date) == workweek.FULL:
                continue  # off on the original date (or outside the range): the OBSERVED row counts
            target.holidays.append(original)
            target.fact = pick_holiday(target.holidays, chosen)
            d.holidays = [h for h in d.holidays if h.id != d.fact.id]
            d.observed_elsewhere = original.date.isoformat()
            d.fact = pick_holiday(d.holidays, chosen)
    # An OBSERVED row that still counts: its ORIGINAL date is display-only ("observed on <date>", 02 §3.6 NONE).
    observed = [d for d in resolved if d.fact is not None and d.fact.observance == "OBSERVED"]
    if observed:
        partners = holiday_calendar_service.original_partners(
            db, company_id, [h for d in observed for h in d.holidays if h.id == d.fact.id])
        for d in observed:
            original = partners.get(d.fact.id)
            target = index.get(original.date) if original is not None else None
            if target is not None and target.fact is None and target.observed_elsewhere is None:
                target.observed_elsewhere = d.date.isoformat()
    # NEXT_WORKING_DAY: substitutes for single holidays on a rest day.
    taken = {d.date for d in resolved if d.fact is not None and d.fact.non_working}
    for d in resolved:
        if (d.fact is None or not d.fact.non_working or d.fact.observance != "ORIGINAL"
                or observance.get(d.calendar_id) != "NEXT_WORKING_DAY" or not d.week.rest_day(d.date)):
            continue
        for step in range(1, SUBSTITUTE_HORIZON_DAYS + 1):
            candidate = index.get(d.date + timedelta(days=step))
            if candidate is None:
                break  # beyond the evaluated range: no substitute shown inside it
            if (candidate.employed and candidate.week.portion(candidate.date) != workweek.FULL
                    and candidate.date not in taken):
                holiday = next(h for h in d.holidays if h.id == d.fact.id)
                candidate.fact = _holiday_fact(holiday, chosen, observance="EMPLOYEE_SUBSTITUTE", observed_from=d.date)
                taken.add(candidate.date)
                break


def _facts_for(db: Session, company_id: int, employee_id: int, start: date, end: date, patterns,
               batch: "_Batch") -> List[DayFacts]:
    days = _dates(start, end)
    employee = batch.employee(employee_id)
    assignments = batch.assignments.get(employee_id, [])
    resolved = _resolve_days(db, company_id, employee, assignments, patterns, days, batch)
    lookback: List[_Day] = []
    if "NEXT_WORKING_DAY" in batch.observance([d.calendar_id for d in resolved]).values():
        # Substitutes chain over two horizons before the range, so the answer never depends on the range asked.
        lookback = _resolve_days(db, company_id, employee, assignments, patterns,
                                 [start - timedelta(days=i) for i in range(SUBSTITUTE_LOOKBACK_DAYS, 0, -1)], batch)
    resolved = lookback + resolved
    chosen = _chosen_optional(db, company_id, employee_id, {h.id for d in resolved for h in d.holidays})
    _apply_observance(db, company_id, resolved, chosen, batch)
    resolved = resolved[len(lookback):]
    declared: Dict[date, str] = {}
    by_calendar: Dict[int, List[date]] = defaultdict(list)
    for d in resolved:
        if d.calendar_id:
            by_calendar[d.calendar_id].append(d.date)
    for calendar_id, cal_days in by_calendar.items():
        declared.update(batch.declared(calendar_id, cal_days))
    shifts = batch.shifts
    first = _covering(assignments, start) or _nearest(assignments, start)
    standard = lambda: batch.standard(employee_id, first, start)  # noqa: E731 - read only when a day needs it
    version = batch.version([d.calendar_id for d in resolved],
                            [(d.week.pattern_id, d.week.version) for d in resolved],
                            [d.placement.branch_id for d in resolved if d.placement is not None], start, end)
    result: List[DayFacts] = []

    for d in resolved:
        week, placement = d.week, d.placement
        portion = week.portion(d.date)
        rest = week.rest_day(d.date)
        made_up = declared.get(d.date)
        if made_up and week.roster_off is None:
            # A declared (make-up) working day turns a WEEKLY weekly off into work; a CYCLE keeps its offs.
            portion = workweek.apply_declared_working_day(week.pattern, portion, made_up)
            rest = rest and portion == workweek.FULL
        work = working_portion(d.employed, portion, d.fact)
        resolved_shift = shifts.get((employee_id, d.date))
        fte = Decimal(str(getattr(placement, "fte", None) or 1)) if placement is not None else Decimal(1)
        minutes, spans = _shift_minutes(resolved_shift, d.date, work, fte, standard) if d.employed else (0, ())
        result.append(DayFacts(
            d.date, d.employed, placement, placement.branch_id if placement else None, week.pattern_id,
            portion == workweek.FULL, d.fact, week.version, week.work_source, portion, rest, d.calendar_id,
            d.calendar_source, bool(made_up), made_up,
            legal_entity_id=placement.legal_entity_id if placement is not None else None,
            timezone=batch.zone(placement) if placement is not None else None,
            working_portion=work, scheduled_minutes=minutes,
            calendar_version=batch.day_version(version, employee_id, d.date),
            shift_source=resolved_shift.source if resolved_shift is not None else "NONE",
            shift_ids=(resolved_shift.shift_id,) if resolved_shift is not None and resolved_shift.shift_id else (),
            shift_version_ids=(resolved_shift.shift_version_id,)
            if resolved_shift is not None and resolved_shift.shift_version_id else (),
            roster_entry_ids=(resolved_shift.roster_entry_id,)
            if resolved_shift is not None and resolved_shift.roster_entry_id else (),
            instances=spans, observed_elsewhere=d.observed_elsewhere,
        ))
    return result


def day_facts_many(db: Session, company_id: int, employee_ids: Sequence[int], start: date,
                   end: date, *, light: bool = False, batch_out: Optional[dict] = None) -> Dict[int, List[DayFacts]]:
    """The day facts of several employees (LH 05 §3.1): one implementation for every consumer; at most
    ``MAX_EMPLOYEE_DAYS`` employee-days per call (RANGE_TOO_LONG). ``light`` (dashboards, which read portions only)
    skips shift minutes and the calendar version: ``scheduled_minutes`` is then the 8 h default and
    ``calendar_version`` empty."""
    days = _dates(start, end)
    ids = list(dict.fromkeys(employee_ids))
    if len(ids) * len(days) > MAX_EMPLOYEE_DAYS:
        raise bad_request("Choose fewer employees or a shorter range.", "RANGE_TOO_LONG")
    patterns = _resolver(db, company_id)
    owned = "lh_graph_memo" not in db.info
    if owned:
        db.info["lh_graph_memo"] = {}
    try:
        batch = _Batch(db, company_id, ids, start, end, light=light)
        # The holiday-calendar criterion ``shift_ids`` (EC AS-INT-07) is resolved once for the whole batch, the
        # substitute look-back included, and only when a criteria row uses it.
        patterns.prime_shift_criterion(ids, start - timedelta(days=SUBSTITUTE_LOOKBACK_DAYS), end)
        if batch_out is not None:  # the attendance batch loader reuses the resolved shifts (AS-M3, IG-AS-77)
            batch_out["shifts"] = batch.shifts
        return {employee_id: _facts_for(db, company_id, employee_id, start, end, patterns, batch)
                for employee_id in ids}
    finally:
        if owned:
            db.info.pop("lh_graph_memo", None)


def day_facts(db: Session, company_id: int, employee_id: int, start: date, end: date) -> List[DayFacts]:
    """Employment, branch, work week, weekly off, holiday, working portion and minutes of every date of
    ``start..end`` (SPINE §7; LH 05 §3)."""
    return day_facts_many(db, company_id, [employee_id], start, end)[employee_id]


# ---------------------------------------------------------------------------
# Expected presence (LH 05 §4.2, SPINE V3-062) — attendance dashboards read it (BUG-12)
# ---------------------------------------------------------------------------
_HALVES = {"FULL": {"FIRST_HALF", "SECOND_HALF"}, "FIRST_HALF": {"FIRST_HALF"}, "SECOND_HALF": {"SECOND_HALF"},
           "NONE": set()}


def _portion_of(halves: set) -> str:
    if halves == {"FIRST_HALF", "SECOND_HALF"}:
        return "FULL"
    return next(iter(halves)) if halves else "NONE"


def portion_units(portion: str) -> Decimal:
    return {"FULL": ONE, "FIRST_HALF": HALF, "SECOND_HALF": HALF}.get(portion, ZERO)


@dataclass(frozen=True)
class Expectation:
    date: date
    expected_portion: str
    expected_minutes: int
    reasons: Tuple[str, ...]
    on_leave_portion: str = "NONE"
    on_duty_portion: str = "NONE"
    working_portion: str = "NONE"
    leave_ids: Tuple[int, ...] = ()
    calendar_version: Optional[str] = None


def expected_presence(db: Session, company_id: int, employee_ids: Sequence[int], start: date,
                      end: date, *, light: bool = False,
                      facts: Optional[Dict[int, List[DayFacts]]] = None) -> Dict[int, List[Expectation]]:
    """What attendance should expect per employee and date (LH 05 §4.2): the working portion, minus suspensions and
    APPROVED leave; APPROVED on-duty satisfies its portion without a punch; pending leave only adds a reason."""
    # ``facts``: day facts the caller already holds for the same employees and range (AS-M3 batch loader).
    facts = facts if facts is not None else day_facts_many(db, company_id, employee_ids, start, end, light=light)
    ids = list(facts)
    leave_rows = db.query(LeaveDay.employee_id, LeaveDay.date, LeaveDay.portion, LeaveDay.leave_id).join(
        Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id)).filter(
        LeaveDay.company_id == company_id, LeaveDay.employee_id.in_(ids or [-1]), LeaveDay.date >= start,
        LeaveDay.date <= end, LeaveDay.voided_at.is_(None), func.lower(Leave.status) == "approved").all()
    on_leave: Dict[Tuple[int, date], set] = defaultdict(set)
    leave_ids: Dict[Tuple[int, date], set] = defaultdict(set)
    for employee_id, day, portion, leave_id in leave_rows:
        on_leave[(employee_id, day)] |= _HALVES.get(portion, set())
        leave_ids[(employee_id, day)].add(leave_id)
    pending: set = set()
    for employee_id, s_, e_ in db.query(Leave.employee_id, Leave.start_date, Leave.end_date).filter(
            Leave.company_id == company_id, Leave.employee_id.in_(ids or [-1]), Leave.start_date <= end,
            Leave.end_date >= start, func.lower(Leave.status) == "pending"):
        for n in range((min(e_, end) - max(s_, start)).days + 1):
            pending.add((employee_id, max(s_, start) + timedelta(days=n)))
    suspended: set = set()
    for row in db.query(EmployeeSuspension).filter(
            EmployeeSuspension.company_id == company_id, EmployeeSuspension.employee_id.in_(ids or [-1]),
            EmployeeSuspension.status == "ACTIVE"):
        first = max(row.start_date, start)
        last = min(row.end_date or end, end)
        for n in range((last - first).days + 1):
            suspended.add((row.employee_id, first + timedelta(days=n)))
    on_duty: Dict[Tuple[int, date], set] = defaultdict(set)
    for employee_id, s_, e_, portion in _approved_on_duty(db, company_id, ids, start, end):
        for n in range((min(e_, end) - max(s_, start)).days + 1):
            on_duty[(employee_id, max(s_, start) + timedelta(days=n))] |= _HALVES.get(portion or "FULL", set())
    out: Dict[int, List[Expectation]] = {}
    for employee_id, rows in facts.items():
        items: List[Expectation] = []
        for f in rows:
            key = (employee_id, f.date)
            reasons: List[str] = []
            if not f.employed:
                items.append(Expectation(f.date, "NONE", 0, ("NOT_EMPLOYED",), calendar_version=f.calendar_version))
                continue
            halves = set(_HALVES[f.working_portion])
            if f.weekly_off_portion:
                reasons.append("WEEKLY_OFF")
            if f.holiday is not None and f.holiday.non_working:
                reasons.append("HOLIDAY")
            if key in suspended and halves:
                halves = set()
                reasons.append("SUSPENDED")
            leave = halves & on_leave.get(key, set())
            if leave:
                halves -= leave
                reasons.append("LEAVE")
            if key in pending:
                reasons.append("LEAVE_PENDING")
            duty = halves & on_duty.get(key, set())
            if duty:
                reasons.append("ON_DUTY")
            portion = _portion_of(halves)
            if portion == f.working_portion:
                minutes = f.scheduled_minutes
            elif portion == "NONE" or f.working_portion == "NONE":
                minutes = 0
            else:
                minutes = f.scheduled_minutes // 2
            items.append(Expectation(f.date, portion, minutes, tuple(dict.fromkeys(reasons)), _portion_of(leave),
                                     _portion_of(duty), f.working_portion, tuple(sorted(leave_ids.get(key, ()))),
                                     f.calendar_version))
        out[employee_id] = items
    return out


def _approved_on_duty(db: Session, company_id: int, ids: Sequence[int], start: date, end: date):
    """APPROVED on-duty requests in the range: (employee, date, date, portion)."""
    return [(r.employee_id, r.date, r.date, r.portion) for r in db.query(OnDutyRequest).filter(
        OnDutyRequest.company_id == company_id, OnDutyRequest.employee_id.in_(list(ids) or [-1]),
        OnDutyRequest.date >= start, OnDutyRequest.date <= end, OnDutyRequest.status == "APPROVED")]


# ---------------------------------------------------------------------------
# Leave days and balance transactions
# ---------------------------------------------------------------------------
def portion_on(day: date, start: date, end: date, start_portion: str, end_portion: str) -> str:
    if start == end:
        if start_portion == "SECOND_HALF":
            return "SECOND_HALF"
        return "FIRST_HALF" if end_portion == "FIRST_HALF" else "FULL"
    if day == start and start_portion == "SECOND_HALF":
        return "SECOND_HALF"
    if day == end and end_portion == "FIRST_HALF":
        return "FIRST_HALF"
    return "FULL"


def _units(portion: str) -> Decimal:
    return ONE if portion == "FULL" else HALF


def counts_against_balance(facts: DayFacts, leave_type: LeaveType) -> bool:
    return facts.employed and (facts.working or bool(leave_type.counts_non_working_days))


def plan_leave(db: Session, company_id: int, employee_id: int, leave_type: LeaveType, start: date, end: date,
               start_portion: str = "FULL", end_portion: str = "FULL") -> Tuple[Decimal, List[Tuple[DayFacts, str, bool]]]:
    """The charged days of a leave and, per date, its facts, portion and whether it is charged."""
    plan = []
    total = ZERO
    for facts in day_facts(db, company_id, employee_id, start, end):
        portion = portion_on(facts.date, start, end, start_portion, end_portion)
        counted = counts_against_balance(facts, leave_type)
        if counted:
            total += _units(portion)
        plan.append((facts, portion, counted))
    return total, plan


def pay_treatment_snapshot(db: Session, company_id: int, employee_id: int, leave_type: LeaveType, on: date) -> dict:
    """LEAVE_PAY_POLICY row of the leave type in force for the employee on ``on``, else the leave type's paid flag."""
    from app.payroll.services import policies

    snapshot = {"leave_type_code": leave_type.code, "counts_non_working_days": bool(leave_type.counts_non_working_days),
                "pay_treatment": "FULL" if leave_type.is_paid else "UNPAID", "source": "LEAVE_TYPE"}
    try:
        resolved = policies.resolve_for_employee(db, company_id, "LEAVE_PAY_POLICY", employee_id, on)
    except Exception:
        return snapshot
    if resolved.get("status") != "RESOLVED":
        return snapshot
    version = resolved["version"]
    row = next((r for r in (version.get("fields") or {}).get("rows", []) if r.get("leave_type_code") == leave_type.code), None)
    if row is None:
        return snapshot | {"policy_version_id": version["id"]}
    treatment = policy_types.leave_pay_treatment(row, is_paid=bool(leave_type.is_paid))
    snapshot.update(pay_treatment=treatment, source="POLICY", policy_version_id=version["id"],
                    policy_hash=version.get("policy_hash"))
    if treatment == "PARTIAL" and row.get("partial_percent") is not None:
        snapshot["partial_percent"] = str(row["partial_percent"])
    if treatment == "TIERED":
        snapshot["tiers"] = [{"from_day": t["from_day"], "to_day": t["to_day"], "percent": str(t["percent"])}
                             for t in row.get("tiers") or []]
        snapshot["tiers_window_months"] = int(row.get("tiers_window_months") or 12)
    return snapshot


def leave_content(leave: Leave, days: Sequence[LeaveDay]) -> dict:
    return {
        "id": leave.id, "employee_id": leave.employee_id, "leave_type": leave.leave_type, "status": leave.status,
        "start_date": leave.start_date, "end_date": leave.end_date, "start_portion": leave.start_portion,
        "end_portion": leave.end_portion,
        "days": sorted(
            [{"date": d.date, "portion": d.portion, "units": d.units, "counts": bool(d.counts_against_balance),
              "pay": d.pay_treatment_snapshot, "voided": d.voided_at is not None} for d in days],
            key=lambda item: (item["date"], item["portion"])),
    }


def _active_leave_days(db: Session, leave: Leave) -> List[LeaveDay]:
    return db.query(LeaveDay).filter(LeaveDay.company_id == leave.company_id, LeaveDay.leave_id == leave.id,
                                     LeaveDay.voided_at.is_(None)).order_by(LeaveDay.date, LeaveDay.portion).all()


def _transaction(db: Session, company_id: int, *, employee_id: int, leave_type: str, delta: Decimal, reason: str,
                 key: str, effective_date: date, ref_type: str, ref_id: int, actor_user_id: Optional[int],
                 note: Optional[str] = None) -> bool:
    """Append a balance transaction once per idempotency key; False when the key was already used (EC-LV-07)."""
    exists = db.query(LeaveBalanceTransaction.id).filter(
        LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.idempotency_key == key).first()
    if exists or delta == 0:
        return False
    db.add(LeaveBalanceTransaction(
        company_id=company_id, employee_id=employee_id, leave_type=leave_type, delta=delta, reason=reason,
        idempotency_key=key, effective_date=effective_date, ref_type=ref_type, ref_id=ref_id,
        created_by_user_id=actor_user_id, note=note,
    ))
    db.flush()
    return True


def _by_year(days: Iterable[LeaveDay]) -> Dict[int, Tuple[Decimal, date]]:
    years: Dict[int, Tuple[Decimal, date]] = {}
    for row in days:
        if not row.counts_against_balance:
            continue
        total, first = years.get(row.date.year, (ZERO, row.date))
        years[row.date.year] = (total + Decimal(row.units), min(first, row.date))
    return years


def generate_leave_days(db: Session, leave: Leave, actor_user_id: Optional[int] = None) -> List[LeaveDay]:
    """At approval: one leave_days row per date, USE transactions per year, and a change-log entry."""
    from app.services import leave_access

    if leave_access.v2_enabled(db, leave.company_id):  # leave v2: plan charging + ledger per cycle
        from app.services import leave_postings

        return leave_postings.generate(db, leave, actor_user_id)
    if _active_leave_days(db, leave):
        return _active_leave_days(db, leave)
    leave_type = db.query(LeaveType).filter(LeaveType.company_id == leave.company_id,
                                            LeaveType.code == leave.leave_type).first()
    if leave_type is None:
        raise bad_request("The leave type no longer exists.", "INVALID_LEAVE_TYPE")
    attendance_service.lock_employee(db, leave.employee_id)
    total, plan = plan_leave(db, leave.company_id, leave.employee_id, leave_type, leave.start_date, leave.end_date,
                             leave.start_portion or "FULL", leave.end_portion or "FULL")
    snapshot = pay_treatment_snapshot(db, leave.company_id, leave.employee_id, leave_type, leave.start_date)
    rows = []
    for facts, portion, counted in plan:
        rows.append(LeaveDay(company_id=leave.company_id, leave_id=leave.id, employee_id=leave.employee_id,
                             date=facts.date, portion=portion, units=_units(portion), counts_against_balance=counted,
                             pay_treatment_snapshot=snapshot))
    db.add_all(rows)
    try:
        with db.begin_nested():
            db.flush()
    except DBAPIError:
        raise conflict("Another approved leave already covers some of these dates.", "LEAVE_OVERLAP")
    leave.number_of_days = total
    for year, (units, first) in sorted(_by_year(rows).items()):
        _transaction(db, leave.company_id, employee_id=leave.employee_id, leave_type=leave.leave_type, delta=-units,
                     reason="USE", key=f"leave:{leave.id}:use:{year}", effective_date=first, ref_type="leave",
                     ref_id=leave.id, actor_user_id=actor_user_id)
    log_change(db, leave.company_id, record_type="leave", record_id=leave.id, change="CREATED",
               content=leave_content(leave, rows), employee_id=leave.employee_id, date_from=leave.start_date,
               date_to=leave.end_date, actor_user_id=actor_user_id)
    return rows


def void_leave_days(db: Session, leave: Leave, reason: str, actor_user_id: Optional[int] = None) -> None:
    """Cancellation: void the days, give the charged days back (once), log the change (EC-LV-04)."""
    from app.services import leave_access

    if leave_access.v2_enabled(db, leave.company_id):
        from app.services import leave_postings

        leave_postings.void(db, leave, reason, actor_user_id)
        return
    rows = _active_leave_days(db, leave)
    now = utcnow()
    for row in rows:
        row.voided_at, row.void_reason = now, reason[:30]
    db.flush()
    for year, (units, first) in sorted(_by_year(rows).items()):
        _transaction(db, leave.company_id, employee_id=leave.employee_id, leave_type=leave.leave_type, delta=units,
                     reason="USE", key=f"leave:{leave.id}:cancel:{year}", effective_date=first, ref_type="leave",
                     ref_id=leave.id, actor_user_id=actor_user_id, note="Approved leave cancelled")
    log_change(db, leave.company_id, record_type="leave", record_id=leave.id, change="VOIDED",
               content=leave_content(leave, rows), employee_id=leave.employee_id, date_from=leave.start_date,
               date_to=leave.end_date, actor_user_id=actor_user_id)


def reevaluate_leave_days(db: Session, company_id: int, dates: Iterable[date], actor_user_id: Optional[int] = None, *,
                          employee_ids: Optional[Iterable[int]] = None, note: Optional[str] = None) -> int:
    """A holiday, work week, roster or shift changed on ``dates``: re-decide whether approved leave there is charged
    (EC-LV-06, risk 7). ``employee_ids`` limits it to those employees (a shift version or rule, JC-AS-34); ``note``
    is the v2 request message ("Roster changed", ...)."""
    wanted = sorted(set(dates))
    if not wanted:
        return 0
    people = sorted(set(employee_ids)) if employee_ids is not None else None
    from app.services import leave_access

    if leave_access.v2_enabled(db, company_id):  # leave v2: regenerate the touched requests under their plan
        from app.services import leave_recalc

        return leave_recalc.recount_dates(db, company_id, wanted, actor_user_id=actor_user_id, employee_ids=people,
                                          **({"note": note} if note else {}))
    query = db.query(LeaveDay).filter(LeaveDay.company_id == company_id, LeaveDay.date.in_(wanted),
                                      LeaveDay.voided_at.is_(None))
    if people is not None:
        query = query.filter(LeaveDay.employee_id.in_(people or [0]))
    rows = query.order_by(LeaveDay.id).all()
    changed = 0
    for row in rows:
        leave = db.get(Leave, row.leave_id)
        leave_type = leave and db.query(LeaveType).filter(LeaveType.company_id == company_id,
                                                          LeaveType.code == leave.leave_type).first()
        if leave is None or leave_type is None:
            continue
        facts = day_facts(db, company_id, row.employee_id, row.date, row.date)[0]
        counted = counts_against_balance(facts, leave_type)
        if counted == bool(row.counts_against_balance):
            continue
        row.counts_against_balance = counted
        units = Decimal(row.units)
        delta = -units if counted else units
        previous = db.query(func.count(LeaveBalanceTransaction.id)).filter(
            LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.ref_type == "leave_day",
            LeaveBalanceTransaction.ref_id == row.id).scalar() or 0
        _transaction(db, company_id, employee_id=row.employee_id, leave_type=leave.leave_type, delta=delta, reason="USE",
                     key=f"leave_day:{row.id}:recount:{previous + 1}", effective_date=row.date, ref_type="leave_day",
                     ref_id=row.id, actor_user_id=actor_user_id,
                     note="Holiday calendar changed" if not counted else "Holiday removed")
        leave.number_of_days = Decimal(leave.number_of_days) - delta
        db.flush()
        log_change(db, company_id, record_type="leave", record_id=leave.id, change="UPDATED",
                   content=leave_content(leave, _active_leave_days(db, leave)), employee_id=leave.employee_id,
                   date_from=row.date, actor_user_id=actor_user_id, details={"reason": "HOLIDAY_CHANGED"})
        changed += 1
    return changed


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, _calendar.monthrange(year, month)[1]))


def tiered_history(db: Session, company_id: int, employee_id: int, leave_type_code: str, start: date, end: date,
                   window_months: int = 12) -> Tuple[Decimal, Tuple[Tuple[date, Decimal], ...]]:
    """``days_before_period`` and ``rolls_off`` of TIERED pay (F-02, EC-LV-05).

    A leave day ``x`` is inside the rolling window of date ``d`` while ``d < x + window_months``.
    """
    earliest = _add_months(start, -window_months)
    rows = (
        db.query(LeaveDay.date, LeaveDay.units)
        .join(Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id))
        .filter(LeaveDay.company_id == company_id, LeaveDay.employee_id == employee_id, LeaveDay.voided_at.is_(None),
                LeaveDay.counts_against_balance.is_(True), Leave.leave_type == leave_type_code,
                LeaveDay.date >= earliest, LeaveDay.date < start)
        .all()
    )
    before = ZERO
    rolls: Dict[date, Decimal] = defaultdict(lambda: ZERO)
    for day, units in rows:
        leaves_window = _add_months(day, window_months)
        if leaves_window < start:
            continue
        before += Decimal(units)
        if leaves_window <= end:
            rolls[leaves_window] += Decimal(units)
    return before, tuple(sorted(rolls.items()))


# ---------------------------------------------------------------------------
# Ledger inputs (the snapshot builder's read API)
# ---------------------------------------------------------------------------
def _shift_hours(shift: Optional[Shift]) -> Optional[Decimal]:
    if shift is None or shift.duration_hours is None:
        return None
    hours = Decimal(shift.duration_hours) - Decimal(shift.unpaid_break_minutes or 0) / Decimal(60)
    return hours if hours > 0 else None


def _leave_portions(db: Session, company_id: int, employee_id: int, start: date, end: date
                    ) -> Dict[date, List[ledger.LeavePortion]]:
    rows = (
        db.query(LeaveDay, Leave)
        .join(Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id))
        .filter(LeaveDay.company_id == company_id, LeaveDay.employee_id == employee_id, LeaveDay.voided_at.is_(None),
                LeaveDay.date >= start, LeaveDay.date <= end, Leave.status == "approved")
        .order_by(LeaveDay.date, LeaveDay.portion)
        .all()
    )
    hashes: Dict[int, str] = {}
    tiered: Dict[Tuple[str, int], ledger.TieredPay] = {}
    result: Dict[date, List[ledger.LeavePortion]] = defaultdict(list)
    for row, leave in rows:
        if leave.id not in hashes:
            hashes[leave.id] = content_hash(leave_content(leave, _active_leave_days(db, leave)))
        snap = row.pay_treatment_snapshot or {}
        treatment = snap.get("pay_treatment") or "FULL"
        partial = Decimal(snap["partial_percent"]) if treatment == "PARTIAL" and snap.get("partial_percent") else None
        tiered_pay = None
        if treatment == "TIERED":
            window = int(snap.get("tiers_window_months") or 12)
            key = (leave.leave_type, window)
            if key not in tiered:
                before, rolls = tiered_history(db, company_id, employee_id, leave.leave_type, start, end, window)
                tiers = tuple(ledger.Tier(t["from_day"], t.get("to_day"), Decimal(str(t["percent"])))
                              for t in snap.get("tiers") or [])
                tiered[key] = ledger.TieredPay(tiers, before, rolls)
            tiered_pay = tiered[key]
        if treatment == "PARTIAL" and partial is None:
            treatment = "UNPAID"
        result[row.date].append(ledger.LeavePortion(
            leave_id=leave.id, leave_type_code=leave.leave_type, portion=row.portion, pay_treatment=treatment,
            partial_percent=partial, tiered=tiered_pay,
            counts_non_working_days=bool(snap.get("counts_non_working_days", False)), content_hash=hashes[leave.id],
        ))
    return result


def _attendance_inputs(db: Session, company_id: int, employee_id: int, start: date, end: date,
                       lock_at: Optional[datetime], early_exit_grace_minutes: int) -> Dict[date, ledger.AttendanceInput]:
    records = (
        db.query(Attendance)
        .filter(Attendance.company_id == company_id, Attendance.employee_id == employee_id,
                Attendance.date >= start, Attendance.date <= end,
                Attendance.voided_at.is_(None))  # AS-M2: voided sessions are never pay inputs (spine §15.4)
        .order_by(Attendance.date, Attendance.check_in_time)
        .all()
    )
    if not records:
        return {}
    if lock_at is not None and any(attendance_service.is_live(r) for r in records):
        policy = attendance_policy.load(db, company_id)
        attendance_service.lock_employee(db, employee_id)
        for record in records:
            db.refresh(record)
            if attendance_service.is_live(record):
                attendance_service.expire_if_needed(db, record, policy, lock_at)
    now = lock_at or attendance_service._now()
    breaks = attendance_service.breaks_by_session(db, [r.id for r in records])
    result: Dict[date, ledger.AttendanceInput] = {}
    for record in records:
        hours = attendance_service.hours_for(record, breaks[record.id], now)
        closed = record.closed_at is not None and record.check_out_time is None
        worked = hours.net_seconds if (hours.complete and not hours.provisional and not closed) else None
        early = bool(record.early_exit) and (record.early_exit_minutes or 0) > early_exit_grace_minutes
        # A correction rewrites the stored mark itself, so no mark is both stored and regularised (audit 03 §1.8).
        result[record.date] = ledger.AttendanceInput(
            status=STATUS_MAP.get(record.status, "PRESENT"), session_id=record.id, closed_without_punch_out=closed,
            worked_seconds=worked, late=record.status == "late", early_exit=early, regularised=False,
            content_hash=attendance_service.content_hash(record, breaks[record.id]),
        )
    return result


def _evaluated_inputs(db: Session, company_id: int, employee_id: int, start: date, end: date,
                      sessions: Dict[date, ledger.AttendanceInput]) -> Dict[date, ledger.AttendanceInput]:
    """Attendance v2 tenants (AS 05 §4, JC-AS-01): a STANDARD ``attendance_days`` row decides its date through
    ``DAY_RESULT_TO_LEDGER`` (an input, or none); LEGACY_V1 rows and dates without a row keep today's session input
    (the LEGACY_V1 port is exact, so the ledger output is the same either way; IG-AS-66)."""
    from app.payroll import day_results
    from app.services import attendance_access, attendance_days

    if not attendance_access.v2_enabled(db, company_id):
        return sessions
    result = dict(sessions)
    for (_, day), row in attendance_days.days_for(db, company_id, [employee_id], start, end).items():
        if row.rules_mode != "STANDARD":
            continue
        mapped = day_results.to_attendance_input(row)
        if mapped is None:
            result.pop(day, None)
        else:
            result[day] = mapped
    return result


def _lop_by_date(db: Session, company_id: int, employee_id: int, start: date, end: date, statuses=("APPROVED",)):
    result: Dict[date, List[LopInput]] = defaultdict(list)
    for row in db.query(LopInput).filter(LopInput.company_id == company_id, LopInput.employee_id == employee_id,
                                         LopInput.date >= start, LopInput.date <= end, LopInput.status.in_(statuses)):
        result[row.date].append(row)
    return result


def _on_duty_by_date(db: Session, company_id: int, employee_id: int, start: date, end: date):
    result: Dict[date, List[OnDutyRequest]] = defaultdict(list)
    for row in db.query(OnDutyRequest).filter(OnDutyRequest.company_id == company_id,
                                              OnDutyRequest.employee_id == employee_id, OnDutyRequest.date >= start,
                                              OnDutyRequest.date <= end, OnDutyRequest.status == "APPROVED"):
        result[row.date].append(row)
    return result


def _suspensions(db: Session, company_id: int, employee_id: int, start: date, end: date) -> List[EmployeeSuspension]:
    return db.query(EmployeeSuspension).filter(
        EmployeeSuspension.company_id == company_id, EmployeeSuspension.employee_id == employee_id,
        EmployeeSuspension.status == "ACTIVE", EmployeeSuspension.paid.is_(False),
        EmployeeSuspension.start_date <= end, EmployeeSuspension.end_date >= start,
    ).order_by(EmployeeSuspension.start_date).all()


def lop_content(row: LopInput) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "date": row.date, "portion": row.portion,
            "status": row.status, "reason": row.reason}


def suspension_content(row: EmployeeSuspension) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "start_date": row.start_date, "end_date": row.end_date,
            "paid": bool(row.paid), "status": row.status}


def on_duty_content(row: OnDutyRequest) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "date": row.date, "portion": row.portion,
            "status": row.status}


def ledger_inputs(
    db: Session, company_id: int, employee_id: int, start: date, end: date, *,
    lock_at: Optional[datetime] = None, early_exit_grace_minutes: int = 0,
    standard_hours_per_day: Optional[Decimal] = None, use_shift_duration: bool = True,
) -> List[ledger.DayInput]:
    """Every date of ``start..end`` as a ledger.DayInput (F-02, F-03, F-04).

    Dates outside employment carry ``employed=False`` with the weekly off and holiday of the nearest
    placement. Approved leave days, holidays of the branch on each date (optional ones per the
    employee's choice), approved manual LOP and on-duty, unpaid suspensions and attendance with canonical
    net seconds (None when the punch-out is missing). With ``lock_at`` live sessions that are past the
    maximum shift length at that instant are first closed as missing punch-outs (audit 03 §1.3).
    ``segment`` is the primary assignment id; ``standard_hours`` is the net hours of the shift that resolves on
    the date (``shift_resolution``: rule > placement, JC-AS-02), or ``standard_hours_per_day``, times the
    assignment FTE when either differs from the default; ``use_shift_duration = False`` uses the standard hours
    for everyone (BUG-AS-31).
    """
    facts = day_facts(db, company_id, employee_id, start, end)
    leaves = _leave_portions(db, company_id, employee_id, start, end)
    attendance = _evaluated_inputs(db, company_id, employee_id, start, end, _attendance_inputs(
        db, company_id, employee_id, start, end, lock_at, early_exit_grace_minutes))
    lops = _lop_by_date(db, company_id, employee_id, start, end)
    duties = _on_duty_by_date(db, company_id, employee_id, start, end)
    suspensions = _suspensions(db, company_id, employee_id, start, end)
    from app.services import shift_resolution

    resolved = shift_resolution.shifts_for_many(db, company_id, [employee_id], start, end)
    days: List[ledger.DayInput] = []
    for fact in facts:
        assignment = fact.assignment
        suspension = next((s for s in suspensions if s.start_date <= fact.date <= s.end_date), None)
        holiday = None
        if fact.holiday is not None:
            holiday = ledger.HolidayInput(fact.holiday.id, fact.holiday.type, fact.holiday.name, fact.holiday.content_hash)
        standard = None
        if assignment is not None and fact.employed:
            fte = Decimal(assignment.fte if assignment.fte is not None else 1)
            standard = shift_resolution.standard_hours(resolved.get((employee_id, fact.date)), fte,
                                                       standard_hours_per_day, use_shift_duration=use_shift_duration)
        days.append(ledger.DayInput(
            date=fact.date,
            employed=fact.employed,
            suspended_unpaid=suspension is not None and fact.employed,
            leaves=tuple(leaves.get(fact.date, ())),
            holiday=holiday,
            weekly_off=fact.weekly_off,
            manual_lop=tuple(ledger.ManualLop(r.id, r.portion, content_hash(lop_content(r))) for r in lops.get(fact.date, ())),
            attendance=attendance.get(fact.date),
            on_duty=tuple(ledger.OnDuty(r.id, r.portion, content_hash(on_duty_content(r))) for r in duties.get(fact.date, ())),
            segment=str(assignment.id) if assignment is not None and fact.employed else "",
            standard_hours=standard,
            assignment_id=assignment.id if assignment is not None else None,
            suspension_id=suspension.id if suspension is not None and fact.employed else None,
            work_week_id=fact.work_week_id,
        ))
    return days


def _pending_leave_v2(db: Session, company_id: int, employee_id: int, start: date, end: date) -> List[dict]:
    """Leave v2 items still open at the cutoff (LH SPINE §8): returned requests, change requests of approved leave,
    comp-off claims of worked dates, pending encashments and approved leave flagged BALANCE_CONFLICT."""
    from app.models.leave_v2 import CompOffClaim, LeaveChangeRequest, LeaveEncashmentRequest

    items: List[dict] = []
    touching = (Leave.company_id == company_id, Leave.employee_id == employee_id, Leave.start_date <= end,
                Leave.end_date >= start)
    for leave in db.query(Leave).filter(*touching, Leave.status == "returned").order_by(Leave.start_date, Leave.id):
        items.append({"type": "leave_returned", "id": leave.id, "start": leave.start_date.isoformat(),
                      "end": leave.end_date.isoformat(), "leave_type_code": leave.leave_type})
    for change, leave in db.query(LeaveChangeRequest, Leave).join(
            Leave, and_(Leave.company_id == LeaveChangeRequest.company_id, Leave.id == LeaveChangeRequest.leave_id)
    ).filter(*touching, LeaveChangeRequest.status == "PENDING").order_by(LeaveChangeRequest.id):
        items.append({"type": "leave_change", "id": change.id, "leave_id": leave.id, "kind": change.kind,
                      "start": leave.start_date.isoformat(), "end": leave.end_date.isoformat()})
    for claim in db.query(CompOffClaim).filter(
            CompOffClaim.company_id == company_id, CompOffClaim.employee_id == employee_id,
            CompOffClaim.worked_date >= start, CompOffClaim.worked_date <= end, CompOffClaim.status == "PENDING"):
        items.append({"type": "comp_off_claim", "id": claim.id, "start": claim.worked_date.isoformat(),
                      "end": claim.worked_date.isoformat()})
    for request in db.query(LeaveEncashmentRequest).filter(
            LeaveEncashmentRequest.company_id == company_id, LeaveEncashmentRequest.employee_id == employee_id,
            LeaveEncashmentRequest.status == "PENDING"):
        items.append({"type": "leave_encashment", "id": request.id, "leave_type_code": request.leave_type_code,
                      "units": str(request.units)})
    for leave in db.query(Leave).filter(*touching, Leave.status == "approved",
                                        Leave.flags.contains(["BALANCE_CONFLICT"])):
        items.append({"type": "leave_balance_conflict", "id": leave.id, "start": leave.start_date.isoformat(),
                      "end": leave.end_date.isoformat(), "leave_type_code": leave.leave_type})
    return items


def pending_inputs(db: Session, company_id: int, employee_id: int, start: date, end: date) -> List[dict]:
    """Requests still awaiting a decision that touch ``start..end`` (exceptions at the input cutoff, EC-LV-02)."""
    items: List[dict] = []
    for leave in db.query(Leave).filter(
        Leave.company_id == company_id, Leave.employee_id == employee_id, Leave.start_date <= end,
        Leave.end_date >= start,
        or_(Leave.status == "pending", (Leave.status == "approved") & (Leave.cancellation_status == "pending")),
    ).order_by(Leave.start_date, Leave.id):
        items.append({"type": "leave_cancellation" if leave.status == "approved" else "leave", "id": leave.id,
                      "start": leave.start_date.isoformat(), "end": leave.end_date.isoformat(),
                      "leave_type_code": leave.leave_type})
    items.extend(_pending_leave_v2(db, company_id, employee_id, start, end))
    for row in _lop_by_date(db, company_id, employee_id, start, end, statuses=("PENDING",)).values():
        items.extend({"type": "lop_input", "id": r.id, "start": r.date.isoformat(), "end": r.date.isoformat(),
                      "portion": r.portion} for r in row)
    for row in db.query(OnDutyRequest).filter(
        OnDutyRequest.company_id == company_id, OnDutyRequest.employee_id == employee_id,
        OnDutyRequest.date >= start, OnDutyRequest.date <= end, OnDutyRequest.status == "PENDING",
    ).order_by(OnDutyRequest.date, OnDutyRequest.id):
        items.append({"type": "on_duty", "id": row.id, "start": row.date.isoformat(), "end": row.date.isoformat(),
                      "portion": row.portion})
    return items


# ---------------------------------------------------------------------------
# Shared command helpers
# ---------------------------------------------------------------------------
def _audit(db: Session, principal: Optional[Principal], action: str, description: str, entity_type: str, entity_id: int,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description, entity_type, entity_id,
                        details=json.loads(json.dumps(details, default=_json_default)))


def _portion(value: str) -> str:
    if value not in PORTIONS:
        raise bad_request("Portion must be FULL, FIRST_HALF or SECOND_HALF.", "INVALID_PORTION")
    return value


def _slots(portion: str) -> set:
    return {"AM", "PM"} if portion == "FULL" else ({"AM"} if portion == "FIRST_HALF" else {"PM"})


def _overlapping(rows: Iterable[Any], portion: str) -> bool:
    return any(_slots(r.portion) & _slots(portion) for r in rows)


def _ensure_in_scope(db: Session, principal: Principal, permission: str, employee_id: int, start: date, end: date) -> None:
    access.ensure_employee_in_scope(db, principal, permission, employee_id, start, end)


def _check_row_version(row: Any, expected: Optional[int]) -> None:
    from app.core import concurrency

    concurrency.check_row_version(row, expected)


# ---------------------------------------------------------------------------
# Manual LOP inputs (EC-ATT-16)
# ---------------------------------------------------------------------------
def lop_json(row: LopInput) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "date": row.date.isoformat(), "portion": row.portion,
            "reason": row.reason, "status": row.status, "approval_request_id": row.approval_request_id,
            "created_by_user_id": row.created_by_user_id, "row_version": row.row_version,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "cancelled_at": row.cancelled_at.isoformat() if row.cancelled_at else None}


def lop_conflicts(db: Session, company_id: int, employee_id: int, day: date, portion: str) -> List[dict]:
    """Why a manual LOP input on ``portion`` of ``day`` is refused: a leave, holiday, weekly off, suspension or LOP slot."""
    inputs = ledger_inputs(db, company_id, employee_id, day, day)[0]
    cfg = ledger.LedgerConfig(attendance_enabled=True, leave_enabled=True)
    found = [item.to_dict() for item in ledger.manual_lop_conflicts(inputs, portion, cfg)]
    pending = _lop_by_date(db, company_id, employee_id, day, day, statuses=("PENDING",)).get(day, [])
    if _overlapping(pending, portion):
        found.append({"code": "MANUAL_LOP_CONFLICT", "message": "A manual LOP input for this slot is awaiting approval.",
                      "detail": "PENDING_LOP"})
    return found


def create_lop_input(db: Session, principal: Principal, *, employee_id: int, day: date, portion: str, reason: str) -> dict:
    _ensure_in_scope(db, principal, INPUTS_EDIT, employee_id, day, day)
    portion = _portion(portion)
    reason = (reason or "").strip()
    if not reason:
        raise bad_request("Enter a reason for the LOP input.", "REASON_REQUIRED")
    attendance_service.lock_employee(db, employee_id)
    conflicts = lop_conflicts(db, principal.company_id, employee_id, day, portion)
    if conflicts:
        raise conflict("Manual LOP cannot be entered on this slot: " + conflicts[0]["message"], "LOP_SLOT_TAKEN")
    row = LopInput(company_id=principal.company_id, employee_id=employee_id, date=day, portion=portion, reason=reason,
                   status="PENDING", created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    request = approvals.submit(db, principal, module=LOP_MODULE, subject_type=LOP_SUBJECT, subject_id=row.id,
                               subject_employee_id=employee_id, context={"date": day, "portion": portion})
    row.approval_request_id = request.id
    db.flush()
    _audit(db, principal, "LOP_INPUT_CREATED", f"Manual LOP for employee #{employee_id} on {day} ({portion})",
           "lop_input", row.id, after=lop_content(row), approval_request_id=request.id)
    return lop_json(row)


@approvals.on_decided(LOP_MODULE, LOP_SUBJECT)
def _lop_decided(db: Session, principal: Optional[Principal], request) -> None:
    row = db.query(LopInput).filter(LopInput.company_id == request.company_id, LopInput.id == request.subject_id).first()
    if row is None or row.status != "PENDING":
        return
    row.status = {"APPROVED": "APPROVED", "REJECTED": "REJECTED"}.get(request.status, "CANCELLED")
    row.decided_at = utcnow()
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="lop_input", record_id=row.id, change="UPDATED",
               content=lop_content(row), employee_id=row.employee_id, date_from=row.date,
               actor_user_id=principal.user_id if principal else None)


def cancel_lop_input(db: Session, principal: Principal, lop_id: int, *, row_version: Optional[int] = None) -> dict:
    row = db.query(LopInput).filter(LopInput.company_id == principal.company_id, LopInput.id == lop_id).with_for_update().first()
    if row is None:
        raise not_found("LOP input")
    _ensure_in_scope(db, principal, INPUTS_EDIT, row.employee_id, row.date, row.date)
    _check_row_version(row, row_version)
    if row.status not in ("PENDING", "APPROVED"):
        raise conflict(f"This LOP input is already {row.status.lower()}.", "LOP_INPUT_CLOSED")
    if row.status == "PENDING":
        approvals.withdraw(db, principal, module=LOP_MODULE, subject_type=LOP_SUBJECT, subject_id=row.id,
                           reason="LOP input cancelled")
    before = lop_content(row)
    row.status, row.cancelled_at, row.cancelled_by_user_id = "CANCELLED", utcnow(), principal.user_id
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="lop_input", record_id=row.id, change="VOIDED", content=lop_content(row),
               employee_id=row.employee_id, date_from=row.date, actor_user_id=principal.user_id)
    _audit(db, principal, "LOP_INPUT_CANCELLED", f"Cancelled manual LOP #{row.id}", "lop_input", row.id,
           before=before, after=lop_content(row))
    return lop_json(row)


def _range_filter(column_from, column_to, start: Optional[date], end: Optional[date]):
    clauses = []
    if start is not None:
        clauses.append(column_to >= start)
    if end is not None:
        clauses.append(column_from <= end)
    return clauses


def _list_scope(db: Session, principal: Principal, permission: str, start: Optional[date], end: Optional[date]):
    if not principal.has(permission):
        raise forbidden()
    if start is not None and end is not None and end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    today = clock.company_today(db, principal.company_id)
    return access.payroll_employee_scope(db, principal, permission, start or date(1900, 1, 1), end or today + timedelta(days=3660))


def list_lop_inputs(db: Session, principal: Principal, *, employee_id: Optional[int] = None, start: Optional[date] = None,
                    end: Optional[date] = None, status: Optional[str] = None, limit: int = 200) -> List[dict]:
    scope = _list_scope(db, principal, INPUTS_READ, start, end)
    query = db.query(LopInput).filter(LopInput.company_id == principal.company_id, scope.clause(LopInput.employee_id),
                                      *_range_filter(LopInput.date, LopInput.date, start, end))
    if employee_id is not None:
        query = query.filter(LopInput.employee_id == employee_id)
    if status:
        query = query.filter(LopInput.status == status)
    return [lop_json(r) for r in query.order_by(LopInput.date.desc(), LopInput.id.desc()).limit(limit)]


# ---------------------------------------------------------------------------
# Suspensions
# ---------------------------------------------------------------------------
def suspension_json(row: EmployeeSuspension) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat(), "paid": bool(row.paid), "reason": row.reason, "status": row.status,
            "row_version": row.row_version, "created_by_user_id": row.created_by_user_id,
            "cancelled_at": row.cancelled_at.isoformat() if row.cancelled_at else None}


def create_suspension(db: Session, principal: Principal, *, employee_id: int, start: date, end: date, paid: bool,
                      reason: str) -> dict:
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    _ensure_in_scope(db, principal, INPUTS_EDIT, employee_id, start, end)
    reason = (reason or "").strip()
    if not reason:
        raise bad_request("Enter a reason for the suspension.", "REASON_REQUIRED")
    attendance_service.lock_employee(db, employee_id)
    overlap = db.query(EmployeeSuspension.id).filter(
        EmployeeSuspension.company_id == principal.company_id, EmployeeSuspension.employee_id == employee_id,
        EmployeeSuspension.status == "ACTIVE", EmployeeSuspension.start_date <= end, EmployeeSuspension.end_date >= start,
    ).first()
    if overlap:
        raise conflict("Another suspension already covers some of these dates.", "SUSPENSION_OVERLAP")
    employee = _employee(db, principal.company_id, employee_id)
    assignments = primary_assignments(db, principal.company_id, employee_id)
    if not all(employed_on(employee, _covering(assignments, d), d) for d in (start, end)):
        raise bad_request("A suspension must fall inside the employment.", "OUTSIDE_EMPLOYMENT")
    row = EmployeeSuspension(company_id=principal.company_id, employee_id=employee_id, start_date=start, end_date=end,
                             paid=paid, reason=reason, status="ACTIVE", created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    log_change(db, row.company_id, record_type="suspension", record_id=row.id, change="CREATED",
               content=suspension_content(row), employee_id=employee_id, date_from=start, date_to=end,
               actor_user_id=principal.user_id)
    _audit(db, principal, "SUSPENSION_CREATED",
           f"{'Paid' if paid else 'Unpaid'} suspension of employee #{employee_id} from {start} to {end}",
           "employee_suspension", row.id, after=suspension_content(row))
    return suspension_json(row)


def cancel_suspension(db: Session, principal: Principal, suspension_id: int, *, row_version: Optional[int] = None) -> dict:
    row = db.query(EmployeeSuspension).filter(EmployeeSuspension.company_id == principal.company_id,
                                              EmployeeSuspension.id == suspension_id).with_for_update().first()
    if row is None:
        raise not_found("Suspension")
    _ensure_in_scope(db, principal, INPUTS_EDIT, row.employee_id, row.start_date, row.end_date)
    _check_row_version(row, row_version)
    if row.status != "ACTIVE":
        raise conflict("This suspension is already cancelled.", "SUSPENSION_CLOSED")
    before = suspension_content(row)
    row.status, row.cancelled_at, row.cancelled_by_user_id = "CANCELLED", utcnow(), principal.user_id
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="suspension", record_id=row.id, change="VOIDED",
               content=suspension_content(row), employee_id=row.employee_id, date_from=row.start_date,
               date_to=row.end_date, actor_user_id=principal.user_id)
    _audit(db, principal, "SUSPENSION_CANCELLED", f"Cancelled suspension #{row.id}", "employee_suspension", row.id,
           before=before, after=suspension_content(row))
    return suspension_json(row)


def list_suspensions(db: Session, principal: Principal, *, employee_id: Optional[int] = None, start: Optional[date] = None,
                     end: Optional[date] = None, limit: int = 200) -> List[dict]:
    scope = _list_scope(db, principal, INPUTS_READ, start, end)
    query = db.query(EmployeeSuspension).filter(
        EmployeeSuspension.company_id == principal.company_id, scope.clause(EmployeeSuspension.employee_id),
        *_range_filter(EmployeeSuspension.start_date, EmployeeSuspension.end_date, start, end))
    if employee_id is not None:
        query = query.filter(EmployeeSuspension.employee_id == employee_id)
    return [suspension_json(r) for r in query.order_by(EmployeeSuspension.start_date.desc()).limit(limit)]


# ---------------------------------------------------------------------------
# On-duty requests
# ---------------------------------------------------------------------------
def on_duty_json(row: OnDutyRequest) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "date": row.date.isoformat(), "portion": row.portion,
            "reason": row.reason, "status": row.status, "approval_request_id": row.approval_request_id,
            "requested_by_user_id": row.requested_by_user_id, "row_version": row.row_version,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None}


def _on_duty_target(db: Session, principal: Principal, employee_id: Optional[int]) -> Employee:
    if employee_id is None or authz.is_self(principal, employee_id):
        if not principal.has("attendance:mark"):
            raise forbidden()
        return authz.own_employee(db, principal)
    return authz.ensure_employee_access(db, principal, "attendance:regularize", employee_id)


def create_on_duty(db: Session, principal: Principal, *, employee_id: Optional[int], day: date, portion: str,
                   reason: str) -> dict:
    employee = _on_duty_target(db, principal, employee_id)
    portion = _portion(portion)
    reason = (reason or "").strip()
    if not reason:
        raise bad_request("Enter where or why you were on duty.", "REASON_REQUIRED")
    attendance_service.lock_employee(db, employee.id)
    facts = day_facts(db, principal.company_id, employee.id, day, day)[0]
    if not facts.employed:
        raise bad_request("On-duty work must fall inside the employment.", "OUTSIDE_EMPLOYMENT")
    existing = db.query(OnDutyRequest).filter(
        OnDutyRequest.company_id == principal.company_id, OnDutyRequest.employee_id == employee.id,
        OnDutyRequest.date == day, OnDutyRequest.status.in_(("PENDING", "APPROVED"))).all()
    if _overlapping(existing, portion):
        raise conflict("On-duty work is already recorded for this slot.", "ON_DUTY_OVERLAP")
    row = OnDutyRequest(company_id=principal.company_id, employee_id=employee.id, date=day, portion=portion,
                        reason=reason, status="PENDING", requested_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    request = approvals.submit(db, principal, module=ON_DUTY_MODULE, subject_type=ON_DUTY_SUBJECT, subject_id=row.id,
                               subject_employee_id=employee.id, context={"date": day, "portion": portion})
    row.approval_request_id = request.id
    db.flush()
    _audit(db, principal, "ON_DUTY_REQUESTED", f"On-duty request for employee #{employee.id} on {day} ({portion})",
           "on_duty_request", row.id, after=on_duty_content(row), approval_request_id=request.id)
    return on_duty_json(row)


@approvals.on_decided(ON_DUTY_MODULE, ON_DUTY_SUBJECT)
def _on_duty_decided(db: Session, principal: Optional[Principal], request) -> None:
    # One approval for a whole trip (AS-M4 batch, OD-5): every row of the batch carries the approval request id.
    rows = db.query(OnDutyRequest).filter(
        OnDutyRequest.company_id == request.company_id,
        or_(OnDutyRequest.id == request.subject_id, OnDutyRequest.approval_request_id == request.id)).all()
    for row in rows:
        if row.status != "PENDING":
            continue
        row.status = {"APPROVED": "APPROVED", "REJECTED": "REJECTED"}.get(request.status, "CANCELLED")
        row.decided_at = utcnow()
        row.row_version += 1
        db.flush()
        log_change(db, row.company_id, record_type="on_duty", record_id=row.id, change="UPDATED",
                   content=on_duty_content(row), employee_id=row.employee_id, date_from=row.date,
                   actor_user_id=principal.user_id if principal else None)


def cancel_on_duty(db: Session, principal: Principal, request_id: int, *, row_version: Optional[int] = None) -> dict:
    row = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == principal.company_id,
                                         OnDutyRequest.id == request_id).with_for_update().first()
    if row is None:
        raise not_found("On-duty request")
    own = authz.is_self(principal, row.employee_id)
    if not own:
        if not authz.can_access_employee(db, principal, "attendance:regularize", row.employee_id):
            raise not_found("On-duty request")
    _check_row_version(row, row_version)
    if row.status not in ("PENDING", "APPROVED"):
        raise conflict(f"This request is already {row.status.lower()}.", "ON_DUTY_CLOSED")
    if row.status == "APPROVED" and own and not principal.has_company_scope("attendance:regularize"):
        raise forbidden("Ask HR to withdraw approved on-duty work.")
    if row.status == "PENDING":
        approvals.withdraw(db, principal, module=ON_DUTY_MODULE, subject_type=ON_DUTY_SUBJECT, subject_id=row.id,
                           reason="On-duty request cancelled")
    before = on_duty_content(row)
    row.status, row.cancelled_at = "CANCELLED", utcnow()
    row.row_version += 1
    db.flush()
    log_change(db, row.company_id, record_type="on_duty", record_id=row.id, change="VOIDED",
               content=on_duty_content(row), employee_id=row.employee_id, date_from=row.date,
               actor_user_id=principal.user_id)
    _audit(db, principal, "ON_DUTY_CANCELLED", f"Cancelled on-duty request #{row.id}", "on_duty_request", row.id,
           before=before, after=on_duty_content(row))
    return on_duty_json(row)


def list_on_duty(db: Session, principal: Principal, *, employee_id: Optional[int] = None, start: Optional[date] = None,
                 end: Optional[date] = None, status: Optional[str] = None, limit: int = 200) -> List[dict]:
    if start is not None and end is not None and end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    query = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == principal.company_id,
                                           *_range_filter(OnDutyRequest.date, OnDutyRequest.date, start, end))
    if principal.has("attendance:read"):
        scope = authz.employee_scope(db, principal, "attendance:read")
        query = query.filter(scope.clause(OnDutyRequest.employee_id))
    elif principal.employee_id is not None and principal.has("attendance:mark"):
        query = query.filter(OnDutyRequest.employee_id == principal.employee_id)
    else:
        raise forbidden()
    if employee_id is not None:
        query = query.filter(OnDutyRequest.employee_id == employee_id)
    if status:
        query = query.filter(OnDutyRequest.status == status)
    return [on_duty_json(r) for r in query.order_by(OnDutyRequest.date.desc(), OnDutyRequest.id.desc()).limit(limit)]


# ---------------------------------------------------------------------------
# Optional holidays (EC-HOL-05)
# ---------------------------------------------------------------------------
def optional_quota(db: Session, company_id: int, employee_id: int, on: date) -> int:
    from app.payroll.services import settings as settings_engine
    from app.payroll.settings_registry import UNSET

    value = settings_engine.resolve(db, company_id, "holiday.optional_quota_per_year", employee_id=employee_id, on=on).value
    return 0 if value is UNSET or value is None else int(value)


def _optional_holidays_for(db: Session, company_id: int, employee_id: int, year: int) -> List[Holiday]:
    start, end = date(year, 1, 1), date(year, 12, 31)
    seen: Dict[int, Holiday] = {}
    for fact in day_facts(db, company_id, employee_id, start, end):
        if fact.holiday is None:
            continue
        found = (holiday_service.holidays_by_calendar_date(db, company_id, {fact.calendar_id: [fact.date]})
                 if fact.calendar_id else
                 holiday_service.holidays_by_branch_date(db, company_id, {fact.branch_id: [fact.date]}))
        for holiday in found.get(fact.date, []):
            if holiday.holiday_type == "OPTIONAL":
                seen[holiday.id] = holiday
    return sorted(seen.values(), key=lambda h: (h.date, h.id))


def optional_holidays(db: Session, principal: Principal, *, year: int, employee_id: Optional[int] = None) -> dict:
    if employee_id is None or authz.is_self(principal, employee_id):
        if not principal.has("holiday:read"):
            raise forbidden()
        employee = authz.own_employee(db, principal)
    else:
        if not principal.has_company_scope("holiday:manage"):
            raise forbidden()
        employee = _employee(db, principal.company_id, employee_id)
    available = _optional_holidays_for(db, principal.company_id, employee.id, year)
    chosen = {r.holiday_id: r for r in db.query(EmployeeOptionalHoliday).filter(
        EmployeeOptionalHoliday.company_id == principal.company_id, EmployeeOptionalHoliday.employee_id == employee.id,
        EmployeeOptionalHoliday.year == year, EmployeeOptionalHoliday.status == "CHOSEN")}
    quota = optional_quota(db, principal.company_id, employee.id, date(year, 12, 31))
    return {
        "employee_id": employee.id, "year": year, "quota": quota, "used": len(chosen),
        "remaining": max(quota - len(chosen), 0),
        "holidays": [holiday_service.serialize(h) | {"chosen": h.id in chosen} for h in available],
    }


def choose_optional_holiday(db: Session, principal: Principal, holiday_id: int) -> dict:
    if not principal.has("holiday:read"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    holiday = db.query(Holiday).filter(Holiday.company_id == principal.company_id, Holiday.id == holiday_id).first()
    if holiday is None or holiday.holiday_type != "OPTIONAL":
        raise not_found("Optional holiday")
    if holiday.id not in {h.id for h in _optional_holidays_for(db, principal.company_id, employee.id, holiday.date.year)}:
        raise not_found("Optional holiday")
    if holiday.date < clock.company_today(db, principal.company_id):
        raise conflict("Optional holidays are chosen before the day.", "HOLIDAY_IN_PAST")
    attendance_service.lock_employee(db, employee.id)
    rows = db.query(EmployeeOptionalHoliday).filter(
        EmployeeOptionalHoliday.company_id == principal.company_id, EmployeeOptionalHoliday.employee_id == employee.id,
        EmployeeOptionalHoliday.year == holiday.date.year, EmployeeOptionalHoliday.status == "CHOSEN").all()
    if any(r.holiday_id == holiday.id for r in rows):
        raise conflict("You already chose this holiday.", "ALREADY_CHOSEN")
    quota = optional_quota(db, principal.company_id, employee.id, holiday.date)
    if len(rows) >= quota:
        raise conflict(f"You have used all {quota} optional holidays of {holiday.date.year}; take leave instead.",
                       "OPTIONAL_QUOTA_EXCEEDED")
    row = EmployeeOptionalHoliday(company_id=principal.company_id, employee_id=employee.id, holiday_id=holiday.id,
                                  year=holiday.date.year, status="CHOSEN", chosen_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    log_change(db, row.company_id, record_type="optional_holiday", record_id=row.id, change="CREATED",
               content={"holiday_id": holiday.id, "employee_id": employee.id, "status": row.status},
               employee_id=employee.id, date_from=holiday.date, actor_user_id=principal.user_id)
    reevaluate_leave_days(db, principal.company_id, [holiday.date], actor_user_id=principal.user_id)
    AuditLogService.log(db, principal, "Holidays", "OPTIONAL_CHOSEN", f"Chose optional holiday '{holiday.name}'",
                        "employee_optional_holiday", row.id, details={"holiday_id": holiday.id, "year": row.year})
    return {"id": row.id, "holiday_id": holiday.id, "year": row.year, "status": row.status}


def cancel_optional_holiday(db: Session, principal: Principal, holiday_id: int) -> dict:
    if not principal.has("holiday:read"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    row = db.query(EmployeeOptionalHoliday).filter(
        EmployeeOptionalHoliday.company_id == principal.company_id, EmployeeOptionalHoliday.employee_id == employee.id,
        EmployeeOptionalHoliday.holiday_id == holiday_id, EmployeeOptionalHoliday.status == "CHOSEN").with_for_update().first()
    if row is None:
        raise not_found("Optional holiday choice")
    holiday = db.get(Holiday, holiday_id)
    if holiday.date < clock.company_today(db, principal.company_id):
        raise conflict("A past optional holiday stays as taken.", "HOLIDAY_IN_PAST")
    row.status, row.cancelled_at = "CANCELLED", utcnow()
    db.flush()
    log_change(db, row.company_id, record_type="optional_holiday", record_id=row.id, change="VOIDED",
               content={"holiday_id": holiday_id, "employee_id": employee.id, "status": row.status},
               employee_id=employee.id, date_from=holiday.date, actor_user_id=principal.user_id)
    reevaluate_leave_days(db, principal.company_id, [holiday.date], actor_user_id=principal.user_id)
    AuditLogService.log(db, principal, "Holidays", "OPTIONAL_CANCELLED", f"Withdrew optional holiday '{holiday.name}'",
                        "employee_optional_holiday", row.id, details={"holiday_id": holiday_id})
    return {"id": row.id, "holiday_id": holiday_id, "year": row.year, "status": row.status}


# ---------------------------------------------------------------------------
# Day-ledger preview
# ---------------------------------------------------------------------------
def _preview_config(db: Session, company_id: int, employee_id: int, on: date) -> ledger.LedgerConfig:
    """Platform-default ledger config overlaid with the employee's resolved switches (no policy fields)."""
    from app.payroll.services import settings as settings_engine
    from app.payroll.settings_registry import UNSET

    keys = ("payroll.attendance.enabled", "payroll.leave.enabled", "payroll.leave.when_disabled_treat_approved_leave_as",
            "payroll.attendance.half_day_value", "payroll.lop.sandwich_rule", "payroll.lop.zero_paid_days")
    try:
        values = {k: r.value for k, r in settings_engine.resolve_many(
            db, company_id, keys, employee_id=employee_id, on=on, strict=False).items()}
    except Exception:
        values = {}

    def pick(key: str, default: Any) -> Any:
        value = values.get(key, UNSET)
        return default if value is UNSET or value is None else value

    return ledger.LedgerConfig(
        attendance_enabled=bool(pick("payroll.attendance.enabled", False)),
        leave_enabled=bool(pick("payroll.leave.enabled", False)),
        when_disabled_treat_approved_leave_as=pick("payroll.leave.when_disabled_treat_approved_leave_as", "PAID_EXCUSED"),
        half_day_value=Decimal(str(pick("payroll.attendance.half_day_value", "0.5"))),
        sandwich_rule=pick("payroll.lop.sandwich_rule", "OFF"),
        zero_paid_days=pick("payroll.lop.zero_paid_days", "EXCEPTION"),
    )


def day_ledger_preview(db: Session, principal: Principal, employee_id: int, start: date, end: date) -> dict:
    """The ledger inputs of a range and the ledger they produce with the employee's switches (3.4 §4.4)."""
    if not principal.has(INPUTS_READ):
        raise forbidden()
    _dates(start, end)
    _ensure_in_scope(db, principal, INPUTS_READ, employee_id, start, end)
    from app.payroll.engine.snapshot import day_to_dict

    days = ledger_inputs(db, principal.company_id, employee_id, start, end)
    cfg = _preview_config(db, principal.company_id, employee_id, end)
    result = ledger.build_ledger(days, cfg)
    return {"employee_id": employee_id, "start": start.isoformat(), "end": end.isoformat(),
            "inputs": [day_to_dict(d) for d in days], "ledger": result.to_dict(),
            "pending": pending_inputs(db, principal.company_id, employee_id, start, end),
            "config": {"attendance_enabled": cfg.attendance_enabled, "leave_enabled": cfg.leave_enabled}}
