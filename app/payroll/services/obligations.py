"""Payment obligations and instruction creation (3.5 §5.8 step 4, §9; 3.7 §3.4, §5, §8.1, §10, §11; EC-PAY-04,
EC-PAY-15, EC-PAY-16, EC-PAY-17, EC-PAY-23, EC-BNK-03, EC-BNK-09, EC-RUN-13).

* On the outbox event ``payroll.run.finalized`` (job ``payments.run_finalized``) every CALCULATED run
  employee with a positive net gets one obligation per currency equal to the net and its instructions.
  A zero or negative net creates nothing here (no employee instruction; employer liabilities stay with
  the run and a negative net is a recovery receivable, EC-PAY-15, EC-PAY-16). Repeating the event adds
  nothing (unique obligation per run employee and currency; unique instruction identity).
* Instructions come from the payee's ACTIVE bank accounts through ``banking.allocation.allocate_net``
  (split payments when ``payroll.bank.split_payments_enabled``, EC-BNK-09) and store a beneficiary
  snapshot: masked JSON for display and the identifiers sealed with the tenant data key, bound to the
  instruction's idempotency key. A later bank change never redirects an instruction (EC-BNK-03).
  An allocation to an account in another currency is converted with an FX snapshot; without a rate the
  instruction waits NOT_READY (never 1:1, EC-PAY-17). A quote older than ``payroll.fx.max_rate_age_hours``
  is re-quoted before batching (both quotes kept in the history); without a usable rate the payment is
  held (FX_RATE_MISSING) until one exists (EC-PAY-20).
* Readiness: no account → NOT_READY (MISSING_BANK_ACCOUNT); cooling-off not over → NOT_READY
  (COOLING_OFF); a failed account → NOT_READY (ACCOUNT_FAILED); an unverified account when
  ``payroll.bank.exclude_unverified_from_payout`` → ON_HOLD (UNVERIFIED_ACCOUNT); a risk hold on the
  account → ON_HOLD (ACCOUNT_REVIEW); a payment hold on the run employee → ON_HOLD (EMPLOYEE_HOLD).
  ``refresh_readiness`` re-checks before batching: system-placed holds lift when their cause is gone,
  a payee's new account replaces a placeholder, a hold placed on the run employee later holds the
  unsent instructions. Releasing a run employee's payment hold on a finalized run
  lifts EMPLOYEE_HOLD in the same transaction (``runs.create_payment_intent`` is rebound to
  ``create_payment_intent`` here); the instruction then joins the next batch (EC-RUN-13).
* Reissue (EC-PAY-04): a FAILED / REJECTED / RETURNED / REVERSED instruction is replaced by a new one for
  the same obligation and amount (no new earnings), to the payee's account as it is now; with
  ``payroll.payments.reissue_requires_approval`` a second person approves it first. The old one is
  REISSUED.
* Balances (EC-PAY-23): per employee, run, legal entity or batch and currency: obligated, intended,
  submitted, settled (paid to date), returned and outstanding = obligated − paid to date, exactly.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, conflict, not_found
from app.core.principal import Principal
from app.models.pay_runs import PayrollRun, PayrollRunEmployee
from app.models.payment_flows import PaymentEvent, PaymentInstruction, PaymentObligation
from app.models.payments import BankAccount, FundingAccount
from app.payroll import state_machines as sm
from app.payroll.banking import allocation as alloc
from app.payroll.banking.errors import AllocationError, CryptoError
from app.payroll.banking.masking import mask_account, mask_iban, mask_name
from app.payroll.fx import FxError
from app.payroll.money import Money
from app.payroll.services import bank_accounts, jobs, keys, outbox
from app.payroll.services import payment_events as events
from app.payroll.services import settings as settings_engine

FINALIZED_TOPIC = "payroll.run.finalized"
HOLD_RELEASED_TOPIC = "payroll.payment_hold_released"
FINALIZED_JOB = "payments.run_finalized"
HOLD_RELEASED_JOB = "payments.hold_released"
PREPARE, APPROVE = events.PREPARE, events.APPROVE
OBLIGATIONS_CREATED = "OBLIGATIONS_CREATED"
REISSUE_REQUESTED = "PAYMENT_REISSUE_REQUESTED"
INSTRUCTIONS_RECREATED = "INSTRUCTIONS_RECREATED"
SNAPSHOT_PURPOSE = keys.DEFAULT_PURPOSE
# Reasons an instruction is not ready or held, as plain messages (exclusions report, 3.7 §8.3).
REASONS = {
    "MISSING_BANK_ACCOUNT": "No active bank account on file",
    "COOLING_OFF": "The bank account is in its cooling-off period",
    "ACCOUNT_FAILED": "A payment to the bank account failed; the account must be fixed",
    "ACCOUNT_INACTIVE": "The bank account is no longer active",
    "UNVERIFIED_ACCOUNT": "The bank account is not verified (payroll.bank.exclude_unverified_from_payout)",
    "ACCOUNT_REVIEW": "The bank account is held for review (duplicate or risk flag)",
    "EMPLOYEE_HOLD": "The employee's payment is on hold",
    "ALLOCATION_INVALID": "The payee's split-payment rules are invalid",
    "FX_RATE_MISSING": "No usable exchange rate for the account currency",
    "FX_QUOTE_EXPIRED": "The exchange-rate quote expired and no current rate exists (EC-PAY-20)",
}
SYSTEM_HOLDS = ("UNVERIFIED_ACCOUNT", "ACCOUNT_REVIEW")
# Holds placed by an approved run reversal (reversals._hold_unsent): only the reversal ends them.
REVERSAL_HOLDS = ("RUN_REVERSAL",)
# Routing keys of bank_accounts.routing, mapped to the bank-file routing schemes (banking.files).
ROUTING_SCHEMES = {"ifsc": "IFSC", "sort_code": "SORT_CODE", "aba": "ABA", "routing_number": "ABA", "bsb": "BSB",
                   "bank_code": "BANK_CODE"}
BIC_KEYS = ("bic", "swift", "swift_bic")


# ---------------------------------------------------------------------------
# Bank accounts and funding accounts (the bank-accounts milestone's tables)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PayeeAccount:
    """A payee account serving now, with the bank-accounts service's snapshot (payable, hold reasons)."""

    row: BankAccount
    snapshot: dict

    @property
    def id(self) -> int:
        return self.row.id

    @property
    def currency(self) -> str:
        return self.row.currency

    @property
    def allocation(self) -> dict:
        return dict(self.row.allocation or {})


def payee_accounts(db: Session, company_id: int, employee_id: int, at: Optional[datetime] = None
                   ) -> List[PayeeAccount]:
    """The employee's accounts serving at ``at`` (default now), in allocation order. A replacement still in
    its cooling-off period does not compete with the account it replaces, which keeps serving until then."""
    items = bank_accounts.usable_accounts(db, company_id, employee_id, at or events.now())
    if not items:
        return []
    ids = [int(item["bank_account_id"]) for item in items]
    rows = {r.id: r for r in db.query(BankAccount).filter(BankAccount.company_id == company_id,
                                                          BankAccount.id.in_(ids))}
    serving = set(ids)
    out = [PayeeAccount(rows[int(item["bank_account_id"])], item) for item in items
           if not (rows[int(item["bank_account_id"])].replaces_account_id in serving and not item["payable"])]
    return sorted(out, key=lambda a: (int(a.allocation.get("order") or 0), a.id))


def load_funding_account(db: Session, company_id: int, funding_account_id: int) -> FundingAccount:
    row = db.query(FundingAccount).filter(FundingAccount.company_id == company_id,
                                          FundingAccount.id == funding_account_id).first()
    if row is None:
        raise not_found("Funding account")
    return row


def mark_bank_account_failed(db: Session, principal: Optional[Principal], row: PaymentInstruction) -> None:
    """A failed or returned payment: the bank-accounts service marks the account FAILED and asks the
    employee (masked) to update it (EC-BNK-10, EC-PAY-04)."""
    if row.bank_account_id is not None:
        bank_accounts.mark_payment_failed(db, row.bank_account_id, row.failure_message or "The payment failed")


def mark_bank_account_settled(db: Session, row: PaymentInstruction) -> None:
    if row.bank_account_id is not None:
        bank_accounts.mark_payment_settled(db, row.bank_account_id)


def routing_of(routing: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(routing scheme, routing code, BIC) from an account's public routing identifiers."""
    values = {str(k).lower(): v for k, v in (routing or {}).items() if isinstance(v, str) and v}
    scheme = code = None
    for key, name in ROUTING_SCHEMES.items():
        if key in values:
            scheme, code = name, values[key]
            break
    bic = next((values[k] for k in BIC_KEYS if k in values), None)
    return scheme, code, bic


@dataclass(frozen=True)
class Readiness:
    status: str  # READY | NOT_READY | ON_HOLD
    reason: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.status == "READY"


def readiness_of(account: Optional[PayeeAccount]) -> Readiness:
    """Whether a payment to ``account`` can go now (3.7 §8.1 NOT_READY / ON_HOLD rules), from the
    bank-accounts service's hold reasons (cooling-off, risk hold, failed or unverified account)."""
    if account is None:
        return Readiness("NOT_READY", "MISSING_BANK_ACCOUNT")
    reasons = list(account.snapshot.get("hold_reasons") or [])
    if any(r.startswith("HELD_") for r in reasons):
        return Readiness("ON_HOLD", "ACCOUNT_REVIEW")
    if any(r.startswith("VERIFICATION_") for r in reasons):
        return Readiness("NOT_READY", "ACCOUNT_FAILED")
    if "UNVERIFIED" in reasons:
        return Readiness("ON_HOLD", "UNVERIFIED_ACCOUNT")
    if "COOLING_OFF" in reasons:
        return Readiness("NOT_READY", "COOLING_OFF")
    if reasons or not account.snapshot.get("payable", False):
        return Readiness("NOT_READY", "ACCOUNT_INACTIVE")
    return Readiness("READY")


def current_readiness(db: Session, row: PaymentInstruction) -> Readiness:
    """Readiness of an instruction's snapshot account as of now (ACCOUNT_INACTIVE when it no longer serves)."""
    if row.bank_account_id is None:
        return Readiness("NOT_READY", "MISSING_BANK_ACCOUNT")
    account = next((a for a in payee_accounts(db, row.company_id, row.employee_id) if a.id == row.bank_account_id),
                   None)
    return readiness_of(account) if account is not None else Readiness("NOT_READY", "ACCOUNT_INACTIVE")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BankRules:
    split_enabled: bool
    max_accounts: int


def _setting(db: Session, company_id: int, key: str, *, on: date, legal_entity_id: int,
             employee_id: Optional[int] = None) -> Any:
    try:
        return settings_engine.resolve(db, company_id, key, on=on, legal_entity_id=legal_entity_id,
                                       employee_id=employee_id).value
    except settings_engine.SettingConfigError as exc:
        raise conflict(exc.message, exc.code)


def bank_rules(db: Session, company_id: int, legal_entity_id: int, on: date) -> BankRules:
    split = bool(_setting(db, company_id, "payroll.bank.split_payments_enabled", on=on, legal_entity_id=legal_entity_id))
    most = int(_setting(db, company_id, "payroll.bank.max_accounts_per_payee", on=on, legal_entity_id=legal_entity_id))
    return BankRules(split, most if split else 1)


def reissue_requires_approval(db: Session, company_id: int, legal_entity_id: int, on: date) -> bool:
    return bool(_setting(db, company_id, "payroll.payments.reissue_requires_approval", on=on,
                         legal_entity_id=legal_entity_id))


# ---------------------------------------------------------------------------
# Beneficiary snapshot (3.7 §3.4, I-10)
# ---------------------------------------------------------------------------
def _snapshot_aad(idempotency_key: str) -> bytes:
    return f"payment_instructions.beneficiary:{idempotency_key}".encode("utf-8")


def beneficiary_snapshot(db: Session, account: PayeeAccount, idempotency_key: str) -> Tuple[dict, str]:
    """Masked display data and the sealed identifiers of ``account`` at this moment (3.7 §3.4, I-10)."""
    clear = bank_accounts.beneficiary_plaintext(db, account.row.company_id, account.snapshot)
    number, iban = clear.get("account_number"), clear.get("iban")
    row = account.row
    scheme, code, bic = routing_of(row.routing or {})
    snapshot = {
        "bank_account_id": row.id, "holder_name_masked": mask_name(row.holder_name),
        "account_masked": mask_iban(iban) if iban else mask_account(number), "last4": row.last4,
        "bank_name": row.bank_name, "country": row.country, "currency": row.currency, "routing_scheme": scheme,
        "routing_code_masked": mask_account(code) if code else None, "fingerprint": row.fingerprint,
        "account_row_version": row.row_version, "verification_status": row.verification_status,
        "source_snapshot_hash": account.snapshot.get("snapshot_hash"), "taken_at": events.now().isoformat(),
    }
    sealed = json.dumps({"holder_name": row.holder_name, "account_number": number, "iban": iban,
                         "routing_scheme": scheme, "routing_code": code, "bic": bic},
                        sort_keys=True, separators=(",", ":"))
    enc = keys.encrypt_field(db, row.company_id, sealed, SNAPSHOT_PURPOSE, aad=_snapshot_aad(idempotency_key))
    return snapshot, enc


def open_snapshot(db: Session, row: PaymentInstruction) -> dict:
    """The sealed beneficiary of an instruction, in clear (bank files and statement matching only)."""
    if not row.beneficiary_enc:
        raise conflict("The payment has no beneficiary on file.", "BENEFICIARY_MISSING")
    try:
        text = keys.decrypt_field(db, row.company_id, row.beneficiary_enc, SNAPSHOT_PURPOSE,
                                  aad=_snapshot_aad(row.idempotency_key))
    except CryptoError:
        raise AppError(500, "The beneficiary snapshot cannot be opened.", "BENEFICIARY_UNREADABLE") from None
    return json.loads(text)


# ---------------------------------------------------------------------------
# Creating obligations and instructions
# ---------------------------------------------------------------------------
def _idempotency_key(obligation: PaymentObligation, account_id: Optional[int], sequence: int) -> str:
    return f"obl:{obligation.id}:acct:{account_id or 0}:seq:{sequence}"


def _next_sequence(db: Session, obligation: PaymentObligation) -> int:
    current = db.query(func.max(PaymentInstruction.sequence)).filter(
        PaymentInstruction.company_id == obligation.company_id,
        PaymentInstruction.obligation_id == obligation.id).scalar()
    return int(current or 0) + 1


def _new_instruction(db: Session, obligation: PaymentObligation, *, account: Optional[PayeeAccount], amount: Decimal,
                     sequence: int, readiness: Readiness, note: Optional[str] = None,
                     reissue_of: Optional[int] = None, account_amount: Optional[Decimal] = None,
                     account_currency: Optional[str] = None, fx_conversion_id: Optional[int] = None,
                     principal: Optional[Principal] = None) -> PaymentInstruction:
    key = _idempotency_key(obligation, account.id if account is not None else None, sequence)
    snapshot, enc = ({}, None) if account is None else beneficiary_snapshot(db, account, key)
    initial = "READY" if readiness.status == "READY" else "NOT_READY"
    row = PaymentInstruction(
        company_id=obligation.company_id, obligation_id=obligation.id, run_id=obligation.run_id,
        employee_id=obligation.employee_id, legal_entity_id=obligation.legal_entity_id,
        bank_account_id=account.id if account is not None else None, sequence=sequence, idempotency_key=key,
        beneficiary_snapshot=snapshot, beneficiary_enc=enc, amount=amount, currency=obligation.currency,
        account_amount=account_amount, account_currency=account_currency, fx_conversion_id=fx_conversion_id,
        status=initial, status_reason=None if initial == "READY" else readiness.reason,
        status_note=note, evidence_status=sm.EvidenceStatus.UNVERIFIED.value, reissue_of_instruction_id=reissue_of,
    )
    db.add(row)
    db.flush()
    row.reference = f"PI{row.id:08d}"
    db.add(PaymentEvent(company_id=row.company_id, instruction_id=row.id, event_status="CREATED", outcome="RECORDED",
                        from_status=initial, to_status=initial, source="SYSTEM" if principal is None else "USER",
                        actor_user_id=principal.user_id if principal is not None else None,
                        details={k: v for k, v in {"reason": row.status_reason, "reissue_of": reissue_of}.items()
                                 if v is not None}, at=events.now()))
    db.flush()
    if readiness.status == "ON_HOLD":
        row = events.transition(db, None, row, "HOLD", system=True, reason=REASONS[readiness.reason],
                                updates={"status_reason": readiness.reason})
    return row


def _fx(db: Session, obligation: PaymentObligation, amount: Decimal, target: str, on: date
        ) -> Tuple[Optional[Decimal], Optional[int]]:
    from app.payroll.services import fx as fx_service

    try:
        conversion = fx_service.convert(db, Money(amount, obligation.currency), target, on=on,
                                        purpose="PAY_TO_PAYMENT", ref_type="payment_obligation", ref_id=obligation.id)
    except (FxError, AppError):
        return None, None
    return Decimal(conversion.result_amount), conversion.id


def build_instructions(db: Session, obligation: PaymentObligation, *, amount: Optional[Decimal] = None,
                       hold_reason: Optional[str] = None, reissue_of: Optional[int] = None,
                       single_account: bool = False, principal: Optional[Principal] = None) -> List[PaymentInstruction]:
    """Instructions for ``amount`` (default: the obligation) from the payee's accounts as they are now."""
    total = Decimal(obligation.amount) if amount is None else amount
    on = obligation.pay_date or events.now().date()
    rules = bank_rules(db, obligation.company_id, obligation.legal_entity_id, on)
    accounts = payee_accounts(db, obligation.company_id, obligation.employee_id)
    sequence = _next_sequence(db, obligation)
    held = Readiness("ON_HOLD", hold_reason) if hold_reason else None
    if not accounts:
        return [_new_instruction(db, obligation, account=None, amount=total, sequence=sequence,
                                 readiness=held or Readiness("NOT_READY", "MISSING_BANK_ACCOUNT"),
                                 reissue_of=reissue_of, principal=principal)]
    if single_account or not rules.split_enabled:
        payable = [a for a in accounts if readiness_of(a).usable]
        candidates = payable or accounts
        primary = next((a for a in candidates if a.allocation.get("type") in ("PRIMARY", "REMAINDER")), candidates[0])
        allocations = [alloc.Allocation(primary.id, "PRIMARY", None, 0, primary.currency)]
        max_accounts = 1
    else:
        allocations = []
        for account in accounts:
            rule = account.allocation
            value = rule.get("value")
            allocations.append(alloc.Allocation(account.id, str(rule.get("type") or "PRIMARY"),
                                                Decimal(str(value)) if value is not None else None,
                                                int(rule.get("order") or 0), account.currency))
        max_accounts = rules.max_accounts
    try:
        result = alloc.allocate_net(total, obligation.currency, allocations, max_accounts=max_accounts)
    except AllocationError as exc:
        raise conflict(exc.message, exc.code)
    if result.outcome != "ALLOCATED":
        note = "; ".join(f"{i.code}: {i.message}" for i in result.issues) or result.outcome
        return [_new_instruction(db, obligation, account=None, amount=total, sequence=sequence,
                                 readiness=held or Readiness("NOT_READY", "ALLOCATION_INVALID"), note=note[:1000],
                                 reissue_of=reissue_of, principal=principal)]
    by_id = {a.id: a for a in accounts}
    note = "; ".join(i.message for i in result.issues) or None
    created = []
    for line in result.payable_lines:
        account = by_id[int(line.account_id)]
        readiness = held or readiness_of(account)
        account_amount = conversion_id = None
        account_currency = None
        if line.requires_fx:
            account_amount, conversion_id = _fx(db, obligation, line.amount, line.account_currency, on)
            account_currency = line.account_currency if account_amount is not None else None
            if account_amount is None and readiness.status == "READY":
                readiness = Readiness("NOT_READY", "FX_RATE_MISSING")
        created.append(_new_instruction(
            db, obligation, account=account, amount=line.amount, sequence=sequence, readiness=readiness,
            note=(line.explanation if note is None else f"{line.explanation}; {note}")[:1000], reissue_of=reissue_of,
            account_amount=account_amount, account_currency=account_currency, fx_conversion_id=conversion_id,
            principal=principal))
    return created


def _payment_hold(row: PayrollRunEmployee) -> bool:
    return row.hold_type == "PAYMENT"


def create_for_run(db: Session, company_id: int, run_id: int) -> dict:
    """Obligations and instructions of a finalized run (idempotent; the outbox handler calls it)."""
    from app.payroll.services import runs

    run = runs.load_run(db, company_id, run_id)
    if not sm.is_run_finalized(run.status):
        raise jobs.PermanentJobError(f"run {run_id} is not finalized")
    period = runs.period_of(db, run)
    rows = db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.status == "CALCULATED").order_by(PayrollRunEmployee.id).all()
    existing = {(o.run_employee_id, o.currency) for o in db.query(PaymentObligation).filter(
        PaymentObligation.company_id == company_id, PaymentObligation.run_id == run.id)}
    created = skipped = 0
    for row in rows:
        net = Decimal(row.net) if row.net is not None else None
        if net is None or net <= 0:  # EC-PAY-15 / EC-PAY-16: nothing to pay the employee
            skipped += 1
            continue
        if (row.id, row.currency) in existing:
            continue
        obligation = PaymentObligation(
            company_id=company_id, source_type="RUN_EMPLOYEE", run_id=run.id, run_employee_id=row.id,
            payee_type="EMPLOYEE", employee_id=row.employee_id, legal_entity_id=run.legal_entity_id,
            pay_group_id=run.pay_group_id, amount=net, currency=row.currency, status="OPEN",
            pay_date=period.pay_date,
        )
        db.add(obligation)
        db.flush()
        build_instructions(db, obligation, hold_reason="EMPLOYEE_HOLD" if _payment_hold(row) else None)
        created += 1
    if created:
        events.audit(db, None, OBLIGATIONS_CREATED, f"Payment obligations for run {run.number}: {created}",
                     "payroll_run", run.id, company_id, created=created, without_payment=skipped)
    return {"run_id": run.id, "created": created, "without_payment": skipped}


@jobs.register(FINALIZED_JOB)
def _on_run_finalized(ctx: jobs.JobContext) -> dict:
    run_id = ctx.payload.get("run_id")
    if not isinstance(run_id, int):
        raise jobs.PermanentJobError("payroll.run.finalized needs a run_id")
    return create_for_run(ctx.db, ctx.company_id, run_id)


@jobs.register(HOLD_RELEASED_JOB)
def _on_hold_released(ctx: jobs.JobContext) -> dict:
    run_employee_id = ctx.payload.get("run_employee_id")
    if not isinstance(run_employee_id, int):
        raise jobs.PermanentJobError("payroll.payment_hold_released needs a run_employee_id")
    return {"released": release_employee_hold(ctx.db, ctx.company_id, run_employee_id)}


outbox.subscribe(FINALIZED_TOPIC, FINALIZED_JOB)
# Releases queued by create_payment_intent before the obligations existed (idempotent handler).
outbox.subscribe(HOLD_RELEASED_TOPIC, HOLD_RELEASED_JOB)


# ---------------------------------------------------------------------------
# Readiness and holds
# ---------------------------------------------------------------------------
def _run_employee_for(db: Session, obligation: PaymentObligation) -> Optional[PayrollRunEmployee]:
    if obligation.run_employee_id is None:
        return None
    return db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == obligation.company_id,
                                               PayrollRunEmployee.id == obligation.run_employee_id).first()


def release_employee_hold(db: Session, company_id: int, run_employee_id: int, *,
                          principal: Optional[Principal] = None, being_released: bool = False) -> int:
    """EC-RUN-13: a released payment hold lifts EMPLOYEE_HOLD; the instructions join the next batch.
    Nothing happens while the run employee is still on a payment hold (a stale or replayed event), unless
    the runs service is releasing that hold in this transaction (``being_released``)."""
    run_employee = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == company_id,
                                                       PayrollRunEmployee.id == run_employee_id).first()
    if run_employee is None or (_payment_hold(run_employee) and not being_released):
        return 0
    rows = db.query(PaymentInstruction).join(
        PaymentObligation, (PaymentObligation.company_id == PaymentInstruction.company_id)
        & (PaymentObligation.id == PaymentInstruction.obligation_id),
    ).filter(PaymentInstruction.company_id == company_id, PaymentObligation.run_employee_id == run_employee_id,
             PaymentInstruction.status == "ON_HOLD", PaymentInstruction.status_reason == "EMPLOYEE_HOLD").all()
    released = []
    for row in rows:
        row = events.transition(db, principal, row, "RELEASE_HOLD", system=True,
                                reason="The employee's payment hold was released on the run",
                                updates={"status_reason": None}, details={"run_employee_id": run_employee_id})
        released.append(ready_if_usable(db, row))
    events.sync_after(db, principal, released)
    return len(released)


def create_payment_intent(db: Session, principal: Principal, run: PayrollRun, row: PayrollRunEmployee) -> dict:
    """``runs.create_payment_intent`` (EC-RUN-13), installed by ``install_hooks``: a released payment hold of
    a finalized run lifts EMPLOYEE_HOLD on the run employee's unsent instructions in the same transaction;
    they join the next batch with its own approval. The runs service calls this before it clears the hold,
    so the hold is treated as released here.

    When the finalization event has not created the obligations yet, the release is queued instead
    (outbox ``payroll.payment_hold_released``): obligations created after the release are not held, and
    the queued job lifts a hold placed by a finalization job that ran concurrently."""
    exists = db.query(PaymentObligation.id).filter(PaymentObligation.company_id == run.company_id,
                                                   PaymentObligation.run_employee_id == row.id).first() is not None
    if not exists:
        event = outbox.emit(db, HOLD_RELEASED_TOPIC, {"run_id": run.id, "run_employee_id": row.id})
        return {"status": "QUEUED", "outbox_event_id": event.id}
    released = release_employee_hold(db, run.company_id, row.id, principal=principal, being_released=True)
    return {"status": "RELEASED" if released else "NOTHING_HELD", "released_instructions": released}


def release_hold(db: Session, principal: Principal, instruction_id: int, reason: str, *,
                 expected_version: Optional[int] = None) -> PaymentInstruction:
    """A person releases a held payment (payroll:payment:approve). A hold whose cause is still present is
    refused: the run employee's payment hold is released on the run (EC-RUN-13), an unverified or flagged
    account is fixed on the account; system holds lift by themselves once their cause is gone."""
    row = events.get_instruction_for(db, principal, instruction_id, APPROVE, lock=True)
    if row.status == "ON_HOLD":
        obligation = db.query(PaymentObligation).filter(PaymentObligation.company_id == row.company_id,
                                                        PaymentObligation.id == row.obligation_id).one()
        run_employee = _run_employee_for(db, obligation)
        if run_employee is not None and _payment_hold(run_employee):
            raise conflict("The employee's payment is on hold on the run; release it there.", "RUN_EMPLOYEE_HELD")
        if row.status_reason in REVERSAL_HOLDS:
            raise conflict("The payment is held by an approved run reversal; it is cancelled when the reversal is "
                           "finalized and cannot be released by hand.", "REVERSAL_HOLD")
        if row.status_reason in SYSTEM_HOLDS and current_readiness(db, row).status == "ON_HOLD":
            raise conflict(f"{REASONS[row.status_reason]}: fix the bank account first.", "HOLD_CAUSE_PRESENT")
    row = events.transition(db, principal, row, "RELEASE_HOLD", reason=reason, expected_version=expected_version,
                            updates={"status_reason": None})
    row = ready_if_usable(db, row)
    events.sync_after(db, principal, [row])
    return row


def fx_target(row: PaymentInstruction) -> Optional[str]:
    """The account currency an instruction still needs a conversion into (EC-PAY-17), else None."""
    target = (row.beneficiary_snapshot or {}).get("currency")
    if target and target != row.currency and row.account_amount is None:
        return target
    return None


def _requote(db: Session, row: PaymentInstruction, target: str, on: Optional[date] = None) -> bool:
    """Convert a waiting instruction into its account currency with a current rate (EC-PAY-17, EC-PAY-20:
    never 1:1, never a stale rate). Records both amounts and the conversion; False without a usable rate."""
    obligation = db.query(PaymentObligation).filter(PaymentObligation.company_id == row.company_id,
                                                    PaymentObligation.id == row.obligation_id).one()
    when = on or obligation.pay_date or events.now().date()
    amount, conversion_id = _fx(db, obligation, Decimal(row.amount), target, when)
    if amount is None:
        return False
    row.account_amount, row.account_currency, row.fx_conversion_id = amount, target, conversion_id
    row.row_version += 1
    db.flush()
    return True


def fx_quote_expired(db: Session, row: PaymentInstruction) -> bool:
    """EC-PAY-20: the instruction's FX quote is older than ``payroll.fx.max_rate_age_hours`` (a stale rate
    is never paid out: an unsent payment is re-quoted before batching, a batched one blocks the release)."""
    if row.fx_conversion_id is None:
        return False
    from app.models.catalogue import FxConversion
    from app.payroll.services import fx as fx_service

    conversion = db.query(FxConversion).filter(FxConversion.company_id == row.company_id,
                                               FxConversion.id == row.fx_conversion_id).first()
    if conversion is None or conversion.created_at is None:
        return True
    max_age = fx_service.settings_for(db, row.company_id, events.now().date()).max_rate_age_hours
    return conversion.created_at + timedelta(hours=max_age) < events.now()


def _requote_expired(db: Session, row: PaymentInstruction) -> PaymentInstruction:
    """Re-quote an unsent instruction whose FX quote expired (EC-PAY-20) with today's rate, recording both
    quotes; without a usable rate it is held (FX_RATE_MISSING) until one exists. It then joins a batch whose
    approval covers the new amount."""
    target = row.account_currency or (row.beneficiary_snapshot or {}).get("currency")
    old_amount, old_conversion = row.account_amount, row.fx_conversion_id
    if target and _requote(db, row, target, on=events.now().date()):
        db.add(PaymentEvent(
            company_id=row.company_id, instruction_id=row.id, event_status="FX_REQUOTED", outcome="RECORDED",
            from_status=row.status, to_status=row.status, source="SYSTEM", at=events.now(),
            details={"previous_amount": events.money_json(old_amount, target), "previous_conversion_id": old_conversion,
                     "amount": events.money_json(row.account_amount, target), "conversion_id": row.fx_conversion_id}))
        db.flush()
        return row
    return events.transition(db, None, row, "HOLD", system=True, reason=REASONS["FX_QUOTE_EXPIRED"],
                             updates={"status_reason": "FX_RATE_MISSING"})


def _release_fx_hold(db: Session, row: PaymentInstruction) -> Optional[PaymentInstruction]:
    """A payment held for a missing rate is re-quoted and released once a current rate exists."""
    target = row.account_currency or (row.beneficiary_snapshot or {}).get("currency")
    if not target or not _requote(db, row, target, on=events.now().date()):
        return None
    row = events.transition(db, None, row, "RELEASE_HOLD", system=True, reason="A current exchange rate exists",
                            updates={"status_reason": None})
    return ready_if_usable(db, row)


def ready_if_usable(db: Session, row: PaymentInstruction) -> PaymentInstruction:
    """NOT_READY -> READY when the snapshot's account is usable now (or hold it / record why not). An
    instruction to an account in another currency becomes READY only once it is converted (EC-PAY-17)."""
    if row.status != "NOT_READY" or row.bank_account_id is None:
        return row
    readiness = current_readiness(db, row)
    if readiness.status == "READY":
        target = fx_target(row)
        if target is not None and not _requote(db, row, target):
            readiness = Readiness("NOT_READY", "FX_RATE_MISSING")
        else:
            return events.transition(db, None, row, "MARK_READY", system=True, BANK_ACCOUNT_USABLE=True,
                                     updates={"status_reason": None})
    if readiness.status == "ON_HOLD":
        return events.transition(db, None, row, "HOLD", system=True, reason=REASONS[readiness.reason],
                                 updates={"status_reason": readiness.reason})
    if readiness.reason and row.status_reason != readiness.reason:
        row.status_reason = readiness.reason
        row.row_version += 1
        db.flush()
    return row


def refresh_readiness(db: Session, principal: Principal, run_id: int) -> dict:
    """Re-check the unsent instructions of a run outside batches before batching (3.7 §8.1, §8.3)."""
    rows = db.query(PaymentInstruction).filter(
        PaymentInstruction.company_id == principal.company_id, PaymentInstruction.run_id == run_id,
        PaymentInstruction.batch_id.is_(None),
        PaymentInstruction.status.in_(("NOT_READY", "READY", "ON_HOLD"))).order_by(PaymentInstruction.id).all()
    changed: List[PaymentInstruction] = []
    for row in rows:
        obligation = db.query(PaymentObligation).filter(PaymentObligation.company_id == row.company_id,
                                                        PaymentObligation.id == row.obligation_id).one()
        run_employee = _run_employee_for(db, obligation)
        employee_held = run_employee is not None and _payment_hold(run_employee)
        if employee_held and row.status in ("NOT_READY", "READY"):
            changed.append(events.transition(db, None, row, "HOLD", system=True, reason=REASONS["EMPLOYEE_HOLD"],
                                             updates={"status_reason": "EMPLOYEE_HOLD"}))
            continue
        if row.status == "READY":
            # A risk hold or an unverified account found after the instruction became READY holds it before
            # it can join a batch (a later fix of the account lifts the system hold below).
            readiness = current_readiness(db, row)
            if readiness.status == "ON_HOLD":
                changed.append(events.transition(db, None, row, "HOLD", system=True, reason=REASONS[readiness.reason],
                                                 updates={"status_reason": readiness.reason}))
                continue
        if row.status == "READY" and fx_quote_expired(db, row):  # EC-PAY-20: never pay out a stale quote
            changed.append(_requote_expired(db, row))
            continue
        if row.status == "ON_HOLD" and row.status_reason == "FX_RATE_MISSING":
            released = _release_fx_hold(db, row)
            if released is not None:
                changed.append(released)
            continue
        if row.status == "ON_HOLD":
            # System-placed holds lift when their cause is gone; a hold placed by a person needs a person.
            if row.status_reason not in SYSTEM_HOLDS or current_readiness(db, row).status == "ON_HOLD":
                continue
            row = events.transition(db, None, row, "RELEASE_HOLD", system=True,
                                    reason="The cause of the hold on the bank account is gone",
                                    updates={"status_reason": None})
            changed.append(ready_if_usable(db, row))
            continue
        if row.status == "NOT_READY" and row.bank_account_id is None:
            if not payee_accounts(db, row.company_id, row.employee_id):
                continue
            # A placeholder without an account is replaced by instructions to the account now on file.
            changed.append(events.transition(db, principal, row, "CANCEL", reason="A bank account is now on file",
                                             updates={"status_reason": "REPLACED"}))
            changed.extend(build_instructions(db, obligation, amount=Decimal(row.amount), principal=principal))
            continue
        if row.status == "NOT_READY" and current_readiness(db, row).reason == "ACCOUNT_INACTIVE":
            # The snapshot account stopped serving before anything was sent: pay the account now on file.
            changed.append(events.transition(db, principal, row, "CANCEL",
                                             reason="The bank account was replaced before payment",
                                             updates={"status_reason": "REPLACED"}))
            changed.extend(build_instructions(db, obligation, amount=Decimal(row.amount), principal=principal))
            continue
        if row.status == "NOT_READY":
            changed.append(ready_if_usable(db, row))
    events.sync_after(db, principal, changed)
    return {"checked": len(rows), "changed": len(changed)}


# ---------------------------------------------------------------------------
# Bank changes after approval (EC-BNK-03)
# ---------------------------------------------------------------------------
def bank_changes(db: Session, rows: Sequence[PaymentInstruction]) -> List[dict]:
    """Instructions whose payee's bank details changed after the snapshot (EC-BNK-03): the snapshot is kept
    (no silent redirect); finance may proceed with it or cancel and recreate the instructions with
    re-approval."""
    warnings = []
    for row in rows:
        if row.bank_account_id is None or row.status in ("CANCELLED", "REISSUED"):
            continue
        snapshot = row.beneficiary_snapshot or {}
        account = db.query(BankAccount).filter(BankAccount.company_id == row.company_id,
                                               BankAccount.id == row.bank_account_id).first()
        change = None
        if account is None or account.status != "ACTIVE" or account.replaced_by_account_id is not None:
            change = "ACCOUNT_REPLACED"
        elif account.fingerprint != snapshot.get("fingerprint"):
            change = "ACCOUNT_DETAILS_CHANGED"
        elif row.created_at is not None and bank_accounts.changed_since(db, row.company_id, row.employee_id,
                                                                        row.created_at):
            change = "BANK_DETAILS_CHANGED"
        if change:
            warnings.append({"instruction_id": row.id, "employee_id": row.employee_id, "change": change,
                             "snapshot_account": snapshot.get("account_masked"),
                             "message": "Bank details changed after approval",
                             "options": ["PROCEED_WITH_SNAPSHOT", "RECREATE_INSTRUCTIONS"]})
    return warnings


def recreate_instructions(db: Session, principal: Principal, rows: Sequence[PaymentInstruction], reason: str
                          ) -> List[PaymentInstruction]:
    """Cancel unsent instructions and recreate them from the current bank details (they need a new batch
    and its approval)."""
    created: List[PaymentInstruction] = []
    for row in rows:
        if row.status not in ("READY", "NOT_READY", "ON_HOLD"):
            raise conflict("Only unsent instructions outside a batch can be recreated.", "INVALID_TRANSITION")
        obligation = db.query(PaymentObligation).filter(PaymentObligation.company_id == row.company_id,
                                                        PaymentObligation.id == row.obligation_id).one()
        events.transition(db, principal, row, "CANCEL", reason=reason, updates={"status_reason": "RECREATED"})
        created.extend(build_instructions(db, obligation, amount=Decimal(row.amount), principal=principal))
    events.audit(db, principal, INSTRUCTIONS_RECREATED, f"{len(rows)} payment instruction(s) recreated from current "
                 "bank details", "payment_instruction", rows[0].id if rows else 0, principal.company_id,
                 cancelled=[r.id for r in rows], created=[r.id for r in created], reason=reason)
    events.sync_after(db, principal, list(rows) + created)
    return created


# ---------------------------------------------------------------------------
# Reissue (3.7 §10, EC-PAY-04)
# ---------------------------------------------------------------------------
REISSUABLE = ("REJECTED", "FAILED", "RETURNED", "REVERSED")


def _usable_account(db: Session, row: PaymentInstruction) -> Optional[PayeeAccount]:
    return next((a for a in payee_accounts(db, row.company_id, row.employee_id) if readiness_of(a).usable), None)


def _perform_reissue(db: Session, principal: Principal, row: PaymentInstruction, reason: str,
                     requested_by: Optional[int]) -> List[PaymentInstruction]:
    obligation = db.query(PaymentObligation).filter(PaymentObligation.company_id == row.company_id,
                                                    PaymentObligation.id == row.obligation_id).one()
    old = events.transition(db, principal, row, "REISSUE", system=True, reason=reason, REISSUE_APPROVAL=True,
                            BANK_ACCOUNT_USABLE=True, updates={"reissue_request": None},
                            details={"requested_by_user_id": requested_by, "approved_by_user_id": principal.user_id})
    created = build_instructions(db, obligation, amount=Decimal(old.amount), reissue_of=old.id, single_account=True,
                                 principal=principal)
    events.sync_after(db, principal, [old, *created])
    return created


def request_reissue(db: Session, principal: Principal, instruction_id: int, reason: str, *,
                    expected_version: Optional[int] = None) -> dict:
    """Reissue a failed or returned payment to the payee's fixed account; needs approval when configured."""
    row = events.get_instruction_for(db, principal, instruction_id, PREPARE, lock=True)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The payment instruction was changed by someone else.", "ROW_VERSION_CONFLICT")
    if row.status not in REISSUABLE:
        raise conflict(f"Only a failed or returned payment can be reissued; this one is {row.status}.",
                       "INVALID_TRANSITION")
    text = events.reason_text(reason)
    if _usable_account(db, row) is None:
        raise conflict("Fix the payee's bank details first: no usable bank account is on file.",
                       "BANK_ACCOUNT_NOT_USABLE")
    if reissue_requires_approval(db, row.company_id, row.legal_entity_id, events.now().date()):
        if row.reissue_request:
            raise conflict("A reissue of this payment is already waiting for approval.", "REISSUE_PENDING")
        row.reissue_request = {"requested_by_user_id": principal.user_id, "at": events.now().isoformat(),
                               "reason": text}
        row.row_version += 1
        db.flush()
        events.audit(db, principal, REISSUE_REQUESTED, f"Reissue of payment {row.reference} requested: {text}",
                     "payment_instruction", row.id, row.company_id, reason=text)
        return {"status": "APPROVAL_PENDING", "instruction": events.instruction_json(row), "created": []}
    created = _perform_reissue(db, principal, row, text, principal.user_id)
    return {"status": "REISSUED", "instruction": events.instruction_json(row),
            "created": [events.instruction_json(r) for r in created]}


def approve_reissue(db: Session, principal: Principal, instruction_id: int, note: Optional[str] = None, *,
                    expected_version: Optional[int] = None) -> dict:
    """A second person approves a requested reissue (payroll.payments.reissue_requires_approval)."""
    row = events.get_instruction_for(db, principal, instruction_id, APPROVE, lock=True)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The payment instruction was changed by someone else.", "ROW_VERSION_CONFLICT")
    request = dict(row.reissue_request or {})
    if not request:
        raise conflict("No reissue of this payment is waiting for approval.", "NO_REISSUE_PENDING")
    if request.get("requested_by_user_id") == principal.user_id:
        raise AppError(403, "Someone other than the requester must approve the reissue.", "DUAL_CONTROL")
    if _usable_account(db, row) is None:
        raise conflict("No usable bank account is on file for the payee.", "BANK_ACCOUNT_NOT_USABLE")
    reason = request.get("reason") or "Reissue approved"
    if note:
        reason = f"{reason} (approved: {events.reason_text(note)})"
    created = _perform_reissue(db, principal, row, reason, request.get("requested_by_user_id"))
    return {"status": "REISSUED", "instruction": events.instruction_json(row),
            "created": [events.instruction_json(r) for r in created]}


# ---------------------------------------------------------------------------
# Balances (EC-PAY-23)
# ---------------------------------------------------------------------------
LIVE_EXCLUDED = ("CANCELLED", "REISSUED")


def _empty() -> Dict[str, Decimal]:
    return {k: Decimal(0) for k in ("obligated", "intended", "submitted", "settled", "returned", "outstanding")}


def figures(obligations: Sequence[PaymentObligation], instructions: Sequence[PaymentInstruction],
            key) -> Dict[Tuple[Any, str], Dict[str, Decimal]]:
    """Totals per (key, currency): ``key(obligation)`` groups obligations, ``outstanding = obligated − settled``."""
    totals: Dict[Tuple[Any, str], Dict[str, Decimal]] = defaultdict(_empty)
    by_obligation: Dict[int, List[PaymentInstruction]] = defaultdict(list)
    for row in instructions:
        by_obligation[row.obligation_id].append(row)
    for obligation in obligations:
        bucket = totals[(key(obligation), obligation.currency)]
        bucket["obligated"] += Decimal(obligation.amount)
        rows = by_obligation.get(obligation.id, [])
        bucket["intended"] += sum((Decimal(r.amount) for r in rows if r.status not in LIVE_EXCLUDED), Decimal(0))
        part = events.obligation_figures(rows)
        for name in ("submitted", "settled", "returned"):
            bucket[name] += part[name]
    for bucket in totals.values():
        bucket["outstanding"] = bucket["obligated"] - bucket["settled"]
    return totals


def _figures_json(totals: Dict[Tuple[Any, str], Dict[str, Decimal]], label: str) -> List[dict]:
    out = []
    for (key, currency), bucket in sorted(totals.items(), key=lambda item: (str(item[0][0]), item[0][1])):
        out.append({label: key, "currency": currency,
                    **{name: events.money_json(value, currency) for name, value in bucket.items()}})
    return out


def balances(db: Session, principal: Principal, *, group_by: str = "employee", employee_id: Optional[int] = None,
             run_id: Optional[int] = None, legal_entity_id: Optional[int] = None) -> dict:
    """Paid to date and outstanding per employee, run or legal entity (EC-PAY-23); scoped like obligations."""
    if group_by not in ("employee", "run", "legal_entity"):
        raise bad_request("group_by is employee, run or legal_entity.", "INVALID_GROUP")
    events.require_view(principal)
    query = db.query(PaymentObligation).filter(PaymentObligation.company_id == principal.company_id,
                                               events.scope_clause(principal, PaymentObligation.legal_entity_id))
    if employee_id is not None:
        query = query.filter(PaymentObligation.employee_id == employee_id)
    if run_id is not None:
        query = query.filter(PaymentObligation.run_id == run_id)
    if legal_entity_id is not None:
        query = query.filter(PaymentObligation.legal_entity_id == legal_entity_id)
    obligations = query.all()
    ids = [o.id for o in obligations]
    instructions = db.query(PaymentInstruction).filter(
        PaymentInstruction.company_id == principal.company_id, PaymentInstruction.obligation_id.in_(ids)).all() if ids else []
    attr = {"employee": "employee_id", "run": "run_id", "legal_entity": "legal_entity_id"}[group_by]
    totals = figures(obligations, instructions, lambda o: getattr(o, attr))
    return {"group_by": group_by, "items": _figures_json(totals, f"{group_by}_id")}


# ---------------------------------------------------------------------------
# Listing and self-service
# ---------------------------------------------------------------------------
def obligation_json(obligation: PaymentObligation, rows: Sequence[PaymentInstruction], *, for_self: bool = False
                    ) -> dict:
    totals = figures([obligation], rows, lambda o: o.id)[(obligation.id, obligation.currency)]
    data = {
        "id": obligation.id, "run_id": obligation.run_id, "employee_id": obligation.employee_id,
        "amount": events.money_json(obligation.amount, obligation.currency), "status": obligation.status,
        "pay_date": obligation.pay_date,
        "paid_to_date": events.money_json(totals["settled"], obligation.currency),
        "returned": events.money_json(totals["returned"], obligation.currency),
        "outstanding": events.money_json(totals["outstanding"], obligation.currency),
        "instructions": [events.instruction_json(r, for_self=for_self) for r in rows],
    }
    if not for_self:
        data.update({"run_employee_id": obligation.run_employee_id, "legal_entity_id": obligation.legal_entity_id,
                     "source_type": obligation.source_type, "carried_at": obligation.carried_at,
                     "carried_reason": obligation.carried_reason, "row_version": obligation.row_version})
    return data


def _instructions_of(db: Session, company_id: int, obligation_ids: Iterable[int]) -> Dict[int, List[PaymentInstruction]]:
    ids = list(obligation_ids)
    grouped: Dict[int, List[PaymentInstruction]] = defaultdict(list)
    if not ids:
        return grouped
    for row in db.query(PaymentInstruction).filter(PaymentInstruction.company_id == company_id,
                                                   PaymentInstruction.obligation_id.in_(ids)).order_by(
            PaymentInstruction.id):
        grouped[row.obligation_id].append(row)
    return grouped


def list_obligations(db: Session, principal: Principal, *, run_id: Optional[int] = None,
                     employee_id: Optional[int] = None, status: Optional[str] = None, limit: int = 200,
                     offset: int = 0) -> dict:
    """Obligations of the caller's scope (out-of-scope entities are invisible, also in the count)."""
    events.require_view(principal)
    query = db.query(PaymentObligation).filter(PaymentObligation.company_id == principal.company_id,
                                               events.scope_clause(principal, PaymentObligation.legal_entity_id))
    if run_id is not None:
        query = query.filter(PaymentObligation.run_id == run_id)
    if employee_id is not None:
        query = query.filter(PaymentObligation.employee_id == employee_id)
    if status is not None:
        query = query.filter(PaymentObligation.status == status)
    total = query.count()
    rows = query.order_by(PaymentObligation.id).offset(max(offset, 0)).limit(min(max(limit, 1), 500)).all()
    grouped = _instructions_of(db, principal.company_id, [o.id for o in rows])
    return {"total": total, "items": [obligation_json(o, grouped.get(o.id, [])) for o in rows]}


def get_obligation(db: Session, principal: Principal, obligation_id: int) -> dict:
    events.require_view(principal)
    obligation = db.query(PaymentObligation).filter(PaymentObligation.company_id == principal.company_id,
                                                    PaymentObligation.id == obligation_id).first()
    if obligation is None or not events.covers(principal, obligation.legal_entity_id):
        raise not_found("Payment obligation")
    grouped = _instructions_of(db, principal.company_id, [obligation.id])
    return obligation_json(obligation, grouped.get(obligation.id, []))


def my_payments(db: Session, principal: Principal, *, year: Optional[int] = None) -> dict:
    """My Payments (3.7 §11.1): per pay period the net owed and each instruction with its employee label,
    masked account and timestamps. Only the caller's own obligations; never another payee's."""
    if principal.employee_id is None:
        return {"items": [], "years": []}
    query = db.query(PaymentObligation).filter(PaymentObligation.company_id == principal.company_id,
                                               PaymentObligation.employee_id == principal.employee_id)
    all_rows = query.order_by(PaymentObligation.pay_date.desc().nullslast(), PaymentObligation.id.desc()).all()
    years = sorted({o.pay_date.year for o in all_rows if o.pay_date is not None}, reverse=True)
    rows = [o for o in all_rows if year is None or (o.pay_date is not None and o.pay_date.year == year)]
    grouped = _instructions_of(db, principal.company_id, [o.id for o in rows])
    run_ids = {o.run_id for o in rows if o.run_id is not None}
    runs = {r.id: r for r in db.query(PayrollRun).filter(PayrollRun.company_id == principal.company_id,
                                                          PayrollRun.id.in_(run_ids))} if run_ids else {}
    items = []
    for obligation in rows:
        instructions = grouped.get(obligation.id, [])
        timeline = []
        for row in instructions:
            history = db.query(PaymentEvent).filter(
                PaymentEvent.company_id == row.company_id, PaymentEvent.instruction_id == row.id,
                PaymentEvent.outcome.in_(("APPLIED", "RECORDED"))).order_by(PaymentEvent.id).all()
            timeline.append({**events.instruction_json(row, for_self=True), "events": [
                {"status": e.to_status, "label": events.employee_label(_label_view(row, e.to_status))["text"],
                 "at": e.at} for e in history]})
        run = runs.get(obligation.run_id)
        data = obligation_json(obligation, instructions, for_self=True)
        data["instructions"] = timeline
        data["run_label"] = run.label if run is not None else None
        items.append(data)
    return {"items": items, "years": years}


@dataclass
class _LabelView:
    status: str
    submission_channel: Optional[str]
    evidence_status: str
    settled_at: Optional[datetime]
    reconciled_at: Optional[datetime]
    settled_amount: Optional[Decimal]
    currency: str


def _label_view(row: PaymentInstruction, status: str) -> _LabelView:
    """How an earlier status of ``row`` reads to the employee (its channel and evidence as of now)."""
    channel = row.submission_channel if status in events.IN_FLIGHT or status in events.SETTLED or \
        status in events.FAILURES or status in events.RETURNS else None
    evidence = row.evidence_status if status in events.SETTLED else sm.EvidenceStatus.UNVERIFIED.value
    return _LabelView(status, channel, evidence, row.settled_at, row.reconciled_at, row.settled_amount, row.currency)


# ---------------------------------------------------------------------------
# Hooks into the runs service
# ---------------------------------------------------------------------------
def install_hooks() -> None:
    """Replace the runs service's "until the payments milestone" hook with the obligations-backed one
    (the pattern of ``runs.install_hooks``: rebinding the module attribute replaces it for every caller).
    Idempotent; runs when this module is imported (the API includes the payments routers, the worker
    imports every service module)."""
    from app.payroll.services import runs

    runs.create_payment_intent = create_payment_intent  # EC-RUN-13


install_hooks()
