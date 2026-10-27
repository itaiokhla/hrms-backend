"""Shift catalogue, versions and assignment rules (AS-M1; 03-shifts/01 §2-§3, §12-§14; AS 11 §6.2).

Shifts carry their times in effective-dated versions (``shift_versions``); the ``shifts`` legacy columns mirror the
version valid on the owner's local today. A new version starts no earlier than that today (guard A-24, trigger
``shift_versions_guard``); the version valid on F - 1 ends at F - 1, a scheduled version starting at F is replaced, a
later scheduled one bounds the new one, and a temporary change (``effective_to``) gets a return copy. Every change
re-resolves the affected employees' dates (``shift_resolution``), writes ``log_change('shift_version' |
'shift_rule')`` per employee, enqueues ``leave.recalculate`` when that handler exists (LH), refreshes the mirrors
when today is covered, audits and emits ids-only outbox events.

Services never commit: routes own the transaction through ``idempotency.execute`` or an explicit commit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pytz
from sqlalchemy import func, or_
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.authz import employee_scope
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.permission_registry import Scope
from app.core.principal import Principal
from app.database.db import current_company_id
from app.models.company import Company
from app.models.employee import Employee, Shift
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_runs import PayPeriod
from app.models.shifts import (
    COLOR_TOKENS, DEFAULT_RULE_PRIORITY, ShiftAssignmentRule, ShiftVersion, version_content, version_hash,
)
from app.payroll import policy_types as pt
from app.payroll.services import jobs, outbox
from app.payroll.services.time_inputs import log_change
from app.services import shift_engine as engine
from app.services import shift_resolution as resolution
from app.services.audit_log_service import AuditLogService

READ, MANAGE, ASSIGN = "shift:read", "shift:manage", "shift:assign"
MODULE = "Shifts"
ONE_DAY = timedelta(days=1)
IMPACT_DAYS = 62  # preview / effects window from the effective date (the explain range cap)
SAMPLE = 20
LIST_SORTS = {"code": Shift.code, "name": Shift.name, "kind": Shift.kind, "status": Shift.status}
RULE_BULK_PRIORITY = 1000  # employee band (03-shifts/01 §12.3)
RECALC_JOB = "leave.recalculate"
VERSION_FIELDS = (
    "start_time", "end_time", "segments", "break_rules", "unpaid_break_minutes", "half_split_time", "core_start_time",
    "core_end_time", "expected_net_minutes", "grace_in_minutes", "grace_out_minutes", "early_in_window_minutes",
    "late_out_window_minutes", "anchor_timezone", "safety_critical", "outdoor", "long_duty",
)


# ------------------------------------------------------------------------------------------- zones and dates
def owner_zone(db: Session, shift: Shift) -> str:
    """Branch zone, else legal-entity zone, else company zone (03-shifts/01 §2.3)."""
    branch = db.get(Branch, shift.branch_id) if shift.branch_id else None
    entity_id = shift.legal_entity_id or (branch.legal_entity_id if branch is not None else None)
    entity = db.get(LegalEntity, entity_id) if entity_id else None
    company = db.get(Company, shift.company_id)
    for zone in (getattr(branch, "timezone", None), getattr(entity, "timezone", None), getattr(company, "timezone", None)):
        if zone and zone in pytz.all_timezones_set:
            return zone
    return "UTC"


def owner_today(db: Session, shift: Shift) -> date:
    return clock.utcnow().astimezone(pytz.timezone(owner_zone(db, shift))).date()


def _company_today(db: Session, company_id: int) -> date:
    company = db.get(Company, company_id)
    zone = company.timezone if company is not None and company.timezone in pytz.all_timezones_set else "UTC"
    return clock.utcnow().astimezone(pytz.timezone(zone)).date()


# --------------------------------------------------------------------------------------------- serializers
def serialize_version(row: ShiftVersion) -> dict:
    return {
        "id": row.id, "shift_id": row.shift_id, "version_no": row.version_no,
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        **{f: getattr(row, f) for f in VERSION_FIELDS},
        "crosses_midnight": row.crosses_midnight,
        "duration_hours": str(row.duration_hours) if row.duration_hours is not None else None,
        "content_hash": row.content_hash, "reason": row.reason,
        "voided_at": row.voided_at.isoformat() if row.voided_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def serialize_shift(shift: Shift, current: Optional[ShiftVersion] = None) -> dict:
    return {
        "id": shift.id, "code": shift.code, "name": shift.name, "kind": shift.kind, "color_token": shift.color_token,
        "legal_entity_id": shift.legal_entity_id, "branch_id": shift.branch_id, "status": shift.status,
        "description": shift.description, "row_version": shift.row_version,
        # Legacy mirror (kept for GET /shifts consumers).
        "start_time": shift.start_time, "end_time": shift.end_time,
        "duration_hours": shift.duration_hours, "unpaid_break_minutes": shift.unpaid_break_minutes,
        "half_split_time": shift.half_split_time, "is_flexible": shift.is_flexible, "is_active": shift.is_active,
        "current_version_id": shift.current_version_id,
        "current_version": serialize_version(current) if current is not None else None,
    }


# -------------------------------------------------------------------------------------------- permissions
def _scope_covers_shift(principal: Principal, code: str, shift_branch: Optional[int], shift_entity: Optional[int],
                        branch_entity: Optional[int]) -> bool:
    """Owner-scope rule of §15: tenant-wide shifts need COMPANY; an entity shift its LE (or COMPANY); a branch
    shift its branch, its LE or COMPANY."""
    if principal.is_company_admin or principal.has_company_scope(code):
        return True
    if code == MANAGE and principal.has_company_scope("department:manage"):
        return True  # alias kept for one release (AS 06 S2)
    entities = principal.scope_targets(code, Scope.LEGAL_ENTITY.value)
    branches = principal.scope_targets(code, Scope.BRANCH.value)
    if shift_branch is not None:
        return shift_branch in branches or (branch_entity is not None and branch_entity in entities)
    if shift_entity is not None:
        return shift_entity in entities
    return False


def _require_manage(db: Session, principal: Principal, shift: Shift) -> None:
    branch = db.get(Branch, shift.branch_id) if shift.branch_id else None
    if not _scope_covers_shift(principal, MANAGE, shift.branch_id, shift.legal_entity_id,
                               branch.legal_entity_id if branch is not None else None):
        raise forbidden("Managing this shift needs the Manage shifts permission for its owner.")


def _readable_shift_ids(db: Session, principal: Principal) -> Optional[set]:
    """None = the whole catalogue; else the shifts that apply to the caller (SELF-only readers, §15)."""
    scopes = principal.scopes(READ)
    if principal.is_company_admin or any(s != Scope.SELF.value for s in scopes):
        return None
    if Scope.SELF.value not in scopes or principal.employee_id is None:
        return set()
    today = _company_today(db, principal.company_id)
    resolved = resolution.shifts_for_many(db, principal.company_id, [principal.employee_id], today,
                                          today + timedelta(days=31))
    return {r.shift_id for r in resolved.values() if r is not None}


def load_shift(db: Session, principal: Principal, shift_id: int, *, lock: bool = False) -> Shift:
    query = db.query(Shift).filter(Shift.company_id == principal.company_id, Shift.id == shift_id)
    shift = (query.with_for_update() if lock else query).first()
    readable = _readable_shift_ids(db, principal) if shift is not None else set()
    if shift is None or (readable is not None and shift.id not in readable):
        raise not_found("Shift")
    return shift


def _versions(db: Session, shift: Shift, *, include_void: bool = False) -> List[ShiftVersion]:
    query = db.query(ShiftVersion).filter(ShiftVersion.company_id == shift.company_id,
                                          ShiftVersion.shift_id == shift.id)
    if not include_void:
        query = query.filter(ShiftVersion.voided_at.is_(None))
    return query.order_by(ShiftVersion.effective_from, ShiftVersion.version_no).all()


def _version_on(rows: Sequence[ShiftVersion], day: date) -> Optional[ShiftVersion]:
    return next((r for r in rows if r.effective_from <= day and (r.effective_to is None or day <= r.effective_to)),
                None)


# ------------------------------------------------------------------------------------------------- reads
def list_shifts(db: Session, principal: Principal, *, q: Optional[str] = None, status: Optional[str] = "ACTIVE",
                kind: Optional[str] = None, branch_id: Optional[int] = None, legal_entity_id: Optional[int] = None,
                sort: Optional[str] = None, skip: int = 0, limit: int = 50) -> dict:
    """The catalogue (BUG-AS-35: ``shift:read`` and a status filter; default ACTIVE, ``status=ALL`` for every row)."""
    query = db.query(Shift).filter(Shift.company_id == principal.company_id)
    readable = _readable_shift_ids(db, principal)
    if readable is not None:
        query = query.filter(Shift.id.in_(sorted(readable) or [0]))
    if status and status != "ALL":
        if status not in ("ACTIVE", "ARCHIVED"):
            raise bad_request("status must be ACTIVE, ARCHIVED or ALL.", "INVALID_FILTER")
        query = query.filter(Shift.status == status)
    if kind:
        query = query.filter(Shift.kind == kind)
    if branch_id is not None:
        query = query.filter(Shift.branch_id == branch_id)
    if legal_entity_id is not None:
        query = query.filter(Shift.legal_entity_id == legal_entity_id)
    if q:
        needle = "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(func.lower(Shift.code).like(needle, escape="\\"),
                                 func.lower(Shift.name).like(needle, escape="\\")))
    key, descending = (sort or "code").lstrip("-"), (sort or "").startswith("-")
    if key not in LIST_SORTS:
        raise bad_request(f"sort must be one of {', '.join(sorted(LIST_SORTS))}.", "INVALID_SORT")
    column = LIST_SORTS[key]
    total = query.count()
    rows = query.order_by(column.desc() if descending else column.asc(), Shift.id).offset(skip).limit(limit).all()
    versions = {v.id: v for v in db.query(ShiftVersion).filter(
        ShiftVersion.company_id == principal.company_id,
        ShiftVersion.id.in_([r.current_version_id for r in rows if r.current_version_id] or [0]))}
    return {"items": [serialize_shift(r, versions.get(r.current_version_id)) for r in rows], "total": total,
            "skip": skip, "limit": limit}


def usage_counts(db: Session, shift: Shift, on: date) -> dict:
    """Live references of §3.9 that exist before AS-M5: active rules, current and future placements."""
    rules = db.query(func.count(ShiftAssignmentRule.id)).filter(
        ShiftAssignmentRule.company_id == shift.company_id, ShiftAssignmentRule.shift_id == shift.id,
        ShiftAssignmentRule.status == "ACTIVE",
        or_(ShiftAssignmentRule.effective_to.is_(None), ShiftAssignmentRule.effective_to >= on)).scalar()
    placements = db.query(func.count(EmployeeAssignment.id)).filter(
        EmployeeAssignment.company_id == shift.company_id, EmployeeAssignment.shift_id == shift.id,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on)).scalar()
    return {"rules": int(rules or 0), "placements": int(placements or 0)}


def get_shift(db: Session, principal: Principal, shift_id: int) -> dict:
    shift = load_shift(db, principal, shift_id)
    rows = _versions(db, shift, include_void=True)
    current = next((r for r in rows if r.id == shift.current_version_id), None)
    return {**serialize_shift(shift, current), "versions": [serialize_version(r) for r in rows],
            "usage": usage_counts(db, shift, owner_today(db, shift)), "owner_today": owner_today(db, shift).isoformat()}


def list_versions(db: Session, principal: Principal, shift_id: int) -> List[dict]:
    shift = load_shift(db, principal, shift_id)
    return [serialize_version(r) for r in _versions(db, shift, include_void=True)]


# --------------------------------------------------------------------------------------- version building
def _issues(problems: Iterable[engine.VersionIssue]) -> AppError:
    issues = [{"field": p.field, "code": p.code, "message": p.message} for p in problems]
    return AppError(422, issues[0]["message"] if issues else "The version is not valid.", "VALIDATION_ERROR",
                    extra={"issues": issues})


def _data(kind: str, values: dict) -> engine.ShiftVersionData:
    return engine.ShiftVersionData(kind=kind, **{f: values.get(f) for f in VERSION_FIELDS
                                                 if f not in ("early_in_window_minutes", "late_out_window_minutes")},
                                   early_in_window_minutes=values.get("early_in_window_minutes", 120),
                                   late_out_window_minutes=values.get("late_out_window_minutes", 240))


def build_values(kind: str, body: Any, on_date: date, zone: str) -> dict:
    """Validated version columns from a ``VersionIn`` body (engine validation, mirror values computed)."""
    values = {f: getattr(body, f) for f in VERSION_FIELDS}
    values["segments"] = list(values["segments"] or [])
    values["break_rules"] = list(values["break_rules"] or [])
    data = _data(kind, values)
    problems = engine.validate_version(data)
    if isinstance(body, _LegacyVersion):
        # Legacy clients never send core hours; a FLEXIBLE legacy shift keeps none, like the migrated version 1
        # of an existing flexible shift (03-shifts/01 migration), so only that requirement is waived here.
        problems = tuple(p for p in problems if not (p.field == "core_start_time" and p.code == "REQUIRED"))
    if problems:
        raise _issues(problems)
    mirror = engine.legacy_mirror(data, on_date, zone)
    values["crosses_midnight"] = bool(data.crosses_midnight) if data.crosses_midnight is not None else False
    values["duration_hours"] = mirror.duration_hours if mirror.duration_hours and mirror.duration_hours > 0 else None
    if values["break_rules"]:
        values["unpaid_break_minutes"] = sum(int(r.get("minutes") or 0) for r in values["break_rules"]
                                             if not r.get("paid"))
    return values


def _apply_mirror(db: Session, shift: Shift, row: Optional[ShiftVersion], zone: str, today: date) -> None:
    if row is None:
        return
    data = resolution.version_data(row, shift.kind)
    mirror = engine.legacy_mirror(data, today, zone)
    legacy_shaped = not row.segments and not row.break_rules and row.expected_net_minutes is None
    shift.start_time, shift.end_time = mirror.start_time, mirror.end_time
    # Legacy-shaped versions mirror their stored values (zero diff for v1 readers, IG-AS-27).
    shift.duration_hours = row.duration_hours if legacy_shaped else mirror.duration_hours
    shift.unpaid_break_minutes = row.unpaid_break_minutes if legacy_shaped else mirror.unpaid_break_minutes
    shift.is_flexible = mirror.is_flexible
    shift.half_split_time = row.half_split_time
    shift.current_version_id = row.id


def refresh_mirrors(db: Session, company_id: int, on: Optional[date] = None, *,
                    shift_ids: Optional[Iterable[int]] = None) -> int:
    """Point every shift's mirror at the version valid on its owner's local today (job ``holiday.link_activate``
    calls this daily, V3-131); returns how many shifts changed."""
    query = db.query(Shift).filter(Shift.company_id == company_id)
    if shift_ids is not None:
        query = query.filter(Shift.id.in_(list(shift_ids) or [0]))
    changed = 0
    for shift in query.order_by(Shift.id):
        zone = owner_zone(db, shift)
        today = on or owner_today(db, shift)
        row = _version_on(_versions(db, shift), today)
        if row is not None and row.id != shift.current_version_id:
            _apply_mirror(db, shift, row, zone, today)
            changed += 1
    db.flush()
    return changed


def _flush_versions(db: Session) -> None:
    """Flush and run the deferred tiling check now, mapping trigger errors to the API codes (AS 11 §4.4)."""
    try:
        with db.begin_nested():
            db.flush()
            db.execute(_SET_IMMEDIATE)
    except (IntegrityError, DBAPIError) as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "shift_versions_gap":
            raise AppError(422, "Versions of a shift must follow each other without a gap.", "SHIFT_VERSION_GAP")
        if constraint in ("shift_versions_in_force", "shift_versions_no_overlap"):
            raise conflict("A version in force cannot change; add a version from today or later.",
                           "SHIFT_VERSION_IN_FORCE")
        raise
    db.execute(_SET_DEFERRED)


from sqlalchemy import text as _text  # noqa: E402

_SET_IMMEDIATE = _text("SET CONSTRAINTS shift_versions_tiling IMMEDIATE")
_SET_DEFERRED = _text("SET CONSTRAINTS shift_versions_tiling DEFERRED")


def _next_no(db: Session, shift: Shift) -> int:
    return int(db.query(func.coalesce(func.max(ShiftVersion.version_no), 0)).filter(
        ShiftVersion.company_id == shift.company_id, ShiftVersion.shift_id == shift.id).scalar()) + 1


def _plan(rows: List[ShiftVersion], start: date, end: Optional[date], today: date, on_overlap: str) -> dict:
    """The tiling after inserting [start, end] (03-shifts/01 §3.4); raises the documented refusals."""
    replaced = [r for r in rows if r.effective_from == start and r.effective_from > today]
    later = sorted((r for r in rows if r.effective_from > start and r not in replaced), key=lambda r: r.effective_from)
    warnings: List[dict] = []
    honoured = True
    if later:
        bound = later[0].effective_from - ONE_DAY
        if end is None:
            end = bound
        elif end > bound:
            if on_overlap != "TRUNCATE":
                raise AppError(409, "The new version would run into a scheduled version.",
                               "SHIFT_VERSION_OVERLAP_SCHEDULED",
                               extra={"details": {"bounding_version_no": later[0].version_no,
                                                  "bounding_effective_from": later[0].effective_from.isoformat()}})
            warnings.append({"code": "VERSION_RANGE_TRUNCATED", "requested_effective_to": end.isoformat(),
                             "applied_effective_to": bound.isoformat()})
            end, honoured = bound, False
    previous = _version_on([r for r in rows if r not in replaced], start - ONE_DAY)
    return_copy = None
    if end is not None and honoured and not any(r.effective_from == end + ONE_DAY for r in later):
        source = _version_on([r for r in rows if r not in replaced], end + ONE_DAY)
        if source is not None:
            next_start = next((r.effective_from for r in later if r.effective_from > end), None)
            return_copy = (source, end + ONE_DAY, next_start - ONE_DAY if next_start else source.effective_to)
    return {"replaced": replaced, "end": end, "previous": previous, "return_copy": return_copy,
            "warnings": warnings}


# ------------------------------------------------------------------------------------------------ effects
def _population(db: Session, company_id: int, shift_id: int, start: date, end: Optional[date],
                **kwargs) -> Dict[int, List[date]]:
    """Employees whose resolved shift is ``shift_id`` on dates of [start, end ∧ start + IMPACT_DAYS)."""
    stop = min(end, start + timedelta(days=IMPACT_DAYS - 1)) if end else start + timedelta(days=IMPACT_DAYS - 1)
    employee_ids = [e for (e,) in db.query(EmployeeAssignment.employee_id).filter(
        EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
        EmployeeAssignment.effective_from <= stop,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start)).distinct()]
    if not employee_ids:
        return {}
    resolved = resolution.shifts_for_many(db, company_id, employee_ids, start, stop, **kwargs)
    found: Dict[int, List[date]] = {}
    for (employee_id, day), item in sorted(resolved.items()):
        if item is not None and item.shift_id == shift_id:
            found.setdefault(employee_id, []).append(day)
    return found


def _payroll_states(db: Session, company_id: int, employee_dates: Dict[int, List[date]]) -> Dict[str, int]:
    """Dates by payroll state of their pay period (OPEN / INPUT_LOCKED / FINALIZED). Interim reading of the pay
    periods until payroll ships ``calendars.period_state_on`` (V3-136): CLOSED = FINALIZED, PROCESSING =
    INPUT_LOCKED, everything else (no pay group, no period) OPEN."""
    counts = {"OPEN": 0, "INPUT_LOCKED": 0, "FINALIZED": 0}
    if not employee_dates:
        return counts
    groups = dict(db.query(EmployeeAssignment.employee_id, EmployeeAssignment.pay_group_id).filter(
        EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
        EmployeeAssignment.employee_id.in_(list(employee_dates))).all())
    periods = db.query(PayPeriod).filter(PayPeriod.company_id == company_id,
                                         PayPeriod.status.in_(("PROCESSING", "CLOSED"))).all()
    for employee_id, days in employee_dates.items():
        group = groups.get(employee_id)
        for day in days:
            state = "OPEN"
            for period in periods:
                if period.pay_group_id == group and period.attendance_start <= day <= period.attendance_end:
                    state = "FINALIZED" if period.status == "CLOSED" else "INPUT_LOCKED"
                    break
            counts[state] += 1
    return counts


def _effects(db: Session, principal: Principal, *, record_type: str, record_id: int, version: int, content: dict,
             population: Dict[int, List[date]], source_kind: str) -> dict:
    """log_change per affected employee (JC-AS-11) and the leave recount for dates >= today (JC-AS-34)."""
    for employee_id, days in sorted(population.items()):
        log_change(db, principal.company_id, record_type=record_type, record_id=record_id, change="CREATED",
                   content=content, employee_id=employee_id, date_from=min(days), date_to=max(days),
                   actor_user_id=principal.user_id)
    recount = False
    if population and jobs.find_handler(RECALC_JOB) is not None:
        jobs.enqueue(db, company_id=principal.company_id, type=RECALC_JOB,
                     payload={"employee_ids": sorted(population), "from": min(min(d) for d in population.values())
                              .isoformat(), "source_kind": source_kind, "source_id": record_id},
                     idempotency_key=f"recalc:shift:{source_kind}:{record_id}:{version}")
        recount = True
    return {"employees": len(population), "leave_recalculation_queued": recount}


def _emit(db: Session, topic: str, payload: dict) -> None:
    if current_company_id(db) is not None:
        outbox.emit(db, topic, payload)


# ------------------------------------------------------------------------------------------------- writes
def _check_scope_refs(db: Session, company_id: int, legal_entity_id: Optional[int], branch_id: Optional[int]
                      ) -> Tuple[Optional[int], Optional[int]]:
    if branch_id is not None:
        branch = db.query(Branch).filter(Branch.company_id == company_id, Branch.id == branch_id).first()
        if branch is None:
            raise not_found("Branch")
        if legal_entity_id is not None and legal_entity_id != branch.legal_entity_id:
            raise bad_request("The branch does not belong to that legal entity.", "ORG_BRANCH_MISMATCH")
        return branch.legal_entity_id, branch.id
    if legal_entity_id is not None and db.query(LegalEntity.id).filter(
            LegalEntity.company_id == company_id, LegalEntity.id == legal_entity_id).first() is None:
        raise not_found("Legal entity")
    return legal_entity_id, None


def _insert_version(db: Session, principal: Principal, shift: Shift, values: dict, start: date, end: Optional[date],
                    number: int, reason: Optional[str]) -> ShiftVersion:
    row = ShiftVersion(company_id=shift.company_id, shift_id=shift.id, version_no=number, effective_from=start,
                       effective_to=end, content_hash=version_hash(values), reason=reason,
                       created_by_user_id=principal.user_id,
                       **{k: v for k, v in values.items() if k in VERSION_FIELDS or k in ("crosses_midnight",
                                                                                          "duration_hours")})
    db.add(row)
    return row


def create_shift(db: Session, principal: Principal, data) -> dict:
    cid = principal.company_id
    entity_id, branch_id = _check_scope_refs(db, cid, data.legal_entity_id, data.branch_id)
    probe = Shift(company_id=cid, legal_entity_id=entity_id, branch_id=branch_id)
    branch = db.get(Branch, branch_id) if branch_id else None
    if not _scope_covers_shift(principal, MANAGE, branch_id, entity_id, branch.legal_entity_id if branch else None):
        raise forbidden("Creating this shift needs the Manage shifts permission for its owner.")
    if db.query(Shift.id).filter(Shift.company_id == cid, Shift.code == data.code).first():
        raise conflict("Another shift already uses this code.", "CONFLICT")
    if db.query(Shift.id).filter(Shift.company_id == cid, func.lower(Shift.name) == data.name.lower()).first():
        raise conflict("A shift with this name already exists.", "CONFLICT")
    probe.company_id = cid
    zone = owner_zone(db, probe)
    today = clock.utcnow().astimezone(pytz.timezone(zone)).date()
    start = data.version.effective_from or today
    if start < today and not data.version.reason:
        raise AppError(422, "A first version dated before today needs a reason.", "VALIDATION_ERROR",
                       extra={"issues": [{"field": "version.reason", "code": "REASON_REQUIRED",
                                          "message": "Give a reason for a backdated first version."}]})
    values = build_values(data.kind, data.version, start, zone)
    data_view = _data(data.kind, values)
    mirror = engine.legacy_mirror(data_view, today, zone)
    shift = Shift(company_id=cid, code=data.code, name=data.name, kind=data.kind, color_token=data.color_token,
                  legal_entity_id=entity_id, branch_id=branch_id, status="ACTIVE", is_active=True,
                  description=data.description, start_time=mirror.start_time, end_time=mirror.end_time,
                  duration_hours=values["duration_hours"], unpaid_break_minutes=values["unpaid_break_minutes"],
                  half_split_time=values["half_split_time"], is_flexible=mirror.is_flexible)
    db.add(shift)
    db.flush()
    row = _insert_version(db, principal, shift, values, start, data.version.effective_to, 1, data.version.reason)
    _flush_versions(db)
    if data.version.effective_to is None or data.version.effective_to >= today:
        if start <= today:
            _apply_mirror(db, shift, row, zone, today)
    db.flush()
    AuditLogService.log(db, principal, MODULE, "SHIFT_SAVED", f"Created shift {shift.code}", "shift", shift.id,
                        details={"after": {"code": shift.code, "name": shift.name, "kind": shift.kind}})
    _emit(db, "shift.version.activated", {"shift_id": shift.id, "shift_version_id": row.id})
    return {**serialize_shift(shift, row), "versions": [serialize_version(row)]}


def update_shift(db: Session, principal: Principal, shift_id: int, data, expected_row_version: Optional[int]) -> dict:
    from app.core.concurrency import check_row_version

    shift = load_shift(db, principal, shift_id, lock=True)
    _require_manage(db, principal, shift)
    check_row_version(shift, expected_row_version)
    if shift.status == "ARCHIVED":
        raise AppError(422, "Restore the shift before editing it.", "SHIFT_ARCHIVED")
    today = owner_today(db, shift)
    in_force = any(r.effective_from <= today for r in _versions(db, shift))
    before = {"name": shift.name, "color_token": shift.color_token, "description": shift.description,
              "code": shift.code, "kind": shift.kind}
    changes = data.model_dump(exclude_unset=True, exclude={"row_version"})
    if "kind" in changes and changes["kind"] != shift.kind:
        if in_force:
            raise AppError(409, "The kind of a shift in use cannot change; create a new shift instead.",
                           "SHIFT_IN_USE", extra={"details": {"reason": "KIND_LOCKED",
                                                              "counts": usage_counts(db, shift, today)}})
    if "code" in changes and changes["code"] != shift.code:
        if in_force:
            raise AppError(409, "The code of a shift in use cannot change.", "SHIFT_IN_USE",
                           extra={"details": {"reason": "CODE_LOCKED"}})
        if db.query(Shift.id).filter(Shift.company_id == shift.company_id, Shift.code == changes["code"],
                                     Shift.id != shift.id).first():
            raise conflict("Another shift already uses this code.", "CONFLICT")
    if "name" in changes and changes["name"] and db.query(Shift.id).filter(
            Shift.company_id == shift.company_id, func.lower(Shift.name) == changes["name"].lower(),
            Shift.id != shift.id).first():
        raise conflict("A shift with this name already exists.", "CONFLICT")
    for key, value in changes.items():
        if value is not None or key == "description":
            setattr(shift, key, value)
    shift.row_version += 1
    db.flush()
    after = {k: getattr(shift, k) for k in before}
    AuditLogService.log(db, principal, MODULE, "SHIFT_SAVED", f"Edited shift {shift.code}", "shift", shift.id,
                        details={"before": before, "after": after})
    return get_shift(db, principal, shift.id)


def set_status(db: Session, principal: Principal, shift_id: int, archive: bool,
               expected_row_version: Optional[int]) -> dict:
    from app.core.concurrency import check_row_version

    shift = load_shift(db, principal, shift_id, lock=True)
    _require_manage(db, principal, shift)
    check_row_version(shift, expected_row_version)
    target = "ARCHIVED" if archive else "ACTIVE"
    if shift.status == target:
        raise AppError(409, f"The shift is already {target.lower()}.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": shift.status}})
    if archive:
        counts = usage_counts(db, shift, owner_today(db, shift))
        if any(counts.values()):
            raise AppError(409, "The shift is still in use.", "SHIFT_IN_USE", extra={"details": {"counts": counts}})
    shift.status = target
    shift.is_active = not archive
    shift.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, MODULE, "SHIFT_ARCHIVED" if archive else "SHIFT_RESTORED",
                        f"{'Archived' if archive else 'Restored'} shift {shift.code}", "shift", shift.id)
    return get_shift(db, principal, shift.id)


def create_version(db: Session, principal: Principal, shift_id: int, body, *, legacy: bool = False) -> dict:
    """POST /shifts/{id}/versions (§3.4); ``body.dry_run`` previews without writing (§3.6)."""
    shift = load_shift(db, principal, shift_id, lock=True)
    _require_manage(db, principal, shift)
    if shift.status == "ARCHIVED":
        raise AppError(422, "Restore the shift before changing its times.", "SHIFT_ARCHIVED")
    zone = owner_zone(db, shift)
    today = clock.utcnow().astimezone(pytz.timezone(zone)).date()
    rows = _versions(db, shift)
    start = body.effective_from or (today + ONE_DAY if legacy else today)
    if start < today:
        in_force_today = _version_on(rows, today)
        next_allowed = today + ONE_DAY if in_force_today is not None and in_force_today.effective_from == today \
            else today
        raise AppError(409, "A version can start no earlier than the shift owner's today.", "SHIFT_VERSION_IN_FORCE",
                       extra={"details": {"next_allowed_from": next_allowed.isoformat(), "zone": zone}})
    if any(r.effective_from == start and r.effective_from <= today for r in rows):
        raise AppError(409, "The version starting today is already in force; start the change tomorrow.",
                       "SHIFT_VERSION_IN_FORCE",
                       extra={"details": {"next_allowed_from": (today + ONE_DAY).isoformat(), "zone": zone}})
    if body.effective_to is not None and body.effective_to < start:
        raise AppError(422, "The end date is before the start date.", "VALIDATION_ERROR",
                       extra={"issues": [{"field": "effective_to", "code": "BEFORE_START",
                                          "message": "The end date is before the start date."}]})
    values = build_values(shift.kind, body, start, zone)
    plan = _plan(rows, start, body.effective_to, today, body.on_overlap)
    old = resolution.version_data(plan["previous"], shift.kind) if plan["previous"] is not None else None
    new = _data(shift.kind, values)
    population = _population(db, shift.company_id, shift.id, start, plan["end"])
    if start == today and population:
        started = _started_today(db, shift.company_id, sorted(population), today)
        if started:
            raise AppError(422, "Some employees already started this shift today; start the change tomorrow.",
                           "SHIFT_VERSION_STARTED", extra={"details": {"counts": {"employees": started}}})
    preview = {
        "versions": _preview_table(rows, plan, start, values),
        "affected_employees": {"count": len(population), "sample": sorted(population)[:SAMPLE]},
        "dates_by_payroll_state": _payroll_states(db, shift.company_id, population),
        "scheduled_minutes": {"old": _minutes(old, start, zone), "new": _minutes(new, start, zone)},
        "warnings": plan["warnings"],
    }
    if body.dry_run:
        return {"dry_run": True, **preview}
    number = _next_no(db, shift)
    for row in plan["replaced"]:
        db.delete(row)
    if plan["previous"] is not None and (plan["previous"].effective_to is None
                                         or plan["previous"].effective_to >= start):
        plan["previous"].effective_to = start - ONE_DAY
    db.flush()
    row = _insert_version(db, principal, shift, values, start, plan["end"], number, body.reason)
    created = [row]
    if plan["return_copy"] is not None:
        source, copy_from, copy_to = plan["return_copy"]
        copy_values = {f: getattr(source, f) for f in VERSION_FIELDS + ("crosses_midnight", "duration_hours")}
        created.append(_insert_version(db, principal, shift, copy_values, copy_from, copy_to, number + 1,
                                       f"Return to version {source.version_no} after a temporary change"))
    _flush_versions(db)
    if start <= today:
        _apply_mirror(db, shift, row, zone, today)
    shift.row_version += 1
    db.flush()
    effects = _effects(db, principal, record_type="shift_version", record_id=row.id, version=row.version_no,
                       content=version_content(values), population=population, source_kind="shift_version")
    AuditLogService.log(db, principal, MODULE, "SHIFT_VERSION_ACTIVATED",
                        f"Shift {shift.code}: version {row.version_no} from {start.isoformat()}", "shift", shift.id,
                        details={"before": serialize_version(plan["previous"]) if plan["previous"] else None,
                                 "after": serialize_version(row)})
    _emit(db, "shift.version.activated", {"shift_id": shift.id, "shift_version_id": row.id})
    return {"dry_run": False, "version": serialize_version(row), "created": [serialize_version(r) for r in created],
            "effects": effects, **{k: preview[k] for k in ("affected_employees", "warnings")}}


def _minutes(version: Optional[engine.ShiftVersionData], day: date, zone: str) -> Optional[int]:
    if version is None:
        return None
    try:
        return engine.scheduled_net_minutes(version, day, zone)
    except engine.ShiftEngineError:
        return None


def _preview_table(rows: List[ShiftVersion], plan: dict, start: date, values: dict) -> List[dict]:
    table = []
    for row in rows:
        if row in plan["replaced"]:
            continue
        end = row.effective_to
        if row is plan["previous"] and (end is None or end >= start):
            end = start - ONE_DAY
        table.append({"version_no": row.version_no, "effective_from": row.effective_from.isoformat(),
                      "effective_to": end.isoformat() if end else None, "start_time": row.start_time,
                      "end_time": row.end_time, "new": False})
    table.append({"version_no": None, "effective_from": start.isoformat(),
                  "effective_to": plan["end"].isoformat() if plan["end"] else None,
                  "start_time": values.get("start_time"), "end_time": values.get("end_time"), "new": True})
    if plan["return_copy"] is not None:
        source, copy_from, copy_to = plan["return_copy"]
        table.append({"version_no": None, "effective_from": copy_from.isoformat(),
                      "effective_to": copy_to.isoformat() if copy_to else None, "start_time": source.start_time,
                      "end_time": source.end_time, "new": True, "return_of": source.version_no})
    return sorted(table, key=lambda r: r["effective_from"])


def _started_today(db: Session, company_id: int, employee_ids: Sequence[int], today: date) -> int:
    """Employees with an attendance session that already started on ``today`` (§3.3 rule 2)."""
    from app.models.attendance import Attendance

    return int(db.query(func.count(func.distinct(Attendance.employee_id))).filter(
        Attendance.company_id == company_id, Attendance.employee_id.in_(list(employee_ids)),
        Attendance.date == today, Attendance.check_in_time.isnot(None)).scalar() or 0)


def void_version(db: Session, principal: Principal, shift_id: int, version_no: int, reason: str) -> dict:
    """Optional undo (§3.3 rule 3): off until ``shift.version.void_window_minutes`` exists (AS-M3 settings)."""
    shift = load_shift(db, principal, shift_id)
    _require_manage(db, principal, shift)
    raise AppError(422, "Undoing a version is turned off for this company.", "FEATURE_DISABLED",
                   extra={"details": {"setting": "shift.version.void_window_minutes", "value": 0}})


# ----------------------------------------------------------------------------------------- legacy routes
def legacy_list(db: Session, principal: Principal) -> List[dict]:
    """GET /api/v1/shifts legacy shape (BUG-AS-35: needs shift:read; active shifts only)."""
    rows = list_shifts(db, principal, status="ACTIVE", sort="name", limit=1000)["items"]
    return [{"id": r["id"], "code": r["code"], "name": r["name"], "kind": r["kind"], "start_time": r["start_time"],
             "end_time": r["end_time"], "duration_hours": r["duration_hours"],
             "unpaid_break_minutes": r["unpaid_break_minutes"], "is_flexible": r["is_flexible"],
             "is_active": r["is_active"]} for r in rows]


class _LegacyVersion:
    """A ``VersionIn``-shaped view of the legacy ShiftWrite body."""

    def __init__(self, data, effective_from: Optional[date] = None):
        self.effective_from = effective_from
        self.effective_to = None
        self.start_time, self.end_time = data.start_time, data.end_time
        self.segments, self.break_rules = [], []
        self.unpaid_break_minutes = data.unpaid_break_minutes
        self.half_split_time = self.core_start_time = self.core_end_time = None
        self.expected_net_minutes = self.grace_in_minutes = self.grace_out_minutes = None
        self.early_in_window_minutes, self.late_out_window_minutes = 120, 240
        self.anchor_timezone = None
        self.safety_critical = self.outdoor = False
        self.long_duty = data.start_time == data.end_time
        self.reason = None
        self.on_overlap = "REJECT"
        self.acknowledged_warnings = []
        self.dry_run = False


def _legacy_code(db: Session, company_id: int, name: str) -> str:
    import re

    base = (re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "SHIFT")[:20]
    code, n = base, 1
    while db.query(Shift.id).filter(Shift.company_id == company_id, Shift.code == code).first():
        n += 1
        code = base[: 20 - len(f"_{n}")] + f"_{n}"
    return code


LEGACY_FIRST_DATE = date(2000, 1, 1)


def legacy_create(db: Session, principal: Principal, data) -> Shift:
    """Legacy POST /shifts: a FIXED (or FLEXIBLE) tenant-wide shift whose version 1 starts 2000-01-01, so it applies
    to every date as a legacy shift always did (zero diff, like the migrated version 1). ``duration_hours`` is kept as
    sent (or the gross span when omitted, BUG-AS-23)."""
    from types import SimpleNamespace

    kind = "FLEXIBLE" if data.is_flexible else "FIXED"
    version = _LegacyVersion(data, LEGACY_FIRST_DATE)
    version.reason = "Created on the legacy shift screen"
    body = SimpleNamespace(code=_legacy_code(db, principal.company_id, data.name), name=data.name, kind=kind,
                           color_token="shift-1", legal_entity_id=None, branch_id=None, description=data.description,
                           version=version)
    created = create_shift(db, principal, body)
    shift = db.get(Shift, created["id"])
    if data.duration_hours is not None:
        _legacy_duration(db, shift, db.get(ShiftVersion, created["versions"][0]["id"]), data.duration_hours)
    if not data.is_active:
        shift.status, shift.is_active = "ARCHIVED", False
    db.flush()
    return shift


def _legacy_duration(db: Session, shift: Shift, version: ShiftVersion, duration: Decimal) -> None:
    """A legacy client's explicit duration wins over the computed span (zero diff with today's form)."""
    values = {f: getattr(version, f) for f in VERSION_FIELDS + ("crosses_midnight",)}
    values["duration_hours"] = Decimal(duration)
    version.duration_hours = Decimal(duration)
    version.content_hash = version_hash(values)
    if shift.current_version_id == version.id:
        shift.duration_hours = Decimal(duration)


def legacy_update(db: Session, principal: Principal, shift_id: int, data) -> Shift:
    """Legacy PUT /shifts/{id} (A-24): catalogue fields in place; changed times become a version from the owner's
    local tomorrow; ``unpaid_break_minutes`` is writable (BUG-AS-22)."""
    shift = load_shift(db, principal, shift_id, lock=True)
    _require_manage(db, principal, shift)
    if db.query(Shift.id).filter(Shift.company_id == shift.company_id, func.lower(Shift.name) == data.name.lower(),
                                 Shift.id != shift.id).first():
        raise conflict("A shift with this name already exists.")
    shift.name = data.name
    shift.description = data.description
    unpaid = data.unpaid_break_minutes if "unpaid_break_minutes" in data.model_fields_set else shift.unpaid_break_minutes
    changed = (data.start_time, data.end_time, unpaid) != (shift.start_time, shift.end_time, shift.unpaid_break_minutes)
    duration_changed = data.duration_hours is not None and Decimal(data.duration_hours) != (
        Decimal(shift.duration_hours) if shift.duration_hours is not None else None)
    if shift.status == "ACTIVE" and not data.is_active:
        shift.status, shift.is_active = "ARCHIVED", False
    elif shift.status == "ARCHIVED" and data.is_active:
        shift.status, shift.is_active = "ACTIVE", True
    if changed or duration_changed:
        if shift.status == "ARCHIVED":
            shift.status, shift.is_active = "ACTIVE", True
            archived_again = True
        else:
            archived_again = False
        body = _LegacyVersion(data)
        body.unpaid_break_minutes = unpaid
        result = create_version(db, principal, shift.id, body, legacy=True)
        if data.duration_hours is not None:
            _legacy_duration(db, shift, db.get(ShiftVersion, result["version"]["id"]), data.duration_hours)
        if archived_again:
            shift.status, shift.is_active = "ARCHIVED", False
    shift.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, MODULE, "SHIFT_SAVED", f"Saved shift '{shift.name}'", "shift", shift.id)
    return shift


# ------------------------------------------------------------------------------------------ assignment rules
def serialize_rule(row: ShiftAssignmentRule) -> dict:
    return {"id": row.id, "name": row.name, "shift_id": row.shift_id, "criteria": row.criteria,
            "priority": row.priority, "effective_from": row.effective_from.isoformat(),
            "effective_to": row.effective_to.isoformat() if row.effective_to else None,
            "legal_entity_id": row.legal_entity_id, "status": row.status, "reason": row.reason,
            "replaces_rule_id": row.replaces_rule_id, "team_id_snapshot": row.team_id_snapshot,
            "row_version": row.row_version}


def _criteria(raw: dict) -> pt.AssignmentCriteria:
    if "shift_ids" in (raw or {}):
        raise bad_request("Shift rules cannot use shift criteria.", "INVALID_CRITERIA")
    try:
        return pt.parse_criteria(raw)
    except pt.PolicyValidationError as error:
        raise AppError(400, error.message if hasattr(error, "message") else str(error), "INVALID_CRITERIA")


def _assign_scope_check(db: Session, principal: Principal, criteria: pt.AssignmentCriteria, on: date) -> None:
    """§12.6: the criteria must stay inside the actor's ``shift:assign`` scope (else unknown id, 404)."""
    if principal.is_company_admin or principal.has_company_scope(ASSIGN):
        return
    if not principal.has(ASSIGN):
        raise forbidden()
    if criteria.everyone:
        raise not_found("Criteria value")
    entities = principal.scope_targets(ASSIGN, Scope.LEGAL_ENTITY.value)
    branches = principal.scope_targets(ASSIGN, Scope.BRANCH.value)
    if criteria.legal_entity_ids and set(criteria.legal_entity_ids) <= set(entities):
        return
    if criteria.branch_ids and set(criteria.branch_ids) <= set(branches):
        return
    if criteria.employee_ids:
        scope = employee_scope(db, principal, ASSIGN)
        found = {e for (e,) in db.query(Employee.id).filter(
            Employee.company_id == principal.company_id, Employee.id.in_(list(criteria.employee_ids)),
            scope.clause(Employee.id))}
        if found == set(criteria.employee_ids):
            return
    raise not_found("Criteria value")


def _shift_scope_check(db: Session, shift: Shift, criteria: pt.AssignmentCriteria, on: date) -> None:
    """§12.5: a branch- / entity-owned shift needs criteria pinned inside its owner (SP-AS-163)."""
    if shift.branch_id is None and shift.legal_entity_id is None:
        return
    if shift.branch_id is not None and criteria.branch_ids and set(criteria.branch_ids) == {shift.branch_id}:
        return
    if shift.branch_id is None and criteria.legal_entity_ids and set(criteria.legal_entity_ids) == {
            shift.legal_entity_id}:
        return
    if criteria.employee_ids:
        placed = {a.employee_id: a for a in db.query(EmployeeAssignment).filter(
            EmployeeAssignment.company_id == shift.company_id, EmployeeAssignment.is_primary.is_(True),
            EmployeeAssignment.employee_id.in_(list(criteria.employee_ids)), EmployeeAssignment.effective_from <= on,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))}
        refused = [{"employee_id": e, "reason": "SHIFT_SCOPE"} for e in criteria.employee_ids
                   if e not in placed or not resolution.owner_scope_contains(shift, placed[e])]
        if not refused:
            return
        raise AppError(422, "Some employees are outside the shift's branch or entity.", "NOT_ELIGIBLE",
                       extra={"details": {"refused": refused, "reasons": ["SHIFT_SCOPE"]}})
    raise bad_request("A branch or entity shift needs criteria inside its branch or entity.", "INVALID_CRITERIA")


def _priority_conflicts(db: Session, company_id: int, rule: ShiftAssignmentRule, criteria: pt.AssignmentCriteria,
                        ignore: Iterable[int] = ()) -> None:
    skip = set(ignore)
    for other in db.query(ShiftAssignmentRule).filter(
            ShiftAssignmentRule.company_id == company_id, ShiftAssignmentRule.status == "ACTIVE",
            ShiftAssignmentRule.priority == rule.priority):
        if other.id == rule.id or other.id in skip:
            continue
        if other.effective_to is not None and other.effective_to < rule.effective_from:
            continue
        if rule.effective_to is not None and other.effective_from > rule.effective_to:
            continue
        try:
            if not criteria.may_overlap(pt.parse_criteria(other.criteria)):
                continue
        except pt.PolicyValidationError:
            continue
        raise AppError(409, f"Rule '{other.name}' has the same priority ({rule.priority}) and can match the same "
                            "people. Give one of them a different priority.", "PRIORITY_CONFLICT",
                       extra={"details": {"conflicts": [{"priority": rule.priority,
                                                         "rule_ids": sorted([other.id, rule.id or 0])}]}})


def _rule_owner_today(db: Session, company_id: int, legal_entity_id: Optional[int]) -> date:
    entity = db.get(LegalEntity, legal_entity_id) if legal_entity_id else None
    if entity is not None and entity.timezone in pytz.all_timezones_set:
        return clock.utcnow().astimezone(pytz.timezone(entity.timezone)).date()
    return _company_today(db, company_id)


def _load_rule(db: Session, principal: Principal, rule_id: int, *, lock: bool = False) -> ShiftAssignmentRule:
    query = db.query(ShiftAssignmentRule).filter(ShiftAssignmentRule.company_id == principal.company_id,
                                                 ShiftAssignmentRule.id == rule_id)
    row = (query.with_for_update() if lock else query).first()
    if row is None:
        raise not_found("Shift rule")
    return row


def list_rules(db: Session, principal: Principal, *, shift_id: Optional[int] = None, status: Optional[str] = None,
               skip: int = 0, limit: int = 50) -> dict:
    query = db.query(ShiftAssignmentRule).filter(ShiftAssignmentRule.company_id == principal.company_id)
    if shift_id is not None:
        query = query.filter(ShiftAssignmentRule.shift_id == shift_id)
    if status:
        query = query.filter(ShiftAssignmentRule.status == status)
    total = query.count()
    rows = query.order_by(ShiftAssignmentRule.priority.desc(), ShiftAssignmentRule.id).offset(skip).limit(limit).all()
    return {"items": [serialize_rule(r) for r in rows], "total": total, "skip": skip, "limit": limit}


def get_rule(db: Session, principal: Principal, rule_id: int) -> dict:
    return serialize_rule(_load_rule(db, principal, rule_id))


def _rule_population(db: Session, company_id: int, rule: ShiftAssignmentRule, *, before_skip: Iterable[int] = (),
                     draft: Optional[ShiftAssignmentRule] = None) -> Tuple[Dict[int, List[date]], List[dict]]:
    """Employees whose resolved shift changes because of the rule (before -> after) over the impact window."""
    start = rule.effective_from
    stop = min(rule.effective_to, start + timedelta(days=IMPACT_DAYS - 1)) if rule.effective_to else \
        start + timedelta(days=IMPACT_DAYS - 1)
    employee_ids = [e for (e,) in db.query(EmployeeAssignment.employee_id).filter(
        EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
        EmployeeAssignment.effective_from <= stop,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start)).distinct()]
    if not employee_ids:
        return {}, []
    skip = set(before_skip)
    before = resolution.shifts_for_many(db, company_id, employee_ids, start, stop, skip_rule_ids=skip | {rule.id or 0})
    after = resolution.shifts_for_many(db, company_id, employee_ids, start, stop,
                                       skip_rule_ids=skip | {rule.id or 0}, extra_rules=[draft or rule])
    changed: Dict[int, List[date]] = {}
    sample: List[dict] = []
    for key in sorted(before):
        old, new = before[key], after[key]
        if (old.shift_id if old else None) != (new.shift_id if new else None):
            changed.setdefault(key[0], []).append(key[1])
    for employee_id in sorted(changed)[:SAMPLE]:
        first = changed[employee_id][0]
        old, new = before[(employee_id, first)], after[(employee_id, first)]
        sample.append({"employee_id": employee_id, "from": first.isoformat(),
                       "before": {"shift_id": old.shift_id, "source": old.source} if old else None,
                       "after": {"shift_id": new.shift_id, "source": new.source} if new else None})
    return changed, sample


def _draft_rule(principal: Principal, data, criteria: pt.AssignmentCriteria) -> ShiftAssignmentRule:
    return ShiftAssignmentRule(id=None, company_id=principal.company_id, name=data.name, shift_id=data.shift_id,
                               criteria=criteria.model_dump(exclude_none=True), priority=data.priority,
                               effective_from=data.effective_from, effective_to=data.effective_to,
                               legal_entity_id=data.legal_entity_id, status="ACTIVE", reason=data.reason,
                               replaces_rule_id=getattr(data, "replaces_rule_id", None),
                               team_id_snapshot=getattr(data, "team_id_snapshot", None),
                               created_by_user_id=principal.user_id, row_version=1)


def _usable_shift(db: Session, principal: Principal, shift_id: int) -> Shift:
    shift = db.query(Shift).filter(Shift.company_id == principal.company_id, Shift.id == shift_id).first()
    if shift is None:
        raise not_found("Shift")
    if shift.status != "ACTIVE":
        raise AppError(422, "The shift is archived.", "SHIFT_ARCHIVED")
    return shift


def _past_start_check(db: Session, principal: Principal, data) -> None:
    today = _rule_owner_today(db, principal.company_id, data.legal_entity_id)
    if data.effective_from < today and not data.reason:
        raise AppError(422, "A rule that starts in the past needs a reason.", "VALIDATION_ERROR",
                       extra={"issues": [{"field": "reason", "code": "REASON_REQUIRED",
                                          "message": "Give a reason for a rule that starts in the past."}]})


def create_rule(db: Session, principal: Principal, data, *, simulate: bool = False) -> dict:
    criteria = _criteria(data.criteria)
    shift = _usable_shift(db, principal, data.shift_id)
    _assign_scope_check(db, principal, criteria, data.effective_from)
    _shift_scope_check(db, shift, criteria, data.effective_from)
    if data.effective_to is not None and data.effective_to < data.effective_from:
        raise AppError(422, "The end date is before the start date.", "VALIDATION_ERROR")
    if data.legal_entity_id is not None:
        _check_scope_refs(db, principal.company_id, data.legal_entity_id, None)
    replaced = None
    if getattr(data, "replaces_rule_id", None):
        replaced = _load_rule(db, principal, data.replaces_rule_id, lock=True)
    draft = _draft_rule(principal, data, criteria)
    _priority_conflicts(db, principal.company_id, draft, criteria, ignore=[replaced.id] if replaced else [])
    population, sample = _rule_population(db, principal.company_id, draft,
                                          before_skip=[replaced.id] if replaced and simulate else [], draft=draft)
    preview = {"employees_affected": len(population), "sample": sample,
               "dates_changed": sum(len(d) for d in population.values()),
               "dates_by_payroll_state": _payroll_states(db, principal.company_id, population)}
    if simulate:
        return preview
    _past_start_check(db, principal, data)
    if replaced is not None:
        if replaced.effective_from >= data.effective_from:
            raise AppError(422, "The replacement must start after the rule it replaces.", "VALIDATION_ERROR")
        replaced.effective_to = data.effective_from - ONE_DAY
        replaced.row_version += 1
    db.add(draft)
    db.flush()
    effects = _effects(db, principal, record_type="shift_rule", record_id=draft.id, version=draft.row_version,
                       content=serialize_rule(draft), population=population, source_kind="shift_rule")
    warnings = _locked_warning(preview, data.apply_to_locked_periods)
    AuditLogService.log(db, principal, MODULE, "ASSIGNMENT_RULE_SAVED", f"Saved shift rule '{draft.name}'",
                        "shift_assignment_rule", draft.id, details={"after": serialize_rule(draft)})
    _emit(db, "shift.assignment_rule.changed", {"rule_id": draft.id})
    return {**serialize_rule(draft), "effects": effects, "warnings": warnings, "preview": preview}


def _locked_warning(preview: dict, apply_to_locked: bool) -> List[dict]:
    locked = preview["dates_by_payroll_state"]["INPUT_LOCKED"] + preview["dates_by_payroll_state"]["FINALIZED"]
    if locked and not apply_to_locked:
        return [{"code": "LOCKED_DATES_NOT_RECOMPUTED", "count": locked,
                 "link": "/api/v1/attendance/days/recompute"}]
    return []


def update_rule(db: Session, principal: Principal, rule_id: int, data, expected_row_version: Optional[int]) -> dict:
    from app.core.concurrency import check_row_version

    row = _load_rule(db, principal, rule_id, lock=True)
    check_row_version(row, expected_row_version)
    criteria = _criteria(data.criteria if data.criteria is not None else row.criteria)
    _assign_scope_check(db, principal, criteria, row.effective_from)
    today = _rule_owner_today(db, principal.company_id, row.legal_entity_id)
    changes = data.model_dump(exclude_unset=True, exclude={"row_version", "apply_to_locked_periods"})
    locked_fields = [f for f in ("shift_id", "criteria", "priority") if f in changes
                     and changes[f] != getattr(row, f)]
    if locked_fields and row.effective_from <= today:
        raise AppError(409, "A rule in force cannot change its shift, criteria or priority; replace it from a date.",
                       "INVALID_TRANSITION", extra={"details": {"current_status": "IN_FORCE",
                                                                "fields": locked_fields}})
    if "status" in changes and changes["status"] != row.status and row.effective_from < today \
            and not data.apply_to_locked_periods:
        population, _ = _rule_population(db, principal.company_id, row)
        states = _payroll_states(db, principal.company_id, population)
        if states["INPUT_LOCKED"] or states["FINALIZED"]:
            raise AppError(409, "Pausing this rule changes locked payroll dates; confirm with a reason.",
                           "INVALID_TRANSITION", extra={"details": {"dates_by_payroll_state": states}})
    before = serialize_rule(row)
    if "shift_id" in changes:
        shift = _usable_shift(db, principal, changes["shift_id"])
        _shift_scope_check(db, shift, criteria, row.effective_from)
    if "effective_to" in changes and changes["effective_to"] is not None and changes["effective_to"] < row.effective_from:
        raise AppError(422, "The end date is before the start date.", "VALIDATION_ERROR")
    for key, value in changes.items():
        setattr(row, key, criteria.model_dump(exclude_none=True) if key == "criteria" else value)
    if row.status == "ACTIVE":
        _priority_conflicts(db, principal.company_id, row, criteria)
    row.row_version += 1
    db.flush()
    population, _ = _rule_population(db, principal.company_id, row)
    effects = _effects(db, principal, record_type="shift_rule", record_id=row.id, version=row.row_version,
                       content=serialize_rule(row), population=population, source_kind="shift_rule")
    AuditLogService.log(db, principal, MODULE, "ASSIGNMENT_RULE_SAVED", f"Edited shift rule '{row.name}'",
                        "shift_assignment_rule", row.id, details={"before": before, "after": serialize_rule(row)})
    _emit(db, "shift.assignment_rule.changed", {"rule_id": row.id})
    return {**serialize_rule(row), "effects": effects}


def simulate_rule(db: Session, principal: Principal, data) -> dict:
    return create_rule(db, principal, data, simulate=True)


def bulk_assign(db: Session, principal: Principal, data, matching_ids=None) -> dict:
    """§13: one employee_ids rule (priority 1000) or a dated placement change per employee; preview first."""
    from app.services import saved_views

    if data.employee_ids is not None:
        scope = saved_views.bulk_scope(ids=data.employee_ids)
    else:
        scope = saved_views.bulk_scope(filter_query=data.filter, expected_total=data.expected_total,
                                       snapshot=data.snapshot, matching_ids=matching_ids)
    ids = scope.ids
    shift = _usable_shift(db, principal, data.shift_id)
    allowed = employee_scope(db, principal, ASSIGN)
    in_scope = {e for (e,) in db.query(Employee.id).filter(Employee.company_id == principal.company_id,
                                                           Employee.id.in_(ids), allowed.clause(Employee.id))}
    if in_scope != set(ids):
        raise not_found("One or more employees")
    criteria = pt.AssignmentCriteria(employee_ids=sorted(ids))
    _shift_scope_check(db, shift, criteria, data.effective_from)
    name = data.name or f"{shift.code} — bulk {data.effective_from.isoformat()}"
    if data.apply_as == "RULE":
        from types import SimpleNamespace

        body = SimpleNamespace(name=name[:100], shift_id=shift.id, criteria={"employee_ids": sorted(ids)},
                               priority=RULE_BULK_PRIORITY, effective_from=data.effective_from,
                               effective_to=data.effective_to, legal_entity_id=None, reason=data.reason,
                               replaces_rule_id=None, team_id_snapshot=None,
                               apply_to_locked_periods=data.apply_to_locked_periods)
        if data.dry_run:
            return {"dry_run": True, "apply_as": "RULE", "items": len(ids), **simulate_rule(db, principal, body)}
        rule = create_rule(db, principal, body)
        return {"dry_run": False, "apply_as": "RULE", "rule": rule,
                "results": [{"employee_id": e, "status": "ok"} for e in sorted(ids)]}
    # PLACEMENT: a dated placement change per employee (a permanent base-shift change).
    from app.services import org_structure_service

    results = []
    for employee_id in sorted(ids):
        employee = db.get(Employee, employee_id)
        current = org_structure_service.current_assignment(db, employee_id, data.effective_from)
        if current is None:
            results.append({"employee_id": employee_id, "status": "NOT_ELIGIBLE", "reason": "NOT_PLACED"})
            continue
        if current.shift_id == shift.id:
            results.append({"employee_id": employee_id, "status": "ok", "unchanged": True})
            continue
        if data.dry_run:
            results.append({"employee_id": employee_id, "status": "ok", "before_shift_id": current.shift_id})
            continue
        org_structure_service.record_shift_change(db, principal, employee, shift.id, effective=data.effective_from)
        results.append({"employee_id": employee_id, "status": "ok"})
    return {"dry_run": data.dry_run, "apply_as": "PLACEMENT", "results": results}


# --------------------------------------------------------------------------------------------- tenant seed
GEN_VERSION = {
    "start_time": "09:00", "end_time": "18:00", "segments": [],
    "break_rules": [{"kind": "LUNCH", "mode": "FIXED_WINDOW", "minutes": 60, "paid": False,
                     "start": "13:00", "end": "14:00"}],
    "unpaid_break_minutes": 60, "half_split_time": None, "core_start_time": None, "core_end_time": None,
    "expected_net_minutes": None, "grace_in_minutes": None, "grace_out_minutes": None,
    "early_in_window_minutes": 120, "late_out_window_minutes": 240, "anchor_timezone": None,
    "safety_critical": False, "outdoor": False, "long_duty": False, "crosses_midnight": False,
    "duration_hours": Decimal("9.00"),
}


def seed_company_shift(db: Session, company_id: int) -> Shift:
    """New tenants get ``GEN`` 09:00-18:00 with a 60-minute unpaid fixed lunch 13:00-14:00 (BUG-AS-33)."""
    shift = Shift(company_id=company_id, code="GEN", name="General Shift", kind="FIXED", color_token=COLOR_TOKENS[0],
                  status="ACTIVE", is_active=True, start_time="09:00", end_time="18:00",
                  duration_hours=Decimal("9.00"), unpaid_break_minutes=60, is_flexible=False)
    db.add(shift)
    db.flush()
    row = ShiftVersion(company_id=company_id, shift_id=shift.id, version_no=1,
                       effective_from=date(2000, 1, 1), content_hash=version_hash(GEN_VERSION),
                       reason="Default shift of a new company",
                       **{k: v for k, v in GEN_VERSION.items()})
    db.add(row)
    db.flush()
    shift.current_version_id = row.id
    db.flush()
    return shift
