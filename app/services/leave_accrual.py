"""Entitlement, accrual, expiry and year end for V2_ACTIVE tenants (SPINE §6.5, §6.7, §6.9; 03-leave/02).

``accrue_employee`` posts, per type rule of the employee's plan, the difference between what the rule says was earned
in the current cycle up to today and what was already credited (cumulative method, so reruns and catch-up after a
stopped scheduler post each missing unit exactly once; keys name the transition ``{posted}->{earned}``). Joiners,
leavers, probation BLOCK and NO_ENTITLEMENT tenure start the entitlement later; ``max_balance`` and ``accrual_cap``
stop credits. ``expire_lots`` writes one EXPIRE per lot remainder after its ``expires_on``.

Year end (§6.7): ``preview_year_end`` computes per employee and type the balance at the cycle end, the carry-forward
(capped), the YEAR_END encashment (capped) and the lapse; ``post_year_end`` (a different person than the maker unless
AUTO_POST) writes CARRY_FORWARD out of the old cycle and into the next as a lot with its expiry, LAPSE, and an approved
YEAR_END encashment request that payroll pays once. ``reverse_year_end`` posts compensating rows.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import LeaveType
from app.models.leave_v2 import LeaveEncashmentRequest, LeaveYearEndRun
from app.models.time_inputs import LeaveBalanceTransaction
from app.services import leave_access, leave_ledger, leave_plans
from app.services.audit_log_service import AuditLogService
from app.services.leave_cycles import Cycle, add_months, cycle_for, earned_to_date

ZERO = Decimal(0)
CREDIT_SOURCES = ("ACCRUAL_JOB", "MIGRATION")


def _start_of_entitlement(employee: Employee, rule: dict) -> date:
    start = employee.hire_date
    if (rule.get("during_probation") or "ALLOW") == "BLOCK" and employee.probation_end_date:
        end = employee.probation_end_date
        if employee.confirmation_date is not None:
            end = min(end, employee.confirmation_date - timedelta(days=1))
        start = max(start, end + timedelta(days=1))
    tenure = (rule.get("eligibility") or {}).get("min_tenure")
    if tenure and tenure.get("mode") == "NO_ENTITLEMENT":
        reached = leave_plans._tenure_reached(employee, tenure)
        if reached is not None:
            start = max(start, reached)
    return start


def credited(db: Session, company_id: int, employee_id: int, code: str, cycle: Cycle) -> Decimal:
    rows = db.query(LeaveBalanceTransaction).filter(
        LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.employee_id == employee_id,
        LeaveBalanceTransaction.leave_type == code, LeaveBalanceTransaction.reason.in_(("GRANT", "ACCRUAL", "PRORATE")),
        LeaveBalanceTransaction.source.in_(CREDIT_SOURCES)).all()
    return sum((Decimal(r.delta) for r in rows if leave_ledger._in_cycle(r, cycle)), ZERO)


def accrue_employee(db: Session, company_id: int, employee: Employee, today: date) -> int:
    resolution = leave_plans.resolve(db, company_id, employee.id, today)
    if resolution.status != "RESOLVED":
        return 0
    types = {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == company_id)}
    posted = 0
    for rule in resolution.fields.get("types", []):
        lt = types.get(rule["leave_type_code"])
        ent = rule.get("entitlement") or {}
        if lt is None or lt.is_unlimited or rule.get("frozen") or not lt.is_active:
            continue
        if ent.get("method") not in ("FRONT_LOADED", "PERIODIC", "TENURE_SLABS"):
            continue
        elig = leave_plans.eligibility(db, company_id, employee, lt, rule, today)
        if elig.status == "NOT_ELIGIBLE" and elig.structural:
            continue
        cycle = leave_plans.cycle_of(db, company_id, employee, rule, today, resolution)
        earned = earned_to_date(ent, cycle, today, joined=_start_of_entitlement(employee, rule),
                                left=employee.termination_date,
                                joiner=(rule.get("proration") or {}).get("joiner", "PRORATE"),
                                leaver=(rule.get("proration") or {}).get("leaver", "PRORATE"),
                                service_start=employee.service_start_date or employee.hire_date,
                                rounding=(rule.get("rounding") or {}).get("mode", "NONE_2DP"))
        have = credited(db, company_id, employee.id, lt.code, cycle)
        delta = earned - have
        if delta > 0:
            view = leave_ledger.balance(db, company_id, employee, lt, today, rule=rule, resolution=resolution)
            for limit in (rule.get("max_balance"), rule.get("accrual_cap")):
                if limit is not None:
                    delta = min(delta, max(Decimal(str(limit)) - view.balance, ZERO))
        if delta == 0:
            continue
        reason = "PRORATE" if delta < 0 else ("GRANT" if ent["method"] != "PERIODIC" else "ACCRUAL")
        months = ent.get("credit_expiry_months")
        expires = add_months(today, int(months)) if months and delta > 0 else None
        if leave_ledger.post(db, company_id, employee_id=employee.id, type_code=lt.code, delta=delta, reason=reason,
                             key=f"accrual:{employee.id}:{lt.code}:{cycle.start.isoformat()}:{have}->{earned}",
                             effective_date=max(today if ent["method"] == "PERIODIC" else cycle.start,
                                                employee.hire_date),
                             cycle=cycle, source="ACCRUAL_JOB", expires_on=expires,
                             policy_version_id=resolution.version_id,
                             note=f"{ent['method'].replace('_', ' ').title()} entitlement {earned} for "
                                  f"{cycle.start:%d %b %Y}–{cycle.end:%d %b %Y}"):
            posted += 1
    return posted


def expire_lots(db: Session, company_id: int, employee: Employee, today: date) -> int:
    codes = [c for (c,) in db.query(LeaveBalanceTransaction.leave_type).filter(
        LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.employee_id == employee.id,
        LeaveBalanceTransaction.expires_on.isnot(None), LeaveBalanceTransaction.expires_on < today).distinct()]
    posted = 0
    for code in codes:
        for lot in leave_ledger.lots(db, company_id, employee.id, code):
            if lot.expires_on is None or lot.expires_on >= today or lot.remaining <= 0:
                continue
            if leave_ledger.post(db, company_id, employee_id=employee.id, type_code=code, delta=-lot.remaining,
                                 reason="EXPIRE", key=f"expire:{lot.txn_id}", effective_date=lot.expires_on + timedelta(days=1),
                                 source="ACCRUAL_JOB", ref_type="leave_balance_transaction", ref_id=lot.txn_id,
                                 note=f"Credit of {lot.credited_on:%d %b %Y} expired on {lot.expires_on:%d %b %Y}"):
                posted += 1
    return posted


def run_tenant(db: Session, company_id: int, today: Optional[date] = None) -> dict:
    if not leave_access.v2_enabled(db, company_id):
        return {"skipped": "LEGACY_V1"}
    today = today or clock.company_today(db, company_id)
    people = (db.query(Employee).filter(Employee.company_id == company_id, Employee.hire_date <= today,
                                        (Employee.termination_date.is_(None)) | (Employee.termination_date >= today - timedelta(days=31)))
              .order_by(Employee.id).all())
    credits = expiries = 0
    for person in people:
        credits += accrue_employee(db, company_id, person, today)
        expiries += expire_lots(db, company_id, person, today)
    from app.services import leave_events

    closed = leave_events.close_finished(db, company_id, today)
    db.flush()
    return {"employees": len(people), "credits": credits, "expiries": expiries, "events_closed": closed,
            "date": today.isoformat()}


def _jobs():
    from app.payroll.services import jobs

    return jobs


@_jobs().register("leave.accrue")
def _accrue_job(ctx) -> dict:
    payload = ctx.payload or {}
    day = date.fromisoformat(payload["local_date"]) if payload.get("local_date") else None
    return run_tenant(ctx.db, ctx.company_id, day)


# --------------------------------------------------------------------------- year end (§6.7)

def _require_year_end(principal: Principal) -> None:
    if not principal.has("leave:year_end"):
        raise forbidden("Year end needs the leave year-end permission.")


def preview_year_end(db: Session, principal: Principal, cycle_end: date, *, legal_entity_id: Optional[int] = None
                     ) -> LeaveYearEndRun:
    """DRAFT run: per (employee, type) whose cycle ends on ``cycle_end``, the carry / encash / lapse split."""
    _require_year_end(principal)
    leave_access.require_v2(db, principal.company_id)
    cid = principal.company_id
    if db.query(LeaveYearEndRun.id).filter(LeaveYearEndRun.company_id == cid, LeaveYearEndRun.cycle_end == cycle_end,
                                           LeaveYearEndRun.status == "POSTED",
                                           LeaveYearEndRun.legal_entity_id.is_(legal_entity_id) if legal_entity_id is None
                                           else LeaveYearEndRun.legal_entity_id == legal_entity_id).first():
        raise AppError(409, "This year end is already posted.", "YEAR_END_RUN_EXISTS")
    types = {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == cid)}
    rows, totals = [], {"employees": 0, "carry": ZERO, "encash": ZERO, "lapse": ZERO, "negative": 0}
    people = db.query(Employee).filter(Employee.company_id == cid, Employee.hire_date <= cycle_end,
                                       (Employee.termination_date.is_(None)) | (Employee.termination_date > cycle_end)
                                       ).order_by(Employee.id).all()
    for person in people:
        where = leave_plans.placement(db, cid, person.id, cycle_end)
        if legal_entity_id is not None and (where is None or where.legal_entity_id != legal_entity_id):
            continue
        # §6.7 step 1: the cycle's final accruals are posted first (catch-up up to the cycle end).
        accrue_employee(db, cid, person, min(cycle_end, clock.company_today(db, cid)))
        resolution = leave_plans.resolve(db, cid, person.id, cycle_end)
        counted = False
        for rule in resolution.fields.get("types", []):
            lt = types.get(rule["leave_type_code"])
            if lt is None or lt.is_unlimited or ((rule.get("balance_scope") or {}).get("kind") or "CYCLE") != "CYCLE":
                continue
            cycle = leave_plans.cycle_of(db, cid, person, rule, cycle_end, resolution)
            if cycle.end != cycle_end:
                continue
            view = leave_ledger.balance(db, cid, person, lt, cycle_end, rule=rule, resolution=resolution)
            bal = view.balance
            cap = (rule.get("carry_forward") or {}).get("cap")
            enc = rule.get("encashment") or {}
            if bal < 0:
                carry, encash, lapse = (bal, ZERO, ZERO) if (rule.get("negative_at_cycle_end") or "CARRY_NEGATIVE") \
                    == "CARRY_NEGATIVE" else (ZERO, ZERO, bal)
                totals["negative"] += 1
            else:
                carry = bal if cap is None else min(bal, Decimal(str(cap)))
                excess = bal - carry
                encash = ZERO
                if enc.get("eligible") and "YEAR_END" in (enc.get("triggers") or []):
                    limit = enc.get("max_units_per_cycle")
                    encash = excess if limit is None else min(excess, Decimal(str(limit)))
                lapse = excess - encash
            if bal == 0 and carry == 0:
                continue
            counted = True
            rows.append({"employee_id": person.id, "name": person.full_name, "leave_type": lt.code,
                         "cycle_start": cycle.start.isoformat(), "balance": str(bal), "carry": str(carry),
                         "encash": str(encash), "lapse": str(lapse),
                         "carry_expiry_months": (rule.get("carry_forward") or {}).get("expiry_months"),
                         "policy_version_id": resolution.version_id})
            totals["carry"] += carry if carry > 0 else ZERO
            totals["encash"] += encash
            totals["lapse"] += lapse if lapse > 0 else ZERO
        totals["employees"] += 1 if counted else 0
    run = (db.query(LeaveYearEndRun).filter(LeaveYearEndRun.company_id == cid, LeaveYearEndRun.cycle_end == cycle_end,
                                            LeaveYearEndRun.status == "DRAFT").first())
    if run is None:
        run = LeaveYearEndRun(company_id=cid, cycle_end=cycle_end, legal_entity_id=legal_entity_id,
                              created_by_user_id=principal.user_id)
        db.add(run)
    run.rows = rows
    run.summary = {k: str(v) if isinstance(v, Decimal) else v for k, v in totals.items()} | {
        "early_preview": clock.company_today(db, cid) <= cycle_end, "rows": len(rows)}
    run.created_by_user_id = principal.user_id
    run.row_version = (run.row_version or 0) + 1
    db.flush()
    AuditLogService.log(db, principal, "Leave", "YEAR_END_PREVIEWED", f"Year-end preview for {cycle_end}", "leave_year_end",
                        run.id, details={"rows": len(rows)})
    return run


def post_year_end(db: Session, principal: Principal, run_id: int) -> LeaveYearEndRun:
    _require_year_end(principal)
    cid = principal.company_id
    run = db.query(LeaveYearEndRun).filter(LeaveYearEndRun.company_id == cid, LeaveYearEndRun.id == run_id
                                           ).with_for_update().first()
    if run is None:
        raise not_found("Year-end run")
    if run.status != "DRAFT":
        raise AppError(409, "Only a draft year end can be posted.", "INVALID_TRANSITION")
    if clock.company_today(db, cid) <= run.cycle_end:
        raise AppError(409, "The leave year has not ended yet (early preview).", "CYCLE_NOT_ENDED")
    mode = leave_access.setting(db, cid, "leave.year_end.mode", default="MANUAL_REVIEW")
    if mode == "MANUAL_REVIEW" and run.created_by_user_id == principal.user_id:
        raise AppError(403, {"message": "A different person posts the year end.", "details": {"cause": "MAKER"}},
                       "SELF_DECISION_FORBIDDEN")
    for row in run.rows:
        emp, code = int(row["employee_id"]), row["leave_type"]
        old = Cycle(date.fromisoformat(row["cycle_start"]), run.cycle_end)
        person = db.get(Employee, emp)
        resolution = leave_plans.resolve(db, cid, emp, run.cycle_end + timedelta(days=1))
        rule = resolution.rule(code)
        new = leave_plans.cycle_of(db, cid, person, rule, run.cycle_end + timedelta(days=1), resolution)
        carry, encash, lapse = Decimal(row["carry"]), Decimal(row["encash"]), Decimal(row["lapse"])
        base = f"ye:{run.id}:{emp}:{code}"
        if carry:
            months = row.get("carry_expiry_months")
            expires = add_months(new.start, int(months)) - timedelta(days=1) if months else None
            leave_ledger.post(db, cid, employee_id=emp, type_code=code, delta=-carry, reason="CARRY_FORWARD",
                              key=f"cf:{emp}:{code}:{old.start.isoformat()}:out", effective_date=old.end, cycle=old,
                              source="YEAR_END", ref_type="leave_year_end", ref_id=run.id,
                              note="Carried to the next leave year")
            leave_ledger.post(db, cid, employee_id=emp, type_code=code, delta=carry, reason="CARRY_FORWARD",
                              key=f"cf:{emp}:{code}:{old.start.isoformat()}:in", effective_date=new.start, cycle=new,
                              source="YEAR_END", ref_type="leave_year_end", ref_id=run.id, expires_on=expires,
                              note=f"Carried from {old.start:%Y}")
        if encash > 0:
            request = LeaveEncashmentRequest(company_id=cid, employee_id=emp, leave_type_code=code,
                                             cycle_start=old.start, units=encash, trigger="YEAR_END", status="APPROVED",
                                             decided_by_user_id=principal.user_id, decided_at=utcnow(),
                                             decision_note="Year-end encashment", year_end_run_id=run.id)
            db.add(request)
            db.flush()
            from app.services import leave_encashment

            leave_encashment.send_to_payroll(db, request, principal.user_id)
        if lapse:
            leave_ledger.post(db, cid, employee_id=emp, type_code=code, delta=-lapse, reason="LAPSE",
                              key=f"lapse:{emp}:{code}:{old.end.isoformat()}", effective_date=old.end, cycle=old,
                              source="YEAR_END", ref_type="leave_year_end", ref_id=run.id,
                              note="Unused leave lapsed at year end")
        del base
    run.status, run.posted_by_user_id, run.posted_at = "POSTED", principal.user_id, utcnow()
    db.flush()
    AuditLogService.log(db, principal, "Leave", "YEAR_END_POSTED", f"Posted year end {run.cycle_end}", "leave_year_end",
                        run.id, details=run.summary)
    return run


def reverse_year_end(db: Session, principal: Principal, run_id: int) -> LeaveYearEndRun:
    _require_year_end(principal)
    cid = principal.company_id
    run = db.query(LeaveYearEndRun).filter(LeaveYearEndRun.company_id == cid, LeaveYearEndRun.id == run_id
                                           ).with_for_update().first()
    if run is None:
        raise not_found("Year-end run")
    if run.status != "POSTED":
        raise AppError(409, "Only a posted year end can be reversed.", "INVALID_TRANSITION")
    paid = db.query(LeaveEncashmentRequest.id).filter(LeaveEncashmentRequest.company_id == cid,
                                                      LeaveEncashmentRequest.year_end_run_id == run.id,
                                                      LeaveEncashmentRequest.status == "PAID").first()
    if paid:
        raise AppError(409, "Payroll already paid this year end's encashment.", "INVALID_TRANSITION")
    for txn in db.query(LeaveBalanceTransaction).filter(LeaveBalanceTransaction.company_id == cid,
                                                        LeaveBalanceTransaction.ref_type == "leave_year_end",
                                                        LeaveBalanceTransaction.ref_id == run.id).all():
        leave_ledger.post(db, cid, employee_id=txn.employee_id, type_code=txn.leave_type, delta=-Decimal(txn.delta),
                          reason=txn.reason, key=f"{txn.idempotency_key}:rev", effective_date=txn.effective_date,
                          cycle=Cycle(txn.cycle_start, txn.cycle_end) if txn.cycle_start else None, source="YEAR_END",
                          ref_type="leave_year_end_reversal", ref_id=run.id, note="Year end reversed")
    for request in db.query(LeaveEncashmentRequest).filter(LeaveEncashmentRequest.company_id == cid,
                                                           LeaveEncashmentRequest.year_end_run_id == run.id,
                                                           LeaveEncashmentRequest.status == "APPROVED").all():
        from app.services import leave_encashment

        leave_encashment.withdraw_from_payroll(db, request)
        request.status = "CANCELLED"
    run.status, run.reversed_by_user_id, run.reversed_at = "REVERSED", principal.user_id, utcnow()
    db.flush()
    AuditLogService.log(db, principal, "Leave", "YEAR_END_REVERSED", f"Reversed year end {run.cycle_end}",
                        "leave_year_end", run.id)
    return run


def run_json(run: LeaveYearEndRun) -> dict:
    return {"id": run.id, "cycle_end": run.cycle_end.isoformat(), "status": run.status,
            "legal_entity_id": run.legal_entity_id, "summary": run.summary, "rows": run.rows,
            "created_by_user_id": run.created_by_user_id, "posted_by_user_id": run.posted_by_user_id,
            "posted_at": run.posted_at.isoformat() if run.posted_at else None,
            "reversed_at": run.reversed_at.isoformat() if run.reversed_at else None}
