"""Work weeks: patterns, effective-dated versions, templates and the impact preview (LH-M1 WI-1.8; IG-05 naming).

Owner doc 04-holiday/03 §3, §5 (versions §5.1-5.3, ownership §5.6, preview §5.7, legacy §5.8, API §5.9); data
11 §4.1.12, §4.2.14; UI 09-ui-ux/02 §19. Services take ``(db, principal, ...)`` and never commit.

* ``work_week_pattern_versions`` is the source of truth; the pattern's rule columns mirror the version valid on the
  company-local today (refreshed here when a version is already valid, else by job ``holiday.link_activate``).
* Version posts tile: the version covering ``effective_from`` is closed the day before, future versions starting on
  or after it are deleted (they never applied); history before ``effective_from`` is never rewritten.
* Backdating, immutability and the default ``effective_from`` use ``pattern_local_today`` (LH-TZ-08).
* Half-day weekly offs are evaluated and previewed, but saving them waits for LH-M5b (V-9 gate,
  ``HALF_DAY_OFFS_ENABLED``) because payroll charges half offs only from then.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.company import Company
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import BranchCalendarLink, HolidayCalendarAssignment, WorkWeekPattern, WorkWeekPatternVersion
from app.models.time_inputs import LeaveDay
from app.models.user import User
from app.payroll import workweek
from app.payroll.services import outbox, time_inputs
from app.services import holiday_assignments, leave_jobs
from app.services.audit_log_service import AuditLogService
from app.services.holiday_assignments import MIGRATED_FROM, WorkWeekResolver, zone_today
from app.services.notification_service import NotificationService

AUDIT_MODULE = "Holidays"
# V-9: half-day weekly offs are charged by payroll from LH-M5b; until then they are previewed but not saved.
HALF_DAY_OFFS_ENABLED = False
HALF_DAY_GATE_MESSAGE = "Half-day weekly offs are not enabled yet."
BACKDATE_DAYS_DEFAULT = 7  # holiday.backdate_days default (WI-1.10)
PREVIEW_DEFAULT_DAYS = 56  # the modal's "next 8 weeks" strip
NOTIFY_LIMIT = 500
READ_CODES = ("holiday:read", "holiday:manage", "org:read", "org:manage_dimensions")
# Illustrative weekly-rest limits (04-holiday/03 §6.3, appendix-a §3.4): verify in the country pack before use.
# Below VERIFIED every violation is a warning that needs acknowledgement; VERIFIED packs (LH-M9) block with 422.
REST_LIMITS: Mapping[str, workweek.RestLimits] = {
    "IN": workweek.RestLimits(max_days_per_week=6, max_consecutive_days=10, verified=False),
}
_ONE_DAY = timedelta(days=1)


# ---------------------------------------------------------------------------------------------- helpers
def backdate_days(db: Session, company_id: int) -> int:
    """``holiday.backdate_days`` when the Holidays settings section exists (WI-1.10), else the default 7."""
    from app.payroll import settings_registry

    if "holiday.backdate_days" not in settings_registry.REGISTRY:
        return BACKDATE_DAYS_DEFAULT
    from app.services.holiday_years import holiday_setting

    return int(holiday_setting(db, company_id, "holiday.backdate_days", BACKDATE_DAYS_DEFAULT))  # fails closed


def _engine_error(exc: workweek.WorkWeekError) -> AppError:
    if exc.code == "RANGE_TOO_LONG":
        return bad_request(f"{exc.message}.", "RANGE_TOO_LONG")
    return bad_request(f"Work week: {exc.message}.", exc.code)


def parse_rules(rules: Any, *, allow_half_days: bool = HALF_DAY_OFFS_ENABLED) -> workweek.WorkWeekPattern:
    """Engine validation (V-1..V-8) and the V-9 gate; errors are 400 with the engine code."""
    try:
        pattern = workweek.WorkWeekPattern.from_json(rules)
    except workweek.WorkWeekError as exc:
        raise _engine_error(exc) from None
    if pattern.half_day_offs and not allow_half_days:
        raise bad_request(HALF_DAY_GATE_MESSAGE, "INVALID_PORTION")
    return pattern


def _code_from_name(name: str) -> str:
    code = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    return (code or "WORK_WEEK")[:30].rstrip("_")


def _clean_code(value: Any) -> str:
    code = "".join(str(value or "").split()).upper()
    if not code or len(code) > 30 or not all(c.isalnum() or c in "-_." for c in code):
        raise bad_request("Code must be 1-30 letters, digits, '-', '_' or '.'.", "ORG_CODE_INVALID")
    return code


def _clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    if not name or len(name) > 100:
        raise bad_request("Name must be 1-100 characters.", "ORG_NAME_INVALID")
    return name


def _company_today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


# ---------------------------------------------------------------------------------------------- scope (§5.6)
def can_read(principal: Principal) -> bool:
    return principal.is_company_admin or any(principal.scopes(code) for code in READ_CODES)


def _entity_targets(db: Session, principal: Principal, code: str) -> Optional[Set[int]]:
    """None = every entity (COMPANY); else the entities covered at LE scope or through BR targets."""
    if principal.is_company_admin or principal.has_company_scope(code):
        return None
    entities = set(principal.scope_targets(code, "LEGAL_ENTITY"))
    branches = principal.scope_targets(code, "BRANCH")
    if branches:
        entities |= {e for (e,) in db.query(Branch.legal_entity_id).filter(
            Branch.company_id == principal.company_id, Branch.id.in_(sorted(branches)))}
    return entities


def _readable_entities(db: Session, principal: Principal) -> Optional[Set[int]]:
    out: Set[int] = set()
    for code in READ_CODES:
        targets = _entity_targets(db, principal, code) if principal.scopes(code) or principal.is_company_admin \
            else set()
        if targets is None:
            return None
        out |= targets
    return out


def _linked_branches(db: Session, company_id: int, pattern_id: int, today: date) -> Set[int]:
    """Branches whose current or future link (or, without links, current column) uses the pattern."""
    linked = {b for (b,) in db.query(BranchCalendarLink.branch_id).filter(
        BranchCalendarLink.company_id == company_id, BranchCalendarLink.work_week_pattern_id == pattern_id,
        or_(BranchCalendarLink.effective_to.is_(None), BranchCalendarLink.effective_to >= today))}
    with_links = {b for (b,) in db.query(BranchCalendarLink.branch_id).filter(
        BranchCalendarLink.company_id == company_id).distinct()}
    linked |= {b for (b,) in db.query(Branch.id).filter(
        Branch.company_id == company_id, Branch.work_week_pattern_id == pattern_id,
        Branch.id.notin_(sorted(with_links)) if with_links else True)}
    return linked


def can_manage(db: Session, principal: Principal, entity_id: Optional[int], *, pattern_id: Optional[int] = None,
               code: str = "holiday:manage") -> bool:
    """Tenant-wide patterns: COMPANY scope only. Entity-owned: COMPANY, LE of that entity, or BR when every branch
    using the pattern on or after today is in the actor's branch targets (04-holiday/03 §5.6)."""
    if principal.is_company_admin or principal.has_company_scope(code):
        return True
    if entity_id is None:
        return False
    if entity_id in principal.scope_targets(code, "LEGAL_ENTITY"):
        return True
    branch_targets = principal.scope_targets(code, "BRANCH")
    if not branch_targets:
        return False
    own = {b for (b,) in db.query(Branch.id).filter(Branch.company_id == principal.company_id,
                                                     Branch.legal_entity_id == entity_id)}
    if not own & set(branch_targets):
        return False
    if pattern_id is None:
        return True
    used = _linked_branches(db, principal.company_id, pattern_id, _company_today(db, principal.company_id))
    return used <= set(branch_targets)


def _get_visible(db: Session, principal: Principal, pattern_id: int, *, lock: bool = False) -> WorkWeekPattern:
    query = db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == principal.company_id,
                                             WorkWeekPattern.id == int(pattern_id))
    row = (query.with_for_update() if lock else query).first()
    if row is None or not can_read(principal):
        raise not_found("Work week")
    entities = _readable_entities(db, principal)
    if entities is not None and row.legal_entity_id is not None and row.legal_entity_id not in entities:
        raise not_found("Work week")
    return row


def _get_managed(db: Session, principal: Principal, pattern_id: int) -> WorkWeekPattern:
    row = _get_visible(db, principal, pattern_id, lock=True)
    if not can_manage(db, principal, row.legal_entity_id, pattern_id=row.id):
        if principal.scopes("holiday:manage") or principal.is_company_admin:
            raise forbidden(f"Work week {row.code} is managed by another scope.")
        raise forbidden()
    return row


# ---------------------------------------------------------------------------------------------- clocks (§5.1)
def _zones_of_users(db: Session, company_id: int, pattern_id: int, on_or_after: date) -> Set[str]:
    """Zones of employees who resolve to the pattern on or after ``on_or_after`` (placements, links, entity)."""
    zones: Set[str] = set()
    current = or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on_or_after)
    work_tz = getattr(EmployeeAssignment, "work_timezone", None)
    resolver = WorkWeekResolver(db, company_id)
    rows = (db.query(EmployeeAssignment).filter(EmployeeAssignment.company_id == company_id,
                                                EmployeeAssignment.is_primary.is_(True), current).all())
    for a in rows:
        day = max(a.effective_from, on_or_after)
        if resolver.resolve(a, day).pattern_id != pattern_id:
            continue
        zone = getattr(a, "work_timezone", None) if work_tz is not None else None
        if not zone:
            branch = resolver.branch(a.branch_id)
            zone = branch.timezone if branch is not None else None
        if not zone:
            entity = resolver.entity(a.legal_entity_id)
            zone = entity.timezone if entity is not None else None
        if zone:
            zones.add(zone)
    for branch_id in _linked_branches(db, company_id, pattern_id, on_or_after):
        branch = resolver.branch(branch_id)
        if branch is not None and branch.timezone:
            zones.add(branch.timezone)
    return zones


def pattern_local_today(db: Session, company_id: int, pattern_id: int) -> date:
    """Furthest-ahead local today among the zones of the pattern's users, else the owning entity's zone, else the
    company zone (LH-TZ-08)."""
    company_today = _company_today(db, company_id)
    zones = _zones_of_users(db, company_id, pattern_id, company_today - _ONE_DAY)
    if zones:
        return max(zone_today(z) for z in zones)
    row = db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == company_id,
                                           WorkWeekPattern.id == pattern_id).first()
    if row is not None and row.legal_entity_id is not None:
        entity = db.get(LegalEntity, row.legal_entity_id)
        if entity is not None and entity.timezone:
            return zone_today(entity.timezone)
    return company_today


def _owner_local_today(db: Session, company_id: int, entity_id: Optional[int]) -> date:
    if entity_id is not None:
        entity = db.get(LegalEntity, entity_id)
        if entity is not None and entity.timezone:
            return zone_today(entity.timezone)
    return _company_today(db, company_id)


# ---------------------------------------------------------------------------------------------- versions
def _versions(db: Session, company_id: int, pattern_id: int) -> List[WorkWeekPatternVersion]:
    return (db.query(WorkWeekPatternVersion)
            .filter(WorkWeekPatternVersion.company_id == company_id, WorkWeekPatternVersion.pattern_id == pattern_id)
            .order_by(WorkWeekPatternVersion.effective_from).all())


def _as_engine(rows: Iterable[WorkWeekPatternVersion]) -> List[workweek.PatternVersion]:
    return [workweek.PatternVersion(r.version, r.effective_from, r.effective_to,
                                    workweek.WorkWeekPattern.from_json(r.rules)) for r in rows]


def pattern_on(db: Session, company_id: int, pattern_id: int, day: date) -> workweek.WorkWeekPattern:
    """The rules of the version valid on ``day`` (before version 1: version 1)."""
    return WorkWeekResolver(db, company_id).version_on(pattern_id, day).pattern


def _mirror(row: WorkWeekPattern, pattern: workweek.WorkWeekPattern) -> bool:
    """Copy rules into the pattern's mirror columns; True when something changed."""
    data = pattern.to_json()
    values = {
        "kind": pattern.kind,
        "weekly_off_days": data.get("weekly_off_days", []),
        "nth_weekday_offs": data.get("nth_weekday_offs", []),
        "half_day_offs": data.get("half_day_offs", []),
        "cycle_length_days": data.get("cycle_length_days"),
        "cycle_off_days": data.get("cycle_off_days"),
        "anchor_date": pattern.anchor_date,
        "rest_days": data.get("rest_days"),
    }
    changed = False
    for key, value in values.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
            changed = True
    return changed


def refresh_mirrors(db: Session, company_id: int) -> int:
    """Mirror every pattern to the version valid on the company-local today; emit ``workweek.version.activated``."""
    today = _company_today(db, company_id)
    activated = 0
    for row in db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == company_id).order_by(WorkWeekPattern.id):
        versions = _versions(db, company_id, row.id)
        current = next((v for v in versions if v.effective_from <= today
                        and (v.effective_to is None or today <= v.effective_to)), None)
        if current is None:
            continue
        if _mirror(row, workweek.WorkWeekPattern.from_json(current.rules)):
            activated += 1
            outbox.emit(db, "workweek.version.activated", {"pattern_id": row.id, "version": current.version,
                                                            "effective_date": current.effective_from.isoformat()})
    db.flush()
    return activated


def ensure_first_version(db: Session, row: WorkWeekPattern, *, effective_from: date = MIGRATED_FROM,
                         actor_user_id: Optional[int] = None) -> WorkWeekPatternVersion:
    """Version 1 of a pattern created by a legacy path (applies to every date, like before versions existed)."""
    existing = db.query(WorkWeekPatternVersion).filter(WorkWeekPatternVersion.company_id == row.company_id,
                                                       WorkWeekPatternVersion.pattern_id == row.id).first()
    if existing is not None:
        return existing
    pattern = holiday_assignments.pattern_from_row(row)
    version = WorkWeekPatternVersion(company_id=row.company_id, pattern_id=row.id, version=1, rules=pattern.to_json(),
                                     effective_from=effective_from, effective_to=None,
                                     content_hash=pattern.content_hash(), created_by_user_id=actor_user_id)
    db.add(version)
    db.flush()
    return version


# ---------------------------------------------------------------------------------------------- usage
def usage_counts(db: Session, company_id: int, on: Optional[date] = None) -> Dict[int, Dict[str, int]]:
    """{pattern_id: {"branches", "employees", "criteria"}} of people resolving to each pattern on ``on``."""
    on = on or _company_today(db, company_id)
    out: Dict[int, Dict[str, int]] = {}

    def bucket(pid: int) -> Dict[str, int]:
        return out.setdefault(pid, {"branches": 0, "employees": 0, "criteria": 0})

    resolver = WorkWeekResolver(db, company_id)
    branches = db.query(Branch).filter(Branch.company_id == company_id, Branch.status == "ACTIVE").all()
    for branch in branches:
        pid = resolver.branch_pattern_on(branch.id, on)
        if pid:
            bucket(pid)["branches"] += 1
    for row in resolver.criteria_rows():
        if row.effective_to is None or row.effective_to >= on:
            bucket(row.work_week_pattern_id)["criteria"] += 1
    assignments = (db.query(EmployeeAssignment)
                   .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
                           EmployeeAssignment.effective_from <= on,
                           or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))
                   .all())
    seen: Set[int] = set()
    for a in assignments:
        if a.employee_id in seen:
            continue
        seen.add(a.employee_id)
        pid = resolver.resolve(a, on).pattern_id
        if pid:
            bucket(pid)["employees"] += 1
    return out


def usage_reason(db: Session, company_id: int, pattern_id: int) -> Optional[str]:
    """What keeps a pattern in use on or after today (409 PATTERN_IN_USE), or None (§5.8 step 6)."""
    today = _company_today(db, company_id)
    if db.query(EmployeeAssignment.id).filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.work_week_pattern_id == pattern_id,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= today)).first():
        return "current or future placements"
    if _linked_branches(db, company_id, pattern_id, today):
        return "a current or future branch link"
    if db.query(HolidayCalendarAssignment.id).filter(
            HolidayCalendarAssignment.company_id == company_id,
            HolidayCalendarAssignment.work_week_pattern_id == pattern_id,
            or_(HolidayCalendarAssignment.effective_to.is_(None), HolidayCalendarAssignment.effective_to >= today)
    ).first():
        return "a current or future calendar assignment"
    if db.query(LegalEntity.id).filter(LegalEntity.company_id == company_id, LegalEntity.status == "ACTIVE",
                                       LegalEntity.default_work_week_pattern_id == pattern_id).first():
        return "a legal entity default"
    return None


# ---------------------------------------------------------------------------------------------- serializers
def _state(row: WorkWeekPatternVersion, today: date) -> str:
    if row.effective_from > today:
        return "SCHEDULED"
    if row.effective_to is not None and row.effective_to < today:
        return "PAST"
    return "CURRENT"


def serialize_version(row: WorkWeekPatternVersion, today: date, authors: Optional[Mapping[int, str]] = None) -> dict:
    pattern = workweek.WorkWeekPattern.from_json(row.rules)
    return {
        "id": row.id, "version": row.version, "rules": row.rules, "kind": pattern.kind, "summary": pattern.summary(),
        "effective_from": row.effective_from.isoformat(),
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "reason": row.reason, "content_hash": row.content_hash, "state": _state(row, today),
        "created_by_user_id": row.created_by_user_id,
        "created_by": (authors or {}).get(row.created_by_user_id) if row.created_by_user_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _allowed_actions(db: Session, principal: Principal, row: WorkWeekPattern, in_use: Optional[str]) -> List[str]:
    if not can_manage(db, principal, row.legal_entity_id, pattern_id=row.id):
        return []
    actions = ["edit", "add_version"]
    if row.kind == workweek.CYCLE:
        actions.append("duplicate_as_crew")
    if row.status == "ACTIVE" and in_use is None:
        actions.append("deactivate")
    if row.status == "INACTIVE":
        actions.append("activate")
    return actions


def serialize(db: Session, principal: Principal, row: WorkWeekPattern, *, today: Optional[date] = None,
              counts: Optional[Mapping[int, Mapping[str, int]]] = None,
              versions: Optional[List[WorkWeekPatternVersion]] = None, detail: bool = False) -> dict:
    today = today or _company_today(db, row.company_id)
    versions = versions if versions is not None else _versions(db, row.company_id, row.id)
    current = next((v for v in versions if v.effective_from <= today
                    and (v.effective_to is None or today <= v.effective_to)), versions[0] if versions else None)
    upcoming = next((v for v in versions if v.effective_from > today), None)
    rules = current.rules if current is not None else holiday_assignments.pattern_from_row(row).to_json()
    pattern = workweek.WorkWeekPattern.from_json(rules)
    entity = db.get(LegalEntity, row.legal_entity_id) if row.legal_entity_id else None
    used = (counts or {}).get(row.id, {"branches": 0, "employees": 0, "criteria": 0})
    in_use = usage_reason(db, row.company_id, row.id) if detail else None
    out = {
        "id": row.id, "code": row.code, "name": row.name, "name_i18n": row.name_i18n or {}, "kind": pattern.kind,
        "status": row.status, "legal_entity_id": row.legal_entity_id,
        "owner": entity.legal_name if entity is not None else "Tenant-wide", "row_version": row.row_version,
        "rules": rules, "summary": pattern.summary(), "has_half_day_offs": pattern.has_half_day_offs,
        "current_version": serialize_version(current, today) if current is not None else None,
        "next_change": ({"version": upcoming.version, "effective_from": upcoming.effective_from.isoformat()}
                        if upcoming is not None else None),
        "versions_count": len(versions),
        "used_by": dict(used),
        "can_manage": can_manage(db, principal, row.legal_entity_id, pattern_id=row.id),
    }
    if detail:
        out["in_use"] = in_use
        out["allowed_actions"] = _allowed_actions(db, principal, row, in_use)
        out["local_today"] = pattern_local_today(db, row.company_id, row.id).isoformat()
    return out


# ---------------------------------------------------------------------------------------------- reads
def list_templates(db: Session, principal: Principal) -> List[dict]:
    today = _company_today(db, principal.company_id)
    out = []
    for template in workweek.TEMPLATES.values():
        rules = template.rules_for(today)
        pattern = workweek.WorkWeekPattern.from_json(rules)
        available = HALF_DAY_OFFS_ENABLED or not template.needs_half_day_offs
        out.append({"code": template.code, "name": template.name, "kind": template.kind,
                    "typical_use": template.typical_use, "rules": rules, "summary": pattern.summary(),
                    "needs_half_day_offs": template.needs_half_day_offs, "available": available,
                    "unavailable_reason": None if available else "Available after half-day weekly offs are enabled"})
    return out


SORTS = {"code": WorkWeekPattern.code, "name": WorkWeekPattern.name, "kind": WorkWeekPattern.kind,
         "status": WorkWeekPattern.status}


def list_patterns(db: Session, principal: Principal, *, q: Optional[str] = None, kind: Optional[str] = None,
                  status: Optional[str] = None, legal_entity_id: Optional[int] = None, owner: Optional[str] = None,
                  sort: str = "code", skip: int = 0, limit: int = 25) -> dict:
    if not can_read(principal):
        raise forbidden()
    cid = principal.company_id
    query = db.query(WorkWeekPattern).filter(WorkWeekPattern.company_id == cid)
    entities = _readable_entities(db, principal)
    if entities is not None:
        query = query.filter(or_(WorkWeekPattern.legal_entity_id.is_(None),
                                 WorkWeekPattern.legal_entity_id.in_(sorted(entities) or [-1])))
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(WorkWeekPattern.code.ilike(like, escape="\\"),
                                 WorkWeekPattern.name.ilike(like, escape="\\")))
    if kind:
        if kind not in workweek.KINDS:
            raise bad_request("Kind must be WEEKLY or CYCLE.", "INVALID_FILTER")
        query = query.filter(WorkWeekPattern.kind == kind)
    if status:
        if status not in ("ACTIVE", "INACTIVE"):
            raise bad_request("Status must be ACTIVE or INACTIVE.", "INVALID_FILTER")
        query = query.filter(WorkWeekPattern.status == status)
    if owner == "TENANT":
        query = query.filter(WorkWeekPattern.legal_entity_id.is_(None))
    elif legal_entity_id:
        query = query.filter(WorkWeekPattern.legal_entity_id == legal_entity_id)
    descending = sort.startswith("-")
    column = SORTS.get(sort.lstrip("-"))
    if column is None:
        raise bad_request(f"Sort by one of {', '.join(sorted(SORTS))}.", "INVALID_SORT")
    total = query.count()
    rows = query.order_by(column.desc() if descending else column, WorkWeekPattern.id).offset(skip).limit(limit).all()
    today = _company_today(db, cid)
    counts = usage_counts(db, cid, today)
    return {"items": [serialize(db, principal, r, today=today, counts=counts) for r in rows], "total": total,
            "skip": skip, "limit": limit, "local_today": today.isoformat(),
            "can_create": principal.is_company_admin or bool(principal.scopes("holiday:manage"))}


def get_pattern(db: Session, principal: Principal, pattern_id: int) -> dict:
    row = _get_visible(db, principal, pattern_id)
    cid = principal.company_id
    today = _company_today(db, cid)
    counts = usage_counts(db, cid, today)
    out = serialize(db, principal, row, today=today, counts=counts, detail=True)
    out["versions"] = list_versions(db, principal, pattern_id, row=row)
    out["used_by_detail"] = _used_by_detail(db, cid, row.id, today)
    return out


def list_versions(db: Session, principal: Principal, pattern_id: int, *, row: Optional[WorkWeekPattern] = None
                  ) -> List[dict]:
    row = row or _get_visible(db, principal, pattern_id)
    today = _company_today(db, principal.company_id)
    versions = _versions(db, principal.company_id, row.id)
    users = {v.created_by_user_id for v in versions if v.created_by_user_id}
    authors = dict(db.query(User.id, User.full_name).filter(User.company_id == principal.company_id,
                                                           User.id.in_(sorted(users)))) if users else {}
    return [serialize_version(v, today, authors) for v in sorted(versions, key=lambda v: v.version, reverse=True)]


def _used_by_detail(db: Session, company_id: int, pattern_id: int, today: date) -> dict:
    branch_ids = _linked_branches(db, company_id, pattern_id, today)
    branches = (db.query(Branch).filter(Branch.company_id == company_id, Branch.id.in_(sorted(branch_ids))).all()
                if branch_ids else [])
    links = {}
    for link in db.query(BranchCalendarLink).filter(BranchCalendarLink.company_id == company_id,
                                                    BranchCalendarLink.work_week_pattern_id == pattern_id,
                                                    or_(BranchCalendarLink.effective_to.is_(None),
                                                        BranchCalendarLink.effective_to >= today)):
        links.setdefault(link.branch_id, []).append(holiday_assignments.serialize_link(link))
    criteria = (db.query(HolidayCalendarAssignment)
                .filter(HolidayCalendarAssignment.company_id == company_id,
                        HolidayCalendarAssignment.work_week_pattern_id == pattern_id).all())
    overridden = (db.query(EmployeeAssignment)
                  .filter(EmployeeAssignment.company_id == company_id,
                          EmployeeAssignment.work_week_pattern_id == pattern_id,
                          or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= today))
                  .all())
    return {
        "branches": [{"id": b.id, "code": b.code, "name": b.name, "links": links.get(b.id, [])}
                     for b in sorted(branches, key=lambda b: b.code)],
        "criteria": [{"id": c.id, "priority": c.priority, "criteria": c.criteria,
                      "effective_from": c.effective_from.isoformat(),
                      "effective_to": c.effective_to.isoformat() if c.effective_to else None} for c in criteria],
        "employees": [{"employee_id": a.employee_id, "assignment_id": a.id,
                       "effective_from": a.effective_from.isoformat(),
                       "effective_to": a.effective_to.isoformat() if a.effective_to else None} for a in overridden],
    }


# ---------------------------------------------------------------------------------------------- rest check (§6.3)
def _entities_using(db: Session, company_id: int, row: Optional[WorkWeekPattern],
                    entity_id: Optional[int]) -> List[LegalEntity]:
    owner = entity_id if row is None else row.legal_entity_id
    if owner is not None:
        entity = db.get(LegalEntity, owner)
        return [entity] if entity is not None else []
    query = db.query(LegalEntity).filter(LegalEntity.company_id == company_id, LegalEntity.status == "ACTIVE")
    if row is not None:
        today = _company_today(db, company_id)
        branches = _linked_branches(db, company_id, row.id, today)
        entity_ids = {e for (e,) in db.query(Branch.legal_entity_id).filter(
            Branch.company_id == company_id, Branch.id.in_(sorted(branches)))} if branches else set()
        entity_ids |= {e for (e,) in db.query(EmployeeAssignment.legal_entity_id).filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.work_week_pattern_id == row.id)}
        if entity_ids:
            query = query.filter(LegalEntity.id.in_(sorted(entity_ids)))
    return query.order_by(LegalEntity.id).all()


def rest_findings(db: Session, company_id: int, previous: Optional[workweek.WorkWeekPattern],
                  new: workweek.WorkWeekPattern, effective_from: date, *, row: Optional[WorkWeekPattern] = None,
                  entity_id: Optional[int] = None) -> Tuple[List[dict], List[dict]]:
    """(warnings, blocking) per legal entity whose pack limits apply (LH-WWK-16, -17, -29)."""
    warnings: List[dict] = []
    blocking: List[dict] = []
    seen: Set[str] = set()
    for entity in _entities_using(db, company_id, row, entity_id):
        limits = REST_LIMITS.get(entity.country_code)
        if limits is None:
            continue
        for violation in workweek.check_version(previous, new, effective_from, limits):
            item = {**violation.to_json(), "legal_entity_id": entity.id, "legal_entity": entity.legal_name,
                    "country": entity.country_code, "verified": limits.verified}
            key = f"{entity.id}:{violation.rule}:{violation.start}"
            if key in seen:
                continue
            seen.add(key)
            (blocking if limits.verified else warnings).append(item)
    return warnings, blocking


def _check_rest(warnings: List[dict], blocking: List[dict], acknowledged: bool) -> None:
    if blocking:
        raise AppError(422, blocking[0]["message"], "WEEKLY_REST_EXCEEDED", extra={"issues": blocking})
    if warnings and not acknowledged:
        raise AppError(422, "Review the weekly-rest warnings and confirm to save.", "WARNINGS_NOT_ACKNOWLEDGED",
                       extra={"issues": warnings})


# ---------------------------------------------------------------------------------------------- impact (§5.7)
def _affected_employees(db: Session, company_id: int, pattern_id: Optional[int], start: date
                        ) -> List[Tuple[EmployeeAssignment, WorkWeekResolver]]:
    """Primary placements current on or after ``start`` that resolve to ``pattern_id`` on some date from ``start``."""
    if pattern_id is None:
        return []
    resolver = WorkWeekResolver(db, company_id)
    rows = (db.query(EmployeeAssignment)
            .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
                    or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start))
            .order_by(EmployeeAssignment.employee_id, EmployeeAssignment.effective_from).all())
    out = []
    seen: Set[int] = set()
    for a in rows:
        if a.employee_id in seen:
            continue
        day = max(a.effective_from, start)
        if resolver.resolve(a, day).pattern_id == pattern_id:
            seen.add(a.employee_id)
            out.append((a, resolver))
    return out


def impact(db: Session, company_id: int, pattern_id: Optional[int], old_by_date: Mapping[date, Optional[str]],
           new_by_date: Mapping[date, Optional[str]]) -> dict:
    """Employees, date classes, leave recount and pending requests touched by a change of weekly-off portions."""
    changed = sorted(d for d in new_by_date if old_by_date.get(d) != new_by_date[d])
    counts = {"working_to_off": 0, "off_to_working": 0, "full_to_half": 0, "half_change": 0}
    for day in changed:
        a, b = old_by_date.get(day), new_by_date[day]
        if a is None and b == workweek.FULL:
            counts["working_to_off"] += 1
        elif a == workweek.FULL and b is None:
            counts["off_to_working"] += 1
        elif a == workweek.FULL and b in workweek.HALF_PORTIONS:
            counts["full_to_half"] += 1
        else:
            counts["half_change"] += 1
    start = min(new_by_date) if new_by_date else None
    affected = _affected_employees(db, company_id, pattern_id, start) if start else []
    employee_ids = sorted({a.employee_id for a, _ in affected})
    per_branch: Dict[int, int] = {}
    for a, _ in affected:
        per_branch[a.branch_id] = per_branch.get(a.branch_id, 0) + 1
    branch_names = dict(db.query(Branch.id, Branch.name).filter(Branch.company_id == company_id,
                                                               Branch.id.in_(sorted(per_branch) or [-1])))
    recount: Dict[str, Dict[str, Any]] = {}
    pending = 0
    if employee_ids and changed:
        days = (db.query(LeaveDay, Leave.leave_type)
                .join(Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id))
                .filter(LeaveDay.company_id == company_id, LeaveDay.employee_id.in_(employee_ids),
                        LeaveDay.date.in_(changed), LeaveDay.voided_at.is_(None)).all())
        requests: Dict[str, Set[int]] = {}
        for leave_day, leave_type in days:
            b = new_by_date.get(leave_day.date)
            delta = 0.0
            if b == workweek.FULL and leave_day.counts_against_balance:
                delta = float(leave_day.units)  # credited back
            elif b is None and not leave_day.counts_against_balance:
                delta = -float(leave_day.units)  # debited
            if delta:
                entry = recount.setdefault(leave_type, {"leave_type": leave_type, "requests": 0, "units": 0.0})
                entry["units"] += delta
                requests.setdefault(leave_type, set()).add(leave_day.leave_id)
        for leave_type, ids in requests.items():
            recount[leave_type]["requests"] = len(ids)
        first, last = changed[0], changed[-1]
        pending_rows = (db.query(Leave).filter(Leave.company_id == company_id, Leave.employee_id.in_(employee_ids),
                                               Leave.status == "pending", Leave.start_date <= last,
                                               Leave.end_date >= first).all())
        changed_set = set(changed)
        pending = sum(1 for leave in pending_rows
                      if any(d in changed_set for d in workweek.dates_between(leave.start_date, leave.end_date)))
    return {
        "employees_affected": {"count": len(employee_ids),
                               "per_branch": [{"branch_id": b, "branch": branch_names.get(b), "count": n}
                                              for b, n in sorted(per_branch.items())]},
        "date_changes": {**counts, "dates": [d.isoformat() for d in changed[:400]]},
        "leave_recount": {"requests": sum(v["requests"] for v in recount.values()),
                          "units": round(sum(v["units"] for v in recount.values()), 2),
                          "by_type": sorted(recount.values(), key=lambda v: v["leave_type"])},
        "pending_requests": pending,
        "locked_periods": _locked_periods(db, company_id, [a for a, _ in affected], changed),
        # Declared working days arrive with calendar_working_days (calendar goal); none can exist before it.
        "declared_working_days": [],
    }


# pay_periods.status -> the period state of V3-069 (IG-AS-29: read directly until calendars.period_state_on ships).
PERIOD_STATE = {"PROCESSING": "INPUT_LOCKED", "CLOSED": "FINALIZED"}


def _locked_periods(db: Session, company_id: int, assignments: List[EmployeeAssignment],
                    changed: List[date]) -> List[dict]:
    """Payroll periods of the affected pay groups that contain a changed date and are input-locked or finalized:
    the change reaches them only through source_change_log and retro detection (§5.3)."""
    from app.models.pay_org import PayGroup
    from app.models.pay_runs import PayPeriod

    groups = sorted({a.pay_group_id for a in assignments if a.pay_group_id})
    if not groups or not changed:
        return []
    rows = (db.query(PayPeriod).filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id.in_(groups),
                                       PayPeriod.status.in_(tuple(PERIOD_STATE)),
                                       PayPeriod.period_start <= changed[-1], PayPeriod.period_end >= changed[0])
            .order_by(PayPeriod.period_start, PayPeriod.pay_group_id).all())
    names = dict(db.query(PayGroup.id, PayGroup.name).filter(PayGroup.company_id == company_id,
                                                            PayGroup.id.in_(groups)))
    changed_set = set(changed)
    out = []
    for period in rows:
        dates = [d for d in workweek.dates_between(period.period_start, period.period_end) if d in changed_set]
        if dates:
            out.append({"pay_group_id": period.pay_group_id, "pay_group": names.get(period.pay_group_id),
                        "period_start": period.period_start.isoformat(), "period_end": period.period_end.isoformat(),
                        "state": PERIOD_STATE[period.status], "changed_dates": len(dates)})
    return out


def _portions(resolve, start: date, end: date) -> Dict[date, Optional[str]]:
    return {day: resolve(day) for day in workweek.dates_between(start, end)}


# ---------------------------------------------------------------------------------------------- preview
def preview(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    """POST /api/v1/work-weeks/preview: create mode {rules, legal_entity_id?, from?, to?} or version mode
    {pattern_id, rules, effective_from?, from?, to?}. No writes. Half-day rules are previewed (V-9 gates saving)."""
    if not can_read(principal):
        raise forbidden()
    cid = principal.company_id
    pattern_id = data.get("pattern_id")
    row = _get_visible(db, principal, pattern_id) if pattern_id else None
    # stored: the saved pattern as it applies per date (each date under its own version; Schedule tab).
    stored = bool(data.get("stored")) and row is not None
    rules_in = data.get("rules")
    if rules_in is None and row is not None:
        rules_in = pattern_on(db, cid, row.id, _company_today(db, cid)).to_json()
    pattern = parse_rules(rules_in, allow_half_days=True)
    entity_id = row.legal_entity_id if row is not None else data.get("legal_entity_id")
    local_today = (pattern_local_today(db, cid, row.id) if row is not None
                   else _owner_local_today(db, cid, entity_id))
    effective_from = data.get("effective_from") or local_today
    start = data.get("from") or effective_from
    end = data.get("to") or (start + timedelta(days=PREVIEW_DEFAULT_DAYS - 1))
    try:
        if end < start:
            raise workweek.WorkWeekError("INVALID_RANGE", "the preview ends before it starts")
        if (end - start).days + 1 > workweek.MAX_RANGE_DAYS:
            raise workweek.WorkWeekError("RANGE_TOO_LONG", f"a preview covers at most {workweek.MAX_RANGE_DAYS} days")
    except workweek.WorkWeekError as exc:
        raise _engine_error(exc) from None
    resolver = WorkWeekResolver(db, cid) if row is not None else None

    def previous_on(day: date) -> Optional[workweek.WorkWeekPattern]:
        return resolver.version_on(row.id, day).pattern if resolver is not None else None

    marks = []
    for day in workweek.dates_between(start, end):
        rules = previous_on(day) if stored else (pattern if day >= effective_from or row is None else previous_on(day))
        portion = workweek.weekly_off_portion(rules, day)
        before = workweek.weekly_off_portion(previous_on(day), day) if row is not None else None
        marks.append({"date": day.isoformat(), "weekday": day.weekday(), "portion": portion,
                      "rest_day": workweek.is_rest_day(rules, day), "cycle_day": workweek.cycle_day(rules, day),
                      "occurrence": workweek.occurrence_in_month(day), "in_effect": stored or day >= effective_from,
                      "changed": not stored and row is not None and day >= effective_from and before != portion,
                      "previous_portion": before if row is not None else None})
    full = sum(1 for m in marks if m["portion"] == workweek.FULL)
    half = sum(1 for m in marks if m["portion"] in workweek.HALF_PORTIONS)
    previous = previous_on(effective_from - _ONE_DAY) if row is not None else None
    warnings, blocking = rest_findings(db, cid, previous, pattern, effective_from, row=row, entity_id=entity_id)
    result = {
        "mode": "version" if row is not None else "create",
        "pattern_id": row.id if row is not None else None,
        "rules": pattern.to_json(), "summary": pattern.summary(), "kind": pattern.kind,
        "effective_from": effective_from.isoformat(), "local_today": local_today.isoformat(),
        "backdated": effective_from < local_today, "from": start.isoformat(), "to": end.isoformat(),
        "days": marks,
        "totals": {"days": len(marks), "full_offs": full, "half_offs": half,
                   "working_days": len(marks) - full - half * 0.5 if half else len(marks) - full},
        "rest_check": {"warnings": warnings, "violations": blocking},
        "half_day_offs_enabled": HALF_DAY_OFFS_ENABLED,
        "issues": ([{"code": "INVALID_PORTION", "message": HALF_DAY_GATE_MESSAGE}]
                   if pattern.half_day_offs and not HALF_DAY_OFFS_ENABLED else []),
    }
    if row is not None and not stored:
        horizon_end = leave_jobs.horizon(effective_from)
        old = _portions(lambda d: workweek.weekly_off_portion(previous_on(d), d), effective_from, horizon_end)
        new = {d: workweek.weekly_off_portion(pattern, d) for d in old}
        same = workweek.version_on(_as_engine(_versions(db, cid, row.id)), effective_from)
        result["no_change"] = same is not None and same.pattern == pattern
        result["impact"] = impact(db, cid, row.id, old, new)
    else:
        result["no_change"] = False
        result["impact"] = None
    return result


# ---------------------------------------------------------------------------------------------- writes
def _audit(db: Session, principal: Optional[Principal], action: str, text: str, row: WorkWeekPattern,
           details: Mapping[str, Any]) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, text, "work_week_pattern", row.id, details=dict(details))


def _flush(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if getattr(exc.orig, "pgcode", None) == "23505":
            raise conflict("Another work week already uses this code.", "ORG_CODE_TAKEN") from None
        if getattr(exc.orig, "pgcode", None) == "23P01":
            raise conflict("Someone else changed the versions of this work week. Reload and try again.",
                           "ROW_VERSION_CONFLICT") from None
        raise bad_request("Work week: the change breaks a data rule.", "ORG_CONSTRAINT") from None


def create_pattern(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    """POST /api/v1/work-weeks: the pattern and version 1 (§5.9); rules from the body or a template."""
    cid = principal.company_id
    entity_id = data.get("legal_entity_id")
    if entity_id is not None:
        entity = db.query(LegalEntity).filter(LegalEntity.company_id == cid, LegalEntity.id == int(entity_id)).first()
        if entity is None:
            raise not_found("Legal entity")
        entity_id = entity.id
    if not can_manage(db, principal, entity_id):
        raise forbidden("Only company-wide holiday administrators can create tenant-wide work weeks."
                        if entity_id is None else "You cannot manage work weeks of this legal entity.")
    rules = data.get("rules")
    if rules is None and data.get("template_code"):
        template = workweek.TEMPLATES.get(str(data["template_code"]))
        if template is None:
            raise bad_request("Unknown work-week template.", "INVALID_TEMPLATE")
        rules = template.rules_for(_owner_local_today(db, cid, entity_id))
    pattern = parse_rules(rules)
    name = _clean_name(data.get("name"))
    code = _clean_code(data.get("code") or _code_from_name(name))
    if db.query(WorkWeekPattern.id).filter(WorkWeekPattern.company_id == cid, WorkWeekPattern.code == code).first():
        raise conflict("Another work week already uses this code.", "ORG_CODE_TAKEN")
    local_today = _owner_local_today(db, cid, entity_id)
    effective_from = data.get("effective_from") or local_today
    warnings, blocking = rest_findings(db, cid, None, pattern, effective_from, entity_id=entity_id)
    _check_rest(warnings, blocking, bool(data.get("acknowledge_warnings")))
    row = WorkWeekPattern(company_id=cid, code=code, name=name, name_i18n=dict(data.get("name_i18n") or {}),
                          legal_entity_id=entity_id, status="ACTIVE", row_version=1, kind=pattern.kind,
                          weekly_off_days=[], nth_weekday_offs=[], half_day_offs=[])
    _mirror(row, pattern)
    db.add(row)
    _flush(db)
    version = WorkWeekPatternVersion(company_id=cid, pattern_id=row.id, version=1, rules=pattern.to_json(),
                                     effective_from=effective_from, effective_to=None,
                                     reason=(data.get("reason") or None), content_hash=pattern.content_hash(),
                                     created_by_user_id=principal.user_id)
    db.add(version)
    _flush(db)
    time_inputs.log_change(db, cid, record_type="work_week_version", record_id=version.id, change="CREATED",
                           content=version.rules, date_from=effective_from, date_to=None,
                           actor_user_id=principal.user_id, details={"pattern_id": row.id, "version": 1})
    _audit(db, principal, "SAVE", f"Created work week {row.code} ({row.name})", row,
           {"after": {"code": row.code, "name": row.name, "legal_entity_id": entity_id, "rules": version.rules,
                      "effective_from": effective_from.isoformat()},
            "template_code": data.get("template_code"), "acknowledged_warnings": [w["rule"] for w in warnings]})
    _audit(db, principal, "WORKWEEK_VERSIONED", f"Work week {row.code} version 1 from {effective_from.isoformat()}",
           row, {"version": 1, "after": version.rules, "effective_from": effective_from.isoformat()})
    out = serialize(db, principal, row, detail=True)
    out["warnings"] = warnings
    return out


def update_pattern(db: Session, principal: Principal, pattern_id: int, data: Mapping[str, Any], *,
                   expected_row_version: Optional[int] = None) -> dict:
    """PATCH /api/v1/work-weeks/{id}: code, name, name_i18n and status in place; rules -> a new version (§5.8)."""
    row = _get_managed(db, principal, pattern_id)
    if expected_row_version is not None and expected_row_version != row.row_version:
        raise conflict("This work week was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    before = {"code": row.code, "name": row.name, "name_i18n": row.name_i18n, "status": row.status}
    if "code" in data and data["code"] is not None and data["code"] != row.code:
        code = _clean_code(data["code"])
        if usage_reason(db, row.company_id, row.id):
            raise conflict("The code of a work week in use cannot change.", "CODE_IMMUTABLE")
        if db.query(WorkWeekPattern.id).filter(WorkWeekPattern.company_id == row.company_id,
                                               WorkWeekPattern.code == code, WorkWeekPattern.id != row.id).first():
            raise conflict("Another work week already uses this code.", "ORG_CODE_TAKEN")
        row.code = code
    if "name" in data and data["name"] is not None:
        row.name = _clean_name(data["name"])
    if "name_i18n" in data and data["name_i18n"] is not None:
        row.name_i18n = dict(data["name_i18n"])
    if "status" in data and data["status"] is not None:
        if data["status"] not in ("ACTIVE", "INACTIVE"):
            raise bad_request("Status must be ACTIVE or INACTIVE.", "ORG_STATUS_INVALID")
        if data["status"] == "INACTIVE" and row.status != "INACTIVE":
            where = usage_reason(db, row.company_id, row.id)
            if where:
                raise conflict(f"Work week {row.code} is still used by {where}.", "PATTERN_IN_USE")
        row.status = data["status"]
    after = {"code": row.code, "name": row.name, "name_i18n": row.name_i18n, "status": row.status}
    version_result = None
    if data.get("rules") is not None:
        version_result = add_version(db, principal, row.id, {
            "rules": data["rules"], "effective_from": data.get("effective_from"), "reason": data.get("reason"),
            "acknowledge_warnings": data.get("acknowledge_warnings")}, expected_row_version=None, _row=row)
    if before != after:
        row.row_version += 1
        _flush(db)
        _audit(db, principal, "SAVE", f"Updated work week {row.code} ({row.name})", row,
               {"before": {k: v for k, v in before.items() if before[k] != after[k]},
                "after": {k: v for k, v in after.items() if before[k] != after[k]}})
    out = serialize(db, principal, row, detail=True)
    if version_result is not None:
        out["warnings"] = version_result.get("warnings", [])
        out["recount"] = version_result.get("recount")
    return out


def add_version(db: Session, principal: Optional[Principal], pattern_id: int, data: Mapping[str, Any], *,
                expected_row_version: Optional[int] = None, legacy: bool = False,
                _row: Optional[WorkWeekPattern] = None) -> dict:
    """POST /api/v1/work-weeks/{id}/versions (§5.2). ``legacy``: the old PATCH alias (never backdated, no-op when
    the rules are unchanged)."""
    row = _row or _get_managed(db, principal, pattern_id)
    cid = row.company_id
    if expected_row_version is not None and expected_row_version != row.row_version:
        raise conflict("This work week was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    pattern = parse_rules(data.get("rules"))
    local_today = pattern_local_today(db, cid, row.id)
    effective_from = local_today if legacy else (data.get("effective_from") or local_today)
    reason = (data.get("reason") or "").strip() or None
    backdated = effective_from < local_today
    if backdated:
        if principal is not None and not can_manage(db, principal, row.legal_entity_id, pattern_id=row.id,
                                                    code="holiday:publish"):
            raise forbidden("Backdated work-week changes need the 'Publish and backdate holidays' permission.")
        if not reason:
            raise bad_request("Give a reason for a backdated change.", "REASON_REQUIRED")
        limit = local_today - timedelta(days=backdate_days(db, cid))
        if effective_from < limit:
            raise AppError(422, f"A change can be backdated to {limit.isoformat()} at the earliest.", "BACKDATE_LIMIT")

    rows = _versions(db, cid, row.id)
    if not rows:
        rows = [ensure_first_version(db, row)]
    engine_versions = _as_engine(rows)
    valid = workweek.version_on(engine_versions, effective_from)
    if valid is not None and valid.pattern == pattern:
        if legacy:
            return {"version": None, "warnings": [], "recount": None, "no_change": True}
        raise conflict("These rules are already in effect on that date.", "NO_CHANGE")
    previous = (workweek.version_on(engine_versions, effective_from - _ONE_DAY) or valid)
    warnings, blocking = rest_findings(db, cid, previous.pattern if previous else None, pattern, effective_from,
                                       row=row)
    if not legacy:
        _check_rest(warnings, blocking, bool(data.get("acknowledge_warnings")))

    # Old rules per date over the recount horizon, before the tiling changes them.
    horizon_end = leave_jobs.horizon(effective_from)
    resolver = WorkWeekResolver(db, cid)
    old = _portions(lambda d: workweek.weekly_off_portion(resolver.version_on(row.id, d).pattern, d),
                    effective_from, horizon_end)

    plan = workweek.plan_version(engine_versions, effective_from)
    by_version = {r.version: r for r in rows}
    removed = [by_version[v] for v in plan.delete]
    before_details = [serialize_version(r, local_today) for r in removed]
    for r in removed:
        db.delete(r)
    _flush(db)
    if plan.close is not None:
        closing = by_version[plan.close[0]]
        closing.effective_to = plan.close[1]
        _flush(db)
    version = WorkWeekPatternVersion(company_id=cid, pattern_id=row.id, version=plan.new_version,
                                     rules=pattern.to_json(), effective_from=effective_from,
                                     effective_to=plan.new_effective_to, reason=reason,
                                     content_hash=pattern.content_hash(),
                                     created_by_user_id=principal.user_id if principal else None)
    db.add(version)
    _flush(db)
    company_today = _company_today(db, cid)
    if effective_from <= company_today and (plan.new_effective_to is None or company_today <= plan.new_effective_to):
        _mirror(row, pattern)
        outbox.emit(db, "workweek.version.activated", {"pattern_id": row.id, "version": version.version,
                                                        "effective_date": effective_from.isoformat()})
    row.row_version += 1
    _flush(db)

    actor = principal.user_id if principal else None
    time_inputs.log_change(db, cid, record_type="work_week_version", record_id=version.id, change="CREATED",
                           content=version.rules, date_from=effective_from, date_to=None, actor_user_id=actor,
                           details={"pattern_id": row.id, "version": version.version, "backdated": backdated})
    new = {d: workweek.weekly_off_portion(pattern, d) for d in old}
    changed = [d for d in old if old[d] != new[d]]
    recount = leave_jobs.recount_dates(db, cid, changed,
                                       key=f"recalc:workweek:{row.id}:{version.version}:{version.content_hash[:16]}",
                                       actor_user_id=actor)
    old_summary = previous.pattern.summary() if previous else "—"
    _audit(db, principal, "WORKWEEK_VERSIONED",
           f"Work week {row.code} version {version.version} from {effective_from.isoformat()}", row,
           {"version": version.version, "before": previous.pattern.to_json() if previous else None,
            "after": version.rules, "effective_from": effective_from.isoformat(), "reason": reason,
            "backdated": backdated, "replaced_versions": before_details, "legacy": legacy,
            "acknowledged_warnings": [w["rule"] for w in warnings]})
    future_changes = [d for d in changed if d >= company_today]
    if future_changes:
        _notify(db, cid, row, effective_from, old_summary, pattern.summary())
    return {"version": serialize_version(version, company_today), "warnings": warnings, "recount": recount,
            "changed_dates": len(changed), "no_change": False}


def _notify(db: Session, company_id: int, row: WorkWeekPattern, effective_from: date, old: str, new: str) -> int:
    """In-app notice to employees whose future weekly offs change (email arrives with templates in M5a)."""
    affected = _affected_employees(db, company_id, row.id, max(effective_from, _company_today(db, company_id)))
    employee_ids = sorted({a.employee_id for a, _ in affected})[:NOTIFY_LIMIT]
    if not employee_ids:
        return 0
    users = db.query(Employee.user_id).filter(Employee.company_id == company_id, Employee.id.in_(employee_ids),
                                              Employee.user_id.isnot(None)).all()
    for (user_id,) in users:
        NotificationService.create(
            db, user_id, "Your weekly offs change",
            f"From {effective_from.isoformat()} your weekly offs change: {old} → {new}.", "info", "/holidays")
    return len(users)


def new_version_for_legacy(db: Session, principal: Principal, row: WorkWeekPattern, rules: Mapping[str, Any]) -> None:
    """Legacy PATCH /payroll/org/work-weeks/{id} with rule fields: a version effective pattern-local today."""
    add_version(db, principal, row.id, {"rules": dict(rules)}, legacy=True, _row=row)


def duplicate_as_crew(db: Session, principal: Principal, pattern_id: int, data: Mapping[str, Any]) -> dict:
    """"Duplicate as crew" (CYCLE only, §3.4): same rules, anchor moved by ``offset_days``."""
    row = _get_visible(db, principal, pattern_id)
    if row.kind != workweek.CYCLE:
        raise bad_request("Only cycle work weeks can be duplicated as crews.", "KIND_MISMATCH")
    current = pattern_on(db, row.company_id, row.id, _company_today(db, row.company_id))
    offset = int(data.get("offset_days") or 0)
    rules = current.to_json()
    rules["anchor_date"] = (current.anchor_date + timedelta(days=offset)).isoformat()
    return create_pattern(db, principal, {"code": data.get("code"), "name": data.get("name"), "rules": rules,
                                          "legal_entity_id": row.legal_entity_id,
                                          "acknowledge_warnings": data.get("acknowledge_warnings")})


def company_zone(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.timezone if company is not None and company.timezone else "UTC"


def count_versions(db: Session, company_id: int) -> int:
    return db.query(func.count(WorkWeekPatternVersion.id)).filter(WorkWeekPatternVersion.company_id == company_id).scalar()


def history(db: Session, principal: Principal, pattern_id: int, *, limit: int = 100) -> List[dict]:
    """The pattern's audit trail (History tab): action, actor, time and description; no free-text details."""
    from app.models.audit_log import AuditLog

    row = _get_visible(db, principal, pattern_id)
    rows = (db.query(AuditLog)
            .filter(AuditLog.company_id == principal.company_id, AuditLog.module == AUDIT_MODULE,
                    AuditLog.entity_type == "work_week_pattern", AuditLog.entity_id == str(row.id))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).all())
    return [{"id": r.id, "action": r.action, "description": r.description, "actor_email": r.actor_email,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]
