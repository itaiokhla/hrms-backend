"""Bank statement import, reconciliation queues, webhooks and run reconciliation (3.7 §9, §10; 3.5 §5.10;
decision I-17; F-09, F-10, F-11; EC-PAY-04, EC-PAY-06, EC-PAY-07, EC-PAY-08, EC-PAY-11, EC-PAY-12, EC-MON-18).

* Statement import (CSV with a per-bank column mapping, ISO 20022 CAMT.053) is idempotent: the same file
  for the same funding account imports once (``file_sha256``) and a line already seen in an earlier
  import of the account is never stored again (``line_hash``, F-11). A statement of another account is
  refused. Counterparty accounts are stored masked only; free text is scrubbed of account-like numbers.
* New lines are matched with ``banking.matching`` against the instructions paid from the funding account
  (reference first, then exact amount + beneficiary account + ±3 business days). A unique exact debit
  match is settlement evidence: the instruction settles (evidence VERIFIED, F-10) and is RECONCILED
  (matched to a statement line). A credit that brings a payment back makes it RETURNED (obligation open
  again, account FAILED, EC-PAY-04). Everything else waits in a queue: mismatches (amount or currency
  differs, several candidates, EC-PAY-11 / EC-MON-18), unmatched bank lines (EC-PAY-12) and possible
  double payments; the overdue queue lists sent payments past their expected settlement date.
* Decisions on the queue (payroll:payment:reconcile): accept an amount difference with a reason (the
  instruction settles and the difference is recorded as a settlement variance), link a line to the
  right instruction, or escalate it. Nothing is ever settled automatically from a mismatch.
* Webhooks (SANDBOX adapter): ``find_webhook_target`` resolves the tenant from the provider reference
  (system session), ``apply_webhook_event`` applies the signed event in the tenant's session (sandbox
  events never touch production instructions; duplicates processed once, EC-PAY-06; out-of-order events
  never move a status backwards, EC-PAY-07), ``log_webhook`` keeps the platform inbox (no tenant data).
* Run reconciliation: PAID -> RECONCILED when every settled instruction is matched; PARTIALLY_PAID ->
  RECONCILED with every unsettled item carried forward with its reason (decision I-17).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import hashlib
from dataclasses import fields as dataclass_fields
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.pay_runs import PayrollRun
from app.models.payment_flows import (
    BankStatementImport,
    BankStatementLine,
    PaymentInstruction,
    PaymentObligation,
    PaymentWebhookEvent,
    ReconciliationMatch,
)
from app.models.payments import FundingAccount
from app.payroll import state_machines as sm
from app.payroll.banking import matching
from app.payroll.banking import statements as parser
from app.payroll.banking.errors import BankingError
from app.payroll.banking.masking import mask_account, mask_iban, scrub_text
from app.payroll.money import quantize_amount
from app.payroll.services import funding_accounts, obligations
from app.payroll.services import payment_adapters as adapters
from app.payroll.services import payment_events as events

RECONCILE = events.RECONCILE
STATEMENT_IMPORTED = "STATEMENT_IMPORTED"
MISMATCH_RESOLVED = "MISMATCH_RESOLVED"
MATCH_LINKED = "RECONCILIATION_LINKED"
MATCH_ESCALATED = "RECONCILIATION_ESCALATED"
FORMATS = ("CSV", "CAMT053")
MAX_STATEMENT_BYTES = 10 * 1024 * 1024
REVIEW_STATUSES = ("MISMATCH", "UNMATCHED", "DUPLICATE")
QUEUES: Mapping[str, Tuple[str, ...]] = {
    "review": REVIEW_STATUSES, "mismatch": ("MISMATCH",), "unmatched": ("UNMATCHED",), "duplicate": ("DUPLICATE",),
    "escalated": ("ESCALATED",), "resolved": ("MATCHED", "RETURN", "ACCEPTED_VARIANCE", "LINKED"),
    "ignored": ("IGNORED",),
}
EVIDENCE_MATCHES = ("MATCHED", "ACCEPTED_VARIANCE", "LINKED")
SENT_STATUSES = tuple(s for s in sm.INSTRUCTION_RANK if sm.INSTRUCTION_RANK[s] >= sm.INSTRUCTION_RANK["QUEUED"]) + (
    "REJECTED", "FAILED", "RETURNED", "REVERSED", "REISSUED")
CARRIED_STATUSES = ("NOT_READY", "READY", "APPROVAL_PENDING", "APPROVED", "QUEUED", "ON_HOLD", "REJECTED", "FAILED",
                    "RETURNED", "REVERSED")
_MAPPING_FIELDS = {f.name for f in dataclass_fields(parser.CsvColumnMapping)}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
def _funding_for(db: Session, principal: Principal, funding_account_id: int) -> FundingAccount:
    """A funding account inside the caller's reconcile scope (403 without the permission, else 404)."""
    if not principal.has(RECONCILE):
        raise forbidden()
    row = db.query(FundingAccount).filter(FundingAccount.company_id == principal.company_id,
                                          FundingAccount.id == funding_account_id).first()
    if row is None or not events.covers(principal, row.legal_entity_id, [RECONCILE]):
        raise not_found("Funding account")
    return row


def _scoped_funding_ids(db: Session, principal: Principal) -> List[int]:
    return [i for (i,) in db.query(FundingAccount.id).filter(
        FundingAccount.company_id == principal.company_id,
        events.scope_clause(principal, FundingAccount.legal_entity_id, [RECONCILE]))]


# ---------------------------------------------------------------------------
# Statement import (F-11)
# ---------------------------------------------------------------------------
def csv_mapping(raw: Optional[Mapping[str, Any]]) -> parser.CsvColumnMapping:
    """A CSV column mapping from request data (unknown keys refused; validated by the parser)."""
    if not isinstance(raw, Mapping) or not raw:
        raise bad_request("A CSV statement needs a column mapping.", "MAPPING_REQUIRED")
    unknown = set(raw) - _MAPPING_FIELDS
    if unknown:
        raise bad_request(f"Unknown mapping fields: {', '.join(sorted(unknown))}.", "MAPPING_INVALID")
    values = dict(raw)
    for name in ("debit_values", "credit_values"):
        if name in values:
            values[name] = frozenset(str(v).upper() for v in values[name])
    try:
        mapping = parser.CsvColumnMapping(**values)
        mapping.validate()
    except BankingError as exc:
        raise AppError(422, exc.message, exc.code)
    except (TypeError, ValueError):
        raise AppError(422, "The column mapping is invalid.", "MAPPING_INVALID")
    return mapping


def _parse(fmt: str, content: bytes, mapping: Optional[Mapping[str, Any]]) -> parser.ParsedStatement:
    try:
        if fmt == "CSV":
            return parser.parse_csv_statement(content, csv_mapping(mapping), max_bytes=MAX_STATEMENT_BYTES)
        if fmt == "CAMT053":
            return parser.parse_camt053(content, max_bytes=MAX_STATEMENT_BYTES)
    except BankingError as exc:
        raise AppError(422, exc.message, exc.code)
    raise bad_request("The statement format is CSV or CAMT053.", "FORMAT_INVALID")


def _normal(value: Optional[str]) -> str:
    return matching.normalize_account(value) if value else ""


def _check_account(db: Session, funding: FundingAccount, parsed: parser.ParsedStatement) -> None:
    """A CAMT statement names its account: it must be this funding account's."""
    if not parsed.account:
        return
    clear = funding_accounts.funding_plaintext(db, funding.company_id, funding_accounts.funding_snapshot(
        db, funding.company_id, funding.id))
    ours = {_normal(clear.get("account_number")), _normal(clear.get("iban"))} - {""}
    if ours and _normal(parsed.account) not in ours:
        raise conflict("The statement belongs to another bank account than the funding account.",
                       "STATEMENT_ACCOUNT_MISMATCH")


def _mask(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    compact = _normal(value)
    return (mask_iban(compact) if compact[:2].isalpha() else mask_account(compact))[:40]


def _scrub(value: Optional[str], limit: int) -> Optional[str]:
    """Free text (counterparty, remittance) with account-like numbers masked."""
    return scrub_text(value)[:limit] if value else None


def _text(value: Optional[str], limit: int) -> Optional[str]:
    """Structured references (payment and bank references) are kept as the bank wrote them."""
    return value[:limit] if value else None


def import_json(row: BankStatementImport) -> dict:
    return {"id": row.id, "funding_account_id": row.funding_account_id, "format": row.format,
            "file_name": row.file_name, "file_sha256": row.file_sha256, "statement_ref": row.statement_ref,
            "account_masked": row.account_masked, "line_count": row.line_count, "new_line_count": row.new_line_count,
            "rejected": row.rejected or [], "counts": row.counts or {}, "imported_by_user_id": row.imported_by_user_id,
            "created_at": row.created_at}


def import_statement(db: Session, principal: Principal, *, funding_account_id: int, fmt: str, file_name: str,
                     content: bytes, mapping: Optional[Mapping[str, Any]] = None) -> dict:
    """Import and reconcile a statement of a funding account; re-importing it adds nothing (F-11)."""
    funding = _funding_for(db, principal, funding_account_id)
    if fmt not in FORMATS:
        raise bad_request("The statement format is CSV or CAMT053.", "FORMAT_INVALID")
    if not content:
        raise bad_request("Attach the statement file.", "FILE_REQUIRED")
    if len(content) > MAX_STATEMENT_BYTES:
        raise AppError(413, "The statement file is too large.", "FILE_TOO_LARGE")
    sha = hashlib.sha256(content).hexdigest()
    existing = db.query(BankStatementImport).filter(
        BankStatementImport.company_id == funding.company_id, BankStatementImport.funding_account_id == funding.id,
        BankStatementImport.file_sha256 == sha).first()
    if existing is not None:
        return {"import": import_json(existing), "duplicate": True, "matches": []}
    parsed = _parse(fmt, content, mapping)
    _check_account(db, funding, parsed)
    hashes = [line.line_hash for line in parsed.lines]
    seen = {h for (h,) in db.query(BankStatementLine.line_hash).filter(
        BankStatementLine.company_id == funding.company_id, BankStatementLine.funding_account_id == funding.id,
        BankStatementLine.line_hash.in_(hashes))} if hashes else set()
    fresh = [line for line in parsed.lines if line.line_hash not in seen]
    record = BankStatementImport(
        company_id=funding.company_id, funding_account_id=funding.id, format=fmt,
        file_name=(file_name or "statement")[:200], file_sha256=sha,
        statement_ref=(parsed.statement_id or None) and parsed.statement_id[:100],
        account_masked=_mask(parsed.account), line_count=len(parsed.lines), new_line_count=len(fresh),
        rejected=[{"source_row": r.source_row, "code": r.code, "message": r.message[:300]}
                  for r in (*parsed.rejected, *parsed.notices)],
        counts={}, imported_by_user_id=principal.user_id)
    db.add(record)
    db.flush()
    stored: List[Tuple[BankStatementLine, parser.StatementLine]] = []
    for line in fresh:
        row = BankStatementLine(
            company_id=funding.company_id, import_id=record.id, funding_account_id=funding.id,
            source_row=line.source_row, value_date=line.value_date, booking_date=line.booking_date,
            amount=line.amount, currency=line.currency, direction=line.direction,
            reference=_text(line.reference, 140), end_to_end_id=_text(line.end_to_end_id, 140),
            bank_reference=_text(line.bank_reference, 140), counterparty=_scrub(line.counterparty, 200),
            counterparty_account_masked=_mask(line.counterparty_account), remittance=_scrub(line.remittance, 2000),
            return_reason=(line.return_reason or None) and line.return_reason[:10], line_hash=line.line_hash)
        db.add(row)
        stored.append((row, line))
    db.flush()
    results = _reconcile_lines(db, principal, funding, stored)
    counts: Dict[str, int] = {}
    for match in results:
        counts[match.status] = counts.get(match.status, 0) + 1
    record.counts = counts
    db.flush()
    events.audit(db, principal, STATEMENT_IMPORTED, f"Bank statement {record.file_name} imported: {len(fresh)} new "
                 f"line(s) of {len(parsed.lines)}", "bank_statement_import", record.id, record.company_id,
                 funding_account_id=funding.id, format=fmt, sha256=sha, counts=counts,
                 rejected=len(record.rejected or []))
    return {"import": import_json(record), "duplicate": False, "matches": [match_json(db, m) for m in results]}


def _instruction_refs(db: Session, funding: FundingAccount) -> Tuple[List[matching.InstructionRef],
                                                                     Dict[int, PaymentInstruction]]:
    rows = db.query(PaymentInstruction).filter(
        PaymentInstruction.company_id == funding.company_id, PaymentInstruction.funding_account_id == funding.id,
        PaymentInstruction.status.in_(SENT_STATUSES), PaymentInstruction.reference.isnot(None),
    ).order_by(PaymentInstruction.id).all()
    evidence: Dict[int, set] = {}
    if rows:
        for instruction_id, line_hash in db.query(ReconciliationMatch.instruction_id, BankStatementLine.line_hash).join(
                BankStatementLine, (BankStatementLine.company_id == ReconciliationMatch.company_id)
                & (BankStatementLine.id == ReconciliationMatch.statement_line_id)).filter(
                ReconciliationMatch.company_id == funding.company_id,
                ReconciliationMatch.instruction_id.in_([r.id for r in rows]),
                ReconciliationMatch.status.in_(EVIDENCE_MATCHES), BankStatementLine.direction == "DEBIT"):
            evidence.setdefault(instruction_id, set()).add(line_hash)
    refs = []
    for row in rows:
        try:
            clear = obligations.open_snapshot(db, row)
        except AppError:
            clear = {}
        refs.append(matching.InstructionRef(
            instruction_id=row.id, reference=row.reference, amount=events.payout_amount(row),
            currency=events.payout_currency(row), status=row.status,
            accounts=tuple(v for v in (clear.get("account_number"), clear.get("iban")) if v),
            expected_settlement_date=row.expected_settlement_date,
            submitted_on=row.submitted_at.date() if row.submitted_at else None,
            evidence_line_hashes=frozenset(evidence.get(row.id, ()))))
    return refs, {r.id: r for r in rows}


def reconcile_instruction(db: Session, principal: Principal, row: PaymentInstruction, line_id: int
                          ) -> PaymentInstruction:
    """SETTLED -> RECONCILED: the instruction is matched to a statement line."""
    return events.transition(db, principal, row, "RECONCILE", STATEMENT_MATCHED=True, source="STATEMENT",
                             evidence_ref=f"statement_line:{line_id}", updates={"reconciled_at": events.now()},
                             details={"statement_line_id": line_id})


def _settle_from_line(db: Session, principal: Principal, row: PaymentInstruction, line: BankStatementLine, *,
                      amount: Optional[Decimal] = None, updates: Optional[Mapping[str, Any]] = None
                      ) -> events.EvidenceResult:
    return events.apply_evidence(
        db, principal, row, "SETTLED", source="STATEMENT", evidence_source="STATEMENT",
        external_event_id=f"line:{line.id}", evidence_ref=f"statement_line:{line.id}",
        amount=Decimal(line.amount) if amount is None else amount, follow_up=False, updates_extra=updates)


def _reconcile_lines(db: Session, principal: Principal, funding: FundingAccount,
                     stored: Sequence[Tuple[BankStatementLine, parser.StatementLine]]) -> List[ReconciliationMatch]:
    if not stored:
        return []
    refs, by_id = _instruction_refs(db, funding)
    results = matching.match_lines([line for _, line in stored], refs)
    touched: List[PaymentInstruction] = []
    out = []
    for (row, _line), result in zip(stored, results):
        status, reason, message = result.outcome, result.reason, result.message
        instruction = by_id.get(result.instruction_id) if result.instruction_id else None
        if result.outcome == matching.MATCHED and instruction is not None:
            applied = _settle_from_line(db, principal, instruction, row)
            if applied.outcome == "APPLIED" and applied.instruction.status == "SETTLED":
                touched.append(reconcile_instruction(db, principal, applied.instruction, row.id))
            elif applied.outcome == "APPLIED":
                touched.append(applied.instruction)
            else:
                status, reason = "MISMATCH", (applied.reason or applied.outcome)[:40]
                message = "the line could not be recorded as settlement evidence; review it"
        elif result.outcome == matching.RETURN and instruction is not None:
            applied = events.apply_evidence(
                db, principal, instruction, "RETURNED", source="STATEMENT", external_event_id=f"line:{row.id}",
                evidence_ref=f"statement_line:{row.id}", reason_code=row.return_reason, follow_up=False)
            if applied.outcome == "APPLIED":
                touched.append(applied.instruction)
            else:
                status, reason = "MISMATCH", (applied.reason or applied.outcome)[:40]
                message = "a credit for this payment could not be recorded as a return; review it"
        elif result.outcome == matching.ALREADY_MATCHED:
            status = "DUPLICATE"
        match = ReconciliationMatch(
            company_id=funding.company_id, statement_line_id=row.id, funding_account_id=funding.id,
            instruction_id=result.instruction_id if status not in ("UNMATCHED", "IGNORED") else None,
            status=status, reason=reason[:40], message=(message or "")[:300] or None,
            candidate_ids=list(result.candidate_ids),
            variance_amount=result.variance, variance_currency=row.currency if result.variance is not None else None)
        db.add(match)
        out.append(match)
    db.flush()
    events.sync_after(db, principal, touched)
    return out


# ---------------------------------------------------------------------------
# Queues
# ---------------------------------------------------------------------------
def _line_json(line: BankStatementLine) -> dict:
    return {"id": line.id, "import_id": line.import_id, "source_row": line.source_row, "value_date": line.value_date,
            "booking_date": line.booking_date, "amount": events.money_json(line.amount, line.currency),
            "direction": line.direction, "reference": line.reference, "end_to_end_id": line.end_to_end_id,
            "counterparty": line.counterparty, "counterparty_account": line.counterparty_account_masked,
            "return_reason": line.return_reason,
            "return_message": parser.return_reason_message(line.return_reason) if line.return_reason else None}


def match_json(db: Session, match: ReconciliationMatch) -> dict:
    line = db.query(BankStatementLine).filter(BankStatementLine.company_id == match.company_id,
                                              BankStatementLine.id == match.statement_line_id).one()
    instruction = None
    if match.instruction_id is not None:
        row = events.load_instruction(db, match.company_id, match.instruction_id)
        instruction = {"id": row.id, "reference": row.reference, "status": row.status, "employee_id": row.employee_id,
                       "amount": events.money_json(events.payout_amount(row), events.payout_currency(row)),
                       "account": (row.beneficiary_snapshot or {}).get("account_masked")}
    return {"id": match.id, "status": match.status, "reason": match.reason, "message": match.message,
            "funding_account_id": match.funding_account_id, "candidate_ids": match.candidate_ids or [],
            "variance": events.money_json(match.variance_amount, match.variance_currency),
            "decided_by_user_id": match.decided_by_user_id, "decided_at": match.decided_at,
            "decision_reason": match.decision_reason, "row_version": match.row_version,
            "line": _line_json(line), "instruction": instruction}


def list_imports(db: Session, principal: Principal, *, funding_account_id: Optional[int] = None,
                 limit: int = 50, offset: int = 0) -> dict:
    if not principal.has(RECONCILE):
        raise forbidden()
    ids = _scoped_funding_ids(db, principal)
    query = db.query(BankStatementImport).filter(BankStatementImport.company_id == principal.company_id,
                                                 BankStatementImport.funding_account_id.in_(ids or [0]))
    if funding_account_id is not None:
        query = query.filter(BankStatementImport.funding_account_id == funding_account_id)
    total = query.count()
    rows = query.order_by(BankStatementImport.id.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 200)).all()
    return {"total": total, "items": [import_json(r) for r in rows]}


def get_import(db: Session, principal: Principal, import_id: int) -> BankStatementImport:
    if not principal.has(RECONCILE):
        raise forbidden()
    row = db.query(BankStatementImport).filter(BankStatementImport.company_id == principal.company_id,
                                               BankStatementImport.id == import_id).first()
    if row is None or row.funding_account_id not in _scoped_funding_ids(db, principal):
        raise not_found("Bank statement")
    return row


def list_matches(db: Session, principal: Principal, *, import_id: Optional[int] = None, queue: Optional[str] = None,
                 funding_account_id: Optional[int] = None, limit: int = 200, offset: int = 0) -> dict:
    """Match decisions in the caller's reconcile scope; ``queue`` = review | mismatch | unmatched | duplicate |
    escalated | resolved | ignored."""
    if not principal.has(RECONCILE):
        raise forbidden()
    if queue is not None and queue not in QUEUES:
        raise bad_request(f"queue is one of {', '.join(QUEUES)}.", "INVALID_QUEUE")
    ids = _scoped_funding_ids(db, principal)
    query = db.query(ReconciliationMatch).filter(ReconciliationMatch.company_id == principal.company_id,
                                                 ReconciliationMatch.funding_account_id.in_(ids or [0]))
    if import_id is not None:
        get_import(db, principal, import_id)
        query = query.join(BankStatementLine, (BankStatementLine.company_id == ReconciliationMatch.company_id)
                           & (BankStatementLine.id == ReconciliationMatch.statement_line_id)).filter(
            BankStatementLine.import_id == import_id)
    if funding_account_id is not None:
        query = query.filter(ReconciliationMatch.funding_account_id == funding_account_id)
    if queue is not None:
        query = query.filter(ReconciliationMatch.status.in_(QUEUES[queue]))
    total = query.count()
    rows = query.order_by(ReconciliationMatch.id).offset(max(offset, 0)).limit(min(max(limit, 1), 500)).all()
    return {"total": total, "items": [match_json(db, m) for m in rows]}


def overdue(db: Session, principal: Principal, *, funding_account_id: Optional[int] = None,
            as_of: Optional[date] = None, grace: int = 0) -> dict:
    """Sent payments past their expected settlement date without evidence (the overdue queue)."""
    if not principal.has(RECONCILE):
        raise forbidden()
    if grace < 0 or grace > 60:
        raise bad_request("grace is 0-60 business days.", "INVALID_GRACE")
    query = db.query(PaymentInstruction).filter(
        PaymentInstruction.company_id == principal.company_id,
        events.scope_clause(principal, PaymentInstruction.legal_entity_id, [RECONCILE]),
        PaymentInstruction.status.in_(tuple(matching.OPEN_STATUSES)),
        PaymentInstruction.expected_settlement_date.isnot(None), PaymentInstruction.reference.isnot(None))
    if funding_account_id is not None:
        query = query.filter(PaymentInstruction.funding_account_id == funding_account_id)
    rows = {r.id: r for r in query}
    refs = [matching.InstructionRef(instruction_id=r.id, reference=r.reference, amount=events.payout_amount(r),
                                    currency=events.payout_currency(r), status=r.status,
                                    expected_settlement_date=r.expected_settlement_date) for r in rows.values()]
    when = as_of or clock.company_today(db, principal.company_id)
    items = matching.find_overdue(refs, when, grace=grace)
    return {"as_of": when, "items": [{**events.instruction_json(rows[i.instruction_id]),
                                      "business_days_overdue": i.business_days_overdue} for i in items]}


# ---------------------------------------------------------------------------
# Decisions: accept a variance, link, escalate (EC-PAY-11, EC-PAY-12, EC-MON-18)
# ---------------------------------------------------------------------------
def _load_match(db: Session, principal: Principal, match_id: int, expected_version: Optional[int]
                ) -> Tuple[ReconciliationMatch, BankStatementLine]:
    if not principal.has(RECONCILE):
        raise forbidden()
    match = db.query(ReconciliationMatch).filter(
        ReconciliationMatch.company_id == principal.company_id, ReconciliationMatch.id == match_id,
    ).with_for_update(of=ReconciliationMatch).populate_existing().first()
    if match is None or match.funding_account_id not in _scoped_funding_ids(db, principal):
        raise not_found("Reconciliation match")
    if expected_version is not None and match.row_version != expected_version:
        raise conflict("The match was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    line = db.query(BankStatementLine).filter(BankStatementLine.company_id == match.company_id,
                                              BankStatementLine.id == match.statement_line_id).one()
    return match, line


def _decide(match: ReconciliationMatch, principal: Principal, status: str, reason: str, **values: Any) -> None:
    match.status = status
    match.decided_by_user_id = principal.user_id
    match.decided_at = events.now()
    match.decision_reason = reason
    for name, value in values.items():
        setattr(match, name, value)
    match.row_version += 1


def _evidence_taken(db: Session, match: ReconciliationMatch, instruction_id: int) -> bool:
    """Another debit line is already this instruction's settlement evidence."""
    return db.query(ReconciliationMatch.id).join(
        BankStatementLine, (BankStatementLine.company_id == ReconciliationMatch.company_id)
        & (BankStatementLine.id == ReconciliationMatch.statement_line_id)).filter(
        ReconciliationMatch.company_id == match.company_id, ReconciliationMatch.instruction_id == instruction_id,
        ReconciliationMatch.status.in_(EVIDENCE_MATCHES), ReconciliationMatch.id != match.id,
        BankStatementLine.direction == "DEBIT").first() is not None


def _pay_currency_variance(row: PaymentInstruction, variance: Decimal) -> Decimal:
    if row.account_amount is None:
        return variance
    return quantize_amount(variance * Decimal(row.amount) / Decimal(row.account_amount), row.currency)


def accept_variance(db: Session, principal: Principal, match_id: int, reason: str, *,
                    expected_version: Optional[int] = None) -> dict:
    """A reviewer accepts an amount difference with a reason: the instruction settles and the difference is
    recorded as a settlement variance (EC-PAY-11, EC-MON-18). Never automatic."""
    match, line = _load_match(db, principal, match_id, expected_version)
    text = events.reason_text(reason)
    if match.status != "MISMATCH" or match.reason != "AMOUNT_DIFFERS" or match.instruction_id is None:
        raise conflict("Only an amount difference can be accepted as a settlement variance.", "NOT_A_VARIANCE")
    row = events.get_instruction_for(db, principal, match.instruction_id, RECONCILE, lock=True)
    if row.status not in events.IN_FLIGHT and row.status != "PARTIALLY_SETTLED":
        raise conflict(f"The payment is {row.status}; only a payment in flight can settle with a variance.",
                       "INVALID_TRANSITION")
    if line.currency != events.payout_currency(row):
        raise conflict("The currencies differ; this is not an amount variance.", "CURRENCY_DIFFERS")
    variance = Decimal(line.amount) - events.payout_amount(row)
    pay_variance = _pay_currency_variance(row, variance)
    variance_fields = {"variance_amount": pay_variance, "variance_reason": text}
    if variance < 0:
        if row.status != "PARTIALLY_SETTLED":
            result = _settle_from_line(db, principal, row, line)
            if result.outcome != "APPLIED":
                raise conflict(f"The evidence could not be recorded ({result.reason or result.outcome}).",
                               "EVIDENCE_NOT_APPLIED")
            row = result.instruction
        row = events.transition(db, principal, row, "ACCEPT_VARIANCE", reason=text, source="STATEMENT",
                                evidence_ref=f"statement_line:{line.id}",
                                updates={**variance_fields, "settled_amount": Decimal(row.amount)},
                                details={"statement_line_id": line.id, "variance": str(pay_variance),
                                         "match_id": match.id})
    else:
        result = _settle_from_line(db, principal, row, line, amount=events.payout_amount(row),
                                   updates=variance_fields)
        if result.outcome != "APPLIED":
            raise conflict(f"The evidence could not be recorded ({result.reason or result.outcome}).",
                           "EVIDENCE_NOT_APPLIED")
        row = result.instruction
        events.audit(db, principal, MISMATCH_RESOLVED, f"Payment {row.reference}: settlement variance "
                     f"{pay_variance} {row.currency} accepted: {text}", "payment_instruction", row.id, row.company_id,
                     statement_line_id=line.id, variance=str(pay_variance), match_id=match.id, reason=text)
    row = reconcile_instruction(db, principal, row, line.id)
    _decide(match, principal, "ACCEPTED_VARIANCE", text)
    db.flush()
    events.sync_after(db, principal, [row])
    return match_json(db, match)


def link(db: Session, principal: Principal, match_id: int, instruction_id: int, reason: str, *,
         expected_version: Optional[int] = None) -> dict:
    """A reviewer links an unexplained line to the right instruction of the same funding account: a debit
    of exactly the payout amount settles (or reconciles an already settled) payment, a credit returns it."""
    match, line = _load_match(db, principal, match_id, expected_version)
    text = events.reason_text(reason)
    if match.status not in REVIEW_STATUSES:
        raise conflict("Only a line waiting for review can be linked.", "ALREADY_DECIDED")
    row = events.get_instruction_for(db, principal, instruction_id, RECONCILE, lock=True)  # 404 if foreign
    if row.funding_account_id != match.funding_account_id:
        raise conflict("The payment was not paid from this statement's funding account.", "OTHER_FUNDING_ACCOUNT")
    if line.currency != events.payout_currency(row) or Decimal(line.amount) != events.payout_amount(row):
        raise conflict("The amounts differ: accept a variance on the matched line instead.", "AMOUNT_DIFFERS")
    if line.direction == "DEBIT":
        if _evidence_taken(db, match, row.id):
            raise conflict("Another statement line is already this payment's evidence (possible double payment): "
                           "escalate it.", "ALREADY_MATCHED")
        if row.status in events.IN_FLIGHT or row.status == "PARTIALLY_SETTLED":
            result = _settle_from_line(db, principal, row, line)
            if result.outcome != "APPLIED":
                raise conflict(f"The evidence could not be recorded ({result.reason or result.outcome}).",
                               "EVIDENCE_NOT_APPLIED")
            row = result.instruction
        if row.status != "SETTLED":
            raise conflict(f"The payment is {row.status}; it cannot be matched to this debit.", "INVALID_TRANSITION")
        row = reconcile_instruction(db, principal, row, line.id)
    else:
        result = events.apply_evidence(db, principal, row, "RETURNED", source="STATEMENT",
                                       external_event_id=f"line:{line.id}", evidence_ref=f"statement_line:{line.id}",
                                       reason_code=line.return_reason, follow_up=False)
        if result.outcome != "APPLIED":
            raise conflict(f"The return could not be recorded ({result.reason or result.outcome}).",
                           "EVIDENCE_NOT_APPLIED")
        row = result.instruction
    _decide(match, principal, "LINKED", text, instruction_id=row.id)
    db.flush()
    events.audit(db, principal, MATCH_LINKED, f"Statement line linked to payment {row.reference}: {text}",
                 "reconciliation_match", match.id, match.company_id, instruction_id=row.id,
                 statement_line_id=line.id, reason=text)
    events.sync_after(db, principal, [row])
    return match_json(db, match)


def escalate(db: Session, principal: Principal, match_id: int, reason: str, *,
             expected_version: Optional[int] = None) -> dict:
    """Escalate a line (possible double payment, unknown debit) for investigation; nothing settles."""
    match, line = _load_match(db, principal, match_id, expected_version)
    text = events.reason_text(reason)
    if match.status not in REVIEW_STATUSES:
        raise conflict("Only a line waiting for review can be escalated.", "ALREADY_DECIDED")
    previous = match.status
    _decide(match, principal, "ESCALATED", text)
    db.flush()
    events.audit(db, principal, MATCH_ESCALATED, f"Statement line escalated ({previous}): {text}",
                 "reconciliation_match", match.id, match.company_id, previous=previous, statement_line_id=line.id,
                 reason=text)
    return match_json(db, match)


# ---------------------------------------------------------------------------
# Run reconciliation (3.5 §5.10, decision I-17)
# ---------------------------------------------------------------------------
def reconcile_run(db: Session, principal: Principal, run_id: int, reason: Optional[str] = None, *,
                  expected_version: Optional[int] = None) -> dict:
    """PAID -> RECONCILED when every settled instruction is matched to evidence; PARTIALLY_PAID -> RECONCILED
    with every unsettled item carried forward with its reason (returned, failed, held ...)."""
    from app.payroll.services import runs

    run = runs.get_run_for(db, principal, run_id, RECONCILE, lock=True)
    if expected_version is not None and run.row_version != expected_version:
        raise conflict("The run was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    rows = db.query(PaymentInstruction).filter(PaymentInstruction.company_id == run.company_id,
                                               PaymentInstruction.run_id == run.id).order_by(PaymentInstruction.id).all()
    statuses = [r.status for r in rows]
    matched = events.all_settled_reconciled(statuses)
    if run.status == "PAID":
        run = runs.transition(db, principal, run, "RECONCILE", reason=reason, SETTLED_INSTRUCTIONS_MATCHED=matched)
        return _run_payment_json(db, run)
    if run.status != "PARTIALLY_PAID":
        raise conflict(f"A run is reconciled from PAID or PARTIALLY_PAID; this one is {run.status}.",
                       "INVALID_TRANSITION")
    text = events.reason_text(reason)
    in_flight = [r for r in rows if r.status in events.IN_FLIGHT or r.status == "PARTIALLY_SETTLED"]
    if in_flight:
        raise conflict(f"{len(in_flight)} payment(s) are still in flight; wait for their evidence before "
                       "reconciling with carried items.", "PAYMENTS_IN_FLIGHT")
    if not matched:
        raise conflict(sm.GUARDS["SETTLED_INSTRUCTIONS_MATCHED"], "SETTLED_INSTRUCTIONS_NOT_MATCHED")
    now = events.now()
    carried = []
    for row in rows:
        if row.status in CARRIED_STATUSES and row.carried_at is None:
            row.carried_at = now
            row.row_version += 1
            carried.append({"instruction_id": row.id, "status": row.status, "reason": row.status_reason
                            or row.failure_code or row.status})
    obligation_ids = sorted({r.obligation_id for r in rows if r.status in CARRIED_STATUSES})
    for obligation in db.query(PaymentObligation).filter(PaymentObligation.company_id == run.company_id,
                                                         PaymentObligation.id.in_(obligation_ids or [0])):
        if obligation.status != "SETTLED" and obligation.carried_at is None:
            obligation.carried_at = now
            obligation.carried_reason = text
            obligation.row_version += 1
    db.flush()
    run = runs.transition(db, principal, run, "RECONCILE", reason=text, SETTLED_INSTRUCTIONS_MATCHED=True,
                          UNSETTLED_ITEMS_CARRIED=True, details={"carried": carried})
    return _run_payment_json(db, run)


def _run_payment_json(db: Session, run: PayrollRun) -> dict:
    rows = db.query(PaymentInstruction).filter(PaymentInstruction.company_id == run.company_id,
                                               PaymentInstruction.run_id == run.id).all()
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {"run_id": run.id, "status": run.status, "row_version": run.row_version, "instruction_counts": counts,
            "carried": [{"instruction_id": r.id, "status": r.status} for r in rows if r.carried_at is not None]}


def run_payments(db: Session, principal: Principal, run_id: int) -> dict:
    """The run's payment state: status, instruction counts and carried items."""
    from app.payroll.services import runs

    events.require_view(principal)
    run = runs.load_run(db, principal.company_id, run_id)
    if not events.covers(principal, run.legal_entity_id):
        raise not_found("Payroll run")
    return _run_payment_json(db, run)


# ---------------------------------------------------------------------------
# Webhooks (3.7 §7.2; EC-PAY-06, EC-PAY-07, EC-PAY-08)
# ---------------------------------------------------------------------------
WEBHOOK_OUTCOME = {"APPLIED": "PROCESSED", "DUPLICATE": "DUPLICATE", "STALE": "PROCESSED", "CONFLICT": "CONFLICT"}


def webhook_seen(system_db: Session, adapter: str, event_id: str) -> bool:
    return system_db.query(PaymentWebhookEvent.id).filter(PaymentWebhookEvent.adapter == adapter,
                                                          PaymentWebhookEvent.event_id == event_id).first() is not None


def find_webhook_target(system_db: Session, provider_ref: str) -> Optional[Tuple[int, int]]:
    """(company id, instruction id) of a provider reference; references are unique across tenants."""
    found = system_db.query(PaymentInstruction.company_id, PaymentInstruction.id).filter(
        PaymentInstruction.provider_ref == provider_ref).first()
    return (int(found[0]), int(found[1])) if found else None


def apply_webhook_event(db: Session, company_id: int, instruction_id: int, event: adapters.VerifiedEvent) -> str:
    """Apply a verified provider event in the tenant's session; returns APPLIED, DUPLICATE, STALE or CONFLICT."""
    row = events.load_instruction(db, company_id, instruction_id)
    sandbox = event.adapter == adapters.SANDBOX
    if event.currency is not None and event.currency != events.payout_currency(row):
        events.record_unapplied(db, row, event.status, "CONFLICT", source="WEBHOOK",
                                external_event_id=event.event_id, evidence_ref=f"{event.adapter}:{event.event_id}",
                                sandbox=sandbox, details={"reason": "CURRENCY_DIFFERS"})
        return "CONFLICT"
    result = events.apply_evidence(
        db, None, row, event.status, source="WEBHOOK", evidence_source="WEBHOOK", external_event_id=event.event_id,
        evidence_ref=f"{event.adapter}:{event.event_id}", amount=event.amount, sandbox=sandbox,
        reason_code=event.reason_code)
    return result.outcome


def log_webhook(system_db: Session, *, adapter: str, outcome: str, payload_sha256: str,
                event: Optional[adapters.VerifiedEvent] = None, claimed_event_id: Optional[str] = None,
                error_code: Optional[str] = None) -> bool:
    """Record a delivery in the platform inbox; False when a verified event id is already there.

    The inbox stores no tenant identifiers: the applied event is the tenant's ``payment_events`` row."""
    values = {"adapter": adapter[:20], "outcome": outcome, "payload_sha256": payload_sha256,
              "error_code": (error_code or None) and error_code[:40], "received_at": clock.utcnow()}
    if event is not None and outcome != "REJECTED":
        values.update(event_id=event.event_id, event_type=event.event_type, signed_at=event.signed_at)
    else:
        values.update(claimed_event_id=claimed_event_id)
    statement = insert(PaymentWebhookEvent).values(**values)
    if event is not None and outcome != "REJECTED":
        statement = statement.on_conflict_do_nothing(constraint="uq_payment_webhook_events_event")
    result = system_db.execute(statement)
    return bool(result.rowcount)


def is_duplicate_error(exc: IntegrityError) -> bool:
    return "uq_payment_events_external" in str(getattr(exc, "orig", exc))

