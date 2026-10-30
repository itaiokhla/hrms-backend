"""Effective-dated work-calendar placement: branch links, criteria rows and the work-week resolver (LH-M1 WI-1.8).

Owner docs: 04-holiday/01 §4.1, §4.3 (branch links), 04-holiday/03 §5.4 (work-week order), SPINE §7 step 3.

Work-week order for an employee-local date D, against the primary placement valid on D (else the nearest, F-04):

1. a published roster entry when the employee is roster-based (``holiday_rosters.provider()``; none before AS-M5);
2. ``employee_assignments.work_week_pattern_id`` (an explicit employee-level pattern);
3. the highest-priority ``holiday_calendar_assignments`` row with a pattern whose criteria match on D;
4. the ``branch_calendar_links`` row of the branch valid on D (a branch without link rows: its current column);
5. ``legal_entities.default_work_week_pattern_id``;
6. the country weekend of the entity; 7. ``MON_FRI``.

The rules of a pattern on D come from the version valid on D (``work_week_pattern_versions``), never from the
mirror columns, so activation timing never changes a day fact (LH-WWK-24).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pytz
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.company import Company
from app.models.employee import Employee
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import (
    BranchCalendarLink,
    EmploymentType,
    HolidayCalendar,
    HolidayCalendarAssignment,
    WorkWeekPattern,
    WorkWeekPatternVersion,
)
from app.payroll import countries, workweek
from app.payroll.services import jobs, outbox
from app.services import holiday_rosters
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

AUDIT_MODULE = "Holidays"
MIGRATED_FROM = date(2000, 1, 1)
CRITERIA_KEYS = {
    "legal_entity_ids": "legal_entity_id", "branch_ids": "branch_id", "pay_group_ids": "pay_group_id",
    "department_ids": "department_id", "grade_ids": "grade_id", "designation_ids": "designation_id",
    "employee_ids": "employee_id",
}
_ONE_DAY = timedelta(days=1)
CRITERION_CHUNK_DAYS = 31  # an unprimed ``shift_ids`` lookup loads this many dates of the employee at once


# ---------------------------------------------------------------------------------------------- clocks
def zone_today(zone: Optional[str]) -> date:
    try:
        tz = pytz.timezone(zone or "UTC")
    except pytz.UnknownTimeZoneError:
        tz = pytz.utc
    return clock.utcnow().astimezone(tz).date()


def branch_local_today(db: Session, branch: Branch) -> date:
    zone = branch.timezone
    if not zone:
        entity = db.get(LegalEntity, branch.legal_entity_id)
        zone = entity.timezone if entity is not None else None
    if not zone:
        company = db.get(Company, branch.company_id)
        zone = company.timezone if company is not None else None
    return zone_today(zone)


# ---------------------------------------------------------------------------------------------- resolver
@dataclass(frozen=True)
class ResolvedWeek:
    """The work week of one employee-local date (SPINE §7 fields ``work_source``, ``pattern_version``)."""

    pattern_id: Optional[int]
    version: Optional[int]
    pattern: workweek.WorkWeekPattern
    source: str  # ROSTER | ASSIGNMENT | CRITERIA | BRANCH_LINK | ENTITY | COUNTRY | DEFAULT
    work_source: str = "PATTERN"  # PATTERN | ROSTER | ROSTER_UNPUBLISHED
    roster_off: Optional[bool] = None  # a published roster entry: True = OFF (a full rest day), False = WORK

    def portion(self, day: date) -> Optional[str]:
        """Weekly-off portion of ``day``: the roster entry when one is published, else the pattern version."""
        if self.roster_off is not None:
            return workweek.FULL if self.roster_off else None
        return workweek.weekly_off_portion(self.pattern, day)

    def rest_day(self, day: date) -> bool:
        if self.roster_off is not None:
            return self.roster_off
        return workweek.is_rest_day(self.pattern, day)


def pattern_from_row(row: WorkWeekPattern) -> workweek.WorkWeekPattern:
    """The mirror columns as an engine value (legacy readers; versions are preferred)."""
    rules: Dict[str, Any]
    if row.kind == workweek.CYCLE:
        rules = {"kind": workweek.CYCLE, "cycle_length_days": row.cycle_length_days,
                 "anchor_date": row.anchor_date.isoformat() if row.anchor_date else None,
                 "cycle_off_days": list(row.cycle_off_days or [])}
    else:
        rules = {"weekly_off_days": list(row.weekly_off_days or []), "nth_weekday_offs": list(row.nth_weekday_offs or [])}
    if row.half_day_offs:
        rules["kind"] = row.kind
        rules["half_day_offs"] = list(row.half_day_offs)
    if row.rest_days:
        rules["kind"] = row.kind
        rules["rest_days"] = list(row.rest_days)
    return workweek.WorkWeekPattern.from_json(rules)


class WorkWeekResolver:
    """Per-request cache of patterns, versions, links and criteria rows of one tenant."""

    def __init__(self, db: Session, company_id: int):
        self.db, self.company_id = db, company_id
        self._versions: Dict[int, List[Tuple[workweek.PatternVersion, int]]] = {}
        self._links: Dict[int, List[BranchCalendarLink]] = {}
        self._branches: Dict[int, Optional[Branch]] = {}
        self._entities: Dict[int, Optional[LegalEntity]] = {}
        self._criteria: Optional[List[HolidayCalendarAssignment]] = None
        self._employees: Dict[int, Optional[Employee]] = {}
        self._employment_codes: Optional[Dict[int, str]] = None
        # ``shift_ids`` criterion (§4.4.1, EC AS-INT-07): {(employee_id, date): shift_id | None}, loaded lazily — once
        # for a primed (employees, range) batch, else per employee in CRITERION_CHUNK_DAYS chunks.
        self._criterion_shifts: Dict[Tuple[int, date], Optional[int]] = {}
        self._criterion_window: Optional[Tuple[frozenset, date, date]] = None

    # ---- shift_ids criterion
    def prime_shift_criterion(self, employee_ids: Iterable[int], start: date, end: date) -> None:
        """Day-facts batches (``time_inputs._Batch``): the first ``shift_ids`` match inside ``start..end`` loads the
        criterion shifts of every employee of the batch with one ``criterion_shifts_many`` call. Nothing is read
        when no criteria row uses ``shift_ids``."""
        self._criterion_window = (frozenset(employee_ids), start, end)

    def criterion_shift(self, employee_id: int, day: date) -> Optional[int]:
        """The shift the ``shift_ids`` criterion matches for (E, D): ``shift_resolution.criterion_shifts_many``
        (the resolved shift; a HOLIDAY_RULE OFF entry on the shift it replaced, ``planned_shift_id``; any other OFF
        entry or no shift → None)."""
        key = (employee_id, day)
        if key not in self._criterion_shifts:
            from app.services import shift_resolution

            window = self._criterion_window
            if window is not None and employee_id in window[0] and window[1] <= day <= window[2]:
                ids, start, end = sorted(window[0]), window[1], window[2]
                if len(ids) * ((end - start).days + 1) > shift_resolution.MAX_EMPLOYEE_DAYS:
                    ids = [employee_id]  # a batch above the resolver cap loads one employee at a time
            else:
                ids, start, end = [employee_id], day, day + timedelta(days=CRITERION_CHUNK_DAYS - 1)
            self._criterion_shifts.update(
                shift_resolution.criterion_shifts_many(self.db, self.company_id, ids, start, end))
        return self._criterion_shifts.get(key)

    def _criterion_shift_of(self, assignment: EmployeeAssignment, day: date) -> Optional[int]:
        """A date the placement covers matches on the resolved criterion shift; a date outside it (the nearest
        placement of F-04, outside employment) keeps the placement's own shift as before."""
        if assignment.effective_from <= day and (assignment.effective_to is None or day <= assignment.effective_to):
            try:
                return self.criterion_shift(assignment.employee_id, day)
            except AppError:  # shift data that cannot be resolved (range, version gaps): the placement's shift
                logger.warning("shift_ids criterion unresolved for employee %s on %s", assignment.employee_id, day)
        return assignment.shift_id

    # ---- loaders
    def branch(self, branch_id: Optional[int]) -> Optional[Branch]:
        if branch_id is None:
            return None
        if branch_id not in self._branches:
            self._branches[branch_id] = self.db.get(Branch, branch_id)
        return self._branches[branch_id]

    def entity(self, entity_id: Optional[int]) -> Optional[LegalEntity]:
        if entity_id is None:
            return None
        if entity_id not in self._entities:
            self._entities[entity_id] = self.db.get(LegalEntity, entity_id)
        return self._entities[entity_id]

    def versions(self, pattern_id: int) -> List[Tuple[workweek.PatternVersion, int]]:
        """(version, row id) sorted by effective_from; a pattern without rows gets its mirror as version 0."""
        if pattern_id not in self._versions:
            rows = (self.db.query(WorkWeekPatternVersion)
                    .filter(WorkWeekPatternVersion.company_id == self.company_id,
                            WorkWeekPatternVersion.pattern_id == pattern_id)
                    .order_by(WorkWeekPatternVersion.effective_from).all())
            out = [(workweek.PatternVersion(r.version, r.effective_from, r.effective_to,
                                            workweek.WorkWeekPattern.from_json(r.rules)), r.id) for r in rows]
            if not out:
                row = self.db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == self.company_id,
                                                            WorkWeekPattern.id == pattern_id).one()
                out = [(workweek.PatternVersion(0, MIGRATED_FROM, None, pattern_from_row(row)), 0)]
            self._versions[pattern_id] = out
        return self._versions[pattern_id]

    def version_on(self, pattern_id: int, day: date) -> workweek.PatternVersion:
        """The version valid on ``day``; before the first version, the first one (nearest)."""
        versions = self.versions(pattern_id)
        found = workweek.version_on([v for v, _ in versions], day)
        return found or versions[0][0]

    def links(self, branch_id: int) -> List[BranchCalendarLink]:
        if branch_id not in self._links:
            self._links[branch_id] = (self.db.query(BranchCalendarLink)
                                      .filter(BranchCalendarLink.company_id == self.company_id,
                                              BranchCalendarLink.branch_id == branch_id)
                                      .order_by(BranchCalendarLink.effective_from).all())
        return self._links[branch_id]

    def branch_pattern_on(self, branch_id: Optional[int], day: date) -> Optional[int]:
        if branch_id is None:
            return None
        rows = self.links(branch_id)
        if not rows:
            branch = self.branch(branch_id)
            return branch.work_week_pattern_id if branch is not None else None
        for row in rows:
            if row.effective_from <= day and (row.effective_to is None or day <= row.effective_to):
                return row.work_week_pattern_id
        return None

    def branch_calendar_on(self, branch_id: Optional[int], day: date) -> Optional[int]:
        """The holiday calendar of the branch link valid on ``day`` (a branch without links: its column)."""
        if branch_id is None:
            return None
        rows = self.links(branch_id)
        if not rows:
            branch = self.branch(branch_id)
            return branch.holiday_calendar_id if branch is not None else None
        for row in rows:
            if row.effective_from <= day and (row.effective_to is None or day <= row.effective_to):
                return row.holiday_calendar_id
        return None

    def criteria_rows(self, *, calendars: bool = False) -> List[HolidayCalendarAssignment]:
        """Criteria rows with a pattern (default) or with a calendar, lowest priority number first."""
        if self._criteria is None:
            self._criteria = (self.db.query(HolidayCalendarAssignment)
                              .filter(HolidayCalendarAssignment.company_id == self.company_id)
                              .order_by(HolidayCalendarAssignment.priority, HolidayCalendarAssignment.id).all())
        if calendars:
            return [r for r in self._criteria if r.calendar_id is not None]
        return [r for r in self._criteria if r.work_week_pattern_id is not None]

    def _employment_code(self, type_id: Optional[int]) -> Optional[str]:
        if self._employment_codes is None:
            self._employment_codes = dict(self.db.query(EmploymentType.id, EmploymentType.code)
                                          .filter(EmploymentType.company_id == self.company_id))
        return self._employment_codes.get(type_id) if type_id else None

    def _employee(self, employee_id: int) -> Optional[Employee]:
        if employee_id not in self._employees:
            self._employees[employee_id] = self.db.get(Employee, employee_id)
        return self._employees[employee_id]

    def _teams(self, employee_id: int, day: date) -> Dict[int, str]:
        """{team_id: member_role} of the employee on ``day`` (X-M1 ``team_service.team_attributes``); {} without
        teams."""
        key = (employee_id, day)
        cache = self.__dict__.setdefault("_team_cache", {})
        if key not in cache:
            try:
                from app.services import team_service

                cache[key] = team_service.team_attributes(self.db, self.company_id, [employee_id], day).get(
                    employee_id, {})
            except Exception:  # noqa: BLE001 - teams not installed: team dimensions match nothing
                cache[key] = {}
        return cache[key]

    def _team_match(self, criteria: Mapping[str, Any], assignment: EmployeeAssignment, day: date) -> bool:
        """``team_ids`` / ``team_member_roles`` (AssignmentCriteria, X-M1; same semantics as shift criteria): a
        membership in one of the teams (any team when only roles are given) whose role is listed (any role when only
        teams are given)."""
        team_ids = criteria.get("team_ids")
        roles = criteria.get("team_member_roles")
        teams = self._teams(assignment.employee_id, day)
        candidates = {t: r for t, r in teams.items() if not team_ids or t in team_ids}
        if not candidates:
            return False
        return not roles or any(role in roles for role in candidates.values())

    def matches(self, criteria: Mapping[str, Any], assignment: EmployeeAssignment, day: Optional[date] = None) -> bool:
        """Every given dimension matches as of ``day`` (any listed value, §4.4, §4.4.1); team dimensions use the
        employee's team memberships on the date (X-M1); unknown dimensions never match (safe default)."""
        if not criteria:
            return False
        day = day or assignment.effective_from
        if ("team_ids" in criteria or "team_member_roles" in criteria) and not self._team_match(criteria, assignment,
                                                                                               day):
            return False
        for key, values in criteria.items():
            if key in ("team_ids", "team_member_roles"):
                continue
            if key in CRITERIA_KEYS:
                if getattr(assignment, CRITERIA_KEYS[key]) not in (values or []):
                    return False
            elif key == "employment_types":
                if self._employment_code(assignment.employment_type_id) not in (values or []):
                    return False
            elif key == "shift_ids":
                # The resolved shift of the date (§4.4.1): a HOLIDAY_RULE OFF entry matches on the shift it replaced,
                # so publishing it never removes its own holiday (EC AS-INT-07); MANUAL / DAY_OVERRIDE OFF never match.
                if self._criterion_shift_of(assignment, day) not in (values or []):
                    return False
            elif key in ("hired_on_or_after", "hired_on_or_before", "tenure_months_min", "tenure_months_max"):
                hired = getattr(self._employee(assignment.employee_id), "hire_date", None)
                if hired is None:
                    return False
                if key == "hired_on_or_after" and hired < _as_date(values):
                    return False
                if key == "hired_on_or_before" and hired > _as_date(values):
                    return False
                months = (day.year - hired.year) * 12 + day.month - hired.month - (1 if day.day < hired.day else 0)
                if key == "tenure_months_min" and months < int(values):
                    return False
                if key == "tenure_months_max" and months > int(values):
                    return False
            elif key == "in_probation":
                end = getattr(self._employee(assignment.employee_id), "probation_end_date", None)
                if bool(values) != (end is not None and day <= end):
                    return False
            else:
                return False
        return True

    def criteria_calendar_on(self, assignment: EmployeeAssignment, day: date) -> Optional[int]:
        for row in self.criteria_rows(calendars=True):
            if row.effective_from <= day and (row.effective_to is None or day <= row.effective_to) \
                    and self.matches(row.criteria or {}, assignment, day):
                return row.calendar_id
        return None

    def calendar_on(self, assignment: Optional[EmployeeAssignment], day: date) -> Tuple[Optional[int], str]:
        """Holiday calendar of one date (04-holiday/01 §4.1): employee override, then the highest-priority criteria
        row, then the branch link valid on the date (a branch without links: its column); else none (legacy
        location rule)."""
        if assignment is None:
            return None, "NONE"
        if assignment.holiday_calendar_id:
            return assignment.holiday_calendar_id, "OVERRIDE"
        found = self.criteria_calendar_on(assignment, day)
        if found:
            return found, "CRITERIA"
        found = self.branch_calendar_on(assignment.branch_id, day)
        if found:
            return found, "BRANCH_LINK"
        return None, "NONE"

    def criteria_pattern_on(self, assignment: EmployeeAssignment, day: date) -> Optional[int]:
        for row in self.criteria_rows():
            if row.effective_from <= day and (row.effective_to is None or day <= row.effective_to) \
                    and self.matches(row.criteria or {}, assignment, day):
                return row.work_week_pattern_id
        return None

    # ---- resolution
    def resolve(self, assignment: Optional[EmployeeAssignment], day: date,
                employee_id: Optional[int] = None) -> ResolvedWeek:
        work_source = "PATTERN"
        subject = employee_id or (assignment.employee_id if assignment is not None else None)
        if subject is not None:
            roster = holiday_rosters.provider()
            if roster.is_roster_based(self.db, self.company_id, subject, day):
                entry = roster.entries(self.db, self.company_id, subject, day, day).get(day)
                if entry is not None:
                    base = self._pattern_side(assignment, day, "ROSTER_UNPUBLISHED")
                    return ResolvedWeek(base.pattern_id, base.version, base.pattern, "ROSTER", "ROSTER",
                                        entry.kind == "OFF")
                work_source = "ROSTER_UNPUBLISHED"
        return self._pattern_side(assignment, day, work_source)

    def _pattern_side(self, assignment: Optional[EmployeeAssignment], day: date, work_source: str) -> ResolvedWeek:
        if assignment is None:
            return ResolvedWeek(None, None, workweek.MON_FRI, "DEFAULT", work_source)
        for source, pattern_id in (
            ("ASSIGNMENT", assignment.work_week_pattern_id),
            ("CRITERIA", None),
            ("BRANCH_LINK", None),
            ("ENTITY", None),
        ):
            if source == "CRITERIA":
                pattern_id = self.criteria_pattern_on(assignment, day)
            elif source == "BRANCH_LINK":
                pattern_id = self.branch_pattern_on(assignment.branch_id, day)
            elif source == "ENTITY":
                entity = self.entity(assignment.legal_entity_id)
                pattern_id = getattr(entity, "default_work_week_pattern_id", None)
            if pattern_id:
                version = self.version_on(pattern_id, day)
                return ResolvedWeek(pattern_id, version.version or None, version.pattern, source, work_source)
        entity = self.entity(assignment.legal_entity_id)
        if entity is not None:
            try:
                weekend = frozenset(countries.default_weekend(entity.country_code))
                return ResolvedWeek(None, None, workweek.WorkWeekPattern(weekend), "COUNTRY", work_source)
            except Exception:  # noqa: BLE001 - unknown country data: fall back like today
                pass
        return ResolvedWeek(None, None, workweek.MON_FRI, "DEFAULT", work_source)


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


# The placement resolver serves the work week (§5.4) and the holiday calendar (04-holiday/01 §4.1).
PlacementResolver = WorkWeekResolver


@dataclass(frozen=True)
class ResolvedPlacement:
    calendar_id: Optional[int]
    calendar_source: str  # OVERRIDE | CRITERIA | BRANCH_LINK | NONE
    pattern_id: Optional[int]
    pattern_source: str
    assignment_id: Optional[int]


def resolve(db: Session, company_id: int, employee_id: int, day: date) -> ResolvedPlacement:
    """Calendar and work week of one employee-local date (resolver steps 2-3, SPINE §7, V3-063)."""
    from app.payroll.services.time_inputs import _covering, _nearest, primary_assignments

    rows = primary_assignments(db, company_id, employee_id)
    assignment = _covering(rows, day) or _nearest(rows, day)
    resolver = WorkWeekResolver(db, company_id)
    calendar_id, calendar_source = resolver.calendar_on(assignment, day)
    week = resolver.resolve(assignment, day, employee_id)
    return ResolvedPlacement(calendar_id, calendar_source, week.pattern_id, week.source,
                             assignment.id if assignment is not None else None)


# ---------------------------------------------------------------------------------------------- scope helpers
def covers_branch(principal: Principal, code: str, branch: Branch) -> bool:
    """``code`` held at COMPANY, or at LEGAL_ENTITY of the branch's entity, or at BRANCH for the branch."""
    if principal.is_company_admin or principal.has_company_scope(code):
        return True
    return (branch.legal_entity_id in principal.scope_targets(code, "LEGAL_ENTITY")
            or branch.id in principal.scope_targets(code, "BRANCH"))


def _pattern_usable(db: Session, company_id: int, pattern_id: int, entity_id: int) -> WorkWeekPattern:
    row = db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == company_id,
                                           WorkWeekPattern.id == int(pattern_id)).first()
    if row is None:
        raise not_found("Work week")
    if row.status != "ACTIVE":
        raise bad_request(f"Work week {row.code} is inactive.", "ORG_DIMENSION_INACTIVE")
    if row.legal_entity_id is not None and row.legal_entity_id != entity_id:
        raise AppError(422, f"Work week {row.code} belongs to another legal entity.", "PATTERN_ENTITY_MISMATCH")
    return row


def _calendar_usable(db: Session, company_id: int, calendar_id: int) -> HolidayCalendar:
    row = db.query(HolidayCalendar).filter(HolidayCalendar.company_id == company_id,
                                           HolidayCalendar.id == int(calendar_id)).first()
    if row is None:
        raise not_found("Holiday calendar")
    if row.status != "ACTIVE":
        raise bad_request(f"Holiday calendar {row.code} is inactive.", "ORG_DIMENSION_INACTIVE")
    return row


# ---------------------------------------------------------------------------------------------- branch links
def serialize_link(row: BranchCalendarLink) -> dict:
    return {"id": row.id, "branch_id": row.branch_id, "holiday_calendar_id": row.holiday_calendar_id,
            "work_week_pattern_id": row.work_week_pattern_id, "effective_from": row.effective_from.isoformat(),
            "effective_to": row.effective_to.isoformat() if row.effective_to else None, "reason": row.reason,
            "row_version": row.row_version}


def _ensure_history(db: Session, branch: Branch, links: List[BranchCalendarLink], before: date) -> None:
    """A branch without link rows gets one from 2000-01-01 holding today's columns, so earlier dates keep them."""
    if links or before <= MIGRATED_FROM:
        return
    if branch.holiday_calendar_id is None and branch.work_week_pattern_id is None:
        return
    row = BranchCalendarLink(company_id=branch.company_id, branch_id=branch.id,
                             holiday_calendar_id=branch.holiday_calendar_id,
                             work_week_pattern_id=branch.work_week_pattern_id, effective_from=MIGRATED_FROM,
                             effective_to=None, reason="Link history before the first effective-dated change",
                             row_version=1)
    db.add(row)
    db.flush()
    links.append(row)


def link_clock(db: Session, branch: Branch, pattern_ids: Iterable[Optional[int]],
               calendar_ids: Iterable[Optional[int]] = ()) -> date:
    """T_link (04-holiday/01 §4.3 step 2): the furthest-ahead of the branch's local today, ``calendar_local_today``
    of the old and the new calendar and the local today of the old and the new pattern."""
    from app.services import holiday_calendar_service, holiday_work_weeks  # local: both import this module

    today = branch_local_today(db, branch)
    for pattern_id in pattern_ids:
        if pattern_id:
            today = max(today, holiday_work_weeks.pattern_local_today(db, branch.company_id, pattern_id))
    for calendar_id in calendar_ids:
        if calendar_id:
            today = max(today, holiday_calendar_service.calendar_local_today(db, branch.company_id, calendar_id))
    return today


def set_branch_link(db: Session, principal: Optional[Principal], branch_id: int, data: Mapping[str, Any], *,
                    expected_row_version: Optional[int] = None, legacy: bool = False) -> dict:
    """PUT /api/v1/branches/{id}/calendar-link (04-holiday/01 §4.3): close, insert, mirror, signal, recount."""
    from app.payroll.services import time_inputs
    from app.services import holiday_work_weeks, leave_jobs

    company_id = principal.company_id if principal is not None else None
    branch = db.query(Branch).filter(Branch.company_id == company_id, Branch.id == int(branch_id)) \
        .with_for_update().first()
    if branch is None or (principal is not None and not covers_branch(principal, "holiday:manage", branch)):
        raise not_found("Branch")
    links = (db.query(BranchCalendarLink).filter(BranchCalendarLink.company_id == company_id,
                                                 BranchCalendarLink.branch_id == branch.id)
             .order_by(BranchCalendarLink.effective_from).all())
    resolver = WorkWeekResolver(db, company_id)
    new_pattern = data.get("work_week_pattern_id")
    if "work_week_pattern_id" in data and new_pattern is None or "holiday_calendar_id" in data and \
            data.get("holiday_calendar_id") is None:
        raise bad_request("A branch always needs a holiday calendar and a work week.", "ORG_BRANCH_LINK_REQUIRED")
    if new_pattern is not None:
        _pattern_usable(db, company_id, new_pattern, branch.legal_entity_id)
    new_calendar = data.get("holiday_calendar_id")
    if new_calendar is not None:
        calendar = _calendar_usable(db, company_id, new_calendar)
        if calendar.legal_entity_id is not None and calendar.legal_entity_id != branch.legal_entity_id:
            raise AppError(422, f"Holiday calendar {calendar.code} belongs to another legal entity.",
                           "CALENDAR_ENTITY_MISMATCH")

    old_pattern_today = resolver.branch_pattern_on(branch.id, branch_local_today(db, branch))
    old_calendar_today = resolver.branch_calendar_on(branch.id, branch_local_today(db, branch))
    t_link = link_clock(db, branch, (old_pattern_today, new_pattern), (old_calendar_today, new_calendar))
    effective_from = data.get("effective_from") or t_link
    # The legacy alias never backdates; but a branch's *first* link through it is its initial setup, which applied
    # to every date before links were effective-dated (same rule as the migration backfill, zero diff).
    first_legacy_link = legacy and not links
    if legacy:
        effective_from = MIGRATED_FROM if first_legacy_link else t_link
    recount_from = branch_local_today(db, branch) if first_legacy_link else effective_from
    reason = (data.get("reason") or "").strip() or None
    if effective_from < t_link and not first_legacy_link:
        if principal is not None and not covers_branch(principal, "holiday:publish", branch):
            raise forbidden("Backdated calendar links need the 'Publish and backdate holidays' permission.")
        if not reason:
            raise bad_request("Give a reason for a backdated change.", "REASON_REQUIRED")
        limit = t_link - timedelta(days=holiday_work_weeks.backdate_days(db, company_id))
        if effective_from < limit:
            raise AppError(422, f"A change can be backdated to {limit.isoformat()} at the earliest.", "BACKDATE_LIMIT")
    _ensure_history(db, branch, links, effective_from)
    covering = next((r for r in links if r.effective_from <= effective_from
                     and (r.effective_to is None or effective_from <= r.effective_to)), None)
    if expected_row_version is not None and covering is not None and covering.row_version != expected_row_version:
        raise conflict("This link was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    calendar_id = data.get("holiday_calendar_id") or (covering.holiday_calendar_id if covering else
                                                       branch.holiday_calendar_id)
    pattern_id = new_pattern or (covering.work_week_pattern_id if covering else branch.work_week_pattern_id)
    if calendar_id is None or pattern_id is None:
        raise bad_request("A branch always needs a holiday calendar and a work week.", "ORG_BRANCH_LINK_REQUIRED")
    if covering is not None and covering.holiday_calendar_id == calendar_id \
            and covering.work_week_pattern_id == pattern_id:
        if legacy:
            return serialize_link(covering)
        raise conflict("The branch already uses this calendar and work week on that date.", "NO_CHANGE")

    before = [serialize_link(r) for r in links]
    removed = [r for r in links if r.effective_from >= effective_from]
    for row in removed:
        db.delete(row)
    db.flush()
    if covering is not None and covering.effective_from < effective_from:
        covering.effective_to = effective_from - _ONE_DAY
        covering.row_version += 1
        db.flush()
    row = BranchCalendarLink(company_id=company_id, branch_id=branch.id, holiday_calendar_id=calendar_id,
                             work_week_pattern_id=pattern_id, effective_from=effective_from, effective_to=None,
                             reason=reason, created_by_user_id=principal.user_id if principal else None,
                             row_version=1)
    db.add(row)
    db.flush()
    if effective_from <= branch_local_today(db, branch):
        branch.holiday_calendar_id, branch.work_week_pattern_id = calendar_id, pattern_id
        db.flush()

    time_inputs.log_change(db, company_id, record_type="calendar_link", record_id=row.id, change="CREATED",
                           content=serialize_link(row), date_from=effective_from, date_to=None,
                           actor_user_id=principal.user_id if principal else None,
                           details={"branch_id": branch.id})
    previous = old_pattern_today if covering is None else covering.work_week_pattern_id
    recount = leave_jobs.recount_pattern_switch(db, company_id, branch_id=branch.id, old_pattern_id=previous,
                                                new_pattern_id=pattern_id, date_from=recount_from,
                                                actor_user_id=principal.user_id if principal else None)
    old_calendar = old_calendar_today if covering is None else covering.holiday_calendar_id
    if old_calendar != calendar_id:
        calendar_recount = leave_jobs.recount_calendar_switch(
            db, company_id, key=f"recalc:link-calendar:{branch.id}:{calendar_id}:{recount_from.isoformat()}",
            old_calendar_id=old_calendar, new_calendar_id=calendar_id, date_from=recount_from,
            actor_user_id=principal.user_id if principal else None)
        recount = {"pattern": recount, "calendar": calendar_recount}
    AuditLogService.log(db, principal, AUDIT_MODULE, "CALENDAR_LINKED",
                        f"Branch {branch.code}: calendar #{calendar_id}, work week #{pattern_id} "
                        f"from {effective_from.isoformat()}", "branch", branch.id,
                        details={"before": before, "after": serialize_link(row), "reason": reason,
                                 "legacy": legacy})
    outbox.emit(db, "calendar.link.changed", {"branch_id": branch.id, "link_id": row.id,
                                              "effective_date": effective_from.isoformat()})
    return {**serialize_link(row), "recount": recount}


# ---------------------------------------------------------------------------------------------- activation job
def activate(db: Session, company_id: int, local_date: date, zone: Optional[str] = None) -> dict:
    """Mirror the links and pattern versions valid on ``local_date`` (idempotent per date and by data)."""
    from app.services import holiday_work_weeks

    branches = db.query(Branch).filter(Branch.company_id == company_id).order_by(Branch.id).all()
    resolver = WorkWeekResolver(db, company_id)
    changed_links = 0
    for branch in branches:
        if zone is not None and (branch.timezone or "") != zone:
            continue
        links = resolver.links(branch.id)
        current = next((r for r in links if r.effective_from <= local_date
                        and (r.effective_to is None or local_date <= r.effective_to)), None)
        if current is None:
            continue
        if (branch.holiday_calendar_id, branch.work_week_pattern_id) != (current.holiday_calendar_id,
                                                                         current.work_week_pattern_id):
            branch.holiday_calendar_id = current.holiday_calendar_id
            branch.work_week_pattern_id = current.work_week_pattern_id
            changed_links += 1
            outbox.emit(db, "calendar.link.changed", {"branch_id": branch.id, "link_id": current.id,
                                                      "effective_date": current.effective_from.isoformat()})
            if current.holiday_calendar_id:
                _notify_link(db, company_id, branch, current)
    activated = holiday_work_weeks.refresh_mirrors(db, company_id)
    db.flush()
    return {"links": changed_links, "patterns": activated}


def _notify_link(db: Session, company_id: int, branch: Branch, link) -> int:
    """LH-NTF-14: on the day a branch link takes effect, the branch's employees hear once "From <date> your holidays
    follow <calendar>" (category holiday.calendar; nothing at save time for a link more than 7 days ahead)."""
    from app.models.notification import Notification
    from app.models.pay_org import HolidayCalendar
    from app.services.notification_service import NotificationService

    calendar = db.get(HolidayCalendar, link.holiday_calendar_id)
    if calendar is None:
        return 0
    title = f"From {link.effective_from.strftime('%d %b %Y').lstrip('0')} your holidays follow {calendar.name}"
    users = {u for (u,) in db.query(Employee.user_id).join(
        EmployeeAssignment, (EmployeeAssignment.company_id == Employee.company_id)
        & (EmployeeAssignment.employee_id == Employee.id)).filter(
        Employee.company_id == company_id, Employee.user_id.isnot(None), EmployeeAssignment.is_primary.is_(True),
        EmployeeAssignment.branch_id == branch.id, EmployeeAssignment.effective_from <= link.effective_from,
        (EmployeeAssignment.effective_to.is_(None)) | (EmployeeAssignment.effective_to >= link.effective_from))}
    sent = 0
    for user_id in sorted(users):
        if db.query(Notification.id).filter(Notification.company_id == company_id, Notification.user_id == user_id,
                                            Notification.title == title).first() is None:
            NotificationService.create(db, user_id, title, f"{branch.name} now uses {calendar.name}.", "info",
                                       "/holidays")
            sent += 1
    return sent


@jobs.register("holiday.link_activate")
def _link_activate_job(ctx: jobs.JobContext) -> dict:
    payload = ctx.payload or {}
    local_date = date.fromisoformat(payload["local_date"]) if payload.get("local_date") else \
        zone_today(payload.get("timezone"))
    return activate(ctx.db, ctx.company_id, local_date, payload.get("timezone"))


# ---------------------------------------------------------------------------------------------- criteria assignments
EXTRA_CRITERIA = ("shift_ids", "team_ids", "team_member_roles", "hired_on_or_after", "hired_on_or_before",
                  "tenure_months_min", "tenure_months_max", "in_probation")
ID_REFERENCES = {"legal_entity_ids": LegalEntity, "branch_ids": Branch}


def validate_criteria(db: Session, company_id: int, criteria: Any) -> Dict[str, Any]:
    """AssignmentCriteria + D-X-09 dimensions (04-holiday/01 §4.4, §4.4.1); ``everyone`` and empty criteria refused."""
    from app.payroll.policy_types import CRITERIA_DIMENSIONS

    if not isinstance(criteria, Mapping) or not criteria:
        raise AppError(422, "Name at least one group the row applies to.", "INVALID_CRITERIA")
    allowed = {name for name, _ in CRITERIA_DIMENSIONS} | set(EXTRA_CRITERIA)
    if criteria.get("everyone"):
        raise AppError(422, "'Everyone' would override every branch link; use a branch link instead.",
                       "INVALID_CRITERIA")
    out: Dict[str, Any] = {}
    for key, value in criteria.items():
        if key == "everyone":
            continue
        if key not in allowed:
            raise AppError(422, f"Unknown criteria key {key}.", "INVALID_CRITERIA")
        if key in ("hired_on_or_after", "hired_on_or_before"):
            try:
                out[key] = _as_date(value).isoformat()
            except ValueError:
                raise AppError(422, f"{key} must be YYYY-MM-DD.", "INVALID_CRITERIA") from None
        elif key in ("tenure_months_min", "tenure_months_max"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AppError(422, f"{key} must be a whole number of months.", "INVALID_CRITERIA")
            out[key] = value
        elif key == "in_probation":
            if not isinstance(value, bool):
                raise AppError(422, "in_probation must be true or false.", "INVALID_CRITERIA")
            out[key] = value
        else:
            if not isinstance(value, list) or not value:
                raise AppError(422, f"{key} must list at least one value.", "INVALID_CRITERIA")
            out[key] = sorted(set(value), key=str)
            model = ID_REFERENCES.get(key)
            if model is not None:
                found = {i for (i,) in db.query(model.id).filter(model.company_id == company_id,
                                                                  model.id.in_(out[key]))}
                missing = [v for v in out[key] if v not in found]
                if missing:
                    raise AppError(422, f"Unknown {key}: {missing}.", "UNKNOWN_REFERENCE")
    if not out:
        raise AppError(422, "Name at least one group the row applies to.", "INVALID_CRITERIA")
    return out


def serialize_assignment(db: Session, row: HolidayCalendarAssignment) -> dict:
    calendar = db.get(HolidayCalendar, row.calendar_id) if row.calendar_id else None
    pattern = db.get(WorkWeekPattern, row.work_week_pattern_id) if row.work_week_pattern_id else None
    return {"id": row.id, "calendar_id": row.calendar_id, "calendar_code": calendar.code if calendar else None,
            "calendar_name": calendar.name if calendar else None, "work_week_pattern_id": row.work_week_pattern_id,
            "work_week_code": pattern.code if pattern else None, "criteria": row.criteria or {},
            "priority": row.priority, "effective_from": row.effective_from.isoformat(),
            "effective_to": row.effective_to.isoformat() if row.effective_to else None,
            "legal_entity_id": row.legal_entity_id, "reason": row.reason, "row_version": row.row_version}


def list_assignments(db: Session, principal: Principal, *, calendar_id: Optional[int] = None,
                     work_week_pattern_id: Optional[int] = None) -> List[dict]:
    from app.services import holiday_calendar_service

    if not holiday_calendar_service.can_read(principal):
        raise forbidden()
    query = db.query(HolidayCalendarAssignment).filter(HolidayCalendarAssignment.company_id == principal.company_id)
    if calendar_id:
        query = query.filter(HolidayCalendarAssignment.calendar_id == calendar_id)
    if work_week_pattern_id:
        query = query.filter(HolidayCalendarAssignment.work_week_pattern_id == work_week_pattern_id)
    return [serialize_assignment(db, r) for r in query.order_by(HolidayCalendarAssignment.priority,
                                                                 HolidayCalendarAssignment.effective_from)]


def _assignment_clock(db: Session, company_id: int, calendar_ids: Iterable[Optional[int]]) -> date:
    from app.services import holiday_calendar_service

    today = max([zone_today(b.timezone) for b in db.query(Branch).filter(Branch.company_id == company_id)
                 if b.timezone] or [clock.company_today(db, company_id)])
    for cal in calendar_ids:
        if cal:
            today = max(today, holiday_calendar_service.calendar_local_today(db, company_id, cal))
    return today


def _priority_conflict(db: Session, company_id: int, row_id: Optional[int], priority: int, start: date,
                       end: Optional[date], has_calendar: bool, has_pattern: bool) -> bool:
    query = db.query(HolidayCalendarAssignment).filter(HolidayCalendarAssignment.company_id == company_id,
                                                       HolidayCalendarAssignment.priority == priority)
    if row_id:
        query = query.filter(HolidayCalendarAssignment.id != row_id)
    for other in query:
        overlaps = (other.effective_to is None or other.effective_to >= start) and (end is None or end >= other.effective_from)
        same_kind = (has_calendar and other.calendar_id is not None) or (has_pattern and other.work_week_pattern_id is not None)
        if overlaps and same_kind:
            return True
    return False


def _first_affected(row: Optional[HolidayCalendarAssignment], start: date, end: Optional[date],
                    criteria: Mapping[str, Any], priority: int, calendar_id: Any, pattern_id: Any) -> date:
    """The first date whose resolution can change: the start of a new row; for an edit, the earlier start when the
    window opens, the people, the priority or the target change, else the day after the earlier end."""
    if row is None:
        return start
    if (row.effective_from != start or row.criteria != criteria or row.priority != priority
            or row.calendar_id != (calendar_id or None) or row.work_week_pattern_id != (pattern_id or None)):
        return min(start, row.effective_from)
    if end != row.effective_to:
        ends = [d for d in (end, row.effective_to) if d is not None]
        return min(ends) + timedelta(days=1)
    return date.max


def save_assignment(db: Session, principal: Principal, data: Mapping[str, Any], assignment_id: Optional[int] = None, *,
                    expected_row_version: Optional[int] = None) -> dict:
    """POST / PATCH /api/v1/holiday-calendar-assignments (04-holiday/01 §4.4): criteria rows for a calendar and/or a
    work week, priority unique among date-overlapping rows of the same kind (``CALENDAR_ASSIGNMENT_CONFLICT``)."""
    from app.services import holiday_calendar_service, holiday_work_weeks

    cid = principal.company_id
    if not (principal.is_company_admin or principal.scopes("holiday:manage")):
        raise forbidden()
    row = None
    if assignment_id is not None:
        row = db.query(HolidayCalendarAssignment).filter(HolidayCalendarAssignment.company_id == cid,
                                                         HolidayCalendarAssignment.id == int(assignment_id)) \
            .with_for_update().first()
        if row is None:
            raise not_found("Calendar assignment")
        if expected_row_version is not None and expected_row_version != row.row_version:
            raise conflict("This assignment was changed by someone else. Reload and try again.",
                           "ROW_VERSION_CONFLICT")
    before = serialize_assignment(db, row) if row is not None else None
    calendar_id = data.get("calendar_id", row.calendar_id if row else None)
    pattern_id = data.get("work_week_pattern_id", row.work_week_pattern_id if row else None)
    if not calendar_id and not pattern_id:
        raise AppError(422, "A row needs a holiday calendar, a work week or both.", "INVALID_ASSIGNMENT")
    entity_id = data.get("legal_entity_id", row.legal_entity_id if row else None)
    calendar = _calendar_usable(db, cid, calendar_id) if calendar_id and (row is None or calendar_id != row.calendar_id) \
        else (db.get(HolidayCalendar, calendar_id) if calendar_id else None)
    pattern = _pattern_usable(db, cid, pattern_id, entity_id) if pattern_id and (
        row is None or pattern_id != row.work_week_pattern_id) else (db.get(WorkWeekPattern, pattern_id) if pattern_id else None)
    criteria = validate_criteria(db, cid, data.get("criteria", row.criteria if row else None))
    for owned in (calendar, pattern):
        owner = getattr(owned, "legal_entity_id", None)
        if owner is not None:
            if entity_id is None:
                entity_id = owner
            if owner != entity_id or criteria.get("legal_entity_ids", [owner]) != [owner]:
                raise AppError(422, f"{owned.code} belongs to another legal entity than the people of this row.",
                               "CALENDAR_ENTITY_MISMATCH" if owned is calendar else "PATTERN_ENTITY_MISMATCH")
    if entity_id is not None:
        criteria.setdefault("legal_entity_ids", [entity_id])
        if not (principal.is_company_admin or principal.has_company_scope("holiday:manage")
                or entity_id in principal.scope_targets("holiday:manage", "LEGAL_ENTITY")):
            raise forbidden("You cannot assign calendars in this legal entity.")
    elif not (principal.is_company_admin or principal.has_company_scope("holiday:manage")):
        raise forbidden("Rows that are not limited to one legal entity need company-wide holiday administration.")
    priority = int(data.get("priority", row.priority if row else 100))
    if not 1 <= priority <= 999:
        raise AppError(422, "Priority is 1-999 (lower wins).", "INVALID_PRIORITY")
    start = _as_date(data["effective_from"]) if data.get("effective_from") else (
        row.effective_from if row else clock.company_today(db, cid))
    end = _as_date(data["effective_to"]) if data.get("effective_to") else (
        None if "effective_to" in data or row is None else row.effective_to)
    if end is not None and end < start:
        raise AppError(422, "The assignment ends before it starts.", "INVALID_RANGE")
    if _priority_conflict(db, cid, row.id if row else None, priority, start, end, bool(calendar_id), bool(pattern_id)):
        raise conflict("Another assignment with this priority overlaps these dates; choose another priority.",
                       "CALENDAR_ASSIGNMENT_CONFLICT")
    reason = (data.get("reason") or "").strip() or None
    t_link = _assignment_clock(db, cid, (calendar_id, row.calendar_id if row else None))
    touched_from = _first_affected(row, start, end, criteria, priority, calendar_id, pattern_id)
    if touched_from != date.max and touched_from < t_link:
        if not (principal.is_company_admin or principal.has("holiday:publish")):
            raise forbidden("Backdated assignments need the 'Publish and backdate holidays' permission.")
        if not reason:
            raise bad_request("Give a reason for a backdated assignment.", "REASON_REQUIRED")
        limit = t_link - timedelta(days=holiday_work_weeks.backdate_days(db, cid))
        if touched_from < limit:
            raise AppError(422, f"An assignment can be backdated to {limit.isoformat()} at the earliest.",
                           "BACKDATE_LIMIT")
    if row is None:
        row = HolidayCalendarAssignment(company_id=cid, created_by_user_id=principal.user_id, row_version=1)
        db.add(row)
    else:
        row.row_version += 1
    row.calendar_id, row.work_week_pattern_id = calendar_id or None, pattern_id or None
    row.criteria, row.priority, row.effective_from, row.effective_to = criteria, priority, start, end
    row.legal_entity_id, row.reason = entity_id, reason
    holiday_calendar_service._flush(db)
    after = serialize_assignment(db, row)
    from app.payroll.services import time_inputs
    from app.services import leave_jobs

    time_inputs.log_change(db, cid, record_type="calendar_link", record_id=row.id,
                           change="CREATED" if before is None else "UPDATED", content=after,
                           date_from=None if touched_from == date.max else touched_from,
                           date_to=None, actor_user_id=principal.user_id, details={"assignment_id": row.id})
    dates: List[date] = []
    if touched_from != date.max:
        horizon_end = leave_jobs.horizon(touched_from)
        last = max(d for d in (end, before and before.get("effective_to") and _as_date(before["effective_to"]),
                               horizon_end) if d is not None)
        dates = list(workweek.dates_between(touched_from, min(horizon_end, last)))
    recount = leave_jobs.recount_dates(db, cid, dates, key=f"recalc:assignment:{row.id}:{row.row_version}",
                                       actor_user_id=principal.user_id)
    AuditLogService.log(db, principal, AUDIT_MODULE, "CALENDAR_ASSIGNED",
                        f"Calendar assignment #{row.id} (priority {priority}) from {start.isoformat()}",
                        "holiday_calendar_assignment", row.id,
                        details={"before": before, "after": after, "reason": reason})
    outbox.emit(db, "calendar.link.changed", {"assignment_id": row.id, "effective_date": start.isoformat()})
    return {**after, "recount": recount}
