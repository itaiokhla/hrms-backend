"""Approved leave → ``leave_days`` and ledger USE rows for V2_ACTIVE tenants (SPINE §6.1, §6.4, §8).

``generate`` writes one ``leave_days`` row per employed date (charged or not, as payroll reads them today) with the
plan's charge reason and counting basis, then ``sync_ledger`` makes the leave's USE rows equal, per balance scope
(cycle, or LOTS), to minus the charged units of its active rows. Every later change (cancel, partial cancel, modify,
convert, recount) voids / regenerates rows and calls ``sync_ledger`` again with the new request version, so the ledger
always nets to what is booked, exactly once (keys ``leave:{id}:v{version}:...``). EVENT leave writes no ledger rows:
its charge is kept on ``leave_events.used_units`` (``leave_events.sync_used``). LEGACY_V1 tenants keep the legacy
writers of ``time_inputs`` untouched.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.errors import bad_request, conflict
from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import Leave, LeaveType
from app.models.time_inputs import LeaveBalanceTransaction, LeaveDay
from app.services import leave_charging, leave_ledger, leave_plans
from app.services.leave_cycles import Cycle

ZERO = Decimal(0)


def _active_rows(db: Session, leave: Leave) -> List[LeaveDay]:
    return (db.query(LeaveDay).filter(LeaveDay.company_id == leave.company_id, LeaveDay.leave_id == leave.id,
                                      LeaveDay.voided_at.is_(None)).order_by(LeaveDay.date, LeaveDay.portion).all())


def scope_kind(rule: Optional[dict], leave_type: Optional[LeaveType]) -> str:
    """CYCLE, LOTS or EVENT: the rule's balance scope, else the type's natural one."""
    kind = ((rule or {}).get("balance_scope") or {}).get("kind")
    if kind:
        return kind
    if leave_type is not None and leave_type.kind == "COMP_OFF":
        return "LOTS"
    if leave_type is not None and leave_type.kind == "EVENT":
        return "EVENT"
    return "CYCLE"


def _scope_key(db: Session, leave: Leave, employee: Employee, rule: Optional[dict], day: date,
               leave_type: LeaveType) -> Optional[Cycle]:
    if scope_kind(rule, leave_type) in ("LOTS", "EVENT"):
        return None
    return leave_plans.cycle_of(db, leave.company_id, employee, rule, day)


def sync_ledger(db: Session, leave: Leave, *, actor_user_id: Optional[int] = None, note: Optional[str] = None) -> int:
    """Post the difference between the booked charge and the leave's USE rows, per scope. Returns rows posted.

    EVENT leave writes no ledger rows (03-leave/06 §6.1, V3-053(7)): its charge is kept on the event
    (``leave_events.used_units``, recomputed here), so a CONVERT into or out of an EVENT type moves the units between
    the ledger and the event exactly once."""
    from app.services import leave_events

    employee = db.get(Employee, leave.employee_id)
    leave_type = db.query(LeaveType).filter(LeaveType.company_id == leave.company_id,
                                            LeaveType.code == leave.leave_type).first()
    rule = leave_plans.rules_for(db, leave.company_id, leave.employee_id, leave.leave_type, leave.start_date)
    event_scope = scope_kind(rule, leave_type) == "EVENT"
    # Keyed by (type, scope): a CONVERT moves the charge from the old type's rows to the new type's.
    Key = Tuple[str, Optional[date]]
    wanted: Dict[Key, Decimal] = defaultdict(lambda: ZERO)
    cycles: Dict[Key, Optional[Cycle]] = {}
    first: Dict[Key, date] = {}
    for row in _active_rows(db, leave):
        if not row.counts_against_balance or event_scope:
            continue
        cycle = _scope_key(db, leave, employee, rule, row.date, leave_type)
        key = (leave.leave_type, cycle.start if cycle else None)
        cycles[key] = cycle
        wanted[key] -= Decimal(row.units)
        first[key] = min(first.get(key, row.date), row.date)
    have: Dict[Key, Decimal] = defaultdict(lambda: ZERO)
    types: Dict[str, Optional[LeaveType]] = {leave.leave_type: leave_type}
    for txn in db.query(LeaveBalanceTransaction).filter(
            LeaveBalanceTransaction.company_id == leave.company_id, LeaveBalanceTransaction.ref_type == "leave",
            LeaveBalanceTransaction.ref_id == leave.id, LeaveBalanceTransaction.reason == "USE"):
        if txn.leave_type not in types:
            types[txn.leave_type] = db.query(LeaveType).filter(LeaveType.company_id == leave.company_id,
                                                               LeaveType.code == txn.leave_type).first()
        if txn.cycle_start is not None:
            key = (txn.leave_type, txn.cycle_start)
            cycles.setdefault(key, Cycle(txn.cycle_start, txn.cycle_end) if txn.cycle_end else None)
        else:  # legacy rows: the calendar-year cycle of their date (or LOTS for comp-off)
            other = types[txn.leave_type]
            txn_rule = leave_plans.rules_for(db, leave.company_id, leave.employee_id, txn.leave_type, txn.effective_date)
            cycle = _scope_key(db, leave, employee, txn_rule, txn.effective_date, other) if other else None
            key = (txn.leave_type, cycle.start if cycle else None)
            cycles.setdefault(key, cycle)
        have[key] += Decimal(txn.delta)
        first.setdefault(key, txn.effective_date)
    posted = 0
    for key in sorted(set(wanted) | set(have), key=lambda k: (k[0], k[1] or date.min)):
        delta = wanted[key] - have[key]
        if delta == 0:
            continue
        code, scope = key
        label = (scope.isoformat() if scope else "lots") + ("" if code == leave.leave_type else f":{code}")
        kind = "use" if delta < 0 else "cancel"
        n = db.query(LeaveBalanceTransaction.id).filter(
            LeaveBalanceTransaction.company_id == leave.company_id,
            LeaveBalanceTransaction.idempotency_key.like(f"leave:{leave.id}:v{leave.version}:{kind}:{label}%")).count()
        suffix = f":{n + 1}" if n else ""
        if leave_ledger.post(db, leave.company_id, employee_id=leave.employee_id, type_code=code,
                             delta=delta, reason="USE", key=f"leave:{leave.id}:v{leave.version}:{kind}:{label}{suffix}",
                             effective_date=first.get(key, leave.start_date), cycle=cycles.get(key),
                             source="LEAVE", ref_type="leave", ref_id=leave.id, actor_user_id=actor_user_id,
                             note=note, policy_version_id=leave.plan_version_id):
            posted += 1
    leave_events.sync_used(db, leave.company_id, leave.event_id)
    return posted


def generate(db: Session, leave: Leave, actor_user_id: Optional[int] = None) -> List[LeaveDay]:
    """At approval (V2_ACTIVE): rows per employed date under the plan's counting basis and sandwich, then the ledger."""
    from app.payroll.services import time_inputs
    from app.core import concurrency

    existing = _active_rows(db, leave)
    if existing:
        return existing
    leave_type = db.query(LeaveType).filter(LeaveType.company_id == leave.company_id,
                                            LeaveType.code == leave.leave_type).first()
    if leave_type is None:
        raise bad_request("The leave type no longer exists.", "INVALID_LEAVE_TYPE")
    concurrency.lock_employees(db, [leave.employee_id])  # X-INT-56: a long wait is 409 LOCK_TIMEOUT, never a hang
    rule = leave_plans.rules_for(db, leave.company_id, leave.employee_id, leave.leave_type, leave.start_date)
    total, days = leave_charging.charge(db, leave.company_id, leave.employee_id, rule, leave.start_date,
                                        leave.end_date, leave.start_portion or "FULL", leave.end_portion or "FULL",
                                        exclude_leave_id=leave.id)
    basis = ((rule or {}).get("counting_basis") or {}).get("basis") or "WORKING_DAYS"
    snapshot = time_inputs.pay_treatment_snapshot(db, leave.company_id, leave.employee_id, leave_type,
                                                  leave.start_date)
    charges_off_days = any(d.charge_reason in ("BASIS", "SANDWICH") for d in days)
    snapshot = dict(snapshot) | {"counts_non_working_days": bool(charges_off_days or leave_type.counts_non_working_days)}
    rows = []
    for day in days:
        if not day.employed:
            continue
        rows.append(LeaveDay(company_id=leave.company_id, leave_id=leave.id, employee_id=leave.employee_id,
                             date=day.date, portion=day.portion, units=day.units,
                             counts_against_balance=day.counted, pay_treatment_snapshot=snapshot,
                             charge_reason=day.charge_reason, counting_basis=basis))
    db.add_all(rows)
    try:
        with db.begin_nested():
            db.flush()
    except DBAPIError:
        raise conflict("Another approved leave already covers some of these dates.", "LEAVE_OVERLAP")
    leave.number_of_days = total
    sync_ledger(db, leave, actor_user_id=actor_user_id)
    time_inputs.log_change(db, leave.company_id, record_type="leave", record_id=leave.id, change="CREATED",
                           content=time_inputs.leave_content(leave, rows), employee_id=leave.employee_id,
                           date_from=leave.start_date, date_to=leave.end_date, actor_user_id=actor_user_id)
    return rows


def void(db: Session, leave: Leave, reason: str, actor_user_id: Optional[int] = None, *,
         dates: Optional[set] = None) -> List[LeaveDay]:
    """Void every active row (or only ``dates``), give the charge back through the ledger, log the change."""
    from app.payroll.services import time_inputs

    rows = [r for r in _active_rows(db, leave) if dates is None or r.date in dates]
    now = utcnow()
    for row in rows:
        row.voided_at, row.void_reason = now, reason[:30]
    db.flush()
    sync_ledger(db, leave, actor_user_id=actor_user_id, note=f"Leave {reason.lower().replace('_', ' ')}")
    time_inputs.log_change(db, leave.company_id, record_type="leave", record_id=leave.id,
                           change="VOIDED" if dates is None else "UPDATED",
                           content=time_inputs.leave_content(leave, _active_rows(db, leave) or rows),
                           employee_id=leave.employee_id, date_from=min((r.date for r in rows), default=leave.start_date),
                           date_to=max((r.date for r in rows), default=leave.end_date), actor_user_id=actor_user_id)
    return rows


def regenerate(db: Session, leave: Leave, actor_user_id: Optional[int] = None, *, note: str = "Leave changed"
               ) -> List[LeaveDay]:
    """After a change of dates, portions or type: void all rows, write them again for the current request, re-sync."""
    rows = _active_rows(db, leave)
    now = utcnow()
    for row in rows:
        row.voided_at, row.void_reason = now, "CHANGED"
    db.flush()
    return generate(db, leave, actor_user_id)
