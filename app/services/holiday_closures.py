"""Emergency and backdated closures (LH 04-holiday/01 §5.8; 04-holiday/02 §10; LH-HOL-24, LH-PAY-06).

``backdated_closure`` records a closure on today or a past date of a PUBLISHED year: inputs date, name, portion,
type (default COMPANY), category (default EMERGENCY) and a mandatory reason; window ``holiday.backdate_days``
(``BACKDATE_LIMIT``). The dry run returns the amendment impact plus the closure impact restricted to the date —
employees resolved to the calendar, who was scheduled to work (and on which shift), attendance sessions that become
holiday work, approved and pending leave, and each employee's pay-period state (OPEN / INPUT_LOCKED / FINALIZED).
When a locked or finalized period is affected the commit needs the date typed back (``confirm_date``).

The commit is the amendment of 01 §6.4 (audit ``BACKDATED_CLOSURE``, ``source_change_log``, year version + 1, leave
recount job for the date whatever the period state). ``flag_payroll`` then marks payroll: employees in an
input-locked period are listed for the payroll manager (change detection reads ``source_change_log``), finalized
periods get the job ``holiday.closure_retro``, which runs ``retro.detect`` per employee for that date — the
finalized run never changes; the difference is paid in a later run.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, conflict
from app.core.principal import Principal
from app.models.pay_org import HolidayCalendar
from app.payroll.services import jobs
from app.services.audit_log_service import AuditLogService

RETRO_JOB = "holiday.closure_retro"
UNPAID_HINT = "Not paid? Record unpaid leave for the affected employees instead."


def _svc():
    from app.services import holiday_calendar_service

    return holiday_calendar_service


def _years():
    from app.services import holiday_years

    return holiday_years


def affected_employees(db: Session, company_id: int, calendar: HolidayCalendar, day: date) -> List[int]:
    """Employees who resolve to the calendar (or a calendar including it) on ``day``."""
    from app.payroll.services import time_inputs

    svc = _svc()
    flattener = svc.Flattener(db, company_id)
    calendars = set(svc.dependents(flattener.includes, calendar.id)) | {calendar.id}
    candidates: Set[int] = set()
    for cal in sorted(calendars):
        candidates |= svc.calendar_users(db, company_id, cal, day)[1]
    if not candidates:
        return []
    facts = time_inputs.day_facts_many(db, company_id, sorted(candidates), day, day)
    return sorted(e for e, items in facts.items() if items and items[0].employed and items[0].calendar_id in calendars)


def closure_impact(db: Session, company_id: int, calendar: HolidayCalendar, day: date) -> dict:
    """What the closure touches on ``day`` (01 §5.8 step 2), per employee and in totals."""
    from app.models.attendance import Attendance
    from app.models.employee import Employee, Shift
    from app.models.leave import Leave
    from app.models.time_inputs import LeaveDay
    from app.payroll.services import calendars as pay_calendars, time_inputs

    employees = affected_employees(db, company_id, calendar, day)
    facts = time_inputs.day_facts_many(db, company_id, employees, day, day) if employees else {}
    names = {e.id: f"{e.first_name} {e.last_name}".strip() for e in db.query(Employee).filter(
        Employee.company_id == company_id, Employee.id.in_(employees or [0]))}
    sessions = {e for (e,) in db.query(Attendance.employee_id).filter(
        Attendance.company_id == company_id, Attendance.date == day, Attendance.employee_id.in_(employees or [0]),
        Attendance.voided_at.is_(None))}  # AS-M2: voided sessions never count
    approved: Dict[int, List[int]] = {}
    for leave_id, employee_id in db.query(LeaveDay.leave_id, LeaveDay.employee_id).filter(
            LeaveDay.company_id == company_id, LeaveDay.date == day, LeaveDay.voided_at.is_(None),
            LeaveDay.employee_id.in_(employees or [0])):
        approved.setdefault(employee_id, []).append(leave_id)
    pending = {e for (e,) in db.query(Leave.employee_id).filter(
        Leave.company_id == company_id, Leave.employee_id.in_(employees or [0]), func.lower(Leave.status) == "pending",
        Leave.start_date <= day, Leave.end_date >= day)}
    shift_names = dict(db.query(Shift.id, Shift.name).filter(Shift.company_id == company_id))
    rows = []
    totals = {"employees": len(employees), "scheduled": 0, "on_shift": 0, "attendance_sessions": 0,
              "approved_leave": 0, "pending_leave": 0, "input_locked": 0, "finalized": 0}
    for employee_id in employees:
        fact = facts[employee_id][0]
        state = pay_calendars.period_state_on(db, company_id, employee_id, day)
        scheduled = fact.working_portion != "NONE"
        shift = shift_names.get(fact.shift_ids[0]) if fact.shift_ids else None
        rows.append({"employee_id": employee_id, "name": names.get(employee_id), "scheduled": scheduled,
                     "working_portion": fact.working_portion, "shift": shift,
                     "scheduled_minutes": fact.scheduled_minutes,
                     "attendance_session": employee_id in sessions,
                     "approved_leave_ids": sorted(set(approved.get(employee_id, ()))),
                     "pending_leave": employee_id in pending, "period_state": state})
        totals["scheduled"] += scheduled
        totals["on_shift"] += bool(scheduled and shift)
        totals["attendance_sessions"] += employee_id in sessions
        totals["approved_leave"] += bool(approved.get(employee_id))
        totals["pending_leave"] += employee_id in pending
        totals["input_locked"] += state == "INPUT_LOCKED"
        totals["finalized"] += state == "FINALIZED"
    return {"date": day.isoformat(), "totals": totals, "employees": rows,
            "requires_typed_confirm": bool(totals["input_locked"] or totals["finalized"])}


def _validate(db: Session, principal: Principal, calendar: HolidayCalendar, data: Mapping[str, Any]) -> Tuple[date, str]:
    years = _years()
    svc = _svc()
    day = years._parse_date(data.get("date"))  # noqa: SLF001
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    if day > today:
        raise bad_request("A closure is recorded for today or a past date. Add a future holiday instead.",
                          "NOT_BACKDATED")
    window = svc.backdate_days(db, principal.company_id)
    if (today - day).days > window:
        raise AppError(422, f"Closures can be recorded up to {window} days back. Ask an administrator to raise the "
                            f"limit or record leave adjustments.", "BACKDATE_LIMIT")
    reason = (data.get("reason") or "").strip()
    if not reason:
        raise AppError(422, "Give the reason for the closure (for example the emergency).", "REASON_REQUIRED")
    if years.state_of(db, principal.company_id, calendar.id, day.year) != "PUBLISHED":
        raise conflict(f"{day.year} is not published. Publish the year first, then record the closure.",
                       "YEAR_NOT_PUBLISHED")
    return day, reason


def backdated_closure(db: Session, principal: Principal, calendar_id: int, data: Mapping[str, Any]) -> Tuple[int, dict]:
    """POST /holiday-calendars/{id}/closures: dry run (impact) or commit (amendment + payroll flags)."""
    svc = _svc()
    years = _years()
    calendar = svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    day, reason = _validate(db, principal, calendar, data)
    payload = {"date": day.isoformat(), "name": (data.get("name") or "").strip() or "Emergency closure",
               "portion": data.get("portion") or "FULL", "holiday_type": data.get("holiday_type") or "COMPANY",
               "category": data.get("category") or "EMERGENCY", "status": "CONFIRMED", "reason": reason,
               "description": data.get("description")}
    impact = closure_impact(db, principal.company_id, calendar, day)
    hint = UNPAID_HINT if payload["category"] in ("EMERGENCY", "SHUTDOWN") else None
    if data.get("dry_run"):
        status, body = years.add_occurrence(db, principal, calendar.id, {**payload, "dry_run": True})
        return status, {**body, "closure": impact, "hint": hint}
    if impact["requires_typed_confirm"] and str(data.get("confirm_date") or "") != day.isoformat():
        raise AppError(422, f"A locked or finalized pay period is affected: type {day.isoformat()} to confirm.",
                       "CONFIRM_DATE_REQUIRED", extra={"details": {"totals": impact["totals"]}})
    status, body = years.add_occurrence(db, principal, calendar.id, {
        **payload, "acknowledged_warnings": list(data.get("acknowledged_warnings") or ())})
    return status, {**body, "closure": impact, "hint": hint}


def flag_payroll(db: Session, principal: Optional[Principal], calendar: HolidayCalendar, dates: Iterable[date],
                 holiday_ids: Iterable[int], *, action: str = "CLOSURE_PAYROLL_FLAGGED") -> dict:
    """After a backdated closure commits (called by ``amend_year``): input-locked employees are listed for change
    detection; finalized periods get ``holiday.closure_retro`` (``retro.detect`` per employee, 05 §5.6)."""
    from app.payroll.services import calendars as pay_calendars

    cid = calendar.company_id
    locked: Dict[str, List[int]] = {}
    finalized: Dict[str, List[int]] = {}
    holiday_ids = [i for i in holiday_ids if i]
    for day in sorted(set(dates)):
        for employee_id in affected_employees(db, cid, calendar, day):
            state = pay_calendars.period_state_on(db, cid, employee_id, day)
            if state == "INPUT_LOCKED":
                locked.setdefault(day.isoformat(), []).append(employee_id)
            elif state == "FINALIZED":
                finalized.setdefault(day.isoformat(), []).append(employee_id)
    job_id = None
    if finalized:
        ids = sorted(holiday_ids)
        job = jobs.enqueue(db, company_id=cid, type=RETRO_JOB,
                              payload={"calendar_id": calendar.id, "holiday_ids": ids, "dates": finalized},
                              idempotency_key=f"{RETRO_JOB}:{calendar.id}:{'-'.join(str(i) for i in ids)}",
                              created_by_user_id=getattr(principal, "user_id", None))
        job_id = getattr(job, "id", job)
    if locked or finalized:
        AuditLogService.log(db, principal, "Holidays", action,
                            f"{calendar.code}: holiday change reaches locked pay periods", "holiday_calendar",
                            calendar.id,
                            details={"input_locked": {d: len(v) for d, v in locked.items()},
                                     "finalized": {d: len(v) for d, v in finalized.items()}, "job_id": job_id})
    return {"input_locked": locked, "finalized": finalized, "retro_job_id": job_id}


@jobs.register(RETRO_JOB)
def _closure_retro_job(ctx) -> dict:
    """Finalized periods: one retro proposal per employee and closure date (the finalized run is never edited)."""
    from app.payroll.services import retro

    payload = ctx.payload or {}
    change = retro.Change("ATTENDANCE", reason="Backdated holiday closure",
                          ref={"calendar_id": payload.get("calendar_id"), "holiday_ids": payload.get("holiday_ids")})
    proposals = 0
    for day, employees in sorted((payload.get("dates") or {}).items()):
        on = date.fromisoformat(day)
        for employee_id in employees:
            outcomes = retro.detect(ctx.db, None, ctx.company_id, int(employee_id), on, change, to_date=on)
            proposals += sum(1 for o in outcomes if o.proposal is not None)
    return {"proposals": proposals}
