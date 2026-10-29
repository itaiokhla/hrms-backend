"""Leave requests with configurable, hierarchy-aware approval routing.

Routing rules (explicit, so behaviour after organisational change is predictable):
* A request instantiates the company's approval chain at submission time;
  steps with ``min_days`` only apply to requests of at least that length.
* Hierarchy steps (primary manager, skip-level manager, department head)
  resolve their approver *when evaluated*, so a manager change re-routes
  pending steps automatically and removes the previous manager's authority.
  Decided steps are history and never change.
* A step with no eligible approver (no manager, approver is the requester,
  role without members) is skipped. If every step is skipped, a Company
  Administrator step is appended so a human always decides.
* Nobody approves their own request. Approvers must currently hold
  ``leave:approve`` covering the requester; delegates act for an approver
  only during an active delegation window and only while that approver
  still has authority. Company-wide approvers may override, which is
  recorded as such.
* Approval generates one leave_days row per date (weekly offs and holidays
  are charged only when the leave type counts non-working days) and USE
  balance transactions; cancelling approved leave is itself a request that
  an approver decides, after which the days are voided and given back.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Union

from sqlalchemy import extract, func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core import authz, concurrency
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal, build_principal
from app.core.security import utcnow
from app.models.company import Company
from app.models.employee import Department, Employee
from app.models.leave import ApprovalChainStep, Leave, LeaveApproval, LeaveType
from app.models.time_inputs import LeaveBalanceTransaction
from app.models.rbac import Role, UserRoleAssignment
from app.models.user import User
from app.services.audit_log_service import AuditLogService
from app.services.company_service import today_for
from app.services.notification_service import NotificationService

APPROVER_LABELS = {
    "primary_manager": "Reporting manager",
    "skip_level_manager": "Manager's manager",
    "department_head": "Department head",
    "role": "Role",
}


# ---------------------------------------------------------------------------
# Leave types and balances
# ---------------------------------------------------------------------------
LEAVE_TYPE_FLAGS = ("kind", "allow_half_day", "counts_non_working_days")


def leave_types(db: Session, company_id: int, include_inactive: bool = False) -> List[LeaveType]:
    query = db.query(LeaveType).filter(LeaveType.company_id == company_id)
    if not include_inactive:
        query = query.filter(LeaveType.is_active.is_(True))
    return query.order_by(LeaveType.name).all()


def days_out(value) -> Union[int, float]:
    """Leave days for JSON: whole days stay integers (as before half days existed)."""
    days = Decimal(value or 0)
    return int(days) if days == days.to_integral_value() else float(days)


def balance_totals(db: Session, company_id: int, employee_id: int, year: int) -> Dict[str, Dict[str, Decimal]]:
    """Per leave type: used (USE transactions), other balance movements and pending requests of ``year``."""
    totals: Dict[str, Dict[str, Decimal]] = {}

    def bucket(code: str) -> Dict[str, Decimal]:
        return totals.setdefault(code, {"used": Decimal(0), "adjusted": Decimal(0), "pending": Decimal(0)})

    for code, reason, delta in (
        db.query(LeaveBalanceTransaction.leave_type, LeaveBalanceTransaction.reason,
                 func.coalesce(func.sum(LeaveBalanceTransaction.delta), 0))
        .filter(LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.employee_id == employee_id,
                extract("year", LeaveBalanceTransaction.effective_date) == year)
        .group_by(LeaveBalanceTransaction.leave_type, LeaveBalanceTransaction.reason)
    ):
        if reason == "USE":
            bucket(code)["used"] -= Decimal(delta)
        else:
            bucket(code)["adjusted"] += Decimal(delta)
    for code, days in (
        db.query(Leave.leave_type, func.coalesce(func.sum(Leave.number_of_days), 0))
        .filter(Leave.company_id == company_id, Leave.employee_id == employee_id, Leave.status == "pending",
                extract("year", Leave.start_date) == year)
        .group_by(Leave.leave_type)
    ):
        bucket(code)["pending"] += Decimal(days)
    return totals


def _local_today(db: Session, employee: Optional[Employee]) -> date:
    """Today for one person (LH BUG-07): their work location's date, never the server's or the company's."""
    from app.services import org_structure_service

    return org_structure_service.employee_local_today(db, employee)


def balances(db: Session, company_id: int, employee_id: int, year: Optional[int] = None) -> Dict[str, dict]:
    if not year:
        subject = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == employee_id).first()
        year = _local_today(db, subject).year if subject else today_for(db.get(Company, company_id)).year
    totals = balance_totals(db, company_id, employee_id, year)
    result = {}
    for lt in leave_types(db, company_id):
        t = totals.get(lt.code, {"used": Decimal(0), "adjusted": Decimal(0), "pending": Decimal(0)})
        entitled = Decimal(lt.annual_quota) + t["adjusted"]
        result[lt.code] = {
            "name": lt.name,
            "is_paid": lt.is_paid,
            "is_unlimited": lt.is_unlimited,
            "total": lt.annual_quota,
            "used": days_out(t["used"]),
            "pending": days_out(t["pending"]),
            "remaining": None if lt.is_unlimited else days_out(max(entitled - t["used"], Decimal(0))),
            "available": None if lt.is_unlimited else days_out(max(entitled - t["used"] - t["pending"], Decimal(0))),
        }
    return result


# ---------------------------------------------------------------------------
# Approver resolution
# ---------------------------------------------------------------------------
def _active_employee(db: Session, employee_id: Optional[int]) -> Optional[Employee]:
    if employee_id is None:
        return None
    employee = db.get(Employee, employee_id)
    return employee if employee is not None and employee.is_active else None


def resolve_step_employee(db: Session, requester: Employee, step: LeaveApproval) -> Optional[Employee]:
    if step.approver_type == "primary_manager":
        approver = _active_employee(db, requester.manager_id)
    elif step.approver_type == "skip_level_manager":
        manager = db.get(Employee, requester.manager_id) if requester.manager_id else None
        approver = _active_employee(db, manager.manager_id) if manager else None
    elif step.approver_type == "department_head":
        department = db.get(Department, requester.department_id) if requester.department_id else None
        approver = _active_employee(db, department.head_employee_id) if department else None
    else:
        return None
    if approver is None or approver.id == requester.id:
        return None
    return approver


def role_member_user_ids(db: Session, role_id: int, exclude_user_id: Optional[int] = None) -> List[int]:
    now = utcnow()
    query = (
        db.query(UserRoleAssignment.user_id)
        .join(User, User.id == UserRoleAssignment.user_id)
        .filter(
            UserRoleAssignment.role_id == role_id,
            User.status == "active",
            UserRoleAssignment.effective_from <= now,
        )
        .filter((UserRoleAssignment.effective_until.is_(None)) | (UserRoleAssignment.effective_until > now))
    )
    if exclude_user_id:
        query = query.filter(UserRoleAssignment.user_id != exclude_user_id)
    return [r[0] for r in query.all()]


def _step_has_approver(db: Session, requester: Employee, step: LeaveApproval) -> bool:
    if step.approver_type == "role":
        return bool(role_member_user_ids(db, step.role_id, exclude_user_id=requester.user_id))
    return resolve_step_employee(db, requester, step) is not None


def _notify_step(db: Session, leave: Leave, requester: Employee, step: LeaveApproval) -> None:
    title = "Leave request awaiting your approval"
    # Never the leave type: a type name can reveal health or family matters (LH BUG-30, V3-106).
    message = f"{requester.full_name} requested {days_out(leave.number_of_days)} day(s) of leave."
    if step.approver_type == "role":
        for user_id in role_member_user_ids(db, step.role_id, exclude_user_id=requester.user_id)[:25]:
            NotificationService.create(db, user_id, title, message, "leave", "/leaves")
    else:
        approver = resolve_step_employee(db, requester, step)
        if approver and approver.user_id:
            NotificationService.create(db, approver.user_id, title, message, "leave", "/leaves")


def _admin_role(db: Session, company_id: int) -> Role:
    return db.query(Role).filter(Role.company_id == company_id, Role.system_key == "company_admin").one()


def _finalize(db: Session, leave: Leave, requester: Employee, status: str, actor_user_id: Optional[int]) -> None:
    leave.status = status
    leave.current_step = None
    leave.decided_at = utcnow()
    leave.decided_by_user_id = actor_user_id
    if status == "approved":
        from app.payroll.services import time_inputs

        time_inputs.generate_leave_days(db, leave, actor_user_id)
    if requester.user_id:
        NotificationService.create(
            db, requester.user_id, f"Leave request {status}",
            f"Your leave from {leave.start_date} to {leave.end_date} was {status}.",
            "leave", "/employee/leaves",
        )


def advance(db: Session, leave: Leave, requester: Employee, actor_user_id: Optional[int] = None) -> None:
    """Move a pending request to its next actionable step, or finish it."""
    for step in leave.approvals:
        if step.status in ("approved", "skipped"):
            continue
        if step.status in ("rejected", "cancelled"):
            return
        if _step_has_approver(db, requester, step):
            if step.status == "waiting":
                step.status = "pending"
                _notify_step(db, leave, requester, step)
            leave.current_step = step.step_order
            return
        step.status = "skipped"

    if not any(s.status == "approved" for s in leave.approvals):
        admin_role = _admin_role(db, leave.company_id)
        admin_step = next(
            (s for s in leave.approvals if s.approver_type == "role" and s.role_id == admin_role.id), None
        )
        if admin_step is None:
            fallback = LeaveApproval(
                company_id=leave.company_id,
                step_order=(max((s.step_order for s in leave.approvals), default=0) + 1),
                approver_type="role",
                role_id=admin_role.id,
                status="pending",
            )
            leave.approvals.append(fallback)
            leave.current_step = fallback.step_order
            _notify_step(db, leave, requester, fallback)
            return
        # Only the requester could approve: keep it pending for another administrator.
        admin_step.status = "pending"
        leave.current_step = admin_step.step_order
        return
    _finalize(db, leave, requester, "approved", actor_user_id)


def reroute_pending_for(db: Session, company_id: int, employee_ids: List[int]) -> None:
    """Re-evaluate pending requests after a reporting-line change."""
    if not employee_ids:
        return
    affected = set(employee_ids)
    affected.update(
        r[0] for r in db.query(Employee.id).filter(Employee.manager_id.in_(employee_ids)).all()
    )
    pending = (
        db.query(Leave)
        .options(selectinload(Leave.approvals))
        .filter(Leave.company_id == company_id, Leave.status == "pending", Leave.employee_id.in_(affected))
        .all()
    )
    for leave in pending:
        advance(db, leave, db.get(Employee, leave.employee_id))


# ---------------------------------------------------------------------------
# Authorisation to act on a step
# ---------------------------------------------------------------------------
def _current_step(leave: Leave) -> Optional[LeaveApproval]:
    return next((s for s in leave.approvals if s.status == "pending"), None)


def _approver_can_act(db: Session, principal: Principal, requester: Employee) -> bool:
    return principal.has("leave:approve") and authz.can_access_employee(db, principal, "leave:approve", requester.id)


def _cancellation_pending(leave: Leave) -> bool:
    return leave.status == "approved" and leave.cancellation_status == "pending"


def can_act(db: Session, principal: Principal, leave: Leave, requester: Employee) -> Optional[str]:
    if principal.employee_id == leave.employee_id:
        return None
    if _cancellation_pending(leave):
        # Whoever may approve this person's leave decides the cancellation.
        return "approver" if _approver_can_act(db, principal, requester) else None
    if leave.status != "pending":
        return None
    step = _current_step(leave)
    if step is None:
        return None
    if step.approver_type == "role":
        if step.role_id in principal.role_ids and _approver_can_act(db, principal, requester):
            return "approver"
    else:
        approver = resolve_step_employee(db, requester, step)
        if approver is not None:
            if approver.id == principal.employee_id and _approver_can_act(db, principal, requester):
                return "approver"
            from app.services.org_service import active_delegators_for

            if approver.id in active_delegators_for(db, principal.employee_id) and approver.user_id:
                approver_user = db.get(User, approver.user_id)
                if approver_user and approver_user.status == "active":
                    delegator = build_principal(db, approver_user, None, db.get(Company, leave.company_id))
                    if _approver_can_act(db, delegator, requester):
                        return "delegate"
    if principal.has_company_scope("leave:approve"):
        return "override"
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _portions(db: Session, company_id: int, leave_type: LeaveType, data) -> tuple:
    start_portion = getattr(data, "start_portion", None) or "FULL"
    end_portion = getattr(data, "end_portion", None) or "FULL"
    if start_portion not in ("FULL", "SECOND_HALF") or end_portion not in ("FULL", "FIRST_HALF"):
        raise bad_request("A leave starts with a full day or the second half and ends with a full day or the first half.",
                          "INVALID_PORTION")
    if data.start_date == data.end_date and start_portion == "SECOND_HALF" and end_portion == "FIRST_HALF":
        raise bad_request("Choose either the first or the second half of the day.", "INVALID_PORTION")
    if (start_portion, end_portion) != ("FULL", "FULL"):
        from app.payroll.services import settings as settings_engine

        enabled = settings_engine.resolve(db, company_id, "leave.half_day.enabled", on=data.start_date).value
        if enabled is not True:
            raise bad_request("Half-day leave is not enabled for your company.", "HALF_DAY_DISABLED")
        if not leave_type.allow_half_day:
            raise bad_request(f"{leave_type.name} is taken in whole days only.", "HALF_DAY_NOT_ALLOWED")
    return start_portion, end_portion


def _slots_taken(leave: Leave) -> Dict[date, str]:
    from app.payroll.services import time_inputs

    return {
        d: time_inputs.portion_on(d, leave.start_date, leave.end_date, leave.start_portion or "FULL",
                                  leave.end_portion or "FULL")
        for d in (leave.start_date + timedelta(days=n) for n in range((leave.end_date - leave.start_date).days + 1))
    }


def apply(db: Session, principal: Principal, data) -> Leave:
    from app.payroll.services import time_inputs

    if not principal.has("leave:apply"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    # One request at a time per person (LH BUG-03, LCK-02): the overlap and balance checks below then see every
    # earlier request, so parallel or double-clicked applies cannot book the same days twice.
    concurrency.lock_employees(db, [employee.id])
    if data.start_date > data.end_date:
        raise bad_request("Start date must be on or before the end date.")
    if data.start_date < _local_today(db, employee):  # the applicant's date, not the company's (LH BUG-07)
        raise bad_request("Leave cannot start in the past.")
    if (data.end_date - data.start_date).days > 365:
        raise bad_request("A single request cannot exceed one year.")
    leave_type = (
        db.query(LeaveType)
        .filter(LeaveType.company_id == principal.company_id, LeaveType.code == data.leave_type, LeaveType.is_active.is_(True))
        .first()
    )
    if leave_type is None:
        raise bad_request("Unknown leave type.", "INVALID_LEAVE_TYPE")
    start_portion, end_portion = _portions(db, principal.company_id, leave_type, data)

    days, plan = time_inputs.plan_leave(db, principal.company_id, employee.id, leave_type, data.start_date,
                                        data.end_date, start_portion, end_portion)
    if not all(facts.employed for facts, _, _ in plan):
        raise bad_request("Leave can only be taken while employed.", "OUTSIDE_EMPLOYMENT")
    if days <= 0:
        raise bad_request("The selected dates are weekly offs or holidays; no leave is needed.", "NO_WORKING_DAYS")
    if not leave_type.is_unlimited:
        balance = balances(db, principal.company_id, employee.id, data.start_date.year)[leave_type.code]
        if Decimal(str(balance["available"])) < days:
            raise bad_request(
                f"Insufficient {leave_type.name} balance. Available: {balance['available']} day(s).",
                "INSUFFICIENT_BALANCE",
            )
    wanted = {facts.date: portion for facts, portion, _ in plan}
    for other in db.query(Leave).filter(
        Leave.employee_id == employee.id,
        Leave.status.in_(("pending", "approved")),
        Leave.start_date <= data.end_date,
        Leave.end_date >= data.start_date,
    ):
        for day, portion in _slots_taken(other).items():
            mine = wanted.get(day)
            if mine is not None and (mine == "FULL" or portion == "FULL" or mine == portion):
                raise conflict("You already have leave booked on some of these dates.", "LEAVE_OVERLAP")

    leave = Leave(
        company_id=principal.company_id,
        employee_id=employee.id,
        leave_type=leave_type.code,
        start_date=data.start_date,
        end_date=data.end_date,
        start_portion=start_portion,
        end_portion=end_portion,
        number_of_days=days,
        reason=data.reason,
        status="pending",
    )
    db.add(leave)
    db.flush()
    # AS-M6 (03-shifts/03 §4.2 "Leave applied afterwards"): pending swaps / releases of these dates are withdrawn and
    # an unfilled released shift goes back, so a pickup never silently refunds this leave (EC AS-SWP-05).
    from app.services import shift_requests

    shift_requests.cancel_for_leave(db, principal.company_id, employee.id, data.start_date, data.end_date)
    chain = (
        db.query(ApprovalChainStep)
        .filter(ApprovalChainStep.company_id == principal.company_id, ApprovalChainStep.module == "leave")
        .order_by(ApprovalChainStep.step_order)
        .all()
    )
    order = 0
    for step in chain:
        if step.min_days is not None and days < step.min_days:
            continue
        order += 1
        leave.approvals.append(
            LeaveApproval(
                company_id=principal.company_id,
                step_order=order,
                approver_type=step.approver_type,
                role_id=step.role_id,
                status="waiting",
            )
        )
    db.flush()
    advance(db, leave, employee)
    AuditLogService.log(
        db, principal, "Leave", "APPLY",
        f"{employee.full_name} applied for {days_out(days)} day(s) of {leave_type.code} leave",
        "leave", leave.id, details={"start_portion": start_portion, "end_portion": end_portion, "days": str(days)},
    )
    return leave


def _load_leave(db: Session, principal: Principal, leave_id: int, lock: bool = False) -> Leave:
    query = db.query(Leave).options(selectinload(Leave.approvals)).filter(
        Leave.company_id == principal.company_id, Leave.id == leave_id
    )
    if lock:
        query = query.with_for_update(of=Leave)
    leave = query.first()
    if leave is None:
        raise not_found("Leave request")
    return leave


def _require_override_step_up(db: Session, principal: Principal) -> None:
    """LH-SEC-02 (07 §5.2): with ``leave.override.requires_step_up`` on, an override decision needs a fresh step-up
    of the session (403 STEP_UP_REQUIRED); the assigned approver and delegates never need it."""
    from app.payroll.services import settings as settings_engine

    enabled = settings_engine.resolve(db, principal.company_id, "leave.override.requires_step_up",
                                      on=today_for(db.get(Company, principal.company_id))).value
    if enabled is True:
        from app.payroll.services.step_up import ensure_step_up

        ensure_step_up(db, principal)


def decide(db: Session, principal: Principal, leave_id: int, approve: bool, remarks: Optional[str]) -> Leave:
    leave = _load_leave(db, principal, leave_id, lock=True)
    requester = db.get(Employee, leave.employee_id)
    visible = authz.can_access_employee(db, principal, "leave:read", requester.id)
    acted_as = can_act(db, principal, leave, requester)
    if acted_as is None:
        if leave.employee_id == principal.employee_id:
            raise forbidden("You cannot approve your own leave.")
        if not visible:
            raise not_found("Leave request")
        if leave.status != "pending":
            raise bad_request(f"This request is already {leave.status}.")
        raise forbidden("This request is not awaiting your approval.")

    if acted_as == "override":
        _require_override_step_up(db, principal)
    if _cancellation_pending(leave):
        return _decide_cancellation(db, principal, leave, requester, approve, remarks)
    step = _current_step(leave)
    step.status = "approved" if approve else "rejected"
    step.acted_by_user_id = principal.user_id
    step.acted_as = acted_as
    step.acted_at = utcnow()
    step.remarks = remarks
    if approve:
        advance(db, leave, requester, principal.user_id)
    else:
        for later in leave.approvals:
            if later.status in ("waiting", "pending"):
                later.status = "cancelled"
        _finalize(db, leave, requester, "rejected", principal.user_id)
    leave.remarks = remarks or leave.remarks
    AuditLogService.log(
        db, principal, "Leave", "APPROVE" if approve else "REJECT",
        f"{'Approved' if approve else 'Rejected'} step {step.step_order} of leave #{leave.id} for {requester.full_name}"
        + (" (override)" if acted_as == "override" else " (delegated)" if acted_as == "delegate" else ""),
        "leave", leave.id,
    )
    return leave


def _decide_cancellation(db: Session, principal: Principal, leave: Leave, requester: Employee, approve: bool,
                         remarks: Optional[str]) -> Leave:
    leave.cancellation_status = "approved" if approve else "rejected"
    leave.cancellation_decided_at = utcnow()
    leave.cancellation_decided_by_user_id = principal.user_id
    leave.remarks = remarks or leave.remarks
    if approve:
        from app.payroll.services import time_inputs

        leave.status = "cancelled"
        time_inputs.void_leave_days(db, leave, "CANCELLED", principal.user_id)
    outcome = "approved" if approve else "rejected"
    if requester.user_id:
        NotificationService.create(
            db, requester.user_id, f"Leave cancellation {outcome}",
            f"Cancelling your leave from {leave.start_date} to {leave.end_date} was {outcome}.",
            "leave", "/employee/leaves",
        )
    AuditLogService.log(
        db, principal, "Leave", "CANCEL_APPROVE" if approve else "CANCEL_REJECT",
        f"{outcome.capitalize()} cancelling leave #{leave.id} of {requester.full_name}",
        "leave", leave.id, details={"cancellation_status": leave.cancellation_status, "remarks": remarks},
    )
    return leave


def cancel(db: Session, principal: Principal, leave_id: int, reason: Optional[str] = None) -> Leave:
    """Pending leave is withdrawn at once; approved leave gets a cancellation request (EC-LV-04).

    ``reason`` is kept on the request (LH BUG-01) and never copied into notifications or the audit trail.
    """
    leave = _load_leave(db, principal, leave_id, lock=True)
    if leave.employee_id != principal.employee_id:
        raise not_found("Leave request")
    if leave.status not in ("approved", "pending"):
        raise bad_request("Only pending or approved requests can be cancelled.")
    if leave.status == "approved" and leave.cancellation_status == "pending":
        raise conflict("Cancelling this leave is already awaiting approval.", "CANCELLATION_PENDING")
    if reason is not None:
        leave.cancellation_reason = reason.strip() or None
    if leave.status == "approved":
        leave.cancellation_status = "pending"
        leave.cancellation_requested_at = utcnow()
        leave.cancellation_decided_at = None
        leave.cancellation_decided_by_user_id = None
        requester = db.get(Employee, leave.employee_id)
        manager = _active_employee(db, requester.manager_id)
        if manager is not None and manager.user_id:
            NotificationService.create(
                db, manager.user_id, "Leave cancellation awaiting your approval",
                f"{requester.full_name} asked to cancel approved leave from {leave.start_date}.",
                "leave", "/leaves",
            )
        AuditLogService.log(db, principal, "Leave", "CANCEL_REQUESTED", f"Requested cancellation of leave #{leave.id}",
                            "leave", leave.id)
        return leave
    leave.status = "cancelled"
    leave.current_step = None
    for step in leave.approvals:
        if step.status in ("waiting", "pending"):
            step.status = "cancelled"
    AuditLogService.log(db, principal, "Leave", "CANCEL", f"Cancelled leave #{leave.id}", "leave", leave.id)
    return leave


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def serialize(db: Session, leave: Leave, requester: Employee, can_act_as: Optional[str] = None) -> dict:
    roles = {}
    steps = []
    for step in leave.approvals:
        if step.approver_type == "role":
            if step.role_id not in roles:
                role = db.get(Role, step.role_id)
                roles[step.role_id] = role.name if role else "Role"
            approver = roles[step.role_id]
        elif step.status in ("waiting", "pending"):
            resolved = resolve_step_employee(db, requester, step)
            approver = resolved.full_name if resolved else None
        else:
            approver = None
        actor = db.get(User, step.acted_by_user_id).full_name if step.acted_by_user_id else None
        steps.append(
            {
                "step_order": step.step_order,
                "approver_type": step.approver_type,
                "approver_label": APPROVER_LABELS.get(step.approver_type, step.approver_type),
                "approver": approver,
                "status": step.status,
                "acted_by": actor,
                "acted_as": step.acted_as,
                "acted_at": step.acted_at.isoformat() if step.acted_at else None,
                "remarks": step.remarks,
            }
        )
    return {
        "id": leave.id,
        "employee_id": requester.id,
        "employee_name": requester.full_name,
        "department": requester.department.name if requester.department else None,
        "leave_type": leave.leave_type,
        "start_date": leave.start_date.isoformat(),
        "end_date": leave.end_date.isoformat(),
        "number_of_days": days_out(leave.number_of_days),
        "start_portion": leave.start_portion or "FULL",
        "end_portion": leave.end_portion or "FULL",
        "cancellation_status": leave.cancellation_status,
        "reason": leave.reason,
        "status": leave.status,
        "remarks": leave.remarks,
        "created_at": leave.created_at.isoformat(),
        "decided_at": leave.decided_at.isoformat() if leave.decided_at else None,
        "steps": steps,
        "can_act": can_act_as is not None,
        "acting_as": can_act_as,
    }


def list_leaves(db: Session, principal: Principal, status: Optional[str], employee_id: Optional[int], skip: int, limit: int):
    scope = authz.employee_scope(db, principal, "leave:read")
    query = (
        db.query(Leave)
        .options(selectinload(Leave.approvals), joinedload(Leave.employee))
        .join(Employee, Employee.id == Leave.employee_id)
        .filter(Leave.company_id == principal.company_id, scope.clause(Leave.employee_id))
    )
    if status:
        query = query.filter(Leave.status == status)
    if employee_id:
        query = query.filter(Leave.employee_id == employee_id)
    total = query.order_by(None).count()
    rows = query.order_by(Leave.created_at.desc(), Leave.id.desc()).offset(skip).limit(limit).all()
    return total, [serialize(db, leave, leave.employee) for leave in rows]


def approval_inbox(db: Session, principal: Principal) -> List[dict]:
    if principal.employee_id is None and not principal.has("leave:approve"):
        return []
    pending = (
        db.query(Leave)
        .options(selectinload(Leave.approvals), joinedload(Leave.employee))
        .filter(Leave.company_id == principal.company_id,
                (Leave.status == "pending") | ((Leave.status == "approved") & (Leave.cancellation_status == "pending")))
        .order_by(Leave.start_date)
        .limit(500)
        .all()
    )
    result = []
    for leave in pending:
        acting = can_act(db, principal, leave, leave.employee)
        if acting:
            result.append(serialize(db, leave, leave.employee, acting))
    return result


def my_leaves(db: Session, principal: Principal) -> List[dict]:
    employee = authz.own_employee(db, principal)
    rows = (
        db.query(Leave)
        .options(selectinload(Leave.approvals))
        .filter(Leave.employee_id == employee.id)
        .order_by(Leave.created_at.desc())
        .limit(200)
        .all()
    )
    return [serialize(db, leave, employee) for leave in rows]


def upcoming_approved(db: Session, principal: Principal, days: int = 30) -> List[dict]:
    scope = authz.employee_scope(db, principal, "leave:read")
    # The viewer's own today anchors the window of this many-person list (LH BUG-07).
    viewer = db.get(Employee, principal.employee_id) if principal.employee_id else None
    today = _local_today(db, viewer) if viewer else today_for(db.get(Company, principal.company_id))
    rows = (
        db.query(Leave, Employee)
        .join(Employee, Employee.id == Leave.employee_id)
        .filter(
            Leave.company_id == principal.company_id,
            Leave.status == "approved",
            Leave.end_date >= today,
            Leave.start_date <= today + timedelta(days=days),
            scope.clause(Leave.employee_id),
        )
        .order_by(Leave.start_date)
        .limit(10)
        .all()
    )
    return [
        {
            "employee_name": e.full_name,
            "leave_type": leave.leave_type,
            "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(),
        }
        for leave, e in rows
    ]


# ---------------------------------------------------------------------------
# Policy configuration
# ---------------------------------------------------------------------------
def save_leave_type(db: Session, principal: Principal, data, type_id: Optional[int] = None) -> LeaveType:
    clash = db.query(LeaveType.id).filter(LeaveType.company_id == principal.company_id, LeaveType.code == data.code)
    if type_id:
        clash = clash.filter(LeaveType.id != type_id)
    if clash.first():
        raise conflict("A leave type with this code already exists.")
    if type_id:
        lt = db.query(LeaveType).filter(LeaveType.company_id == principal.company_id, LeaveType.id == type_id).first()
        if lt is None:
            raise not_found("Leave type")
        if data.code != lt.code and db.query(Leave.id).filter(
            Leave.company_id == principal.company_id, Leave.leave_type == lt.code
        ).first():
            # History refers to the code: renaming it would orphan leaves and balances (EC-LV-11).
            raise conflict("This leave type has leave history; its code cannot change. Deactivate it instead.",
                           "LEAVE_TYPE_CODE_FROZEN")
    else:
        lt = LeaveType(company_id=principal.company_id, kind=None, allow_half_day=True, counts_non_working_days=False)
        db.add(lt)
    values = data.model_dump()
    # LH BUG-02: the type flags are written only when sent (today's policy tab sends none of them), so a legacy
    # update never turns a comp-off type into ordinary leave; the booleans never become NULL.
    for key in LEAVE_TYPE_FLAGS:
        if key not in data.model_fields_set or (key != "kind" and values[key] is None):
            values.pop(key)
    for key, value in values.items():
        setattr(lt, key, value)
    db.flush()
    AuditLogService.log(db, principal, "Leave", "TYPE_SAVED", f"Saved leave type {lt.code}", "leave_type", lt.id)
    return lt


def get_chain(db: Session, company_id: int) -> List[dict]:
    steps = (
        db.query(ApprovalChainStep)
        .filter(ApprovalChainStep.company_id == company_id, ApprovalChainStep.module == "leave")
        .order_by(ApprovalChainStep.step_order)
        .all()
    )
    roles = {r.id: r.name for r in db.query(Role).filter(Role.company_id == company_id).all()}
    return [
        {
            "step_order": s.step_order,
            "approver_type": s.approver_type,
            "approver_label": APPROVER_LABELS.get(s.approver_type),
            "role_id": s.role_id,
            "role_name": roles.get(s.role_id),
            "min_days": s.min_days,
        }
        for s in steps
    ]


def save_chain(db: Session, principal: Principal, steps) -> List[dict]:
    if len(steps) > 10:
        raise bad_request("An approval chain can have at most 10 steps.")
    for step in steps:
        if step.approver_type == "role":
            if step.role_id is None:
                raise bad_request("Choose a role for role-based steps.")
            role = db.query(Role.id).filter(Role.company_id == principal.company_id, Role.id == step.role_id).first()
            if role is None:
                raise not_found("Role")
        elif step.role_id is not None:
            raise bad_request("Only role-based steps take a role.")
    db.query(ApprovalChainStep).filter(
        ApprovalChainStep.company_id == principal.company_id, ApprovalChainStep.module == "leave"
    ).delete(synchronize_session=False)
    db.flush()
    for index, step in enumerate(steps, start=1):
        db.add(
            ApprovalChainStep(
                company_id=principal.company_id,
                module="leave",
                step_order=index,
                approver_type=step.approver_type,
                role_id=step.role_id,
                min_days=step.min_days,
            )
        )
    db.flush()
    AuditLogService.log(
        db, principal, "Leave", "CHAIN_UPDATED",
        "Approval chain: " + " -> ".join(s.approver_type for s in steps) if steps else "Approval chain cleared",
    )
    return get_chain(db, principal.company_id)
