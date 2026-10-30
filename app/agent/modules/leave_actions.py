"""Deterministic leave action handlers, run only by the confirm endpoint (docs/leave_agent.md §13).

Each handler re-reads the request, refuses a request that changed since the card was prepared (``ACTION_STALE``)
and then calls the leave service, which takes the employee lock and runs every leave rule again (balance, overlap,
policy, permissions). Handlers never commit: the confirm transaction does, together with the action's completion.
Results are stable ids and statuses only.
"""

from decimal import Decimal
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.agent.base import ActionSpec
from app.agent.modules.leave_tools import snapshot
from app.core.errors import AppError, forbidden, not_found
from app.core.principal import Principal
from app.models.leave import Leave, V2_STATUS
from app.schemas.leave_v2 import LeaveRequestIn
from app.services import leave_changes, leave_ledger, leave_requests


def _status(leave: Leave) -> str:
    return V2_STATUS.get(leave.status, (leave.status or "").upper())


def _unchanged(db: Session, principal: Principal, payload: Dict[str, Any]) -> Leave:
    leave = leave_requests.load(db, principal, int(payload["leave_request_id"]), lock=True)
    seen = payload.get("snapshot") or {}
    now = snapshot(leave)
    keys = ("leave_type", "start_date", "end_date", "start_portion", "end_portion", "version", "row_version")
    if any(seen.get(k) != now.get(k) for k in keys):
        raise AppError(409, "This leave request changed after the card was prepared. Ask the assistant again.",
                       "ACTION_STALE")
    return leave


def _own(principal: Principal, leave: Leave) -> None:
    if principal.employee_id is None or leave.employee_id != principal.employee_id:
        raise not_found("Leave request")


def execute_leave_application(db: Session, principal: Principal, payload: Dict[str, Any]) -> Dict[str, Any]:
    data = LeaveRequestIn(**payload["input"])  # strict: the stored canonical input only (no employee_id)
    leave = leave_requests.submit(db, principal, data)
    db.flush()
    status = _status(leave)
    days = leave_ledger._num(Decimal(leave.number_of_days or 0))
    outcome = "was approved" if status == "APPROVED" else "was submitted and is pending approval"
    return {"leave_request_id": leave.id, "status": status, "days": days, "leave_type": leave.leave_type,
            "start_date": leave.start_date.isoformat(), "end_date": leave.end_date.isoformat(),
            "message": f"Leave request #{leave.id} {outcome}."}


def execute_leave_withdrawal(db: Session, principal: Principal, payload: Dict[str, Any]) -> Dict[str, Any]:
    leave = _unchanged(db, principal, payload)
    _own(principal, leave)
    leave = leave_requests.withdraw(db, principal, leave.id, payload.get("reason"))
    db.flush()
    return {"leave_request_id": leave.id, "status": _status(leave),
            "message": f"Leave request #{leave.id} was withdrawn."}


def execute_leave_cancellation(db: Session, principal: Principal, payload: Dict[str, Any]) -> Dict[str, Any]:
    leave = _unchanged(db, principal, payload)
    _own(principal, leave)
    row = leave_changes.request_change(db, principal, leave.id, kind="CANCEL", proposed={},
                                       reason=payload.get("reason"))
    db.flush()
    applied = row.status != "PENDING"
    message = (f"Leave #{leave.id} was cancelled." if applied else
               f"Cancelling leave #{leave.id} was requested and is waiting for approval.")
    return {"leave_request_id": leave.id, "change_id": row.id, "change_status": row.status, "message": message}


def _decide(db: Session, principal: Principal, payload: Dict[str, Any], approve: bool) -> Dict[str, Any]:
    leave = _unchanged(db, principal, payload)
    if principal.employee_id is not None and leave.employee_id == principal.employee_id:
        raise forbidden("You cannot approve or reject your own leave.")
    leave = leave_requests.decide_request(db, principal, leave.id, approve, payload.get("remarks"))
    status = _status(leave)
    verb = "approved" if approve else "rejected"
    return {"leave_request_id": leave.id, "status": status, "decision": "APPROVE" if approve else "REJECT",
            "message": f"You {verb} leave request #{leave.id} (now {status.lower()})."}


def execute_leave_approval(db: Session, principal: Principal, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _decide(db, principal, payload, True)


def execute_leave_rejection(db: Session, principal: Principal, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _decide(db, principal, payload, False)


ACTIONS = (
    ActionSpec("leave.apply", "leave", "leave:apply", "R2", True, execute_leave_application, "a leave application"),
    ActionSpec("leave.withdraw", "leave", "leave:apply", "R2", True, execute_leave_withdrawal,
               "withdrawing a leave request"),
    ActionSpec("leave.cancel", "leave", "leave:apply", "R2", True, execute_leave_cancellation,
               "cancelling approved leave"),
    ActionSpec("leave.approve", "leave", "leave:approve", "R3", False, execute_leave_approval,
               "approving a leave request"),
    ActionSpec("leave.reject", "leave", "leave:approve", "R3", False, execute_leave_rejection,
               "rejecting a leave request"),
)
