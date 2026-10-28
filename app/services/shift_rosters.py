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


def grid(db: Session, principal: Principal, period_id: int) -> dict:
    """The period grid: employees in scope, published entries, drafts and the shift legend (§13)."""
    period = load_period(db, principal, period_id)
    employees = _owner_employees(db, period)
    people = {e.id: e for e in db.query(Employee).filter(Employee.company_id == period.company_id,
                                                         Employee.id.in_(employees or [0]))}
    entries = entries_many(db, period.company_id, employees, period.start_date, period.end_date)
    drafts = db.query(RosterDraft).filter(RosterDraft.company_id == period.company_id,
                                          RosterDraft.roster_period_id == period.id).all()
    shift_ids = {r.shift_id for rows in entries.values() for r in rows if r.shift_id} | {
        d.shift_id for d in drafts if d.shift_id}
    shifts = db.query(Shift).filter(Shift.company_id == period.company_id,
                                    or_(Shift.id.in_(shift_ids or [0]), Shift.status == "ACTIVE")).all()
    from app.services import roster_consents  # EC AS-ROS-06: badge of entries a consent change made invalid

    conflicts = roster_consents.open_conflicts(db, period.company_id, [r.id for rows in entries.values() for r in rows])
    return {
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
        row.note = getattr(cell, "note", None)
        row.reason = getattr(cell, "reason", None)
        row.base_entry_id = base.id if base is not None else None
        row.base_entry_version = base.version if base is not None else None
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

    def values(self, version: int) -> dict:
        return {"employee_id": self.employee_id, "shift_date": self.shift_date, "entry_seq": self.entry_seq,
                "kind": self.kind, "shift_id": self.shift_id, "shift_version_id": self.shift_version_id,
                "custom_start_time": self.custom_start_time, "custom_end_time": self.custom_end_time,
                "custom_segments": self.custom_segments, "site_id": self.site_id,
                "planned_work_mode": self.planned_work_mode, "version": version}


def _published_cell(row: RosterEntry) -> _Cell:
    return _Cell(row.employee_id, row.shift_date, row.entry_seq, row.kind, row.shift_id, row.shift_version_id,
                 row.custom_start_time, row.custom_end_time, row.custom_segments, row.site_id, row.planned_work_mode,
                 row.source, row.note, entry=row)


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


def _gate(outcome: ValidationOutcome, principal: Principal, exemptions: Mapping[str, Any]) -> None:
    if exemptions and not principal.has(EXEMPT):
        raise forbidden("Publishing with an exemption needs the Publish with exemption permission.")
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
           initiated_by: str = "EMPLOYER", reason_code: Optional[str] = None) -> RosterPublication:
    """Write one publication: entries, change rows, the append-only publication, change log, audit, outbox."""
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
                                note=cell.note)
            db.add(entry)
            change_kind = "CREATED"
        else:
            entry = cell.entry
            for key in ("kind", "shift_id", "shift_version_id", "custom_start_time", "custom_end_time",
                        "custom_segments", "site_id", "planned_work_mode"):
                setattr(entry, key, getattr(cell, key))
            entry.source, entry.note = cell.source, cell.note
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
                                 initiated_by=initiated_by, reason_code=reason_code))
        by_employee.setdefault(cell.employee_id, []).append(cell.shift_date)
    db.flush()
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
                           draft.planned_work_mode, draft.source, draft.note))
        cells[-1].draft = draft  # type: ignore[attr-defined]
    return cells


def publish(db: Session, principal: Principal, period_id: int, data, *, validate_only: bool = False) -> dict:
    """POST /rosters/{period_id}/publish (§5.1): dry run, gate, write. ``validate_only`` is the planner's
    ``POST /validate`` (a dry run covered by ``roster:plan``, never a write)."""
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
    _covering(db, principal, PLAN if validate_only else PUBLISH, employees)
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
    if data.dry_run:
        final = [c.values(c.entry.version + 1 if c.entry is not None and c.changed else
                          c.entry.version if c.entry is not None else 1) for c in candidate]
        ids = set(scope["employee_ids"])
        return {"dry_run": True, "changes": [c.values(0) | {"change_kind": "WITHDRAWN" if c.kind is None else
                                                             "UPDATED" if c.entry is not None else "CREATED"}
                                             for c in changed],
                "results": [_result_json(r) for r in outcome.results],
                "blocking": [_result_json(r) for r in outcome.blocking],
                "snapshot_hash": snapshot_hash(v for v in final if v["employee_id"] in ids
                                               and scope["from"] <= v["shift_date"].isoformat() <= scope["to"])}
    _gate(outcome, principal, exemptions)
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
    _gate(outcome, principal, exemptions)
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
            "entries_reversioned": len(changed), "entries_repointed": repointed}
