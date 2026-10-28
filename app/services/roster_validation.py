"""Builds the inputs of ``roster_rules.validate`` from a candidate roster and runs it (AS-M5; 03-shifts/02 §6.1).

* Instances come from ``shift_engine.build_instance`` in the employee's work zone of the date (DST-correct UTC
  instants), with the entry's stored ``shift_version_id`` (else the version valid on the date) or its custom times.
* Limits (IG-AS-31): the WORKING_TIME pack of the employee's legal-entity country (``PACKS``; DRAFT packs act as WARN,
  "Compliance not verified") combined with ``DEFAULT_SCHEDULING_POLICY`` of ``shift_rosters`` (11 h minimum rest and
  48 h a week, BLOCK) until ``SCHEDULING_POLICY`` ships with AS-M3; a policy only makes a limit stricter, so for each
  rule the stricter value and the higher severity win.
* Context: the employee-local today (never the server date), the publication instant, the 14-day seam span and the
  attendance consent ledger (``roster_consents.consents_for``: a valid NIGHT_WORK consent satisfies
  ``NIGHT_WORK_CONSENT_MISSING``; EC AS-ROS-06).
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pytz
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import clock
from app.models.company import Company
from app.models.employee import Employee, Shift
from app.models.org import EmployeeAssignment, LegalEntity
from app.models.shifts import ShiftVersion
from app.services import roster_rules as rules
from app.services import shift_engine as engine
from app.services import shift_resolution

SEVERITY_RANK = {rules.WARN: 0, rules.REQUIRE_REASON: 1, rules.BLOCK: 2}
# WORKING_TIME pack values used by roster_rules (appendix-a §8, 03-shifts/02 §6.3; all DRAFT = not verified).
PACKS: Dict[str, Dict[str, Any]] = {
    "IN": {"weekly_max_hours": Decimal(48), "max_days_per_week": 6, "max_consecutive_days": 10,
           "weekly_rest_min_hours": Decimal(24), "verified": False,
           "night_work_groups": {"WOMEN": ("19:00", "06:00")}},  # OSH Code s.43 (DRAFT: REQUIRE_REASON)
    "AE": {"weekly_max_hours": Decimal(48), "daily_max_hours": Decimal(10), "verified": False},
    "US": {"verified": False},
}


def _zone_name(value: Optional[str]) -> Optional[str]:
    return value if value and value in pytz.all_timezones_set else None


def zones_for(db: Session, company_id: int, cells: Sequence[Any]) -> Dict[Tuple[int, date], str]:
    """The work zone of each (employee, date): placement's work zone → branch → legal entity → company."""
    if not cells:
        return {}
    employees = sorted({c.employee_id for c in cells})
    start, end = min(c.shift_date for c in cells), max(c.shift_date for c in cells)
    placements = shift_resolution._placements(db, company_id, employees, start, end)
    zones = shift_resolution._Zones(db, company_id, placements)
    out: Dict[Tuple[int, date], str] = {}
    for cell in cells:
        current = shift_resolution._placement_on(placements.get(cell.employee_id, []), cell.shift_date)
        out[(cell.employee_id, cell.shift_date)] = _zone_name(zones.of(current[0]) if current else None) or \
            _zone_name(zones.company_zone) or "UTC"
    return out


class _Versions:
    def __init__(self, db: Session, company_id: int, shift_ids: Iterable[int]):
        ids = sorted({i for i in shift_ids if i})
        self.shifts = {s.id: s for s in db.query(Shift).filter(Shift.company_id == company_id,
                                                               Shift.id.in_(ids or [0]))}
        self.rows: Dict[int, List[ShiftVersion]] = {}
        for row in (db.query(ShiftVersion).filter(ShiftVersion.company_id == company_id,
                                                  ShiftVersion.shift_id.in_(ids or [0]),
                                                  ShiftVersion.voided_at.is_(None))
                    .order_by(ShiftVersion.shift_id, ShiftVersion.effective_from)):
            self.rows.setdefault(row.shift_id, []).append(row)

    def data(self, shift_id: int, version_id: Optional[int], day: date) -> Optional[engine.ShiftVersionData]:
        shift = self.shifts.get(shift_id)
        if shift is None:
            return None
        rows = self.rows.get(shift_id, [])
        row = next((r for r in rows if r.id == version_id), None) if version_id else None
        row = row or next((r for r in rows if r.effective_from <= day and (r.effective_to is None or day <= r.effective_to)),
                          None)
        return shift_resolution.version_data(row, shift.kind) if row is not None else shift_resolution.legacy_data(shift)


def _instance(versions: _Versions, values: Mapping[str, Any], zone: str) -> Optional[engine.ShiftInstance]:
    if values.get("kind") != "WORK":
        return None
    day = values["shift_date"]
    if values.get("shift_id"):
        data = versions.data(values["shift_id"], values.get("shift_version_id"), day)
        if data is None:
            return None
        if values.get("custom_start_time"):
            from dataclasses import replace

            data = replace(data, start_time=values["custom_start_time"], end_time=values["custom_end_time"])
    else:
        data = engine.ShiftVersionData(kind="FIXED", start_time=values.get("custom_start_time"),
                                       end_time=values.get("custom_end_time"),
                                       segments=values.get("custom_segments") or ())
    try:
        return engine.build_instance(data, day, zone)
    except engine.ShiftEngineError:
        return None


def instance_start(db: Session, company_id: int, values: Mapping[str, Any], zone: str) -> Optional[datetime]:
    """Start of the entry's instance; an OFF entry starts at 00:00 local of its date (§5.4 notice hours)."""
    if values.get("kind") == "OFF":
        return pytz.timezone(zone).localize(datetime.combine(values["shift_date"], time(0, 0))).astimezone(pytz.utc)
    instance = _instance(_Versions(db, company_id, [values.get("shift_id")]), values, zone)
    return instance.start_at if instance is not None else None


def _planned(versions: _Versions, cell: Any, zone: str) -> rules.PlannedInstance:
    values = cell.values(1)
    withdrawn = cell.kind is None
    if withdrawn:  # the withdrawal of a published entry: describe the old instance
        values = {k: getattr(cell.entry, k) for k in ("employee_id", "shift_date", "entry_seq", "kind", "shift_id",
                                                      "shift_version_id", "custom_start_time", "custom_end_time",
                                                      "custom_segments")}
    instance = _instance(versions, values, zone)
    previous = None
    if cell.changed and cell.entry is not None and not withdrawn:
        previous = _instance(versions, {k: getattr(cell.entry, k) for k in (
            "kind", "shift_date", "shift_id", "shift_version_id", "custom_start_time", "custom_end_time",
            "custom_segments")}, zone)
    shift = versions.shifts.get(values.get("shift_id")) if values.get("shift_id") else None
    return rules.PlannedInstance(
        shift_date=cell.shift_date, kind=values.get("kind") or rules.WORK,
        entry_seq=cell.entry_seq, start_at=instance.start_at if instance else None,
        end_at=instance.end_at if instance else None,
        net_minutes=instance.scheduled_net_minutes if instance else None, shift_id=values.get("shift_id"),
        shift_version_id=values.get("shift_version_id"), archived=bool(shift is not None and shift.status != "ACTIVE"),
        has_on_call=bool(instance and any(s.kind == "ON_CALL" for s in instance.segments)),
        changed=bool(cell.changed), withdrawn=withdrawn,
        previous_kind=cell.entry.kind if cell.entry is not None and cell.changed else None,
        previous_start_at=previous.start_at if previous is not None else None,
        previous_net_minutes=previous.scheduled_net_minutes if previous is not None else None,
        entry_id=cell.entry.id if cell.entry is not None else None)


def _stricter(pack_value, pack_verified: bool, policy_value, policy_severity: str, *, lower_is_stricter: bool,
              source: str) -> Optional[rules.Limit]:
    """The stricter of the pack field (WARN unless verified) and the tenant policy (its severity)."""
    candidates = []
    if pack_value is not None:
        candidates.append((Decimal(pack_value), rules.BLOCK if pack_verified else rules.WARN, "WORKING_TIME pack",
                           pack_verified))
    if policy_value is not None:
        candidates.append((Decimal(policy_value), policy_severity, source, True))
    if not candidates:
        return None
    value = min(c[0] for c in candidates) if lower_is_stricter else max(c[0] for c in candidates)
    severity = max((c[1] for c in candidates), key=lambda s: SEVERITY_RANK[s])
    verified = any(c[3] for c in candidates)
    ref = next(c[2] for c in candidates if c[0] == value)
    return rules.Limit(value=value, severity=severity, source_ref=ref, verified=verified)


def limits_for(db: Session, company_id: int, employee_ids: Sequence[int], on: date) -> rules.LimitsData:
    from app.services.shift_rosters import DEFAULT_SCHEDULING_POLICY as policy

    countries: Dict[int, Optional[str]] = {}
    for employee_id, country in (db.query(EmployeeAssignment.employee_id, LegalEntity.country_code)
                                 .join(LegalEntity, (LegalEntity.company_id == EmployeeAssignment.company_id)
                                       & (LegalEntity.id == EmployeeAssignment.legal_entity_id))
                                 .filter(EmployeeAssignment.company_id == company_id,
                                         EmployeeAssignment.employee_id.in_(list(employee_ids) or [0]),
                                         EmployeeAssignment.is_primary.is_(True),
                                         EmployeeAssignment.effective_from <= on,
                                         or_(EmployeeAssignment.effective_to.is_(None),
                                             EmployeeAssignment.effective_to >= on))):
        countries[employee_id] = country
    per: Dict[int, rules.EmployeeLimits] = {}
    for employee_id in employee_ids:
        pack = PACKS.get(countries.get(employee_id) or "", {})
        verified = bool(pack.get("verified"))
        days = pack.get("max_days_per_week")
        consecutive = pack.get("max_consecutive_days")
        per[employee_id] = rules.EmployeeLimits(
            min_rest_hours=_stricter(pack.get("min_rest_hours"), verified, policy["min_rest_hours"],
                                     policy["min_rest_severity"], lower_is_stricter=False,
                                     source="SCHEDULING_POLICY default"),
            weekly_max_hours=_stricter(pack.get("weekly_max_hours"), verified, policy["max_hours_per_week"],
                                       policy["weekly_hours_severity"], lower_is_stricter=True,
                                       source="SCHEDULING_POLICY default"),
            daily_max_hours=_stricter(pack.get("daily_max_hours"), verified, None, rules.WARN,
                                      lower_is_stricter=True, source=""),
            max_days_per_week=rules.Limit(days, rules.WARN, "WORKING_TIME pack", verified) if days else None,
            max_consecutive_days=rules.Limit(consecutive, rules.WARN, "WORKING_TIME pack", verified)
            if consecutive else None,
            weekly_rest_min_hours=rules.Limit(pack["weekly_rest_min_hours"], rules.WARN, "WORKING_TIME pack", False)
            if pack.get("weekly_rest_min_hours") else None,
            night_work_groups={group: rules.NightWorkRule(window, True, rules.REQUIRE_REASON,
                                                          f"WORKING_TIME pack night_work.protected_groups[{group}]",
                                                          verified)
                               for group, window in (pack.get("night_work_groups") or {}).items()},
        )
    return rules.LimitsData(per_employee=per)


def _local_today(zone: str) -> date:
    return clock.utcnow().astimezone(pytz.timezone(zone)).date()


def run(db: Session, company_id: int, candidate: Sequence[Any], changed: Sequence[Any], *, publish_at: datetime,
        acknowledged: Sequence[str], reasons: Mapping[str, Any], exemptions: Mapping[str, Any]):
    from app.services.shift_rosters import ValidationOutcome

    cells = list(candidate) + [c for c in changed if c.kind is None]
    zones = zones_for(db, company_id, cells)
    versions = _Versions(db, company_id, {c.shift_id for c in cells if c.shift_id} | {
        c.entry.shift_id for c in cells if c.entry is not None and c.entry.shift_id})
    employees = sorted({c.employee_id for c in changed})
    people = {e.id: e for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                         Employee.id.in_(employees or [0]))}
    company = db.get(Company, company_id)
    schedules = []
    today: Dict[int, date] = {}
    for employee_id in employees:
        mine = [c for c in cells if c.employee_id == employee_id]
        zone = next((zones[(c.employee_id, c.shift_date)] for c in mine), _zone_name(company.timezone) or "UTC")
        today[employee_id] = _local_today(zone)
        person = people.get(employee_id)
        schedules.append(rules.EmployeeSchedule(
            employee_id=employee_id, work_timezone=zone,
            instances=tuple(sorted((_planned(versions, c, zones[(c.employee_id, c.shift_date)]) for c in mine),
                                   key=lambda p: (p.shift_date, p.entry_seq))),
            employment_start=person.hire_date if person is not None else None,
            employment_end=person.termination_date if person is not None else None))
    first_day = min((c.shift_date for c in changed), default=publish_at.date())
    limits = limits_for(db, company_id, employees, first_day)
    schedules = [rules.EmployeeSchedule(
        employee_id=s.employee_id, work_timezone=s.work_timezone, instances=s.instances,
        employment_start=s.employment_start, employment_end=s.employment_end,
        protected_groups=rules.protected_groups_for(
            getattr(people.get(s.employee_id), "gender", None),
            limits.for_employee(s.employee_id).night_work_groups.keys()))
        for s in schedules]
    from app.services import roster_consents

    context = rules.RuleContext(today=today, publish_at=publish_at,
                                period_states=period_states(db, company_id, changed),
                                affected_leave=affected_leave(db, company_id, changed),
                                consents=tuple(roster_consents.consents_for(db, company_id, employees)))
    results = rules.validate(rules.ScheduleData(employees=tuple(schedules)), limits, context)
    results += placement_results(db, company_id, changed, versions)
    blocking, unacknowledged, missing = rules.summarize(results, acknowledged, reasons, exemptions)
    return ValidationOutcome(results=results, blocking=blocking, missing_reasons=missing,
                             unacknowledged=unacknowledged)


def period_states(db: Session, company_id: int, changed: Sequence[Any]) -> Dict[date, str]:
    """Payroll state of each changed date (EC AS-ROS-01): the strictest state among the changed employees' pay
    periods covering it — CLOSED = FINALIZED, PROCESSING = INPUT_LOCKED (interim reading until payroll ships
    ``calendars.period_state_on``, IG-AS-29)."""
    from app.models.pay_runs import PayPeriod

    if not changed:
        return {}
    employees = sorted({c.employee_id for c in changed})
    days = sorted({c.shift_date for c in changed})
    groups = {g for (g,) in db.query(EmployeeAssignment.pay_group_id).filter(
        EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id.in_(employees),
        EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.pay_group_id.isnot(None))}
    if not groups:
        return {}
    periods = db.query(PayPeriod).filter(PayPeriod.company_id == company_id, PayPeriod.pay_group_id.in_(groups),
                                         PayPeriod.status.in_(("PROCESSING", "CLOSED")),
                                         PayPeriod.attendance_start <= days[-1],
                                         PayPeriod.attendance_end >= days[0]).all()
    out: Dict[date, str] = {}
    for day in days:
        for period in periods:
            if period.attendance_start <= day <= period.attendance_end:
                state = rules.FINALIZED if period.status == "CLOSED" else rules.INPUT_LOCKED
                if out.get(day) != rules.FINALIZED:
                    out[day] = state
    return out


def placement_results(db: Session, company_id: int, changed: Sequence[Any], versions: "_Versions") -> List[Any]:
    """``ENTRY_OUT_OF_PLACEMENT`` (03-shifts/02 §3.4, §16; EC AS-ROS-05): a created / changed WORK entry whose
    shift is owned by a branch or legal entity that does not contain the employee's placement on that date (a
    transfer) is an integrity BLOCK; the planner withdraws it or plans the new owner's shift."""
    work = [c for c in changed if c.kind == "WORK" and c.shift_id]
    if not work:
        return []
    start, end = min(c.shift_date for c in work), max(c.shift_date for c in work)
    placements = shift_resolution._placements(db, company_id, sorted({c.employee_id for c in work}), start, end)
    found: Dict[int, List[date]] = {}
    for cell in work:
        shift = versions.shifts.get(cell.shift_id)
        current = shift_resolution._placement_on(placements.get(cell.employee_id, []), cell.shift_date)
        if shift is None or current is None:
            continue
        if not shift_resolution.owner_scope_contains(shift, current[0]):
            found.setdefault(cell.employee_id, []).append(cell.shift_date)
    return [rules.RuleResult("ENTRY_OUT_OF_PLACEMENT", rules.BLOCK, employee_id, tuple(sorted(days)), None, None,
                             None, "placement", "roster.entry_out_of_placement")
            for employee_id, days in sorted(found.items())]


def affected_leave(db: Session, company_id: int, changed: Sequence[Any]) -> List[Any]:
    """Approved leave whose charged units the publication changes (03-shifts/02 §5.6, D-AS-17; EC AS-ROS-09): a
    rostered OFF on a charged leave day releases it, a WORK entry on an uncharged one charges it. Only the leave id
    and units travel (never the leave type: masked for planners, EC AS-ROS-08)."""
    from app.models.time_inputs import LeaveDay

    cells = {(c.employee_id, c.shift_date): c for c in changed if c.kind}  # a withdrawal falls back to rule / placement
    if not cells:
        return []
    rows = db.query(LeaveDay).filter(LeaveDay.company_id == company_id, LeaveDay.voided_at.is_(None),
                                     LeaveDay.employee_id.in_(sorted({e for e, _ in cells})),
                                     LeaveDay.date.in_(sorted({d for _, d in cells}))).all()
    by_leave: Dict[Tuple[int, int], List[Any]] = {}
    for row in rows:
        if (row.employee_id, row.date) in cells:
            by_leave.setdefault((row.employee_id, row.leave_id), []).append(row)
    impacts = []
    for (employee_id, leave_id), days in sorted(by_leave.items()):
        old = sum((Decimal(d.units) for d in days if d.counts_against_balance), Decimal("0.00"))
        new = sum((Decimal(d.units) for d in days if cells[(employee_id, d.date)].kind == "WORK"), Decimal("0.00"))
        if old != new:
            impacts.append(rules.LeaveImpact(employee_id, leave_id, tuple(sorted(d.date for d in days)), old, new))
    return impacts
