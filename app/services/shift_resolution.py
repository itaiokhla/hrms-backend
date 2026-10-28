"""Which shift applies to employee E on shift date D (AS-M1; 03-shifts/01 §11; D-AS-13, D-AS-65; fixes BUG-27 once).

One helper for every reader (attendance, LH day facts, payroll standard hours, overtime day types, explain):

0. E has no primary placement valid on D -> ``None`` (not employed);
1. published, not withdrawn roster entries (AS-M5, ``shift_rosters.entries_many``): the first WORK entry in
   ``entry_seq`` order (its stored ``shift_version_id``, or custom times as a FIXED block); an OFF entry = no shift;
2. the ACTIVE ``shift_assignment_rules`` row valid on D whose criteria match E's placement **as of D**, highest
   priority (``policy_types.resolve_assignment``); a rule whose shift is owned by another branch / legal entity
   than E's placement on D is skipped (``OWNER_SCOPE_MISMATCH``); an ``AMBIGUOUS`` tie (refused at save) fails
   closed to step 3;
3. the primary placement's ``shift_id`` valid on D;
4. nothing -> source ``NONE`` (callers use ``payroll.standard_hours_per_day``).

Every resolved shift uses the non-void version valid on D; a shift without versions (inserted outside the catalogue
service) is read through its legacy columns. The batch form loads placements, rules, shifts and versions for the whole
range with a fixed number of queries and resolves in memory (spine §5.11).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.company import Company
from app.models.employee import Shift
from app.models.org import Branch, EmployeeAssignment, LegalEntity
from app.models.pay_org import EmploymentType
from app.models.shifts import ShiftAssignmentRule, ShiftVersion
from app.payroll import policy_types as pt
from app.services import shift_engine as engine

MAX_EMPLOYEE_DAYS = 200_000  # LH day_facts_many cap (RANGE_TOO_LONG)
ONE = Decimal(1)


def _roster_entries(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date) -> dict:
    """``{(employee_id, date): [RosterEntry rows, entry_seq order]}`` of published, not withdrawn entries."""
    from app.services import shift_rosters

    return shift_rosters.entries_many(db, company_id, employee_ids, start, end)


@dataclass(frozen=True)
class ResolvedShift:
    """The shift that applies on one date and where it came from."""

    shift_date: date
    source: str  # ROSTER | RULE | PLACEMENT
    shift_id: int
    shift_version_id: Optional[int]
    version: engine.ShiftVersionData
    work_timezone: str
    rule_id: Optional[int] = None
    assignment_id: Optional[int] = None
    roster_entry_id: Optional[int] = None
    entry_seq: Optional[int] = None
    # Legacy gross hours of the version (or of the shift without versions), for the zero-diff standard hours.
    duration_hours: Optional[Decimal] = None
    explain: Tuple[dict, ...] = field(default=(), compare=False)


# ------------------------------------------------------------------------------------------------- loading
def _dates(start: date, end: date) -> List[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _placements(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date):
    rows = (db.query(EmployeeAssignment, EmploymentType.code)
            .outerjoin(EmploymentType, (EmploymentType.company_id == EmployeeAssignment.company_id)
                       & (EmploymentType.id == EmployeeAssignment.employment_type_id))
            .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(list(employee_ids)),
                    EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= end,
                    or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start))
            .order_by(EmployeeAssignment.employee_id, EmployeeAssignment.effective_from, EmployeeAssignment.id)
            .all())
    by_employee: Dict[int, list] = {}
    for assignment, employment_type in rows:
        by_employee.setdefault(assignment.employee_id, []).append((assignment, employment_type))
    return by_employee


def _placement_on(rows: list, day: date):
    found = None
    for assignment, employment_type in rows:
        if assignment.effective_from <= day and (assignment.effective_to is None or day <= assignment.effective_to):
            found = (assignment, employment_type)  # the latest start wins when rows touch
    return found


def _rules(db: Session, company_id: int, start: date, end: date) -> List[ShiftAssignmentRule]:
    return (db.query(ShiftAssignmentRule)
            .filter(ShiftAssignmentRule.company_id == company_id, ShiftAssignmentRule.status == "ACTIVE",
                    ShiftAssignmentRule.effective_from <= end,
                    or_(ShiftAssignmentRule.effective_to.is_(None), ShiftAssignmentRule.effective_to >= start))
            .order_by(ShiftAssignmentRule.id).all())


def version_data(row: ShiftVersion, kind: str) -> engine.ShiftVersionData:
    return engine.ShiftVersionData(
        kind=kind, start_time=row.start_time, end_time=row.end_time, segments=row.segments or (),
        break_rules=row.break_rules or (), unpaid_break_minutes=row.unpaid_break_minutes,
        half_split_time=row.half_split_time, core_start_time=row.core_start_time, core_end_time=row.core_end_time,
        expected_net_minutes=row.expected_net_minutes, grace_in_minutes=row.grace_in_minutes,
        grace_out_minutes=row.grace_out_minutes, early_in_window_minutes=row.early_in_window_minutes,
        late_out_window_minutes=row.late_out_window_minutes, anchor_timezone=row.anchor_timezone,
        long_duty=row.long_duty, safety_critical=row.safety_critical, outdoor=row.outdoor, shift_id=row.shift_id,
        shift_version_id=row.id, version_no=row.version_no, effective_from=row.effective_from,
        effective_to=row.effective_to)


def legacy_data(shift: Shift) -> engine.ShiftVersionData:
    """A shift that has no version row: its legacy columns as one open-ended version (zero diff)."""
    kind = shift.kind or ("FLEXIBLE" if shift.is_flexible else "FIXED")
    return engine.ShiftVersionData(
        kind=kind, start_time=shift.start_time, end_time=shift.end_time,
        unpaid_break_minutes=shift.unpaid_break_minutes, long_duty=shift.start_time == shift.end_time,
        shift_id=shift.id)


class _Catalogue:
    """Shifts and their non-void versions, loaded once per call."""

    def __init__(self, db: Session, company_id: int, shift_ids: Iterable[int]):
        ids = sorted({i for i in shift_ids if i is not None})
        self.shifts: Dict[int, Shift] = {}
        self.versions: Dict[int, List[ShiftVersion]] = {}
        if not ids:
            return
        for shift in db.query(Shift).filter(Shift.company_id == company_id, Shift.id.in_(ids)):
            self.shifts[shift.id] = shift
        for row in (db.query(ShiftVersion)
                    .filter(ShiftVersion.company_id == company_id, ShiftVersion.shift_id.in_(ids),
                            ShiftVersion.voided_at.is_(None))
                    .order_by(ShiftVersion.shift_id, ShiftVersion.effective_from)):
            self.versions.setdefault(row.shift_id, []).append(row)

    def version_by_id(self, version_id: int) -> Optional[ShiftVersion]:
        return next((r for rows in self.versions.values() for r in rows if r.id == version_id), None)

    def on(self, shift_id: int, day: date) -> Tuple[Optional[ShiftVersion], Optional[engine.ShiftVersionData],
                                                    Optional[Decimal]]:
        shift = self.shifts.get(shift_id)
        if shift is None:
            return None, None, None
        rows = self.versions.get(shift_id)
        if not rows:
            return None, legacy_data(shift), shift.duration_hours
        for row in rows:
            if row.effective_from <= day and (row.effective_to is None or day <= row.effective_to):
                data = version_data(row, shift.kind)
                if row.id == shift.current_version_id and not row.segments and not row.break_rules:
                    # Today's legacy-shaped version reads the shift's mirror columns, which legacy writers (and
                    # direct SQL) still change in place: payroll sees exactly what it saw before AS-M1.
                    data = replace(data, unpaid_break_minutes=shift.unpaid_break_minutes)
                    return row, data, shift.duration_hours
                return row, data, row.duration_hours
        return None, None, None  # dated before version 1 (never for migrated shifts, which start 2000-01-01)


class _Zones:
    def __init__(self, db: Session, company_id: int, placements: Dict[int, list]):
        branch_ids = {a.branch_id for rows in placements.values() for a, _ in rows if a.branch_id}
        entity_ids = {a.legal_entity_id for rows in placements.values() for a, _ in rows if a.legal_entity_id}
        self.branches = {b.id: b for b in db.query(Branch).filter(Branch.company_id == company_id,
                                                                  Branch.id.in_(branch_ids))} if branch_ids else {}
        self.entities = {e.id: e for e in db.query(LegalEntity).filter(LegalEntity.company_id == company_id,
                                                                       LegalEntity.id.in_(entity_ids))} if entity_ids else {}
        company = db.get(Company, company_id)
        self.company_zone = (company.timezone if company is not None else None) or "UTC"

    def of(self, assignment: EmployeeAssignment) -> str:
        explicit = getattr(assignment, "work_timezone", None)  # LH-M1 column, when present
        branch = self.branches.get(assignment.branch_id)
        entity = self.entities.get(assignment.legal_entity_id)
        for zone in (explicit, getattr(branch, "timezone", None), getattr(entity, "timezone", None)):
            if zone:
                return zone
        return self.company_zone


def _attributes(assignment: EmployeeAssignment, employment_type: Optional[str]) -> pt.EmployeeAttributes:
    return pt.EmployeeAttributes(
        employee_id=assignment.employee_id, legal_entity_id=assignment.legal_entity_id,
        branch_id=assignment.branch_id, pay_group_id=assignment.pay_group_id, department_id=assignment.department_id,
        grade_id=assignment.grade_id, employment_type=employment_type, designation_id=assignment.designation_id)


def owner_scope_contains(shift: Shift, assignment: EmployeeAssignment) -> bool:
    """A branch- or entity-owned shift applies only to people placed in that unit (§2.3, SP-AS-163)."""
    if shift.branch_id is not None and assignment.branch_id != shift.branch_id:
        return False
    if shift.legal_entity_id is not None and assignment.legal_entity_id != shift.legal_entity_id:
        return False
    return True


def _assignment_rules(rows: Sequence[ShiftAssignmentRule]) -> List[Tuple[ShiftAssignmentRule, pt.AssignmentRule]]:
    from app.services.shift_criteria import parse_shift_criteria

    built = []
    for row in rows:
        try:
            built.append((row, pt.AssignmentRule(rule_id=row.id, policy_id=row.shift_id, priority=row.priority,
                                                 criteria=parse_shift_criteria(row.criteria),
                                                 effective_from=row.effective_from,
                                                 effective_to=row.effective_to, status=row.status)))
        except pt.PolicyValidationError:
            continue  # a stored criteria object the current schema rejects never matches
    return built


def _team_memberships(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date,
                      rules) -> Dict[int, list]:
    """Membership rows overlapping the range, loaded only when a rule names a team dimension (X-M1, D-X-09)."""
    if not any(getattr(rule.criteria, "has_team_dimensions", False) for _, rule in rules):
        return {}
    from app.models.teams import TeamMembership

    rows = db.query(TeamMembership).filter(
        TeamMembership.company_id == company_id, TeamMembership.employee_id.in_(list(employee_ids)),
        TeamMembership.effective_from <= end,
        or_(TeamMembership.effective_to.is_(None), TeamMembership.effective_to >= start)).all()
    result: Dict[int, list] = {}
    for row in rows:
        result.setdefault(row.employee_id, []).append(row)
    return result


def _teams_on(rows: Sequence, day: date) -> Dict[int, str]:
    return {r.team_id: r.member_role for r in rows
            if r.effective_from <= day and (r.effective_to is None or r.effective_to >= day)}


# ------------------------------------------------------------------------------------------------ resolving
def shifts_for_many(db: Session, company_id: int, employee_ids: Sequence[int], start: date, end: date, *,
                    with_explain: bool = False, extra_rules: Sequence[ShiftAssignmentRule] = (),
                    skip_rule_ids: Iterable[int] = ()) -> Dict[Tuple[int, date], Optional[ResolvedShift]]:
    """``{(employee_id, date): ResolvedShift | None}`` for every employee and date of ``start..end``.

    ``extra_rules`` / ``skip_rule_ids`` let simulate and preview resolve with a draft rule (never stored).
    """
    if end < start:
        raise AppError(400, "The end date is before the start date.", "INVALID_RANGE")
    ids = sorted(set(employee_ids))
    days = _dates(start, end)
    if len(ids) * len(days) > MAX_EMPLOYEE_DAYS:
        raise AppError(400, "Choose fewer employees or a shorter range.", "RANGE_TOO_LONG")
    result: Dict[Tuple[int, date], Optional[ResolvedShift]] = {(e, d): None for e in ids for d in days}
    if not ids:
        return result
    placements = _placements(db, company_id, ids, start, end)
    skip = set(skip_rule_ids)
    rule_rows = [r for r in _rules(db, company_id, start, end) if r.id not in skip] + list(extra_rules)
    rules = _assignment_rules(rule_rows)
    memberships = _team_memberships(db, company_id, ids, start, end, rules)
    roster = _roster_entries(db, company_id, ids, start, end)
    shift_ids = ({r.shift_id for r in rule_rows} | {a.shift_id for rows in placements.values() for a, _ in rows}
                 | {e.shift_id for entries in roster.values() for e in entries})
    catalogue = _Catalogue(db, company_id, shift_ids)
    zones = _Zones(db, company_id, placements)
    for employee_id in ids:
        rows = placements.get(employee_id, [])
        for day in days:
            current = _placement_on(rows, day)
            if current is None:
                continue
            assignment, employment_type = current
            zone = zones.of(assignment)
            explain: List[dict] = []
            entries = roster.get((employee_id, day))
            if entries:
                resolved = _from_roster(day, entries, zone, catalogue, assignment, explain, with_explain)
                if with_explain:
                    explain.append({"step": "RULE", "won": False, "reason": "ROSTER_WINS"})
                    explain.append({"step": "PLACEMENT", "shift_id": assignment.shift_id, "won": False,
                                    "reason": "ROSTER_WINS"})
            else:
                resolved = _resolve_day(day, assignment, employment_type, zone, rules, catalogue, explain,
                                        with_explain, _teams_on(memberships.get(employee_id, ()), day))
            if resolved is not None and with_explain:
                resolved = ResolvedShift(**{**resolved.__dict__, "explain": tuple(explain)})
            result[(employee_id, day)] = resolved
    return result


def _from_roster(day, entries, zone, catalogue: _Catalogue, assignment, explain: list,
                 with_explain: bool) -> Optional[ResolvedShift]:
    """Step 1: the first WORK entry; an OFF entry (always alone on its date) gives no shift."""
    work = [e for e in entries if e.kind == "WORK"]
    if with_explain:
        explain.append({"step": "ROSTER", "roster_entry_id": (work or entries)[0].id, "won": True,
                        "reason": None if work else "ROSTER_OFF"})
    if not work:
        return None
    entry = work[0]
    if entry.shift_id is None:  # custom times: an open block without a shift (D-AS-13)
        data = engine.ShiftVersionData(kind="FIXED", start_time=entry.custom_start_time, end_time=entry.custom_end_time,
                                       segments=entry.custom_segments or ())
        return ResolvedShift(day, "ROSTER", 0, None, data, zone, assignment_id=assignment.id,
                             roster_entry_id=entry.id, entry_seq=entry.entry_seq)
    row = catalogue.version_by_id(entry.shift_version_id) if entry.shift_version_id else None
    if row is not None:
        data, duration, version_id = version_data(row, catalogue.shifts[entry.shift_id].kind), row.duration_hours, row.id
    else:
        found, data, duration = catalogue.on(entry.shift_id, day)
        version_id = found.id if found is not None else None
    if data is None:
        return None
    if entry.custom_start_time:  # a one-date override of the shift's times
        data = replace(data, start_time=entry.custom_start_time, end_time=entry.custom_end_time)
    return ResolvedShift(day, "ROSTER", entry.shift_id, version_id, data, zone, assignment_id=assignment.id,
                         duration_hours=duration, roster_entry_id=entry.id, entry_seq=entry.entry_seq)


def _resolve_day(day, assignment, employment_type, zone, rules, catalogue: _Catalogue, explain: list,
                 with_explain: bool, teams: Optional[Dict[int, str]] = None) -> Optional[ResolvedShift]:
    attributes = _attributes(assignment, employment_type)
    live = [(row, rule) for row, rule in rules if rule.active_on(day) and rule.criteria.matches(attributes)
            and (not hasattr(rule.criteria, "team_match") or rule.criteria.team_match(teams))]
    # Highest priority first; a rule outside the shift's owner scope is skipped and the next one considered.
    live.sort(key=lambda pair: (-pair[1].priority, pair[0].id))
    chosen = None
    for index, (row, rule) in enumerate(live):
        shift = catalogue.shifts.get(row.shift_id)
        reason = None
        if shift is None:
            reason = "SHIFT_MISSING"
        elif not owner_scope_contains(shift, assignment):
            reason = "OWNER_SCOPE_MISMATCH"
        elif chosen is None:
            tied = [r for r, x in live[index + 1:] if x.priority == rule.priority
                    and catalogue.shifts.get(r.shift_id) is not None
                    and owner_scope_contains(catalogue.shifts[r.shift_id], assignment)]
            if tied:
                reason = "AMBIGUOUS"  # refused at save; fail closed to the placement
                chosen = False
            else:
                chosen = row
        if with_explain:
            explain.append({"step": "RULE", "rule_id": row.id, "name": row.name, "priority": row.priority,
                            "shift_id": row.shift_id, "won": chosen is row,
                            "reason": reason or (None if chosen is row else "LOWER_PRIORITY")})
    if chosen:
        row_version, data, duration = catalogue.on(chosen.shift_id, day)
        if data is not None:
            if with_explain and assignment.shift_id:
                explain.append({"step": "PLACEMENT", "shift_id": assignment.shift_id, "won": False,
                                "reason": "RULE_WINS"})
            return ResolvedShift(day, "RULE", chosen.shift_id, row_version.id if row_version else None, data, zone,
                                 rule_id=chosen.id, assignment_id=assignment.id, duration_hours=duration)
    if assignment.shift_id:
        row_version, data, duration = catalogue.on(assignment.shift_id, day)
        if with_explain:
            explain.append({"step": "PLACEMENT", "shift_id": assignment.shift_id, "won": data is not None,
                            "reason": None if data is not None else "NO_VERSION_ON_DATE"})
        if data is not None:
            return ResolvedShift(day, "PLACEMENT", assignment.shift_id, row_version.id if row_version else None, data,
                                 zone, assignment_id=assignment.id, duration_hours=duration)
    return None


def shift_for(db: Session, company_id: int, employee_id: int, day: date) -> Optional[ResolvedShift]:
    return shifts_for_many(db, company_id, [employee_id], day, day)[(employee_id, day)]


def explain(db: Session, company_id: int, employee_id: int, start: date, end: date) -> List[dict]:
    """Per date: the winning step, the version and every candidate with why it lost (§11.4)."""
    resolved = shifts_for_many(db, company_id, [employee_id], start, end, with_explain=True)
    ids = {r.shift_id for r in resolved.values() if r is not None}
    codes = dict(db.query(Shift.id, Shift.code).filter(Shift.company_id == company_id, Shift.id.in_(ids)).all())         if ids else {}
    out = []
    for day in _dates(start, end):
        item = resolved[(employee_id, day)]
        if item is None:
            out.append({"date": day.isoformat(), "source": "NONE", "shift_id": None, "shift_version_id": None,
                        "candidates": []})
            continue
        out.append({"date": day.isoformat(), "source": item.source, "shift_id": item.shift_id,
                    "shift_version_id": item.shift_version_id, "rule_id": item.rule_id,
                    "shift_code": codes.get(item.shift_id), "kind": item.version.kind,
                    "anchor_timezone": item.version.anchor_timezone,
                    "work_timezone": item.work_timezone, "start_time": item.version.start_time,
                    "end_time": item.version.end_time, "candidates": list(item.explain)})
    return out


# ------------------------------------------------------------------------------------------- standard hours
def _legacy_shaped(version: engine.ShiftVersionData) -> bool:
    """Migrated / legacy-edited versions: no segments, break rules or flexible targets (zero-diff path)."""
    return (not version.segments and not version.break_rules and version.expected_net_minutes is None
            and version.core_start_time is None and version.kind in ("FIXED", "FLEXIBLE"))


def scheduled_hours(resolved: Optional[ResolvedShift]) -> Optional[Decimal]:
    """Net scheduled hours of the resolved version before FTE, or None (no shift / no usable duration).

    A legacy-shaped version keeps today's ``duration_hours - unpaid_break_minutes / 60`` (time_inputs
    ``_shift_hours``), so payroll standard hours do not move for unchanged data; every other version uses
    ``shift_engine.scheduled_net_minutes`` (segments, break rules, ON_CALL excluded, expected net minutes).
    """
    if resolved is None:
        return None
    version = resolved.version
    custom_block = resolved.source == "ROSTER" and not resolved.shift_id
    if _legacy_shaped(version) and not custom_block:
        if resolved.duration_hours is None:
            return None
        hours = Decimal(resolved.duration_hours) - Decimal(version.unpaid_break_minutes or 0) / Decimal(60)
        return hours if hours > 0 else None
    try:
        minutes = engine.scheduled_net_minutes(version, resolved.shift_date, resolved.work_timezone)
    except engine.ShiftEngineError:
        return None  # FLEXIBLE / OPEN without expected minutes: the standard-hours fallback applies
    return Decimal(minutes) / Decimal(60) if minutes > 0 else None


def standard_hours(resolved: Optional[ResolvedShift], fte: Decimal, standard_hours_per_day: Optional[Decimal], *,
                   use_shift_duration: bool = True) -> Optional[Decimal]:
    """Payroll standard hours of one date (JC-AS-02; BUG-AS-31 consumer of ``use_shift_duration``).

    None keeps the ledger's own default (no shift and FTE 1, as before AS-M1).
    """
    base = scheduled_hours(resolved) if use_shift_duration else None
    if base is None and (fte != ONE or not use_shift_duration) and standard_hours_per_day is not None:
        base = Decimal(standard_hours_per_day)
    return base * fte if base is not None else None
