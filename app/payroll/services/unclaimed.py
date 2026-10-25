"""Unclaimed pay as COMPANY POLICY (EC-PAY-19; table ``payment_unclaimed_events``).

This is a policy value of the company, not law: escheatment (the deposit of unclaimed wages with an authority) is
country-specific and **not implemented** here. The setting ``payroll.payments.unclaimed_after_days`` (default null =
off) says after how many days net pay that is still unpaid is moved to ``UNCLAIMED``; nothing is ever paid, deleted
or reversed automatically.

* **Marking** (the daily job ``payments.unclaimed_scan`` or the manual scan of a payment user): an OPEN or PARTIAL
  obligation of a legal entity whose setting is a number of days ``N`` becomes UNCLAIMED when all of this holds:
  its pay date (or, after a release, the release day) is at least ``N`` days ago in the entity's time zone; no
  instruction is in flight (batched, submitted, acknowledged, processing, partly settled); none is on hold (a
  person or the system holds it for a reason of its own: the scan never overrides a hold); and at least one
  instruction is unresolved: never sent (NOT_READY or READY) or failed, rejected, returned or reversed without a
  reissue. Unsent instructions are put ON_HOLD with the reason UNCLAIMED, so no batch can pick them up; failed
  ones stay as they are and cannot be reissued while the obligation is UNCLAIMED. The marking is audited, written
  to the append-only log and told to the employee and to finance (in-app; ``register_notifier`` adds channels).
* **Release** (``payroll:payment:approve`` in scope, step-up, a reason): the obligation goes back to what its
  instructions say (OPEN, PARTIAL, SETTLED or CANCELLED), its UNCLAIMED holds are lifted and the instructions are
  re-checked (READY when the bank account is usable, otherwise NOT_READY or ON_HOLD with the system's own reason).
  The policy window restarts from the release day, so a released obligation is not marked again at once. A payment
  itself still needs a batch and the usual approvals.
* **Reports**: ``summary`` and ``list_unclaimed`` give the unclaimed totals per currency (outstanding amounts);
  the obligations report already lists UNCLAIMED rows with their status.

Services take (db, principal, ...) and never commit; the job handler runs in the tenant's job transaction.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pytz
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.payment_flows import PaymentInstruction, PaymentObligation
from app.models.unclaimed import REASON_MAX, PaymentUnclaimedEvent
from app.payroll import settings_registry as reg
from app.payroll.money import amount_str
from app.payroll.services import access, holders, jobs, outbox, self_service, step_up
from app.payroll.services import obligations
from app.payroll.services import payment_events as events
from app.payroll.services import settings as settings_engine

SETTING = "payroll.payments.unclaimed_after_days"
JOB = "payments.unclaimed_scan"
SEED_JOB = "payments.unclaimed_seed"
HOLD_REASON = "UNCLAIMED"
MARKED = "UNCLAIMED_MARKED"
RELEASED = "UNCLAIMED_RELEASED"
SCAN_AT = time(2, 0)
SCAN_LIMIT = 500
POLICY_NOTE = "company policy; escheatment rules are country-specific and not implemented"

# Instruction statuses that decide whether an obligation may be marked.
UNRESOLVED = frozenset(("NOT_READY", "READY", "REJECTED", "FAILED", "RETURNED", "REVERSED"))
BUSY = frozenset(("APPROVAL_PENDING", "APPROVED", "QUEUED", "SUBMITTED", "ACKNOWLEDGED", "PROCESSING",
                  "PARTIALLY_SETTLED"))
IGNORED = frozenset(("CANCELLED", "REISSUED", "SETTLED", "RECONCILED"))

Notifier = Callable[[Session, PaymentObligation, Mapping[str, Any]], None]
NOTIFIERS: List[Notifier] = []


def register_notifier(func: Notifier) -> Notifier:
    """Add a channel told about every marking (``details``: window_days, amount, currency, held count). The in-app
    notification of the employee and of finance is registered below; e-mail can be added without touching this
    module."""
    if func not in NOTIFIERS:
        NOTIFIERS.append(func)
    return func


# --------------------------------------------------------------------------- pure rules


def classify(statuses: Iterable[str]) -> Tuple[bool, str]:
    """``(eligible, reason)`` for the statuses of an obligation's instructions (pure)."""
    seen = set(statuses)
    unknown = seen - UNRESOLVED - BUSY - IGNORED - {"ON_HOLD"}
    if unknown:
        return False, "UNKNOWN_STATUS"
    if seen & BUSY:
        return False, "PAYMENT_IN_PROGRESS"
    if "ON_HOLD" in seen:
        return False, "ON_HOLD"
    if not seen & UNRESOLVED:
        return False, "NOTHING_UNRESOLVED"
    return True, "UNRESOLVED"


def due_on(pay_date: Optional[date], released_on: Optional[date], created_on: date, window_days: int) -> date:
    """The first day the obligation may be marked: the window after its pay date (its creation day without one),
    restarted by the latest release."""
    base = pay_date or created_on
    if released_on is not None and released_on > base:
        base = released_on
    return base + timedelta(days=window_days)


def next_scan_at(now: datetime) -> datetime:
    today = now.astimezone(timezone.utc).date()
    slot = datetime.combine(today, SCAN_AT, tzinfo=timezone.utc)
    return slot if slot > now else slot + timedelta(days=1)


# --------------------------------------------------------------------------- loading and figures


def _instructions(db: Session, company_id: int, obligation_ids: Sequence[int]) -> Dict[int, List[PaymentInstruction]]:
    result: Dict[int, List[PaymentInstruction]] = defaultdict(list)
    if not obligation_ids:
        return result
    rows = db.query(PaymentInstruction).filter(PaymentInstruction.company_id == company_id,
                                               PaymentInstruction.obligation_id.in_(list(obligation_ids))
                                               ).order_by(PaymentInstruction.id).all()
    for row in rows:
        result[row.obligation_id].append(row)
    return result


def outstanding(obligation: PaymentObligation, rows: Sequence[PaymentInstruction]) -> Decimal:
    settled = events.obligation_figures(rows)["settled"]
    return max(Decimal(0), Decimal(obligation.amount) - settled)


def _last_events(db: Session, company_id: int, obligation_ids: Sequence[int]) -> Dict[Tuple[int, str], PaymentUnclaimedEvent]:
    found: Dict[Tuple[int, str], PaymentUnclaimedEvent] = {}
    if not obligation_ids:
        return found
    rows = db.query(PaymentUnclaimedEvent).filter(PaymentUnclaimedEvent.company_id == company_id,
                                                  PaymentUnclaimedEvent.obligation_id.in_(list(obligation_ids))
                                                  ).order_by(PaymentUnclaimedEvent.id).all()
    for row in rows:
        found[(row.obligation_id, row.action)] = row
    return found


def _money(amount: Decimal, currency: str) -> dict:
    return {"amount": amount_str(amount, currency), "currency": currency}


def _local_date(entity: LegalEntity, value: datetime) -> date:
    """The day of an instant in the legal entity's time zone (UTC when it has none or an unknown one)."""
    try:
        zone = pytz.timezone(entity.timezone) if entity.timezone else pytz.utc
    except pytz.UnknownTimeZoneError:
        zone = pytz.utc
    return value.astimezone(zone).date()


# --------------------------------------------------------------------------- marking


def _setting(db: Session, company_id: int, entity: LegalEntity, on: date) -> Optional[int]:
    """The window in days for the entity, or None when the policy is off. An invalid stored value raises."""
    value = settings_engine.resolve(db, company_id, SETTING, on=on, legal_entity_id=entity.id).value
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise settings_engine.SettingConfigError(SETTING, [reg.Issue("CONFIG_INVALID", "ERROR", (SETTING,),
                                                 f"{SETTING} must be a whole number of days")])
    return value


def mark(db: Session, obligation: PaymentObligation, rows: Sequence[PaymentInstruction], *, window_days: int,
         principal: Optional[Principal] = None, reason: Optional[str] = None) -> PaymentUnclaimedEvent:
    """Move an eligible obligation to UNCLAIMED (caller checked eligibility and holds the row lock)."""
    held: List[int] = []
    for row in rows:
        if row.status in ("NOT_READY", "READY"):
            moved = events.transition(db, None, row, "HOLD", system=True,
                                      reason=f"Unclaimed after {window_days} days (company policy)",
                                      updates={"status_reason": HOLD_REASON},
                                      details={"obligation_id": obligation.id, "window_days": window_days})
            held.append(moved.id)
    amount = outstanding(obligation, rows)
    obligation.status = "UNCLAIMED"
    obligation.row_version += 1
    db.flush()
    text = reason or f"Unpaid {window_days} days after the pay date; {POLICY_NOTE}"
    entry = PaymentUnclaimedEvent(
        company_id=obligation.company_id, obligation_id=obligation.id, employee_id=obligation.employee_id,
        legal_entity_id=obligation.legal_entity_id, action="MARKED", reason=text[:REASON_MAX], window_days=window_days,
        amount=amount, currency=obligation.currency, held_instruction_ids=held, result_status="UNCLAIMED",
        actor_user_id=principal.user_id if principal is not None else None, at=clock.utcnow())
    db.add(entry)
    db.flush()
    events.audit(db, principal, MARKED, f"Obligation #{obligation.id} marked unclaimed after {window_days} days "
                 f"({POLICY_NOTE})", "payment_obligation", obligation.id, obligation.company_id,
                 window_days=window_days, amount=str(amount), currency=obligation.currency, held_instructions=held,
                 employee_id=obligation.employee_id)
    details = {"window_days": window_days, "amount": amount_str(amount, obligation.currency),
               "currency": obligation.currency, "held": len(held)}
    for notify in list(NOTIFIERS):
        notify(db, obligation, details)
    return entry


def _candidates(db: Session, company_id: int, entity_id: int, limit: int) -> List[PaymentObligation]:
    return (db.query(PaymentObligation)
            .filter(PaymentObligation.company_id == company_id, PaymentObligation.legal_entity_id == entity_id,
                    PaymentObligation.status.in_(("OPEN", "PARTIAL")))
            .order_by(PaymentObligation.id).limit(limit).all())


def scan(db: Session, company_id: int, *, principal: Optional[Principal] = None, on: Optional[date] = None,
         legal_entity_id: Optional[int] = None, limit: int = SCAN_LIMIT) -> dict:
    """Mark every eligible obligation of the tenant (or one legal entity) per the policy; never pays, never deletes.

    A legal entity whose setting is off, or does not resolve to a valid number (fail closed), is skipped and
    listed. ``on`` is only for tests; the entity's local date is used otherwise."""
    entities = db.query(LegalEntity).filter(LegalEntity.company_id == company_id)
    if legal_entity_id is not None:
        entities = entities.filter(LegalEntity.id == legal_entity_id)
    result: Dict[str, Any] = {"marked": [], "skipped": [], "entities_off": [], "errors": [], "checked": 0}
    budget = limit
    for entity in entities.order_by(LegalEntity.id).all():
        today = on or self_service.local_today(db, company_id, entity)
        try:
            window = _setting(db, company_id, entity, today)
        except settings_engine.SettingConfigError as exc:
            result["errors"].append({"legal_entity_id": entity.id, "code": exc.code, "message": str(exc)})
            continue
        if window is None:
            result["entities_off"].append(entity.id)
            continue
        found = _candidates(db, company_id, entity.id, budget)
        instructions = _instructions(db, company_id, [o.id for o in found])
        released = {k[0]: e for k, e in _last_events(db, company_id, [o.id for o in found]).items() if k[1] == "RELEASED"}
        for obligation in found:
            result["checked"] += 1
            budget -= 1
            rel = released.get(obligation.id)
            first_day = due_on(obligation.pay_date, _local_date(entity, rel.at) if rel is not None else None,
                               _local_date(entity, obligation.created_at), window)
            if today < first_day:
                continue
            locked = (db.query(PaymentObligation).filter(PaymentObligation.company_id == company_id,
                                                         PaymentObligation.id == obligation.id)
                      .with_for_update(of=PaymentObligation).populate_existing().one())
            rows = (db.query(PaymentInstruction).filter(PaymentInstruction.company_id == company_id,
                                                        PaymentInstruction.obligation_id == obligation.id)
                    .order_by(PaymentInstruction.id).with_for_update(of=PaymentInstruction).populate_existing().all())
            if locked.status not in ("OPEN", "PARTIAL"):
                continue
            ok, why = classify(r.status for r in rows)
            if not ok:
                result["skipped"].append({"obligation_id": obligation.id, "reason": why})
                continue
            if outstanding(locked, rows) <= 0:
                continue
            entry = mark(db, locked, rows, window_days=window, principal=principal)
            result["marked"].append({"obligation_id": obligation.id, "legal_entity_id": entity.id,
                                     "amount": amount_str(entry.amount, entry.currency), "currency": entry.currency,
                                     "held_instruction_ids": list(entry.held_instruction_ids)})
        notify_finance(db, company_id, entity.id,
                       sum(1 for m in result["marked"] if m["legal_entity_id"] == entity.id))
        if budget <= 0:
            result["truncated"] = True
            break
    return result


def run_scan(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None) -> dict:
    """A payment user runs the scan now (``payroll:payment:approve``; scoped to their legal entities)."""
    if not principal.has(events.APPROVE):
        raise forbidden()
    scope = access.target_scope(principal, events.APPROVE)
    if legal_entity_id is not None:
        known = db.query(LegalEntity.id).filter(LegalEntity.company_id == principal.company_id,
                                                LegalEntity.id == legal_entity_id).first()
        if known is None or not scope.covers(legal_entity_id):
            raise not_found("Legal entity")
        return scan(db, principal.company_id, principal=principal, legal_entity_id=legal_entity_id)
    if scope.all_company:
        return scan(db, principal.company_id, principal=principal)
    merged: Dict[str, Any] = {"marked": [], "skipped": [], "entities_off": [], "errors": [], "checked": 0}
    for entity_id in sorted(scope.legal_entity_ids):
        part = scan(db, principal.company_id, principal=principal, legal_entity_id=entity_id)
        for key in ("marked", "skipped", "entities_off", "errors"):
            merged[key].extend(part[key])
        merged["checked"] += part["checked"]
    return merged


# --------------------------------------------------------------------------- release


def _load(db: Session, principal: Principal, obligation_id: int, *, lock: bool = False) -> PaymentObligation:
    if not principal.has(events.APPROVE):
        raise forbidden()
    query = db.query(PaymentObligation).filter(PaymentObligation.company_id == principal.company_id,
                                               PaymentObligation.id == obligation_id)
    if lock:
        query = query.with_for_update(of=PaymentObligation).populate_existing()
    row = query.one_or_none()
    if row is None or not access.target_scope(principal, events.APPROVE).covers(row.legal_entity_id):
        raise not_found("Payment obligation")
    return row


def release(db: Session, principal: Principal, obligation_id: int, reason: str, *,
            expected_version: Optional[int] = None) -> PaymentObligation:
    """Release an UNCLAIMED obligation back to the payment flow (step-up, a reason); no money moves here."""
    obligation = _load(db, principal, obligation_id, lock=True)
    step_up.ensure_step_up(db, principal)
    text = events.reason_text(reason, limit=REASON_MAX)
    if expected_version is not None and obligation.row_version != expected_version:
        raise conflict("The obligation was changed by someone else. Reload it.", "ROW_VERSION_CONFLICT")
    if obligation.status != "UNCLAIMED":
        raise conflict(f"Only an unclaimed obligation can be released; this one is {obligation.status}.",
                       "NOT_UNCLAIMED")
    rows = (db.query(PaymentInstruction).filter(PaymentInstruction.company_id == obligation.company_id,
                                                PaymentInstruction.obligation_id == obligation.id)
            .order_by(PaymentInstruction.id).with_for_update(of=PaymentInstruction).populate_existing().all())
    obligation.status = "OPEN"
    obligation.row_version += 1
    db.flush()
    changed: List[PaymentInstruction] = []
    for row in rows:
        if row.status == "ON_HOLD" and row.status_reason == HOLD_REASON:
            lifted = events.transition(db, principal, row, "RELEASE_HOLD", reason=f"Unclaimed pay released: {text}",
                                       updates={"status_reason": None}, details={"obligation_id": obligation.id})
            changed.append(obligations.ready_if_usable(db, lifted))
    if changed:
        events.sync_after(db, principal, changed)
    obligation = events.refresh_obligation(db, obligation.id, obligation.company_id)
    rows_now = _instructions(db, obligation.company_id, [obligation.id])[obligation.id]
    amount = outstanding(obligation, rows_now) or Decimal(obligation.amount)
    db.add(PaymentUnclaimedEvent(
        company_id=obligation.company_id, obligation_id=obligation.id, employee_id=obligation.employee_id,
        legal_entity_id=obligation.legal_entity_id, action="RELEASED", reason=text, window_days=None, amount=amount,
        currency=obligation.currency, held_instruction_ids=[r.id for r in changed], result_status=obligation.status,
        actor_user_id=principal.user_id, at=clock.utcnow()))
    db.flush()
    events.audit(db, principal, RELEASED, f"Unclaimed obligation #{obligation.id} released: {text}",
                 "payment_obligation", obligation.id, obligation.company_id, reason=text,
                 result_status=obligation.status, released_instructions=[r.id for r in changed],
                 employee_id=obligation.employee_id)
    return obligation


# --------------------------------------------------------------------------- reads


def _view_clause(principal: Principal):
    clauses = [access.run_scope_clause(principal, p, PaymentObligation.legal_entity_id, None)
               for p in events.VIEW_PERMISSIONS if principal.has(p)]
    if not clauses:
        raise forbidden()
    return or_(*clauses)


def _row_json(obligation: PaymentObligation, rows: Sequence[PaymentInstruction],
              last: Mapping[Tuple[int, str], PaymentUnclaimedEvent], names: Mapping[int, dict],
              today: date) -> dict:
    marked = last.get((obligation.id, "MARKED"))
    released = last.get((obligation.id, "RELEASED"))
    return {
        "id": obligation.id, "employee_id": obligation.employee_id, "employee": names.get(obligation.employee_id),
        "legal_entity_id": obligation.legal_entity_id, "run_id": obligation.run_id, "status": obligation.status,
        "amount": _money(Decimal(obligation.amount), obligation.currency),
        "outstanding": _money(outstanding(obligation, rows), obligation.currency),
        "pay_date": obligation.pay_date.isoformat() if obligation.pay_date else None,
        "unclaimed_at": marked.at.isoformat() if marked is not None and obligation.status == "UNCLAIMED" else None,
        "window_days": marked.window_days if marked is not None else None,
        "days_unpaid": (today - obligation.pay_date).days if obligation.pay_date else None,
        "reason": marked.reason if marked is not None and obligation.status == "UNCLAIMED" else None,
        "held_instructions": sum(1 for r in rows if r.status == "ON_HOLD" and r.status_reason == HOLD_REASON),
        "last_released_at": released.at.isoformat() if released is not None else None,
        "row_version": obligation.row_version,
    }


def list_unclaimed(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None,
                   currency: Optional[str] = None, skip: int = 0, limit: int = 50) -> dict:
    """UNCLAIMED obligations in the caller's payment scope, with the unclaimed totals per currency (outstanding)."""
    query = db.query(PaymentObligation).filter(PaymentObligation.company_id == principal.company_id,
                                               PaymentObligation.status == "UNCLAIMED", _view_clause(principal))
    if legal_entity_id is not None:
        query = query.filter(PaymentObligation.legal_entity_id == legal_entity_id)
    if currency is not None:
        query = query.filter(PaymentObligation.currency == currency)
    everything = query.order_by(PaymentObligation.id.desc()).all()
    instructions = _instructions(db, principal.company_id, [o.id for o in everything])
    totals: Dict[str, Dict[str, Any]] = {}
    for obligation in everything:
        entry = totals.setdefault(obligation.currency, {"currency": obligation.currency, "count": 0,
                                                        "outstanding": Decimal(0)})
        entry["count"] += 1
        entry["outstanding"] += outstanding(obligation, instructions[obligation.id])
    page = everything[skip: skip + limit]
    last = _last_events(db, principal.company_id, [o.id for o in page])
    names = {}
    ids = sorted({o.employee_id for o in page})
    if ids:
        names = {e.id: {"id": e.id, "name": e.full_name, "employee_code": e.employee_code}
                 for e in db.query(Employee).filter(Employee.company_id == principal.company_id,
                                                    Employee.id.in_(ids))}
    today = clock.company_today(db, principal.company_id)
    return {
        "total": len(everything), "policy": {"setting": SETTING, "note": POLICY_NOTE},
        "totals": [{"currency": t["currency"], "count": t["count"], "outstanding": _money(t["outstanding"], t["currency"])}
                   for t in sorted(totals.values(), key=lambda t: t["currency"])],
        "items": [_row_json(o, instructions[o.id], last, names, today) for o in page],
    }


def history(db: Session, principal: Principal, obligation_id: int) -> List[dict]:
    """The marking and release log of one obligation in the caller's scope."""
    obligation = db.query(PaymentObligation).filter(PaymentObligation.company_id == principal.company_id,
                                                    PaymentObligation.id == obligation_id,
                                                    _view_clause(principal)).one_or_none()
    if obligation is None:
        raise not_found("Payment obligation")
    rows = (db.query(PaymentUnclaimedEvent).filter(PaymentUnclaimedEvent.company_id == principal.company_id,
                                                   PaymentUnclaimedEvent.obligation_id == obligation.id)
            .order_by(PaymentUnclaimedEvent.id).all())
    return [{"id": r.id, "action": r.action, "reason": r.reason, "window_days": r.window_days,
             "amount": _money(Decimal(r.amount), r.currency), "result_status": r.result_status,
             "held_instruction_ids": list(r.held_instruction_ids or []), "actor_user_id": r.actor_user_id,
             "at": r.at.isoformat()} for r in rows]


# --------------------------------------------------------------------------- notifications (in-app)


@register_notifier
def _notify_employee(db: Session, obligation: PaymentObligation, details: Mapping[str, Any]) -> None:
    """The employee is told in-app, without an amount: the payment is on hold and what to do about it.

    Pay of a deceased employee's settlement belongs to the nominees (payee type NOMINEE): the deceased employee's own
    login is never told about it (finance is, through ``notify_finance``)."""
    if obligation.payee_type != "EMPLOYEE":
        return
    user_id = db.query(Employee.user_id).filter(Employee.company_id == obligation.company_id,
                                                Employee.id == obligation.employee_id).scalar()
    holders.notify_users(db, [user_id], "A payment to you is on hold",
                         "Your pay has not reached you yet and was put on hold. Please check your bank details "
                         "or contact HR.", "/me/payments", "warning")


def notify_finance(db: Session, company_id: int, legal_entity_id: int, marked: int) -> int:
    """Finance (holders of payroll:payment:approve for the entity) is told once per scan and entity, in-app, with the
    number of obligations and no amounts or names."""
    if marked <= 0:
        return 0
    noun = "payment was" if marked == 1 else "payments were"
    return holders.notify_holders(db, company_id, events.APPROVE, legal_entity_id, "Unclaimed pay",
                                  f"{marked} {noun} marked unclaimed under the company policy. Review "
                                  "them in Payments.", "/payroll/payments", notification_type="warning")


# --------------------------------------------------------------------------- jobs


def schedule_scan(db: Session, company_id: int, *, after: Optional[datetime] = None) -> None:
    """Queue the tenant's next daily scan (one job per tenant and day, idempotent)."""
    slot = next_scan_at(after or clock.utcnow())
    jobs.enqueue(db, company_id=company_id, type=JOB, payload={"slot": slot.isoformat()},
                 idempotency_key=f"unclaimed-scan:{slot.strftime('%Y%m%d')}", run_at=slot, priority=190)


@jobs.register(JOB)
def _scan_job(ctx: jobs.JobContext) -> dict:
    outcome = scan(ctx.db, ctx.company_id)
    schedule_scan(ctx.db, ctx.company_id)
    return {"checked": outcome["checked"], "marked": len(outcome["marked"]), "errors": len(outcome["errors"])}


@jobs.register(SEED_JOB)
def _seed_job(ctx: jobs.JobContext) -> dict:
    """A tenant with payments gets its daily scan chain from its first finalized run on (idempotent)."""
    schedule_scan(ctx.db, ctx.company_id)
    return {}


outbox.subscribe(obligations.FINALIZED_TOPIC, SEED_JOB)

__all__ = [
    "HOLD_REASON", "JOB", "NOTIFIERS", "POLICY_NOTE", "SETTING", "classify", "due_on", "history", "list_unclaimed",
    "mark", "next_scan_at", "outstanding", "register_notifier", "release", "run_scan", "scan", "schedule_scan",
]
