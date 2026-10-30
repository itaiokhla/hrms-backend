"""Leave encashment requests (SPINE §5.4, §8; 03-leave/02 §13) — V2_ACTIVE tenants.

The plan decides units (``encashment.eligible``, ``triggers``, ``step``, ``max_units_per_cycle``,
``min_balance_after``); money stays payroll's (``LEAVE_PAY_POLICY.encashment_formula`` → the LEAVE_ENCASHMENT
component, ``UNITS("LEAVE_ENCASHMENT")``). PENDING and APPROVED requests reserve their units. Approval hands the units
to payroll as an APPROVED system wage input of the next open period (``external_ref LEAVE-ENC-{id}``); payroll
finalization posts the ENCASH ledger row exactly once per (run employee, request) and marks the request PAID
(``mark_paid``). When the employee has no pay group or no open period yet, the request stays APPROVED with
``payroll_reference`` empty and a note, and HR can retry.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import authz, clock, concurrency
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import LeaveType
from app.models.leave_v2 import LeaveEncashmentRequest
from app.services import leave_access, leave_ledger, leave_plans
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

ZERO = Decimal(0)
COMPONENT = "LEAVE_ENCASHMENT"


def request_json(row: LeaveEncashmentRequest, name: Optional[str] = None) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "employee_name": name, "leave_type": row.leave_type_code,
            "units": leave_ledger._num(Decimal(row.units)), "trigger": row.trigger, "status": row.status,
            "reason": row.reason, "decision_note": row.decision_note,
            "cycle_start": row.cycle_start.isoformat() if row.cycle_start else None,
            "payroll_reference": row.payroll_reference, "paid_at": row.paid_at.isoformat() if row.paid_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None, "row_version": row.row_version}


def check(db: Session, company_id: int, employee: Employee, leave_type: LeaveType, units: Decimal, on: date, *,
          trigger: str = "ON_REQUEST", exclude_id: Optional[int] = None) -> dict:
    """Validate an encashment of ``units``; returns the balance facts (raises with a stable code otherwise)."""
    rule = leave_plans.rules_for(db, company_id, employee.id, leave_type.code, on)
    enc = (rule or {}).get("encashment") or {}
    if not enc.get("eligible") or trigger not in (enc.get("triggers") or []):
        raise AppError(422, f"{leave_type.name} cannot be encashed on request.", "ENCASHMENT_NOT_ALLOWED")
    if trigger == "ON_REQUEST" and employee.notice_start_date and employee.notice_start_date <= on \
            and not rule.get("encashable_in_notice"):
        raise AppError(422, "Encashment is not available during the notice period.", "ENCASHMENT_NOT_ALLOWED")
    step = Decimal(str(enc.get("step") or "0.5"))
    if units <= 0 or (units % step) != 0:
        raise AppError(422, {"message": f"Encash in steps of {step}.", "details": {"rule": "encashment_step"}},
                       "RULE_INVALID")
    view = leave_ledger.balance(db, company_id, employee, leave_type, on, rule=rule)
    available = view.balance - view.pending - leave_ledger.reserved_encashment(db, company_id, employee.id,
                                                                               leave_type.code, exclude_id=exclude_id)
    keep = Decimal(str(enc.get("min_balance_after") or "0"))
    if units > available - keep:
        raise AppError(400, f"You can encash at most {leave_ledger._num(max(available - keep, ZERO))} unit(s).",
                       "INSUFFICIENT_BALANCE", extra={"details": {"available": leave_ledger._num(available),
                                                                  "min_balance_after": leave_ledger._num(keep)}})
    cap = enc.get("max_units_per_cycle")
    if cap is not None and view.cycle_start:
        used = Decimal(db.query(func.coalesce(func.sum(LeaveEncashmentRequest.units), 0)).filter(
            LeaveEncashmentRequest.company_id == company_id, LeaveEncashmentRequest.employee_id == employee.id,
            LeaveEncashmentRequest.leave_type_code == leave_type.code,
            LeaveEncashmentRequest.cycle_start == date.fromisoformat(view.cycle_start),
            LeaveEncashmentRequest.status.in_(("PENDING", "APPROVED", "PAID")),
            *( [LeaveEncashmentRequest.id != exclude_id] if exclude_id else [])).scalar() or 0)
        if used + units > Decimal(str(cap)):
            raise AppError(422, f"At most {cap} unit(s) can be encashed per leave year (already {leave_ledger._num(used)}).",
                           "MAX_UNITS")
    return {"available": available, "cycle_start": date.fromisoformat(view.cycle_start) if view.cycle_start else None}


def create(db: Session, principal: Principal, *, leave_type: str, units: Decimal, reason: Optional[str],
           employee_id: Optional[int] = None) -> LeaveEncashmentRequest:
    leave_access.require_v2(db, principal.company_id)
    if employee_id is None or employee_id == principal.employee_id:
        if not principal.has("leave:apply"):
            raise forbidden()
        employee = authz.own_employee(db, principal)
    else:
        employee = authz.ensure_employee_access(db, principal, "leave:apply_on_behalf", employee_id)
    concurrency.lock_employees(db, [employee.id])
    lt = db.query(LeaveType).filter(LeaveType.company_id == principal.company_id, LeaveType.code == leave_type).first()
    if lt is None or not lt.is_active:
        raise bad_request("Unknown leave type.", "INVALID_LEAVE_TYPE")
    from app.services import leave_requests

    today = leave_requests.local_today(db, employee)
    facts = check(db, principal.company_id, employee, lt, Decimal(units), today)
    row = LeaveEncashmentRequest(company_id=principal.company_id, employee_id=employee.id, leave_type_code=lt.code,
                                 cycle_start=facts["cycle_start"], units=Decimal(units), trigger="ON_REQUEST",
                                 status="PENDING", reason=(reason or "").strip() or None,
                                 requested_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    _notify_approvers(db, row, employee)
    AuditLogService.log(db, principal, "Leave", "ENCASHMENT_REQUESTED",
                        f"Encashment of {units} unit(s) of {lt.code} requested for {employee.full_name}",
                        "leave_encashment", row.id)
    return row


def _notify_approvers(db: Session, row: LeaveEncashmentRequest, employee: Employee) -> None:
    from app.services import leave_service

    manager = leave_service._active_employee(db, employee.manager_id)
    if manager is not None and manager.user_id:
        NotificationService.create(db, manager.user_id, "Leave encashment awaiting approval",
                                   f"{employee.full_name} asked to encash leave.", "leave", "/leaves/approvals")


def _load(db: Session, principal: Principal, request_id: int, *, lock: bool = False) -> LeaveEncashmentRequest:
    query = db.query(LeaveEncashmentRequest).filter(LeaveEncashmentRequest.company_id == principal.company_id,
                                                    LeaveEncashmentRequest.id == request_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Encashment request")
    return row


def _can_decide(db: Session, principal: Principal, row: LeaveEncashmentRequest) -> bool:
    if row.employee_id == principal.employee_id:
        return False
    return principal.has("leave:approve") and authz.can_access_employee(db, principal, "leave:approve", row.employee_id)


def decide(db: Session, principal: Principal, request_id: int, approve: bool, note: Optional[str]) -> LeaveEncashmentRequest:
    row = _load(db, principal, request_id, lock=True)
    if not _can_decide(db, principal, row):
        if row.employee_id == principal.employee_id:
            raise forbidden("You cannot approve your own encashment.")
        if not authz.can_access_employee(db, principal, "leave:read", row.employee_id):
            raise not_found("Encashment request")
        raise forbidden("This encashment is not awaiting your approval.")
    if row.status != "PENDING":
        raise AppError(409, "This encashment was already decided.", "INVALID_TRANSITION")
    employee = db.get(Employee, row.employee_id)
    concurrency.lock_employees(db, [employee.id])
    if approve:
        lt = db.query(LeaveType).filter(LeaveType.company_id == row.company_id, LeaveType.code == row.leave_type_code).one()
        check(db, row.company_id, employee, lt, Decimal(row.units), clock.company_today(db, row.company_id),
              exclude_id=row.id)
    elif not (note or "").strip():
        raise bad_request("Give a reason for rejecting the encashment.", "REASON_REQUIRED")
    row.status = "APPROVED" if approve else "REJECTED"
    row.decided_by_user_id, row.decided_at = principal.user_id, utcnow()
    row.decision_note = (note or "").strip() or None
    row.row_version += 1
    if approve:
        send_to_payroll(db, row, principal.user_id)
    if employee.user_id:
        NotificationService.create(db, employee.user_id, f"Leave encashment {row.status.lower()}",
                                   f"Your request to encash {leave_ledger._num(Decimal(row.units))} unit(s) was "
                                   f"{row.status.lower()}.", "leave", "/leaves/balances")
    AuditLogService.log(db, principal, "Leave", "ENCASHMENT_" + row.status, f"Encashment #{row.id} {row.status.lower()}",
                        "leave_encashment", row.id)
    db.flush()
    return row


def withdraw(db: Session, principal: Principal, request_id: int) -> LeaveEncashmentRequest:
    row = _load(db, principal, request_id, lock=True)
    if row.employee_id != principal.employee_id:
        raise not_found("Encashment request")
    if row.status != "PENDING":
        raise AppError(409, "Only a pending encashment can be withdrawn.", "INVALID_TRANSITION")
    row.status = "WITHDRAWN"
    row.row_version += 1
    db.flush()
    return row


def send_to_payroll(db: Session, row: LeaveEncashmentRequest, actor_user_id: Optional[int]) -> Optional[int]:
    """An APPROVED system wage input of the LEAVE_ENCASHMENT component (units) in the next open pay period."""
    from app.models.variable_inputs import WageInput
    from app.payroll import state_machines as sm
    from app.payroll.services import wage_inputs

    if row.payroll_reference:
        return None
    today = clock.company_today(db, row.company_id)
    try:
        with db.begin_nested():
            place = wage_inputs.placement(db, row.company_id, row.employee_id, today)
            period = wage_inputs.next_open_period(db, row.company_id, place.pay_group, today - timedelta(days=40))
            if period is None:
                raise ValueError("no open pay period")
            comp = wage_inputs.component(db, row.company_id, COMPONENT, period[0])
            wage = WageInput(company_id=row.company_id, employee_id=row.employee_id,
                             legal_entity_id=place.legal_entity_id, pay_group_id=place.pay_group_id,
                             component_id=comp.id, component_code=comp.code, units=Decimal(row.units),
                             currency=place.currency, target_period_start=period[0], target_period_end=period[1],
                             reason=f"Leave encashment #{row.id}: {row.leave_type_code}", attachments=[],
                             source="SYSTEM", status=sm.WageInputStatus.APPROVED, external_ref=f"LEAVE-ENC-{row.id}",
                             flags=[], created_by_user_id=actor_user_id, row_version=1, submitted_at=utcnow(),
                             decided_at=utcnow())
            db.add(wage)
            db.flush()
        row.payroll_reference = f"wage_input:{wage.id}"
        return wage.id
    except Exception as exc:  # payroll not set up for the person yet: keep it approved, HR retries
        row.decision_note = ((row.decision_note or "") + f" [Payroll hand-off pending: {str(exc)[:120]}]").strip()
        return None


def withdraw_from_payroll(db: Session, row: LeaveEncashmentRequest) -> None:
    from app.models.variable_inputs import WageInput

    if not (row.payroll_reference or "").startswith("wage_input:"):
        return
    wage = db.query(WageInput).filter(WageInput.company_id == row.company_id,
                                      WageInput.id == int(row.payroll_reference.split(":")[1])).first()
    if wage is not None and wage.consumed_by_run_employee_id is None:
        wage.status = "CANCELLED"
        row.payroll_reference = None


def paid_requests_for_inputs(db: Session, company_id: int, wage_input_ids) -> List[tuple]:
    """Finalization hook: (leave type, units, ref) for encashment requests paid through these wage inputs."""
    refs = [f"wage_input:{i}" for i in wage_input_ids]
    rows = db.query(LeaveEncashmentRequest).filter(LeaveEncashmentRequest.company_id == company_id,
                                                   LeaveEncashmentRequest.payroll_reference.in_(refs or ["-"]),
                                                   LeaveEncashmentRequest.status == "APPROVED").all()
    out = []
    for row in rows:
        row.status, row.paid_at = "PAID", utcnow()
        out.append((row.leave_type_code, Decimal(row.units), f"encash_req:{row.id}"))
    return out


def list_requests(db: Session, principal: Principal, *, mine: bool = False, status: Optional[str] = None) -> List[dict]:
    query = db.query(LeaveEncashmentRequest, Employee).join(
        Employee, (Employee.company_id == LeaveEncashmentRequest.company_id) & (Employee.id == LeaveEncashmentRequest.employee_id)
    ).filter(LeaveEncashmentRequest.company_id == principal.company_id)
    if mine:
        query = query.filter(LeaveEncashmentRequest.employee_id == principal.employee_id)
    else:
        read, approve = (authz.employee_scope(db, principal, code) for code in ("leave:read", "leave:approve"))
        scope = read if approve.is_empty else approve if read.is_empty else authz.EmployeeScope(
            all_company=read.all_company or approve.all_company, selects=list(read.selects) + list(approve.selects))
        query = query.filter(scope.clause(LeaveEncashmentRequest.employee_id))
    if status:
        query = query.filter(LeaveEncashmentRequest.status == status)
    rows = query.order_by(LeaveEncashmentRequest.id.desc()).limit(200).all()
    out = []
    for row, person in rows:
        item = request_json(row, person.full_name)
        item["can_decide"] = row.status == "PENDING" and _can_decide(db, principal, row)
        out.append(item)
    return out
