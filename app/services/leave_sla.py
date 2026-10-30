"""Hourly leave housekeeping (job ``leave.sla_scan``; SPINE §12, 03-leave/04 SLA, 03-leave/03 T11, 03-leave/05).

* A PENDING request waiting longer than ``leave.approval.sla_hours`` at one step reminds its approver once per step;
  with ``leave.approval.sla_action = ESCALATE`` the approver's manager is told as well (the step itself is unchanged:
  hierarchy steps resolve their approver when evaluated, and company-wide approvers may override).
* A RETURNED request not resubmitted within ``leave.approval.returned_expiry_days`` is withdrawn by the system.
* An OPEN document request past its due date becomes OVERDUE and the employee and HR are told once.
Each effect is idempotent (flags on the request, statuses on the document request), so reruns change nothing.
"""

from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session, selectinload

from app.core import clock
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.leave_v2 import LeaveDocumentRequest, LeaveMessage
from app.services import leave_access
from app.services.notification_service import NotificationService


def _waiting_since(leave: Leave):
    acted = [s.acted_at for s in leave.approvals if s.acted_at is not None]
    return max([leave.submitted_at or leave.created_at] + acted)


def scan(db: Session, company_id: int) -> dict:
    from app.services import leave_service

    now = utcnow()
    today = clock.company_today(db, company_id)
    sla = int(leave_access.setting(db, company_id, "leave.approval.sla_hours", default=48) or 48)
    action = leave_access.setting(db, company_id, "leave.approval.sla_action", default="REMIND")
    reminded = escalated = expired = overdue = 0
    pending = (db.query(Leave).options(selectinload(Leave.approvals))
               .filter(Leave.company_id == company_id, Leave.status == "pending").all())
    for leave in pending:
        step = leave_service._current_step(leave)
        if step is None or now - _waiting_since(leave) < timedelta(hours=sla):
            continue
        marker = f"SLA_REMINDED_{step.step_order}"
        flags = list(leave.flags or [])
        if marker in flags:
            continue
        requester = db.get(Employee, leave.employee_id)
        title = "Leave request waiting for your decision"
        message = f"{requester.full_name}'s leave request has waited more than {sla} hours."
        if step.approver_type == "role":
            for user_id in leave_service.role_member_user_ids(db, step.role_id, exclude_user_id=requester.user_id)[:25]:
                NotificationService.create(db, user_id, title, message, "leave", "/leaves/approvals")
        else:
            approver = leave_service.resolve_step_employee(db, requester, step)
            if approver is not None and approver.user_id:
                NotificationService.create(db, approver.user_id, title, message, "leave", "/leaves/approvals")
            if action == "ESCALATE" and approver is not None:
                boss = leave_service._active_employee(db, approver.manager_id)
                if boss is not None and boss.user_id:
                    NotificationService.create(db, boss.user_id, "Leave approval overdue",
                                               f"{approver.full_name} has not decided {requester.full_name}'s leave "
                                               f"request for more than {sla} hours.", "leave", "/leaves/approvals")
                    escalated += 1
        leave.flags = flags + [marker]
        db.add(LeaveMessage(company_id=company_id, leave_id=leave.id, kind="SYSTEM_EVENT",
                            body=f"Reminder sent: waiting more than {sla} hours at step {step.step_order}."))
        reminded += 1
    days = int(leave_access.setting(db, company_id, "leave.approval.returned_expiry_days", default=14) or 14)
    from app.services import leave_requests, org_structure_service

    for leave in db.query(Leave).filter(Leave.company_id == company_id, Leave.status == "returned",
                                        Leave.returned_at.isnot(None)).all():
        requester = db.get(Employee, leave.employee_id)
        # 03-leave/03 T11 (LH-REQ-21): expiry_date = min(returned + N days, max(start, returned) + 7) in the
        # employee's local dates; withdrawn once the employee's local today is after it.
        returned_on = org_structure_service.employee_local_date(db, requester, leave.returned_at)
        expiry = min(returned_on + timedelta(days=days), max(leave.start_date, returned_on) + timedelta(days=7))
        if leave_requests.local_today(db, requester) <= expiry:
            continue
        leave_requests._close(db, leave, "withdrawn")  # its unpaid sibling and open steps too
        db.add(LeaveMessage(company_id=company_id, leave_id=leave.id, kind="SYSTEM_EVENT",
                            body=f"Withdrawn automatically: not resubmitted by {expiry:%d %b %Y}."))
        if requester.user_id:
            NotificationService.create(db, requester.user_id, "Leave request withdrawn",
                                       "Your returned leave request expired without being resubmitted.", "leave",
                                       "/leaves/requests")
        expired += 1
    for request in db.query(LeaveDocumentRequest).filter(LeaveDocumentRequest.company_id == company_id,
                                                         LeaveDocumentRequest.status == "OPEN",
                                                         LeaveDocumentRequest.due_date < today).all():
        request.status = "OVERDUE"
        leave = db.get(Leave, request.leave_id)
        requester = db.get(Employee, leave.employee_id) if leave else None
        if requester is not None and requester.user_id:
            NotificationService.create(db, requester.user_id, "Leave document overdue",
                                       f"A {request.category.replace('_', ' ').lower()} for your leave was due on "
                                       f"{request.due_date:%d %b %Y}.", "leave", f"/leaves/requests?modal=leave&id={leave.id}")
        overdue += 1
    db.flush()
    return {"reminded": reminded, "escalated": escalated, "returned_expired": expired, "documents_overdue": overdue}


def _jobs():
    from app.payroll.services import jobs

    return jobs


@_jobs().register("leave.sla_scan")
def _scan_job(ctx) -> dict:
    return scan(ctx.db, ctx.company_id)
