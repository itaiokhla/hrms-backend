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
        self._employment_codes: Optional[Dict[int, str]] = None

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

    def criteria_rows(self) -> List[HolidayCalendarAssignment]:
        if self._criteria is None:
            self._criteria = (self.db.query(HolidayCalendarAssignment)
                              .filter(HolidayCalendarAssignment.company_id == self.company_id,
                                      HolidayCalendarAssignment.work_week_pattern_id.isnot(None))
                              .order_by(HolidayCalendarAssignment.priority, HolidayCalendarAssignment.id).all())
        return self._criteria

    def _employment_code(self, type_id: Optional[int]) -> Optional[str]:
        if self._employment_codes is None:
            self._employment_codes = dict(self.db.query(EmploymentType.id, EmploymentType.code)
                                          .filter(EmploymentType.company_id == self.company_id))
        return self._employment_codes.get(type_id) if type_id else None

    def matches(self, criteria: Mapping[str, Any], assignment: EmployeeAssignment) -> bool:
        """Every given dimension matches (any listed value); unknown dimensions never match (safe default)."""
        if not criteria:
            return False
        for key, values in criteria.items():
            if key in CRITERIA_KEYS:
                if getattr(assignment, CRITERIA_KEYS[key]) not in (values or []):
                    return False
            elif key == "employment_types":
                if self._employment_code(assignment.employment_type_id) not in (values or []):
                    return False
            elif key == "shift_ids":
                # The resolved shift of the date; until shift resolution ships (AS-M1) the placement's shift.
                if assignment.shift_id not in (values or []):
                    return False
            else:
                return False
        return True

    def criteria_pattern_on(self, assignment: EmployeeAssignment, day: date) -> Optional[int]:
        for row in self.criteria_rows():
            if row.effective_from <= day and (row.effective_to is None or day <= row.effective_to) \
                    and self.matches(row.criteria or {}, assignment):
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


def link_clock(db: Session, branch: Branch, pattern_ids: Iterable[Optional[int]]) -> date:
    """T_link (04-holiday/01 §4.3 step 2): the furthest-ahead of the branch's local today and the local today of the
    old and the new pattern (calendar_local_today joins when the calendar service ships)."""
    from app.services import holiday_work_weeks  # local: holiday_work_weeks imports this module

    today = branch_local_today(db, branch)
    for pattern_id in pattern_ids:
        if pattern_id:
            today = max(today, holiday_work_weeks.pattern_local_today(db, branch.company_id, pattern_id))
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
    if data.get("holiday_calendar_id") is not None:
        _calendar_usable(db, company_id, data["holiday_calendar_id"])

    old_pattern_today = resolver.branch_pattern_on(branch.id, branch_local_today(db, branch))
    t_link = link_clock(db, branch, (old_pattern_today, new_pattern))
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
    activated = holiday_work_weeks.refresh_mirrors(db, company_id)
    db.flush()
    return {"links": changed_links, "patterns": activated}


@jobs.register("holiday.link_activate")
def _link_activate_job(ctx: jobs.JobContext) -> dict:
    payload = ctx.payload or {}
    local_date = date.fromisoformat(payload["local_date"]) if payload.get("local_date") else \
        zone_today(payload.get("timezone"))
    return activate(ctx.db, ctx.company_id, local_date, payload.get("timezone"))
