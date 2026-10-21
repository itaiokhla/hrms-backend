from datetime import date
from typing import List, Optional

from sqlalchemy import extract, func, or_
from sqlalchemy.orm import Session

from app.core.errors import conflict, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.holiday import Holiday
from app.services.audit_log_service import AuditLogService


def serialize(h: Holiday) -> dict:
    return {
        "id": h.id,
        "name": h.name,
        "date": h.date.isoformat(),
        "location": h.location,
        "is_optional": h.is_optional,
        "description": h.description,
    }


def list_holidays(db: Session, principal: Principal, year: Optional[int], location: Optional[str], mine: bool) -> List[dict]:
    query = db.query(Holiday).filter(Holiday.company_id == principal.company_id)
    if year:
        query = query.filter(extract("year", Holiday.date) == year)
    if mine and principal.employee_id:
        employee_location = db.query(Employee.location).filter(Employee.id == principal.employee_id).scalar()
        query = query.filter(or_(Holiday.location.is_(None), Holiday.location == employee_location))
    elif location:
        query = query.filter(or_(Holiday.location.is_(None), Holiday.location == location))
    return [serialize(h) for h in query.order_by(Holiday.date).all()]


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


def save(db: Session, principal: Principal, data, holiday_id: Optional[int] = None) -> Holiday:
    clash = db.query(Holiday.id).filter(
        Holiday.company_id == principal.company_id,
        Holiday.date == data.date,
        func.lower(Holiday.name) == data.name.lower(),
    )
    if holiday_id:
        clash = clash.filter(Holiday.id != holiday_id)
    if clash.first():
        raise conflict("This holiday already exists on that date.")
    if holiday_id:
        holiday = db.query(Holiday).filter(Holiday.company_id == principal.company_id, Holiday.id == holiday_id).first()
        if holiday is None:
            raise not_found("Holiday")
    else:
        holiday = Holiday(company_id=principal.company_id)
        db.add(holiday)
    for key, value in data.model_dump().items():
        setattr(holiday, key, value or None if key == "location" else value)
    if not holiday_id:
        from app.payroll.services import org_dimensions

        # The payroll view of the same holiday: its branch's (or the company) calendar and its type (3.4 §8).
        holiday.holiday_type = org_dimensions.default_holiday_type(bool(holiday.is_optional))
        holiday.calendar_id = org_dimensions.calendar_for_holiday(db, principal.company_id, holiday.location, principal)
    db.flush()
    AuditLogService.log(db, principal, "Holidays", "SAVE", f"Saved holiday '{holiday.name}' on {holiday.date}", "holiday", holiday.id)
    return holiday


def delete(db: Session, principal: Principal, holiday_id: int) -> None:
    holiday = db.query(Holiday).filter(Holiday.company_id == principal.company_id, Holiday.id == holiday_id).first()
    if holiday is None:
        raise not_found("Holiday")
    if holiday.date < date.today():
        raise conflict("Past holidays are kept for payroll history and cannot be deleted.")
    AuditLogService.log(db, principal, "Holidays", "DELETE", f"Deleted holiday '{holiday.name}'", "holiday", holiday.id)
    db.delete(holiday)
