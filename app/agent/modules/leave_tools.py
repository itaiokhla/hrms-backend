"""Leave tools: thin adapters over the leave services (docs/leave_agent.md §14). No leave rule lives here.

R1 reads return the minimum fields for the conversation, for the signed-in employee only (identity from the
``ToolContext`` principal; the schemas carry no identity). R2 / R3 tools run the leave validator or the approver
check and save a prepared ``AgentAction``; they never submit, withdraw, cancel or decide anything.

Free text stored by people (leave reasons, notes) is not returned to the model: it is untrusted and not needed.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.agent import action_service
from app.agent.base import Capability, ToolContext
from app.core import authz
from app.core.errors import AppError, forbidden, not_found
from app.models.employee import Employee
from app.models.leave import Leave, LeaveType, V2_STATUS
from app.schemas.agent import (
    ApprovalsArgs, LeaveApplyArgs, LeaveBalanceArgs, LeaveCancelArgs, LeaveDecisionArgs, LeaveRequestsArgs, NoArgs,
    parse_iso_date,
)
from app.schemas.leave_v2 import LeaveRequestIn
from app.services import leave_access, leave_ledger, leave_requests, leave_service, leave_types

_num = leave_ledger._num


def _days(value) -> Any:
    return _num(Decimal(value or 0))


def _type_name(lt: Optional[LeaveType], code: str) -> str:
    return leave_types.display_name(lt) if lt is not None else code


def _types(ctx: ToolContext) -> Dict[str, LeaveType]:
    return {t.code: t for t in ctx.db.query(LeaveType).filter(LeaveType.company_id == ctx.principal.company_id)}


def snapshot(leave: Leave) -> Dict[str, Any]:
    """What the user saw when the action was prepared; confirm refuses if the request changed since."""
    return {"leave_type": leave.leave_type, "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(), "start_portion": leave.start_portion or "FULL",
            "end_portion": leave.end_portion or "FULL", "version": leave.version, "row_version": leave.row_version,
            "status": leave.status, "cancellation_status": leave.cancellation_status}


# =============================================================================================== R1 reads
def get_leave_types(ctx: ToolContext, args: NoArgs) -> Dict[str, Any]:
    rows = leave_types.list_types(ctx.db, ctx.principal.company_id)
    return {"leave_types": [
        {"code": t["code"], "name": t["display_name"], "unit": t["unit"], "paid": t["is_paid"],
         "unlimited": t["is_unlimited"], "half_day_allowed": t["allow_half_day"],
         "apply_in_chat": t["kind"] != "EVENT"} for t in rows]}


def get_my_leave_balance(ctx: ToolContext, args: LeaveBalanceArgs) -> Dict[str, Any]:
    employee = authz.own_employee(ctx.db, ctx.principal)
    day = parse_iso_date(args.on_date) if args.on_date else ctx.today
    rows = leave_ledger.balances_for(ctx.db, ctx.principal.company_id, employee, day)
    return {"date": day.isoformat(), "balances": [
        {"leave_type": b["leave_type_code"], "name": b["name"], "unit": b["unit"], "available": b["available"],
         "balance": b["balance"], "pending": b["pending"], "unlimited": b["unlimited"]} for b in rows]}


def get_my_leave_requests(ctx: ToolContext, args: LeaveRequestsArgs) -> Dict[str, Any]:
    principal = ctx.principal
    employee = authz.own_employee(ctx.db, principal)
    query = ctx.db.query(Leave).filter(Leave.company_id == principal.company_id, Leave.employee_id == employee.id)
    if args.status:
        query = query.filter(Leave.status == args.status.lower())
    rows = query.order_by(Leave.start_date.desc(), Leave.id.desc()).limit(args.limit).all()
    types = _types(ctx)
    items = []
    for leave in rows:
        allowed = leave_requests.allowed_actions(ctx.db, principal, leave)
        items.append({"id": leave.id, "leave_type": leave.leave_type,
                      "leave_type_name": _type_name(types.get(leave.leave_type), leave.leave_type),
                      "start_date": leave.start_date.isoformat(), "end_date": leave.end_date.isoformat(),
                      "start_portion": leave.start_portion or "FULL", "end_portion": leave.end_portion or "FULL",
                      "days": _days(leave.number_of_days), "status": V2_STATUS.get(leave.status, leave.status.upper()),
                      "cancellation_status": leave.cancellation_status,
                      "can_cancel": "WITHDRAW" in allowed or "CHANGE" in allowed})
    return {"requests": items, "count": len(items)}


def get_my_pending_approvals(ctx: ToolContext, args: ApprovalsArgs) -> Dict[str, Any]:
    principal = ctx.principal
    items = leave_service.approval_inbox(ctx.db, principal)[: args.limit]
    types = _types(ctx)
    out = []
    for item in items:
        leave = ctx.db.get(Leave, item["id"])
        if leave is None or leave.employee_id == principal.employee_id:
            continue
        kind = "CANCELLATION" if leave.status == "approved" and leave.cancellation_status == "pending" else "REQUEST"
        out.append({"id": leave.id, "employee_name": item["employee_name"],
                    "leave_type_name": _type_name(types.get(leave.leave_type), leave.leave_type),
                    "start_date": item["start_date"], "end_date": item["end_date"], "days": item["number_of_days"],
                    "kind": kind})
    return {"pending": out, "count": len(out)}


# =============================================================================================== R2 prepared writes
def _resolve_type(ctx: ToolContext, value: str) -> Optional[LeaveType]:
    """The tenant's active type by exact code, else by case-insensitive code or name (a convenience, not a rule)."""
    active = [t for t in _types(ctx).values() if t.is_active]
    wanted = value.strip()
    for match in (lambda t: t.code == wanted, lambda t: t.code.lower() == wanted.lower(),
                  lambda t: t.name.lower() == wanted.lower() or leave_types.display_name(t).lower() == wanted.lower()):
        found = [t for t in active if match(t)]
        if len(found) == 1:
            return found[0]
    return None


def _issues(pv) -> List[Dict[str, Any]]:
    return [{"code": i.code, "severity": i.severity, "message": i.message} for i in pv.issues]


def prepare_leave_application(ctx: ToolContext, args: LeaveApplyArgs) -> Dict[str, Any]:
    principal = ctx.principal
    lt = _resolve_type(ctx, args.leave_type)
    if lt is None:
        return {"status": "not_prepared", "issues": [{"code": "INVALID_LEAVE_TYPE", "severity": "ERROR",
                                                      "message": "Unknown leave type; use a code from get_leave_types."}]}
    data = LeaveRequestIn(leave_type=lt.code, start_date=args.start_date, end_date=args.end_date,
                          start_portion=args.start_portion, end_portion=args.end_portion,
                          reason=(args.reason or "").strip() or None)
    pv = leave_requests.validate(ctx.db, principal, data)  # the same validator as the leave form and submit
    if pv.errors:
        return {"status": "not_prepared", "issues": _issues(pv),
                "message": "Nothing was prepared. Explain the issues; do not retry with the same input."}
    canonical = {"leave_type": lt.code, "start_date": data.start_date.isoformat(), "end_date": data.end_date.isoformat(),
                 "start_portion": data.start_portion, "end_portion": data.end_portion, "reason": data.reason}
    days = _num(pv.charged)
    details = {"leave_type": lt.code, "leave_type_name": _type_name(lt, lt.code), "unit": lt.unit,
               "start_date": canonical["start_date"], "end_date": canonical["end_date"],
               "start_portion": data.start_portion, "end_portion": data.end_portion, "days": days,
               "reason": data.reason, "unlimited": bool(lt.is_unlimited),
               "available_before": _num(pv.balance_before), "available_after": _num(pv.balance_after),
               "split_unpaid_days": _num(pv.split_units) if pv.split_units else None,
               "auto_approve": pv.auto_approve,
               "warnings": [i.message for i in pv.issues if i.severity == "WARNING"]}
    unit = "hour(s)" if lt.unit == "HOUR" else "day(s)"
    span = canonical["start_date"] if canonical["start_date"] == canonical["end_date"] else \
        f"{canonical['start_date']} to {canonical['end_date']}"
    summary = f"Apply {days} {unit} of {details['leave_type_name']}: {span}"
    action = action_service.prepare_action(ctx, "leave.apply", {"input": canonical, "employee_id": pv.employee.id},
                                           details, summary)
    return {"status": "prepared", "action_id": action.id, "summary": summary, "details": details,
            "next_step": "The user must review and confirm the action card. Nothing has been submitted yet."}


def _own_leave(ctx: ToolContext, leave_id: int) -> Leave:
    leave = ctx.db.query(Leave).filter(Leave.company_id == ctx.principal.company_id, Leave.id == leave_id).first()
    if leave is None or ctx.principal.employee_id is None or leave.employee_id != ctx.principal.employee_id:
        raise not_found("Leave request")
    return leave


def prepare_leave_cancellation(ctx: ToolContext, args: LeaveCancelArgs) -> Dict[str, Any]:
    principal = ctx.principal
    leave = _own_leave(ctx, args.leave_request_id)
    allowed = leave_requests.allowed_actions(ctx.db, principal, leave)
    if leave.status in ("pending", "returned") and "WITHDRAW" in allowed:
        action_type, verb = "leave.withdraw", "Withdraw"
    elif leave.status == "approved" and "CHANGE" in allowed:
        if leave.cancellation_status == "pending":
            raise AppError(409, "Cancelling this leave is already waiting for a decision.", "CHANGE_PENDING")
        action_type, verb = "leave.cancel", "Cancel"
    else:
        raise AppError(409, f"This request is {V2_STATUS.get(leave.status, leave.status)} and cannot be cancelled "
                            "here.", "INVALID_TRANSITION")
    lt = _types(ctx).get(leave.leave_type)
    name = _type_name(lt, leave.leave_type)
    reason = (args.reason or "").strip() or None
    details = {"leave_request_id": leave.id, "leave_type_name": name, "start_date": leave.start_date.isoformat(),
               "end_date": leave.end_date.isoformat(), "days": _days(leave.number_of_days),
               "current_status": V2_STATUS.get(leave.status, leave.status.upper()), "reason": reason,
               "operation": "WITHDRAW" if action_type == "leave.withdraw" else "CANCEL",
               "note": "Withdraws the pending request." if action_type == "leave.withdraw" else
               "Asks to cancel approved leave; your approver may need to agree."}
    summary = f"{verb} {name} request #{leave.id} ({details['start_date']} to {details['end_date']})"
    action = action_service.prepare_action(ctx, action_type, {"leave_request_id": leave.id, "reason": reason,
                                                              "employee_id": principal.employee_id,
                                                              "snapshot": snapshot(leave)}, details, summary)
    return {"status": "prepared", "action_id": action.id, "summary": summary,
            "next_step": "The user must review and confirm the action card. Nothing has changed yet."}


# =============================================================================================== R3 approvals
def _decidable(ctx: ToolContext, leave_id: int) -> Leave:
    """A request the caller may act on now (the leave service's own check); never the caller's own request."""
    principal = ctx.principal
    if not principal.has("leave:approve"):
        raise forbidden()
    leave = leave_requests.load(ctx.db, principal, leave_id)
    if principal.employee_id is not None and leave.employee_id == principal.employee_id:
        raise forbidden("You cannot approve or reject your own leave.")
    requester = ctx.db.get(Employee, leave.employee_id)
    if leave_service.can_act(ctx.db, principal, leave, requester) is None:
        if not authz.can_access_employee(ctx.db, principal, "leave:read", requester.id):
            raise not_found("Leave request")
        raise forbidden("This request is not awaiting your approval.")
    return leave


def _prepare_decision(ctx: ToolContext, args: LeaveDecisionArgs, approve: bool) -> Dict[str, Any]:
    leave = _decidable(ctx, args.leave_request_id)
    remarks = (args.remarks or "").strip() or None
    if not approve and not remarks and leave_access.setting(ctx.db, ctx.principal.company_id,
                                                            "leave.approval.reject_reason_required", default=True):
        return {"status": "needs_input", "missing": ["remarks"],
                "message": "Ask the approver for the reason to give the employee, then call this tool again."}
    requester = ctx.db.get(Employee, leave.employee_id)
    lt = _types(ctx).get(leave.leave_type)
    kind = "CANCELLATION" if leave.status == "approved" and leave.cancellation_status == "pending" else "REQUEST"
    details = {"leave_request_id": leave.id, "employee_name": requester.full_name,
               "leave_type_name": _type_name(lt, leave.leave_type), "start_date": leave.start_date.isoformat(),
               "end_date": leave.end_date.isoformat(), "days": _days(leave.number_of_days), "kind": kind,
               "decision": "APPROVE" if approve else "REJECT", "remarks": remarks}
    verb = "Approve" if approve else "Reject"
    what = "cancellation of " if kind == "CANCELLATION" else ""
    summary = (f"{verb} {what}{requester.full_name}'s {details['leave_type_name']} "
               f"({details['start_date']} to {details['end_date']})")
    action = action_service.prepare_action(
        ctx, "leave.approve" if approve else "leave.reject",
        {"leave_request_id": leave.id, "remarks": remarks, "employee_id": ctx.principal.employee_id,
         "snapshot": snapshot(leave)}, details, summary)
    return {"status": "prepared", "action_id": action.id, "summary": summary,
            "next_step": "The approver must confirm the action card. Nothing has been decided yet."}


def prepare_leave_approval(ctx: ToolContext, args: LeaveDecisionArgs) -> Dict[str, Any]:
    return _prepare_decision(ctx, args, True)


def prepare_leave_rejection(ctx: ToolContext, args: LeaveDecisionArgs) -> Dict[str, Any]:
    return _prepare_decision(ctx, args, False)


# =============================================================================================== catalogue
CAPABILITIES = (
    Capability("get_leave_types", "leave", "R1", "read", ("leave:read", "leave:apply"), False, NoArgs,
               "List the active leave types of the user's company (code, name, unit, paid, half-day rules).",
               get_leave_types),
    Capability("get_my_leave_balance", "leave", "R1", "read", ("leave:read",), True, LeaveBalanceArgs,
               "Read the signed-in employee's own leave balances (available, pending) on a date, default today.",
               get_my_leave_balance),
    Capability("get_my_leave_requests", "leave", "R1", "read", ("leave:read",), True, LeaveRequestsArgs,
               "List the signed-in employee's own latest leave requests with id, dates, days, status and whether "
               "they can be cancelled.", get_my_leave_requests),
    Capability("prepare_leave_application", "leave", "R2", "prepared_write", ("leave:apply",), True, LeaveApplyArgs,
               "Validate a leave application for the signed-in employee and prepare it for the user's explicit "
               "confirmation. It never submits the leave. Returns the issues instead when the request is invalid.",
               prepare_leave_application, "leave.apply"),
    Capability("prepare_leave_cancellation", "leave", "R2", "prepared_write", ("leave:apply",), True,
               LeaveCancelArgs,
               "Prepare withdrawing one of the signed-in employee's own pending requests, or asking to cancel own "
               "approved leave, for explicit confirmation. It never changes anything by itself.",
               prepare_leave_cancellation, "leave.withdraw|leave.cancel"),
    Capability("get_my_pending_approvals", "leave", "R1", "read", ("leave:approve",), False, ApprovalsArgs,
               "List leave requests and cancellations waiting for the signed-in approver's decision.",
               get_my_pending_approvals),
    Capability("prepare_leave_approval", "leave", "R3", "prepared_write", ("leave:approve",), False,
               LeaveDecisionArgs,
               "Prepare approving a request from get_my_pending_approvals for the approver's explicit confirmation. "
               "Never the approver's own request; it decides nothing by itself.", prepare_leave_approval,
               "leave.approve"),
    Capability("prepare_leave_rejection", "leave", "R3", "prepared_write", ("leave:approve",), False,
               LeaveDecisionArgs,
               "Prepare rejecting a request from get_my_pending_approvals with the approver's reason, for explicit "
               "confirmation. Never the approver's own request; it decides nothing by itself.",
               prepare_leave_rejection, "leave.reject"),
)
