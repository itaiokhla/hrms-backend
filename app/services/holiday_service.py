"""Company holidays and holiday calendars (3.4 §8; 02 §10, §19).

A branch observes the holidays of its holiday calendar and of that calendar's ancestors. A
holiday created on the legacy screen with a location belongs to that branch's own calendar, one
without a location to the tenant-wide COMPANY calendar (migration 0008 mapped existing rows the
same way); ``location`` stays for today's screens. Branches without a calendar fall back to the
legacy rule (no location, or the branch's name). A transferred employee follows the branch of each
date (EC-HOL-04). Past holidays keep their date, place and type (EC-HOL-06); every change is
written to the source change log so open pay runs detect it.
"""

from collections import defaultdict
from datetime import date
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import extract, func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import conflict, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.holiday import Holiday
from app.models.org import Branch, EmployeeAssignment
from app.models.pay_org import HolidayCalendar
from app.services.audit_log_service import AuditLogService

MAX_DEPTH = 50
LOCKED_FIELDS = ("date", "location", "is_optional")


def serialize(h: Holiday) -> dict:
    return {
        "id": h.id,
        "name": h.name,
        "date": h.date.isoformat(),
        "location": h.location,
        "is_optional": h.is_optional,
        "description": h.description,
        "calendar_id": h.calendar_id,
        "holiday_type": h.holiday_type,
    }


def content(h: Holiday) -> dict:
    """The fields that decide pay; their hash is the holiday's version (3.4 §2)."""
    return {"id": h.id, "name": h.name, "date": h.date.isoformat(), "calendar_id": h.calendar_id, "holiday_type": h.holiday_type,
            "location": h.location, "is_optional": bool(h.is_optional)}


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------
def calendar_lineage(db: Session, company_id: int, calendar_id: Optional[int]) -> List[int]:
    """The calendar and its ancestors, nearest first."""
    lineage: List[int] = []
    current = calendar_id
    while current is not None and current not in lineage and len(lineage) < MAX_DEPTH:
        lineage.append(current)
        current = db.query(HolidayCalendar.parent_calendar_id).filter(
            HolidayCalendar.company_id == company_id, HolidayCalendar.id == current
        ).scalar()
    return lineage


def branch_holidays(db: Session, company_id: int, branch: Optional[Branch], start: date, end: date) -> List[Holiday]:
    """Holidays observed at ``branch`` between ``start`` and ``end`` (its calendar lineage, else the legacy rule)."""
    query = db.query(Holiday).filter(Holiday.company_id == company_id, Holiday.date >= start, Holiday.date <= end)
    if branch is not None and branch.holiday_calendar_id is not None:
        query = query.filter(Holiday.calendar_id.in_(calendar_lineage(db, company_id, branch.holiday_calendar_id)))
    elif branch is not None:
        query = query.filter(or_(Holiday.location.is_(None), Holiday.location == branch.name))
    else:
        query = query.filter(Holiday.location.is_(None))
    return query.order_by(Holiday.date, Holiday.id).all()


def holidays_by_branch_date(
    db: Session, company_id: int, branch_dates: Dict[Optional[int], Sequence[date]]
) -> Dict[date, List[Holiday]]:
    """Holidays per date for ``{branch_id: dates the employee was there}`` (EC-HOL-04)."""
    result: Dict[date, List[Holiday]] = defaultdict(list)
    for branch_id, days in branch_dates.items():
        if not days:
            continue
        branch = db.get(Branch, branch_id) if branch_id is not None else None
        wanted = set(days)
        for holiday in branch_holidays(db, company_id, branch, min(wanted), max(wanted)):
            if holiday.date in wanted:
                result[holiday.date].append(holiday)
    return result


def _current_branch(db: Session, company_id: int, employee_id: int, on: date) -> Optional[Branch]:
    placement = (
        db.query(EmployeeAssignment)
        .filter(
            EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
            EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= on,
            or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on),
        )
        .first()
    )
    return db.get(Branch, placement.branch_id) if placement is not None else None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def list_holidays(db: Session, principal: Principal, year: Optional[int], location: Optional[str], mine: bool) -> List[dict]:
    query = db.query(Holiday).filter(Holiday.company_id == principal.company_id)
    if year:
        query = query.filter(extract("year", Holiday.date) == year)
    if mine and principal.employee_id:
        employee_location = db.query(Employee.location).filter(Employee.id == principal.employee_id).scalar()
        legacy = or_(Holiday.location.is_(None) & Holiday.calendar_id.is_(None), Holiday.location == employee_location)
        branch = _current_branch(db, principal.company_id, principal.employee_id, clock.company_today(db, principal.company_id))
        if branch is not None and branch.holiday_calendar_id is not None:
            lineage = calendar_lineage(db, principal.company_id, branch.holiday_calendar_id)
            query = query.filter(or_(Holiday.calendar_id.in_(lineage), legacy))
        else:
            query = query.filter(or_(Holiday.location.is_(None), Holiday.location == employee_location))
    elif location:
        query = query.filter(or_(Holiday.location.is_(None), Holiday.location == location))
    return [serialize(h) for h in query.order_by(Holiday.date, Holiday.id).all()]


def locations(db: Session, principal: Principal) -> List[str]:
    rows = (
        db.query(Holiday.location)
        .filter(Holiday.company_id == principal.company_id, Holiday.location.is_not(None))
        .union(
            db.query(Employee.location).filter(
                Employee.company_id == principal.company_id, Employee.location.is_not(None)
            )
        )
        .all()
    )
    return sorted({r[0] for r in rows if r[0]})


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _record_change(db: Session, principal: Optional[Principal], holiday: Holiday, change: str,
                   dates: Iterable[date]) -> None:
    from app.payroll.services import time_inputs

    days = sorted(set(dates))
    version = content(holiday) | ({"deleted": True} if change == "DELETED" else {})
    time_inputs.log_change(
        db, holiday.company_id, record_type="holiday", record_id=holiday.id, change=change, content=version,
        date_from=days[0], date_to=days[-1], actor_user_id=principal.user_id if principal else None,
        details={"calendar_id": holiday.calendar_id, "holiday_type": holiday.holiday_type},
    )
    time_inputs.reevaluate_leave_days(db, holiday.company_id, days, actor_user_id=principal.user_id if principal else None)


def save(db: Session, principal: Principal, data, holiday_id: Optional[int] = None) -> Holiday:
    from app.payroll.services import org_dimensions

    cid = principal.company_id
    values = data.model_dump()
    values["location"] = values.get("location") or None
    today = clock.company_today(db, cid)
    if holiday_id:
        holiday = db.query(Holiday).filter(Holiday.company_id == cid, Holiday.id == holiday_id).first()
        if holiday is None:
            raise not_found("Holiday")
        before = content(holiday)
        changed = [k for k in LOCKED_FIELDS if k in values and values[k] != getattr(holiday, k)]
        if changed and (holiday.date < today or values.get("date", holiday.date) < today):
            raise conflict("A past holiday keeps its date, location and type; payroll may already have used it.",
                           "HOLIDAY_IN_PAST")
        calendar_id = holiday.calendar_id
        if "location" in changed or calendar_id is None:
            calendar_id = org_dimensions.calendar_for_holiday(db, cid, values["location"], principal)
    else:
        holiday, before, changed = None, None, []
        calendar_id = org_dimensions.calendar_for_holiday(db, cid, values["location"], principal)

    clash = db.query(Holiday.id).filter(
        Holiday.company_id == cid,
        Holiday.date == values["date"],
        func.lower(Holiday.name) == values["name"].lower(),
        func.coalesce(Holiday.calendar_id, 0) == (calendar_id or 0),
    )
    if holiday_id:
        clash = clash.filter(Holiday.id != holiday_id)
    if clash.first():
        raise conflict("This holiday already exists on that date.")
    if holiday is None:
        holiday = Holiday(company_id=cid)
        db.add(holiday)
    old_date = holiday.date
    for key, value in values.items():
        setattr(holiday, key, value)
    holiday.calendar_id = calendar_id
    if holiday_id is None or "is_optional" in changed:
        # The payroll view of the same holiday: its type (3.4 §8).
        holiday.holiday_type = org_dimensions.default_holiday_type(bool(holiday.is_optional))
    db.flush()
    AuditLogService.log(db, principal, "Holidays", "SAVE", f"Saved holiday '{holiday.name}' on {holiday.date}",
                        "holiday", holiday.id, details={"before": before, "after": content(holiday)})
    if before != content(holiday):
        _record_change(db, principal, holiday, "UPDATED" if holiday_id else "CREATED",
                       [d for d in (old_date, holiday.date) if d is not None])
    return holiday


def delete(db: Session, principal: Principal, holiday_id: int) -> None:
    holiday = db.query(Holiday).filter(Holiday.company_id == principal.company_id, Holiday.id == holiday_id).first()
    if holiday is None:
        raise not_found("Holiday")
    if holiday.date < clock.company_today(db, principal.company_id):
        raise conflict("Past holidays are kept for payroll history and cannot be deleted.")
    AuditLogService.log(db, principal, "Holidays", "DELETE", f"Deleted holiday '{holiday.name}'", "holiday", holiday.id,
                        details={"before": content(holiday)})
    snapshot = SimpleNamespace(id=holiday.id, company_id=holiday.company_id, name=holiday.name, date=holiday.date,
                               calendar_id=holiday.calendar_id, holiday_type=holiday.holiday_type,
                               location=holiday.location, is_optional=holiday.is_optional)
    db.delete(holiday)
    db.flush()
    _record_change(db, principal, snapshot, "DELETED", [snapshot.date])
