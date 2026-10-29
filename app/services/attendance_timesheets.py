"""Weekly timesheet approval (AS 04-attendance/04 §12; D-AS-77; GV-AS-REG-13 subset; IG-AS-101).

An employee reviews a week of evaluated days and submits it; the approver of engine module ``timesheet`` (default
route: primary manager, ``attendance:approve``) approves, rejects or returns it in the module-neutral inbox.

* the week starts on Monday (``period_kind`` WEEK); one ``attendance_timesheet_approvals`` row per employee and week;
* submit is refused while a day of the week is not FINAL or has an open attendance request (422 ``NOT_ELIGIBLE``
  with the dates) — v2 tenants only (404 for v1, spine §15.7);
* ``content_hash`` freezes the week's evaluated days; a later day version (``reopen_on_change``, called by the
  evaluation write path) moves an APPROVED or SUBMITTED week to REOPENED and the employee submits again;
* the subject never decides their own week (engine barring); nothing is approved automatically.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core import authz
from app.core.errors import bad_request, conflict, not_found
from app.core.principal import Principal
from app.models.attendance import AttendanceDay
from app.models.attendance_requests import AttendanceRequest, AttendanceTimesheetApproval
from app.models.employee import Employee
from app.payroll.services import approvals
from app.services import attendance_requests as ar
from app.services.audit_log_service import AuditLogService

MODULE, SUBJECT = "timesheet", "TIMESHEET"


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _days(db: Session, company_id: int, employee_id: int, start: date) -> List[AttendanceDay]:
    return (db.query(AttendanceDay)
            .filter(AttendanceDay.company_id == company_id, AttendanceDay.employee_id == employee_id,
                    AttendanceDay.shift_date >= start, AttendanceDay.shift_date <= start + timedelta(days=6))
            .order_by(AttendanceDay.shift_date).all())


def _hash(days: List[AttendanceDay]) -> str:
    raw = "|".join(f"{d.shift_date.isoformat()}:{d.content_hash}" for d in days)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _employee(db: Session, principal: Principal, employee_id: Optional[int]) -> Employee:
    if employee_id is None or authz.is_self(principal, employee_id):
        return authz.own_employee(db, principal)
    return authz.ensure_employee_access(db, principal, "attendance:read", employee_id)


def week(db: Session, principal: Principal, *, employee_id: Optional[int], start: date) -> dict:
    from app.services import attendance_access

    attendance_access.require_v2(db, principal.company_id)
    employee = _employee(db, principal, employee_id)
    start = week_of(start)
    days = _days(db, principal.company_id, employee.id, start)
    row = (db.query(AttendanceTimesheetApproval)
           .filter(AttendanceTimesheetApproval.company_id == principal.company_id,
                   AttendanceTimesheetApproval.employee_id == employee.id,
                   AttendanceTimesheetApproval.period_kind == "WEEK",
                   AttendanceTimesheetApproval.period_start == start).first())
    by_date = {d.shift_date: d for d in days}
    out = []
    for i in range(7):
        d = start + timedelta(days=i)
        day = by_date.get(d)
        out.append({"date": d.isoformat(), "day_result": day.day_result if day else None,
                    "evaluation_state": day.evaluation_state if day else None,
                    "net_minutes": day.net_minutes if day else None,
                    "exceptions": list(day.exception_codes or ()) if day else []})
    total = sum((x["net_minutes"] or 0) for x in out)
    return {"employee_id": employee.id, "employee_name": employee.full_name, "week_start": start.isoformat(),
            "week_end": (start + timedelta(days=6)).isoformat(), "days": out, "net_minutes": total,
            "approval": _json(row), "own": authz.is_self(principal, employee.id)}


def _json(row: Optional[AttendanceTimesheetApproval]) -> Optional[dict]:
    if row is None:
        return None
    return {"id": row.id, "status": row.status, "approval_request_id": row.approval_request_id,
            "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None, "version": row.version,
            "row_version": row.row_version}


def submit(db: Session, principal: Principal, *, start: date) -> dict:
    from app.services import attendance_access, attendance_service

    attendance_access.require_v2(db, principal.company_id)
    if not principal.has("attendance:request"):
        from app.core.errors import forbidden

        raise forbidden()
    employee = authz.own_employee(db, principal)
    start = week_of(start)
    end = start + timedelta(days=6)
    if end >= ar.employee_today(db, employee):
        raise ar.unprocessable("A week can be submitted once it has ended.", "NOT_ELIGIBLE", {"reason": "WEEK_OPEN"})
    attendance_service.lock_employee(db, employee.id)
    days = _days(db, principal.company_id, employee.id, start)
    undecided = [d.shift_date.isoformat() for d in days if d.evaluation_state != "FINAL"]
    pending = [r.shift_date.isoformat() for r in db.query(AttendanceRequest).filter(
        AttendanceRequest.company_id == principal.company_id, AttendanceRequest.employee_id == employee.id,
        AttendanceRequest.shift_date >= start, AttendanceRequest.shift_date <= end,
        AttendanceRequest.status.in_(("PENDING", "RETURNED")))]
    if undecided or pending:
        raise ar.unprocessable("Some days of this week are not settled yet.", "NOT_ELIGIBLE",
                               {"days": sorted(set(undecided + pending))})
    row = (db.query(AttendanceTimesheetApproval)
           .filter(AttendanceTimesheetApproval.company_id == principal.company_id,
                   AttendanceTimesheetApproval.employee_id == employee.id,
                   AttendanceTimesheetApproval.period_kind == "WEEK",
                   AttendanceTimesheetApproval.period_start == start).with_for_update().first())
    if row is not None and row.status in ("SUBMITTED", "APPROVED"):
        raise conflict(f"This week is already {row.status.lower()}.", "INVALID_TRANSITION")
    if row is None:
        row = AttendanceTimesheetApproval(company_id=principal.company_id, employee_id=employee.id,
                                          period_kind="WEEK", period_start=start, period_end=end, status="OPEN")
        db.add(row)
        db.flush()
    else:
        row.version += 1
    row.content_hash = _hash(days)
    request = approvals.submit(db, principal, module=MODULE, subject_type=SUBJECT, subject_id=row.id,
                               subject_employee_id=employee.id,
                               context={"work_date": start.isoformat(), "week_start": start, "week_end": end},
                               attributes={"team_ids": ar.team_ids_on(db, principal.company_id, employee.id, start),
                                           "subject_kind": SUBJECT, "employee_id": employee.id,
                                           "work_date": start.isoformat()})
    row.status, row.approval_request_id, row.submitted_at = "SUBMITTED", request.id, ar._now()
    row.decided_at = None
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, ar.AUDIT_MODULE, "TIMESHEET_SUBMITTED",
                        f"Timesheet {start.isoformat()} submitted", "attendance_timesheet_approval", row.id)
    return week(db, principal, employee_id=None, start=start)


@approvals.on_decided(MODULE, SUBJECT)
def _decided(db: Session, principal: Optional[Principal], request) -> None:
    row = (db.query(AttendanceTimesheetApproval)
           .filter(AttendanceTimesheetApproval.company_id == request.company_id,
                   AttendanceTimesheetApproval.id == request.subject_id).with_for_update().first())
    if row is None or row.approval_request_id != request.id or row.status != "SUBMITTED":
        return
    row.status = {"APPROVED": "APPROVED", "REJECTED": "REJECTED"}.get(request.status, "OPEN")
    row.decided_at = ar._now()
    row.decided_by_user_id = principal.user_id if principal else None
    row.row_version += 1
    db.flush()


def reopen_on_change(db: Session, company_id: int, employee_id: int, day: date) -> Optional[int]:
    """A new day version inside a SUBMITTED / APPROVED week reopens it (§12 Reopen)."""
    start = week_of(day)
    row = (db.query(AttendanceTimesheetApproval)
           .filter(AttendanceTimesheetApproval.company_id == company_id,
                   AttendanceTimesheetApproval.employee_id == employee_id,
                   AttendanceTimesheetApproval.period_kind == "WEEK", AttendanceTimesheetApproval.period_start == start,
                   AttendanceTimesheetApproval.status.in_(("SUBMITTED", "APPROVED"))).first())
    if row is None or row.content_hash == _hash(_days(db, company_id, employee_id, start)):
        return None
    if row.status == "SUBMITTED":
        approvals.withdraw(db, None, module=MODULE, subject_type=SUBJECT, subject_id=row.id,
                           reason="A day of the week changed")
    row.status = "REOPENED"
    row.row_version += 1
    db.flush()
    return row.id


def get_or_404(db: Session, company_id: int, approval_id: int) -> AttendanceTimesheetApproval:
    row = db.get(AttendanceTimesheetApproval, approval_id)
    if row is None or row.company_id != company_id:
        raise not_found("Timesheet")
    return row


__all__ = ["week", "submit", "reopen_on_change", "week_of", "bad_request"]
