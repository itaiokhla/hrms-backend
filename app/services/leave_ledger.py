"""Leave ledger v2 and the single balance reader (SPINE §6.4-§6.6, §6.11; 03-leave/02 §8-§10).

``post`` is the one writer of ``leave_balance_transactions`` for leave v2: append-only, once per idempotency key,
with the balance scope (cycle) stamped on the row. Credits are positive (OPENING, GRANT, ACCRUAL, CARRY_FORWARD,
COMP_OFF; PRORATE and ADJUST either way); debits negative (USE, ENCASH, LAPSE, EXPIRE, PENALTY); a cancellation
writes a positive USE.

``balance`` is the only v2 reader: for a CYCLE type it sums the rows of the cycle containing the date (legacy rows
without a cycle belong to the calendar year of their effective date); for a LOTS type (comp-off) it replays credits as
lots consumed earliest-expiry first. ``reserved`` = units of PENDING requests and of approved, unpaid encashments;
``available = balance - reserved``. Nothing is clamped: a negative balance is shown as negative.

``balance_view`` dispatches: LEGACY_V1 tenants keep the legacy formula (yearly quota), V2_ACTIVE tenants read here.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.employee import Employee
from app.models.leave import Leave, LeaveType
from app.models.leave_v2 import LeaveEncashmentRequest
from app.models.time_inputs import LeaveBalanceTransaction
from app.services import leave_access, leave_plans
from app.services.leave_cycles import Cycle

ZERO = Decimal(0)
CREDIT_REASONS = ("OPENING", "GRANT", "ACCRUAL", "CARRY_FORWARD", "COMP_OFF")
DEBIT_REASONS = ("USE", "ENCASH", "LAPSE", "EXPIRE", "PENALTY")


def post(db: Session, company_id: int, *, employee_id: int, type_code: str, delta: Decimal, reason: str, key: str,
         effective_date: date, cycle: Optional[Cycle] = None, source: str = "LEAVE", ref_type: Optional[str] = None,
         ref_id: Optional[int] = None, actor_user_id: Optional[int] = None, note: Optional[str] = None,
         expires_on: Optional[date] = None, policy_version_id: Optional[int] = None) -> Optional[LeaveBalanceTransaction]:
    """Append one ledger row once per key; None when the key was already used or the delta is zero."""
    delta = Decimal(delta)
    if delta == 0:
        return None
    key = key[:120]
    if db.query(LeaveBalanceTransaction.id).filter(LeaveBalanceTransaction.company_id == company_id,
                                                   LeaveBalanceTransaction.idempotency_key == key).first():
        return None
    row = LeaveBalanceTransaction(
        company_id=company_id, employee_id=employee_id, leave_type=type_code, delta=delta, reason=reason,
        idempotency_key=key, effective_date=effective_date, ref_type=ref_type, ref_id=ref_id,
        created_by_user_id=actor_user_id, note=(note or None) and note[:500], expires_on=expires_on,
        cycle_start=cycle.start if cycle else None, cycle_end=cycle.end if cycle else None, source=source,
        policy_version_id=policy_version_id)
    db.add(row)
    db.flush()
    return row


def _rows(db: Session, company_id: int, employee_id: int, type_code: str) -> List[LeaveBalanceTransaction]:
    return (db.query(LeaveBalanceTransaction)
            .filter(LeaveBalanceTransaction.company_id == company_id,
                    LeaveBalanceTransaction.employee_id == employee_id, LeaveBalanceTransaction.leave_type == type_code)
            .order_by(LeaveBalanceTransaction.effective_date, LeaveBalanceTransaction.id).all())


def _in_cycle(row: LeaveBalanceTransaction, cycle: Cycle) -> bool:
    if row.cycle_start is not None:
        return row.cycle_start == cycle.start
    return cycle.start <= row.effective_date <= cycle.end


# --------------------------------------------------------------------------- lots (SPINE §6.5)

@dataclass
class Lot:
    txn_id: int
    credited_on: date
    expires_on: Optional[date]
    units: Decimal
    remaining: Decimal
    reason: str
    cycle_start: Optional[date] = None


def _usable(lot: Lot, row: LeaveBalanceTransaction, *, late: bool) -> bool:
    """A debit dated D draws on unexpired lots credited on or before D (``late``: a comp-off credit approved up to a
    year after the leave date that used it). Rows stamped with different leave years never draw on each other, so a
    year-end LAPSE or carry-out never eats the carried lot of the next year."""
    if lot.remaining <= 0 or (lot.expires_on is not None and lot.expires_on < row.effective_date):
        return False
    if row.cycle_start is not None and lot.cycle_start is not None and row.cycle_start != lot.cycle_start:
        return False
    if late:
        return row.effective_date < lot.credited_on <= row.effective_date + timedelta(days=366)
    return lot.credited_on <= row.effective_date


def lots(db: Session, company_id: int, employee_id: int, type_code: str) -> List[Lot]:
    """Credits as lots; each debit (and expiry) consumes unexpired lots earliest-expiry first, then non-expiring
    credits oldest first. EXPIRE rows reference their lot (``ref_type='leave_balance_transaction'``)."""
    rows = _rows(db, company_id, employee_id, type_code)
    found = [Lot(r.id, r.effective_date, r.expires_on, Decimal(r.delta), Decimal(r.delta), r.reason, r.cycle_start)
             for r in rows if Decimal(r.delta) > 0 and r.reason != "USE"]
    by_id = {lot.txn_id: lot for lot in found}
    for r in rows:
        delta = Decimal(r.delta)
        if r.reason == "EXPIRE" and r.ref_type == "leave_balance_transaction" and r.ref_id in by_id:
            by_id[r.ref_id].remaining += delta
            continue
        if delta > 0 and r.reason == "USE":  # a cancellation gives units back to the lots it used, latest first
            give = delta
            for lot in sorted(found, key=lambda x: (x.expires_on or date.max), reverse=True):
                room = lot.units - lot.remaining
                if give <= 0:
                    break
                take = min(room, give)
                lot.remaining += take
                give -= take
            continue
        if delta >= 0:
            continue
        need = -delta
        for late in (False, True):
            usable = [lot for lot in found if _usable(lot, r, late=late)]
            for lot in sorted(usable, key=lambda x: (x.expires_on or date.max, x.credited_on, x.txn_id)):
                if need <= 0:
                    break
                take = min(lot.remaining, need)
                lot.remaining -= take
                need -= take
        if need > 0 and found:  # overdrawn: charge the last lot (shown as a negative balance)
            found[-1].remaining -= need
    return found


# --------------------------------------------------------------------------- balances

@dataclass
class BalanceView:
    leave_type_code: str
    name: str
    unit: str
    scope: str
    unlimited: bool
    cycle_start: Optional[str]
    cycle_end: Optional[str]
    opening: Decimal = ZERO
    credited: Decimal = ZERO
    used: Decimal = ZERO
    encashed: Decimal = ZERO
    lapsed: Decimal = ZERO
    expired: Decimal = ZERO
    adjusted: Decimal = ZERO
    balance: Decimal = ZERO
    pending: Decimal = ZERO
    reserved_encashment: Decimal = ZERO
    available: Optional[Decimal] = ZERO
    expiring: List[dict] = field(default_factory=list)
    frozen: bool = False
    eligibility: Optional[str] = None
    available_from: Optional[str] = None
    color_token: Optional[str] = None
    icon: Optional[str] = None
    category: Optional[str] = None
    is_paid: bool = True
    # EVENT scope: one card per OPEN event (entitled / used / pending / remaining); empty for other scopes.
    events: List[dict] = field(default_factory=list)

    def json(self) -> dict:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, Decimal):
                data[key] = _num(value)
        return data


def _num(value: Optional[Decimal]):
    if value is None:
        return None
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value.quantize(Decimal("0.01")))


def pending_units(db: Session, company_id: int, employee_id: int, type_code: str,
                  cycle: Optional[Cycle] = None, exclude_leave_id: Optional[int] = None) -> Decimal:
    query = db.query(func.coalesce(func.sum(Leave.number_of_days), 0)).filter(
        Leave.company_id == company_id, Leave.employee_id == employee_id, Leave.leave_type == type_code,
        Leave.status == "pending")
    if cycle is not None:
        query = query.filter(Leave.start_date >= cycle.start, Leave.start_date <= cycle.end)
    if exclude_leave_id is not None:
        query = query.filter(Leave.id != exclude_leave_id)
    return Decimal(query.scalar() or 0)


def reserved_encashment(db: Session, company_id: int, employee_id: int, type_code: str,
                        exclude_id: Optional[int] = None) -> Decimal:
    query = db.query(func.coalesce(func.sum(LeaveEncashmentRequest.units), 0)).filter(
        LeaveEncashmentRequest.company_id == company_id, LeaveEncashmentRequest.employee_id == employee_id,
        LeaveEncashmentRequest.leave_type_code == type_code,
        LeaveEncashmentRequest.status.in_(("PENDING", "APPROVED")))
    if exclude_id is not None:
        query = query.filter(LeaveEncashmentRequest.id != exclude_id)
    return Decimal(query.scalar() or 0)


def balance(db: Session, company_id: int, employee: Employee, leave_type: LeaveType, on: date, *,
            rule: Optional[dict] = None, resolution=None, exclude_leave_id: Optional[int] = None) -> BalanceView:
    """The v2 balance of one type for the employee on ``on``."""
    resolution = resolution or leave_plans.resolve(db, company_id, employee.id, on)
    rule = rule if rule is not None else resolution.rule(leave_type.code)
    scope = ((rule or {}).get("balance_scope") or {}).get("kind") or (
        "LOTS" if leave_type.kind == "COMP_OFF" else "EVENT" if leave_type.kind == "EVENT" else "CYCLE")
    view = BalanceView(leave_type.code, leave_type.name, leave_type.unit, scope, bool(leave_type.is_unlimited),
                       None, None, color_token=leave_type.color_token, icon=leave_type.icon,
                       category=leave_type.category, is_paid=leave_type.is_paid, frozen=rule is None)
    rows = _rows(db, company_id, employee.id, leave_type.code)
    if scope == "LOTS":
        found = lots(db, company_id, employee.id, leave_type.code)
        live = [lot for lot in found if lot.expires_on is None or lot.expires_on >= on]
        view.credited = sum((lot.units for lot in found), ZERO)
        view.balance = sum((lot.remaining for lot in live), ZERO) + sum(
            (lot.remaining for lot in found if lot.remaining < 0 and lot not in live), ZERO)
        view.expiring = [{"units": _num(lot.remaining), "expires_on": lot.expires_on.isoformat()}
                         for lot in sorted(live, key=lambda x: x.expires_on or date.max)
                         if lot.remaining > 0 and lot.expires_on is not None][:5]
        selected = rows
        view.pending = pending_units(db, company_id, employee.id, leave_type.code, exclude_leave_id=exclude_leave_id)
    elif scope == "EVENT":
        # Per occurrence (03-leave/06 §6.1, V3-053(7)): the OPEN events are the balance; no ledger rows take part.
        from app.services import leave_events

        selected = []
        for event in leave_events.open_events(db, company_id, employee.id, leave_type.code):
            used, pending = leave_events.usage(db, event, exclude_leave_id=exclude_leave_id)
            view.credited += Decimal(event.entitled_units)
            view.used += used
            view.pending += pending
            view.events.append(leave_events.card(db, event, exclude_leave_id=exclude_leave_id))
        view.balance = view.credited - view.used
    else:
        cycle = leave_plans.cycle_of(db, company_id, employee, rule, on, resolution)
        view.cycle_start, view.cycle_end = cycle.start.isoformat(), cycle.end.isoformat()
        selected = [r for r in rows if _in_cycle(r, cycle)]
        view.balance = sum((Decimal(r.delta) for r in selected), ZERO)
        view.pending = pending_units(db, company_id, employee.id, leave_type.code, cycle,
                                     exclude_leave_id=exclude_leave_id)
        view.expiring = [{"units": _num(Decimal(r.delta)), "expires_on": r.expires_on.isoformat()}
                         for r in selected if r.expires_on is not None and Decimal(r.delta) > 0
                         and r.expires_on >= on][:5]
    for r in selected:
        delta = Decimal(r.delta)
        if r.reason in ("OPENING", "CARRY_FORWARD") and delta > 0:
            view.opening += delta
        elif r.reason in ("GRANT", "ACCRUAL", "COMP_OFF", "PRORATE"):
            view.credited += delta if scope != "LOTS" else ZERO
        elif r.reason == "USE":
            view.used -= delta
        elif r.reason == "ENCASH":
            view.encashed -= delta
        elif r.reason in ("LAPSE",) or (r.reason == "CARRY_FORWARD" and delta < 0):
            view.lapsed -= delta
        elif r.reason == "EXPIRE":
            view.expired -= delta
        elif r.reason in ("ADJUST", "PENALTY"):
            view.adjusted += delta
    view.reserved_encashment = reserved_encashment(db, company_id, employee.id, leave_type.code)
    view.available = None if leave_type.is_unlimited else view.balance - view.pending - view.reserved_encashment
    return view


def legacy_view(db: Session, company_id: int, employee_id: int, leave_type: LeaveType, year: int) -> BalanceView:
    """Today's formula (LEGACY_V1): the yearly quota plus non-USE movements minus use, per calendar year."""
    from app.services import leave_service

    totals = leave_service.balance_totals(db, company_id, employee_id, year).get(
        leave_type.code, {"used": ZERO, "adjusted": ZERO, "pending": ZERO})
    quota = Decimal(leave_type.annual_quota or 0)
    view = BalanceView(leave_type.code, leave_type.name, leave_type.unit, "CYCLE", bool(leave_type.is_unlimited),
                       date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat(), color_token=leave_type.color_token,
                       icon=leave_type.icon, category=leave_type.category, is_paid=leave_type.is_paid)
    view.credited = quota
    view.adjusted = totals["adjusted"]
    view.used = totals["used"]
    view.pending = totals["pending"]
    view.balance = quota + totals["adjusted"] - totals["used"]
    view.available = None if leave_type.is_unlimited else view.balance - view.pending
    return view


def balance_view(db: Session, company_id: int, employee: Employee, leave_type: LeaveType, on: date,
                 **kw) -> BalanceView:
    if leave_access.v2_enabled(db, company_id):
        return balance(db, company_id, employee, leave_type, on, **kw)
    return legacy_view(db, company_id, employee.id, leave_type, on.year)


def balances_for(db: Session, company_id: int, employee: Employee, on: date, *, for_employee: bool = True
                 ) -> List[dict]:
    """Every type the employee can see on ``on``: types of the plan (eligible or holding a balance) and retired
    types still holding units. Structural ineligibility hides a type from the employee unless it has a balance."""
    resolution = leave_plans.resolve(db, company_id, employee.id, on)
    types = {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == company_id)}
    codes = [r["leave_type_code"] for r in resolution.fields.get("types", [])]
    held = {code for (code,) in db.query(LeaveBalanceTransaction.leave_type).filter(
        LeaveBalanceTransaction.company_id == company_id, LeaveBalanceTransaction.employee_id == employee.id).distinct()}
    out = []
    for code in list(dict.fromkeys(codes + sorted(held))):
        lt = types.get(code)
        if lt is None:
            continue
        rule = resolution.rule(code)
        view = balance_view(db, company_id, employee, lt, on, rule=rule, resolution=resolution)
        if rule is not None:
            elig = leave_plans.eligibility(db, company_id, employee, lt, rule, on)
            view.eligibility = elig.status
            view.available_from = elig.available_from.isoformat() if elig.available_from else None
            if for_employee and elig.structural and view.balance == 0:
                continue
        elif view.balance == 0 and not lt.is_active:
            continue
        if not lt.is_active and view.balance == 0:
            continue
        out.append(view.json())
    order = {t.code: (t.display_order, t.name) for t in types.values()}
    return sorted(out, key=lambda item: order.get(item["leave_type_code"], (0, "")))


def ledger_rows(db: Session, company_id: int, employee_id: int, *, type_code: Optional[str] = None,
                start: Optional[date] = None, end: Optional[date] = None, limit: int = 500) -> List[dict]:
    query = db.query(LeaveBalanceTransaction).filter(LeaveBalanceTransaction.company_id == company_id,
                                                     LeaveBalanceTransaction.employee_id == employee_id)
    if type_code:
        query = query.filter(LeaveBalanceTransaction.leave_type == type_code)
    if start:
        query = query.filter(LeaveBalanceTransaction.effective_date >= start)
    if end:
        query = query.filter(LeaveBalanceTransaction.effective_date <= end)
    rows = query.order_by(LeaveBalanceTransaction.effective_date.desc(), LeaveBalanceTransaction.id.desc()).limit(
        limit).all()
    return [{"id": r.id, "leave_type": r.leave_type, "delta": _num(Decimal(r.delta)), "reason": r.reason,
             "effective_date": r.effective_date.isoformat(), "expires_on": r.expires_on.isoformat() if r.expires_on
             else None, "cycle_start": r.cycle_start.isoformat() if r.cycle_start else None,
             "source": r.source, "ref_type": r.ref_type, "ref_id": r.ref_id, "note": r.note,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


def explain_row(db: Session, company_id: int, txn_id: int) -> Optional[dict]:
    row = db.query(LeaveBalanceTransaction).filter(LeaveBalanceTransaction.company_id == company_id,
                                                   LeaveBalanceTransaction.id == txn_id).first()
    if row is None:
        return None
    text = {
        "GRANT": "Yearly entitlement credited at the start of the leave year.",
        "ACCRUAL": "Entitlement earned for the period.", "PRORATE": "Entitlement corrected for a partial year.",
        "CARRY_FORWARD": "Units carried from one leave year to the next.", "OPENING": "Opening balance imported.",
        "COMP_OFF": "Compensatory leave credited for work on a holiday or weekly off.",
        "USE": "Leave taken (a positive row gives units back after a cancellation or change).",
        "ENCASH": "Units paid out.", "LAPSE": "Unused units lapsed at year end.",
        "EXPIRE": "Credit expired unused.", "ADJUST": "Manual adjustment by HR.", "PENALTY": "Deduction for a policy breach.",
    }.get(row.reason, row.reason)
    return {"id": row.id, "reason": row.reason, "explanation": text, "delta": _num(Decimal(row.delta)),
            "effective_date": row.effective_date.isoformat(), "key": row.idempotency_key, "source": row.source,
            "note": row.note, "ref_type": row.ref_type, "ref_id": row.ref_id,
            "cycle": None if row.cycle_start is None else {"start": row.cycle_start.isoformat(),
                                                             "end": row.cycle_end.isoformat() if row.cycle_end else None}}
