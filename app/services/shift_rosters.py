"""Roster periods, drafts, publication and the roster provider (AS-M5; 03-shifts/02 §4, §5, §7; spine §5.4).

* ``ShiftRosterProvider`` is the real LH ``RosterProvider`` (JC-AS-31): registered with ``holiday_rosters`` at import,
  so day facts read published entries (a rostered OFF is a full weekly off, a WORK entry a working day); employees
  without published entries keep their work-week pattern (zero diff). ``entries_many`` is the batch read used by
  ``shift_resolution`` step 1.
* Drafts are invisible to every reader until ``publish``: lock the employees (LCK-02) and the period, build the
  candidate (published entries ⊕ drafts), validate with ``roster_rules`` (§6), gate BLOCK / REQUIRE_REASON / WARN,
  then write entries (version + 1 on change, withdrawals), one ``roster_entry_changes`` row per change with
  ``notice_hours``, the append-only ``roster_publications`` row with ``snapshot_hash``, ``log_change('roster_entry')``
  per employee and contiguous range, audit and outbox (ids only).

Limits (IG-AS-31): the WORKING_TIME pack values of the employee's legal-entity country (appendix-a; DRAFT packs act as
WARN) combined with the tenant scheduling defaults ``DEFAULT_SCHEDULING_POLICY`` (min rest 11 h, 48 h a week, both
BLOCK) until ``SCHEDULING_POLICY`` ships with AS-M3 — a policy may only make a limit stricter (§6.1). A publication
refused for a rest violation answers 422 ``INSUFFICIENT_REST_HOURS`` (issues carry the spec rule keys).

Services never commit (routes own the transaction).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pytz
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.authz import employee_scope
from app.core.concurrency import check_row_version, lock_employees
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.database.db import current_company_id
from app.models.company import Company
from app.models.employee import Employee, Shift
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.rosters import (
    MAX_PERIOD_DAYS, RosterDraft, RosterEntry, RosterEntryChange, RosterPeriod, RosterPublication,
)
from app.models.shifts import ShiftVersion
from app.payroll.services import outbox
from app.payroll.services.time_inputs import log_change
from app.services import holiday_rosters
from app.services import shift_engine as engine
from app.services.audit_log_service import AuditLogService

READ, PLAN, PUBLISH, EXEMPT = "roster:read", "roster:plan", "roster:publish", "roster:exempt"
MODULE = "Rosters"
ONE_DAY = timedelta(days=1)
SEAM_DAYS = 14  # checked span: changed dates ± 14 days (§6.1)
MAX_DRAFT_CELLS = 500
MAX_PUBLISH_ENTRIES = 10_000
REST_RULES = ("MIN_REST_VIOLATION", "WEEKLY_REST_HOURS")
ENTRY_KEYS = ("employee_id", "shift_date", "entry_seq", "kind", "shift_id", "shift_version_id", "custom_start_time",
              "custom_end_time", "custom_segments", "site_id", "planned_work_mode", "version")
# Tenant scheduling defaults until SCHEDULING_POLICY (AS-M3): stricter-only, so they may raise the pack severity.
DEFAULT_SCHEDULING_POLICY = {"min_rest_hours": Decimal(11), "min_rest_severity": "BLOCK",
                             "max_hours_per_week": Decimal(48), "weekly_hours_severity": "BLOCK"}


# =============================================================================================== provider
@dataclass(frozen=True)
class ProviderEntry:
    """LH ``RosterEntry`` (kind, roster_version, published_at, shift_id) + the JC-AS-31 extensions."""

    kind: str
    roster_version: int
    published_at: datetime
    shift_id: Optional[int] = None
    entry_seq: int = 1
    shift_version_id: Optional[int] = None
    custom_times: Optional[Tuple[str, str]] = None
    site_id: Optional[int] = None
    roster_entry_id: Optional[int] = None
    planned_work_mode: Optional[str] = None


def _provider_entry(row: RosterEntry) -> ProviderEntry:
    return ProviderEntry(kind=row.kind, roster_version=row.version, published_at=row.published_at,
                         shift_id=row.shift_id, entry_seq=row.entry_seq, shift_version_id=row.shift_version_id,
                         custom_times=(row.custom_start_time, row.custom_end_time) if row.custom_start_time else None,
                         site_id=row.site_id, roster_entry_id=row.id, planned_work_mode=row.planned_work_mode)


def entries_many(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date
                 ) -> Dict[Tuple[int, date], List[RosterEntry]]:
    """Published, not withdrawn entries in ``entry_seq`` order, keyed by (employee, date); one query."""
    ids = sorted(set(employee_ids))
    if not ids:
        return {}
    found: Dict[Tuple[int, date], List[RosterEntry]] = {}
    for row in (db.query(RosterEntry)
                .filter(RosterEntry.company_id == company_id, RosterEntry.employee_id.in_(ids),
                        RosterEntry.shift_date >= start, RosterEntry.shift_date <= end,
                        RosterEntry.withdrawn_at.is_(None))
                .order_by(RosterEntry.employee_id, RosterEntry.shift_date, RosterEntry.entry_seq)):
        found.setdefault((row.employee_id, row.shift_date), []).append(row)
    return found


_CACHE_KEY = "shift_rosters.provider"


def _cache(db: Session) -> dict:
    return db.info.setdefault(_CACHE_KEY, {"has_entries": {}, "prefetched": []})


def invalidate_provider_cache(db: Session) -> None:
    """A publication changed entries: drop the per-session provider caches."""
    db.info.pop(_CACHE_KEY, None)


class ShiftRosterProvider:
    """The real ``RosterProvider`` (03-shifts/02 §7). Roster-based = a published entry exists for (E, D) until
    ``SCHEDULING_POLICY.schedule_source`` ships (AS-M3).

    Per-session caches keep day-facts batches cheap: a company without any published entry answers every question
    from one query, and ``prefetch`` loads a whole (employees, range) batch once (the day resolvers call the
    per-employee methods in loops). Publications invalidate them (``invalidate_provider_cache``)."""

    def _has_entries(self, db: Session, company_id: int) -> bool:
        cache = _cache(db)["has_entries"]
        if company_id not in cache:
            cache[company_id] = db.query(RosterEntry.id).filter(
                RosterEntry.company_id == company_id, RosterEntry.withdrawn_at.is_(None)).first() is not None
        return cache[company_id]

    def prefetch(self, db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date) -> None:
        """Load the published entries of a batch once; later per-employee calls inside it hit the cache."""
        if not self._has_entries(db, company_id):
            return
        ids = frozenset(employee_ids)
        _cache(db)["prefetched"].append((company_id, ids, start, end,
                                         entries_many(db, company_id, sorted(ids), start, end)))

    def _rows(self, db: Session, company_id: int, employee_id: int, start: date, end: date
              ) -> Dict[Tuple[int, date], List[RosterEntry]]:
        if not self._has_entries(db, company_id):
            return {}
        for cid, ids, first, last, found in _cache(db)["prefetched"]:
            if cid == company_id and employee_id in ids and first <= start and end <= last:
                return {k: v for k, v in found.items() if k[0] == employee_id and start <= k[1] <= end}
        return entries_many(db, company_id, [employee_id], start, end)

    def is_roster_based(self, db: Session, company_id: int, employee_id: int, day: date) -> bool:
        return bool(self._rows(db, company_id, employee_id, day, day))

    def day_entries(self, db: Session, company_id: int, employee_id: int, day: date) -> List[ProviderEntry]:
        return [_provider_entry(r) for r in self._rows(db, company_id, employee_id, day, day).get(
            (employee_id, day), [])]

    def entries(self, db: Session, company_id: int, employee_id: int, start: date, end: date
                ) -> Mapping[date, ProviderEntry]:
        """Day summary: WORK if any WORK entry exists (fields of the lowest WORK entry_seq), else OFF."""
        out: Dict[date, ProviderEntry] = {}
        for (_, day), rows in self._rows(db, company_id, employee_id, start, end).items():
            work = [r for r in rows if r.kind == "WORK"]
            out[day] = _provider_entry(work[0] if work else rows[0])
        return out

    def entries_batch(self, db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date
                      ) -> Dict[int, Dict[date, List[ProviderEntry]]]:
        out: Dict[int, Dict[date, List[ProviderEntry]]] = {}
        if not self._has_entries(db, company_id):
            return out
        for (employee_id, day), rows in entries_many(db, company_id, employee_ids, start, end).items():
            out.setdefault(employee_id, {})[day] = [_provider_entry(r) for r in rows]
        return out


holiday_rosters.register(ShiftRosterProvider())


# =============================================================================================== serializers
def entry_object(values: Mapping[str, Any]) -> dict:
    """The canonical entry object of §5.3 (snapshot hash, change before/after)."""
    out = {}
    for key in ENTRY_KEYS:
        value = values.get(key)
        out[key] = value.isoformat() if isinstance(value, date) else value
    return out


def snapshot_hash(entries: Iterable[Mapping[str, Any]]) -> str:
    rows = sorted((entry_object(e) for e in entries), key=lambda e: (e["employee_id"], e["shift_date"], e["entry_seq"]))
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def serialize_period(row: RosterPeriod, drafts: int = 0) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name, "team_id": row.team_id, "branch_id": row.branch_id,
            "department_ids": row.department_ids, "start_date": row.start_date.isoformat(),
            "end_date": row.end_date.isoformat(), "status": row.status,
            "publish_due_at": row.publish_due_at.isoformat() if row.publish_due_at else None,
            "last_published_at": row.last_published_at.isoformat() if row.last_published_at else None,
            "last_publication_id": row.last_publication_id, "requires_approval": row.requires_approval,
            "row_version": row.row_version, "drafts": drafts}


def serialize_entry(row: RosterEntry) -> dict:
    return {"id": row.id, **entry_object({k: getattr(row, k) for k in ENTRY_KEYS}), "source": row.source,
            "roster_period_id": row.roster_period_id, "publication_id": row.publication_id,
            "published_at": row.published_at.isoformat(), "acknowledged_at":
                row.acknowledged_at.isoformat() if row.acknowledged_at else None, "note": row.note}


def serialize_draft(row: RosterDraft) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "shift_date": row.shift_date.isoformat(),
            "entry_seq": row.entry_seq, "action": row.action, "kind": row.kind, "shift_id": row.shift_id,
            "custom_start_time": row.custom_start_time, "custom_end_time": row.custom_end_time,
            "site_id": row.site_id, "planned_work_mode": row.planned_work_mode, "source": row.source,
            "base_entry_id": row.base_entry_id, "base_entry_version": row.base_entry_version, "note": row.note}


# =============================================================================================== scope
def _owner_employees(db: Session, period: RosterPeriod) -> List[int]:
    """Employees placed in the period's branch (and departments) at any date of the period; for a team-owned period
    the team's members at any date of the period (X-M1; with a branch too, only members placed there)."""
    if period.team_id is not None:
        from app.models.teams import TeamMembership

        members = {e for (e,) in db.query(TeamMembership.employee_id).filter(
            TeamMembership.company_id == period.company_id, TeamMembership.team_id == period.team_id,
            TeamMembership.effective_from <= period.end_date,
            or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= period.start_date),
            or_(TeamMembership.effective_to.is_(None),
                TeamMembership.effective_to >= TeamMembership.effective_from)).distinct()}
        if period.branch_id is None:
            return sorted(members)
    query = db.query(EmployeeAssignment.employee_id).filter(
        EmployeeAssignment.company_id == period.company_id, EmployeeAssignment.is_primary.is_(True),
        EmployeeAssignment.effective_from <= period.end_date,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= period.start_date))
    if period.branch_id is not None:
        query = query.filter(EmployeeAssignment.branch_id == period.branch_id)
    if period.department_ids:
        query = query.filter(EmployeeAssignment.department_id.in_(list(period.department_ids)))
    placed = {e for (e,) in query.distinct()}
    return sorted(placed & members if period.team_id is not None else placed)


def _covering(db: Session, principal: Principal, code: str, employee_ids: Iterable[int]) -> None:
    """Every employee must be inside the caller's scope of ``code`` (else 404, never revealing the id)."""
    ids = sorted(set(employee_ids))
    if not ids:
        return
    if not principal.has(code):
        raise forbidden()
    scope = employee_scope(db, principal, code)
    found = {e for (e,) in db.query(Employee.id).filter(Employee.company_id == principal.company_id,
                                                         Employee.id.in_(ids), scope.clause(Employee.id))}
    if found != set(ids):
        raise not_found("One or more employees")


def _branch_zone(db: Session, company_id: int, branch_id: Optional[int]) -> str:
    branch = db.get(Branch, branch_id) if branch_id else None
    entity = db.get(LegalEntity, branch.legal_entity_id) if branch is not None else None
    company = db.get(Company, company_id)
    for zone in (getattr(branch, "timezone", None), getattr(entity, "timezone", None), getattr(company, "timezone", None)):
        if zone and zone in pytz.all_timezones_set:
            return zone
    return "UTC"


def load_period(db: Session, principal: Principal, period_id: int, *, lock: bool = False) -> RosterPeriod:
    query = db.query(RosterPeriod).filter(RosterPeriod.company_id == principal.company_id, RosterPeriod.id == period_id)
    row = (query.with_for_update() if lock else query).first()
    if row is None:
        raise not_found("Roster period")
    if not (principal.is_company_admin or principal.has_company_scope(READ) or principal.has_company_scope(PLAN)):
        employees = _owner_employees(db, row)
        scope = employee_scope(db, principal, READ)
        visible = db.query(Employee.id).filter(Employee.company_id == principal.company_id,
                                               Employee.id.in_(employees or [0]), scope.clause(Employee.id)).first()
        if visible is None:
            raise not_found("Roster period")
    return row


# =============================================================================================== periods
def create_period(db: Session, principal: Principal, data) -> dict:
    if data.end_date < data.start_date or (data.end_date - data.start_date).days > MAX_PERIOD_DAYS - 1:
        raise AppError(422, f"A roster period covers at most {MAX_PERIOD_DAYS} days.", "ROSTER_PERIOD_TOO_LONG")
    if data.branch_id is None and getattr(data, "team_id", None) is None:
        raise bad_request("Choose the branch (or team) the period plans.", "VALIDATION_ERROR")
    if data.branch_id is not None and db.query(Branch.id).filter(
            Branch.company_id == principal.company_id, Branch.id == data.branch_id).first() is None:
        raise not_found("Branch")
    if getattr(data, "team_id", None) is not None:
        from app.models.teams import Team

        team = db.query(Team).filter(Team.company_id == principal.company_id, Team.id == data.team_id).first()
        if team is None:
            raise not_found("Team")
        if team.status != "ACTIVE":
            raise conflict("The team is archived.", "TEAM_ARCHIVED")
    if db.query(RosterPeriod.id).filter(RosterPeriod.company_id == principal.company_id,
                                        RosterPeriod.code == data.code).first():
        raise conflict("Another roster period uses this code.", "CONFLICT")
    row = RosterPeriod(company_id=principal.company_id, code=data.code, name=data.name, branch_id=data.branch_id,
                       team_id=getattr(data, "team_id", None), department_ids=data.department_ids or None,
                       start_date=data.start_date, end_date=data.end_date, status="DRAFT")
    _covering(db, principal, PLAN, _owner_employees(db, row))
    zone = _branch_zone(db, principal.company_id, data.branch_id)
    notice = getattr(data, "publish_notice_days", None)
    if notice is not None:
        due_day = data.start_date - timedelta(days=notice) + ONE_DAY
        row.publish_due_at = pytz.timezone(zone).localize(datetime.combine(due_day, time(0, 0))).astimezone(timezone.utc)
    db.add(row)
    try:
        with db.begin_nested():
            db.flush()
    except Exception as exc:  # trigger roster_periods_no_overlap
        if "roster_periods_no_overlap" in str(getattr(exc, "orig", exc)):
            raise conflict("Another roster period of this owner covers these dates.", "ROSTER_PERIOD_OVERLAP")
        raise
    AuditLogService.log(db, principal, MODULE, "ROSTER_PERIOD_CREATED", f"Created roster period {row.code}",
                        "roster_period", row.id)
    _emit(db, "roster.period.created", {"roster_period_id": row.id})
    return serialize_period(row)


def list_periods(db: Session, principal: Principal, *, status: Optional[str] = None, skip: int = 0,
                 limit: int = 50) -> dict:
    query = db.query(RosterPeriod).filter(RosterPeriod.company_id == principal.company_id)
    if status:
        query = query.filter(RosterPeriod.status == status)
    rows = query.order_by(RosterPeriod.start_date.desc(), RosterPeriod.id.desc()).all()
    if not (principal.is_company_admin or principal.has_company_scope(READ) or principal.has_company_scope(PLAN)):
        scope = employee_scope(db, principal, READ)
        visible = []
        for row in rows:
            employees = _owner_employees(db, row)
            if employees and db.query(Employee.id).filter(Employee.company_id == principal.company_id,
                                                          Employee.id.in_(employees),
                                                          scope.clause(Employee.id)).first():
                visible.append(row)
        rows = visible
    counts = dict(db.query(RosterDraft.roster_period_id, func.count(RosterDraft.id)).filter(
        RosterDraft.company_id == principal.company_id).group_by(RosterDraft.roster_period_id).all())
    page = rows[skip: skip + limit]
    return {"items": [serialize_period(r, counts.get(r.id, 0)) for r in page], "total": len(rows), "skip": skip,
            "limit": limit}


def _visible_employees(db: Session, principal: Principal, employees: Sequence[int]) -> List[int]:
    """The subset of ``employees`` inside the caller's roster read / plan / publish scope."""
    codes = [c for c in (READ, PLAN, PUBLISH) if principal.has(c)]
    if principal.is_company_admin or any(principal.has_company_scope(c) for c in codes):
        return list(employees)
    visible: set = set()
    for code in codes:
        scope = employee_scope(db, principal, code)
        visible |= {e for (e,) in db.query(Employee.id).filter(Employee.company_id == principal.company_id,
                                                                Employee.id.in_(employees or [0]),
                                                                scope.clause(Employee.id))}
    return [e for e in employees if e in visible]


def grid(db: Session, principal: Principal, period_id: int) -> dict:
    """The period grid: employees in scope, published entries, drafts and the shift legend (§13). Only the employees
    the caller's ``roster:read`` / ``plan`` / ``publish`` scope reaches are returned (a SELF reader sees their own
    row, never colleagues' schedules); drafts are planner work in progress and go to planners and publishers only
    (S5, IG-AS-144)."""
    period = load_period(db, principal, period_id)
    employees = _visible_employees(db, principal, _owner_employees(db, period))
    people = {e.id: e for e in db.query(Employee).filter(Employee.company_id == period.company_id,
                                                         Employee.id.in_(employees or [0]))}
    entries = entries_many(db, period.company_id, employees, period.start_date, period.end_date)
    drafts = db.query(RosterDraft).filter(RosterDraft.company_id == period.company_id,
                                          RosterDraft.roster_period_id == period.id,
                                          RosterDraft.employee_id.in_(employees or [0])).all() \
        if (principal.has(PLAN) or principal.has(PUBLISH)) else []
    shift_ids = {r.shift_id for rows in entries.values() for r in rows if r.shift_id} | {
        d.shift_id for d in drafts if d.shift_id}
    shifts = db.query(Shift).filter(Shift.company_id == period.company_id,
                                    or_(Shift.id.in_(shift_ids or [0]), Shift.status == "ACTIVE")).all()
    from app.services import roster_consents  # EC AS-ROS-06: badge of entries a consent change made invalid

    conflicts = roster_consents.open_conflicts(db, period.company_id, [r.id for rows in entries.values() for r in rows])
    from app.services import shift_requests

    owners = _owner_employees(db, period)
    return {
        # §5.2: whether a publish of this period goes to an approver, and whether one is waiting (drafts frozen)
        "publish_requires_approval": bool(owners) and shift_requests._setting(
            db, period.company_id, APPROVAL_SETTING, owners[0], period.start_date),
        "approval_pending": approval_pending(db, period),
        "consent_conflicts": [{"roster_entry_id": entry_id, "purposes": sorted(purposes)}
                              for entry_id, purposes in sorted(conflicts.items())],
        "period": serialize_period(period, len(drafts)),
        "employees": [{"id": e, "name": f"{people[e].first_name} {people[e].last_name}".strip(),
                       "employee_code": people[e].employee_code} for e in employees if e in people],
        "entries": [serialize_entry(r) for rows in entries.values() for r in rows],
        "drafts": [serialize_draft(d) for d in drafts],
        "shifts": [{"id": s.id, "code": s.code, "name": s.name, "color_token": s.color_token,
                    "start_time": s.start_time, "end_time": s.end_time, "status": s.status} for s in shifts],
    }


def set_period_status(db: Session, principal: Principal, period_id: int, target: str,
                      expected_row_version: Optional[int]) -> dict:
    period = load_period(db, principal, period_id, lock=True)
    check_row_version(period, expected_row_version)
    if target == "CANCELLED":
        if period.status != "DRAFT":
            raise AppError(409, "Only a draft period can be cancelled.", "INVALID_TRANSITION",
                           extra={"details": {"current_status": period.status}})
        _covering(db, principal, PLAN, _owner_employees(db, period))
        db.query(RosterDraft).filter(RosterDraft.company_id == period.company_id,
                                     RosterDraft.roster_period_id == period.id).delete(synchronize_session=False)
    elif target == "CLOSED":
        if period.status != "PUBLISHED":
            raise AppError(409, "Only a published period can be closed.", "INVALID_TRANSITION",
                           extra={"details": {"current_status": period.status}})
        _covering(db, principal, PUBLISH, _owner_employees(db, period))
    period.status = target
    period.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, MODULE, f"ROSTER_PERIOD_{target}", f"Roster period {period.code} {target.lower()}",
                        "roster_period", period.id)
    _emit(db, f"roster.period.{target.lower()}", {"roster_period_id": period.id})
    return serialize_period(period)


# =============================================================================================== drafts
def put_drafts(db: Session, principal: Principal, period_id: int, cells: Sequence[Any],
               expected_version: Optional[int]) -> dict:
    """Upsert drafts (≤ 500 cells); shape checks only — the rules of §6 run at validate / publish (§4.2)."""
    if len(cells) > MAX_DRAFT_CELLS:
        raise AppError(422, f"Save at most {MAX_DRAFT_CELLS} cells at a time.", "TOO_MANY_ITEMS",
                       extra={"details": {"limit": MAX_DRAFT_CELLS}})
    period = load_period(db, principal, period_id, lock=True)
    check_row_version(period, expected_version)
    _frozen(db, period)
    if period.status not in ("DRAFT", "PUBLISHED", "AMENDING"):
        raise AppError(409, "This roster period is closed for changes.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": period.status}})
    owners = set(_owner_employees(db, period))
    _covering(db, principal, PLAN, {c.employee_id for c in cells})
    shift_ids = {c.shift_id for c in cells if getattr(c, "shift_id", None)}
    shifts = {s.id: s for s in db.query(Shift).filter(Shift.company_id == period.company_id,
                                                      Shift.id.in_(shift_ids or [0]))}
    published = entries_many(db, period.company_id, {c.employee_id for c in cells}, period.start_date, period.end_date)
    saved = 0
    for cell in cells:
        if not period.start_date <= cell.shift_date <= period.end_date:
            raise AppError(422, "A cell is outside the period dates.", "VALIDATION_ERROR",
                           extra={"issues": [{"field": "shift_date", "code": "OUT_OF_PERIOD",
                                              "message": cell.shift_date.isoformat()}]})
        if cell.employee_id not in owners:
            raise not_found("Employee")
        if cell.shift_id is not None:
            shift = shifts.get(cell.shift_id)
            if shift is None:
                raise not_found("Shift")
            if shift.status != "ACTIVE":
                raise AppError(422, f"Shift {shift.code} is archived.", "SHIFT_ARCHIVED")
        base = next((r for r in published.get((cell.employee_id, cell.shift_date), []) if r.entry_seq == cell.entry_seq),
                    None)
        if base is not None and base.roster_period_id not in (None, period.id):
            other = db.get(RosterPeriod, base.roster_period_id)
            raise AppError(409, "This cell belongs to another roster period.", "ROSTER_CELL_IN_OTHER_PERIOD",
                           extra={"details": {"period_code": other.code if other else None}})
        row = db.query(RosterDraft).filter(
            RosterDraft.company_id == period.company_id, RosterDraft.roster_period_id == period.id,
            RosterDraft.employee_id == cell.employee_id, RosterDraft.shift_date == cell.shift_date,
            RosterDraft.entry_seq == cell.entry_seq).first()
        if row is None:
            row = RosterDraft(company_id=period.company_id, roster_period_id=period.id, employee_id=cell.employee_id,
                              shift_date=cell.shift_date, entry_seq=cell.entry_seq)
            db.add(row)
        else:
            row.row_version += 1
        row.action = cell.action
        row.kind = cell.kind if cell.action == "UPSERT" else None
        row.shift_id = cell.shift_id if cell.kind == "WORK" else None
        row.custom_start_time = cell.custom_start_time if cell.kind == "WORK" else None
        row.custom_end_time = cell.custom_end_time if cell.kind == "WORK" else None
        row.site_id = getattr(cell, "site_id", None)
        row.planned_work_mode = getattr(cell, "planned_work_mode", None)
        row.source = getattr(cell, "source", None) or "MANUAL"
        if not (row.kind == "OFF" and row.source == "HOLIDAY_RULE"):
            row.planned_shift_id = None  # only a holiday-rule OFF remembers the shift it replaced (§8)
        row.note = getattr(cell, "note", None)
        row.reason = getattr(cell, "reason", None)
        row.base_entry_id = base.id if base is not None else None
        row.base_entry_version = base.version if base is not None else None
        row.created_by_user_id = principal.user_id  # the last saver of the cell (SoD, EC AS-ROS-11)
        saved += 1
        if cell.action == "UPSERT" and cell.kind == "OFF" and cell.entry_seq == 1:
            # An OFF is always alone on its date: later entries of the date are withdrawn too (§7.2).
            db.query(RosterDraft).filter(RosterDraft.company_id == period.company_id,
                                         RosterDraft.roster_period_id == period.id,
                                         RosterDraft.employee_id == cell.employee_id,
                                         RosterDraft.shift_date == cell.shift_date,
                                         RosterDraft.entry_seq > 1).delete(synchronize_session=False)
    if period.status == "PUBLISHED" and saved:
        period.status = "AMENDING"
    period.row_version += 1
    db.flush()
    return {"saved": saved, "period": serialize_period(period, db.query(func.count(RosterDraft.id)).filter(
        RosterDraft.company_id == period.company_id, RosterDraft.roster_period_id == period.id).scalar())}


def discard_drafts(db: Session, principal: Principal, period_id: int, expected_version: Optional[int]) -> dict:
    period = load_period(db, principal, period_id, lock=True)
    check_row_version(period, expected_version)
    _frozen(db, period)
    _covering(db, principal, PLAN, _owner_employees(db, period))
    db.query(RosterDraft).filter(RosterDraft.company_id == period.company_id,
                                 RosterDraft.roster_period_id == period.id).delete(synchronize_session=False)
    if period.status == "AMENDING":
        period.status = "PUBLISHED"
    period.row_version += 1
    db.flush()
    return serialize_period(period)


# =============================================================================================== candidate
@dataclass
class _Cell:
    employee_id: int
    shift_date: date
    entry_seq: int
    kind: Optional[str]  # None = withdrawn
    shift_id: Optional[int] = None
    shift_version_id: Optional[int] = None
    custom_start_time: Optional[str] = None
    custom_end_time: Optional[str] = None
    custom_segments: Any = None
    site_id: Optional[int] = None
    planned_work_mode: Optional[str] = None
    source: str = "MANUAL"
    note: Optional[str] = None
    entry: Optional[RosterEntry] = None  # the published entry this cell replaces
    changed: bool = False
    planned_shift_id: Optional[int] = None  # HOLIDAY_RULE OFF: the shift of the token it replaced (§8, AS-INT-07)

    def values(self, version: int) -> dict:
        return {"employee_id": self.employee_id, "shift_date": self.shift_date, "entry_seq": self.entry_seq,
                "kind": self.kind, "shift_id": self.shift_id, "shift_version_id": self.shift_version_id,
                "custom_start_time": self.custom_start_time, "custom_end_time": self.custom_end_time,
                "custom_segments": self.custom_segments, "site_id": self.site_id,
                "planned_work_mode": self.planned_work_mode, "version": version}


def _published_cell(row: RosterEntry) -> _Cell:
    return _Cell(row.employee_id, row.shift_date, row.entry_seq, row.kind, row.shift_id, row.shift_version_id,
                 row.custom_start_time, row.custom_end_time, row.custom_segments, row.site_id, row.planned_work_mode,
                 row.source, row.note, entry=row, planned_shift_id=row.planned_shift_id)


def _same(cell: _Cell, entry: RosterEntry) -> bool:
    keys = ("kind", "shift_id", "shift_version_id", "custom_start_time", "custom_end_time", "custom_segments",
            "site_id", "planned_work_mode")
    return all(getattr(cell, k) == getattr(entry, k) for k in keys)


def _versions_on(db: Session, company_id: int, shift_ids: Iterable[int]) -> Dict[int, List[ShiftVersion]]:
    out: Dict[int, List[ShiftVersion]] = {}
    for row in (db.query(ShiftVersion).filter(ShiftVersion.company_id == company_id,
                                              ShiftVersion.shift_id.in_(list(shift_ids) or [0]),
                                              ShiftVersion.voided_at.is_(None))
                .order_by(ShiftVersion.shift_id, ShiftVersion.effective_from)):
        out.setdefault(row.shift_id, []).append(row)
    return out


def _candidate(db: Session, company_id: int, changes: Sequence[_Cell], start: date, end: date
               ) -> Tuple[List[_Cell], List[_Cell]]:
    """(candidate cells of the checked span, changed cells): published entries ⊕ changes (changes win per cell)."""
    employees = {c.employee_id for c in changes}
    published = entries_many(db, company_id, employees, start, end)
    cells: Dict[Tuple[int, date, int], _Cell] = {}
    for rows in published.values():
        for row in rows:
            cells[(row.employee_id, row.shift_date, row.entry_seq)] = _published_cell(row)
    changed: List[_Cell] = []
    for change in changes:
        key = (change.employee_id, change.shift_date, change.entry_seq)
        base = cells.get(key)
        change.entry = base.entry if base is not None else None
        if change.kind is None:  # withdrawal
            if base is None:
                continue
            change.changed = True
            changed.append(change)
            cells.pop(key)
            continue
        if base is not None and _same(change, base.entry):
            continue
        change.changed = True
        changed.append(change)
        cells[key] = change
    return list(cells.values()), changed


def _resolve_versions(db: Session, company_id: int, cells: Sequence[_Cell]) -> None:
    """Fill ``shift_version_id`` of new / changed WORK cells with the version valid on the shift date (§4.3)."""
    versions = _versions_on(db, company_id, {c.shift_id for c in cells if c.shift_id})
    for cell in cells:
        if cell.kind == "WORK" and cell.shift_id and cell.changed:
            rows = versions.get(cell.shift_id, [])
            row = next((r for r in rows if r.effective_from <= cell.shift_date
                        and (r.effective_to is None or cell.shift_date <= r.effective_to)), None)
            cell.shift_version_id = row.id if row is not None else None


# =============================================================================================== validation
@dataclass
class ValidationOutcome:
    results: List[Any] = field(default_factory=list)
    blocking: List[Any] = field(default_factory=list)
    missing_reasons: List[Any] = field(default_factory=list)
    unacknowledged: List[Any] = field(default_factory=list)


def _result_json(result: Any) -> dict:
    raw = result if isinstance(result, dict) else getattr(result, "__dict__", {})
    out = {}
    for key, value in raw.items():
        if isinstance(value, (date, datetime)):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [v.isoformat() if isinstance(v, (date, datetime)) else v for v in value]
        else:
            out[key] = value
    return out


def validate_cells(db: Session, company_id: int, candidate: Sequence[_Cell], changed: Sequence[_Cell], *,
                   publish_at: datetime, acknowledged: Sequence[str] = (), reasons: Mapping[str, Any] = None,
                   exemptions: Mapping[str, Any] = None) -> ValidationOutcome:
    from app.services import roster_validation

    return roster_validation.run(db, company_id, candidate, changed, publish_at=publish_at, acknowledged=acknowledged,
                                 reasons=reasons or {}, exemptions=exemptions or {})


def _gate(outcome: ValidationOutcome, principal: Principal, exemptions: Mapping[str, Any],
          db: Optional[Session] = None) -> None:
    if exemptions and not principal.has(EXEMPT):
        raise forbidden("Publishing with an exemption needs the Publish with exemption permission.")
    if exemptions and db is not None:
        # roster:exempt + step-up (03-shifts/02 §6.1; owner decision IG-AS-150 for EC AS-PAY-14: a correction inside a
        # FINALIZED period is an exemption with a reason and a fresh step-up; payroll gets the retro signal).
        from app.payroll.services import step_up

        step_up.ensure_step_up(db, principal)
    if outcome.blocking:
        keys = {getattr(r, "rule_key", None) or (r.get("rule_key") if isinstance(r, dict) else None)
                for r in outcome.blocking}
        rest = keys & set(REST_RULES)
        code = "INSUFFICIENT_REST_HOURS" if rest else "ROSTER_RULE_VIOLATION"
        message = ("The roster leaves too little rest between shifts." if rest
                   else "The roster breaks a working-time rule.")
        raise AppError(422, message, code, extra={"issues": [_result_json(r) for r in outcome.blocking]})
    if outcome.missing_reasons:
        raise AppError(422, "Give a reason for each flagged rule.", "ROSTER_REASON_REQUIRED",
                       extra={"issues": [_result_json(r) for r in outcome.missing_reasons]})
    if outcome.unacknowledged:
        raise AppError(422, "Acknowledge the warnings before publishing.", "WARNINGS_UNACKNOWLEDGED",
                       extra={"issues": [_result_json(r) for r in outcome.unacknowledged]})


# =============================================================================================== publish
def _instance_start(db: Session, company_id: int, cell_values: Mapping[str, Any], zone: str) -> Optional[datetime]:
    from app.services import roster_validation

    return roster_validation.instance_start(db, company_id, cell_values, zone)


def _notice_hours(start: Optional[datetime], at: datetime) -> Optional[Decimal]:
    if start is None:
        return None
    return (Decimal((start - at).total_seconds()) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ranges(days: Sequence[date]) -> List[Tuple[date, date]]:
    out: List[Tuple[date, date]] = []
    for day in sorted(set(days)):
        if out and day == out[-1][1] + ONE_DAY:
            out[-1] = (out[-1][0], day)
        else:
            out.append((day, day))
    return out


def _write(db: Session, principal: Optional[Principal], company_id: int, *, kind: str, period: Optional[RosterPeriod],
           candidate: Sequence[_Cell], changed: Sequence[_Cell], scope: dict, outcome: ValidationOutcome,
           acknowledged: Sequence[str], reasons: Mapping[str, Any], exemptions: Mapping[str, Any],
           initiated_by: str = "EMPLOYER", reason_code: Optional[str] = None,
           request_id: Optional[int] = None) -> RosterPublication:
    """Write one publication: entries, change rows, the append-only publication, change log, audit, outbox.
    ``request_id``: the applied shift request (AS-M6) stamped on its entries and change rows; every other open request
    on a changed entry is SUPERSEDED (03-shifts/03 §4.4)."""
    from app.services import roster_validation

    now = clock.utcnow()
    zones = roster_validation.zones_for(db, company_id, changed)
    # Final state of the scope for the snapshot hash (computed before the append-only row is inserted).
    final = []
    for cell in candidate:
        version = (cell.entry.version + 1 if cell.entry is not None and cell.changed else
                   cell.entry.version if cell.entry is not None else 1)
        final.append(cell.values(version))
    scope_ids = set(scope.get("employee_ids") or [])
    scope_from = date.fromisoformat(scope["from"]) if scope.get("from") else None
    scope_to = date.fromisoformat(scope["to"]) if scope.get("to") else None
    in_scope = [v for v in final if (not scope_ids or v["employee_id"] in scope_ids)
                and (scope_from is None or v["shift_date"] >= scope_from)
                and (scope_to is None or v["shift_date"] <= scope_to)]
    notices = []
    for cell in changed:
        zone = zones.get((cell.employee_id, cell.shift_date), "UTC")
        starts = [s for s in (
            _instance_start(db, company_id, cell.values(1), zone) if cell.kind else None,
            _instance_start(db, company_id, {k: getattr(cell.entry, k) for k in ENTRY_KEYS if k != "version"}, zone)
            if cell.entry is not None else None) if s is not None]
        notices.append(_notice_hours(min(starts), now) if starts else None)
    warnings = [{"rule_key": getattr(r, "rule_key", None), "severity": getattr(r, "severity", None),
                 "acknowledged": getattr(r, "rule_key", None) in set(acknowledged),
                 "reason_given": getattr(r, "rule_key", None) in reasons,
                 "exemption_ref": (exemptions.get(getattr(r, "rule_key", None)) or {}).get("exemption_ref")}
                for r in outcome.results]
    publication = RosterPublication(
        company_id=company_id, roster_period_id=period.id if period is not None else None, kind=kind,
        scope=json.loads(json.dumps(scope, default=str)),
        published_by_user_id=principal.user_id if principal is not None else None, published_at=now,
        entries_created=sum(1 for c in changed if c.kind and c.entry is None),
        entries_updated=sum(1 for c in changed if c.kind and c.entry is not None),
        entries_withdrawn=sum(1 for c in changed if c.kind is None),
        snapshot_hash=snapshot_hash(in_scope),
        min_notice_hours=min((n for n in notices if n is not None), default=None),
        warnings=json.loads(json.dumps(warnings, default=str)))
    db.add(publication)
    invalidate_provider_cache(db)  # entries change below: day facts must not read a stale cache
    db.flush()
    by_employee: Dict[int, List[date]] = {}
    for cell, notice in zip(changed, notices):
        before = entry_object({k: getattr(cell.entry, k) for k in ENTRY_KEYS}) if cell.entry is not None else None
        if cell.kind is None:
            cell.entry.withdrawn_at = now
            cell.entry.row_version += 1
            change_kind, entry, after = "WITHDRAWN", cell.entry, None
        elif cell.entry is None:
            entry = RosterEntry(company_id=company_id, employee_id=cell.employee_id, shift_date=cell.shift_date,
                                entry_seq=cell.entry_seq, kind=cell.kind, shift_id=cell.shift_id,
                                shift_version_id=cell.shift_version_id, custom_start_time=cell.custom_start_time,
                                custom_end_time=cell.custom_end_time, custom_segments=cell.custom_segments,
                                site_id=cell.site_id, planned_work_mode=cell.planned_work_mode, source=cell.source,
                                roster_period_id=period.id if period is not None else None,
                                publication_id=publication.id, version=1, published_at=now,
                                published_by_user_id=principal.user_id if principal is not None else None,
                                note=cell.note, request_id=request_id, planned_shift_id=cell.planned_shift_id)
            db.add(entry)
            change_kind = "CREATED"
        else:
            entry = cell.entry
            for key in ("kind", "shift_id", "shift_version_id", "custom_start_time", "custom_end_time",
                        "custom_segments", "site_id", "planned_work_mode"):
                setattr(entry, key, getattr(cell, key))
            entry.source, entry.note, entry.planned_shift_id = cell.source, cell.note, cell.planned_shift_id
            entry.request_id = request_id
            entry.acknowledged_at = None  # a changed shift is acknowledged again
            entry.version += 1
            entry.row_version += 1
            entry.publication_id, entry.published_at = publication.id, now
            entry.published_by_user_id = principal.user_id if principal is not None else None
            change_kind = "UPDATED"
        db.flush()
        if change_kind != "WITHDRAWN":
            after = entry_object({k: getattr(entry, k) for k in ENTRY_KEYS})
        db.add(RosterEntryChange(company_id=company_id, publication_id=publication.id, entry_id=entry.id,
                                 employee_id=cell.employee_id, shift_date=cell.shift_date, entry_seq=cell.entry_seq,
                                 change_kind=change_kind, before=before, after=after, notice_hours=notice,
                                 initiated_by=initiated_by, reason_code=reason_code, request_id=request_id))
        by_employee.setdefault(cell.employee_id, []).append(cell.shift_date)
    db.flush()
    from app.services import shift_requests

    shift_requests.supersede_for_entries(db, company_id, [c.entry.id for c in changed if c.entry is not None],
                                         keep_request_id=request_id)
    for employee_id, days in sorted(by_employee.items()):
        for first, last in _ranges(days):
            log_change(db, company_id, record_type="roster_entry", record_id=publication.id, change="UPDATED",
                       content={"publication_id": publication.id, "employee_id": employee_id,
                                "from": first.isoformat(), "to": last.isoformat()},
                       employee_id=employee_id, date_from=first, date_to=last,
                       actor_user_id=principal.user_id if principal is not None else None)
    # Leave on changed dates is re-decided from the new roster (D-AS-17, EC AS-ROS-09): a rostered OFF releases a
    # charged leave day, a WORK entry charges it; the key makes a retry a no-op.
    from app.models.time_inputs import LeaveDay
    from app.services import leave_jobs

    leave_dates = sorted({d for (d,) in db.query(LeaveDay.date).filter(
        LeaveDay.company_id == company_id, LeaveDay.voided_at.is_(None),
        LeaveDay.employee_id.in_(sorted(by_employee) or [0]),
        LeaveDay.date.in_(sorted({c.shift_date for c in changed}) or [date(2000, 1, 1)])).distinct()})
    if leave_dates:
        leave_jobs.recount_dates(db, company_id, leave_dates, key=f"recalc:roster:{publication.id}",
                                 actor_user_id=principal.user_id if principal is not None else None)
    if kind != "REQUEST":  # an applied request tells its own parties (03-shifts/03 §8)
        _notify_employees(db, company_id, publication.id, changed)
    if principal is not None:
        AuditLogService.log(db, principal, MODULE, "ROSTER_PUBLISHED",
                            f"Published {len(changed)} roster change(s)", "roster_publication", publication.id,
                            details={"kind": kind, "created": publication.entries_created,
                                     "updated": publication.entries_updated,
                                     "withdrawn": publication.entries_withdrawn,
                                     "rule_keys": sorted({w["rule_key"] for w in warnings if w["rule_key"]})})
        for key, exemption in (exemptions or {}).items():
            AuditLogService.log(db, principal, MODULE, "ROSTER_EXEMPTION_USED", f"Exemption used for {key}",
                                "roster_publication", publication.id,
                                details={"rule_key": key, "exemption_ref": (exemption or {}).get("exemption_ref")})
    _emit(db, "roster.published", {"publication_id": publication.id})
    return publication


NOTICE_LIST_MAX = 7  # dates named in one roster.published / roster.changed notification


def _notify_employees(db: Session, company_id: int, publication_id: int, changed: Sequence[_Cell]) -> int:
    """Notifications ``roster.published`` / ``roster.changed`` (spine §9.3; 03-shifts/02 §14): each employee whose
    entries a publication created, changed or withdrew hears once — the action link carries the publication, the
    dedup ``roster:{publication_id}:{employee_id}`` — with dates and shift codes only, never anyone's reasons. In-app
    until the ``notify.roster.*`` channel settings exist (FU-AS-53)."""
    from app.services.notification_service import NotificationService

    per: Dict[int, List[_Cell]] = {}
    for cell in changed:
        per.setdefault(cell.employee_id, []).append(cell)
    users = dict(db.query(Employee.id, Employee.user_id).filter(
        Employee.company_id == company_id, Employee.id.in_(sorted(per) or [0]), Employee.user_id.isnot(None)))
    codes = dict(db.query(Shift.id, Shift.code).filter(
        Shift.company_id == company_id, Shift.id.in_(sorted({c.shift_id for c in changed if c.shift_id}) or [0])))
    sent = 0
    for employee_id, cells in sorted(per.items()):
        if not users.get(employee_id):
            continue
        items = []
        for cell in sorted(cells, key=lambda c: (c.shift_date, c.entry_seq)):
            what = ("removed" if cell.kind is None else "off" if cell.kind == "OFF" else
                    codes.get(cell.shift_id) or f"{cell.custom_start_time}–{cell.custom_end_time}")
            items.append(f"{cell.shift_date:%a %d %b} {what}")
        text = ", ".join(items[:NOTICE_LIST_MAX]) + (
            f" and {len(items) - NOTICE_LIST_MAX} more" if len(items) > NOTICE_LIST_MAX else "")
        new = all(c.entry is None for c in cells)
        NotificationService.create(db, users[employee_id], "Your schedule was published" if new else
                                   "Your schedule changed", f"{text}. Check My schedule.", "roster",
                                   f"/rosters/my-schedule?publication={publication_id}")
        sent += 1
    return sent


def _period_changes(db: Session, period: RosterPeriod, data) -> List[_Cell]:
    query = db.query(RosterDraft).filter(RosterDraft.company_id == period.company_id,
                                         RosterDraft.roster_period_id == period.id)
    if getattr(data, "employee_ids", None):
        query = query.filter(RosterDraft.employee_id.in_(list(data.employee_ids)))
    if getattr(data, "date_from", None):
        query = query.filter(RosterDraft.shift_date >= data.date_from)
    if getattr(data, "date_to", None):
        query = query.filter(RosterDraft.shift_date <= data.date_to)
    drafts = query.order_by(RosterDraft.employee_id, RosterDraft.shift_date, RosterDraft.entry_seq).all()
    cells = []
    for draft in drafts:
        cells.append(_Cell(draft.employee_id, draft.shift_date, draft.entry_seq,
                           draft.kind if draft.action == "UPSERT" else None, draft.shift_id, None,
                           draft.custom_start_time, draft.custom_end_time, draft.custom_segments, draft.site_id,
                           draft.planned_work_mode, draft.source, draft.note,
                           planned_shift_id=draft.planned_shift_id if draft.action == "UPSERT" else None))
        cells[-1].draft = draft  # type: ignore[attr-defined]
    return cells


SOD_SETTING = "shift.roster.planner_cannot_publish"


def _publisher_user_ids(db: Session, company_id: int, employees: Sequence[int]) -> List[int]:
    """Active users holding ``roster:publish`` at a scope covering every employee of the publication."""
    from app.core.principal import build_principal
    from app.models.user import User
    from app.payroll.services.approvals import _permission_holder_user_ids

    company = db.get(Company, company_id)
    found = []
    for user_id in _permission_holder_user_ids(db, PUBLISH):
        user = db.query(User).filter(User.company_id == company_id, User.id == user_id).one_or_none()
        if user is None or user.status != "active":
            continue
        holder = build_principal(db, user, None, company)
        if not holder.has(PUBLISH):
            continue
        if holder.is_company_admin or holder.has_company_scope(PUBLISH):
            found.append(user_id)
            continue
        scope = employee_scope(db, holder, PUBLISH)
        covered = {e for (e,) in db.query(Employee.id).filter(Employee.company_id == company_id,
                                                               Employee.id.in_(employees), scope.clause(Employee.id))}
        if covered == set(employees):
            found.append(user_id)
    return sorted(set(found))


def _notify_sod_unavailable(db: Session, period: RosterPeriod, planner_ids: Iterable[int] = ()) -> None:
    """``roster.sod_unavailable``: the ``shift:manage`` COMPANY holders are told to grant ``roster:publish`` to someone
    else or to switch the rule off, ``planner_ids`` to ask for that; one unread notice per person and period (the
    link is the dedup)."""
    from app.core.principal import build_principal
    from app.models.notification import Notification
    from app.models.user import User
    from app.payroll.services.approvals import _permission_holder_user_ids
    from app.services.notification_service import NotificationService

    company = db.get(Company, period.company_id)
    link = f"/rosters/{period.id}?sod=unavailable"
    managers = set()
    for user_id in _permission_holder_user_ids(db, "shift:manage"):
        user = db.query(User).filter(User.company_id == period.company_id, User.id == user_id).one_or_none()
        if user is None or user.status != "active":
            continue
        manager = build_principal(db, user, None, company)
        if manager.is_company_admin or manager.has_company_scope("shift:manage"):
            managers.add(user_id)
    for user_id in sorted(managers | set(planner_ids)):
        if db.query(Notification.id).filter(Notification.user_id == user_id,
                                            Notification.action_url == link, Notification.is_read.is_(False)).first():
            continue
        NotificationService.create(db, user_id, f"Roster {period.code} has nobody else to publish it",
                                   "The planner-cannot-publish rule is on and no second person holds Publish "
                                   "schedules for this roster. " + ("Grant it to someone or switch the rule off."
                                                                    if user_id in managers else
                                                                    "Ask an administrator to grant it to someone or "
                                                                    "to switch the rule off."), "roster", link)


def _sod_planner_publish(db: Session, principal: Principal, period: RosterPeriod, changes: Sequence[Any],
                         employees: Sequence[int]) -> None:
    """``shift.roster.planner_cannot_publish`` (03-shifts/02 §2.4, §5.2; EC AS-ROS-11, GV-AS-ROS-16): the last saver of
    the drafts cannot publish them — 403 ``SOD_PLANNER_PUBLISH``, a fixed safeguard ``roster:exempt`` does not lift.
    When no second eligible publisher remains, the ``shift:manage`` COMPANY holders are told (``roster.sod_unavailable``)
    to grant ``roster:publish`` to someone else or to switch the setting off."""
    from app.services import shift_requests

    if not employees or not shift_requests._setting(db, period.company_id, SOD_SETTING, employees[0], period.start_date):
        return
    drafts = [c.draft for c in changes if getattr(c, "draft", None) is not None]
    latest = max(drafts, key=lambda d: (d.updated_at, d.id), default=None)
    if latest is None or latest.created_by_user_id != principal.user_id:
        return
    others = [u for u in _publisher_user_ids(db, period.company_id, employees) if u != principal.user_id]
    if not others:
        _notify_sod_unavailable(db, period)
        db.commit()  # the notice survives the refusal below (IG-AS-143)
    raise AppError(403, "The person who saved these drafts cannot also publish them.", "SOD_PLANNER_PUBLISH",
                   extra={"details": {"eligible_publishers": len(others)}})


def sod_after_access_change(db: Session, company_id: int) -> List[int]:
    """EC AS-ROS-11 at the moment a ``roster:publish`` assignment ends (03-shifts/02 §1.4; GV-AS-ROS-16), called by the
    access module's reroute hook (``rbac_service.reroute_approvals``: a role removed or its window ended — the daily
    ``access.assignment_expire`` included — a grant removed, a scope narrowed). A period with drafts under
    ``shift.roster.planner_cannot_publish`` whose last draft saver is left without a second ``roster:publish`` holder
    covering the drafted employees tells its planners and the ``shift:manage`` COMPANY holders now, not at the next
    refused publish. Never commits; returns the periods flagged."""
    from app.services import shift_requests

    drafted = [p for (p,) in db.query(RosterDraft.roster_period_id).filter(
        RosterDraft.company_id == company_id).distinct()]
    if not drafted:
        return []
    periods = db.query(RosterPeriod).filter(RosterPeriod.company_id == company_id, RosterPeriod.id.in_(drafted)) \
        .order_by(RosterPeriod.id).all()
    flagged = []
    for period in periods:
        drafts = db.query(RosterDraft).filter(RosterDraft.company_id == company_id,
                                              RosterDraft.roster_period_id == period.id)
        employees = sorted(e for (e,) in drafts.with_entities(RosterDraft.employee_id).distinct())
        if not shift_requests._setting(db, company_id, SOD_SETTING, employees[0], period.start_date):
            continue
        planner = drafts.with_entities(RosterDraft.created_by_user_id).order_by(
            RosterDraft.updated_at.desc(), RosterDraft.id.desc()).limit(1).scalar()
        if planner is None or any(u != planner for u in _publisher_user_ids(db, company_id, employees)):
            continue
        _notify_sod_unavailable(db, period, _planner_user_ids(db, company_id, employees))
        flagged.append(period.id)
    return flagged


APPROVAL_SETTING = "shift.roster.publish_requires_approval"
ENGINE_MODULE, ENGINE_SUBJECT = "roster_publish", "ROSTER_PERIOD"


def approval_pending(db: Session, period: RosterPeriod) -> bool:
    """A publication of the period waits in the engine (spine §4.1: drafts frozen)."""
    if period.approval_request_id is None:
        return False
    from app.models.approvals import ApprovalRequest

    status = db.query(ApprovalRequest.status).filter(ApprovalRequest.company_id == period.company_id,
                                                     ApprovalRequest.id == period.approval_request_id).scalar()
    return status == "PENDING"


def _frozen(db: Session, period: RosterPeriod) -> None:
    if approval_pending(db, period):
        raise AppError(409, "This roster waits for publication approval; its drafts cannot change.",
                       "ROSTER_PUBLISH_PENDING", extra={"details": {"approval_request_id": period.approval_request_id}})


def publish(db: Session, principal: Principal, period_id: int, data, *, validate_only: bool = False,
            approved_hash: Optional[str] = None) -> dict:
    """POST /rosters/{period_id}/publish (§5.1): dry run, gate, write. ``validate_only`` is the planner's
    ``POST /validate`` (a dry run covered by ``roster:plan``, never a write). With
    ``shift.roster.publish_requires_approval`` (§5.2; owner decision IG-AS-150) the planner's publish opens an engine
    request (module ``roster_publish``) with the frozen snapshot hash instead of writing; ``approved_hash`` is the
    engine callback's re-run as the approver."""
    period = load_period(db, principal, period_id)
    changes = _period_changes(db, period, data)
    if not changes:
        last = db.get(RosterPublication, period.last_publication_id) if period.last_publication_id else None
        return {"dry_run": bool(data.dry_run), "nothing_to_publish": True,
                "publication_id": last.id if last is not None else None,
                "snapshot_hash": last.snapshot_hash if last is not None else None, "results": []}
    employees = sorted({c.employee_id for c in changes})
    if validate_only:
        data.dry_run = True
    from app.services import shift_requests

    needs_approval = approved_hash is None and not validate_only and shift_requests._setting(
        db, period.company_id, APPROVAL_SETTING, employees[0], period.start_date)
    if approved_hash is None and not data.dry_run:
        _frozen(db, period)
    _covering(db, principal, PLAN if (validate_only or needs_approval) else PUBLISH, employees)
    lock_employees(db, employees)
    period = load_period(db, principal, period_id, lock=True)
    stale = [c for c in changes if getattr(c, "draft").base_entry_id is not None and (
        db.get(RosterEntry, c.draft.base_entry_id) is None
        or db.get(RosterEntry, c.draft.base_entry_id).version != c.draft.base_entry_version
        or db.get(RosterEntry, c.draft.base_entry_id).withdrawn_at is not None)]
    if stale:
        raise AppError(409, "Some cells changed since they were drafted.", "ROSTER_STALE_DRAFT",
                       extra={"details": {"cells": [{"employee_id": c.employee_id,
                                                     "shift_date": c.shift_date.isoformat(),
                                                     "entry_seq": c.entry_seq} for c in stale]}})
    days = [c.shift_date for c in changes]
    candidate, changed = _candidate(db, period.company_id, changes, min(days) - timedelta(days=SEAM_DAYS),
                                    max(days) + timedelta(days=SEAM_DAYS))
    if len(candidate) > MAX_PUBLISH_ENTRIES:
        raise AppError(422, "Too many entries in one publication; narrow the scope.", "ROSTER_PUBLISH_TOO_LARGE")
    _resolve_versions(db, period.company_id, changed)
    acknowledged = list(data.acknowledged_warnings or [])
    reasons = dict(data.reasons or {})
    exemptions = {k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in (data.exemptions or {}).items()}
    if exemptions:
        _covering(db, principal, EXEMPT, employees)
    outcome = validate_cells(db, period.company_id, candidate, changed, publish_at=clock.utcnow(),
                             acknowledged=acknowledged, reasons=reasons, exemptions=exemptions)
    scope = {"employee_ids": sorted(getattr(data, "employee_ids", None) or employees),
             "from": (getattr(data, "date_from", None) or period.start_date).isoformat(),
             "to": (getattr(data, "date_to", None) or period.end_date).isoformat()}
    final = [c.values(c.entry.version + 1 if c.entry is not None and c.changed else
                      c.entry.version if c.entry is not None else 1) for c in candidate]
    ids = set(scope["employee_ids"])
    expected_hash = snapshot_hash(v for v in final if v["employee_id"] in ids
                                  and scope["from"] <= v["shift_date"].isoformat() <= scope["to"])
    if data.dry_run:
        return {"dry_run": True, "changes": [c.values(0) | {"change_kind": "WITHDRAWN" if c.kind is None else
                                                             "UPDATED" if c.entry is not None else "CREATED"}
                                             for c in changed],
                "results": [_result_json(r) for r in outcome.results],
                "blocking": [_result_json(r) for r in outcome.blocking],
                "snapshot_hash": expected_hash, "requires_approval": bool(needs_approval)}
    if approved_hash is not None and approved_hash != expected_hash:
        raise AppError(409, "The roster changed since it was sent for approval; submit it again.",
                       "ROSTER_STALE_DRAFT", extra={"details": {"approved_hash": approved_hash}})
    if needs_approval:
        _gate(outcome, principal, exemptions, db)  # only a publishable roster goes to the approver
        return _request_approval(db, principal, period, data, scope, employees, expected_hash, acknowledged, reasons,
                                 exemptions, outcome)
    if approved_hash is None:
        _sod_planner_publish(db, principal, period, changes, employees)
    _gate(outcome, principal, exemptions, db)
    publication = _write(db, principal, period.company_id, kind="PERIOD", period=period, candidate=candidate,
                         changed=changed, scope=scope, outcome=outcome, acknowledged=acknowledged, reasons=reasons,
                         exemptions=exemptions)
    for change in changes:
        db.delete(change.draft)
    db.flush()
    left = db.query(func.count(RosterDraft.id)).filter(RosterDraft.company_id == period.company_id,
                                                       RosterDraft.roster_period_id == period.id).scalar()
    if not left:
        period.status = "PUBLISHED"
    period.last_published_at = publication.published_at
    period.last_publication_id = publication.id
    period.row_version += 1
    db.flush()
    return {"dry_run": False, "publication_id": publication.id, "snapshot_hash": publication.snapshot_hash,
            "entries_created": publication.entries_created, "entries_updated": publication.entries_updated,
            "entries_withdrawn": publication.entries_withdrawn,
            "min_notice_hours": str(publication.min_notice_hours) if publication.min_notice_hours is not None else None,
            "results": [_result_json(r) for r in outcome.results], "period": serialize_period(period, left)}


def _request_approval(db: Session, principal: Principal, period: RosterPeriod, data, scope: dict, employees: List[int],
                      expected_hash: str, acknowledged: List[str], reasons: dict, exemptions: dict,
                      outcome: ValidationOutcome) -> dict:
    """§5.2: open the ``roster_publish`` engine request — approvers hold ``roster:publish`` covering every employee
    (``party_employee_ids``), never the maker (``maker_user_ids``); the request carries the frozen preview hash and
    what the publish needs (acknowledged warnings, reasons, exemptions, scope). Drafts stay frozen while it waits."""
    from app.payroll.services import approvals

    request = approvals.submit(
        db, principal, module=ENGINE_MODULE, subject_type=ENGINE_SUBJECT, subject_id=period.id,
        subject_employee_id=employees[0],
        context={"snapshot_hash": expected_hash, "scope": scope, "acknowledged_warnings": acknowledged,
                 "reasons": reasons, "exemptions": exemptions, "period_code": period.code,
                 "employee_ids": list(getattr(data, "employee_ids", None) or []),
                 "date_from": getattr(data, "date_from", None), "date_to": getattr(data, "date_to", None),
                 "maker_user_ids": [principal.user_id], "party_employee_ids": employees},
        attributes={"period_code": period.code})
    period.requires_approval = True
    period.approval_request_id = request.id
    period.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, MODULE, "ROSTER_PUBLISH_REQUESTED", f"Roster {period.code} sent for approval",
                        "roster_period", period.id, details={"approval_request_id": request.id,
                                                             "snapshot_hash": expected_hash})
    _emit(db, "roster.publish.requested", {"roster_period_id": period.id, "approval_request_id": request.id})
    return {"dry_run": False, "pending_approval": True, "approval_request_id": request.id,
            "snapshot_hash": expected_hash, "results": [_result_json(r) for r in outcome.results],
            "period": serialize_period(period)}


def _publication_decided(db: Session, principal: Optional[Principal], request) -> None:
    """Engine callback (module ``roster_publish``): APPROVED → the publish runs as the approver with the approved
    hash (409 ROSTER_STALE_DRAFT when the drafts no longer match); REJECTED / RETURNED / CANCELLED → the drafts are
    unfrozen (the request is no longer PENDING)."""
    from types import SimpleNamespace

    period = (db.query(RosterPeriod).filter(RosterPeriod.company_id == request.company_id,
                                            RosterPeriod.id == request.subject_id).with_for_update().first())
    if period is None or period.approval_request_id != request.id:
        return
    if request.status == "APPROVED":
        if principal is None:
            raise conflict("A roster publication is approved by a person.", "APPROVER_REQUIRED")
        ctx = request.context or {}
        data = SimpleNamespace(
            dry_run=False, acknowledged_warnings=ctx.get("acknowledged_warnings") or [],
            reasons=ctx.get("reasons") or {}, exemptions=ctx.get("exemptions") or {},
            employee_ids=ctx.get("employee_ids") or None,
            date_from=date.fromisoformat(ctx["date_from"]) if ctx.get("date_from") else None,
            date_to=date.fromisoformat(ctx["date_to"]) if ctx.get("date_to") else None)
        publish(db, principal, period.id, data, approved_hash=ctx.get("snapshot_hash"))
        return
    if request.status in ("REJECTED", "RETURNED", "CANCELLED"):
        period.row_version += 1
        db.flush()
        AuditLogService.log(db, principal, MODULE, "ROSTER_PUBLISH_DECLINED",
                            f"Roster {period.code} publication {request.status.lower()}", "roster_period", period.id,
                            company_id=period.company_id, actor_type="user" if principal is not None else "system")


def _register_engine_callback() -> None:
    from app.payroll.services import approvals

    approvals.register_callback(ENGINE_MODULE, ENGINE_SUBJECT, _publication_decided)


_register_engine_callback()


def direct_publish(db: Session, principal: Principal, data) -> dict:
    """POST /rosters/direct-publish (§5.5): a few entries without a period (day overrides, corrections)."""
    if len(data.cells) > MAX_DRAFT_CELLS:
        raise AppError(422, f"Publish at most {MAX_DRAFT_CELLS} cells at a time.", "TOO_MANY_ITEMS")
    employees = sorted({c.employee_id for c in data.cells})
    _covering(db, principal, PUBLISH, employees)
    shifts = {s.id: s for s in db.query(Shift).filter(Shift.company_id == principal.company_id, Shift.id.in_(
        [c.shift_id for c in data.cells if c.shift_id] or [0]))}
    for cell in data.cells:
        if cell.shift_id is not None and (cell.shift_id not in shifts or shifts[cell.shift_id].status != "ACTIVE"):
            raise AppError(422, "The shift is archived or unknown.", "SHIFT_ARCHIVED")
    lock_employees(db, employees)
    changes = [_Cell(c.employee_id, c.shift_date, c.entry_seq, c.kind if c.action == "UPSERT" else None, c.shift_id,
                     None, c.custom_start_time, c.custom_end_time, None, getattr(c, "site_id", None),
                     getattr(c, "planned_work_mode", None), getattr(c, "source", None) or "DAY_OVERRIDE",
                     getattr(c, "note", None)) for c in data.cells]
    days = [c.shift_date for c in changes]
    candidate, changed = _candidate(db, principal.company_id, changes, min(days) - timedelta(days=SEAM_DAYS),
                                    max(days) + timedelta(days=SEAM_DAYS))
    if not changed:
        return {"dry_run": data.dry_run, "nothing_to_publish": True, "results": []}
    _resolve_versions(db, principal.company_id, changed)
    exemptions = {k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in (data.exemptions or {}).items()}
    outcome = validate_cells(db, principal.company_id, candidate, changed, publish_at=clock.utcnow(),
                             acknowledged=data.acknowledged_warnings or [], reasons=data.reasons or {},
                             exemptions=exemptions)
    if data.dry_run:
        return {"dry_run": True, "results": [_result_json(r) for r in outcome.results],
                "blocking": [_result_json(r) for r in outcome.blocking]}
    _gate(outcome, principal, exemptions, db)
    scope = {"employee_ids": employees, "from": min(days).isoformat(), "to": max(days).isoformat()}
    publication = _write(db, principal, principal.company_id, kind="DIRECT", period=None, candidate=candidate,
                         changed=changed, scope=scope, outcome=outcome, acknowledged=data.acknowledged_warnings or [],
                         reasons=data.reasons or {}, exemptions=exemptions, reason_code=getattr(data, "reason_code", None))
    return {"dry_run": False, "publication_id": publication.id, "snapshot_hash": publication.snapshot_hash,
            "entries_created": publication.entries_created, "entries_updated": publication.entries_updated,
            "entries_withdrawn": publication.entries_withdrawn,
            "results": [_result_json(r) for r in outcome.results]}


# =============================================================================================== reads
def my_schedule(db: Session, principal: Principal, start: Optional[date], end: Optional[date]) -> dict:
    """GET /me/schedule: the caller's published entries and resolved shifts with times in the work zone; the range
    defaults to the employee-local today and the 13 days after it."""
    if principal.employee_id is None:
        raise not_found("Employee profile")
    if start is None:
        from app.services.org_structure_service import employee_local_today

        start = employee_local_today(db, db.get(Employee, principal.employee_id))
    end = end or start + timedelta(days=13)
    if end < start or (end - start).days > 61:
        raise AppError(400, "Choose a range of at most 62 days.", "INVALID_RANGE")
    from app.services import shift_resolution

    resolved = shift_resolution.shifts_for_many(db, principal.company_id, [principal.employee_id], start, end)
    entries = entries_many(db, principal.company_id, [principal.employee_id], start, end)
    codes = dict(db.query(Shift.id, Shift.code).filter(Shift.company_id == principal.company_id).all())
    days = []
    day = start
    while day <= end:
        item = resolved[(principal.employee_id, day)]
        rows = entries.get((principal.employee_id, day), [])
        days.append({"date": day.isoformat(),
                     "source": item.source if item is not None else ("ROSTER" if rows else "NONE"),
                     "kind": "OFF" if rows and all(r.kind == "OFF" for r in rows) else ("WORK" if item else None),
                     "shift_id": item.shift_id if item is not None else None,
                     "shift_code": codes.get(item.shift_id) if item is not None else None,
                     "start_time": item.version.start_time if item is not None else None,
                     "end_time": item.version.end_time if item is not None else None,
                     "anchor_timezone": item.version.anchor_timezone if item is not None else None,
                     "work_timezone": item.work_timezone if item is not None else None,
                     "entries": [serialize_entry(r) for r in rows]})
        day += ONE_DAY
    return {"employee_id": principal.employee_id, "from": start.isoformat(), "to": end.isoformat(), "days": days}


def _emit(db: Session, topic: str, payload: dict) -> None:
    if current_company_id(db) is not None:
        outbox.emit(db, topic, payload)


def reversion_entries(db: Session, principal: Principal, shift_id: int, version: ShiftVersion, start: date,
                      end: Optional[date]) -> Optional[dict]:
    """A new shift version re-versions the published future entries of that shift it covers (03-shifts/01 §3.5
    step 5; GV-AS-SHF-14): entries whose instance changes get one DIRECT publication (version + 1, the new
    ``shift_version_id``, change rows ``SHIFT_VERSION_CHANGED`` / EMPLOYER with notice hours); entries whose instance
    is identical are only re-pointed (no change row)."""
    from app.services import roster_validation

    query = db.query(RosterEntry).filter(RosterEntry.company_id == principal.company_id,
                                         RosterEntry.shift_id == shift_id, RosterEntry.kind == "WORK",
                                         RosterEntry.withdrawn_at.is_(None), RosterEntry.custom_start_time.is_(None),
                                         RosterEntry.shift_date >= start)
    if end is not None:
        query = query.filter(RosterEntry.shift_date <= end)
    entries = query.order_by(RosterEntry.employee_id, RosterEntry.shift_date).all()
    if not entries:
        return None
    # EC AS-PAY-15 (owner decision IG-AS-150): entries on INPUT_LOCKED / FINALIZED payroll dates keep their old
    # version (evaluation uses the entry's stored version); only OPEN dates are re-versioned or re-pointed.
    states = roster_validation.period_states(db, principal.company_id, [_published_cell(e) for e in entries])
    locked = [e for e in entries if states.get(e.shift_date) in (roster_validation.rules.INPUT_LOCKED,
                                                                  roster_validation.rules.FINALIZED)]
    entries = [e for e in entries if e not in locked]
    if not entries:
        return {"publication_id": None, "entries_reversioned": 0, "entries_repointed": 0,
                "entries_left_on_previous_version": len(locked)}
    cells = [_published_cell(e) for e in entries]
    zones = roster_validation.zones_for(db, principal.company_id, cells)
    versions = roster_validation._Versions(db, principal.company_id, [shift_id])
    changed: List[_Cell] = []
    repointed = 0
    for cell, entry in zip(cells, entries):
        zone = zones.get((cell.employee_id, cell.shift_date), "UTC")
        old = roster_validation._instance(versions, cell.values(entry.version), zone)
        cell.shift_version_id = version.id
        new = roster_validation._instance(versions, cell.values(entry.version), zone)
        same = old is not None and new is not None and (old.start_at, old.end_at, old.scheduled_net_minutes) == (
            new.start_at, new.end_at, new.scheduled_net_minutes)
        if same:
            entry.shift_version_id = version.id  # system re-point (EC AS-SHF-06)
            repointed += 1
            continue
        cell.changed = True
        changed.append(cell)
    publication = None
    if changed:
        days = [c.shift_date for c in changed]
        publication = _write(db, principal, principal.company_id, kind="DIRECT", period=None, candidate=changed,
                             changed=changed, scope={"employee_ids": sorted({c.employee_id for c in changed}),
                                                     "from": min(days).isoformat(), "to": max(days).isoformat()},
                             outcome=ValidationOutcome(), acknowledged=[], reasons={}, exemptions={},
                             reason_code="SHIFT_VERSION_CHANGED")
    db.flush()
    return {"publication_id": publication.id if publication is not None else None,
            "entries_reversioned": len(changed), "entries_repointed": repointed,
            "entries_left_on_previous_version": len(locked)}


# =============================================================================================== jobs
PUBLISH_DUE_WARNING_DAYS = 2  # roster.publish_reminders: due within this many days, or overdue


def close_due_periods(db: Session, company_id: int, local_date: date) -> dict:
    """Job ``roster.close_periods`` (DAILY, daily-local; 03-shifts/02 §4.2): a PUBLISHED period whose end date is
    before the local date and none of whose dates is still OPEN in payroll becomes CLOSED (read-only; later changes
    only by DIRECT publications with reason). A period whose employees belong to no pay group has no payroll state to
    wait for (IG-AS-139)."""
    from types import SimpleNamespace

    from app.services import roster_validation

    closed = []
    rows = db.query(RosterPeriod).filter(RosterPeriod.company_id == company_id, RosterPeriod.status == "PUBLISHED",
                                         RosterPeriod.end_date < local_date).with_for_update(skip_locked=True).all()
    for period in rows:
        employees = _owner_employees(db, period)
        days = [period.start_date + timedelta(days=i) for i in range((period.end_date - period.start_date).days + 1)]
        grouped = employees and db.query(EmployeeAssignment.id).filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(employees),
            EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.pay_group_id.isnot(None)).first()
        if grouped:
            cells = [SimpleNamespace(employee_id=e, shift_date=days[0]) for e in employees] + [
                SimpleNamespace(employee_id=employees[0], shift_date=d) for d in days]
            states = roster_validation.period_states(db, company_id, cells)
            if any(d not in states for d in days):
                continue  # a date is still OPEN in payroll
        period.status = "CLOSED"
        period.row_version += 1
        closed.append(period.id)
        AuditLogService.log(db, None, MODULE, "ROSTER_PERIOD_CLOSED", f"Roster period {period.code} closed",
                            "roster_period", period.id, company_id=company_id, actor_type="system")
        _emit(db, "roster.period.closed", {"roster_period_id": period.id})
    db.flush()
    return {"closed": closed}


SHIFT_REMINDER_MINUTES = 60  # notify.roster.shift_reminder_minutes until the setting exists (FU-AS-53)


def shift_reminders(db: Session, company_id: int, now: Optional[datetime] = None) -> dict:
    """Job ``roster.shift_reminders`` (HOURLY; spine §9.3 ``roster.shift_reminder``): the rostered employee hears about
    a published WORK entry that starts within the next ``SHIFT_REMINDER_MINUTES`` — once per entry version (dedup
    ``shiftrem:{roster_entry_id}:{version}``, carried in the action link), so a changed shift is reminded again and a
    missed hourly run is caught up by the next one."""
    from app.models.notification import Notification
    from app.services import roster_validation
    from app.services.notification_service import NotificationService

    now = now or clock.utcnow()
    until = now + timedelta(minutes=SHIFT_REMINDER_MINUTES)
    day = now.date()
    entries = (db.query(RosterEntry).join(Employee, (Employee.company_id == RosterEntry.company_id)
                                          & (Employee.id == RosterEntry.employee_id))
               .filter(RosterEntry.company_id == company_id, RosterEntry.kind == "WORK",
                       RosterEntry.withdrawn_at.is_(None), Employee.user_id.isnot(None),
                       RosterEntry.shift_date >= day - ONE_DAY, RosterEntry.shift_date <= day + ONE_DAY).all())
    if not entries:
        return {"checked": 0, "reminded": 0}
    zones = roster_validation.zones_for(db, company_id, entries)
    versions = roster_validation._Versions(db, company_id, {e.shift_id for e in entries if e.shift_id})
    users = dict(db.query(Employee.id, Employee.user_id).filter(
        Employee.company_id == company_id, Employee.id.in_(sorted({e.employee_id for e in entries}))))
    codes = dict(db.query(Shift.id, Shift.code).filter(
        Shift.company_id == company_id, Shift.id.in_(sorted({e.shift_id for e in entries if e.shift_id}) or [0])))
    sent = 0
    for entry in entries:
        zone = zones.get((entry.employee_id, entry.shift_date), "UTC")
        instance = roster_validation._instance(versions, {k: getattr(entry, k) for k in ENTRY_KEYS}, zone)
        if instance is None or not (now < instance.start_at <= until):
            continue
        link = f"/rosters/my-schedule?reminder={entry.id}-{entry.version}"
        user_id = users[entry.employee_id]
        if db.query(Notification.id).filter(Notification.user_id == user_id,
                                            Notification.action_url == link).first() is not None:
            continue
        local = instance.start_at.astimezone(pytz.timezone(zone))
        NotificationService.create(db, user_id, "Your shift starts soon",
                                   f"{codes.get(entry.shift_id) or 'Your shift'} starts at {local:%H:%M} "
                                   f"({local.tzname()}).", "roster", link)
        sent += 1
    db.flush()
    return {"checked": len(entries), "reminded": sent}


def _planner_user_ids(db: Session, company_id: int, employees: Sequence[int]) -> List[int]:
    """Active users holding ``roster:plan`` whose scope reaches at least one employee the period plans for."""
    from app.core.principal import build_principal
    from app.models.user import User
    from app.payroll.services.approvals import _permission_holder_user_ids

    company = db.get(Company, company_id)
    found = []
    for user_id in _permission_holder_user_ids(db, PLAN):
        user = db.query(User).filter(User.company_id == company_id, User.id == user_id).one_or_none()
        if user is None or user.status != "active":
            continue
        principal = build_principal(db, user, None, company)
        if not principal.has(PLAN):
            continue
        if principal.is_company_admin or principal.has_company_scope(PLAN):
            found.append(user_id)
            continue
        scope = employee_scope(db, principal, PLAN)
        if employees and db.query(Employee.id).filter(Employee.company_id == company_id, Employee.id.in_(employees),
                                                      scope.clause(Employee.id)).first():
            found.append(user_id)
    return sorted(set(found))


def publish_reminders(db: Session, company_id: int, local_date: date) -> dict:
    """Job ``roster.publish_reminders`` (DAILY, daily-local): planners of a DRAFT or AMENDING period whose
    ``publish_due_at`` falls within ``PUBLISH_DUE_WARNING_DAYS`` (or has passed) get one in-app ``roster.publish_due``
    notification per day (dedup ``pubdue:{roster_period_id}:{local_date}``, carried in the action link)."""
    from app.models.notification import Notification
    from app.services.notification_service import NotificationService

    horizon = datetime.combine(local_date + timedelta(days=PUBLISH_DUE_WARNING_DAYS + 1), time(0, 0), tzinfo=timezone.utc)
    rows = db.query(RosterPeriod).filter(RosterPeriod.company_id == company_id,
                                         RosterPeriod.status.in_(("DRAFT", "AMENDING")),
                                         RosterPeriod.publish_due_at.isnot(None),
                                         RosterPeriod.publish_due_at < horizon,
                                         RosterPeriod.start_date >= local_date).all()
    sent = 0
    for period in rows:
        link = f"/rosters/{period.id}?due={local_date.isoformat()}"
        overdue = period.publish_due_at <= clock.utcnow()
        for user_id in _planner_user_ids(db, company_id, _owner_employees(db, period)):
            if db.query(Notification.id).filter(Notification.user_id == user_id,
                                                Notification.action_url == link).first() is not None:
                continue
            NotificationService.create(
                db, user_id, f"Roster {period.code} {'is overdue for publishing' if overdue else 'is due for publishing'}",
                f"{period.name}: publish by {period.publish_due_at.date().isoformat()} so people get their notice.",
                "roster", link)
            sent += 1
    db.flush()
    return {"periods": len(rows), "notified": sent}
