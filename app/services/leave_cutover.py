"""Per-tenant leave cut-over LEGACY_V1 → V2_ACTIVE (02 §11.3, SPINE §6.11; 13 §7.3) and the setup checklist.

``reconcile`` (counts only, never writes) compares, for every active employee and limited type, today's legacy
balance with the v2 balance the cut-over would produce (a GRANT of the yearly quota for the current calendar year plus
the existing ledger rows). Any unexplained difference blocks go-live. ``go_live`` (company-wide leave configuration,
fresh step-up, typed confirmation) creates the "Default plan" (LEGACY_V1 rules), posts the MIGRATION grants and
switches ``leave.v2.enabled`` in one transaction. ``rollback`` (13 §8.2) switches back and posts compensating rows for
every grant the cut-over and the accrual job wrote, so legacy readers see exactly what they saw before.
"""

from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, forbidden
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.leave import ApprovalChainStep, LeaveType
from app.models.payroll_config import Policy
from app.models.time_inputs import LeaveBalanceTransaction
from app.services import leave_access, leave_ledger, leave_plans
from app.services.audit_log_service import AuditLogService
from app.services.leave_cycles import Cycle

ZERO = Decimal(0)
CONFIRM_TEXT = "GO LIVE"


def _require(principal: Principal) -> None:
    if not principal.has("leave:manage_policy", "COMPANY"):
        raise forbidden("The leave cut-over needs company-wide leave configuration access.")


def _people(db: Session, company_id: int, today: date) -> List[Employee]:
    return (db.query(Employee).filter(Employee.company_id == company_id, Employee.is_active.is_(True),
                                      Employee.hire_date <= today).order_by(Employee.id).all())


def _limited_types(db: Session, company_id: int) -> List[LeaveType]:
    return (db.query(LeaveType).filter(LeaveType.company_id == company_id, LeaveType.is_unlimited.is_(False),
                                       or_(LeaveType.kind.is_(None), LeaveType.kind == "COMP_OFF"))
            .order_by(LeaveType.id).all())


def reconcile(db: Session, company_id: int) -> dict:
    today = clock.company_today(db, company_id)
    year = Cycle(date(today.year, 1, 1), date(today.year, 12, 31))
    rows, unexplained, grants = [], 0, 0
    for person in _people(db, company_id, today):
        for lt in _limited_types(db, company_id):
            legacy = leave_ledger.legacy_view(db, company_id, person.id, lt, today.year)
            if lt.kind == "COMP_OFF":
                v2_balance = sum((lot.remaining for lot in leave_ledger.lots(db, company_id, person.id, lt.code)
                                  if lot.expires_on is None or lot.expires_on >= today), ZERO)
                legacy_balance = legacy.balance - Decimal(lt.annual_quota or 0)
                explained = v2_balance != legacy_balance
                note = "Comp-off credits become expiring lots" if explained else None
            else:
                rows_of_year = db.query(func.coalesce(func.sum(LeaveBalanceTransaction.delta), 0)).filter(
                    LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.employee_id == person.id,
                    LeaveBalanceTransaction.leave_type == lt.code,
                    LeaveBalanceTransaction.effective_date >= year.start,
                    LeaveBalanceTransaction.effective_date <= year.end).scalar()
                v2_balance = Decimal(lt.annual_quota or 0) + Decimal(rows_of_year or 0)
                legacy_balance = legacy.balance
                explained, note = False, None
                if lt.annual_quota:
                    grants += 1
            diff = v2_balance - legacy_balance
            if diff != 0 and not explained:
                unexplained += 1
            if diff != 0 or legacy_balance < 0:
                rows.append({"employee_id": person.id, "name": person.full_name, "leave_type": lt.code,
                             "legacy": leave_ledger._num(legacy_balance), "v2": leave_ledger._num(v2_balance),
                             "difference": leave_ledger._num(diff), "explained": explained, "note": note,
                             "negative": legacy_balance < 0})
    return {"employees": len(_people(db, company_id, today)), "types": len(_limited_types(db, company_id)),
            "grants_to_post": grants, "differences": rows[:500], "unexplained": unexplained,
            "negative_balances": sum(1 for r in rows if r["negative"]), "year": today.year,
            "v2_enabled": leave_access.v2_enabled(db, company_id)}


def go_live(db: Session, principal: Principal, *, confirm: str) -> dict:
    _require(principal)
    from app.payroll.services.step_up import ensure_step_up

    ensure_step_up(db, principal)
    cid = principal.company_id
    if leave_access.v2_enabled(db, cid):
        raise AppError(409, "Leave v2 is already live for this company.", "INVALID_TRANSITION")
    if (confirm or "").strip().upper() != CONFIRM_TEXT:
        raise bad_request(f"Type {CONFIRM_TEXT} to confirm.", "CONFIRMATION_REQUIRED")
    report = reconcile(db, cid)
    if report["unexplained"]:
        raise AppError(409, f"{report['unexplained']} balance(s) differ; review the reconciliation first.",
                       "RECONCILIATION_FAILED", extra={"details": {"unexplained": report["unexplained"]}})
    today = clock.company_today(db, cid)
    year = Cycle(date(today.year, 1, 1), date(today.year, 12, 31))
    go_live_date = leave_access.setting(db, cid, "leave.go_live_date", default=None) or year.start
    leave_plans.ensure_default_plan(db, cid, min(year.start, go_live_date))
    posted = 0
    for person in _people(db, cid, today):
        for lt in _limited_types(db, cid):
            if lt.kind == "COMP_OFF" or not lt.annual_quota:
                continue
            if leave_ledger.post(db, cid, employee_id=person.id, type_code=lt.code, delta=Decimal(lt.annual_quota),
                                 reason="GRANT", key=f"grant:{person.id}:{lt.code}:{year.start.isoformat()}",
                                 effective_date=max(year.start, person.hire_date), cycle=year, source="MIGRATION",
                                 actor_user_id=principal.user_id, note="Migrated from the yearly quota"):
                posted += 1
    leave_access.set_v2_enabled(db, principal, True, reason="Leave cut-over")
    leave_plans._memo(db).clear()
    AuditLogService.log(db, principal, "Leave", "V2_ACTIVATED", "Leave v2 went live", "company", cid,
                        details={"grants": posted, "employees": report["employees"]})
    return {"status": "V2_ACTIVE", "grants_posted": posted, "report": report}


def rollback(db: Session, principal: Principal, *, confirm: str, reason: str) -> dict:
    _require(principal)
    from app.payroll.services.step_up import ensure_step_up

    ensure_step_up(db, principal)
    cid = principal.company_id
    if not leave_access.v2_enabled(db, cid):
        raise AppError(409, "Leave v2 is not live.", "INVALID_TRANSITION")
    if (confirm or "").strip().upper() != "ROLL BACK" or not (reason or "").strip():
        raise bad_request("Type ROLL BACK and give a reason.", "CONFIRMATION_REQUIRED")
    reversed_rows = 0
    for txn in db.query(LeaveBalanceTransaction).filter(
            LeaveBalanceTransaction.company_id == cid, LeaveBalanceTransaction.source.in_(("MIGRATION", "ACCRUAL_JOB")),
            LeaveBalanceTransaction.reason.in_(("GRANT", "ACCRUAL", "PRORATE"))).all():
        if leave_ledger.post(db, cid, employee_id=txn.employee_id, type_code=txn.leave_type, delta=-Decimal(txn.delta),
                             reason="ADJUST", key=f"{txn.idempotency_key}:rev", effective_date=txn.effective_date,
                             cycle=Cycle(txn.cycle_start, txn.cycle_end) if txn.cycle_start else None, source="SYSTEM",
                             ref_type="leave_balance_transaction", ref_id=txn.id, actor_user_id=principal.user_id,
                             note="Leave v2 rolled back"):
            reversed_rows += 1
    leave_access.set_v2_enabled(db, principal, False, reason=reason.strip())
    AuditLogService.log(db, principal, "Leave", "V2_ROLLED_BACK", "Leave v2 rolled back", "company", cid,
                        details={"reversed": reversed_rows, "reason": reason.strip()})
    return {"status": "LEGACY_V1", "reversed_rows": reversed_rows}


def setup(db: Session, principal: Principal) -> dict:
    """V3-116: the leave setup checklist with step statuses."""
    from app.models.org import Branch
    from app.models.pay_org import HolidayCalendar

    if not (principal.has("leave:manage_policy") or principal.has("leave:read", "COMPANY")):
        raise forbidden()
    cid = principal.company_id
    today = clock.company_today(db, cid)
    types = db.query(LeaveType).filter(LeaveType.company_id == cid).all()
    other = [t.code for t in types if t.is_active and t.category == "OTHER"]
    plans = db.query(Policy).filter(Policy.company_id == cid, Policy.type == "LEAVE_PLAN",
                                    Policy.status == "ACTIVE").all()
    v2 = leave_access.v2_enabled(db, cid)
    unassigned = []
    if v2:
        from app.payroll.services import policies

        unassigned = policies.employees_without_policy(db, cid, "LEAVE_PLAN", on=today)
    chain = db.query(func.count(ApprovalChainStep.id)).filter(ApprovalChainStep.company_id == cid,
                                                              ApprovalChainStep.module == "leave").scalar()
    calendars = db.query(func.count(HolidayCalendar.id)).filter(HolidayCalendar.company_id == cid).scalar()
    branches = db.query(func.count(Branch.id)).filter(Branch.company_id == cid).scalar()
    steps = [
        {"key": "types", "title": "Review leave types", "status": "DONE" if types and not other else "TODO",
         "detail": f"{len(types)} type(s)" + (f"; {len(other)} without a category: {', '.join(other[:5])}" if other else ""),
         "link": "/settings/leave/types"},
        {"key": "plans", "title": "Build leave plans", "status": "DONE" if plans else "TODO",
         "detail": f"{len(plans)} plan(s)", "link": "/settings/leave/plans"},
        {"key": "approvals", "title": "Set the approval route", "status": "DONE" if chain else "OPTIONAL",
         "detail": f"{chain} step(s); without steps the reporting manager approves" if chain else
                   "Without steps the company administrators approve.", "link": "/leaves/policy"},
        {"key": "calendars", "title": "Holiday calendars and work weeks", "status": "DONE" if calendars else "TODO",
         "detail": f"{calendars} calendar(s) for {branches} branch(es)", "link": "/holidays/calendars"},
        {"key": "go_live", "title": "Go live with leave plans and the ledger",
         "status": "DONE" if v2 else ("BLOCKED" if not plans else "READY"),
         "detail": "Live: balances come from the ledger." if v2 else
                   "Reconcile balances, then go live (a fresh identity check is required).",
         "link": "/settings/leave/setup"},
    ]
    if v2:
        steps.append({"key": "coverage", "title": "Everyone has a leave plan",
                      "status": "DONE" if not unassigned else "TODO",
                      "detail": f"{len(unassigned)} employee(s) without a plan", "link": "/settings/leave/plans"})
    return {"v2_enabled": v2, "state": "V2_ACTIVE" if v2 else "LEGACY_V1", "steps": steps, "unassigned": unassigned[:50]}
