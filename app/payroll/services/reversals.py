"""Reversal of finalized runs (3.5 §4.1 REVERSE, §12; EC-COR-03, EC-COR-04, EC-COR-16).

* ``request_reversal``: a holder of ``payroll:run:reverse`` asks to reverse a finalized run, for all its
  finalized employees or selected ones, with a reason. One pending request per run; an employee already
  reversed cannot be reversed again; nobody reverses their own pay. The requester also needs
  ``payroll:run:create`` and ``payroll:run:calculate`` (the REVERSAL run is prepared on their behalf).
* ``approve_reversal``: two different approvers (``payroll:run:approve`` in the run's scope), neither the
  requester nor a reversed employee, approve in turn (guard TWO_APPROVERS, also enforced by CHECK
  constraints). The second approval records the REVERSE transition on the original run (its status does
  not change), creates the REVERSAL run (``reverses_run_id``) with the exact negative of every original
  line and figure of the reversed employees, moves it to REVIEW_REQUIRED, and holds the reversed
  employees' unsent payment instructions so nobody submits them meanwhile. The REVERSAL run then has its
  own approval and finalization like any run. ``reject_reversal`` (reason) and ``cancel_reversal`` (the
  requester or another holder of the reverse permission) close a pending request.
* When the REVERSAL run is finalized (outbox ``payroll.run.finalized``), ``on_run_finalized``:
  unsubmitted instructions are cancelled (EC-COR-04); settled amounts become recovery receivables
  (approved arrears RECOVERY proposals with trigger REVERSAL, recovered per
  ``payroll.arrears.recovery_mode`` within the protected net, or manually) — never an automatic bank
  debit (EC-COR-03); instructions still in flight are listed for finance; the outbox event
  ``payroll.run.reversed`` asks the documents milestone to mark the original payslips REVERSED and issue
  the reversal statement (the original documents are never changed).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.corrections import ArrearsProposal, ArrearsProposalLine, PayrollReversal
from app.models.pay_runs import PayrollLine, PayrollRun, PayrollRunEmployee
from app.payroll import audit as payroll_audit
from app.payroll import state_machines as sm
from app.payroll.money import amount_str
from app.payroll.services import access, calendars, off_cycle, outbox
from app.services.audit_log_service import AuditLogService

READ = "payroll:run:read"
REVERSE = sm.RUN_REVERSE
APPROVE = sm.RUN_APPROVE
AUDIT_MODULE = payroll_audit.PAYROLL_RUNS
REVERSED_TOPIC = "payroll.run.reversed"
RECOVERY_COMPONENT = "NET_PAY_RECOVERY"

REVERSAL_REQUESTED = "REVERSAL_REQUESTED"
REVERSAL_APPROVED_STEP = "REVERSAL_APPROVED_STEP"
REVERSAL_APPROVED = "REVERSAL_APPROVED"
REVERSAL_REJECTED = "REVERSAL_REJECTED"
REVERSAL_CANCELLED = "REVERSAL_CANCELLED"
REVERSAL_FINALIZED = "REVERSAL_FINALIZED"

UNSENT = ("NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED", "ON_HOLD")
HOLDABLE = ("NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED")
IN_FLIGHT = ("QUEUED", "SUBMITTED", "ACKNOWLEDGED", "PROCESSING")
SETTLED = ("PARTIALLY_SETTLED", "SETTLED", "RECONCILED")
ZERO = Decimal(0)


def _now() -> dt.datetime:
    return clock.utcnow()


def _reason(value: Optional[str]) -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    return cleaned[:1000]


def _audit(db: Session, principal: Optional[Principal], action: str, description: str, row: PayrollReversal,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description[:500], "payroll_reversal", row.id,
                        company_id=row.company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def _check_version(row: PayrollReversal, expected: Optional[int]) -> None:
    if expected is not None and row.row_version != expected:
        raise conflict("This reversal was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")


def _neg(value: Optional[Decimal]) -> Optional[Decimal]:
    return None if value is None else -Decimal(value)


# ---------------------------------------------------------------------------
# Loading with scope
# ---------------------------------------------------------------------------
def load_reversal(db: Session, principal: Principal, reversal_id: int, permission: str = READ, *,
                  lock: bool = False) -> PayrollReversal:
    if not principal.has(permission):
        raise forbidden()
    query = db.query(PayrollReversal).filter(
        PayrollReversal.company_id == principal.company_id, PayrollReversal.id == reversal_id,
        access.run_scope_clause(principal, permission, PayrollReversal.legal_entity_id, PayrollReversal.pay_group_id))
    if lock:
        query = query.with_for_update(of=PayrollReversal).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Reversal")
    return row


def _reversed_run_employee_ids(db: Session, company_id: int, run_id: int) -> set:
    ids = set()
    for (selected,) in db.query(PayrollReversal.run_employee_ids).filter(
            PayrollReversal.company_id == company_id, PayrollReversal.original_run_id == run_id,
            PayrollReversal.status.in_(("APPROVED", "FINALIZED"))):
        ids.update(int(i) for i in selected or [])
    return ids


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
def request_reversal(db: Session, principal: Principal, run_id: int, *, reason: str,
                     run_employee_ids: Sequence[int] = (), expected_version: Optional[int] = None) -> PayrollReversal:
    from app.payroll.services import runs
    from app.payroll.services.retro import employee_visible

    run = runs.get_run_for(db, principal, run_id, REVERSE, lock=True)
    if expected_version is not None and run.row_version != expected_version:
        raise conflict("The run was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    if run.run_type == sm.RunType.REVERSAL.value:
        raise conflict("A reversal run is not reversed; correct it with another run.", "REVERSAL_OF_REVERSAL")
    if not sm.is_run_finalized(run.status):
        raise conflict("Only finalized runs are reversed; reopen or cancel a run that is not finalized.",
                       "RUN_NOT_FINALIZED")
    off_cycle.ensure_can_prepare(principal, run.legal_entity_id, run.pay_group_id)
    text = _reason(reason)
    period = calendars.load_period(db, run.company_id, run.pay_period_id)
    rows = (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
                    PayrollRunEmployee.status == "CALCULATED")
            .order_by(PayrollRunEmployee.id).all())
    visible = [r for r in rows if employee_visible(db, principal, REVERSE, r.employee_id, period.period_start,
                                                    period.period_end)]
    if run_employee_ids:
        wanted = set(int(i) for i in run_employee_ids)
        chosen = [r for r in visible if r.id in wanted]
        if len(chosen) != len(wanted):
            raise not_found("Run employee")
        selected = True
    else:
        if len(visible) != len(rows):
            raise forbidden("Reversing the whole run needs every employee of it in your scope; select employees.")
        chosen, selected = visible, False
    if not chosen:
        raise conflict("The run has no finalized employees to reverse.", "NOTHING_TO_REVERSE")
    done = _reversed_run_employee_ids(db, run.company_id, run.id) & {r.id for r in chosen}
    if done:
        raise conflict(f"Run employees {sorted(done)} are already reversed.", "ALREADY_REVERSED")
    if principal.employee_id is not None and principal.employee_id in {r.employee_id for r in chosen}:
        raise forbidden("You cannot reverse your own pay.")
    preview: Dict[str, Dict[str, Any]] = {}
    for r in chosen:
        bucket = preview.setdefault(r.currency, {"gross": ZERO, "net": ZERO, "employees": 0})
        bucket["gross"] -= Decimal(r.gross or 0)
        bucket["net"] -= Decimal(r.net or 0)
        bucket["employees"] += 1
    row = PayrollReversal(
        company_id=run.company_id, original_run_id=run.id, legal_entity_id=run.legal_entity_id,
        pay_group_id=run.pay_group_id, run_employee_ids=[r.id for r in chosen],
        employee_ids=sorted({r.employee_id for r in chosen}), selected=selected, reason=text,
        status="PENDING_APPROVAL",
        preview={cur: {"gross": amount_str(v["gross"], cur), "net": amount_str(v["net"], cur),
                       "employees": v["employees"]} for cur, v in preview.items()},
        effects={}, requested_by_user_id=principal.user_id, row_version=1)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        if "uq_payroll_reversals_pending" in str(exc.orig):
            raise conflict("A reversal of this run is already waiting for approval.", "REVERSAL_PENDING")
        raise
    _audit(db, principal, REVERSAL_REQUESTED, f"Requested the reversal of run {run.number}: {text}", row,
           run_id=run.id, run_employee_ids=row.run_employee_ids, selected=selected)
    return row


# ---------------------------------------------------------------------------
# Approve / reject / cancel
# ---------------------------------------------------------------------------
def _check_approver(principal: Principal, row: PayrollReversal) -> None:
    if principal.user_id == row.requested_by_user_id:
        raise forbidden("The requester of a reversal cannot approve it.")
    if principal.user_id == row.first_approver_user_id:
        raise forbidden("A reversal needs two different approvers.")
    if principal.employee_id is not None and principal.employee_id in (row.employee_ids or []):
        raise forbidden("You cannot approve the reversal of your own pay.")


def approve_reversal(db: Session, principal: Principal, reversal_id: int, *, comment: Optional[str] = None,
                     expected_version: Optional[int] = None) -> PayrollReversal:
    row = load_reversal(db, principal, reversal_id, APPROVE, lock=True)
    _check_version(row, expected_version)
    if row.status != "PENDING_APPROVAL":
        raise conflict(f"This reversal is {row.status.lower().replace('_', ' ')}.", "REVERSAL_NOT_PENDING")
    _check_approver(principal, row)
    now = _now()
    text = " ".join((comment or "").split())[:1000] or None
    if row.first_approver_user_id is None:
        row.first_approver_user_id, row.first_approved_at, row.first_comment = principal.user_id, now, text
        row.row_version += 1
        db.flush()
        _audit(db, principal, REVERSAL_APPROVED_STEP, f"First approval of reversal #{row.id}", row, step=1)
        return row
    row.second_approver_user_id, row.second_approved_at, row.second_comment = principal.user_id, now, text
    _execute(db, principal, row)
    return row


def _requester(db: Session, row: PayrollReversal) -> Principal:
    from app.payroll.services import wage_inputs

    requester = wage_inputs.principal_for_user(db, row.company_id, row.requested_by_user_id)
    ok = requester is not None and requester.has(REVERSE) and access.target_scope(requester, REVERSE).covers(
        row.legal_entity_id, row.pay_group_id)
    if ok:
        try:
            off_cycle.ensure_can_prepare(requester, row.legal_entity_id, row.pay_group_id)
        except Exception:
            ok = False
    if not ok:
        raise conflict("The requester can no longer reverse this run; ask a current holder of the reverse permission "
                       "to request it again.", "REQUESTER_NOT_AUTHORIZED")
    return requester


def _negated_lines(db: Session, original: PayrollRunEmployee) -> List[dict]:
    lines = (db.query(PayrollLine).filter(PayrollLine.company_id == original.company_id,
                                          PayrollLine.run_employee_id == original.id)
             .order_by(PayrollLine.sort_order, PayrollLine.id).all())
    result = []
    for line in lines:
        result.append({
            "component_code": line.component_code, "component_id": line.component_id,
            "component_version_id": line.component_version_id, "kind": line.kind, "category": line.category,
            "stage": line.stage, "amount": -Decimal(line.amount), "currency": line.currency,
            "units": line.units, "rate": line.rate,
            "period_ref": {**(line.period_ref or {}), "reverses_line_id": line.id,
                           "original_run_employee_id": original.id},
            "segment": line.segment, "tags": list(line.tags or ()), "visibility": line.visibility,
            "sources": [{"type": "reversal", "line_id": line.id}],
            "trace": {"method": "REVERSAL", "reverses_line_id": line.id},
            "explanation": f"Reversal of line #{line.id}: {line.explanation or line.component_code}"[:2000],
            "sort_order": line.sort_order, "statutory": line.statutory, "fixture": line.fixture,
        })
    return result


def _mirror_population(db: Session, run: PayrollRun, originals: Sequence[PayrollRunEmployee]) -> Dict[int, int]:
    """The reversal run carries exactly the reversed placements: rows are aligned on the originals'
    (employee, assignment); returns {reversal run employee id: original run employee id}."""
    rows = (db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                                PayrollRunEmployee.run_id == run.id)
            .with_for_update(of=PayrollRunEmployee).all())
    by_key = {(r.employee_id, r.employee_assignment_id): r for r in rows}
    mapping: Dict[int, int] = {}
    for original in originals:
        row = by_key.pop((original.employee_id, original.employee_assignment_id), None)
        if row is None:
            row = PayrollRunEmployee(company_id=run.company_id, run_id=run.id, employee_id=original.employee_id,
                                     employee_assignment_id=original.employee_assignment_id, status="PENDING",
                                     currency=original.currency, flags=[], row_version=1)
            db.add(row)
            db.flush()
        mapping[row.id] = original.id
    for row in by_key.values():  # placements the population found that were not reversed
        db.delete(row)
    db.flush()
    return mapping


def _hold_unsent(db: Session, row: PayrollReversal) -> List[int]:
    from app.models.payment_flows import PaymentInstruction, PaymentObligation
    from app.payroll.services import payment_events

    instructions = (db.query(PaymentInstruction)
                    .join(PaymentObligation, and_(PaymentObligation.company_id == PaymentInstruction.company_id,
                                                  PaymentObligation.id == PaymentInstruction.obligation_id))
                    .filter(PaymentInstruction.company_id == row.company_id,
                            PaymentObligation.run_employee_id.in_(list(row.run_employee_ids or [0])),
                            PaymentInstruction.status.in_(HOLDABLE))
                    .order_by(PaymentInstruction.id).all())
    held = []
    for instruction in instructions:
        payment_events.transition(db, None, instruction, "HOLD", company_id=row.company_id, system=True,
                                  reason=f"Run reversal #{row.id} approved; cancelled when the reversal is finalized",
                                  updates={"status_reason": "RUN_REVERSAL"})
        held.append(instruction.id)
    if instructions:
        payment_events.sync_after(db, None, instructions)
    return held


def _execute(db: Session, approver: Principal, row: PayrollReversal) -> None:
    """Second approval: REVERSE on the original, the REVERSAL run with exact negative lines, held payments."""
    from app.payroll.services import runs

    requester = _requester(db, row)
    original = runs.load_run(db, row.company_id, row.original_run_id, lock=True)
    runs.transition(db, requester, original, "REVERSE", reason=row.reason, TWO_APPROVERS=True,
                    details={"reversal_id": row.id, "approver_user_ids": [row.first_approver_user_id,
                                                                          row.second_approver_user_id]})
    originals = (db.query(PayrollRunEmployee)
                 .filter(PayrollRunEmployee.company_id == row.company_id,
                         PayrollRunEmployee.id.in_(list(row.run_employee_ids or [])))
                 .order_by(PayrollRunEmployee.id).all())
    run = runs.create_run(db, requester, runs.RunRequest(
        run_type=sm.RunType.REVERSAL.value, pay_group_id=original.pay_group_id, pay_period_id=original.pay_period_id,
        label=f"Reversal · {original.label}"[:150], employee_ids=tuple(sorted({o.employee_id for o in originals})),
        reverses_run_id=original.id), allow_reversal=True)
    run.selection = {**(run.selection or {}), "reversal": {"reversal_id": row.id, "original_run_id": original.id,
                                                           "run_employee_ids": list(row.run_employee_ids or [])}}
    db.flush()
    mapping = _mirror_population(db, run, originals)
    by_id = {o.id: o for o in originals}
    results = {}
    for reversal_row_id, original_id in mapping.items():
        source = by_id[original_id]
        results[reversal_row_id] = off_cycle.Prepared(
            gross=_neg(source.gross) or ZERO, deductions=_neg(source.deductions) or ZERO,
            reimbursements=_neg(source.reimbursements) or ZERO, net=_neg(source.net) or ZERO,
            employer_cost=_neg(source.employer_cost) or ZERO, lines=_negated_lines(db, source), flags=["REVERSAL"])
    row.reversal_run_id = run.id
    row.status = "APPROVED"
    row.decided_by_user_id, row.decided_at = approver.user_id, _now()
    row.row_version += 1
    db.flush()
    off_cycle.prepare_results(db, requester, run, results, source="REVERSAL")
    held = _hold_unsent(db, row)
    row.effects = {**(row.effects or {}), "instructions_held": held}
    db.flush()
    _audit(db, approver, REVERSAL_APPROVED, f"Reversal #{row.id} approved; reversal run {run.number} created", row,
           reversal_run_id=run.id, first_approver_user_id=row.first_approver_user_id,
           second_approver_user_id=row.second_approver_user_id, instructions_held=held)


def reject_reversal(db: Session, principal: Principal, reversal_id: int, *, reason: str,
                    expected_version: Optional[int] = None) -> PayrollReversal:
    row = load_reversal(db, principal, reversal_id, APPROVE, lock=True)
    _check_version(row, expected_version)
    if row.status != "PENDING_APPROVAL":
        raise conflict(f"This reversal is {row.status.lower().replace('_', ' ')}.", "REVERSAL_NOT_PENDING")
    _check_approver(principal, row)
    row.status, row.decision_note = "REJECTED", _reason(reason)
    row.decided_by_user_id, row.decided_at = principal.user_id, _now()
    row.row_version += 1
    db.flush()
    _audit(db, principal, REVERSAL_REJECTED, f"Rejected reversal #{row.id}: {row.decision_note}", row)
    return row


def cancel_reversal(db: Session, principal: Principal, reversal_id: int, *, reason: str,
                    expected_version: Optional[int] = None) -> PayrollReversal:
    row = load_reversal(db, principal, reversal_id, REVERSE, lock=True)
    _check_version(row, expected_version)
    if row.status != "PENDING_APPROVAL":
        raise conflict(f"This reversal is {row.status.lower().replace('_', ' ')}.", "REVERSAL_NOT_PENDING")
    row.status, row.decision_note = "CANCELLED", _reason(reason)
    row.decided_by_user_id, row.decided_at = principal.user_id, _now()
    row.row_version += 1
    db.flush()
    _audit(db, principal, REVERSAL_CANCELLED, f"Cancelled reversal #{row.id}: {row.decision_note}", row)
    return row


# ---------------------------------------------------------------------------
# Finalization effects (outbox payroll.run.finalized of the REVERSAL run)
# ---------------------------------------------------------------------------
def _recovery(db: Session, row: PayrollReversal, original: PayrollRun, run_employee: PayrollRunEmployee,
              settled: Decimal, currency: str) -> ArrearsProposal:
    """A settled payment of a reversed employee becomes a recovery receivable (EC-COR-03): an approved RECOVERY
    proposal (authorized by the reversal's two approvers), recovered per payroll.arrears.recovery_mode within
    the protected net, or manually; never a bank debit."""
    from app.payroll.services import retro

    period = calendars.load_period(db, original.company_id, original.pay_period_id)
    ref = {"period_id": period.id, "start": period.period_start.isoformat(), "end": period.period_end.isoformat(),
           "run_id": original.id, "run_number": original.number, "run_employee_id": run_employee.id,
           "reversal_id": row.id}
    proposal = ArrearsProposal(
        company_id=row.company_id, employee_id=run_employee.employee_id, legal_entity_id=original.legal_entity_id,
        pay_group_id=original.pay_group_id, original_run_id=original.id, original_run_employee_id=run_employee.id,
        original_period_id=period.id, period_start=period.period_start, period_end=period.period_end,
        currency=currency, trigger="REVERSAL", trigger_ref={"type": "payroll_reversal", "id": row.id,
                                                            "reversal_run_id": row.reversal_run_id},
        changed_sources=[], totals={"arrears": amount_str(ZERO, currency), "recovery": amount_str(-settled, currency),
                                    "net_change": amount_str(-settled, currency)},
        issues=[], status="APPROVED", payment_mode="NEXT_REGULAR_RUN", reason=f"Reversal #{row.id}: {row.reason}"[:1000],
        decided_by_user_id=row.second_approver_user_id, decided_at=_now(), row_version=1)
    db.add(proposal)
    db.flush()
    db.add(ArrearsProposalLine(
        company_id=row.company_id, proposal_id=proposal.id, employee_id=run_employee.employee_id,
        component_code=RECOVERY_COMPONENT, kind="DEDUCTION", category="RECOVERY", tags=[], original_amount=ZERO,
        recalculated_amount=settled, delta=-settled, currency=currency, direction="RECOVERY", period_ref=ref,
        explanation=f"Net pay of run {original.number} paid before its reversal: {amount_str(settled, currency)} "
                    f"{currency} to recover (no automatic bank debit).", sort_order=0))
    db.flush()
    retro._plan_instalments(db, proposal)
    retro._audit(db, None, retro.ARREARS_PROPOSED, f"Recovery #{proposal.id} from reversal #{row.id}", proposal,
                 trigger="REVERSAL", reversal_id=row.id, amount=retro._money(-settled, currency),
                 recovery_mode=proposal.recovery_mode)
    return proposal


def on_run_finalized(db: Session, run: PayrollRun) -> dict:
    """Cancel unsent payments, record recoveries for settled ones, flag in-flight ones, emit
    payroll.run.reversed. Idempotent (a FINALIZED reversal is left alone)."""
    from app.models.payment_flows import PaymentInstruction, PaymentObligation
    from app.payroll.services import payment_events, runs

    if run.run_type != sm.RunType.REVERSAL.value or not sm.is_run_finalized(run.status):
        return {}
    row = (db.query(PayrollReversal)
           .filter(PayrollReversal.company_id == run.company_id, PayrollReversal.reversal_run_id == run.id)
           .with_for_update(of=PayrollReversal).populate_existing().first())
    if row is None or row.status != "APPROVED":
        return {"status": row.status if row else None}
    original = runs.load_run(db, run.company_id, row.original_run_id)
    pairs = (db.query(PaymentInstruction, PaymentObligation)
             .join(PaymentObligation, and_(PaymentObligation.company_id == PaymentInstruction.company_id,
                                           PaymentObligation.id == PaymentInstruction.obligation_id))
             .filter(PaymentInstruction.company_id == run.company_id,
                     PaymentObligation.run_employee_id.in_(list(row.run_employee_ids or [0])))
             .order_by(PaymentInstruction.id).all())
    cancelled: List[int] = []
    in_flight: List[int] = []
    settled_by: Dict[int, Decimal] = {}
    currencies: Dict[int, str] = {}
    touched = []
    for instruction, obligation in pairs:
        if instruction.status in UNSENT:
            payment_events.transition(db, None, instruction, "CANCEL_FOR_REVERSAL", company_id=run.company_id,
                                      system=True, RUN_REVERSAL_AUTHORIZED=True,
                                      reason=f"Run {original.number} reversed by run {run.number}",
                                      updates={"status_reason": "RUN_REVERSED"})
            cancelled.append(instruction.id)
            touched.append(instruction)
        elif instruction.status in IN_FLIGHT:
            in_flight.append(instruction.id)
        elif instruction.status in SETTLED:
            amount = payment_events.settled_in_pay_currency(
                instruction, Decimal(instruction.settled_amount) if instruction.settled_amount is not None else None)
            settled_by[obligation.run_employee_id] = settled_by.get(obligation.run_employee_id, ZERO) + amount
            currencies[obligation.run_employee_id] = instruction.currency
    if touched:
        payment_events.sync_after(db, None, touched)
    recoveries = []
    for run_employee_id, amount in sorted(settled_by.items()):
        if amount <= 0:
            continue
        original_row = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                                           PayrollRunEmployee.id == run_employee_id).one()
        proposal = _recovery(db, row, original, original_row, amount, currencies[run_employee_id])
        recoveries.append({"run_employee_id": run_employee_id, "proposal_id": proposal.id,
                           "amount": amount_str(amount, currencies[run_employee_id]),
                           "currency": currencies[run_employee_id], "recovery_mode": proposal.recovery_mode})
    event = outbox.emit(db, REVERSED_TOPIC, {"run_id": original.id, "reversal_run_id": run.id, "reversal_id": row.id,
                                             "run_employee_ids": list(row.run_employee_ids or [])})
    row.status, row.finalized_at = "FINALIZED", _now()
    row.effects = {**(row.effects or {}), "instructions_cancelled": cancelled, "instructions_in_flight": in_flight,
                   "recoveries": recoveries, "outbox_event_id": event.id}
    row.row_version += 1
    db.flush()
    _audit(db, None, REVERSAL_FINALIZED, f"Reversal #{row.id} finalized by run {run.number}", row,
           instructions_cancelled=cancelled, instructions_in_flight=in_flight,
           recoveries=[r["proposal_id"] for r in recoveries], outbox_event_id=event.id)
    return {"cancelled": cancelled, "in_flight": in_flight, "recoveries": len(recoveries)}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def reversal_json(row: PayrollReversal) -> dict:
    return {
        "id": row.id, "original_run_id": row.original_run_id, "reversal_run_id": row.reversal_run_id,
        "legal_entity_id": row.legal_entity_id, "pay_group_id": row.pay_group_id,
        "run_employee_ids": list(row.run_employee_ids or []), "employee_ids": list(row.employee_ids or []),
        "selected": row.selected, "reason": row.reason, "status": row.status,
        "preview": {cur: {"gross": {"amount": v["gross"], "currency": cur}, "net": {"amount": v["net"], "currency": cur},
                          "employees": v["employees"]} for cur, v in (row.preview or {}).items()},
        "effects": row.effects or {}, "requested_by_user_id": row.requested_by_user_id,
        "approvals": [a for a in (
            {"step": 1, "user_id": row.first_approver_user_id,
             "at": row.first_approved_at.isoformat() if row.first_approved_at else None, "comment": row.first_comment},
            {"step": 2, "user_id": row.second_approver_user_id,
             "at": row.second_approved_at.isoformat() if row.second_approved_at else None,
             "comment": row.second_comment}) if a["user_id"] is not None],
        "decision_note": row.decision_note,
        "finalized_at": row.finalized_at.isoformat() if row.finalized_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None, "row_version": row.row_version,
    }


def get_reversal(db: Session, principal: Principal, reversal_id: int) -> dict:
    return reversal_json(load_reversal(db, principal, reversal_id))


def list_reversals(db: Session, principal: Principal, *, run_id: Optional[int] = None, status: Optional[str] = None,
                   skip: int = 0, limit: int = 50) -> dict:
    if not principal.has(READ):
        raise forbidden()
    query = db.query(PayrollReversal).filter(
        PayrollReversal.company_id == principal.company_id,
        access.run_scope_clause(principal, READ, PayrollReversal.legal_entity_id, PayrollReversal.pay_group_id))
    if run_id is not None:
        query = query.filter(PayrollReversal.original_run_id == run_id)
    if status is not None:
        query = query.filter(PayrollReversal.status == status)
    total = query.count()
    rows = query.order_by(PayrollReversal.id.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": [reversal_json(r) for r in rows]}
