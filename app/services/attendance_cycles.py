"""Attendance period close (AS-M4; 04-attendance/04 §14.3; GV-AS-REG-14).

An attendance-side "reviewed and closed" mark per legal entity (optionally branch and pay group) and cycle. It closes
**self-service**, not payroll: SELF requests and SELF on-duty for dates of a CLOSED cycle answer 422
``ATTENDANCE_PERIOD_CLOSED``; ON_BEHALF and DIRECT continue. Reopening needs step-up and a reason.
Permission ``attendance:close_period`` at COMPANY, or LEGAL_ENTITY / BRANCH / PAY_GROUP targets covering the unit.
"""

from __future__ import annotations


from typing import Any, List, Mapping, Optional

from sqlalchemy.orm import Session

from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.attendance_requests import AttendancePeriodClose, AttendanceRequest
from app.models.time_inputs import OnDutyRequest
from app.services import attendance_requests as ar
from app.services.audit_log_service import AuditLogService

CODE = "attendance:close_period"


def _covers(principal: Principal, row_or_data) -> bool:
    if not principal.has(CODE):
        return False
    if principal.has_company_scope(CODE):
        return True
    get = (lambda k: row_or_data.get(k)) if isinstance(row_or_data, Mapping) else (lambda k: getattr(row_or_data, k))
    for scope, key in (("LEGAL_ENTITY", "legal_entity_id"), ("BRANCH", "branch_id"), ("PAY_GROUP", "pay_group_id")):
        value = get(key)
        if value is not None and value in principal.scope_targets(CODE, scope):
            return True
    return False


def close_json(row: AttendancePeriodClose) -> dict:
    return {"id": row.id, "legal_entity_id": row.legal_entity_id, "branch_id": row.branch_id,
            "pay_group_id": row.pay_group_id, "cycle_start": row.cycle_start.isoformat(),
            "cycle_end": row.cycle_end.isoformat(), "status": row.status,
            "open_exception_count": row.open_exception_count, "closed_by_user_id": row.closed_by_user_id,
            "closed_at": row.closed_at.isoformat() if row.closed_at else None,
            "reopened_at": row.reopened_at.isoformat() if row.reopened_at else None,
            "reopen_reason": row.reopen_reason, "row_version": row.row_version}


def list_closes(db: Session, principal: Principal) -> List[dict]:
    if not principal.has(CODE):
        raise forbidden()
    rows = (db.query(AttendancePeriodClose).filter(AttendancePeriodClose.company_id == principal.company_id)
            .order_by(AttendancePeriodClose.cycle_start.desc(), AttendancePeriodClose.id.desc()).limit(200).all())
    return [close_json(r) for r in rows if _covers(principal, r)]


def create(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    from app.services import attendance_access

    attendance_access.require_v2(db, principal.company_id)
    if not data.get("legal_entity_id"):
        raise bad_request("Choose the legal entity.", "INVALID_FIELDS")
    if not _covers(principal, data):
        raise forbidden()
    start, end = ar._as_date(data.get("cycle_start")), ar._as_date(data.get("cycle_end"))
    if end < start:
        raise bad_request("The cycle ends before it starts.", "INVALID_RANGE")
    existing = db.query(AttendancePeriodClose).filter(
        AttendancePeriodClose.company_id == principal.company_id,
        AttendancePeriodClose.legal_entity_id == data["legal_entity_id"],
        AttendancePeriodClose.branch_id.is_(data.get("branch_id")) if data.get("branch_id") is None
        else AttendancePeriodClose.branch_id == data["branch_id"],
        AttendancePeriodClose.pay_group_id.is_(data.get("pay_group_id")) if data.get("pay_group_id") is None
        else AttendancePeriodClose.pay_group_id == data["pay_group_id"],
        AttendancePeriodClose.cycle_start == start).first()
    if existing is not None:
        return close_json(existing)
    row = AttendancePeriodClose(company_id=principal.company_id, legal_entity_id=data["legal_entity_id"],
                                branch_id=data.get("branch_id"), pay_group_id=data.get("pay_group_id"),
                                cycle_start=start, cycle_end=end, status="OPEN")
    db.add(row)
    db.flush()
    return close_json(row)


def _load(db: Session, principal: Principal, close_id: int) -> AttendancePeriodClose:
    row = (db.query(AttendancePeriodClose)
           .filter(AttendancePeriodClose.company_id == principal.company_id, AttendancePeriodClose.id == close_id)
           .with_for_update().first())
    if row is None or not _covers(principal, row):
        raise not_found("Attendance period")
    return row


def open_items(db: Session, company_id: int, row: AttendancePeriodClose) -> dict:
    """Pending requests and on-duty of the unit's people in the cycle (the close Modal lists them first)."""
    from app.models.org import EmployeeAssignment

    people = db.query(EmployeeAssignment.employee_id).filter(
        EmployeeAssignment.company_id == company_id, EmployeeAssignment.legal_entity_id == row.legal_entity_id,
        EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= row.cycle_end)
    if row.branch_id is not None:
        people = people.filter(EmployeeAssignment.branch_id == row.branch_id)
    if row.pay_group_id is not None:
        people = people.filter(EmployeeAssignment.pay_group_id == row.pay_group_id)
    ids = sorted({e for (e,) in people.all()})
    requests = db.query(AttendanceRequest.id).filter(
        AttendanceRequest.company_id == company_id, AttendanceRequest.employee_id.in_(ids or [0]),
        AttendanceRequest.shift_date >= row.cycle_start, AttendanceRequest.shift_date <= row.cycle_end,
        AttendanceRequest.status.in_(("PENDING", "RETURNED"))).count()
    on_duty = db.query(OnDutyRequest.id).filter(
        OnDutyRequest.company_id == company_id, OnDutyRequest.employee_id.in_(ids or [0]),
        OnDutyRequest.date >= row.cycle_start, OnDutyRequest.date <= row.cycle_end,
        OnDutyRequest.status == "PENDING").count()
    return {"pending_requests": requests, "pending_on_duty": on_duty, "total": requests + on_duty}


def close(db: Session, principal: Principal, close_id: int, *, acknowledge_open_items: bool = False,
          row_version: Optional[int] = None) -> dict:
    row = _load(db, principal, close_id)
    if row_version is not None and row_version != row.row_version:
        raise conflict("This period changed since you loaded it.", "ROW_VERSION_CONFLICT")
    if row.status == "CLOSED":
        raise conflict("This period is already closed.", "INVALID_TRANSITION")
    items = open_items(db, principal.company_id, row)
    if items["total"] and not acknowledge_open_items:
        raise ar.unprocessable(f"{items['total']} item(s) are still open; acknowledge them to close anyway.",
                               "OPEN_ITEMS", items)
    row.status, row.open_exception_count = "CLOSED", items["total"]
    row.closed_by_user_id, row.closed_at = principal.user_id, ar._now()
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, ar.AUDIT_MODULE, "PERIOD_CLOSED",
                        f"Closed attendance {row.cycle_start.isoformat()} to {row.cycle_end.isoformat()}",
                        "attendance_period_close", row.id, details={**close_json(row), "open_items": items})
    return close_json(row)


def reopen(db: Session, principal: Principal, close_id: int, *, reason: str) -> dict:
    from app.payroll.services import step_up

    row = _load(db, principal, close_id)
    if row.status != "CLOSED":
        raise conflict("Only a closed period can be reopened.", "INVALID_TRANSITION")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise bad_request("Give a reason for reopening.", "REASON_REQUIRED")
    step_up.ensure_step_up(db, principal)
    row.status, row.reopened_by_user_id, row.reopened_at, row.reopen_reason = "REOPENED", principal.user_id, \
        ar._now(), reason[:500]
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, ar.AUDIT_MODULE, "PERIOD_REOPENED",
                        f"Reopened attendance {row.cycle_start.isoformat()} to {row.cycle_end.isoformat()}",
                        "attendance_period_close", row.id, details=close_json(row))
    return close_json(row)


__all__ = ["create", "close", "reopen", "list_closes", "open_items"]
