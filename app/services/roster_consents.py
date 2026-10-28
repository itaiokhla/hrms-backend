"""Consents in roster validation (EC AS-ROS-06; 03-shifts/02 §6.5, §16; GV-AS-ROS-14).

* ``consents_for`` turns the attendance consent ledger into ``roster_rules.Consent`` intervals, so publish
  validation (``roster_validation.run``) reads real consents: a valid ``NIGHT_WORK`` consent satisfies
  ``NIGHT_WORK_CONSENT_MISSING``, ``REST_WAIVER`` / ``COMPRESSED_WEEK`` lift their rules as the engine defines.
* Job ``roster.revalidate_employee`` (event-driven: subscribed to ``attendance.consent.changed``) validates the
  employee's **published** future entries (employee-local today .. + ``shift.roster.horizon_days``, 28) against the
  changed consent only. Each breach opens one ``roster_consent_conflicts`` row (the pending item
  ``roster_consent_conflict`` and the grid badge) and notifies the planners holding ``roster:plan`` over the employee
  (``roster.consent_conflict``: dates and counts only, never reasons); entries that are valid again resolve their open
  conflicts. **No entry is ever withdrawn automatically.**
"""

from __future__ import annotations

from datetime import timedelta
from typing import Dict, Iterable, List, Sequence

from sqlalchemy.orm import Session

from app.core import clock
from app.models.employee import Employee
from app.models.rosters import RosterConsentConflict, RosterEntry
from app.payroll.services import jobs, outbox
from app.services import roster_rules as rules

JOB = "roster.revalidate_employee"
CHANGED_TOPIC = "attendance.consent.changed"
HORIZON_DAYS = 28  # shift.roster.horizon_days default (03-shifts/02 §2.4)
LEDGER_PURPOSES = ("NIGHT_WORK", "COMPRESSED_WEEK", "REST_WAIVER", "SCHEDULE_CHANGE_ACCEPTED")
# The consent a revalidation checks -> the roster rule keys that consent can lift.
RULES_OF = {
    "NIGHT_WORK": (rules.NIGHT_WORK_CONSENT_MISSING,),
    "COMPRESSED_WEEK": (rules.DAILY_HOURS_EXCEEDED,),
    "REST_WAIVER": (rules.MIN_REST_VIOLATION,),
    "OVERTIME": (),  # read by overtime proposals (AS-M8), no roster rule yet
}


def consents_for(db: Session, company_id: int, employee_ids: Iterable[int]) -> List[rules.Consent]:
    from datetime import date

    from app.services import attendance_consents

    out = []
    for row in attendance_consents.grant_intervals(db, company_id, employee_ids, LEDGER_PURPOSES):
        subject = row["subject_ref"] or {}
        subject_date = date.fromisoformat(subject["shift_date"]) if subject.get("shift_date") else None
        if row["scope"] == rules.PER_INSTANCE and subject_date is None:
            continue  # roster-entry subjects are matched by AS-M6 requests
        out.append(rules.Consent(employee_id=row["employee_id"], kind=row["purpose"], valid_from=row["valid_from"],
                                 valid_to=row["valid_to"], scope=row["scope"], subject_date=subject_date,
                                 consent_id=row["consent_id"]))
    return out


def _schedule(db: Session, company_id: int, employee: Employee, entries: Sequence[RosterEntry]):
    from app.services import roster_validation

    cells = [type("Cell", (), {"employee_id": e.employee_id, "shift_date": e.shift_date})() for e in entries]
    zones = roster_validation.zones_for(db, company_id, cells)
    versions = roster_validation._Versions(db, company_id, {e.shift_id for e in entries if e.shift_id})
    instances = []
    for entry in entries:
        zone = zones[(entry.employee_id, entry.shift_date)]
        values = {k: getattr(entry, k) for k in ("kind", "shift_date", "shift_id", "shift_version_id",
                                                 "custom_start_time", "custom_end_time", "custom_segments")}
        instance = roster_validation._instance(versions, values, zone)
        instances.append(rules.PlannedInstance(
            shift_date=entry.shift_date, kind=entry.kind, entry_seq=entry.entry_seq,
            start_at=instance.start_at if instance else None, end_at=instance.end_at if instance else None,
            net_minutes=instance.scheduled_net_minutes if instance else None, shift_id=entry.shift_id,
            shift_version_id=entry.shift_version_id, entry_id=entry.id))
    zone = next(iter(zones.values()), "UTC")
    return zone, tuple(instances)


def _planner_user_ids(db: Session, company_id: int, employee_id: int) -> List[int]:
    from app.core import authz
    from app.core.principal import build_principal
    from app.models.company import Company
    from app.models.user import User
    from app.payroll.services.approvals import _permission_holder_user_ids

    company = db.get(Company, company_id)
    found = []
    for user_id in _permission_holder_user_ids(db, "roster:plan"):
        user = db.query(User).filter(User.company_id == company_id, User.id == user_id).one_or_none()
        if user is None or user.status != "active":
            continue
        principal = build_principal(db, user, None, company)
        if principal.employee_id == employee_id:
            continue
        if authz.can_access_employee(db, principal, "roster:plan", employee_id):
            found.append(user_id)
    return sorted(set(found))


def revalidate_employee(db: Session, company_id: int, employee_id: int, purpose: str) -> dict:
    """Validate the published future entries of the employee against ``purpose`` (see the module doc)."""
    from app.services import org_structure_service, roster_validation

    keys = RULES_OF.get(purpose)
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
    if not keys or employee is None:
        return {"checked": 0, "conflicts": 0, "resolved": 0}
    today = org_structure_service.employee_local_today(db, employee)
    horizon = today + timedelta(days=HORIZON_DAYS)
    entries = (db.query(RosterEntry)
               .filter(RosterEntry.company_id == company_id, RosterEntry.employee_id == employee_id,
                       RosterEntry.withdrawn_at.is_(None), RosterEntry.shift_date >= today,
                       RosterEntry.shift_date <= horizon)
               .order_by(RosterEntry.shift_date, RosterEntry.entry_seq).all())
    breaches: Dict[int, RosterEntry] = {}
    if entries:
        zone, instances = _schedule(db, company_id, employee, entries)
        limits = roster_validation.limits_for(db, company_id, [employee_id], today)
        mine = limits.for_employee(employee_id)
        schedule = rules.EmployeeSchedule(
            employee_id=employee_id, work_timezone=zone, instances=instances,
            employment_start=employee.hire_date, employment_end=employee.termination_date,
            protected_groups=rules.protected_groups_for(getattr(employee, "gender", None),
                                                        mine.night_work_groups.keys()))
        context = rules.RuleContext(today=today, consents=tuple(consents_for(db, company_id, [employee_id])),
                                    check_span_days=None)
        by_date: Dict[object, List[RosterEntry]] = {}
        for entry in entries:
            by_date.setdefault(entry.shift_date, []).append(entry)
        for result in rules.validate(rules.ScheduleData(employees=(schedule,)), limits, context):
            if result.rule_key not in keys:
                continue
            for day in result.dates:
                for entry in by_date.get(day, []):
                    if entry.kind == "WORK":
                        breaches[entry.id] = entry
    open_rows = {r.roster_entry_id: r for r in db.query(RosterConsentConflict).filter(
        RosterConsentConflict.company_id == company_id, RosterConsentConflict.employee_id == employee_id,
        RosterConsentConflict.purpose == purpose, RosterConsentConflict.status == "OPEN")}
    now = clock.utcnow()
    resolved = 0
    for entry_id, row in open_rows.items():
        if entry_id not in breaches:
            row.status, row.resolved_at = "RESOLVED", now
            resolved += 1
    created: List[RosterEntry] = []
    from app.services import attendance_consents

    latest = attendance_consents.latest(db, company_id, employee_id, purpose)
    for entry_id, entry in sorted(breaches.items(), key=lambda kv: (kv[1].shift_date, kv[1].entry_seq)):
        if entry_id in open_rows:
            continue
        db.add(RosterConsentConflict(company_id=company_id, employee_id=employee_id, roster_entry_id=entry_id,
                                     roster_period_id=entry.roster_period_id, shift_date=entry.shift_date,
                                     purpose=purpose, rule_key=keys[0], consent_id=getattr(latest, "id", None)))
        created.append(entry)
    db.flush()
    if created:
        from app.payroll.services.holders import notify_users

        dates = ", ".join(sorted({e.shift_date.strftime("%d %b") for e in created}))
        notify_users(db, _planner_user_ids(db, company_id, employee_id), "Schedule needs review",
                     f"{employee.full_name}: {len(created)} published shift(s) no longer have the consent they need "
                     f"({dates}). Review the schedule; nothing was changed.",
                     f"/attendance/schedules/{created[0].roster_period_id}" if created[0].roster_period_id else None,
                     "warning")
    return {"checked": len(entries), "conflicts": len(created), "resolved": resolved}


def open_conflicts(db: Session, company_id: int, entry_ids: Iterable[int]) -> Dict[int, List[str]]:
    """{roster_entry_id: [purpose, ...]} of OPEN conflicts (the grid badge)."""
    ids = sorted(set(entry_ids))
    found: Dict[int, List[str]] = {}
    if not ids:
        return found
    for entry_id, purpose in db.query(RosterConsentConflict.roster_entry_id, RosterConsentConflict.purpose).filter(
            RosterConsentConflict.company_id == company_id, RosterConsentConflict.status == "OPEN",
            RosterConsentConflict.roster_entry_id.in_(ids)):
        found.setdefault(entry_id, []).append(purpose)
    return found


def serialize_conflict(row: RosterConsentConflict) -> dict:
    return {"id": row.id, "kind": "roster_consent_conflict", "employee_id": row.employee_id,
            "roster_entry_id": row.roster_entry_id, "roster_period_id": row.roster_period_id,
            "shift_date": row.shift_date.isoformat(), "purpose": row.purpose, "rule_key": row.rule_key,
            "status": row.status, "created_at": row.created_at.isoformat() if row.created_at else None}


@jobs.register(JOB)
def _revalidate(ctx) -> dict:
    payload = ctx.payload or {}
    purpose = payload.get("purpose")
    if purpose not in RULES_OF or payload.get("employee_id") is None:
        return {"skipped": purpose}
    return revalidate_employee(ctx.db, ctx.company_id, int(payload["employee_id"]), purpose)


outbox.subscribe(CHANGED_TOPIC, JOB)
