"""Payment batches: proposal, dual control, bank files, release and the run's payment states (3.7 §7.1-§7.2,
§8.2-§8.5; 3.5 §5.9; decision I-17; EC-PAY-01, EC-PAY-03, EC-PAY-05, EC-PAY-13, EC-PAY-18, EC-BNK-03).

* ``transition(db, principal, batch, action, ...)`` is the ONLY writer of ``payment_batches.status``: row
  lock, optimistic ``row_version`` (409 ROW_VERSION_CONFLICT), the ``state_machines.PAYMENT_BATCH`` table
  (409 with the machine's code, BATCH_SUBMITTED for a second release, EC-PAY-13), permission and entity
  scope (403 / 404), reasons and guards, and the audit events the table names.
* Proposal (3.7 §8.3): the READY instructions of a finalized run, grouped by funding account, method and
  payout currency and split at ``payroll.payments.batch_max_items``, with an exclusions report naming
  why every other instruction is left out (missing or unverified account, hold, cooling-off, no funding
  account ...). Payroll calculation is never affected.
* Dual control (3.7 §8.4): with ``payroll.approval.payment_dual_control`` the preparer can neither
  approve nor release; releasing needs a fresh step-up and a signatory of the funding account.
* Bank files (3.7 §7.1): only OFFERED formats (GENERIC_CSV). The bytes are deterministic and never
  stored (they hold full account numbers): the SHA-256 is stored and every download regenerates the
  file from the sealed beneficiary snapshots and must reproduce the hash exactly, otherwise it fails
  loudly (409 REGENERATION_MISMATCH). Downloads are audited. A generated or downloaded file moves the
  instructions to QUEUED only (EC-PAY-01).
* Release: "Mark as submitted" by the releaser moves the batch and its instructions to SUBMITTED with
  ``submission_channel = MANUAL_UPLOAD`` and ``evidence_status = UNVERIFIED`` (employee label "Submitted
  for manual payment — unverified"). An API batch is submitted to the SANDBOX payout adapter only
  (in-process, allow-listed endpoint, idempotency key per instruction, EC-PAY-05); its instructions are
  labelled SANDBOX everywhere.
* Run states: the first approved batch moves a FINALIZED run to PAYMENT_READY, the first release to
  SUBMITTED (``runs.transition``); settlement states follow from evidence (``payment_events.sync_run``).
* EC-BNK-03: bank details changed after the snapshot are reported on the batch; finance either proceeds
  with the snapshot (recorded) or cancels the batch and recreates the instructions (new approval).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.pay_runs import PayrollRun
from app.models.payment_flows import PaymentBatch, PaymentFile, PaymentInstruction
from app.models.payments import FundingAccount
from app.payroll import state_machines as sm
from app.payroll.banking import files as bank_files
from app.payroll.services import step_up
from app.payroll.services import obligations
from app.payroll.services import payment_adapters as adapters
from app.payroll.services import payment_events as events
from app.payroll.services import settings as settings_engine

PREPARE, APPROVE, SUBMIT, RECONCILE = events.PREPARE, events.APPROVE, events.SUBMIT, events.RECONCILE
FILE_DOWNLOADED = "PAYMENT_FILE_DOWNLOADED"
BANK_CHANGE_PROCEEDED = "BANK_CHANGE_PROCEEDED"
BANK_CHANGE_RECREATED = "BANK_CHANGE_RECREATED"
SANDBOX_SUBMITTED = "SANDBOX_BATCH_SUBMITTED"
BATCHABLE_RUN_STATES = ("FINALIZED", "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED")
OFFERED_METHODS = ("BANK_FILE", "API")
EXCLUSION_MESSAGES = {
    **obligations.REASONS,
    "NO_FUNDING_ACCOUNT": "No active funding account of the legal entity pays this currency",
    "FUNDING_ACCOUNT_AMBIGUOUS": "Several funding accounts pay this currency; choose one",
    "FUNDING_CURRENCY_MISMATCH": "The chosen funding account pays another currency",
    "FUNDING_FORMAT_NOT_ENABLED": "The funding account does not allow this bank file format",
    "NO_PAYOUT_PROVIDER": "The funding account has no allow-listed payout provider",
    "ALREADY_BATCHED": "The payment is already in a batch",
}
_UPDATABLE = frozenset((
    "approved_by_user_id", "approved_at", "released_by_user_id", "released_at", "submission_channel",
    "cancelled_by_user_id", "cancelled_at", "status_reason", "details",
))
_GUARD_ERRORS = {
    "DUAL_CONTROL": (403, "DUAL_CONTROL"),
    "STEP_UP_MFA": (403, "STEP_UP_REQUIRED"),
    "FUNDING_SIGNATORY": (403, "NOT_A_SIGNATORY"),
    "FILE_HASH_UNCHANGED": (409, "FILE_HASH_CHANGED"),
    "PROVIDER_ACCEPTED": (409, "PROVIDER_NOT_ACCEPTED"),
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_batch(db: Session, company_id: int, batch_id: int, *, lock: bool = False) -> PaymentBatch:
    query = db.query(PaymentBatch).filter(PaymentBatch.company_id == company_id, PaymentBatch.id == batch_id)
    if lock:
        query = query.with_for_update(of=PaymentBatch).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Payment batch")
    return row


def get_batch_for(db: Session, principal: Principal, batch_id: int, permission: Optional[str] = None, *,
                  lock: bool = False, any_of: Sequence[str] = ()) -> PaymentBatch:
    """A batch of the caller's tenant inside the scope of ``permission`` (or of any of ``any_of``, or of any
    payment permission): 403 without the permission, 404 when unknown or outside the scope."""
    if permission is not None:
        if not principal.has(permission):
            raise forbidden()
        held = [permission]
    else:
        held = [p for p in (any_of or events.VIEW_PERMISSIONS) if principal.has(p)]
        if not held:
            raise forbidden()
    row = load_batch(db, principal.company_id, batch_id, lock=lock)
    if not events.covers(principal, row.legal_entity_id, held):
        raise not_found("Payment batch")
    return row


def batch_instructions(db: Session, batch: PaymentBatch) -> List[PaymentInstruction]:
    return db.query(PaymentInstruction).filter(
        PaymentInstruction.company_id == batch.company_id, PaymentInstruction.batch_id == batch.id,
    ).order_by(PaymentInstruction.id).all()


# ---------------------------------------------------------------------------
# The batch transition (the only writer of payment_batches.status)
# ---------------------------------------------------------------------------
def _guard_failed(guard: str) -> AppError:
    status, code = _GUARD_ERRORS.get(guard, (409, f"GUARD_{guard}"))
    return AppError(status, sm.GUARDS.get(guard, guard), code)


def transition(db: Session, principal: Optional[Principal], batch: PaymentBatch | int, action: str, *,
               company_id: Optional[int] = None, reason: Optional[str] = None,
               expected_version: Optional[int] = None, system: bool = False,
               updates: Optional[Mapping[str, Any]] = None, details: Optional[Mapping[str, Any]] = None,
               **guards: bool) -> PaymentBatch:
    """Move a batch by ``action`` of ``state_machines.PAYMENT_BATCH`` (locks the row, audits)."""
    unknown = [g for g in guards if g not in sm.GUARDS]
    if unknown:
        raise ValueError(f"unknown batch guards {unknown}")
    bad_fields = set(updates or {}) - _UPDATABLE
    if bad_fields:
        raise ValueError(f"fields a batch transition may not set: {sorted(bad_fields)}")
    batch_id = batch if isinstance(batch, int) else batch.id
    cid = company_id or (principal.company_id if principal is not None else None) or (
        batch.company_id if isinstance(batch, PaymentBatch) else None)
    if cid is None:
        raise ValueError("a system transition of a batch id needs the company")
    row = load_batch(db, cid, batch_id, lock=True)
    try:
        spec = sm.transition_spec(sm.PAYMENT_BATCH, row.status, action)
    except sm.InvalidTransition as exc:
        message = exc.message
        if exc.code == "BATCH_SUBMITTED":
            message = "This batch was already submitted; it cannot be submitted again (EC-PAY-13)."
        raise AppError(409, message, exc.code)
    if spec.permission is None:
        if not system:
            raise forbidden("This step is performed by the system.")
    elif not system:
        if principal is None:
            raise forbidden()
        events.require_permission_in_scope(principal, spec.permission, row.legal_entity_id, "Payment batch")
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The payment batch was changed by someone else. Reload and try again.",
                       "ROW_VERSION_CONFLICT")
    if spec.reason_required:
        reason = events.reason_text(reason)
    for guard in spec.guards:
        if guard not in guards:
            raise conflict(f"The guard {guard} was not checked for {action}.", "GUARD_NOT_CHECKED")
        if not guards[guard]:
            raise _guard_failed(guard)
    previous = row.status
    for name, value in (updates or {}).items():
        setattr(row, name, value)
    row.status = spec.target
    if spec.target == "CANCELLED":
        row.cancelled_at = row.cancelled_at or events.now()
        row.cancelled_by_user_id = principal.user_id if principal is not None else None
        row.status_reason = reason
    row.row_version += 1
    db.flush()
    event = spec.audit_event or f"BATCH_{action}"
    events.audit(db, principal, event, f"Payment batch {row.reference}: {previous} -> {spec.target}"
                 + (f": {reason}" if reason else ""), "payment_batch", row.id, row.company_id, action=action,
                 from_status=previous, to_status=spec.target, reason=reason, row_version=row.row_version,
                 **dict(details or {}))
    return row


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _resolve(db: Session, company_id: int, key: str, legal_entity_id: int, on: date, *, strict: bool = True) -> Any:
    try:
        return settings_engine.resolve(db, company_id, key, on=on, legal_entity_id=legal_entity_id).value
    except settings_engine.SettingConfigError as exc:
        if not strict:
            return None
        raise conflict(exc.message, exc.code)


def dual_control_on(db: Session, company_id: int, legal_entity_id: int) -> bool:
    return bool(_resolve(db, company_id, "payroll.approval.payment_dual_control", legal_entity_id, events.now().date()))


def _dual_control_ok(db: Session, principal: Principal, batch: PaymentBatch) -> bool:
    """DUAL_CONTROL: the preparer neither approves nor releases (when the setting is ON)."""
    return principal.user_id != batch.prepared_by_user_id or not dual_control_on(
        db, batch.company_id, batch.legal_entity_id)


# ---------------------------------------------------------------------------
# Proposal (3.7 §8.3, EC-PAY-03)
# ---------------------------------------------------------------------------
@dataclass
class Group:
    funding_account_id: int
    method: str
    currency: str
    file_format: Optional[str]
    adapter: Optional[str]
    rows: List[PaymentInstruction] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((events.payout_amount(r) for r in self.rows), Decimal(0))


def _run_for(db: Session, principal: Principal, run_id: int, *, lock: bool = False) -> PayrollRun:
    from app.payroll.services import runs

    run = runs.get_run_for(db, principal, run_id, PREPARE, lock=lock)
    if run.status not in BATCHABLE_RUN_STATES:
        raise conflict(f"Payments are prepared from a finalized run; this run is {run.status}.", "RUN_NOT_FINALIZED")
    return run


def _funding_accounts(db: Session, company_id: int, legal_entity_id: int) -> List[FundingAccount]:
    return db.query(FundingAccount).filter(
        FundingAccount.company_id == company_id, FundingAccount.legal_entity_id == legal_entity_id,
        FundingAccount.status == "ACTIVE").order_by(FundingAccount.id).all()


def _chosen_funding(db: Session, principal: Principal, run: PayrollRun, funding_account_id: Optional[int]
                    ) -> Optional[FundingAccount]:
    """The funding account named by the caller, or by payroll.payments.funding_account_id; a foreign or
    another entity's account is unknown (404)."""
    if funding_account_id is None:
        configured = _resolve(db, run.company_id, "payroll.payments.funding_account_id", run.legal_entity_id,
                              events.now().date(), strict=False)
        if not isinstance(configured, int) or isinstance(configured, bool):
            return None
        funding_account_id = configured
    row = db.query(FundingAccount).filter(FundingAccount.company_id == principal.company_id,
                                          FundingAccount.id == funding_account_id).first()
    if row is None or row.legal_entity_id != run.legal_entity_id:
        raise not_found("Funding account")
    if row.status != "ACTIVE":
        raise conflict("The funding account is inactive.", "FUNDING_ACCOUNT_INACTIVE")
    return row


def _method_details(method: str, account: FundingAccount, file_format: str) -> Tuple[Optional[str], Optional[str],
                                                                                       Optional[str]]:
    """(file_format, adapter, exclusion reason) of paying from ``account`` by ``method``."""
    if method == "BANK_FILE":
        if account.formats and file_format not in account.formats:
            return None, None, "FUNDING_FORMAT_NOT_ENABLED"
        return file_format, None, None
    try:
        adapter, _ = adapters.resolve_endpoint(account.provider_profile)
    except adapters.AdapterError:
        return None, None, "NO_PAYOUT_PROVIDER"
    return None, adapter, None


def _exclusion(row: PaymentInstruction, reason: str) -> dict:
    return {"instruction_id": row.id, "reference": row.reference, "employee_id": row.employee_id,
            "status": row.status, "reason": reason, "message": EXCLUSION_MESSAGES.get(reason, reason),
            "amount": events.money_json(row.amount, row.currency),
            "account": (row.beneficiary_snapshot or {}).get("account_masked")}


def _plan(db: Session, principal: Principal, run: PayrollRun, *, funding_account_id: Optional[int],
          method: Optional[str]) -> Tuple[List[Group], List[dict], List[PaymentInstruction]]:
    on = events.now().date()
    method = method or _resolve(db, run.company_id, "payroll.payments.default_method", run.legal_entity_id, on)
    if method not in OFFERED_METHODS:
        raise conflict("Manual payment batches are not offered; record a payment made outside the system with "
                       "manual proof on a submitted instruction.", "METHOD_NOT_OFFERED")
    file_format = None
    if method == "BANK_FILE":
        file_format = _resolve(db, run.company_id, "payroll.payments.bank_file_format", run.legal_entity_id, on)
        try:
            adapters.file_adapter(file_format)
        except adapters.AdapterError as exc:
            raise conflict(exc.message, exc.code)
    chosen = _chosen_funding(db, principal, run, funding_account_id)
    accounts = _funding_accounts(db, run.company_id, run.legal_entity_id)
    rows = db.query(PaymentInstruction).filter(
        PaymentInstruction.company_id == run.company_id, PaymentInstruction.run_id == run.id,
        PaymentInstruction.status.in_(("NOT_READY", "READY", "ON_HOLD")),
    ).order_by(PaymentInstruction.id).all()
    exclusions: List[dict] = []
    groups: Dict[Tuple[int, str], Group] = {}
    candidates: List[PaymentInstruction] = []
    for row in rows:
        if row.batch_id is not None:
            exclusions.append(_exclusion(row, "ALREADY_BATCHED"))
            continue
        if row.status != "READY":
            exclusions.append(_exclusion(row, row.status_reason or "MISSING_BANK_ACCOUNT"))
            continue
        currency = events.payout_currency(row)
        if chosen is not None:
            if chosen.currency != currency:
                exclusions.append(_exclusion(row, "FUNDING_CURRENCY_MISMATCH"))
                continue
            account = chosen
        else:
            matching = [a for a in accounts if a.currency == currency and a.purpose == "PAYROLL"] or \
                [a for a in accounts if a.currency == currency]
            if not matching:
                exclusions.append(_exclusion(row, "NO_FUNDING_ACCOUNT"))
                continue
            if len(matching) > 1:
                exclusions.append(_exclusion(row, "FUNDING_ACCOUNT_AMBIGUOUS"))
                continue
            account = matching[0]
        fmt, adapter, problem = _method_details(method, account, file_format)
        if problem:
            exclusions.append(_exclusion(row, problem))
            continue
        group = groups.setdefault((account.id, currency), Group(account.id, method, currency, fmt, adapter))
        group.rows.append(row)
        candidates.append(row)
    size = max(int(_resolve(db, run.company_id, "payroll.payments.batch_max_items", run.legal_entity_id, on)), 1)
    split: List[Group] = []
    for key in sorted(groups):
        group = groups[key]
        for start in range(0, len(group.rows), size):
            split.append(Group(group.funding_account_id, group.method, group.currency, group.file_format,
                               group.adapter, group.rows[start:start + size]))
    return split, exclusions, candidates


def _group_json(group: Group) -> dict:
    return {"funding_account_id": group.funding_account_id, "method": group.method, "currency": group.currency,
            "file_format": group.file_format, "adapter": group.adapter, "sandbox": group.adapter == adapters.SANDBOX,
            "item_count": len(group.rows), "total": events.money_json(group.total, group.currency),
            "instruction_ids": [r.id for r in group.rows]}


def propose(db: Session, principal: Principal, run_id: int, *, funding_account_id: Optional[int] = None,
            method: Optional[str] = None) -> dict:
    """The batches a run's READY payments would form, and why every other payment is excluded (EC-PAY-03).
    Re-checks readiness first (system holds lift, new accounts replace placeholders)."""
    run = _run_for(db, principal, run_id)
    obligations.refresh_readiness(db, principal, run.id)
    groups, exclusions, candidates = _plan(db, principal, run, funding_account_id=funding_account_id, method=method)
    return {"run_id": run.id, "batches": [_group_json(g) for g in groups], "exclusions": exclusions,
            "bank_changes": obligations.bank_changes(db, candidates)}


def _reference(db: Session) -> Tuple[int, str]:
    batch_id = int(db.execute(text("SELECT nextval('payment_batches_id_seq')")).scalar())
    return batch_id, f"PB{batch_id:08d}"


def create_batches(db: Session, principal: Principal, run_id: int, *, funding_account_id: Optional[int] = None,
                   method: Optional[str] = None, value_date: Optional[date] = None) -> dict:
    """Create the proposed batches and submit them for approval (instructions READY -> APPROVAL_PENDING)."""
    from app.payroll.services import runs

    run = _run_for(db, principal, run_id, lock=True)
    obligations.refresh_readiness(db, principal, run.id)
    groups, exclusions, candidates = _plan(db, principal, run, funding_account_id=funding_account_id, method=method)
    if not groups:
        raise AppError(409, "No payment of the run is ready to batch; see the exclusions report.",
                       "NOTHING_TO_BATCH", headers=None)
    period = runs.period_of(db, run)
    when = value_date or period.pay_date
    created = []
    for group in groups:
        batch_id, reference = _reference(db)
        batch = PaymentBatch(
            id=batch_id, company_id=run.company_id, reference=reference, run_id=run.id,
            legal_entity_id=run.legal_entity_id, funding_account_id=group.funding_account_id, method=group.method,
            file_format=group.file_format, adapter=group.adapter, currency=group.currency, status="DRAFT",
            item_count=len(group.rows), total_amount=group.total,
            totals={group.currency: str(group.total)}, value_date=when, prepared_by_user_id=principal.user_id,
            details={"exclusions": exclusions, "sandbox": group.adapter == adapters.SANDBOX}, row_version=1,
        )
        db.add(batch)
        db.flush()
        for row in group.rows:
            events.transition(db, principal, row, "SUBMIT_FOR_APPROVAL",
                              updates={"batch_id": batch.id, "funding_account_id": group.funding_account_id},
                              details={"batch_id": batch.id})
        batch = transition(db, principal, batch, "SUBMIT_FOR_APPROVAL",
                           details={"item_count": batch.item_count, "currency": batch.currency,
                                    "total": str(group.total), "method": batch.method})
        created.append(batch)
    return {"run_id": run.id, "batches": [batch_json(db, b) for b in created], "exclusions": exclusions,
            "bank_changes": obligations.bank_changes(db, candidates)}


# ---------------------------------------------------------------------------
# Approval, rejection, cancellation
# ---------------------------------------------------------------------------
def _release_instructions(db: Session, batch: PaymentBatch, reason: str) -> List[PaymentInstruction]:
    """Unsent instructions leave a rejected or cancelled batch and are READY again for another one."""
    released = []
    for row in batch_instructions(db, batch):
        if row.status in ("APPROVAL_PENDING", "APPROVED", "QUEUED"):
            released.append(events.transition(db, None, row, "RELEASE_FROM_BATCH", system=True, reason=reason,
                                              updates={"batch_id": None, "funding_account_id": None},
                                              details={"batch_id": batch.id}))
    return released


def approve(db: Session, principal: Principal, batch_id: int, *, expected_version: Optional[int] = None) -> dict:
    """Approve a batch prepared by someone else (DUAL_CONTROL); a FINALIZED run becomes PAYMENT_READY."""
    from app.payroll.services import runs

    batch = get_batch_for(db, principal, batch_id, APPROVE, lock=True)
    ok = _dual_control_ok(db, principal, batch)
    rows = batch_instructions(db, batch)
    warnings = obligations.bank_changes(db, rows)
    batch = transition(db, principal, batch, "APPROVE", expected_version=expected_version, DUAL_CONTROL=ok,
                       updates={"approved_by_user_id": principal.user_id, "approved_at": events.now()},
                       details={"bank_changes": len(warnings)})
    for row in rows:
        if row.status == "APPROVAL_PENDING":
            events.transition(db, principal, row, "APPROVE", DUAL_CONTROL=ok, details={"batch_id": batch.id})
    if batch.run_id is not None:
        run = runs.load_run(db, batch.company_id, batch.run_id)
        if run.status == "FINALIZED":
            runs.transition(db, principal, run, "APPROVE_PAYMENTS", PAYMENT_BATCH_APPROVED=True,
                            details={"batch_id": batch.id})
    return batch_json(db, batch)


def reject(db: Session, principal: Principal, batch_id: int, reason: str, *,
           expected_version: Optional[int] = None) -> dict:
    batch = get_batch_for(db, principal, batch_id, APPROVE, lock=True)
    batch = transition(db, principal, batch, "REJECT", reason=reason, expected_version=expected_version)
    _release_instructions(db, batch, batch.status_reason or "Batch rejected")
    return batch_json(db, batch)


def cancel(db: Session, principal: Principal, batch_id: int, reason: str, *,
           expected_version: Optional[int] = None) -> dict:
    """Cancel a batch before submission (3.7 §8.2); its payments are READY for another batch."""
    batch = get_batch_for(db, principal, batch_id, PREPARE, lock=True)
    file = _file_of(db, batch)
    batch = transition(db, principal, batch, "CANCEL", reason=reason, expected_version=expected_version,
                       details={"file_downloaded": bool(file and file.download_count)})
    _release_instructions(db, batch, batch.status_reason or "Batch cancelled")
    return batch_json(db, batch)


# ---------------------------------------------------------------------------
# Bank files (3.7 §7.1)
# ---------------------------------------------------------------------------
def _file_of(db: Session, batch: PaymentBatch) -> Optional[PaymentFile]:
    return db.query(PaymentFile).filter(PaymentFile.company_id == batch.company_id,
                                        PaymentFile.batch_id == batch.id).first()


def _records(db: Session, rows: Sequence[PaymentInstruction]) -> List[bank_files.PaymentRecord]:
    records = []
    for row in rows:
        clear = obligations.open_snapshot(db, row)
        records.append(bank_files.PaymentRecord(
            instruction_id=row.id, reference=row.reference, payee_name=clear.get("holder_name") or "",
            amount=events.payout_amount(row), currency=events.payout_currency(row),
            account_number=clear.get("account_number"), iban=clear.get("iban"),
            routing_scheme=clear.get("routing_scheme") if clear.get("routing_code") else None,
            routing_code=clear.get("routing_code") if clear.get("routing_scheme") else None, bic=clear.get("bic")))
    return records


def render_file(db: Session, batch: PaymentBatch, *, expected_sha256: Optional[str] = None) -> bank_files.BankFile:
    """The batch's file from the sealed snapshots of its instructions (never logged, never stored)."""
    rows = batch_instructions(db, batch)
    try:
        return adapters.build_file(batch.file_format, batch.reference, _records(db, rows), value_date=batch.value_date,
                                   expected_sha256=expected_sha256)
    except adapters.AdapterError as exc:
        raise conflict(exc.message, exc.code)


def generate_file(db: Session, principal: Principal, batch_id: int, *, expected_version: Optional[int] = None) -> dict:
    """Generate the approved batch's file: SHA-256 stored, instructions QUEUED (never further, EC-PAY-01)."""
    batch = get_batch_for(db, principal, batch_id, PREPARE, lock=True)
    if batch.method != "BANK_FILE":
        raise conflict("Only bank-file batches have a file.", "NOT_A_FILE_BATCH")
    if batch.status != "APPROVED":
        transition(db, principal, batch, "GENERATE_FILE", expected_version=expected_version)  # refuses: 409
    file = render_file(db, batch)
    batch = transition(db, principal, batch, "GENERATE_FILE", expected_version=expected_version,
                       details={"sha256": file.sha256, "record_count": file.record_count})
    db.add(PaymentFile(
        company_id=batch.company_id, batch_id=batch.id, format=file.format, filename=file.filename,
        media_type=file.media_type, sha256=file.sha256, record_count=file.record_count,
        totals={code: str(value) for code, value in file.totals.items()}, generated_by_user_id=principal.user_id,
        generated_at=events.now(), download_count=0))
    db.flush()
    for row in batch_instructions(db, batch):
        if row.status == "APPROVED":
            events.transition(db, None, row, "QUEUE", system=True, details={"batch_id": batch.id,
                                                                           "file_sha256": file.sha256})
    return batch_json(db, batch)


def download_file(db: Session, principal: Principal, batch_id: int) -> Tuple[str, str, bytes, str]:
    """(filename, media type, bytes, sha256): regenerated and checked against the stored hash (fails loudly);
    every download is audited. A download changes no payment status (EC-PAY-01)."""
    batch = get_batch_for(db, principal, batch_id, any_of=(PREPARE, SUBMIT))
    file = _file_of(db, batch)
    if file is None:
        raise not_found("Payment file")
    regenerated = render_file(db, batch, expected_sha256=file.sha256)
    file.download_count += 1
    file.last_downloaded_at = events.now()
    db.flush()
    events.audit(db, principal, FILE_DOWNLOADED, f"Payment file of batch {batch.reference} downloaded",
                 "payment_batch", batch.id, batch.company_id, sha256=file.sha256, record_count=file.record_count,
                 download_count=file.download_count)
    return file.filename, file.media_type, regenerated.content, file.sha256


# ---------------------------------------------------------------------------
# Release (3.7 §8.4): mark as submitted, or submit to the SANDBOX payout adapter
# ---------------------------------------------------------------------------
def _release_guards(db: Session, principal: Principal, batch: PaymentBatch) -> Dict[str, bool]:
    if not principal.has(SUBMIT):
        raise forbidden()
    step_up.ensure_step_up(db, principal)  # 403 STEP_UP_REQUIRED
    account = db.query(FundingAccount).filter(FundingAccount.company_id == batch.company_id,
                                              FundingAccount.id == batch.funding_account_id).first()
    signatory = account is not None and account.status == "ACTIVE" and principal.user_id in (
        account.signatory_user_ids or [])
    return {"DUAL_CONTROL": _dual_control_ok(db, principal, batch), "STEP_UP_MFA": True,
            "FUNDING_SIGNATORY": signatory}


def _run_submitted(db: Session, principal: Principal, batch: PaymentBatch, dual_ok: bool) -> None:
    from app.payroll.services import runs

    if batch.run_id is None:
        return
    run = runs.load_run(db, batch.company_id, batch.run_id)
    if run.status == "PAYMENT_READY":
        runs.transition(db, principal, run, "SUBMIT_PAYMENTS", DUAL_CONTROL=dual_ok, details={"batch_id": batch.id})


def release(db: Session, principal: Principal, batch_id: int, *, expected_version: Optional[int] = None,
            file_sha256: Optional[str] = None) -> dict:
    """Release an approved batch: a bank file is "marked as submitted" (MANUAL_UPLOAD, evidence UNVERIFIED);
    an API batch goes to the SANDBOX adapter. Needs step-up, a funding signatory and dual control; a
    submitted batch is never submitted again (EC-PAY-13)."""
    batch = get_batch_for(db, principal, batch_id, SUBMIT, lock=True)
    guards = _release_guards(db, principal, batch)
    if batch.method == "API":
        return _release_api(db, principal, batch, guards, expected_version)
    file = _file_of(db, batch)
    unchanged = False
    if batch.status == "FILE_GENERATED" and file is not None:
        try:
            render_file(db, batch, expected_sha256=file.sha256)
            unchanged = file_sha256 is None or file_sha256.strip().lower() == file.sha256
        except AppError:
            unchanged = False
    now = events.now()
    batch = transition(db, principal, batch, "MARK_SUBMITTED", expected_version=expected_version,
                       FILE_HASH_UNCHANGED=unchanged, **guards,
                       updates={"released_by_user_id": principal.user_id, "released_at": now,
                                "submission_channel": sm.SubmissionChannel.MANUAL_UPLOAD.value},
                       details={"sha256": file.sha256 if file else None})
    expected = batch.value_date or now.date()
    for row in batch_instructions(db, batch):
        if row.status == "QUEUED":
            events.transition(db, principal, row, "SUBMIT", FILE_HASH_UNCHANGED=True, **guards,
                              source="FILE", evidence_ref=f"file:{file.sha256}" if file else None,
                              updates={"submission_channel": sm.SubmissionChannel.MANUAL_UPLOAD.value,
                                       "submitted_at": now, "expected_settlement_date": expected},
                              details={"batch_id": batch.id})
    _run_submitted(db, principal, batch, guards["DUAL_CONTROL"])
    return batch_json(db, batch)


def _release_api(db: Session, principal: Principal, batch: PaymentBatch, guards: Dict[str, bool],
                 expected_version: Optional[int]) -> dict:
    account = db.query(FundingAccount).filter(FundingAccount.company_id == batch.company_id,
                                              FundingAccount.id == batch.funding_account_id).first()
    try:
        adapter, endpoint = adapters.resolve_endpoint(account.provider_profile if account else None)
    except adapters.AdapterError as exc:
        raise AppError(exc.status, exc.message, exc.code)
    if adapter != batch.adapter:
        raise conflict("The funding account's payout provider changed; recreate the batch.", "ADAPTER_CHANGED")
    channel = sm.SubmissionChannel.SANDBOX.value
    now = events.now()
    batch = transition(db, principal, batch, "QUEUE_API", expected_version=expected_version, **guards,
                       updates={"released_by_user_id": principal.user_id, "released_at": now,
                                "submission_channel": channel},
                       details={"adapter": adapter, "sandbox": True})
    queued = []
    for row in batch_instructions(db, batch):
        if row.status == "APPROVED":
            queued.append(events.transition(db, None, row, "QUEUE", system=True, details={"batch_id": batch.id}))
    items = [adapters.SubmissionItem(r.id, r.idempotency_key, events.payout_amount(r), events.payout_currency(r))
             for r in queued]
    results = {r.instruction_id: r for r in adapters.sandbox_submit(endpoint, items)}
    expected = batch.value_date or now.date()
    accepted = 0
    for row in queued:
        result = results[row.id]
        if not result.accepted:
            row.status_note = f"SANDBOX provider refused the request: {result.reason}"
            row.row_version += 1
            db.flush()
            continue
        events.transition(db, None, row, sm.event_action("SUBMITTED"), system=True, source="SYSTEM",
                          event_status="SUBMITTED", sandbox=True, PROVIDER_ACCEPTED=True,
                          updates={"provider_ref": result.provider_ref, "submission_channel": channel,
                                   "submitted_at": now, "expected_settlement_date": expected},
                          details={"batch_id": batch.id, "adapter": adapter, "sandbox": True})
        accepted += 1
    if accepted == len(queued) and queued:
        batch = transition(db, None, batch, "RECORD_SUBMITTED", system=True, PROVIDER_ACCEPTED=True,
                           details={"adapter": adapter, "sandbox": True, "accepted": accepted})
        _run_submitted(db, principal, batch, guards["DUAL_CONTROL"])
    events.audit(db, principal, SANDBOX_SUBMITTED, f"Batch {batch.reference} submitted to the SANDBOX payout "
                 "adapter (no money moved)", "payment_batch", batch.id, batch.company_id, accepted=accepted,
                 total=len(queued), sandbox=True)
    return batch_json(db, batch)


# ---------------------------------------------------------------------------
# Bank details changed after the snapshot (EC-BNK-03)
# ---------------------------------------------------------------------------
UNSENT_BATCH_STATES = ("APPROVAL_PENDING", "APPROVED", "FILE_GENERATED", "API_QUEUED")


def resolve_bank_changes(db: Session, principal: Principal, batch_id: int, decision: str, reason: str, *,
                         expected_version: Optional[int] = None) -> dict:
    """PROCEED keeps the snapshot (recorded); RECREATE cancels the batch and recreates the affected
    instructions from the current bank details, which then need a new batch and approval."""
    batch = get_batch_for(db, principal, batch_id, APPROVE, lock=True)
    if expected_version is not None and batch.row_version != expected_version:
        raise conflict("The payment batch was changed by someone else.", "ROW_VERSION_CONFLICT")
    text_reason = events.reason_text(reason)
    if batch.status not in UNSENT_BATCH_STATES:
        raise conflict("Only a batch that was not submitted can be changed.", "BATCH_SUBMITTED")
    rows = batch_instructions(db, batch)
    warnings = obligations.bank_changes(db, rows)
    if not warnings:
        raise conflict("No bank details changed after the snapshot.", "NO_BANK_CHANGES")
    affected = [w["instruction_id"] for w in warnings]
    if decision == "PROCEED":
        details = dict(batch.details or {})
        details["bank_change_decisions"] = list(details.get("bank_change_decisions") or []) + [
            {"decision": "PROCEED", "instruction_ids": affected, "by_user_id": principal.user_id,
             "at": events.now().isoformat(), "reason": text_reason}]
        batch.details = details
        batch.row_version += 1
        db.flush()
        events.audit(db, principal, BANK_CHANGE_PROCEEDED, f"Batch {batch.reference}: proceeding with the bank "
                     f"snapshot for {len(affected)} payment(s)", "payment_batch", batch.id, batch.company_id,
                     instruction_ids=affected, reason=text_reason)
        return {"decision": "PROCEED", "batch": batch_json(db, batch), "created": []}
    if decision != "RECREATE":
        raise bad_request("The decision is PROCEED or RECREATE.", "INVALID_DECISION")
    if not principal.has(PREPARE):
        raise forbidden()
    batch = transition(db, principal, batch, "CANCEL", reason=text_reason, system=False,
                       details={"bank_changes": affected})
    released = {r.id: r for r in _release_instructions(db, batch, text_reason)}
    created = obligations.recreate_instructions(db, principal, [released[i] for i in affected if i in released],
                                                text_reason)
    events.audit(db, principal, BANK_CHANGE_RECREATED, f"Batch {batch.reference} cancelled; {len(created)} "
                 "payment(s) recreated from the current bank details", "payment_batch", batch.id, batch.company_id,
                 cancelled=affected, created=[r.id for r in created], reason=text_reason)
    return {"decision": "RECREATE", "batch": batch_json(db, batch),
            "created": [events.instruction_json(r) for r in created]}


# ---------------------------------------------------------------------------
# Serialization and listing
# ---------------------------------------------------------------------------
def batch_figures(rows: Iterable[PaymentInstruction], currency: str) -> dict:
    """Intended vs submitted vs settled vs returned vs outstanding of a batch (3.7 §9, EC-PAY-23)."""
    rows = list(rows)
    live = [r for r in rows if r.status not in obligations.LIVE_EXCLUDED]
    part = events.obligation_figures(rows)
    intended = sum((Decimal(r.amount) for r in live), Decimal(0))
    figures = {"intended": intended, "submitted": part["submitted"], "settled": part["settled"],
               "returned": part["returned"], "failed": part["failed"], "outstanding": intended - part["settled"]}
    return {name: events.money_json(value, currency) for name, value in figures.items()}


def batch_json(db: Session, batch: PaymentBatch, *, with_instructions: bool = True) -> dict:
    rows = batch_instructions(db, batch)
    file = _file_of(db, batch)
    pay_currency = rows[0].currency if rows else batch.currency
    data = {
        "id": batch.id, "reference": batch.reference, "run_id": batch.run_id, "legal_entity_id": batch.legal_entity_id,
        "funding_account_id": batch.funding_account_id, "method": batch.method, "file_format": batch.file_format,
        "adapter": batch.adapter, "sandbox": batch.adapter == adapters.SANDBOX,
        "badge": "SANDBOX" if batch.adapter == adapters.SANDBOX else None, "currency": batch.currency,
        "status": batch.status, "item_count": batch.item_count,
        "total": events.money_json(batch.total_amount, batch.currency), "value_date": batch.value_date,
        "prepared_by_user_id": batch.prepared_by_user_id, "approved_by_user_id": batch.approved_by_user_id,
        "approved_at": batch.approved_at, "released_by_user_id": batch.released_by_user_id,
        "released_at": batch.released_at, "submission_channel": batch.submission_channel,
        "cancelled_at": batch.cancelled_at, "status_reason": batch.status_reason,
        "exclusions": list((batch.details or {}).get("exclusions") or []),
        "bank_change_decisions": list((batch.details or {}).get("bank_change_decisions") or []),
        "figures": batch_figures(rows, pay_currency),
        "file": None if file is None else {
            "format": file.format, "filename": file.filename, "sha256": file.sha256,
            "record_count": file.record_count, "totals": file.totals, "generated_at": file.generated_at,
            "download_count": file.download_count, "last_downloaded_at": file.last_downloaded_at},
        "row_version": batch.row_version, "created_at": batch.created_at,
    }
    if with_instructions:
        data["instructions"] = [events.instruction_json(r) for r in rows]
        data["bank_changes"] = obligations.bank_changes(db, [r for r in rows if r.status in sm.INSTRUCTION_RANK
                                                             and sm.INSTRUCTION_RANK[r.status] <= sm.INSTRUCTION_RANK[
                                                                 "QUEUED"]])
    return data


def get_batch(db: Session, principal: Principal, batch_id: int) -> dict:
    return batch_json(db, get_batch_for(db, principal, batch_id))


def list_batches(db: Session, principal: Principal, *, run_id: Optional[int] = None, status: Optional[str] = None,
                 limit: int = 100, offset: int = 0) -> dict:
    """Batches of the caller's scope (out-of-scope entities are invisible, also in the count)."""
    events.require_view(principal)
    query = db.query(PaymentBatch).filter(PaymentBatch.company_id == principal.company_id,
                                          events.scope_clause(principal, PaymentBatch.legal_entity_id))
    if run_id is not None:
        query = query.filter(PaymentBatch.run_id == run_id)
    if status is not None:
        query = query.filter(PaymentBatch.status == status)
    total = query.count()
    rows = query.order_by(PaymentBatch.id.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 200)).all()
    return {"total": total, "items": [batch_json(db, b, with_instructions=False) for b in rows]}


def batch_balances(db: Session, principal: Principal, *, run_id: Optional[int] = None) -> dict:
    """Intended / submitted / settled / returned / outstanding per batch (EC-PAY-23)."""
    events.require_view(principal)
    query = db.query(PaymentBatch).filter(PaymentBatch.company_id == principal.company_id,
                                          events.scope_clause(principal, PaymentBatch.legal_entity_id),
                                          PaymentBatch.status != "CANCELLED")
    if run_id is not None:
        query = query.filter(PaymentBatch.run_id == run_id)
    items = []
    for batch in query.order_by(PaymentBatch.id):
        rows = batch_instructions(db, batch)
        items.append({"batch_id": batch.id, "reference": batch.reference, "currency": batch.currency,
                      **batch_figures(rows, rows[0].currency if rows else batch.currency)})
    return {"group_by": "batch", "items": items}


def list_instructions(db: Session, principal: Principal, *, run_id: Optional[int] = None,
                      batch_id: Optional[int] = None, employee_id: Optional[int] = None,
                      status: Optional[str] = None, limit: int = 200, offset: int = 0) -> dict:
    events.require_view(principal)
    query = db.query(PaymentInstruction).filter(
        PaymentInstruction.company_id == principal.company_id,
        events.scope_clause(principal, PaymentInstruction.legal_entity_id))
    for column, value in ((PaymentInstruction.run_id, run_id), (PaymentInstruction.batch_id, batch_id),
                          (PaymentInstruction.employee_id, employee_id), (PaymentInstruction.status, status)):
        if value is not None:
            query = query.filter(column == value)
    total = query.count()
    rows = query.order_by(PaymentInstruction.id).offset(max(offset, 0)).limit(min(max(limit, 1), 500)).all()
    return {"total": total, "items": [events.instruction_json(r) for r in rows]}

