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

from sqlalchemy import and_, exists, extract, func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import conflict, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.holiday import Holiday
from app.models.org import Branch, EmployeeAssignment
from app.services.audit_log_service import AuditLogService

LOCKED_FIELDS = ("date", "location", "is_optional")


def counting_clause():
    """SQL filter for readers that bypass the flattener: not CANCELLED and not in a DRAFT year (01 §6.1)."""
    from app.models.holiday import HolidayCalendarYear

    draft = exists().where(and_(HolidayCalendarYear.company_id == Holiday.company_id,
                                HolidayCalendarYear.calendar_id == Holiday.calendar_id,
                                HolidayCalendarYear.year == extract("year", Holiday.date),
                                HolidayCalendarYear.status == "DRAFT"))
    return and_(Holiday.status != "CANCELLED", ~draft)


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
    """The fields that decide pay; their hash is the holiday's version (3.4 §2).

    ``status``, ``portion`` and ``observance`` join only when they differ from the legacy CONFIRMED / FULL /
    ORIGINAL, so every row that existed before LH-M1 keeps its hash (SPINE §5.1; 04-holiday/01 §5.1).
    """
    out = {"id": h.id, "name": h.name, "date": h.date.isoformat(), "calendar_id": h.calendar_id,
           "holiday_type": h.holiday_type, "location": h.location, "is_optional": bool(h.is_optional)}
    status, portion = getattr(h, "status", None) or "CONFIRMED", getattr(h, "portion", None) or "FULL"
    observance = getattr(h, "observance", None) or "ORIGINAL"
    if status != "CONFIRMED":
        out["status"] = status
    if portion != "FULL":
        out["portion"] = portion
    if observance != "ORIGINAL":
        out["observance"] = observance
        observed_from = getattr(h, "observed_from_date", None)
        out["observed_from_date"] = observed_from.isoformat() if observed_from else None
    return out


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------
def calendar_lineage(db: Session, company_id: int, calendar_id: Optional[int]) -> List[int]:
    """Every calendar whose rows can reach ``calendar_id`` (itself, then its includes in priority order).

    Kept for legacy callers; holiday sets come from ``holiday_calendar_service.flatten`` (includes minus exclusions).
    """
    from app.services import holiday_calendar_service

    return holiday_calendar_service.lineage_ids(db, company_id, calendar_id)


def holidays_by_calendar_date(db: Session, company_id: int,
                              calendar_dates: Dict[int, Sequence[date]]) -> Dict[date, List[Holiday]]:
    """Holidays per date for ``{calendar_id: dates resolved to it}`` through the single flattening resolver."""
    from app.services.holiday_calendar_service import Flattener

    flattener = Flattener(db, company_id)
    result: Dict[date, List[Holiday]] = defaultdict(list)
    for calendar_id, days in calendar_dates.items():
        for day, items in flattener.holidays_on(calendar_id, days).items():
            result[day].extend(items)
    return result


def branch_holidays(db: Session, company_id: int, branch: Optional[Branch], start: date, end: date) -> List[Holiday]:
    """Holidays observed at ``branch`` between ``start`` and ``end`` (its flattened calendar, else the legacy rule)."""
    query = db.query(Holiday).filter(Holiday.company_id == company_id, Holiday.date >= start, Holiday.date <= end,
                                     counting_clause())
    if branch is not None and branch.holiday_calendar_id is not None:
        from app.services.holiday_calendar_service import Flattener

        ids = sorted(Flattener(db, company_id).flatten(branch.holiday_calendar_id, start, end).ids())
        if not ids:
            return []
        query = query.filter(Holiday.id.in_(ids))
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


def _resolved_holiday_ids(db: Session, company_id: int, employee_id: int, year: Optional[int]) -> Optional[set]:
    """Ids of the holidays of every calendar the employee resolves to on each date of ``year``; None without one."""
    from app.payroll.services.time_inputs import _covering, _nearest, primary_assignments
    from app.services.holiday_assignments import WorkWeekResolver
    from app.services.holiday_calendar_service import Flattener

    year = year or clock.company_today(db, company_id).year
    rows = primary_assignments(db, company_id, employee_id)
    if not rows:
        return None
    resolver = WorkWeekResolver(db, company_id)
    resolver.prime_shift_criterion([employee_id], date(year, 1, 1), date(year, 12, 31))
    by_calendar: Dict[int, List[date]] = defaultdict(list)
    day = date(year, 1, 1)
    while day.year == year:
        calendar_id, _ = resolver.calendar_on(_covering(rows, day) or _nearest(rows, day), day)
        if calendar_id:
            by_calendar[calendar_id].append(day)
        day = date.fromordinal(day.toordinal() + 1)
    if not by_calendar:
        return None
    flattener = Flattener(db, company_id)
    found: set = set()
    for calendar_id, days in by_calendar.items():
        found |= {h.id for items in flattener.holidays_on(calendar_id, days).values() for h in items}
    return found


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
    query = db.query(Holiday).filter(Holiday.company_id == principal.company_id, counting_clause())
    if year:
        query = query.filter(extract("year", Holiday.date) == year)
    if mine and principal.employee_id:
        employee_location = db.query(Employee.location).filter(Employee.id == principal.employee_id).scalar()
        legacy = or_(Holiday.location.is_(None) & Holiday.calendar_id.is_(None), Holiday.location == employee_location)
        mine_ids = _resolved_holiday_ids(db, principal.company_id, principal.employee_id, year)
        if mine_ids is not None:
            # Each date follows the calendar resolved for it (override, criteria, effective-dated branch link), so a
            # transferred employee sees the right calendar per date segment (R-08).
            query = query.filter(or_(Holiday.id.in_(sorted(mine_ids) or [-1]), legacy))
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


LEGACY_REASON = "Changed on the Holidays page (legacy form)"


def _legacy_years(db: Session, principal: Principal, pairs: Iterable[tuple]) -> None:
    """Legacy writes keep "saved = effective" (11 §11.6 step 3, V3-042): a PUBLISHED year is amended (version + 1,
    new content hash); a year without a record becomes PUBLISHED v1; a DRAFT year stays a draft."""
    from app.models.pay_org import HolidayCalendar
    from app.services import holiday_years

    for calendar_id, year in sorted({p for p in pairs if p[0] is not None}):
        calendar = db.get(HolidayCalendar, calendar_id)
        if calendar is None:
            continue
        existed = holiday_years.get_record(db, principal.company_id, calendar_id, year) is not None
        record = holiday_years.ensure_year(db, principal.company_id, calendar, year, lock=True)
        if record.status != "PUBLISHED" or not existed:
            continue
        record.version = (record.version or 1) + 1
        record.content_hash = holiday_years.content_hash(db, principal.company_id, calendar_id, year)
        record.row_version = (record.row_version or 1) + 1
        AuditLogService.log(db, principal, "Holidays", "YEAR_AMENDED",
                            f"{calendar.code} {year}: amended to v{record.version}", "holiday_calendar", calendar.id,
                            details={"year": year, "version": record.version, "reason": LEGACY_REASON})


def _amending(db: Session) -> None:
    from sqlalchemy import text

    db.execute(text("SET LOCAL app.holiday_amendment = 'on'"))


def _check_location(db: Session, company_id: int, location: Optional[str]) -> None:
    """LH-HOL-27: with ``holiday.legacy.unknown_location = REFUSE`` a location that names no branch exactly is
    refused (422 UNKNOWN_REFERENCE) instead of landing on the UNMATCHED review calendar (the default, unchanged)."""
    if not location:
        return
    from app.models.org import Branch
    from app.services.holiday_years import holiday_setting

    if holiday_setting(db, company_id, "holiday.legacy.unknown_location", "UNMATCHED") != "REFUSE":
        return
    if db.query(Branch.id).filter(Branch.company_id == company_id, Branch.name == location).first() is None:
        from app.core.errors import AppError

        raise AppError(422, f"No branch is named '{location}'; pick a branch.", "UNKNOWN_REFERENCE",
                       extra={"details": {"field": "location", "value": location}})


def save(db: Session, principal: Principal, data, holiday_id: Optional[int] = None) -> Holiday:
    from app.payroll.services import org_dimensions

    cid = principal.company_id
    values = data.model_dump()
    values["location"] = values.get("location") or None
    kind = values.pop("holiday_type", None)  # BUG-22: PUBLIC / COMPANY chosen on the legacy form
    today = clock.company_today(db, cid)
    if holiday_id:
        holiday = db.query(Holiday).filter(Holiday.company_id == cid, Holiday.id == holiday_id).first()
        if holiday is None:
            raise not_found("Holiday")
        before = content(holiday)
        changed = [k for k in LOCKED_FIELDS if k in values and values[k] != getattr(holiday, k)]
        if kind and not values.get("is_optional") and kind != holiday.holiday_type:
            changed.append("holiday_type")
        if changed and (holiday.date < today or values.get("date", holiday.date) < today):
            raise conflict("A past holiday keeps its date, location and type; payroll may already have used it.",
                           "HOLIDAY_IN_PAST")
        calendar_id = holiday.calendar_id
        if "location" in changed or calendar_id is None:
            if "location" in changed:
                _check_location(db, cid, values["location"])
            calendar_id = org_dimensions.calendar_for_holiday(db, cid, values["location"], principal)
    else:
        holiday, before, changed = None, None, []
        _check_location(db, cid, values["location"])
        calendar_id = org_dimensions.calendar_for_holiday(db, cid, values["location"], principal)

    clash = db.query(Holiday.id).filter(
        Holiday.company_id == cid,
        Holiday.date == values["date"],
        func.lower(Holiday.name) == values["name"].lower(),
        func.coalesce(Holiday.calendar_id, 0) == (calendar_id or 0),
        Holiday.status != "CANCELLED",
    )
    if holiday_id:
        clash = clash.filter(Holiday.id != holiday_id)
    if clash.first():
        raise conflict("This holiday already exists on that date.")
    _amending(db)
    old_pair = (holiday.calendar_id, holiday.date.year) if holiday is not None else None
    if holiday is None:
        holiday = Holiday(company_id=cid)
        db.add(holiday)
    old_date = holiday.date
    for key, value in values.items():
        setattr(holiday, key, value)
    holiday.calendar_id = calendar_id
    if holiday.is_optional:
        holiday.holiday_type = "OPTIONAL"
    elif kind:
        holiday.holiday_type = kind
    elif holiday_id is None or "is_optional" in changed:
        # The payroll view of the same holiday: its type (3.4 §8).
        holiday.holiday_type = org_dimensions.default_holiday_type(bool(holiday.is_optional))
    db.flush()
    if before != content(holiday):
        _legacy_years(db, principal, [p for p in (old_pair, (holiday.calendar_id, holiday.date.year)) if p])
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
    from app.services import holiday_years

    _amending(db)
    if holiday.calendar_id is not None and holiday_years.state_of(
            db, principal.company_id, holiday.calendar_id, holiday.date.year) == "PUBLISHED":
        # A future row of a published year is cancelled, never deleted (01 §6.6, BUG-15).
        before = content(holiday)
        holiday.status = "CANCELLED"
        holiday.row_version = (holiday.row_version or 1) + 1
        db.flush()
        AuditLogService.log(db, principal, "Holidays", "OCCURRENCE_CANCELLED",
                            f"Cancelled holiday '{holiday.name}' (legacy delete)", "holiday", holiday.id,
                            details={"before": before, "reason": "Deleted on the Holidays page (legacy form)"})
        _legacy_years(db, principal, [(holiday.calendar_id, holiday.date.year)])
        # For payroll the cancelled row is gone exactly like a deleted one (EC-HOL-06 change type kept).
        _record_change(db, principal, holiday, "DELETED", [holiday.date])
        return
    AuditLogService.log(db, principal, "Holidays", "DELETE", f"Deleted holiday '{holiday.name}'", "holiday", holiday.id,
                        details={"before": content(holiday)})
    snapshot = SimpleNamespace(id=holiday.id, company_id=holiday.company_id, name=holiday.name, date=holiday.date,
                               calendar_id=holiday.calendar_id, holiday_type=holiday.holiday_type,
                               location=holiday.location, is_optional=holiday.is_optional)
    db.delete(holiday)
    db.flush()
    _record_change(db, principal, snapshot, "DELETED", [snapshot.date])
