"""Overtime proposals from attendance (AS-M8, G-18; 04-attendance/05 §3-§10; spine §5.9, JC-AS-09).

The evaluator already stores per day ``ot_eligible_seconds`` and the daily ``ot_candidate_minutes`` (steps 15-16).
This module turns FINAL days into payroll's ``overtime_entries`` (there is no attendance overtime table, D-AS-29):

* ``propose_week`` — one employee, one payroll week (start = ``payroll.calendar.week_start_day`` of the employee on
  the week's first date, BUG-AS-24). A date takes part only when §4 holds on it (FINAL, STANDARD rules, WORKDAY,
  ``payroll.overtime.enabled`` ON, ``OVERTIME_POLICY`` not exempt with ``source`` ATTENDANCE_WITH_APPROVAL /
  ATTENDANCE_AUTO). The daily part is proposed as soon as the date is FINAL; the weekly part only when every
  employed date of the week is FINAL (SP-AS-071, AS-OT-13), through payroll's pure ``threshold_overtime`` with the
  day facts' scheduled minutes as SHIFT_DURATION (net × FTE, never the gross shift duration).
* Action table §5.2 against the live entry of the date: none → create (AUTO: approved at once, audited
  ``OVERTIME_AUTO_APPROVED``); PENDING of attendance origin → edited in place (approval restarted) or cancelled when
  the minutes fell to 0; APPROVED of attendance origin → never changed: ATTENDANCE_WITH_APPROVAL gets a PENDING
  successor version when the difference exceeds ``min_minutes`` (SP-AS-070), ATTENDANCE_AUTO is flagged
  (``OVERTIME_UNAPPROVED``, owner default D-26); an entry filed by a person (REQUEST) is never touched.
* Consent (§8): where the pack requires it and no GRANTED ``OVERTIME`` consent (STANDING, or PER_INSTANCE with
  ``subject_ref.overtime_entry_id``) is valid on the date, the proposal is still written and reported
  ``CONSENT_MISSING``; with a VERIFIED pack that enforces it, approving is refused (``check_approval``).
* Exclusivity (§10, E2): a worked hour is paid or credited as comp-off, never both — two live entries of one date
  can never disagree on ``comp_off`` (409 ``COMP_OFF_SOURCE_TAKEN``), and comp-off is chosen on the entry itself.
* ``context`` — the approval context: the attendance day (eligible vs candidate, sessions), cap usage per window
  (calendar quarter and rolling 3 months side by side, AS-OT-08), consent status and the comp-off option.

Job ``attendance.ot_proposals`` (daily-local) re-proposes the weeks touching the last ``LOOKBACK_DAYS`` days;
every write is idempotent, so a rerun changes nothing. Services never commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.attendance import AttendanceConsent, AttendanceDay
from app.models.employee import Employee
from app.models.work_items import ACTIVE_OVERTIME_STATUSES, MAX_OVERTIME_MINUTES, OvertimeEntry
from app.payroll import time_pay_overtime as tpo
from app.payroll.services import approvals
from app.payroll.services import overtime as ot
from app.payroll.services import time_inputs
from app.payroll.time_pay_common import TimePayError

LOOKBACK_DAYS = 14
WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
PROPOSAL_REASON = "Proposed from attendance (day version v{version}{weekly})"
CAP_WINDOWS = ("per_day", "per_week", "per_month", "per_quarter")


# ------------------------------------------------------------------------------------------------ inputs
def week_start_day(db: Session, company_id: int, employee_id: int, on: date) -> int:
    """``payroll.calendar.week_start_day`` of the employee on ``on`` (0 = Monday)."""
    from app.payroll.services import settings as settings_service

    try:
        value = settings_service.resolve(db, company_id, "payroll.calendar.week_start_day", employee_id=employee_id,
                                         on=on).value
    except Exception:  # noqa: BLE001 - an invalid payroll setting is reported by payroll; weeks fall back to Monday
        value = "MON"
    return WEEKDAYS.get(str(value or "MON").upper()[:3], 0)


def overtime_enabled(db: Session, company_id: int, employee_id: int, on: date) -> bool:
    from app.payroll.services import settings as settings_service

    try:
        return settings_service.resolve(db, company_id, "payroll.overtime.enabled", employee_id=employee_id,
                                        on=on).value is True
    except Exception:  # noqa: BLE001
        return False


@dataclass
class DateInput:
    day: date
    employed: bool
    day_type: str  # WORKDAY | WEEKLY_OFF | HOLIDAY
    scheduled_minutes: int
    legal_entity_id: Optional[int]
    row: Optional[AttendanceDay]
    view: Optional[ot.PolicyView] = None
    switch_on: bool = False

    @property
    def final(self) -> bool:
        return self.row is not None and self.row.evaluation_state == "FINAL"

    @property
    def qualifies(self) -> bool:
        """§4 conditions 1-5 on this date."""
        rules = self.view.rules if self.view is not None else None
        return (self.employed and self.final and self.row.rules_mode == "STANDARD" and self.switch_on
                and self.day_type == tpo.WORKDAY and rules is not None and not rules.exempt
                and rules.source in ot.ATTENDANCE_SOURCES)

    def worked(self) -> tpo.WorkedDay:
        seconds = min(int(self.row.ot_eligible_seconds or 0), tpo.MAX_SECONDS_PER_DAY) if self.final else 0
        hours = Decimal(self.scheduled_minutes) / 60 if self.scheduled_minutes else None
        return tpo.WorkedDay(date=self.day, worked_seconds=seconds, day_type=self.day_type, shift_hours=hours)


def _inputs(db: Session, company_id: int, employee: Employee, first: date, last: date) -> List[DateInput]:
    facts = {f.date: f for f in time_inputs.day_facts(db, company_id, employee.id, first, last)}
    rows = {r.shift_date: r for r in db.query(AttendanceDay).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.employee_id == employee.id,
        AttendanceDay.shift_date >= first, AttendanceDay.shift_date <= last)}
    out = []
    day = first
    while day <= last:
        f = facts.get(day)
        employed = bool(f is not None and f.employed)
        kind = tpo.WORKDAY
        if f is not None and f.holiday is not None and getattr(f.holiday, "paid", True):
            kind = tpo.HOLIDAY
        elif f is not None and f.weekly_off:
            kind = tpo.WEEKLY_OFF
        item = DateInput(day, employed, kind, int(getattr(f, "scheduled_minutes", 0) or 0) if f else 0,
                         getattr(f, "legal_entity_id", None) if f else None, rows.get(day))
        if employed:
            item.view = ot.resolve_policy(db, company_id, employee.id, day)
            item.switch_on = overtime_enabled(db, company_id, employee.id, day)
        out.append(item)
        day += timedelta(days=1)
    return out


def _daily_minutes(item: DateInput) -> int:
    """The daily part of a qualifying FINAL date (payroll's pure function, SP-AS-072 shortcut)."""
    rules = item.view.rules
    eligible = int(item.row.ot_eligible_seconds or 0)
    if rules.threshold_combination == "WEEKLY":
        return 0
    if rules.daily_threshold is None and not item.scheduled_minutes:
        return min(eligible // 60, MAX_OVERTIME_MINUTES)
    try:
        result = tpo.threshold_overtime([item.worked()], item.view.fields, week_start=item.day.weekday())
    except TimePayError:
        return 0
    return min(int(sum(d.daily_ot_seconds for d in result.days) // 60), MAX_OVERTIME_MINUTES)


def _week_minutes(items: Sequence[DateInput], week_start: int) -> Dict[date, int]:
    """Daily + weekly parts per WORKDAY date of a complete week, with the last employed date's policy (AS-OT-10)."""
    employed = [i for i in items if i.employed]
    anchor = employed[-1]
    if anchor.view is None or anchor.view.rules is None:
        return {}
    days = [i.worked() for i in employed if i.final]
    try:
        result = tpo.threshold_overtime(days, anchor.view.fields, week_start=week_start)
    except TimePayError:
        return {}
    return {d.date: min(d.ot_minutes, MAX_OVERTIME_MINUTES) for d in result.days if d.day_type == tpo.WORKDAY}


# ------------------------------------------------------------------------------------------------ consent
def consent_status(db: Session, company_id: int, employee_id: int, legal_entity_id: Optional[int], on: date,
                   entry_id: Optional[int] = None) -> dict:
    """§8: whether the pack requires OVERTIME consent on ``on`` and whether one is on file."""
    from app.services import attendance_consents, pack_resolver

    required = pack_resolver.resolve(db, company_id, legal_entity_id, "overtime.requires_consent", on)
    enforce = pack_resolver.resolve(db, company_id, legal_entity_id, "overtime.consent_enforce", on)
    if not required.value:
        return {"required": False, "on_file": None, "enforced": False}
    on_file = attendance_consents.has_consent(db, company_id, employee_id, "OVERTIME", on)
    if not on_file and entry_id is not None:
        on_file = db.query(AttendanceConsent.id).filter(
            AttendanceConsent.company_id == company_id, AttendanceConsent.employee_id == employee_id,
            AttendanceConsent.purpose == "OVERTIME", AttendanceConsent.scope == "PER_INSTANCE",
            AttendanceConsent.status == "GRANTED", AttendanceConsent.valid_from <= on,
            func.coalesce(AttendanceConsent.subject_ref["overtime_entry_id"].astext, "") == str(entry_id),
        ).first() is not None
    return {"required": True, "on_file": on_file, "enforced": bool(enforce.value and required.verified),
            "pack_version": required.pack_version}


def check_approval(db: Session, entry: OvertimeEntry) -> None:
    """Called by payroll's decide path before an approval: 422 ``OVERTIME_CONSENT_REQUIRED`` under an enforcing
    VERIFIED pack without consent (AS-OT-09)."""
    state = consent_status(db, entry.company_id, entry.employee_id, _legal_entity(db, entry), entry.date, entry.id)
    if state["required"] and state["enforced"] and not state["on_file"]:
        raise AppError(422, "Record the employee's overtime consent before approving.", "OVERTIME_CONSENT_REQUIRED",
                       extra={"details": {"entry_id": entry.id}})


def _legal_entity(db: Session, entry: OvertimeEntry) -> Optional[int]:
    row = db.query(AttendanceDay.legal_entity_id).filter(
        AttendanceDay.company_id == entry.company_id, AttendanceDay.employee_id == entry.employee_id,
        AttendanceDay.shift_date == entry.date).first()
    return row[0] if row else None


# ------------------------------------------------------------------------------------------------ exclusivity
def ensure_exclusive(db: Session, company_id: int, employee_id: int, day: date, comp_off: bool,
                     exclude_ids: Iterable[int] = ()) -> None:
    """§10 E2: a date's live entries are either all paid or all comp-off (one worked hour, one outcome)."""
    excluded = [i for i in exclude_ids if i]
    query = db.query(OvertimeEntry.id).filter(
        OvertimeEntry.company_id == company_id, OvertimeEntry.employee_id == employee_id, OvertimeEntry.date == day,
        OvertimeEntry.status.in_(ACTIVE_OVERTIME_STATUSES), OvertimeEntry.comp_off.is_(not comp_off))
    if excluded:
        query = query.filter(OvertimeEntry.id.notin_(excluded))
    if query.first() is not None:
        raise conflict("These hours are already claimed " + ("as overtime pay" if comp_off else "as comp-off")
                       + " on this date; one worked hour is paid or taken as time off, never both.",
                       "COMP_OFF_SOURCE_TAKEN")


# ------------------------------------------------------------------------------------------------ the week
@dataclass
class WeekResult:
    employee_id: int
    week_start: date
    complete: bool
    created: List[int] = field(default_factory=list)
    updated: List[int] = field(default_factory=list)
    cancelled: List[int] = field(default_factory=list)
    successors: List[int] = field(default_factory=list)
    flagged: List[str] = field(default_factory=list)
    consent_missing: List[str] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"employee_id": self.employee_id, "week_start": self.week_start.isoformat(),
                "complete": self.complete, "created": self.created, "updated": self.updated,
                "cancelled": self.cancelled, "successors": self.successors, "flagged": self.flagged,
                "consent_missing": self.consent_missing, "skipped": self.skipped}


def propose_week(db: Session, company_id: int, employee: Employee, week_start: date, *,
                 today: Optional[date] = None) -> WeekResult:
    from app.services import attendance_service

    attendance_service.lock_employee(db, employee.id)
    today = today or clock.company_today(db, company_id)
    items = _inputs(db, company_id, employee, week_start, week_start + timedelta(days=6))
    employed = [i for i in items if i.employed]
    complete = bool(employed) and all(i.final for i in employed)
    result = WeekResult(employee.id, week_start, complete)
    if not employed:
        return result
    weekly = _week_minutes(items, week_start.weekday()) if complete else {}
    for item in items:
        if not item.qualifies:
            if item.employed and item.final and item.day_type == tpo.WORKDAY:
                reason = ("SOURCE_NOT_ATTENDANCE" if item.view and item.view.rules else
                          (item.view.status if item.view else "NO_POLICY"))
                if not item.switch_on:
                    reason = "OVERTIME_OFF"
                elif item.view and item.view.rules and item.view.rules.exempt:
                    reason = "EXEMPT"
                result.skipped.append({"date": item.day.isoformat(), "reason": reason})
            continue
        minutes = weekly.get(item.day, _daily_minutes(item)) if complete else _daily_minutes(item)
        _apply(db, company_id, employee, item, minutes, complete and item.day in weekly, result)
    return result


def _origin_is_attendance(entry: OvertimeEntry) -> bool:
    return entry.source in ot.ATTENDANCE_SOURCES and entry.requested_by_user_id is None


def _span(item: DateInput, minutes: int):
    row = item.row
    if row.expected_end_at is None or row.last_out_at is None or minutes <= 0:
        return None, None
    if row.last_out_at - row.expected_end_at >= timedelta(minutes=minutes):
        return row.expected_end_at, row.expected_end_at + timedelta(minutes=minutes)
    return None, None


def _apply(db: Session, company_id: int, employee: Employee, item: DateInput, minutes: int, weekly: bool,
           result: WeekResult) -> None:
    rules = item.view.rules
    live = ot._live_entries(db, company_id, employee.id, item.day, item.day, ())
    reason = PROPOSAL_REASON.format(version=item.row.version, weekly=", with the weekly part" if weekly else "")
    if any(not _origin_is_attendance(e) for e in live):
        result.skipped.append({"date": item.day.isoformat(), "reason": "REQUESTED_BY_PERSON"})
        return
    entry = live[0] if live else None
    if entry is None:
        if minutes <= 0:
            return
        created = _create(db, employee, item, minutes, rules.source, reason, item.view)
        result.created.append(created.id)
        _consent(db, company_id, employee, item, created, result)
        return
    if entry.status == ot.PENDING:
        if minutes <= 0:
            approvals.withdraw(db, None, module=ot.MODULE, subject_type=ot.SUBJECT_TYPE, subject_id=entry.id,
                               reason="No overtime after the attendance change")
            entry.status, entry.decided_at = ot.CANCELLED, utcnow()
            entry.row_version += 1
            ot._flush(db)
            ot._audit(db, None, ot.OVERTIME_CANCELLED, entry,
                      f"Cancelled proposal #{entry.id}: no overtime after the attendance change")
            ot._log_change(db, None, entry, "UPDATED")
            result.cancelled.append(entry.id)
        elif minutes != entry.minutes_requested:
            approvals.withdraw(db, None, module=ot.MODULE, subject_type=ot.SUBJECT_TYPE, subject_id=entry.id,
                               reason="The attendance day changed")
            before = entry.minutes_requested
            entry.minutes_requested = entry.minutes_computed = minutes
            entry.start_at, entry.end_at = _span(item, minutes)
            entry.minutes_approved, entry.reason = None, reason
            entry.row_version += 1
            ot._validate(db, company_id, entry, exclude_ids=[entry.id, entry.supersedes_id])
            ot._flush(db)
            ot._submit(db, None, entry)
            ot._flush(db)
            ot._audit(db, None, ot.OVERTIME_UPDATED, entry,
                      f"Proposal #{entry.id} updated from {before} to {minutes} minutes after an attendance change",
                      before=before, after=minutes)
            result.updated.append(entry.id)
        return
    if entry.status == ot.APPROVED:
        approved = entry.minutes_approved or 0
        if abs(minutes - approved) <= max(rules.min_minutes, 0):
            return
        if entry.source == ot.AUTO:
            result.flagged.append(item.day.isoformat())  # D-26 default: flag only, a person edits it
            return
        successor = OvertimeEntry(
            company_id=company_id, employee_id=employee.id, date=item.day, minutes_requested=minutes,
            minutes_computed=minutes, bucket=tpo.WORKDAY, source=entry.source, status=ot.PENDING,
            comp_off=bool(entry.comp_off), pre_approval=False, reason=reason, version=entry.version + 1,
            supersedes_id=entry.id, requested_by_user_id=None, row_version=1)
        successor.start_at, successor.end_at = _span(item, minutes)
        ot._validate(db, company_id, successor, exclude_ids=[entry.id])
        db.add(successor)
        ot._flush(db)
        ot._submit(db, None, successor)
        ot._flush(db)
        ot._audit(db, None, ot.OVERTIME_VERSION_CREATED, successor,
                  f"Attendance proposes version {successor.version} of approved overtime #{entry.id}: "
                  f"{approved} → {minutes} minutes", supersedes_id=entry.id)
        result.successors.append(successor.id)


def _create(db: Session, employee: Employee, item: DateInput, minutes: int, source: str, reason: str,
            view: ot.PolicyView) -> OvertimeEntry:
    auto = source == ot.AUTO
    entry = OvertimeEntry(
        company_id=employee.company_id, employee_id=employee.id, date=item.day, minutes_requested=minutes,
        minutes_computed=minutes, bucket=tpo.WORKDAY, source=source, status=ot.APPROVED if auto else ot.PENDING,
        minutes_approved=minutes if auto else None, decided_at=utcnow() if auto else None, comp_off=False,
        pre_approval=False, reason=reason, version=1, requested_by_user_id=None, row_version=1)
    entry.start_at, entry.end_at = _span(item, minutes)
    ensure_exclusive(db, employee.company_id, employee.id, item.day, False)
    ot._validate(db, employee.company_id, entry)
    db.add(entry)
    ot._flush(db)
    if auto:
        ot._audit(db, None, ot.OVERTIME_AUTO_APPROVED, entry,
                  f"Approved {minutes} minutes of overtime from attendance automatically (entry #{entry.id})",
                  minutes=minutes, policy_hash=view.policy_hash, day_version=item.row.version)
    else:
        ot._submit(db, None, entry)
        ot._flush(db)
        ot._audit(db, None, ot.OVERTIME_PROPOSED, entry,
                  f"Proposed {minutes} minutes of overtime from attendance (entry #{entry.id})",
                  minutes=minutes, policy_hash=view.policy_hash, day_version=item.row.version)
    ot._log_change(db, None, entry, "CREATED")
    return entry


def _consent(db: Session, company_id: int, employee: Employee, item: DateInput, entry: OvertimeEntry,
             result: WeekResult) -> None:
    state = consent_status(db, company_id, employee.id, item.legal_entity_id, item.day, entry.id)
    if state["required"] and not state["on_file"]:
        result.consent_missing.append(item.day.isoformat())


# ------------------------------------------------------------------------------------------------ batch / job
def weeks_touching(db: Session, company_id: int, employee_id: int, first: date, last: date) -> List[date]:
    starts = set()
    day = first
    while day <= last:
        ws = week_start_day(db, company_id, employee_id, day)
        starts.add(tpo.week_start_of(day, ws))
        day += timedelta(days=7)
    starts.add(tpo.week_start_of(last, week_start_day(db, company_id, employee_id, last)))
    return sorted(starts)


def run(db: Session, company_id: int, *, first: date, last: date,
        employee_ids: Optional[Sequence[int]] = None) -> dict:
    """Propose for every employee with FINAL days in [first, last] (the job and the manual trigger)."""
    query = db.query(AttendanceDay.employee_id).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.shift_date >= first, AttendanceDay.shift_date <= last,
        AttendanceDay.evaluation_state == "FINAL").distinct()
    if employee_ids is not None:
        query = query.filter(AttendanceDay.employee_id.in_(list(employee_ids) or [0]))
    totals = {"employees": 0, "weeks": 0, "created": 0, "updated": 0, "cancelled": 0, "successors": 0,
              "flagged": 0, "consent_missing": 0, "weeks_detail": []}
    today = clock.company_today(db, company_id)
    for (employee_id,) in query.order_by(AttendanceDay.employee_id).all():
        employee = db.get(Employee, employee_id)
        if employee is None:
            continue
        totals["employees"] += 1
        for week in weeks_touching(db, company_id, employee_id, first, last):
            with db.begin_nested():
                week_result = propose_week(db, company_id, employee, week, today=today)
            totals["weeks"] += 1
            for key in ("created", "updated", "cancelled", "successors", "flagged", "consent_missing"):
                totals[key] += len(getattr(week_result, key))
            if any(getattr(week_result, k) for k in ("created", "updated", "cancelled", "successors", "flagged")):
                totals["weeks_detail"].append(week_result.as_dict())
    return totals


def run_for_principal(db: Session, principal: Principal, *, first: date, last: date,
                      employee_ids: Optional[Sequence[int]] = None) -> dict:
    """Manual trigger (``overtime:approve`` or ``payroll:inputs:edit`` in scope): the same proposals as the job."""
    held = [c for c in ot.PROPOSAL_PERMISSIONS if principal.has(c)]
    if not held:
        raise forbidden()
    if last < first or (last - first).days > 62:
        raise bad_request("Choose a range of at most 62 days.", "INVALID_RANGE")
    scope_ids = None
    if not any(principal.has_company_scope(c) for c in held):
        from sqlalchemy import or_

        clauses = [authz.employee_scope(db, principal, code).clause(Employee.id) for code in held]
        scope_ids = {i for (i,) in db.query(Employee.id).filter(Employee.company_id == principal.company_id,
                                                                  or_(*clauses))}
    if employee_ids is not None:
        wanted = set(employee_ids)
        if scope_ids is not None and not wanted <= scope_ids:
            raise not_found("One or more employees")
        scope_ids = wanted
    return run(db, principal.company_id, first=first, last=last,
               employee_ids=sorted(scope_ids) if scope_ids is not None else None)


# ------------------------------------------------------------------------------------------------ context
def _cap_usage(db: Session, company_id: int, employee_id: int, on: date) -> dict:
    """Approved minutes per window around ``on``: calendar quarter (payroll's window) and rolling 3 months."""
    q_start = date(on.year, 3 * ((on.month - 1) // 3) + 1, 1)
    rolling_start = on - timedelta(days=91)

    def approved(first: date, last: date) -> int:
        return int(db.query(func.coalesce(func.sum(OvertimeEntry.minutes_approved), 0)).filter(
            OvertimeEntry.company_id == company_id, OvertimeEntry.employee_id == employee_id,
            OvertimeEntry.status == ot.APPROVED, OvertimeEntry.date >= first, OvertimeEntry.date <= last).scalar())

    return {"quarter": {"from": q_start.isoformat(), "to": on.isoformat(), "minutes": approved(q_start, on)},
            "rolling_3_months": {"from": rolling_start.isoformat(), "to": on.isoformat(),
                                 "minutes": approved(rolling_start, on)}}


def context(db: Session, principal: Principal, entry_id: int) -> dict:
    """The approval context of an entry (§7): attendance day, caps, consent, comp-off option."""
    entry = ot._load(db, principal, entry_id)
    day = db.query(AttendanceDay).filter(AttendanceDay.company_id == entry.company_id,
                                         AttendanceDay.employee_id == entry.employee_id,
                                         AttendanceDay.shift_date == entry.date).first()
    view = ot.resolve_policy(db, entry.company_id, entry.employee_id, entry.date)
    rules = view.rules
    usage = _cap_usage(db, entry.company_id, entry.employee_id, entry.date)
    caps = []
    if rules is not None:
        for window, hours in sorted((rules.caps or {}).items()):
            limit = int(Decimal(hours) * 60)
            used = usage["quarter"]["minutes"] if window == "per_quarter" else None
            caps.append({"window": window, "limit_minutes": limit, "used_minutes": used,
                         "left_minutes": None if used is None else max(0, limit - used),
                         "rolling_used_minutes": usage["rolling_3_months"]["minutes"] if window == "per_quarter"
                         else None,
                         "projected_over": (window == "per_quarter"
                                            and usage["rolling_3_months"]["minutes"] > limit)})
    # Approvers see the attendance context only within their attendance:read reach (§12).
    can_attendance = authz.is_self(principal, entry.employee_id) or authz.can_access_employee(
        db, principal, "attendance:read", entry.employee_id)
    return {
        "entry_id": entry.id, "date": entry.date.isoformat(), "status": entry.status,
        "origin": "ATTENDANCE" if _origin_is_attendance(entry) else "REQUEST",
        "attendance_day": ({
            "evaluation_state": day.evaluation_state, "version": day.version, "day_result": day.day_result,
            "first_in_at": day.first_in_at.isoformat() if day.first_in_at else None,
            "last_out_at": day.last_out_at.isoformat() if day.last_out_at else None,
            "expected_start_at": day.expected_start_at.isoformat() if day.expected_start_at else None,
            "expected_end_at": day.expected_end_at.isoformat() if day.expected_end_at else None,
            "net_minutes": day.net_minutes, "ot_eligible_minutes": day.ot_eligible_minutes,
            "ot_candidate_minutes": day.ot_candidate_minutes, "exceptions": list(day.exception_codes or []),
        } if day is not None and can_attendance else None),
        "policy": None if rules is None else {
            "source": rules.source, "comp_off_allowed": bool(rules.comp_off_allowed),
            "min_minutes": rules.min_minutes, "combination": rules.threshold_combination,
            "policy_hash": view.policy_hash},
        "caps": caps, "usage": usage,
        "consent": consent_status(db, entry.company_id, entry.employee_id, day.legal_entity_id if day else None,
                                  entry.date, entry.id),
        "worked_vs_requested": None if day is None or day.net_minutes is None else {
            "requested": entry.minutes_requested, "candidate": day.ot_candidate_minutes},
    }


def convert(db: Session, principal: Principal, entry_id: int, outcome: str, *, minutes: Optional[int] = None,
            remarks: Optional[str] = None) -> OvertimeEntry:
    """One-click decisions of the review queue: ``PAY`` (approve for payroll) or ``COMP_OFF`` (approve as comp-off)."""
    if outcome not in ("PAY", "COMP_OFF"):
        raise bad_request("Choose pay or comp-off.", "INVALID_FIELDS")
    entry = ot._load(db, principal, entry_id)
    comp_off = outcome == "COMP_OFF"
    ensure_exclusive(db, entry.company_id, entry.employee_id, entry.date, comp_off, exclude_ids=[entry.id,
                                                                                                 entry.supersedes_id])
    step = None
    if entry.approval_request_id is not None:
        from app.models.approvals import ApprovalRequest

        request = db.get(ApprovalRequest, entry.approval_request_id)
        step = getattr(request, "current_step", None)
    return ot.decide_entry(db, principal, entry_id, "APPROVE", minutes_approved=minutes, comp_off=comp_off,
                           remarks=remarks, expected_step=step)


def queue(db: Session, principal: Principal, *, skip: int = 0, limit: int = 25) -> dict:
    """Pending proposals and requests the caller may decide, with the attendance candidate beside them."""
    total, items = ot.list_entries(db, principal, awaiting=True, skip=skip, limit=limit)
    ids = [(i["employee_id"], date.fromisoformat(i["date"])) for i in items]
    days = {}
    if ids:
        rows = db.query(AttendanceDay).filter(
            AttendanceDay.company_id == principal.company_id,
            AttendanceDay.employee_id.in_({e for e, _ in ids}),
            AttendanceDay.shift_date.in_({d for _, d in ids})).all()
        days = {(r.employee_id, r.shift_date): r for r in rows}
    for i in items:
        row = days.get((i["employee_id"], date.fromisoformat(i["date"])))
        i["attendance"] = None if row is None else {"net_minutes": row.net_minutes,
                                                     "ot_candidate_minutes": row.ot_candidate_minutes,
                                                     "evaluation_state": row.evaluation_state}
    return {"items": items, "total": total, "skip": skip, "limit": limit}
