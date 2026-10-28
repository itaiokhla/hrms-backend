"""Rotation patterns, versions, assignments and draft generation (AS-M5; 03-shifts/02 §2-§3).

A pattern is created from one of the 22 platform templates (``rotation_engine.TEMPLATES``, copied on use with a
token → shift map) or from scratch; a version is validated structurally (``rotation_engine.validate_pattern``) and
linted (WARN only); an ACTIVE version is never edited. Rotation assignments (employee targets until X-M1 ships CREW
teams) never overlap per employee (409 ``ROTATION_OVERLAP``). ``generate_drafts`` expands the assignments over a
roster period into ``ROTATION`` drafts (drafts stay invisible until the period is published).

Services never commit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.concurrency import lock_employees
from app.core.errors import AppError, conflict, not_found
from app.core.principal import Principal
from app.models.employee import Shift
from app.models.rosters import RosterDraft, RotationAssignment, RotationPattern, RotationPatternVersion
from app.services import rotation_engine as engine
from app.services import shift_rosters
from app.services.audit_log_service import AuditLogService

MODULE = "Rosters"
BACKDATE_DAYS = 7  # shift.roster.backdate_days default (03-shifts/02 §4.3)
GENERATE_AHEAD_DAYS = 90


def templates() -> List[dict]:
    return [{"key": t.key, "number": t.number, "name": t.name, "cycle_days": t.cycle_days, "tokens": t.tokens,
             "crew_rows": dict(t.crew_rows) if t.crew_rows else None, "crew_offsets": dict(t.crew_offsets),
             "coverage": dict(t.coverage), "week_anchor_weekday": t.week_anchor_weekday, "note": t.note,
             "default_shifts": {k: {"start": v.start, "end": v.end} for k, v in t.default_shifts.items()}
             if hasattr(next(iter(t.default_shifts.values()), None), "start") else {}}
            for t in engine.TEMPLATES.values()]


def _data(row: RotationPatternVersion, code: Optional[str] = None) -> engine.PatternVersionData:
    return engine.PatternVersionData(cycle_days=row.cycle_days, tokens=row.tokens, crew_rows=row.crew_rows,
                                     crew_offsets=row.crew_offsets or {}, token_map=row.token_map or {},
                                     coverage=row.coverage or {}, week_anchor_weekday=row.week_anchor_weekday,
                                     lints_muted=frozenset(row.lints_muted or ()), code=code, version_no=row.version_no)


def _content_hash(values: dict) -> str:
    keys = ("cycle_days", "tokens", "crew_rows", "crew_offsets", "token_map", "coverage", "week_anchor_weekday")
    return hashlib.sha256(json.dumps({k: values.get(k) for k in keys}, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def serialize_version(row: RotationPatternVersion) -> dict:
    return {"id": row.id, "pattern_id": row.pattern_id, "version_no": row.version_no, "status": row.status,
            "cycle_days": row.cycle_days, "tokens": row.tokens, "crew_rows": row.crew_rows,
            "crew_offsets": row.crew_offsets, "token_map": row.token_map, "coverage": row.coverage,
            "week_anchor_weekday": row.week_anchor_weekday, "lints_muted": list(row.lints_muted or []),
            "content_hash": row.content_hash, "row_version": row.row_version}


def serialize_pattern(row: RotationPattern, versions: List[RotationPatternVersion] = ()) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name, "template_key": row.template_key,
            "branch_id": row.branch_id, "legal_entity_id": row.legal_entity_id, "status": row.status,
            "current_version_id": row.current_version_id, "row_version": row.row_version,
            "versions": [serialize_version(v) for v in versions]}


def _check_token_shifts(db: Session, company_id: int, token_map: dict) -> None:
    ids = [engine._ref(v) for v in token_map.values() if v != "OFF"]
    found = {s.id: s for s in db.query(Shift).filter(Shift.company_id == company_id,
                                                     Shift.id.in_([i for i in ids if isinstance(i, int)] or [0]))}
    for ref in ids:
        if not isinstance(ref, int) or ref not in found:
            raise AppError(422, "Map every token to a shift of this company.", "TOKEN_UNMAPPED",
                           extra={"details": {"shift": ref}})
        if found[ref].status != "ACTIVE":
            raise AppError(422, f"Shift {found[ref].code} is archived.", "SHIFT_ARCHIVED")


def _version_values(body: dict, template: Optional[engine.RotationTemplate]) -> dict:
    values = {
        "cycle_days": body.get("cycle_days") or (template.cycle_days if template else None),
        "tokens": body.get("tokens") if body.get("tokens") is not None or body.get("crew_rows") else (
            template.tokens if template else None),
        "crew_rows": body.get("crew_rows") or (dict(template.crew_rows) if template and template.crew_rows else None),
        "crew_offsets": body.get("crew_offsets") or (dict(template.crew_offsets) if template else {}),
        "token_map": body.get("token_map") or {},
        "coverage": body.get("coverage") or (dict(template.coverage) if template else {}),
        "week_anchor_weekday": body.get("week_anchor_weekday", template.week_anchor_weekday if template else None),
        "lints_muted": list(body.get("lints_muted") or []),
    }
    if values["crew_rows"]:
        values["tokens"] = None
    if not values["cycle_days"]:
        raise AppError(422, "Give the cycle length.", "VALIDATION_ERROR")
    return values


def _shift_times(db: Session, company_id: int, token_map: dict) -> Dict[int, engine.ShiftTimes]:
    """``{shift_id: ShiftTimes}`` of the mapped shifts (mirror times and net minutes) for validation and lints."""
    ids = [r for r in (engine._ref(v) for v in token_map.values() if v != "OFF") if isinstance(r, int)]
    out: Dict[int, engine.ShiftTimes] = {}
    for shift in db.query(Shift).filter(Shift.company_id == company_id, Shift.id.in_(ids or [0])):
        net = None
        if shift.duration_hours is not None:
            net = int(round(float(shift.duration_hours) * 60)) - int(shift.unpaid_break_minutes or 0)
        out[shift.id] = engine.ShiftTimes(start=shift.start_time, end=shift.end_time, net_minutes=net,
                                          long_duty=shift.start_time == shift.end_time,
                                          archived=shift.status != "ACTIVE")
    return out


def _validate(values: dict, shifts: Dict[int, engine.ShiftTimes]) -> List[dict]:
    data = engine.PatternVersionData(**{k: v for k, v in values.items() if k != "lints_muted"},
                                     lints_muted=frozenset(values["lints_muted"]))
    issues = engine.validate_pattern(data, shifts)
    if issues:
        raise AppError(422, issues[0].message, issues[0].code,
                       extra={"issues": [{"code": i.code, "rule": i.rule, "message": i.message} for i in issues]})
    return [shift_rosters._result_json(r) for r in engine.lint(data, shifts)]


def create_pattern(db: Session, principal: Principal, data) -> dict:
    cid = principal.company_id
    if db.query(RotationPattern.id).filter(RotationPattern.company_id == cid, RotationPattern.code == data.code).first():
        raise conflict("Another rotation pattern uses this code.", "CONFLICT")
    template = engine.TEMPLATES.get(data.template_key) if data.template_key else None
    if data.template_key and template is None:
        raise not_found("Rotation template")
    pattern = RotationPattern(company_id=cid, code=data.code, name=data.name, template_key=data.template_key,
                              branch_id=data.branch_id, legal_entity_id=data.legal_entity_id, status="ACTIVE")
    db.add(pattern)
    db.flush()
    version = None
    if data.version is not None:
        version = _add_version(db, principal, pattern, data.version, template)
    AuditLogService.log(db, principal, MODULE, "ROTATION_PATTERN_SAVED", f"Created rotation pattern {pattern.code}",
                        "rotation_pattern", pattern.id)
    return {**serialize_pattern(pattern, [version["row"]] if version else []),
            "lints": version["lints"] if version else []}


def _add_version(db: Session, principal: Principal, pattern: RotationPattern, body: dict,
                 template: Optional[engine.RotationTemplate]) -> dict:
    values = _version_values(body, template)
    _check_token_shifts(db, principal.company_id, values["token_map"])
    lints = _validate(values, _shift_times(db, principal.company_id, values["token_map"]))
    number = int(db.query(func.coalesce(func.max(RotationPatternVersion.version_no), 0)).filter(
        RotationPatternVersion.company_id == pattern.company_id,
        RotationPatternVersion.pattern_id == pattern.id).scalar()) + 1
    row = RotationPatternVersion(company_id=pattern.company_id, pattern_id=pattern.id, version_no=number,
                                 status="DRAFT", content_hash=_content_hash(values), **values)
    db.add(row)
    db.flush()
    return {"row": row, "lints": lints}


def _load_pattern(db: Session, principal: Principal, pattern_id: int, *, lock: bool = False) -> RotationPattern:
    query = db.query(RotationPattern).filter(RotationPattern.company_id == principal.company_id,
                                             RotationPattern.id == pattern_id)
    row = (query.with_for_update() if lock else query).first()
    if row is None:
        raise not_found("Rotation pattern")
    return row


def add_version(db: Session, principal: Principal, pattern_id: int, data) -> dict:
    pattern = _load_pattern(db, principal, pattern_id, lock=True)
    template = engine.TEMPLATES.get(pattern.template_key) if pattern.template_key else None
    result = _add_version(db, principal, pattern, data.model_dump(), template)
    return {**serialize_version(result["row"]), "lints": result["lints"]}


def activate_version(db: Session, principal: Principal, pattern_id: int, version_no: int) -> dict:
    pattern = _load_pattern(db, principal, pattern_id, lock=True)
    row = db.query(RotationPatternVersion).filter(RotationPatternVersion.company_id == pattern.company_id,
                                                  RotationPatternVersion.pattern_id == pattern.id,
                                                  RotationPatternVersion.version_no == version_no).first()
    if row is None:
        raise not_found("Rotation pattern version")
    if row.status != "DRAFT":
        raise AppError(409, "Only a draft version can be activated.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": row.status}})
    db.query(RotationPatternVersion).filter(RotationPatternVersion.company_id == pattern.company_id,
                                            RotationPatternVersion.pattern_id == pattern.id,
                                            RotationPatternVersion.status == "ACTIVE").update(
        {"status": "SUPERSEDED"}, synchronize_session=False)
    row.status, row.activated_at = "ACTIVE", clock.utcnow()
    pattern.current_version_id = row.id
    pattern.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, MODULE, "ROTATION_VERSION_ACTIVATED",
                        f"Rotation {pattern.code}: version {row.version_no} active", "rotation_pattern", pattern.id)
    return serialize_version(row)


def list_patterns(db: Session, principal: Principal) -> List[dict]:
    rows = db.query(RotationPattern).filter(RotationPattern.company_id == principal.company_id).order_by(
        RotationPattern.code).all()
    versions: Dict[int, List[RotationPatternVersion]] = {}
    for v in db.query(RotationPatternVersion).filter(RotationPatternVersion.company_id == principal.company_id).order_by(
            RotationPatternVersion.version_no):
        versions.setdefault(v.pattern_id, []).append(v)
    return [serialize_pattern(r, versions.get(r.id, [])) for r in rows]


def preview(db: Session, principal: Principal, pattern_id: int, version_no: int, start: date, end: date,
            crew_code: Optional[str] = None, anchor_date: Optional[date] = None, offset_days: int = 0) -> dict:
    pattern = _load_pattern(db, principal, pattern_id)
    row = db.query(RotationPatternVersion).filter(RotationPatternVersion.company_id == pattern.company_id,
                                                  RotationPatternVersion.pattern_id == pattern.id,
                                                  RotationPatternVersion.version_no == version_no).first()
    if row is None:
        raise not_found("Rotation pattern version")
    days = engine.expand(_data(row, pattern.code), engine.AssignmentData(anchor_date=anchor_date or start,
                                                                        offset_days=offset_days, crew_code=crew_code),
                         start, end)
    return {"days": [{"date": d.date.isoformat(), "token": d.token, "kind": d.kind, "shift_id": d.shift}
                     for d in days]}


def serialize_assignment(row: RotationAssignment) -> dict:
    return {"id": row.id, "pattern_version_id": row.pattern_version_id, "employee_id": row.employee_id,
            "crew_code": row.crew_code, "offset_days": row.offset_days, "anchor_date": row.anchor_date.isoformat(),
            "effective_from": row.effective_from.isoformat(),
            "effective_to": row.effective_to.isoformat() if row.effective_to else None, "status": row.status}


def assign(db: Session, principal: Principal, data) -> dict:
    """One assignment per employee from ``effective_from`` (409 ``ROTATION_OVERLAP`` on a second active one)."""
    version = db.query(RotationPatternVersion).filter(RotationPatternVersion.company_id == principal.company_id,
                                                      RotationPatternVersion.id == data.pattern_version_id).first()
    if version is None:
        raise not_found("Rotation pattern version")
    if version.status != "ACTIVE":
        raise AppError(409, "Activate the version before assigning it.", "INVALID_TRANSITION")
    if data.crew_code and version.crew_rows and data.crew_code not in version.crew_rows and \
            data.crew_code not in (version.crew_offsets or {}):
        raise AppError(422, "Unknown crew for this pattern.", "VALIDATION_ERROR")
    shift_rosters._covering(db, principal, "shift:assign", data.employee_ids)
    lock_employees(db, data.employee_ids)
    from app.models.employee import Employee
    from app.services.org_structure_service import employee_local_today

    for employee_id in sorted(set(data.employee_ids)):
        floor = employee_local_today(db, db.get(Employee, employee_id)) - timedelta(days=BACKDATE_DAYS)
        if data.effective_from < floor:  # EC AS-ROT-06: no exemption channel outside publish
            raise AppError(422, f"A rotation can start at most {BACKDATE_DAYS} days in the past.", "BACKDATE_LIMIT",
                           extra={"details": {"employee_id": employee_id, "earliest": floor.isoformat()}})
    created = []
    for employee_id in sorted(set(data.employee_ids)):
        clash = db.query(RotationAssignment.id).filter(
            RotationAssignment.company_id == principal.company_id, RotationAssignment.employee_id == employee_id,
            RotationAssignment.status == "ACTIVE",
            or_(RotationAssignment.effective_to.is_(None), RotationAssignment.effective_to >= data.effective_from),
            *([RotationAssignment.effective_from <= data.effective_to] if data.effective_to else [])).first()
        if clash:
            raise AppError(409, "This employee already follows a rotation on these dates.", "ROTATION_OVERLAP",
                           extra={"details": {"employee_id": employee_id}})
        row = RotationAssignment(company_id=principal.company_id, pattern_version_id=version.id,
                                 target_kind="EMPLOYEE", employee_id=employee_id, crew_code=data.crew_code,
                                 offset_days=data.offset_days, anchor_date=data.anchor_date,
                                 effective_from=data.effective_from, effective_to=data.effective_to, status="ACTIVE")
        db.add(row)
        created.append(row)
    db.flush()
    AuditLogService.log(db, principal, MODULE, "ROTATION_ASSIGNED", f"Assigned rotation to {len(created)} employee(s)",
                        "rotation_pattern_version", version.id)
    return {"items": [serialize_assignment(r) for r in created]}


def generate_drafts(db: Session, principal: Principal, period_id: int, *, overwrite: bool = False) -> dict:
    """Expand every active assignment of the period's employees into ``ROTATION`` drafts (§3.5)."""
    period = shift_rosters.load_period(db, principal, period_id, lock=True)
    employees = shift_rosters._owner_employees(db, period)
    shift_rosters._covering(db, principal, "roster:plan", employees)
    rows = db.query(RotationAssignment, RotationPatternVersion, RotationPattern).join(
        RotationPatternVersion, (RotationPatternVersion.company_id == RotationAssignment.company_id)
        & (RotationPatternVersion.id == RotationAssignment.pattern_version_id)).join(
        RotationPattern, (RotationPattern.company_id == RotationPatternVersion.company_id)
        & (RotationPattern.id == RotationPatternVersion.pattern_id)).filter(
        RotationAssignment.company_id == period.company_id, RotationAssignment.status == "ACTIVE",
        RotationAssignment.employee_id.in_(employees or [0]), RotationAssignment.effective_from <= period.end_date,
        or_(RotationAssignment.effective_to.is_(None), RotationAssignment.effective_to >= period.start_date)).all()
    existing = {(d.employee_id, d.shift_date, d.entry_seq): d for d in db.query(RosterDraft).filter(
        RosterDraft.company_id == period.company_id, RosterDraft.roster_period_id == period.id)}
    written = 0
    for assignment, version, pattern in rows:
        days = engine.expand(_data(version, pattern.code),
                             engine.AssignmentData(anchor_date=assignment.anchor_date, offset_days=assignment.offset_days,
                                                   crew_code=assignment.crew_code,
                                                   effective_from=assignment.effective_from,
                                                   effective_to=assignment.effective_to),
                             period.start_date, period.end_date)
        for day in days:
            key = (assignment.employee_id, day.date, 1)
            draft = existing.get(key)
            if draft is not None and not overwrite and draft.source != "ROTATION":
                continue  # a planner's edit wins over the generator
            if draft is None:
                draft = RosterDraft(company_id=period.company_id, roster_period_id=period.id,
                                    employee_id=assignment.employee_id, shift_date=day.date, entry_seq=1)
                db.add(draft)
                existing[key] = draft
            draft.action, draft.kind = "UPSERT", day.kind
            draft.shift_id = day.shift if day.kind == "WORK" else None
            draft.source = day.source
            written += 1
    if period.status == "PUBLISHED" and written:
        period.status = "AMENDING"
    period.row_version += 1
    db.flush()
    return {"generated": written, "assignments": len(rows)}


def _month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def generate_ahead(db: Session, principal: Principal, branch_id: int, *, start: Optional[date] = None,
                   days: int = GENERATE_AHEAD_DAYS) -> dict:
    """The roster generator (WI-AS-5.3 ``roster.rotation_generate``): covers ``days`` (default 90) from the branch-
    local today with calendar-month roster periods (each <= 62 days; existing periods of the branch are reused,
    cancelled ones skipped) and expands every active rotation assignment into ``ROTATION`` drafts in each."""
    from types import SimpleNamespace

    import pytz

    from app.models.org import Branch
    from app.models.rosters import RosterPeriod

    if not 1 <= days <= 366:
        raise AppError(422, "Generate between 1 and 366 days ahead.", "VALIDATION_ERROR")
    branch = db.query(Branch).filter(Branch.company_id == principal.company_id, Branch.id == branch_id).first()
    if branch is None:
        raise not_found("Branch")
    if start is None:
        zone = shift_rosters._branch_zone(db, principal.company_id, branch_id)
        start = clock.utcnow().astimezone(pytz.timezone(zone)).date()
    end = start + timedelta(days=days - 1)
    periods = db.query(RosterPeriod).filter(RosterPeriod.company_id == principal.company_id,
                                            RosterPeriod.branch_id == branch_id, RosterPeriod.team_id.is_(None),
                                            RosterPeriod.status != "CANCELLED", RosterPeriod.start_date <= end,
                                            RosterPeriod.end_date >= start).order_by(RosterPeriod.start_date).all()
    cursor = start
    while cursor <= end:
        covering = next((p for p in periods if p.start_date <= cursor <= p.end_date), None)
        if covering is not None:
            cursor = covering.end_date + timedelta(days=1)
            continue
        later = sorted(p.start_date for p in periods if p.start_date > cursor)
        stop = min(_month_end(cursor), end, later[0] - timedelta(days=1) if later else end)
        code = f"RP-{branch.code}-{cursor.strftime('%Y-%m')}" + ("" if cursor.day == 1 else f"-{cursor.day:02d}")
        created = shift_rosters.create_period(db, principal, SimpleNamespace(
            code=code[:40], name=f"{branch.name} {cursor.strftime('%B %Y')}"[:100], branch_id=branch_id,
            team_id=None, department_ids=None, start_date=cursor, end_date=stop, publish_notice_days=None))
        periods.append(db.get(RosterPeriod, created["id"]))
        cursor = stop + timedelta(days=1)
    results = []
    for period in sorted(periods, key=lambda p: p.start_date):
        if period.status in ("CLOSED", "CANCELLED"):
            continue
        out = generate_drafts(db, principal, period.id)
        results.append({"period_id": period.id, "code": period.code, "start_date": period.start_date.isoformat(),
                        "end_date": period.end_date.isoformat(), "generated": out["generated"]})
    return {"from": start.isoformat(), "to": end.isoformat(), "days": days, "periods": results,
            "generated": sum(r["generated"] for r in results)}
