"""Payment instruction status, evidence and history (3.7 §8.1, §9, §10; decision I-17; F-10).

* ``transition(db, principal, instruction, action, ...)`` is the ONLY writer of
  ``payment_instructions.status``. It locks the row (``FOR UPDATE``), checks the optimistic
  ``row_version`` (409 ROW_VERSION_CONFLICT), looks the move up in ``state_machines.PAYMENT_INSTRUCTION``
  (409 with the machine's code), checks the permission and the entity scope (403 / 404; system actions
  need ``system=True``), requires a reason where the table says so, and refuses any declared guard the
  caller did not verify (409 GUARD_NOT_CHECKED). Every applied move appends a ``payment_events`` row;
  the audit log gets only the events the table names (MANUAL_SETTLEMENT, MISMATCH_RESOLVED,
  PAYMENT_REISSUED): instruction history lives in ``payment_events`` (3.9 §8).
* ``apply_evidence``: statement lines, signed provider events and bank reports go through
  ``state_machines.apply_event``: forward jumps are taken, older or duplicate evidence never moves a
  status backwards (EC-PAY-07) and contradictions are kept as CONFLICT events for review. A provider
  event id is processed once per source (EC-PAY-06). A SANDBOX event never touches an instruction that
  was not submitted through the sandbox (3.7 §7.2). ``evidence_status`` becomes VERIFIED only here, when
  settlement evidence is recorded (F-10); the database refuses SETTLED without it.
* Failures and returns: the instruction becomes FAILED / REJECTED / RETURNED with the bank's reason as a
  plain message, the obligation is open again, the account is marked failed through the bank-accounts
  service and the employee is told to check their bank details (masked, EC-PAY-04, EC-BNK-10, EC-PAY-21).
* Manual proof (EC-PAY-14): one person uploads evidence of a payment made outside the system; a second
  person with the reconcile permission reviews it before the instruction may settle.
* ``sync_after``: obligations, batches and runs follow their instructions (3.5 §5.9-§5.10): batch and
  run settlement actions come from ``state_machines.batch_settlement_action`` / ``run_settlement_action``
  and run through ``payment_batches.transition`` and ``runs.transition``; a run (batch) is RECONCILED
  when every settled instruction of it is matched to bank evidence.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import clock, storage
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.payment_flows import (
    PaymentBatch,
    PaymentEvent,
    PaymentInstruction,
    PaymentManualSettlement,
    PaymentObligation,
)
from app.payroll import audit as payroll_audit
from app.payroll import state_machines as sm
from app.payroll.banking.statements import return_reason_message
from app.payroll.money import amount_str, quantize_amount
from app.payroll.services import access
from app.services.audit_log_service import AuditLogService

PREPARE = sm.PAYMENT_PREPARE
APPROVE = sm.PAYMENT_APPROVE
SUBMIT = sm.PAYMENT_SUBMIT
RECONCILE = sm.PAYMENT_RECONCILE
VIEW_PERMISSIONS = (PREPARE, APPROVE, SUBMIT, RECONCILE)
AUDIT_MODULE = payroll_audit.PAYMENTS
logger = logging.getLogger(__name__)
MANUAL_SETTLEMENT = "MANUAL_SETTLEMENT"
MANUAL_PROOF_SUBMITTED = "MANUAL_PROOF_SUBMITTED"
MANUAL_PROOF_REJECTED = "MANUAL_PROOF_REJECTED"
PAYMENT_FAILED = "PAYMENT_FAILED"
EVIDENCE_CATEGORY = "payment-evidence"
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
EVIDENCE_TYPES = (
    (b"%PDF-", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
)
SETTLED = frozenset(("PARTIALLY_SETTLED", "SETTLED", "RECONCILED"))
FULLY_SETTLED = frozenset(("SETTLED", "RECONCILED"))
IN_FLIGHT = frozenset(("SUBMITTED", "ACKNOWLEDGED", "PROCESSING"))
FAILURES = frozenset(("REJECTED", "FAILED"))
RETURNS = frozenset(("RETURNED", "REVERSED"))
UNSENT = frozenset(("NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED"))
EXCLUDED = frozenset(("ON_HOLD", "CANCELLED", "REISSUED"))
# Fields a transition may set besides the status (everything else is server-owned elsewhere).
_UPDATABLE = frozenset((
    "batch_id", "funding_account_id", "status_reason", "status_note", "submission_channel", "evidence_status",
    "evidence_source", "evidence_ref", "provider_ref", "expected_settlement_date", "submitted_at", "settled_at",
    "settled_amount", "variance_amount", "variance_reason", "reconciled_at", "returned_at", "failure_code",
    "failure_message", "carried_at", "reissue_request",
))
_GUARD_ERRORS = {
    "DUAL_CONTROL": (403, "DUAL_CONTROL"),
    "STEP_UP_MFA": (403, "STEP_UP_REQUIRED"),
    "FUNDING_SIGNATORY": (403, "NOT_A_SIGNATORY"),
    "SECOND_REVIEWER": (403, "SECOND_REVIEWER_REQUIRED"),
    "EVIDENCE_UPLOADED": (409, "EVIDENCE_REQUIRED"),
    "BANK_ACCOUNT_USABLE": (409, "BANK_ACCOUNT_NOT_USABLE"),
    "FILE_HASH_UNCHANGED": (409, "FILE_HASH_CHANGED"),
    "REISSUE_APPROVAL": (409, "REISSUE_APPROVAL_REQUIRED"),
    "STATEMENT_MATCHED": (409, "STATEMENT_NOT_MATCHED"),
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now() -> datetime:
    return clock.utcnow()


def audit(db: Session, principal: Optional[Principal], event_name: str, description: str, entity_type: str,
          entity_id: int, company_id: int, **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, event_name, description[:500], entity_type, entity_id,
                        company_id=company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def money_json(amount: Optional[Decimal], currency: Optional[str]) -> Optional[dict]:
    if amount is None or currency is None:
        return None
    return {"amount": amount_str(Decimal(amount), currency), "currency": currency}


def payout_amount(row: PaymentInstruction) -> Decimal:
    """What leaves the funding account: the account amount after FX, else the instruction amount."""
    return Decimal(row.account_amount) if row.account_amount is not None else Decimal(row.amount)


def payout_currency(row: PaymentInstruction) -> str:
    return row.account_currency or row.currency


def settled_in_pay_currency(row: PaymentInstruction, amount: Optional[Decimal]) -> Decimal:
    """The settled part of the instruction in its pay currency (``amount`` is in the payout currency)."""
    if amount is None or row.account_amount is None:
        return Decimal(row.amount) if amount is None else amount
    if amount >= Decimal(row.account_amount):
        return Decimal(row.amount)
    share = Decimal(row.amount) * amount / Decimal(row.account_amount)
    return quantize_amount(share, row.currency, "DOWN")


def reason_text(value: Optional[str], *, required: bool = True, limit: int = 1000) -> Optional[str]:
    text = " ".join((value or "").split())
    if not text:
        if required:
            raise bad_request("Give a reason.", "REASON_REQUIRED")
        return None
    if len(text) < 3:
        raise bad_request("The reason is too short.", "REASON_REQUIRED")
    return text[:limit]


def held_view_permissions(principal: Principal) -> List[str]:
    return [p for p in VIEW_PERMISSIONS if principal.has(p)]


def scope_clause(principal: Principal, column, permissions: Optional[Sequence[str]] = None):
    """Rows whose legal entity is inside the scope of any held payment permission (3.9 §5.4)."""
    held = list(permissions) if permissions is not None else held_view_permissions(principal)
    clauses = [access.run_scope_clause(principal, p, column) for p in held if principal.has(p)]
    if not clauses:
        from sqlalchemy import false

        return false()
    return or_(*clauses)


def covers(principal: Principal, legal_entity_id: int, permissions: Optional[Sequence[str]] = None) -> bool:
    held = list(permissions) if permissions is not None else held_view_permissions(principal)
    return any(principal.has(p) and access.target_scope(principal, p).covers(legal_entity_id) for p in held)


def require_view(principal: Principal) -> None:
    if not held_view_permissions(principal):
        raise forbidden()


def require_permission_in_scope(principal: Principal, permission: str, legal_entity_id: int, what: str) -> None:
    """403 without the permission; 404 when the object's entity is outside its scope."""
    if not principal.has(permission):
        raise forbidden()
    if not access.target_scope(principal, permission).covers(legal_entity_id):
        raise not_found(what)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_instruction(db: Session, company_id: int, instruction_id: int, *, lock: bool = False) -> PaymentInstruction:
    query = db.query(PaymentInstruction).filter(PaymentInstruction.company_id == company_id,
                                                PaymentInstruction.id == instruction_id)
    if lock:
        query = query.with_for_update(of=PaymentInstruction).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Payment instruction")
    return row


def get_instruction_for(db: Session, principal: Principal, instruction_id: int, permission: Optional[str] = None, *,
                        lock: bool = False) -> PaymentInstruction:
    """An instruction of the caller's tenant inside the scope of ``permission`` (or of any payment
    permission when None): 403 without the permission, 404 when unknown or out of scope."""
    if permission is not None and not principal.has(permission):
        raise forbidden()
    if permission is None:
        require_view(principal)
    row = load_instruction(db, principal.company_id, instruction_id, lock=lock)
    if not covers(principal, row.legal_entity_id, [permission] if permission else None):
        raise not_found("Payment instruction")
    return row


# ---------------------------------------------------------------------------
# The instruction transition (the only writer of payment_instructions.status)
# ---------------------------------------------------------------------------
def _guard_failed(guard: str) -> AppError:
    status, code = _GUARD_ERRORS.get(guard, (409, f"GUARD_{guard}"))
    return AppError(status, sm.GUARDS.get(guard, guard), code)


def transition(db: Session, principal: Optional[Principal], instruction: PaymentInstruction | int, action: str, *,
               company_id: Optional[int] = None, reason: Optional[str] = None,
               expected_version: Optional[int] = None, system: bool = False, source: Optional[str] = None,
               event_status: Optional[str] = None, external_event_id: Optional[str] = None,
               evidence_ref: Optional[str] = None, sandbox: bool = False,
               updates: Optional[Mapping[str, Any]] = None, details: Optional[Mapping[str, Any]] = None,
               **guards: bool) -> PaymentInstruction:
    """Move an instruction by ``action`` of ``state_machines.PAYMENT_INSTRUCTION`` and append its event.

    ``guards`` are the caller's verdicts on the transition's guards (``DUAL_CONTROL=True`` ...);
    ``updates`` sets server-owned fields in the same write (``settled_at``, ``submission_channel`` ...).
    """
    unknown = [g for g in guards if g not in sm.GUARDS]
    if unknown:
        raise ValueError(f"unknown instruction guards {unknown}")
    bad_fields = set(updates or {}) - _UPDATABLE
    if bad_fields:
        raise ValueError(f"fields a transition may not set: {sorted(bad_fields)}")
    instruction_id = instruction if isinstance(instruction, int) else instruction.id
    cid = company_id or (principal.company_id if principal is not None else None) or (
        instruction.company_id if isinstance(instruction, PaymentInstruction) else None)
    if cid is None:
        raise ValueError("a system transition of an instruction id needs the company")
    row = load_instruction(db, cid, instruction_id, lock=True)
    try:
        spec = sm.transition_spec(sm.PAYMENT_INSTRUCTION, row.status, action)
    except sm.InvalidTransition as exc:
        raise AppError(409, exc.message, exc.code)
    if spec.permission is None:
        if not system:
            raise forbidden("This step is performed by the system.")
    elif not system:
        if principal is None:
            raise forbidden()
        require_permission_in_scope(principal, spec.permission, row.legal_entity_id, "Payment instruction")
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The payment instruction was changed by someone else. Reload and try again.",
                       "ROW_VERSION_CONFLICT")
    if spec.reason_required:
        reason = reason_text(reason)
    for guard in spec.guards:
        if guard not in guards:
            raise conflict(f"The guard {guard} was not checked for {action}.", "GUARD_NOT_CHECKED")
        if not guards[guard]:
            raise _guard_failed(guard)

    previous = row.status
    for field, value in (updates or {}).items():
        setattr(row, field, value)
    row.status = spec.target
    row.row_version += 1
    db.flush()
    actor = principal.user_id if principal is not None else None
    event = PaymentEvent(
        company_id=row.company_id, instruction_id=row.id, event_status=event_status or action, outcome="APPLIED",
        from_status=previous, to_status=spec.target,
        source=source or ("SYSTEM" if principal is None else "USER"), external_event_id=external_event_id,
        evidence_ref=evidence_ref, sandbox=sandbox, actor_user_id=actor,
        details={k: v for k, v in {"action": action, "reason": reason, **dict(details or {})}.items()
                 if v is not None}, at=now(),
    )
    db.add(event)
    db.flush()
    if spec.audit_event:
        audit(db, principal, spec.audit_event, f"Payment {row.reference or row.id}: {previous} -> {spec.target}"
              + (f": {reason}" if reason else ""), "payment_instruction", row.id, row.company_id, action=action,
              from_status=previous, to_status=spec.target, reason=reason, **dict(details or {}))
    if spec.target in SETTLED and (previous not in SETTLED or "settled_amount" in (updates or {})):
        # Money arrived: if the run employee was reversed while this payment was in flight, the reversal
        # records a recovery receivable for it (EC-COR-03/EC-COR-04; idempotent, never a bank debit).
        from app.payroll.services import reversals

        reversals.on_instruction_settled(db, row)
    return row


def record_unapplied(db: Session, row: PaymentInstruction, event_status: str, outcome: str, *, source: str,
                     external_event_id: Optional[str] = None, evidence_ref: Optional[str] = None,
                     sandbox: bool = False, actor_user_id: Optional[int] = None,
                     details: Optional[Mapping[str, Any]] = None) -> PaymentEvent:
    """Keep evidence that did not change the status (DUPLICATE, STALE, CONFLICT) in the history."""
    event = PaymentEvent(
        company_id=row.company_id, instruction_id=row.id, event_status=event_status, outcome=outcome,
        from_status=row.status, to_status=row.status, source=source, external_event_id=external_event_id,
        evidence_ref=evidence_ref, sandbox=sandbox, actor_user_id=actor_user_id, details=dict(details or {}), at=now(),
    )
    db.add(event)
    db.flush()
    return event


def event_seen(db: Session, company_id: int, source: str, external_event_id: Optional[str]) -> bool:
    if not external_event_id:
        return False
    return db.query(PaymentEvent.id).filter(
        PaymentEvent.company_id == company_id, PaymentEvent.source == source,
        PaymentEvent.external_event_id == external_event_id).first() is not None


# ---------------------------------------------------------------------------
# Evidence (EC-PAY-02, EC-PAY-06, EC-PAY-07, F-10)
# ---------------------------------------------------------------------------
@dataclass
class EvidenceResult:
    outcome: str  # APPLIED | DUPLICATE | STALE | CONFLICT
    instruction: PaymentInstruction
    reason: Optional[str] = None


def apply_evidence(db: Session, principal: Optional[Principal], row: PaymentInstruction, event_status: str, *,
                   source: str, evidence_source: Optional[str] = None, external_event_id: Optional[str] = None,
                   evidence_ref: Optional[str] = None, amount: Optional[Decimal] = None, sandbox: bool = False,
                   reason_code: Optional[str] = None, follow_up: bool = True,
                   updates_extra: Optional[Mapping[str, Any]] = None) -> EvidenceResult:
    """Apply one piece of evidence that ``row`` reached ``event_status`` (statement, webhook, bank report).

    ``amount`` is what the evidence says arrived, in the payout currency (a settlement for less than the
    instruction is PARTIALLY_SETTLED, never SETTLED). Settlement evidence marks the instruction VERIFIED.
    ``updates_extra`` adds server-owned fields to the same write (an accepted variance, for example).
    """
    cid = row.company_id
    actor = principal.user_id if principal is not None else None
    if event_seen(db, cid, source, external_event_id):
        return EvidenceResult("DUPLICATE", row, "EVENT_ALREADY_PROCESSED")
    row = load_instruction(db, cid, row.id, lock=True)
    if sandbox and row.submission_channel != sm.SubmissionChannel.SANDBOX.value:
        record_unapplied(db, row, event_status, "CONFLICT", source=source, external_event_id=external_event_id,
                         evidence_ref=evidence_ref, sandbox=True, actor_user_id=actor,
                         details={"reason": "SANDBOX_EVENT_FOR_PRODUCTION"})
        return EvidenceResult("CONFLICT", row, "SANDBOX_EVENT_FOR_PRODUCTION")
    if not sandbox and row.submission_channel == sm.SubmissionChannel.SANDBOX.value and source != "USER":
        record_unapplied(db, row, event_status, "CONFLICT", source=source, external_event_id=external_event_id,
                         evidence_ref=evidence_ref, actor_user_id=actor, details={"reason": "SANDBOX_INSTRUCTION"})
        return EvidenceResult("CONFLICT", row, "SANDBOX_INSTRUCTION")
    status = event_status
    if status in SETTLED and amount is not None and amount > payout_amount(row):
        # Evidence of more than was paid out is never a settlement (EC-MON-18): keep it for review.
        record_unapplied(db, row, status, "CONFLICT", source=source, external_event_id=external_event_id,
                         evidence_ref=evidence_ref, sandbox=sandbox, actor_user_id=actor,
                         details={"reason": "AMOUNT_EXCEEDS_PAYMENT",
                                  "amount": amount_str(amount, payout_currency(row))})
        return EvidenceResult("CONFLICT", row, "AMOUNT_EXCEEDS_PAYMENT")
    if status == "SETTLED" and amount is not None and amount < payout_amount(row):
        status = "PARTIALLY_SETTLED"
    outcome = sm.apply_event(row.status, status)
    detail = {"amount": amount_str(amount, payout_currency(row)) if amount is not None else None,
              "reason_code": reason_code, "conflict": outcome.reason}
    if not outcome.applied:
        record_unapplied(db, row, status, outcome.outcome, source=source, external_event_id=external_event_id,
                         evidence_ref=evidence_ref, sandbox=sandbox, actor_user_id=actor,
                         details={k: v for k, v in detail.items() if v is not None})
        return EvidenceResult(outcome.outcome, row, outcome.reason)

    at = now()
    updates: Dict[str, Any] = {}
    guards: Dict[str, bool] = {}
    target = outcome.status
    if target == "SUBMITTED":
        guards["PROVIDER_ACCEPTED"] = True
    if target in SETTLED:
        guards["SETTLEMENT_EVIDENCE"] = True
        updates.update(evidence_status=sm.EvidenceStatus.VERIFIED.value, evidence_source=evidence_source,
                       evidence_ref=evidence_ref, settled_at=at, settled_amount=settled_in_pay_currency(row, amount))
    if target in FAILURES or target in RETURNS:
        if row.status in SETTLED:
            guards["RETURN_EVIDENCE"] = True
        message = return_reason_message(reason_code) if target in RETURNS else _failure_message(reason_code)
        updates.update(failure_code=(reason_code or target)[:40], failure_message=message[:300])
        if target in RETURNS:
            updates["returned_at"] = at
    updates.update(dict(updates_extra or {}))
    row = transition(db, None, row, sm.event_action(target), company_id=cid, system=True, source=source,
                     event_status=status, external_event_id=external_event_id, evidence_ref=evidence_ref,
                     sandbox=sandbox, updates=updates,
                     details={k: v for k, v in detail.items() if v is not None}, **guards)
    if target in FAILURES or target in RETURNS:
        on_failure(db, principal, row)
    elif target in FULLY_SETTLED:
        on_settled(db, row)
    if follow_up:
        sync_after(db, principal, [row])
    return EvidenceResult("APPLIED", row)


def _failure_message(code: Optional[str]) -> str:
    if code in (None, "", "FAILED", "REJECTED"):
        return "The bank or provider did not process the payment."
    if code == "AM04":
        return "Insufficient funds in the funding account."
    return return_reason_message(code)


def on_failure(db: Session, principal: Optional[Principal], row: PaymentInstruction) -> None:
    """A failed or returned payment (EC-PAY-04, EC-BNK-10): the bank-accounts service marks the account
    FAILED and asks the employee (masked) to update it; finance sees it in the failures register."""
    from app.payroll.services import obligations

    obligations.mark_bank_account_failed(db, principal, row)
    masked = (row.beneficiary_snapshot or {}).get("account_masked")
    audit(db, principal, PAYMENT_FAILED, f"Payment {row.reference or row.id} {row.status.lower()}: "
          f"{row.failure_message or ''}", "payment_instruction", row.id, row.company_id, status=row.status,
          failure_code=row.failure_code, account=masked, employee_id=row.employee_id)


def on_settled(db: Session, row: PaymentInstruction) -> None:
    """The first settled payment to an account is recorded on it (bank-accounts hook), and the employee is told
    "Salary credited" once per instruction (notify.payroll.payment_status_to_employee)."""
    from app.payroll.services import notifications, obligations

    obligations.mark_bank_account_settled(db, row)
    notifications.payment_credited(db, row)


# ---------------------------------------------------------------------------
# Followers: obligations, batches and runs (3.5 §5.9-§5.10)
# ---------------------------------------------------------------------------
def obligation_figures(rows: Iterable[PaymentInstruction]) -> Dict[str, Decimal]:
    """Intended (live instructions), submitted, settled, returned amounts of one obligation's instructions."""
    figures = {"submitted": Decimal(0), "settled": Decimal(0), "returned": Decimal(0), "failed": Decimal(0)}
    for row in rows:
        amount = Decimal(row.amount)
        if row.submitted_at is not None and row.status not in ("CANCELLED",):
            figures["submitted"] += amount
        if row.status in SETTLED:
            figures["settled"] += Decimal(row.settled_amount if row.settled_amount is not None else row.amount)
        elif row.status in RETURNS:
            figures["returned"] += Decimal(row.settled_amount if row.settled_amount is not None else row.amount)
        elif row.status in FAILURES:
            figures["failed"] += amount
    return figures


def refresh_obligation(db: Session, obligation_id: int, company_id: int) -> PaymentObligation:
    """OPEN, PARTIAL or SETTLED from what has been settled; CANCELLED when nothing is left to pay."""
    obligation = db.query(PaymentObligation).filter(
        PaymentObligation.company_id == company_id, PaymentObligation.id == obligation_id,
    ).with_for_update(of=PaymentObligation).populate_existing().one()
    rows = db.query(PaymentInstruction).filter(PaymentInstruction.company_id == company_id,
                                               PaymentInstruction.obligation_id == obligation_id).all()
    if obligation.status in ("UNCLAIMED",):
        return obligation
    settled = obligation_figures(rows)["settled"]
    if rows and all(r.status == "CANCELLED" for r in rows):
        status = "CANCELLED"
    elif settled >= Decimal(obligation.amount):
        status = "SETTLED"
    elif settled > 0:
        status = "PARTIAL"
    else:
        status = "OPEN"
    if status != obligation.status:
        obligation.status = status
        obligation.row_version += 1
        db.flush()
    return obligation


def sync_after(db: Session, principal: Optional[Principal], rows: Sequence[PaymentInstruction]) -> None:
    """Bring the obligations, batches and runs of ``rows`` in line with their instructions."""
    if not rows:
        return
    cid = rows[0].company_id
    for obligation_id in sorted({r.obligation_id for r in rows}):
        refresh_obligation(db, obligation_id, cid)
    for batch_id in sorted({r.batch_id for r in rows if r.batch_id is not None}):
        sync_batch(db, principal, cid, batch_id)
    for run_id in sorted({r.run_id for r in rows if r.run_id is not None}):
        sync_run(db, principal, cid, run_id)


def _statuses(db: Session, company_id: int, **filters: Any) -> List[str]:
    query = db.query(PaymentInstruction.status).filter(PaymentInstruction.company_id == company_id)
    for name, value in filters.items():
        query = query.filter(getattr(PaymentInstruction, name) == value)
    return [s for (s,) in query.all()]


def all_settled_reconciled(statuses: Sequence[str]) -> bool:
    """Every settled instruction is matched to bank evidence (RECONCILED) and at least one exists."""
    settled = [s for s in statuses if s in SETTLED]
    return bool(settled) and all(s == "RECONCILED" for s in settled)


def can_reconcile(principal: Optional[Principal], legal_entity_id: int) -> bool:
    return principal is not None and principal.has(RECONCILE) and access.target_scope(
        principal, RECONCILE).covers(legal_entity_id)


def sync_batch(db: Session, principal: Optional[Principal], company_id: int, batch_id: int) -> None:
    from app.payroll.services import payment_batches

    batch = db.query(PaymentBatch).filter(PaymentBatch.company_id == company_id, PaymentBatch.id == batch_id).first()
    if batch is None:
        return
    statuses = _statuses(db, company_id, batch_id=batch_id)
    action = sm.batch_settlement_action(batch.status, statuses)
    if action is not None:
        batch = payment_batches.transition(db, None, batch, action, system=True, SETTLEMENT_EVIDENCE=True)
    if batch.status in ("PARTIALLY_SETTLED", "SETTLED") and all_settled_reconciled(statuses) \
            and not any(s in IN_FLIGHT or s == "PARTIALLY_SETTLED" for s in statuses) \
            and can_reconcile(principal, batch.legal_entity_id):
        payment_batches.transition(db, principal, batch, "RECONCILE", SETTLED_INSTRUCTIONS_MATCHED=True)


def sync_run(db: Session, principal: Optional[Principal], company_id: int, run_id: int) -> None:
    """Derive the run's payment state from its instructions (PARTIALLY_PAID, PAID, back to PARTIALLY_PAID on
    a return) and reconcile a PAID run whose settled instructions are all matched to statements."""
    from app.payroll.services import runs

    run = runs.load_run(db, company_id, run_id)
    statuses = _statuses(db, company_id, run_id=run_id)
    action = sm.run_settlement_action(run.status, statuses)
    if action is not None:
        guard = {"RETURN_EVIDENCE": True} if action == "RECORD_RETURN" else {"SETTLEMENT_EVIDENCE": True}
        run = runs.transition(db, None, run, action, system=True, **guard)
    if run.status == "PAID" and all_settled_reconciled(statuses) and can_reconcile(principal, run.legal_entity_id):
        runs.transition(db, principal, run, "RECONCILE", SETTLED_INSTRUCTIONS_MATCHED=True)
    publication_progress(db, company_id, run_id)


def publication_progress(db: Session, company_id: int, run_id: int) -> None:
    """Re-check the run's payslip publication now (ON_PAYMENT_SUBMITTED / ON_SETTLED, 3.6 §7) instead of
    waiting for the hourly sweep. Publication only moves documents and queues notification jobs (nothing is
    rendered here) and is idempotent. It runs in a savepoint of the caller's transaction: a publication
    problem (two settlements publishing the same payslip at once, say) never undoes a payment status; the
    sweep re-checks what was missed."""
    from app.payroll.services import publication

    try:
        with db.begin_nested():
            publication.on_payment_progress(db, company_id, run_id)
    except Exception:  # noqa: BLE001 - best effort by design, see the docstring
        logger.warning("payslip publication re-check failed for run %s", run_id, exc_info=True)


# ---------------------------------------------------------------------------
# Manual proof with a second reviewer (3.7 §7.3, EC-PAY-14)
# ---------------------------------------------------------------------------
def _evidence_type(content: bytes) -> tuple[str, str]:
    for magic, ext, mime in EVIDENCE_TYPES:
        if content.startswith(magic):
            return ext, mime
    raise bad_request("Upload the proof as a PDF, PNG or JPEG file.", "EVIDENCE_TYPE")


def submit_manual_proof(db: Session, principal: Principal, instruction_id: int, *, method: str, reference: str,
                        note: Optional[str], file_name: str, content: bytes,
                        expected_version: Optional[int] = None) -> PaymentManualSettlement:
    """Upload proof of a payment made outside the system; it settles nothing until a second person reviews it."""
    row = get_instruction_for(db, principal, instruction_id, RECONCILE, lock=True)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The payment instruction was changed by someone else.", "ROW_VERSION_CONFLICT")
    if row.status not in IN_FLIGHT and row.status != "PARTIALLY_SETTLED":
        raise conflict(f"Only a submitted payment can be settled manually; this one is {row.status}.",
                       "INVALID_TRANSITION")
    if row.submission_channel == sm.SubmissionChannel.SANDBOX.value:
        raise conflict("Sandbox payments settle only from sandbox events.", "SANDBOX_INSTRUCTION")
    if not content:
        raise bad_request("Attach the proof of payment.", "EVIDENCE_REQUIRED")
    if len(content) > MAX_EVIDENCE_BYTES:
        raise bad_request("The proof file is too large (10 MB at most).", "EVIDENCE_TOO_LARGE")
    ext, mime = _evidence_type(content)
    pending = db.query(PaymentManualSettlement.id).filter(
        PaymentManualSettlement.company_id == row.company_id, PaymentManualSettlement.instruction_id == row.id,
        PaymentManualSettlement.status == "PENDING_REVIEW").first()
    if pending is not None:
        raise conflict("Proof for this payment is already waiting for review.", "PROOF_PENDING")
    reference_text = " ".join((reference or "").split())
    if not reference_text or len(reference_text) > 100:
        raise bad_request("Give the cheque number, receipt or transfer reference (1-100 characters).",
                          "EVIDENCE_REFERENCE_REQUIRED")
    path = storage.save_bytes(row.company_id, EVIDENCE_CATEGORY, content, ext)
    proof = PaymentManualSettlement(
        company_id=row.company_id, instruction_id=row.id, method=method, evidence_reference=reference_text,
        evidence_name=(file_name or f"proof.{ext}")[:200], evidence_mime=mime, evidence_storage_path=path,
        evidence_sha256=hashlib.sha256(content).hexdigest(), note=reason_text(note, required=False),
        status="PENDING_REVIEW", submitted_by_user_id=principal.user_id,
    )
    db.add(proof)
    db.flush()
    audit(db, principal, MANUAL_PROOF_SUBMITTED, f"Proof of manual payment for {row.reference or row.id} uploaded",
          "payment_manual_settlement", proof.id, row.company_id, instruction_id=row.id, method=method,
          sha256=proof.evidence_sha256)
    return proof


def load_manual_proof(db: Session, principal: Principal, proof_id: int, *, lock: bool = False
                      ) -> tuple[PaymentManualSettlement, PaymentInstruction]:
    if not principal.has(RECONCILE):
        raise forbidden()
    query = db.query(PaymentManualSettlement).filter(PaymentManualSettlement.company_id == principal.company_id,
                                                     PaymentManualSettlement.id == proof_id)
    if lock:
        query = query.with_for_update(of=PaymentManualSettlement).populate_existing()
    proof = query.first()
    if proof is None:
        raise not_found("Manual settlement")
    row = load_instruction(db, principal.company_id, proof.instruction_id)
    if not covers(principal, row.legal_entity_id, [RECONCILE]):
        raise not_found("Manual settlement")
    return proof, row


def review_manual_proof(db: Session, principal: Principal, proof_id: int, *, approve: bool, note: Optional[str],
                        expected_version: Optional[int] = None) -> PaymentManualSettlement:
    """The second reviewer accepts (the instruction settles, evidence VERIFIED) or rejects the proof."""
    proof, row = load_manual_proof(db, principal, proof_id, lock=True)
    if expected_version is not None and proof.row_version != expected_version:
        raise conflict("The proof was changed by someone else.", "ROW_VERSION_CONFLICT")
    if proof.status != "PENDING_REVIEW":
        raise conflict("This proof was already reviewed.", "ALREADY_REVIEWED")
    if proof.submitted_by_user_id == principal.user_id:
        raise AppError(403, "A second person must review proof of a manual payment.", "SECOND_REVIEWER_REQUIRED")
    proof.reviewed_by_user_id, proof.reviewed_at = principal.user_id, now()
    proof.row_version += 1
    if not approve:
        proof.status = "REJECTED"
        proof.review_note = reason_text(note)
        db.flush()
        audit(db, principal, MANUAL_PROOF_REJECTED, f"Proof of manual payment for {row.reference or row.id} rejected",
              "payment_manual_settlement", proof.id, row.company_id, instruction_id=row.id, reason=proof.review_note)
        return proof
    proof.status = "APPROVED"
    proof.review_note = reason_text(note, required=False)
    db.flush()
    row = transition(
        db, principal, row, "MANUAL_SETTLE", source="USER", event_status="SETTLED",
        evidence_ref=f"manual:{proof.id}", EVIDENCE_UPLOADED=True, SECOND_REVIEWER=True,
        updates={"evidence_status": sm.EvidenceStatus.VERIFIED.value, "evidence_source": "MANUAL_PROOF",
                 "evidence_ref": f"manual:{proof.id}", "settled_at": now(), "settled_amount": Decimal(row.amount)},
        details={"proof_id": proof.id, "method": proof.method, "uploaded_by_user_id": proof.submitted_by_user_id},
    )
    on_settled(db, row)
    sync_after(db, principal, [row])
    return proof


def manual_proof_json(proof: PaymentManualSettlement) -> dict:
    return {
        "id": proof.id, "instruction_id": proof.instruction_id, "method": proof.method,
        "evidence_reference": proof.evidence_reference, "evidence_name": proof.evidence_name,
        "evidence_mime": proof.evidence_mime, "evidence_sha256": proof.evidence_sha256, "note": proof.note,
        "status": proof.status, "submitted_by_user_id": proof.submitted_by_user_id,
        "reviewed_by_user_id": proof.reviewed_by_user_id, "reviewed_at": proof.reviewed_at,
        "review_note": proof.review_note, "row_version": proof.row_version, "created_at": proof.created_at,
    }


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def employee_label(row: PaymentInstruction) -> dict:
    """The employee-facing label (3.7 §8.1, I-17): never "Paid" before verified settlement evidence."""
    channel = row.submission_channel
    try:
        label = sm.employee_label(row.status, channel, row.evidence_status)
    except sm.InvalidTransition:
        label = sm.employee_payment_label("PROCESSING" if row.status in SETTLED else row.status)
    text = label.text
    if "{date}" in text:
        when = row.settled_at or row.reconciled_at
        text = text.replace("{date}", when.date().isoformat() if when else "")
    if "{amount}" in text:
        received = money_json(row.settled_amount, row.currency)
        text = text.replace("{amount}", f"{received['amount']} {received['currency']}" if received else "")
    return {"key": label.key, "text": text, "badge": label.badge}


def event_json(event: PaymentEvent) -> dict:
    return {
        "id": event.id, "event_status": event.event_status, "outcome": event.outcome,
        "from_status": event.from_status, "to_status": event.to_status, "source": event.source,
        "external_event_id": event.external_event_id, "evidence_ref": event.evidence_ref, "sandbox": event.sandbox,
        "actor_user_id": event.actor_user_id, "details": event.details or {}, "at": event.at,
    }


def instruction_json(row: PaymentInstruction, *, for_self: bool = False) -> dict:
    """An instruction with its beneficiary masked (the ciphertext never leaves the server)."""
    snapshot = dict(row.beneficiary_snapshot or {})
    beneficiary = {k: snapshot.get(k) for k in ("holder_name_masked", "account_masked", "bank_name", "country",
                                                "currency", "routing_scheme", "routing_code_masked")}
    data = {
        "id": row.id, "reference": row.reference, "obligation_id": row.obligation_id, "run_id": row.run_id,
        "employee_id": row.employee_id, "batch_id": row.batch_id, "sequence": row.sequence,
        "amount": money_json(row.amount, row.currency),
        "account_amount": money_json(row.account_amount, row.account_currency),
        "beneficiary": beneficiary, "status": row.status, "label": employee_label(row),
        "submission_channel": row.submission_channel, "evidence_status": row.evidence_status,
        "sandbox": row.submission_channel == sm.SubmissionChannel.SANDBOX.value,
        "submitted_at": row.submitted_at, "settled_at": row.settled_at,
        "settled": money_json(row.settled_amount, row.currency), "reconciled_at": row.reconciled_at,
        "returned_at": row.returned_at, "failure_message": row.failure_message,
        "expected_settlement_date": row.expected_settlement_date,
        "reissue_of_instruction_id": row.reissue_of_instruction_id,
    }
    if not for_self:
        data.update({
            "legal_entity_id": row.legal_entity_id, "funding_account_id": row.funding_account_id,
            "bank_account_id": row.bank_account_id, "status_reason": row.status_reason,
            "status_note": row.status_note, "evidence_source": row.evidence_source,
            "evidence_ref": row.evidence_ref, "provider_ref": row.provider_ref,
            "variance": money_json(row.variance_amount, row.currency), "variance_reason": row.variance_reason,
            "failure_code": row.failure_code, "reissue_request": row.reissue_request,
            "carried_at": row.carried_at, "fx_conversion_id": row.fx_conversion_id,
            "row_version": row.row_version, "created_at": row.created_at,
        })
    return data


def list_events(db: Session, principal: Principal, instruction_id: int) -> dict:
    row = get_instruction_for(db, principal, instruction_id)
    events = db.query(PaymentEvent).filter(PaymentEvent.company_id == row.company_id,
                                           PaymentEvent.instruction_id == row.id).order_by(PaymentEvent.id).all()
    return {"instruction": instruction_json(row), "events": [event_json(e) for e in events]}


def get_instruction(db: Session, principal: Principal, instruction_id: int) -> dict:
    row = get_instruction_for(db, principal, instruction_id)
    proofs = db.query(PaymentManualSettlement).filter(
        PaymentManualSettlement.company_id == row.company_id,
        PaymentManualSettlement.instruction_id == row.id).order_by(PaymentManualSettlement.id).all()
    replacements = [r for (r,) in db.query(PaymentInstruction.id).filter(
        PaymentInstruction.company_id == row.company_id, PaymentInstruction.reissue_of_instruction_id == row.id)]
    return {**instruction_json(row), "manual_proofs": [manual_proof_json(p) for p in proofs],
            "replaced_by_instruction_ids": replacements}
