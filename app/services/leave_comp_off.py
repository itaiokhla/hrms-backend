"""Comp-off claims (SPINE §5.3, §8 "Comp-off earning"; 03-leave/06 §3) — V2_ACTIVE tenants.

An employee who worked on a holiday or weekly off (per day facts, ``comp_off.earn_on``) claims compensatory leave
within ``claim_window_days``. Worked minutes come from the attendance sessions of that shift date; the plan's
``credit_slabs`` turn them into units (default 4 h → 0.5, 8 h → 1.0). One compensation per (employee, date): a date
with an active claim, or with approved full-day leave (``WORKED_ON_LEAVE``), cannot be claimed. Approval (manager in
scope, never the claimant) posts a COMP_OFF lot that expires ``validity_days`` after the worked date and never earlier
than ``min_usable_days`` after the credit. Revoking an approved claim reverses the unused part of its lot; used credit
stays (an HR exception, never a silent deduction).
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import authz, concurrency
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.attendance import Attendance
from app.models.employee import Employee
from app.models.leave import Leave, LeaveType
from app.models.leave_v2 import CompOffClaim
from app.models.time_inputs import LeaveDay
from app.services import leave_access, leave_ledger, leave_plans
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

ZERO = Decimal(0)


def claim_json(row: CompOffClaim, name: Optional[str] = None) -> dict:
    return {"id": row.id, "employee_id": row.employee_id, "employee_name": name, "worked_date": row.worked_date.isoformat(),
            "source": row.source, "worked_minutes": row.worked_minutes,
            "credit_units": leave_ledger._num(Decimal(row.credit_units)), "leave_type": row.leave_type_code,
            "status": row.status, "reason": row.reason, "decision_note": row.decision_note,
            "expires_on": row.expires_on.isoformat() if row.expires_on else None,
            "created_at": row.created_at.isoformat() if row.created_at else None, "row_version": row.row_version}


def worked_minutes(db: Session, company_id: int, employee_id: int, day: date) -> int:
    rows = db.query(Attendance).filter(Attendance.company_id == company_id, Attendance.employee_id == employee_id,
                                       Attendance.date == day, Attendance.voided_at.is_(None),
                                       Attendance.check_out_time.isnot(None)).all()
    return int(sum(((r.check_out_time - r.check_in_time).total_seconds() // 60 for r in rows), 0))


def _plan_rules(db: Session, company_id: int, employee: Employee, day: date):
    resolution = leave_plans.resolve(db, company_id, employee.id, day)
    rules = resolution.fields.get("comp_off")
    types = {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == company_id,
                                                          LeaveType.kind == "COMP_OFF", LeaveType.is_active.is_(True))}
    rule = next((r for r in resolution.fields.get("types", []) if r["leave_type_code"] in types), None)
    if rules is None or rule is None:
        raise AppError(422, "Your leave plan has no comp-off.", "COMP_OFF_NOT_AVAILABLE")
    return rules, types[rule["leave_type_code"]], resolution


def units_for(rules: dict, minutes: int) -> Decimal:
    units = ZERO
    for slab in sorted(rules.get("credit_slabs") or [], key=lambda s: s["min_worked_minutes"]):
        if minutes >= int(slab["min_worked_minutes"]):
            units = Decimal(str(slab["units"]))
    return units


def preview_claim(db: Session, company_id: int, employee: Employee, day: date, today: date) -> dict:
    from app.payroll.services import time_inputs

    rules, lt, _ = _plan_rules(db, company_id, employee, day)
    if day > today:
        raise AppError(422, "You can only claim for a day you already worked.", "COMP_OFF_FUTURE")
    window = int(rules.get("claim_window_days") or 30)
    if (today - day).days > window:
        raise AppError(422, f"Comp-off must be claimed within {window} days of the worked day.", "CLAIM_WINDOW_PASSED")
    facts = time_inputs.day_facts(db, company_id, employee.id, day, day)[0]
    if not facts.employed:
        raise AppError(422, "You were not employed on that day.", "OUTSIDE_EMPLOYMENT")
    is_holiday = facts.holiday is not None and facts.holiday.non_working
    source = "HOLIDAY_WORK" if is_holiday else ("WEEKLY_OFF_WORK" if facts.weekly_off else None)
    allowed = rules.get("earn_on") or ["HOLIDAY", "WEEKLY_OFF"]
    if source is None or (source == "HOLIDAY_WORK" and "HOLIDAY" not in allowed) or (
            source == "WEEKLY_OFF_WORK" and "WEEKLY_OFF" not in allowed):
        raise AppError(422, "Comp-off is earned only for work on a holiday or weekly off.", "NOT_A_NON_WORKING_DAY")
    on_leave = db.query(LeaveDay.id).join(Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id)
                                          ).filter(LeaveDay.company_id == company_id, LeaveDay.employee_id == employee.id,
                                                   LeaveDay.date == day, LeaveDay.voided_at.is_(None),
                                                   LeaveDay.portion == "FULL", Leave.status == "approved").first()
    if on_leave:
        raise AppError(409, "You were on approved leave that day.", "WORKED_ON_LEAVE")
    minutes = worked_minutes(db, company_id, employee.id, day)
    units = units_for(rules, minutes)
    if facts.weekly_off_portion in ("FIRST_HALF", "SECOND_HALF"):
        units = min(units, Decimal("0.5"))
    if units <= 0:
        first = min((int(s["min_worked_minutes"]) for s in rules.get("credit_slabs") or []), default=240)
        raise AppError(422, f"At least {first // 60} h {first % 60} min of recorded work is needed (found {minutes} min).",
                       "NOT_ENOUGH_WORK", extra={"details": {"worked_minutes": minutes}})
    validity = int(rules.get("validity_days") or 60)
    usable = int(rules.get("min_usable_days") or 14)
    expires = max(day + timedelta(days=validity), today + timedelta(days=usable))
    return {"worked_date": day.isoformat(), "source": source, "worked_minutes": minutes,
            "credit_units": leave_ledger._num(units), "leave_type": lt.code, "expires_on": expires.isoformat(),
            "holiday": facts.holiday.name if is_holiday else None}


def create(db: Session, principal: Principal, *, worked_date: date, reason: Optional[str],
           employee_id: Optional[int] = None) -> CompOffClaim:
    from app.services import leave_requests

    cid = principal.company_id
    leave_access.require_v2(db, cid)
    if employee_id is None or employee_id == principal.employee_id:
        if not principal.has("leave:apply"):
            raise forbidden()
        employee = authz.own_employee(db, principal)
    else:
        employee = authz.ensure_employee_access(db, principal, "leave:apply_on_behalf", employee_id)
    where = leave_plans.placement(db, cid, employee.id, worked_date)
    if not leave_access.setting(db, cid, "leave.comp_off.claims.enabled", legal_entity_id=where.legal_entity_id
                                if where else None, branch_id=where.branch_id if where else None, default=True):
        raise AppError(422, "Comp-off claims are turned off.", "COMP_OFF_NOT_AVAILABLE")
    concurrency.lock_employees(db, [employee.id])
    today = leave_requests.local_today(db, employee)
    facts = preview_claim(db, cid, employee, worked_date, today)
    row = CompOffClaim(company_id=cid, employee_id=employee.id, worked_date=worked_date, source=facts["source"],
                       worked_minutes=facts["worked_minutes"], credit_units=Decimal(str(facts["credit_units"])),
                       leave_type_code=facts["leave_type"], status="PENDING", reason=(reason or "").strip() or None,
                       requested_by_user_id=principal.user_id)
    db.add(row)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise conflict("A comp-off claim for this day already exists.", "COMP_OFF_SOURCE_TAKEN")
    from app.services import leave_service

    manager = leave_service._active_employee(db, employee.manager_id)
    if manager is not None and manager.user_id:
        NotificationService.create(db, manager.user_id, "Comp-off claim awaiting approval",
                                   f"{employee.full_name} claimed comp-off for {worked_date:%d %b %Y}.", "leave",
                                   "/leaves/approvals?tab=comp-off")
    AuditLogService.log(db, principal, "Leave", "COMP_OFF_CLAIMED", f"Comp-off claim for {worked_date}", "comp_off_claim",
                        row.id)
    return row


def _load(db: Session, principal: Principal, claim_id: int, *, lock: bool = False) -> CompOffClaim:
    query = db.query(CompOffClaim).filter(CompOffClaim.company_id == principal.company_id, CompOffClaim.id == claim_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Comp-off claim")
    return row


def _can_decide(db: Session, principal: Principal, row: CompOffClaim) -> bool:
    return row.employee_id != principal.employee_id and principal.has("leave:approve") and authz.can_access_employee(
        db, principal, "leave:approve", row.employee_id)


def decide(db: Session, principal: Principal, claim_id: int, approve: bool, note: Optional[str]) -> CompOffClaim:
    row = _load(db, principal, claim_id, lock=True)
    if not _can_decide(db, principal, row):
        if row.employee_id == principal.employee_id:
            raise forbidden("You cannot approve your own claim.")
        if not authz.can_access_employee(db, principal, "leave:read", row.employee_id):
            raise not_found("Comp-off claim")
        raise forbidden("This claim is not awaiting your approval.")
    if row.status != "PENDING":
        raise AppError(409, "This claim was already decided.", "INVALID_TRANSITION")
    if not approve and not (note or "").strip():
        raise bad_request("Give a reason for rejecting the claim.", "REASON_REQUIRED")
    employee = db.get(Employee, row.employee_id)
    row.status = "APPROVED" if approve else "REJECTED"
    row.decided_by_user_id, row.decided_at, row.decision_note = principal.user_id, utcnow(), (note or "").strip() or None
    row.row_version += 1
    if approve:
        from app.services import leave_requests

        today = leave_requests.local_today(db, employee)
        rules, _, resolution = _plan_rules(db, row.company_id, employee, row.worked_date)
        validity = int(rules.get("validity_days") or 60)
        usable = int(rules.get("min_usable_days") or 14)
        row.expires_on = max(row.worked_date + timedelta(days=validity), today + timedelta(days=usable))
        leave_ledger.post(db, row.company_id, employee_id=row.employee_id, type_code=row.leave_type_code,
                          delta=Decimal(row.credit_units), reason="COMP_OFF", key=f"compoff:{row.id}",
                          effective_date=today, source="COMP_OFF", ref_type="comp_off_claim", ref_id=row.id,
                          actor_user_id=principal.user_id, expires_on=row.expires_on,
                          policy_version_id=resolution.version_id,
                          note=f"Worked on {row.worked_date:%d %b %Y} ({row.worked_minutes} min)")
    if employee.user_id:
        NotificationService.create(db, employee.user_id, f"Comp-off claim {row.status.lower()}",
                                   f"Your comp-off claim for {row.worked_date:%d %b %Y} was {row.status.lower()}.",
                                   "leave", "/leaves/balances")
    AuditLogService.log(db, principal, "Leave", "COMP_OFF_" + row.status, f"Comp-off claim #{row.id} {row.status.lower()}",
                        "comp_off_claim", row.id)
    db.flush()
    return row


def withdraw(db: Session, principal: Principal, claim_id: int) -> CompOffClaim:
    row = _load(db, principal, claim_id, lock=True)
    if row.employee_id != principal.employee_id:
        raise not_found("Comp-off claim")
    if row.status != "PENDING":
        raise AppError(409, "Only a pending claim can be withdrawn.", "INVALID_TRANSITION")
    row.status = "WITHDRAWN"
    row.row_version += 1
    db.flush()
    return row


def revoke(db: Session, principal: Principal, claim_id: int, reason: str) -> CompOffClaim:
    if not principal.has("leave:balance_adjust"):
        raise forbidden("Revoking a comp-off needs the balance adjustment permission.")
    row = _load(db, principal, claim_id, lock=True)
    if not authz.can_access_employee(db, principal, "leave:balance_adjust", row.employee_id):
        raise not_found("Comp-off claim")
    if row.status != "APPROVED":
        raise AppError(409, "Only an approved claim can be revoked.", "INVALID_TRANSITION")
    if not (reason or "").strip():
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    from app.models.time_inputs import LeaveBalanceTransaction

    credit = db.query(LeaveBalanceTransaction).filter(LeaveBalanceTransaction.company_id == row.company_id,
                                                      LeaveBalanceTransaction.idempotency_key == f"compoff:{row.id}").first()
    remaining = ZERO
    if credit is not None:
        lot = next((l for l in leave_ledger.lots(db, row.company_id, row.employee_id, row.leave_type_code)
                    if l.txn_id == credit.id), None)
        remaining = max(lot.remaining, ZERO) if lot else ZERO
    if remaining > 0:
        leave_ledger.post(db, row.company_id, employee_id=row.employee_id, type_code=row.leave_type_code,
                          delta=-remaining, reason="EXPIRE", key=f"compoff:{row.id}:revoke", effective_date=row.worked_date,
                          source="COMP_OFF", ref_type="leave_balance_transaction", ref_id=credit.id,
                          actor_user_id=principal.user_id, note=f"Comp-off revoked: {reason.strip()[:200]}")
    row.status = "REVOKED"
    row.decision_note = reason.strip()
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, "Leave", "COMP_OFF_REVOKED", f"Revoked comp-off claim #{row.id}", "comp_off_claim",
                        row.id, details={"reversed_units": str(remaining)})
    return row


def list_claims(db: Session, principal: Principal, *, mine: bool = False, status: Optional[str] = None) -> List[dict]:
    query = db.query(CompOffClaim, Employee).join(
        Employee, (Employee.company_id == CompOffClaim.company_id) & (Employee.id == CompOffClaim.employee_id)
    ).filter(CompOffClaim.company_id == principal.company_id)
    if mine:
        query = query.filter(CompOffClaim.employee_id == principal.employee_id)
    else:
        read, approve = (authz.employee_scope(db, principal, code) for code in ("leave:read", "leave:approve"))
        scope = read if approve.is_empty else approve if read.is_empty else authz.EmployeeScope(
            all_company=read.all_company or approve.all_company, selects=list(read.selects) + list(approve.selects))
        query = query.filter(scope.clause(CompOffClaim.employee_id))
    if status:
        query = query.filter(CompOffClaim.status == status)
    out = []
    for row, person in query.order_by(CompOffClaim.id.desc()).limit(200).all():
        item = claim_json(row, person.full_name)
        item["can_decide"] = row.status == "PENDING" and _can_decide(db, principal, row)
        out.append(item)
    return out
