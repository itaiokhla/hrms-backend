"""Changes to approved leave (SPINE §5.2; 03-leave/03 §10): CANCEL, PARTIAL_CANCEL, MODIFY, CONVERT, RECALL.

One PENDING change per leave (``CHANGE_PENDING``; a legacy cancellation in flight counts too). CANCEL works for every
tenant (it voids the days through the same writer as the legacy cancellation); the other kinds need leave v2 because
they re-charge dates under the plan. Whether a change needs approval: CANCEL / PARTIAL_CANCEL follow the type rule's
``change.cancellation_requires_approval`` (INHERIT = ``leave.cancellation.requires_approval``: ALWAYS,
IF_STARTED_OR_WITHIN_DAYS with ``free_window_days``, NEVER); MODIFY and CONVERT always need an approver unless HR
initiates them with ``leave:change_any``; RECALL (manager or HR, never for EVENT or SENSITIVE leave) waits for the
employee's acknowledgement. Past dates may only be cancelled with ``leave:change_any``.

Applying a change bumps the request ``version``, voids / regenerates ``leave_days`` and posts the ledger difference
(``leave_postings``); every change is logged for payroll (locked periods go to retro). The approver of a change is
whoever may approve the employee's leave (never the requester).
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core import authz, concurrency
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import Leave, LeaveType
from app.models.leave_v2 import LeaveChangeRequest
from app.services import leave_access, leave_ledger, leave_plans, leave_requests
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

KINDS = ("CANCEL", "PARTIAL_CANCEL", "MODIFY", "CONVERT", "RECALL")


def change_json(row: LeaveChangeRequest) -> dict:
    return {"id": row.id, "leave_id": row.leave_id, "employee_id": row.employee_id, "kind": row.kind,
            "status": row.status, "proposed": row.proposed or {}, "reason": row.reason, "initiator": row.initiator,
            "from_version": row.from_version, "decision_note": row.decision_note,
            "units_delta": leave_ledger._num(Decimal(row.units_delta or 0)),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "applied_at": row.applied_at.isoformat() if row.applied_at else None,
            "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
            "requested_by_user_id": row.requested_by_user_id, "row_version": row.row_version}


def _pending(db: Session, leave: Leave) -> Optional[LeaveChangeRequest]:
    return db.query(LeaveChangeRequest).filter(LeaveChangeRequest.company_id == leave.company_id,
                                               LeaveChangeRequest.leave_id == leave.id,
                                               LeaveChangeRequest.status == "PENDING").first()


def _needs_approval(db: Session, leave: Leave, kind: str, today: date, employer: bool) -> bool:
    if employer and kind != "RECALL":
        return False
    if kind in ("MODIFY", "CONVERT", "RECALL"):
        return True
    rule = leave_plans.rules_for(db, leave.company_id, leave.employee_id, leave.leave_type, leave.start_date) or {}
    mode = ((rule.get("change") or {}).get("cancellation_requires_approval")) or "INHERIT"
    if mode == "INHERIT":
        mode = leave_access.setting(db, leave.company_id, "leave.cancellation.requires_approval", default="ALWAYS")
    if mode == "NEVER":
        return False
    if mode == "IF_STARTED_OR_WITHIN_DAYS":
        window = int(leave_access.setting(db, leave.company_id, "leave.cancellation.free_window_days", default=0) or 0)
        return leave.start_date <= today + timedelta(days=window)
    return True


def _dates_of(leave: Leave) -> List[date]:
    return [leave.start_date + timedelta(days=n) for n in range((leave.end_date - leave.start_date).days + 1)]


def request_change(db: Session, principal: Principal, leave_id: int, *, kind: str, proposed: dict,
                   reason: Optional[str]) -> LeaveChangeRequest:
    if kind not in KINDS:
        raise bad_request("Unknown change.", "INVALID_CHANGE")
    leave = leave_requests.load(db, principal, leave_id, lock=True)
    own = leave.employee_id == principal.employee_id
    editor = leave_requests._can_edit_for(db, principal, leave)
    employer = not own and principal.has("leave:change_any") and authz.can_access_employee(
        db, principal, "leave:change_any", leave.employee_id)
    approver = not own and principal.has("leave:approve") and authz.can_access_employee(
        db, principal, "leave:approve", leave.employee_id)
    if kind == "RECALL":
        if not (employer or approver):
            raise forbidden("Recalling an employee needs approval rights over them.")
    elif not (editor or employer):
        if not authz.can_access_employee(db, principal, "leave:read", leave.employee_id):
            raise not_found("Leave request")
        raise forbidden("You cannot change this leave.")
    if kind != "CANCEL":
        leave_access.require_v2(db, principal.company_id)
    if leave.status != "approved":
        raise AppError(409, "Only approved leave can be changed; withdraw a pending request instead.",
                       "INVALID_TRANSITION")
    if _pending(db, leave) is not None or leave.cancellation_status == "pending":
        raise AppError(409, "A change of this leave is already waiting for a decision.", "CHANGE_PENDING")
    employee = db.get(Employee, leave.employee_id)
    concurrency.lock_employees(db, [employee.id])
    today = leave_requests.local_today(db, employee)
    lt = db.query(LeaveType).filter(LeaveType.company_id == leave.company_id, LeaveType.code == leave.leave_type).first()
    proposed = dict(proposed or {})
    if kind in ("CANCEL", "PARTIAL_CANCEL", "RECALL"):
        if kind == "CANCEL":
            dates = _dates_of(leave)
        elif kind == "RECALL":
            if (lt is not None and (lt.kind == "EVENT" or lt.sensitivity == "SENSITIVE")) or leave.sensitivity == "SENSITIVE":
                raise AppError(409, "This leave cannot be recalled.", "RECALL_NOT_ALLOWED")
            rule = leave_plans.rules_for(db, leave.company_id, leave.employee_id, leave.leave_type, leave.start_date) or {}
            if not (rule.get("change") or {}).get("recall_allowed", True):
                raise AppError(409, "This leave cannot be recalled.", "RECALL_NOT_ALLOWED")
            start = date.fromisoformat(proposed.get("from_date") or today.isoformat())
            if start < today or start > leave.end_date:
                raise bad_request("Recall from today or a later day of the leave.", "INVALID_RANGE")
            if not (reason or "").strip():
                raise bad_request("Give a reason for the recall.", "REASON_REQUIRED")
            dates = [d for d in _dates_of(leave) if d >= max(start, leave.start_date)]
            proposed["from_date"] = max(start, leave.start_date).isoformat()
        else:
            dates = sorted({date.fromisoformat(d) for d in proposed.get("dates") or []})
            if not dates or any(d < leave.start_date or d > leave.end_date for d in dates):
                raise bad_request("Choose dates inside the leave.", "INVALID_RANGE")
            if set(dates) == set(_dates_of(leave)):
                kind = "CANCEL"
        if any(d <= today for d in dates) and not employer and kind != "RECALL":
            raise AppError(422, "Leave that has already started can only be changed by HR.",
                           "PAST_DATE_CHANGE_NOT_ALLOWED")
        proposed["dates"] = [d.isoformat() for d in dates]
    elif kind in ("MODIFY", "CONVERT"):
        data = leave_requests.StoredRequest(leave)
        data.start_date = date.fromisoformat(proposed.get("start_date") or leave.start_date.isoformat())
        data.end_date = date.fromisoformat(proposed.get("end_date") or leave.end_date.isoformat())
        data.start_portion = proposed.get("start_portion") or leave.start_portion
        data.end_portion = proposed.get("end_portion") or leave.end_portion
        if kind == "CONVERT":
            if not proposed.get("leave_type") or proposed["leave_type"] == leave.leave_type:
                raise bad_request("Choose the leave type to convert to.", "INVALID_LEAVE_TYPE")
            data.leave_type = proposed["leave_type"]
            if proposed.get("event_id") is not None:  # CONVERT into an EVENT type names the (OPEN) event
                try:
                    data.event_id = int(proposed["event_id"])
                except (TypeError, ValueError):
                    raise bad_request("Choose the event of the new leave type.", "EVENT_REQUIRED")
        if (data.start_date < today or leave.start_date < today) and not employer:
            raise AppError(422, "Leave that has already started can only be changed by HR.",
                           "PAST_DATE_CHANGE_NOT_ALLOWED")
        pv = leave_requests.validate(db, principal, data, mode="RECORD" if employer else "APPLY", existing=leave,
                                     employee=employee, source=leave.source)
        pv.raise_first_error()
        proposed.update(start_date=data.start_date.isoformat(), end_date=data.end_date.isoformat(),
                        start_portion=data.start_portion, end_portion=data.end_portion, leave_type=data.leave_type,
                        units=str(pv.charged), event_id=pv.event.id if pv.event is not None else None)
    row = LeaveChangeRequest(company_id=leave.company_id, leave_id=leave.id, employee_id=leave.employee_id, kind=kind,
                             status="PENDING", from_version=leave.version, proposed=proposed,
                             reason=(reason or "").strip() or None, requested_by_user_id=principal.user_id,
                             initiator="EMPLOYER" if (employer or kind == "RECALL") and not own else "EMPLOYEE")
    db.add(row)
    db.flush()
    leave_requests._message(db, leave, f"Change requested: {kind.replace('_', ' ').lower()}.", author=principal.user_id,
                            details={"change_id": row.id})
    if kind == "RECALL":
        if employee.user_id:
            NotificationService.create(db, employee.user_id, "You are asked to return from leave",
                                       f"Please acknowledge the recall from {proposed['from_date']}.", "leave",
                                       f"/leaves/requests?modal=leave&id={leave.id}")
    elif not _needs_approval(db, leave, kind, today, employer=row.initiator == "EMPLOYER"):
        apply_change(db, principal, row, auto=True)
    else:
        _notify_approvers(db, leave, employee, kind)
    AuditLogService.log(db, principal, "Leave", "CHANGE_REQUESTED", f"{kind} requested for leave #{leave.id}", "leave",
                        leave.id, details={"change_id": row.id, "kind": kind})
    return row


def _notify_approvers(db: Session, leave: Leave, employee: Employee, kind: str) -> None:
    from app.services import leave_service

    manager = leave_service._active_employee(db, employee.manager_id)
    if manager is not None and manager.user_id:
        NotificationService.create(db, manager.user_id, "Leave change awaiting your approval",
                                   f"{employee.full_name} asked to {kind.replace('_', ' ').lower()} approved leave.",
                                   "leave", "/leaves/approvals")


def can_decide(db: Session, principal: Principal, row: LeaveChangeRequest) -> bool:
    if row.kind == "RECALL" or row.employee_id == principal.employee_id or row.requested_by_user_id == principal.user_id:
        return False
    return principal.has("leave:approve") and authz.can_access_employee(db, principal, "leave:approve", row.employee_id)


def _load(db: Session, principal: Principal, change_id: int) -> LeaveChangeRequest:
    row = db.query(LeaveChangeRequest).filter(LeaveChangeRequest.company_id == principal.company_id,
                                              LeaveChangeRequest.id == change_id).with_for_update().first()
    if row is None:
        raise not_found("Change request")
    return row


def decide(db: Session, principal: Principal, change_id: int, approve: bool, note: Optional[str]) -> LeaveChangeRequest:
    row = _load(db, principal, change_id)
    if not can_decide(db, principal, row):
        if row.employee_id == principal.employee_id or row.requested_by_user_id == principal.user_id:
            raise forbidden("You cannot decide your own change.")
        if not authz.can_access_employee(db, principal, "leave:read", row.employee_id):
            raise not_found("Change request")
        raise forbidden("This change is not awaiting your approval.")
    if row.status != "PENDING":
        raise AppError(409, "This change was already decided.", "INVALID_TRANSITION")
    if not approve and not (note or "").strip() and leave_access.setting(
            db, row.company_id, "leave.approval.reject_reason_required", default=True):
        raise bad_request("Give a reason for rejecting the change.", "REASON_REQUIRED")
    row.decided_by_user_id, row.decided_at, row.decision_note = principal.user_id, utcnow(), (note or "").strip() or None
    if approve:
        apply_change(db, principal, row)
    else:
        row.status = "REJECTED"
        row.row_version += 1
        leave = db.get(Leave, row.leave_id)
        leave_requests._message(db, leave, f"Change rejected{': ' + row.decision_note if row.decision_note else '.'}",
                                kind="DECISION_NOTE", author=principal.user_id)
    _notify_employee(db, row)
    AuditLogService.log(db, principal, "Leave", "CHANGE_" + ("APPROVED" if approve else "REJECTED"),
                        f"{row.kind} of leave #{row.leave_id} {'approved' if approve else 'rejected'}", "leave",
                        row.leave_id, details={"change_id": row.id})
    db.flush()
    return row


def _notify_employee(db: Session, row: LeaveChangeRequest) -> None:
    employee = db.get(Employee, row.employee_id)
    if employee is not None and employee.user_id:
        NotificationService.create(db, employee.user_id, f"Leave change {row.status.lower()}",
                                   f"Your request to {row.kind.replace('_', ' ').lower()} leave was {row.status.lower()}.",
                                   "leave", f"/leaves/requests?modal=leave&id={row.leave_id}")


def acknowledge(db: Session, principal: Principal, change_id: int) -> LeaveChangeRequest:
    row = _load(db, principal, change_id)
    if row.employee_id != principal.employee_id or row.kind != "RECALL":
        raise not_found("Change request")
    if row.status != "PENDING":
        raise AppError(409, "This recall was already handled.", "INVALID_TRANSITION")
    row.acknowledged_at = utcnow()
    apply_change(db, principal, row)
    return row


def withdraw(db: Session, principal: Principal, change_id: int) -> LeaveChangeRequest:
    row = _load(db, principal, change_id)
    if row.requested_by_user_id != principal.user_id:
        raise not_found("Change request")
    if row.status != "PENDING":
        raise AppError(409, "Only a pending change can be withdrawn.", "INVALID_TRANSITION")
    row.status = "WITHDRAWN"
    row.row_version += 1
    db.flush()
    return row


def apply_change(db: Session, principal: Principal, row: LeaveChangeRequest, *, auto: bool = False) -> None:
    """Apply an approved (or approval-free) change atomically."""
    from app.payroll.services import time_inputs

    leave = db.query(Leave).filter(Leave.company_id == row.company_id, Leave.id == row.leave_id).with_for_update().one()
    if leave.version != row.from_version:
        raise AppError(409, "The leave changed after this change was requested.", "PREVIEW_CHANGED")
    concurrency.lock_employees(db, [leave.employee_id])
    before = Decimal(leave.number_of_days or 0)
    old_event_id = leave.event_id
    v2 = leave_access.v2_enabled(db, leave.company_id)
    if row.kind == "CANCEL":
        leave.version += 1
        time_inputs.void_leave_days(db, leave, "CANCELLED", principal.user_id)
        leave.status, leave.cancelled_at = "cancelled", utcnow()
        for sibling in db.query(Leave).filter(Leave.company_id == leave.company_id, Leave.parent_leave_id == leave.id,
                                              Leave.status == "approved").all():
            time_inputs.void_leave_days(db, sibling, "CANCELLED", principal.user_id)
            sibling.status, sibling.cancelled_at = "cancelled", utcnow()
    elif row.kind in ("PARTIAL_CANCEL", "RECALL"):
        from app.services import leave_postings

        dates = {date.fromisoformat(d) for d in row.proposed.get("dates") or []}
        leave.version += 1
        leave_postings.void(db, leave, "PARTIAL_CANCEL" if row.kind == "PARTIAL_CANCEL" else "RECALLED",
                            principal.user_id, dates=dates)
        active = leave_postings._active_rows(db, leave)
        leave.number_of_days = sum((Decimal(r.units) for r in active if r.counts_against_balance), Decimal(0))
        if not active:
            leave.status, leave.cancelled_at = "cancelled", utcnow()
    elif row.kind in ("MODIFY", "CONVERT"):
        from app.services import leave_postings

        if not v2:
            raise AppError(409, "Changing dates needs leave v2.", "INVALID_TRANSITION")
        leave.version += 1
        p = row.proposed
        leave.start_date, leave.end_date = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
        leave.start_portion, leave.end_portion = p.get("start_portion") or "FULL", p.get("end_portion") or "FULL"
        if row.kind == "CONVERT":
            leave.leave_type = p["leave_type"]
            new_type = db.query(LeaveType).filter(LeaveType.company_id == leave.company_id,
                                                  LeaveType.code == leave.leave_type).first()
            leave.sensitivity = new_type.sensitivity if new_type else leave.sensitivity
            leave.event_id = p.get("event_id") if new_type is not None and new_type.kind == "EVENT" else None
        leave_postings.regenerate(db, leave, principal.user_id, note=row.kind.title())
        if old_event_id is not None and old_event_id != leave.event_id:  # units go back to the old event
            from app.services import leave_events

            leave_events.sync_used(db, leave.company_id, old_event_id)
    row.status = "APPROVED"
    row.applied_at = utcnow()
    row.units_delta = Decimal(leave.number_of_days or 0) - before
    row.row_version += 1
    if auto:
        row.decided_at = utcnow()
    leave.row_version = (leave.row_version or 1) + 1
    leave_requests._message(db, leave, f"{row.kind.replace('_', ' ').title()} applied"
                                       f"{' automatically' if auto else ''}.", details={"change_id": row.id})
    db.flush()


def list_changes(db: Session, principal: Principal, leave_id: int) -> List[dict]:
    leave = leave_requests.load(db, principal, leave_id)
    if leave.employee_id != principal.employee_id and not authz.can_access_employee(db, principal, "leave:read",
                                                                                   leave.employee_id):
        raise not_found("Leave request")
    rows = db.query(LeaveChangeRequest).filter(LeaveChangeRequest.company_id == leave.company_id,
                                               LeaveChangeRequest.leave_id == leave.id).order_by(LeaveChangeRequest.id).all()
    return [change_json(r) | {"can_decide": r.status == "PENDING" and can_decide(db, principal, r),
                              "can_withdraw": r.status == "PENDING" and r.requested_by_user_id == principal.user_id,
                              "can_acknowledge": r.status == "PENDING" and r.kind == "RECALL"
                              and r.employee_id == principal.employee_id} for r in rows]


def pending_for_approver(db: Session, principal: Principal) -> List[dict]:
    if not principal.has("leave:approve"):
        return []
    rows = (db.query(LeaveChangeRequest, Leave, Employee)
            .join(Leave, (Leave.company_id == LeaveChangeRequest.company_id) & (Leave.id == LeaveChangeRequest.leave_id))
            .join(Employee, (Employee.company_id == Leave.company_id) & (Employee.id == Leave.employee_id))
            .filter(LeaveChangeRequest.company_id == principal.company_id, LeaveChangeRequest.status == "PENDING")
            .order_by(LeaveChangeRequest.id).limit(200).all())
    out = []
    types = {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == principal.company_id)}
    for row, leave, person in rows:
        if can_decide(db, principal, row):
            lt = types.get(leave.leave_type)
            masked = leave_requests.masked_type(db, principal, leave, lt)
            out.append(change_json(row) | {"employee_name": person.full_name,
                                           "leave_type": "protected" if masked else leave.leave_type,
                                           "leave_type_name": "Protected leave" if masked else (
                                               lt.name if lt else leave.leave_type),
                                           "color_token": "leave-neutral" if masked else (
                                               lt.color_token if lt else None),
                                           "start_date": leave.start_date.isoformat(),
                                           "end_date": leave.end_date.isoformat(),
                                           "number_of_days": leave_ledger._num(Decimal(leave.number_of_days))})
    return out
