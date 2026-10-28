"""Day facts over HTTP (LH 05 §3.6; 11 §6.13; LH 14 WI-1.9 `app/routes/calendar_days.py`).

* ``GET /api/v1/calendar/days?employee_id&from&to`` — one employee's day facts (< 400 days). The caller's own days
  always; another employee's with ``leave:read`` or ``holiday:read`` covering that employee (out of scope -> 404).
* ``GET /api/v1/me/calendar?from&to`` — the viewer's own days plus their approved / pending leave per date, the
  calendars and work weeks that applied (segments), the employee-local today and the week layout of the viewer's
  current work week (week start and weekly-off weekdays) for the year grid of ``/holidays``.

Only rows of PUBLISHED years ever appear (DRAFT years are hidden by the resolver); dates are the employee's local
calendar dates (LH-TZ-04: two employees in different zones each see their own "today").
"""

from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import bad_request, forbidden, not_found
from app.core.principal import Principal, get_principal
from app.database.db import get_db

router = APIRouter(prefix="/api/v1", tags=["Day facts"])

MAX_DAY_RANGE = 399  # LH 05 §3.1: end − start < MAX_RANGE_DAYS (400)


def _range(start: date, end: date) -> None:
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days > MAX_DAY_RANGE:
        raise bad_request(f"A range covers at most {MAX_DAY_RANGE + 1} days.", "RANGE_TOO_LONG")


def serialize_fact(fact) -> dict:
    """One day (LH 05 §3.6 response item; no reasons, no leave-type names)."""
    holiday = fact.holiday
    return {
        "date": fact.date.isoformat(), "weekday": fact.date.weekday(), "employed": fact.employed,
        "legal_entity_id": fact.legal_entity_id, "branch_id": fact.branch_id, "timezone": fact.timezone,
        "calendar_id": fact.calendar_id, "calendar_source": fact.calendar_source,
        "work_week_id": fact.work_week_id, "pattern_version": fact.pattern_version, "work_source": fact.work_source,
        "weekly_off": fact.weekly_off, "weekly_off_portion": fact.weekly_off_portion, "rest_day": fact.rest_day,
        "declared_working_day": fact.declared_working_day, "working_portion": fact.working_portion,
        "working": fact.working, "scheduled_minutes": fact.scheduled_minutes,
        "calendar_version": fact.calendar_version, "observed_elsewhere": fact.observed_elsewhere,
        "shift_source": fact.shift_source, "shift_ids": list(fact.shift_ids),
        "holiday": None if holiday is None else {
            "id": holiday.id, "name": holiday.name, "type": holiday.type, "category": holiday.category,
            "portion": holiday.portion, "status": holiday.status, "observance": holiday.observance,
            "observed_from_date": holiday.observed_from_date.isoformat() if holiday.observed_from_date else None,
            "non_working": holiday.non_working,
            # Deprecated alias kept for older clients (SPINE V3-062): it never meant "paid".
            "paid": holiday.non_working,
        },
    }


def week_layout(weekend: List[int]) -> int:
    """The first weekday of a week: the day after the last day of the weekend run (Fri-Sat -> Sunday; none -> Monday)."""
    days = sorted(set(weekend))
    if not days or len(days) == 7:
        return 0
    last = next(d for d in days if (d + 1) % 7 not in days)
    return (last + 1) % 7


def current_weekend(db: Session, company_id: int, employee_id: int, on: date) -> List[int]:
    """The full weekly-off weekdays of the employee's work week valid on ``on`` (a CYCLE has none)."""
    from app.payroll.services import time_inputs
    from app.services.holiday_assignments import WorkWeekResolver

    rows = time_inputs.primary_assignments(db, company_id, employee_id)
    placement = time_inputs._covering(rows, on) or time_inputs._nearest(rows, on)  # noqa: SLF001
    week = WorkWeekResolver(db, company_id).resolve(placement, on, employee_id)
    pattern = week.pattern
    if getattr(pattern, "kind", "WEEKLY") == "CYCLE":
        return []
    return sorted(int(d) for d in (pattern.weekly_off_days or ()))


def _segments(facts, key, label) -> List[dict]:
    out: List[dict] = []
    for f in facts:
        value = key(f)
        if out and out[-1]["id"] == value:
            out[-1]["to"] = f.date.isoformat()
        else:
            out.append({"id": value, "from": f.date.isoformat(), "to": f.date.isoformat(), **label(value)})
    return out


def _calendar_label(db: Session, company_id: int, cache: Dict) -> callable:
    from app.models.holiday import HolidayCalendarYear
    from app.models.pay_org import HolidayCalendar

    def label(calendar_id: Optional[int]) -> dict:
        if calendar_id is None:
            return {"code": None, "name": None, "years": {}}
        if calendar_id not in cache:
            row = db.get(HolidayCalendar, calendar_id)
            years = {str(y): {"status": s, "version": v} for y, s, v in db.query(
                HolidayCalendarYear.year, HolidayCalendarYear.status, HolidayCalendarYear.version).filter(
                HolidayCalendarYear.company_id == company_id, HolidayCalendarYear.calendar_id == calendar_id)}
            cache[calendar_id] = {"code": row.code if row else None, "name": row.name if row else None,
                                  "years": years}
        return cache[calendar_id]

    return label


@router.get("/calendar/days")
def calendar_days(employee_id: int, from_: date = Query(..., alias="from"), to: date = Query(...),
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Day facts of one employee (LH 05 §3.6): self, or ``leave:read`` / ``holiday:read`` covering the employee."""
    from app.core import authz
    from app.models.employee import Employee
    from app.payroll.services import time_inputs
    from app.services import org_structure_service

    _range(from_, to)
    if not authz.is_self(principal, employee_id):
        codes = [c for c in ("leave:read", "holiday:read") if principal.has(c)]
        if not codes:
            raise forbidden()
        last_error: Optional[Exception] = None
        for code in codes:
            try:
                authz.ensure_employee_access(db, principal, code, employee_id)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - try the next covering permission
                last_error = exc
        if last_error is not None:
            raise last_error
    employee = db.query(Employee).filter(Employee.company_id == principal.company_id,
                                         Employee.id == employee_id).first()
    if employee is None:
        raise not_found("Employee")
    facts = time_inputs.day_facts(db, principal.company_id, employee_id, from_, to)
    today = org_structure_service.employee_local_today(db, employee)
    weekend = current_weekend(db, principal.company_id, employee_id, today)
    return {"employee_id": employee_id, "local_today": today.isoformat(), "weekend": weekend,
            "week_start": week_layout(weekend), "days": [serialize_fact(f) for f in facts]}


@router.get("/me/calendar")
def my_calendar(from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
                principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Own day facts (LH 05 §3.6) with own leave per date, calendar and work-week segments and the local today."""
    from app.models.employee import Employee
    from app.models.leave import Leave, LeaveType
    from app.models.time_inputs import LeaveDay
    from app.payroll.services import time_inputs
    from app.services import org_structure_service

    if not principal.employee_id:
        raise not_found("Employee")
    employee = db.get(Employee, principal.employee_id)
    cid = principal.company_id
    today = org_structure_service.employee_local_today(db, employee)
    start = from_ or today.replace(day=1)
    end = to or (start + timedelta(days=41))
    _range(start, end)
    facts = time_inputs.day_facts(db, cid, employee.id, start, end)
    leave: Dict[str, List[dict]] = defaultdict(list)
    names = dict(db.query(LeaveType.code, LeaveType.name).filter(LeaveType.company_id == cid))
    for leave_id, day, portion, units, counted, code, status in db.query(
            LeaveDay.leave_id, LeaveDay.date, LeaveDay.portion, LeaveDay.units, LeaveDay.counts_against_balance,
            Leave.leave_type, Leave.status).join(
            Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id)).filter(
            LeaveDay.company_id == cid, LeaveDay.employee_id == employee.id, LeaveDay.date >= start,
            LeaveDay.date <= end, LeaveDay.voided_at.is_(None)):
        leave[day.isoformat()].append({"leave_id": leave_id, "portion": portion, "status": (status or "").upper(),
                                       "charged_units": f"{units if counted else 0:.2f}",
                                       "label": names.get(code, code)})
    covered = {(d["leave_id"], k) for k, items in leave.items() for d in items}
    for row in db.query(Leave).filter(Leave.company_id == cid, Leave.employee_id == employee.id,
                                      Leave.start_date <= end, Leave.end_date >= start,
                                      func.lower(Leave.status) == "pending"):
        day = max(row.start_date, start)
        while day <= min(row.end_date, end):
            if (row.id, day.isoformat()) not in covered:
                leave[day.isoformat()].append({"leave_id": row.id, "portion": "FULL", "status": "PENDING",
                                               "charged_units": None, "label": names.get(row.leave_type, row.leave_type)})
            day += timedelta(days=1)
    weekend = current_weekend(db, cid, employee.id, today)
    cache: Dict = {}
    days = []
    for f in facts:
        item = serialize_fact(f)
        item["leave"] = leave.get(f.date.isoformat(), [])
        days.append(item)
    return {
        "employee_id": employee.id, "local_today": today.isoformat(), "timezone": org_structure_service.placement_zone(
            db, time_inputs._covering(time_inputs.primary_assignments(db, cid, employee.id), today), cid),  # noqa: SLF001
        "weekend": weekend, "week_start": week_layout(weekend), "from": start.isoformat(), "to": end.isoformat(),
        "calendars": _segments(facts, lambda f: f.calendar_id, _calendar_label(db, cid, cache)),
        "work_weeks": _segments(facts, lambda f: f.work_week_id, lambda _v: {}),
        "days": days,
    }
