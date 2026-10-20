"""Attendance: self-service check-in, company kiosk, manual corrections, analytics.

The employee for self-service actions always comes from the session (never
from a request parameter), face verification happens on the server, and
kiosk matching only ever compares against faces of the caller's company.
"""

from datetime import date, datetime, time, timedelta
from typing import List, Optional

import pytz
from fastapi import status as http_status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import authz
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.attendance import Attendance
from app.models.company import Company
from app.models.employee import Department, Employee, FaceEncoding, Shift
from app.models.leave import Leave
from app.modules.face_recognition import engine as face_engine
from app.services import settings_service
from app.services.audit_log_service import AuditLogService

MIN_SESSION = timedelta(minutes=1)


def _company(db: Session, principal: Principal) -> Company:
    return db.get(Company, principal.company_id)


def _tz(company: Company):
    return pytz.timezone(company.timezone or "UTC")


def local_today(company: Company) -> date:
    return utcnow().astimezone(_tz(company)).date()


def _status_for_check_in(db: Session, company: Company, employee: Employee, when: datetime) -> str:
    shift = db.get(Shift, employee.shift_id) if employee.shift_id else None
    if shift is None or shift.is_flexible:
        return "present"
    grace = settings_service.get_value(db, company.id, "graceTime")
    hour, minute = (int(x) for x in shift.start_time.split(":"))
    local = when.astimezone(_tz(company))
    start = _tz(company).localize(datetime.combine(local.date(), time(hour, minute)))
    return "late" if local > start + timedelta(minutes=int(grace)) else "present"


def _open_record(db: Session, employee_id: int, day: date) -> Optional[Attendance]:
    return (
        db.query(Attendance)
        .filter(Attendance.employee_id == employee_id, Attendance.date == day)
        .order_by(Attendance.check_in_time.desc())
        .first()
    )


def _insert_day(db: Session, record: Attendance) -> Attendance:
    """Insert a person's record for a day; of two concurrent inserts, the second gets a 409."""
    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "uq_attendance_employee_day":
            raise
        raise conflict("Attendance for this day has already been recorded.", "ALREADY_CHECKED_IN")
    return record


def _check_in(db: Session, company: Company, employee: Employee, *, score=None, source="self", actor=None) -> Attendance:
    day = local_today(company)
    existing = _open_record(db, employee.id, day)
    if existing is not None:
        raise conflict(
            "Already checked in today." if existing.check_out_time is None else "Attendance for today is complete.",
            "ALREADY_CHECKED_IN",
        )
    now = utcnow()
    record = Attendance(
        company_id=company.id,
        employee_id=employee.id,
        check_in_time=now,
        date=day,
        shift_id=employee.shift_id,
        face_match_score=score,
        is_manual=False,
        source=source,
        status=_status_for_check_in(db, company, employee, now),
        recorded_by_user_id=actor,
    )
    return _insert_day(db, record)


def _check_out(db: Session, company: Company, employee: Employee) -> Attendance:
    record = _open_record(db, employee.id, local_today(company))
    if record is None or record.check_out_time is not None:
        raise bad_request("No active check-in found for today.", "NOT_CHECKED_IN")
    now = utcnow()
    if now - record.check_in_time < MIN_SESSION:
        raise bad_request("Please wait a minute before checking out.", "TOO_SOON")
    record.check_out_time = now
    return record


def _require_engine() -> None:
    if not face_engine.available():
        raise AppError(
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            "Face recognition is not available on this server.",
            "FACE_ENGINE_UNAVAILABLE",
        )


def _encode_or_400(image: bytes) -> str:
    _require_engine()
    try:
        return face_engine.encode(image)
    except face_engine.FaceNotDetected as exc:
        raise bad_request(str(exc), "FACE_NOT_DETECTED")


def verify_own_face(db: Session, employee: Employee, image: bytes) -> float:
    """1:1 verification of the signed-in employee against their own enrolment."""
    probe = _encode_or_400(image)
    candidates = [
        (e.employee_id, e.encoding)
        for e in db.query(FaceEncoding).filter(FaceEncoding.employee_id == employee.id).all()
    ]
    if not candidates:
        raise bad_request("Your face is not enrolled yet. Ask HR to register it.", "FACE_NOT_ENROLLED")
    match = face_engine.best_match(probe, candidates, app_settings.face_recognition_threshold)
    if match is None:
        raise AppError(http_status.HTTP_403_FORBIDDEN, "Face did not match your profile.", "FACE_MISMATCH")
    return face_engine.similarity(match[1])


def self_check(db: Session, principal: Principal, action: str, image: Optional[bytes], ip: Optional[str]) -> dict:
    if not principal.has("attendance:mark"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    if not employee.is_active:
        raise forbidden("Your employment record is inactive.")
    company = _company(db, principal)
    require_face = settings_service.get_value(db, company.id, "requireFaceForSelfCheckIn")
    score = None
    if require_face:
        if not image:
            raise bad_request("A face capture is required to mark attendance.", "FACE_REQUIRED")
        score = verify_own_face(db, employee, image)
    elif image and face_engine.available():
        score = verify_own_face(db, employee, image)

    if action == "check_in":
        record = _check_in(db, company, employee, score=score, source="self", actor=principal.user_id)
    else:
        record = _check_out(db, company, employee)
    AuditLogService.log(
        db, principal, "Attendance", action.upper(), f"{employee.full_name} {action.replace('_', ' ')}",
        "attendance", record.id, ip,
    )
    return serialize(record, employee)


def kiosk_scan(db: Session, principal: Principal, image: bytes, ip: Optional[str]) -> dict:
    """Shared kiosk: identify within this company, then check in or out."""
    if not principal.has_company_scope("attendance:kiosk"):
        raise forbidden()
    probe = _encode_or_400(image)
    candidates = [
        (e.employee_id, e.encoding)
        for e in db.query(FaceEncoding)
        .join(Employee, Employee.id == FaceEncoding.employee_id)
        .filter(
            FaceEncoding.company_id == principal.company_id,
            FaceEncoding.is_primary.is_(True),
            Employee.is_active.is_(True),
        )
        .all()
    ]
    if not candidates:
        raise bad_request("No faces are enrolled for this company yet.", "NO_ENROLMENTS")
    match = face_engine.best_match(probe, candidates, app_settings.face_recognition_threshold)
    if match is None:
        return {"success": False, "action": "unknown", "message": "Face not recognised."}
    employee = db.get(Employee, match[0])
    company = _company(db, principal)
    score = face_engine.similarity(match[1])
    record = _open_record(db, employee.id, local_today(company))
    if record is None:
        record = _check_in(db, company, employee, score=score, source="kiosk", actor=principal.user_id)
        action, message = "check_in", "Check-in successful"
    elif record.check_out_time is None:
        if utcnow() - record.check_in_time < MIN_SESSION:
            return {"success": False, "action": "wait", "employee_name": employee.full_name,
                    "message": "Please wait a minute before checking out."}
        record.check_out_time = utcnow()
        action, message = "check_out", "Check-out successful"
    else:
        return {"success": True, "action": "completed", "employee_name": employee.full_name,
                "message": "Attendance already completed today."}
    AuditLogService.log(
        db, principal, "Attendance", f"KIOSK_{action.upper()}", f"Kiosk {action.replace('_', ' ')}: {employee.full_name}",
        "attendance", record.id, ip,
    )
    return {"success": True, "action": action, "employee_name": employee.full_name, "message": message,
            "confidence": score, "time": utcnow().isoformat()}


def record_manual(db: Session, principal: Principal, data) -> Attendance:
    # Checked before the scope lookup, which would otherwise answer a manager's attempt on
    # their own attendance with a misleading 404. It only compares ids, so it reveals nothing.
    if authz.is_self(principal, data.employee_id):
        raise forbidden("You cannot correct your own attendance.")
    employee = authz.ensure_employee_access(db, principal, "attendance:regularize", data.employee_id)
    if data.check_out_time and data.check_out_time < data.check_in_time:
        raise bad_request("Check-out must be after check-in.")
    same_day = _open_record(db, employee.id, data.date)
    if data.attendance_id:
        record = db.query(Attendance).filter(
            Attendance.id == data.attendance_id, Attendance.employee_id == employee.id
        ).first()
        if record is None:
            raise not_found("Attendance record")
        if same_day is not None and same_day.id != record.id:
            raise conflict(f"{employee.full_name} already has attendance on {data.date}.", "ATTENDANCE_EXISTS")
    else:
        # One record per person per day: recording a day that already has one corrects it.
        record = same_day
    previous = _summary(record, _company(db, principal)) if record is not None else None
    if record is None:
        record = Attendance(company_id=principal.company_id, employee_id=employee.id, date=data.date)
    record.date = data.date
    record.check_in_time = data.check_in_time
    record.check_out_time = data.check_out_time
    record.status = data.status
    record.notes = data.notes
    record.is_manual = True
    record.source = "manual"
    record.recorded_by_user_id = principal.user_id
    if record.id is None:
        _insert_day(db, record)
    else:
        db.flush()
    description = f"Recorded attendance for {employee.full_name} on {data.date}"
    if previous:
        # The audit trail keeps what the correction replaced.
        description = f"Corrected attendance for {employee.full_name} on {data.date} (was {previous})"
    AuditLogService.log(db, principal, "Attendance", "REGULARIZE", description, "attendance", record.id)
    return record


def _summary(record: Attendance, company: Company) -> str:
    tz = _tz(company)

    def at(moment: Optional[datetime]) -> str:
        return moment.astimezone(tz).strftime("%H:%M") if moment else "-"

    return f"{record.status}, in {at(record.check_in_time)}, out {at(record.check_out_time)}"


def serialize(record: Attendance, employee: Optional[Employee] = None) -> dict:
    end = record.check_out_time or utcnow()
    worked = end - record.check_in_time if record.check_in_time else timedelta()
    return {
        "id": record.id,
        "employee_id": record.employee_id,
        "employee_name": employee.full_name if employee else None,
        "date": record.date.isoformat(),
        "check_in_time": record.check_in_time.isoformat() if record.check_in_time else None,
        "check_out_time": record.check_out_time.isoformat() if record.check_out_time else None,
        "working_seconds": int(worked.total_seconds()),
        "status": record.status,
        "source": record.source,
        "is_manual": record.is_manual,
        "face_match_score": record.face_match_score,
        "notes": record.notes,
    }


def my_today(db: Session, principal: Principal) -> dict:
    employee = authz.own_employee(db, principal)
    company = _company(db, principal)
    record = _open_record(db, employee.id, local_today(company))
    if record is None:
        return {"state": "not_checked_in", "record": None}
    return {
        "state": "working" if record.check_out_time is None else "completed",
        "record": serialize(record, employee),
    }


def list_records(
    db: Session,
    principal: Principal,
    start: date,
    end: date,
    employee_id: Optional[int] = None,
    department_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
):
    if (end - start).days > 366:
        raise bad_request("Choose a range of at most one year.")
    scope = authz.employee_scope(db, principal, "attendance:read")
    query = (
        db.query(Attendance, Employee)
        .join(Employee, Employee.id == Attendance.employee_id)
        .filter(
            Attendance.company_id == principal.company_id,
            Attendance.date >= start,
            Attendance.date <= end,
            scope.clause(Attendance.employee_id),
        )
    )
    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    total = query.order_by(None).count()
    rows = query.order_by(Attendance.date.desc(), Attendance.check_in_time.desc()).offset(skip).limit(limit).all()
    return total, [serialize(a, e) for a, e in rows]


def today_summary(db: Session, principal: Principal) -> dict:
    """Live board for the caller's attendance scope (company, team or self)."""
    scope = authz.employee_scope(db, principal, "attendance:read")
    company = _company(db, principal)
    today = local_today(company)
    employees_q = db.query(Employee).filter(
        Employee.company_id == principal.company_id, Employee.is_active.is_(True), scope.clause(Employee.id)
    )
    total = employees_q.count()
    rows = (
        db.query(Attendance, Employee, Department.name)
        .join(Employee, Employee.id == Attendance.employee_id)
        .outerjoin(Department, Department.id == Employee.department_id)
        .filter(
            Attendance.company_id == principal.company_id,
            Attendance.date == today,
            Employee.is_active.is_(True),
            scope.clause(Employee.id),
        )
        .order_by(Attendance.check_in_time.desc())
        .all()
    )
    on_leave = (
        db.query(func.count(func.distinct(Leave.employee_id)))
        .join(Employee, Employee.id == Leave.employee_id)
        .filter(
            Leave.company_id == principal.company_id,
            Leave.status == "approved",
            Leave.start_date <= today,
            Leave.end_date >= today,
            Employee.is_active.is_(True),
            scope.clause(Employee.id),
        )
        .scalar()
    )
    present_ids = {a.employee_id for a, _, _ in rows}
    working = sum(1 for a, _, _ in rows if a.check_out_time is None)
    late = sum(1 for a, _, _ in rows if a.status == "late")
    present = len(present_ids)
    return {
        "date": today.isoformat(),
        "total_employees": total,
        "present_today": present,
        "absent_today": max(total - present - on_leave, 0),
        "on_leave": on_leave,
        "working_now": working,
        "completed_today": present - working,
        "late_today": late,
        "attendance_percentage": round(present / total * 100, 1) if total else 0,
        "employees": [
            {**serialize(a, e), "department": dept or "-"} for a, e, dept in rows[:200]
        ],
    }


def trend(db: Session, principal: Principal, days: int = 7) -> List[dict]:
    scope = authz.employee_scope(db, principal, "attendance:read")
    company = _company(db, principal)
    today = local_today(company)
    start = today - timedelta(days=days - 1)
    total = db.query(func.count(Employee.id)).filter(
        Employee.company_id == principal.company_id, Employee.is_active.is_(True), scope.clause(Employee.id)
    ).scalar()
    counts = dict(
        db.query(Attendance.date, func.count(func.distinct(Attendance.employee_id)))
        .filter(
            Attendance.company_id == principal.company_id,
            Attendance.date >= start,
            Attendance.date <= today,
            scope.clause(Attendance.employee_id),
        )
        .group_by(Attendance.date)
        .all()
    )
    result = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        present = counts.get(day, 0)
        result.append({"date": day.isoformat(), "label": day.strftime("%d %b"), "present": present,
                       "absent": max(total - present, 0)})
    return result


def monthly_stats(db: Session, principal: Principal, employee_id: int, year: int, month: int) -> dict:
    employee = authz.ensure_employee_access(db, principal, "attendance:read", employee_id)
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    rows = db.query(Attendance.status, func.count(Attendance.id)).filter(
        Attendance.employee_id == employee.id, Attendance.date >= first, Attendance.date <= last
    ).group_by(Attendance.status).all()
    counts = {s: c for s, c in rows}
    return {
        "employee_id": employee.id,
        "year": year,
        "month": month,
        "present": counts.get("present", 0),
        "late": counts.get("late", 0),
        "half_day": counts.get("half-day", 0),
        "absent": counts.get("absent", 0),
    }


def recent_activity(db: Session, principal: Principal, limit: int = 10) -> List[dict]:
    scope = authz.employee_scope(db, principal, "attendance:read")
    rows = (
        db.query(Attendance, Employee)
        .join(Employee, Employee.id == Attendance.employee_id)
        .filter(Attendance.company_id == principal.company_id, scope.clause(Attendance.employee_id))
        .order_by(Attendance.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "employee_name": e.full_name,
            "type": "checkout" if a.check_out_time else "checkin",
            "time": (a.check_out_time or a.check_in_time).isoformat(),
            "message": f"{e.full_name} {'checked out' if a.check_out_time else 'checked in'}",
        }
        for a, e in rows
    ]


def enroll_face(db: Session, principal: Principal, employee_id: int, image: bytes, ext: str, make_primary: bool = True) -> dict:
    employee = authz.ensure_employee_access(db, principal, "face:enroll", employee_id)
    encoding = _encode_or_400(image)
    from app.core import storage

    if make_primary:
        db.query(FaceEncoding).filter(FaceEncoding.employee_id == employee.id).update(
            {FaceEncoding.is_primary: False}, synchronize_session=False
        )
    path = storage.save_bytes(principal.company_id, "faces", image, ext)
    record = FaceEncoding(
        company_id=principal.company_id,
        employee_id=employee.id,
        encoding=encoding,
        image_path=path,
        is_primary=make_primary,
    )
    db.add(record)
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "FACE_ENROLLED", f"Enrolled face for {employee.full_name}", "employee", employee.id)
    return {"id": record.id, "employee_id": employee.id, "is_primary": record.is_primary}


def face_status(db: Session, principal: Principal, employee_id: int) -> dict:
    employee = authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    rows = db.query(FaceEncoding).filter(FaceEncoding.employee_id == employee.id).all()
    return {
        "employee_id": employee.id,
        "total_encodings": len(rows),
        "primary_encoding": any(r.is_primary for r in rows),
        "engine_available": face_engine.available(),
    }


def remove_faces(db: Session, principal: Principal, employee_id: int) -> int:
    employee = authz.ensure_employee_access(db, principal, "face:enroll", employee_id)
    from app.core import storage

    rows = db.query(FaceEncoding).filter(FaceEncoding.employee_id == employee.id).all()
    for row in rows:
        storage.delete(principal.company_id, row.image_path)
        db.delete(row)
    AuditLogService.log(db, principal, "Attendance", "FACE_REMOVED", f"Removed face data for {employee.full_name}", "employee", employee.id)
    return len(rows)
