"""Accounting journals of finalized runs (3.1 §5.18, 3.3 §2.2 ``gl``; decision I-13: OFF by default).

When ``payroll.accounting.enabled`` is ON for the run's legal entity, one journal is produced per run
at ``payroll.accounting.post_on``:

* FINALIZE: the outbox event ``payroll.run.finalized`` ({"run_id"}) queues ``payroll.accounting_finalized``;
* PAYMENT_SETTLED: a committed transaction that moves a run into PAID or RECONCILED (``runs.transition``
  from the payments milestone) queues ``payroll.accounting_settled`` once per run (see "PAYMENT_SETTLED
  trigger" below); an outbox event ``payroll.run.payment_settled`` ({"run_id"}) or a direct call of
  ``on_payment_settled`` do the same. Nothing is posted for a run that is not paid.

Each posting line of a finalized employee (EARNING, DEDUCTION, EMPLOYER_CONTRIBUTION, REIMBURSEMENT,
ROUNDING; benefits in kind and informational lines move no money) is booked as a debit of the mapping's
``debit_account`` and a credit of its ``credit_account`` for the line amount; a negative line swaps the
sides. The mapping is, first match wins: the legal entity's ``gl_account_mappings`` row for the
component code, the component version's own ``gl`` (3.3 §2.2), the tenant-wide mapping row (pack-owned
statutory components carry no ``gl`` and are read-only, EC-STA-18). With ``allocate_by_cost_center`` both
sides carry the employee's cost center for the run period (the run employee's assignment), so the journal
balances per currency and per cost center. Lines are aggregated per (side, account, cost center,
currency, component). Any line without a mapping, or a cost-center allocation without a cost center,
fails the whole journal (FAILED with the errors, never a partial journal); fixing the mapping and posting
again turns the same row POSTED. A journal is balanced per currency (debits = credits) or it is not
posted, and amounts in different currencies are never added together.

One journal per run (UNIQUE (company_id, run_id)): a repeated event, a concurrent worker or a manual
post returns the posted journal (never double-posted). POSTED journals and their lines are immutable
(triggers); ``content_hash`` is recomputed from the lines whenever the journal is read back or exported.
GENERIC_CSV is the only format (more adapters later); cells are escaped (EC-DOC-17).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, event, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.compensation import PayComponentVersion
from app.models.org import EmployeeAssignment, LegalEntity
from app.models.pay_org import CostCenter
from app.models.pay_runs import PayPeriod, PayrollLine, PayrollRun, PayrollRunEmployee
from app.models.reports_compliance import AccountingJournal, AccountingJournalLine, GlAccountMapping
from app.payroll import audit as payroll_audit
from app.payroll.banking.files import escape_csv_cell
from app.payroll.money import amount_str
from app.payroll.services import access, jobs, outbox
from app.payroll.services import settings as settings_engine
from app.services.audit_log_service import AuditLogService

ENABLED_KEY = "payroll.accounting.enabled"
FORMAT_KEY = "payroll.accounting.journal_format"
POST_ON_KEY = "payroll.accounting.post_on"
FINALIZED_TOPIC = "payroll.run.finalized"
SETTLED_TOPIC = "payroll.run.payment_settled"
FINALIZED_JOB = "payroll.accounting_finalized"
SETTLED_JOB = "payroll.accounting_settled"
READ = "payroll:reports:read"
EXPORT = "payroll:reports:export"
POST = "payroll:run:finalize"
CONFIGURE = "payroll:configuration:write"
AUDIT_MODULE = payroll_audit.PAYROLL_RUNS
SUBJECT = "accounting_journal"
MAPPING_SUBJECT = "gl_account_mapping"
POSTING_KINDS = ("EARNING", "DEDUCTION", "EMPLOYER_CONTRIBUTION", "REIMBURSEMENT", "ROUNDING")
FINALIZED_RUN_STATUSES = ("FINALIZED", "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED")
PAID_RUN_STATUSES = ("PAID", "RECONCILED", "CLOSED")
CSV_COLUMNS = ("journal_number", "posting_date", "legal_entity", "run_number", "line_no", "account", "cost_center",
               "currency", "debit", "credit", "component", "description")
_ZERO = Decimal("0")
logger = logging.getLogger("hrms.payroll.accounting")


@dataclass(frozen=True)
class Mapping:
    debit_account: str
    credit_account: str
    allocate_by_cost_center: bool
    source: str  # ENTITY_MAPPING | COMPONENT | TENANT_MAPPING


@dataclass(frozen=True)
class Entry:
    side: str
    account: str
    amount: Decimal
    currency: str
    cost_center_id: Optional[int]
    cost_center_code: Optional[str]
    component_code: str
    description: str


@dataclass(frozen=True)
class SourceLine:
    component_code: str
    component_version_id: Optional[int]
    kind: str
    amount: Decimal
    currency: str
    cost_center_id: Optional[int]
    cost_center_code: Optional[str]
    fixture: bool


# --------------------------------------------------------------------------- pure journal building


def _valid_gl(gl: Any) -> Optional[Mapping]:
    if not isinstance(gl, dict):
        return None
    debit, credit = (gl.get("debit_account") or "").strip(), (gl.get("credit_account") or "").strip()
    if not debit or not credit:
        return None
    return Mapping(debit, credit, bool(gl.get("allocate_by_cost_center")), "COMPONENT")


def resolve_mapping(code: str, version_gl: Any, entity_rows: Dict[str, Mapping], tenant_rows: Dict[str, Mapping]
                    ) -> Optional[Mapping]:
    """Entity mapping row, then the component version's own ``gl``, then the tenant-wide row."""
    return entity_rows.get(code) or _valid_gl(version_gl) or tenant_rows.get(code)


def build_entries(lines: Sequence[SourceLine], mapping_for) -> Tuple[List[Entry], List[dict]]:
    """Aggregated journal entries and the errors that block posting; ``mapping_for(line) -> Mapping | None``."""
    errors: Dict[Tuple[str, str], dict] = {}
    sums: Dict[Tuple[str, str, Optional[int], str, str], Decimal] = {}
    cc_codes: Dict[Optional[int], Optional[str]] = {}
    for line in lines:
        if line.kind not in POSTING_KINDS or line.amount == 0:
            continue
        mapping = mapping_for(line)
        if mapping is None:
            errors.setdefault(("MISSING_GL_MAPPING", line.component_code), {
                "code": "MISSING_GL_MAPPING", "component": line.component_code,
                "message": f"Component {line.component_code} has no GL accounts (map it in the component or in "
                           "the GL mappings of accounting settings).",
            })
            continue
        cost_center_id = None
        if mapping.allocate_by_cost_center:
            if line.cost_center_id is None:
                errors.setdefault(("COST_CENTER_MISSING", line.component_code), {
                    "code": "COST_CENTER_MISSING", "component": line.component_code,
                    "message": f"{line.component_code} is allocated by cost center but an employee has none for "
                               "the period.",
                })
                continue
            cost_center_id = line.cost_center_id
            cc_codes[cost_center_id] = line.cost_center_code
        debit, credit, amount = mapping.debit_account, mapping.credit_account, line.amount
        if amount < 0:
            debit, credit, amount = credit, debit, -amount
        for side, account in (("DEBIT", debit), ("CREDIT", credit)):
            key = (side, account, cost_center_id, line.currency, line.component_code)
            sums[key] = sums.get(key, _ZERO) + amount
    entries = [
        Entry(side=side, account=account, amount=amount, currency=currency, cost_center_id=cc,
              cost_center_code=cc_codes.get(cc), component_code=code,
              description=f"{code} {'debit' if side == 'DEBIT' else 'credit'}")
        for (side, account, cc, currency, code), amount in sorted(
            sums.items(), key=lambda kv: (kv[0][3], kv[0][4], kv[0][0] != "DEBIT", kv[0][1], kv[0][2] or 0))
        if amount != 0
    ]
    for currency, (debit, credit) in balances(entries).items():
        if debit != credit:
            errors[("UNBALANCED", currency)] = {"code": "UNBALANCED", "currency": currency,
                                                "message": f"Debits {debit} and credits {credit} differ in {currency}."}
    return entries, list(errors.values())


def balances(entries: Iterable[Entry]) -> Dict[str, Tuple[Decimal, Decimal]]:
    result: Dict[str, List[Decimal]] = {}
    for entry in entries:
        pair = result.setdefault(entry.currency, [_ZERO, _ZERO])
        pair[0 if entry.side == "DEBIT" else 1] += entry.amount
    return {currency: (pair[0], pair[1]) for currency, pair in sorted(result.items())}


def entries_hash(run_id: int, entries: Sequence[Any]) -> str:
    """SHA-256 of the canonical journal content (works for Entry and AccountingJournalLine rows)."""
    payload = [
        [e.side, e.account, format(Decimal(e.amount).quantize(Decimal("0.0001")), "f"), e.currency,
         e.cost_center_id, e.component_code]
        for e in entries
    ]
    canonical = json.dumps({"run_id": run_id, "lines": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- database


def _settings(db: Session, run: PayrollRun, period: PayPeriod) -> Dict[str, Any]:
    resolved = settings_engine.resolve_many(db, run.company_id, (ENABLED_KEY, FORMAT_KEY, POST_ON_KEY),
                                            legal_entity_id=run.legal_entity_id, on=period.period_end)
    return {key: value.value for key, value in resolved.items()}


def _source_lines(db: Session, run: PayrollRun) -> List[SourceLine]:
    rows = (
        db.query(PayrollLine.component_code, PayrollLine.component_version_id, PayrollLine.kind, PayrollLine.amount,
                 PayrollLine.currency, EmployeeAssignment.cost_center_id, CostCenter.code, PayrollLine.fixture)
        .join(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollLine.company_id,
                                       PayrollRunEmployee.id == PayrollLine.run_employee_id))
        .outerjoin(EmployeeAssignment, and_(EmployeeAssignment.company_id == PayrollRunEmployee.company_id,
                                    EmployeeAssignment.id == PayrollRunEmployee.employee_assignment_id))
        .outerjoin(CostCenter, and_(CostCenter.company_id == PayrollLine.company_id,
                                    CostCenter.id == EmployeeAssignment.cost_center_id))
        .filter(PayrollLine.company_id == run.company_id, PayrollLine.run_id == run.id,
                PayrollRunEmployee.status == "CALCULATED")
        .order_by(PayrollLine.run_employee_id, PayrollLine.sort_order, PayrollLine.id)
        .all()
    )
    return [SourceLine(code, version_id, kind, Decimal(amount), currency, cc_id, cc_code, bool(fixture))
            for code, version_id, kind, amount, currency, cc_id, cc_code, fixture in rows]


def _mapper(db: Session, company_id: int, legal_entity_id: int, lines: Sequence[SourceLine]):
    version_ids = sorted({line.component_version_id for line in lines if line.component_version_id})
    versions = {vid: gl for vid, gl in db.query(PayComponentVersion.id, PayComponentVersion.gl).filter(
        PayComponentVersion.company_id == company_id, PayComponentVersion.id.in_(version_ids))} if version_ids else {}
    entity_rows: Dict[str, Mapping] = {}
    tenant_rows: Dict[str, Mapping] = {}
    for row in db.query(GlAccountMapping).filter(GlAccountMapping.company_id == company_id):
        mapping = Mapping(row.debit_account.strip(), row.credit_account.strip(), row.allocate_by_cost_center,
                          "ENTITY_MAPPING" if row.legal_entity_id is not None else "TENANT_MAPPING")
        if row.legal_entity_id == legal_entity_id:
            entity_rows[row.component_code] = mapping
        elif row.legal_entity_id is None:
            tenant_rows[row.component_code] = mapping
    return lambda line: resolve_mapping(line.component_code, versions.get(line.component_version_id),
                                        entity_rows, tenant_rows)


def _next_number(db: Session, company_id: int, year: int) -> str:
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"accounting_journals:{company_id}"})
    prefix = f"JE-{year:04d}-"
    last = (db.query(func.max(AccountingJournal.number))
            .filter(AccountingJournal.company_id == company_id, AccountingJournal.number.like(f"{prefix}%")).scalar())
    sequence = int(last[len(prefix):]) + 1 if last else 1
    return f"{prefix}{sequence:06d}"


def _totals(entries: Sequence[Entry]) -> Dict[str, dict]:
    counts: Dict[str, int] = {}
    for entry in entries:
        counts[entry.currency] = counts.get(entry.currency, 0) + 1
    return {currency: {"debit": amount_str(debit, currency), "credit": amount_str(credit, currency),
                       "lines": counts[currency]}
            for currency, (debit, credit) in balances(entries).items()}


def _audit(db: Session, principal: Optional[Principal], action: str, description: str, journal: AccountingJournal,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description[:500], SUBJECT, journal.id,
                        company_id=journal.company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def post_journal(db: Session, principal: Optional[Principal], run: PayrollRun, trigger: str) -> AccountingJournal:
    """Post (or return) the run's journal; a FAILED journal records why and is retried in place."""
    if run.status not in FINALIZED_RUN_STATUSES:
        raise conflict("Only finalized runs have journals.", "RUN_NOT_FINALIZED")
    existing = (db.query(AccountingJournal)
                .filter(AccountingJournal.company_id == run.company_id, AccountingJournal.run_id == run.id)
                .with_for_update().populate_existing().first())
    if existing is not None and existing.status == "POSTED":
        return existing
    period = db.query(PayPeriod).filter(PayPeriod.company_id == run.company_id, PayPeriod.id == run.pay_period_id).one()
    lines = _source_lines(db, run)
    entries, errors = build_entries(lines, _mapper(db, run.company_id, run.legal_entity_id, lines))
    if not entries and not errors:
        errors = [{"code": "NOTHING_TO_POST", "message": "The run has no lines that move money."}]
    now = clock.utcnow()
    posting_date = clock.company_today(db, run.company_id) if trigger == "PAYMENT_SETTLED" else period.period_end
    fixture = any(line.fixture for line in lines)
    status = "FAILED" if errors else "POSTED"
    digest = None if errors else entries_hash(run.id, entries)
    values = dict(trigger=trigger, status=status, posting_date=posting_date,
                  totals={} if errors else _totals(entries), line_count=0 if errors else len(entries),
                  content_hash=digest, errors=errors, fixture=fixture, posted_at=None if errors else now,
                  posted_by_user_id=principal.user_id if principal is not None and not errors else None)
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        existing.attempts += 1
        existing.row_version += 1
        journal = existing
        db.flush()
    else:
        journal = AccountingJournal(company_id=run.company_id, run_id=run.id, legal_entity_id=run.legal_entity_id,
                                    number=_next_number(db, run.company_id, posting_date.year), format="GENERIC_CSV",
                                    attempts=1, row_version=1, **values)
        try:
            with db.begin_nested():
                db.add(journal)
                db.flush()
        except IntegrityError:
            # A concurrent poster won: return its journal (never double-posted).
            found = (db.query(AccountingJournal)
                     .filter(AccountingJournal.company_id == run.company_id, AccountingJournal.run_id == run.id)
                     .with_for_update().populate_existing().first())
            if found is None:
                raise
            if found.status == "FAILED" and not errors:
                return post_journal(db, principal, run, trigger)
            return found
    if not errors:
        for number, entry in enumerate(entries, start=1):
            db.add(AccountingJournalLine(
                company_id=run.company_id, journal_id=journal.id, line_no=number, account=entry.account,
                side=entry.side, amount=entry.amount, currency=entry.currency, cost_center_id=entry.cost_center_id,
                cost_center_code=entry.cost_center_code, component_code=entry.component_code,
                description=entry.description[:300],
            ))
        db.flush()
        _audit(db, principal, "JOURNAL_POSTED", f"Posted journal {journal.number} for run {run.number}", journal,
               run_id=run.id, trigger=trigger, lines=len(entries), totals=journal.totals, content_hash=digest)
    else:
        _audit(db, principal, "JOURNAL_FAILED", f"Journal for run {run.number} not posted", journal,
               run_id=run.id, trigger=trigger, errors=[e["code"] + ":" + e.get("component", e.get("currency", ""))
                                                       for e in errors])
    return journal


def handle_event(db: Session, company_id: int, run_id: int, trigger: str) -> dict:
    """Outbox subscriber body: post when accounting is ON and ``post_on`` matches the event."""
    run = db.query(PayrollRun).filter(PayrollRun.company_id == company_id, PayrollRun.id == run_id).first()
    if run is None:
        return {"skipped": "RUN_NOT_FOUND"}
    if run.status not in FINALIZED_RUN_STATUSES:
        return {"skipped": "RUN_NOT_FINALIZED"}
    period = db.query(PayPeriod).filter(PayPeriod.company_id == company_id, PayPeriod.id == run.pay_period_id).one()
    try:
        values = _settings(db, run, period)
    except settings_engine.SettingConfigError as exc:
        raise jobs.PermanentJobError(f"Accounting settings are invalid: {exc.code}") from exc
    if values.get(ENABLED_KEY) is not True:
        return {"skipped": "ACCOUNTING_DISABLED"}
    if values.get(POST_ON_KEY) != trigger:
        return {"skipped": f"POST_ON_{values.get(POST_ON_KEY)}"}
    if trigger == "PAYMENT_SETTLED" and run.status not in PAID_RUN_STATUSES:
        return {"skipped": "RUN_NOT_PAID"}
    journal = post_journal(db, None, run, trigger)
    return {"journal_id": journal.id, "status": journal.status}


def on_payment_settled(db: Session, company_id: int, run_id: int) -> dict:
    """For the payments milestone: call in the transaction that makes a run PAID (same rules as the event)."""
    return handle_event(db, company_id, run_id, "PAYMENT_SETTLED")


@jobs.register(FINALIZED_JOB)
def _on_finalized(ctx: jobs.JobContext) -> dict:
    return handle_event(ctx.db, ctx.company_id, int(ctx.payload["run_id"]), "FINALIZE")


@jobs.register(SETTLED_JOB)
def _on_settled(ctx: jobs.JobContext) -> dict:
    return handle_event(ctx.db, ctx.company_id, int(ctx.payload["run_id"]), "PAYMENT_SETTLED")


outbox.subscribe(FINALIZED_TOPIC, FINALIZED_JOB)
outbox.subscribe(SETTLED_TOPIC, SETTLED_JOB)


# --------------------------------------------------------------------------- PAYMENT_SETTLED trigger
#
# The payments milestone moves a run to PAID (or RECONCILED) through ``runs.transition`` without an event of
# its own. So that ``post_on = PAYMENT_SETTLED`` never depends on every caller remembering to call
# ``on_payment_settled``, a flush that moves a run INTO a paid state records the run, and the transaction's
# commit queues ``payroll.accounting_settled`` once per run (idempotency key; ``handle_event`` re-checks the
# settings, the status and never double-posts). A rolled-back transaction queues nothing.

_PAID_RUNS_KEY = "accounting.paid_run_ids"


def _paid_runs_in_flush(session: Session, _flush_context) -> None:
    from sqlalchemy import inspect as sa_inspect

    for obj in session.dirty:
        if not isinstance(obj, PayrollRun):
            continue
        history = sa_inspect(obj).attrs.status.history  # pre-flush history; never emits SQL
        if not history.added or not history.deleted:
            continue
        if history.added[0] in PAID_RUN_STATUSES and history.deleted[0] not in PAID_RUN_STATUSES:
            session.info.setdefault(_PAID_RUNS_KEY, set()).add((obj.company_id, obj.id))


def _queue_settled_journals(session: Session) -> None:
    from app.database.db import BYPASS_KEY, current_company_id

    pending = session.info.pop(_PAID_RUNS_KEY, None)
    if not pending:
        return
    bound = current_company_id(session)
    for company_id, run_id in sorted(pending):
        if bound != company_id and not session.info.get(BYPASS_KEY):
            # Not the run's tenant transaction: the journal can still be posted by hand (POST .../journal).
            logger.warning("accounting: run %s became paid outside its tenant transaction; journal not queued",
                           run_id)
            continue
        jobs.enqueue(session, company_id=company_id, type=SETTLED_JOB, payload={"run_id": run_id},
                     idempotency_key=f"accounting:settled:{run_id}", max_attempts=5)


def _forget_paid_runs(session: Session, previous_transaction=None) -> None:
    # A savepoint rollback keeps what the outer transaction recorded (the handler re-checks the run status).
    if not getattr(previous_transaction, "nested", False):
        session.info.pop(_PAID_RUNS_KEY, None)


event.listen(Session, "after_flush", _paid_runs_in_flush)
event.listen(Session, "before_commit", _queue_settled_journals)
event.listen(Session, "after_soft_rollback", _forget_paid_runs)


# --------------------------------------------------------------------------- API helpers


def _journal_query(db: Session, principal: Principal, permission: str = READ):
    if not principal.has(permission):
        raise forbidden()
    return (db.query(AccountingJournal)
            .join(PayrollRun, and_(PayrollRun.company_id == AccountingJournal.company_id,
                                   PayrollRun.id == AccountingJournal.run_id))
            .filter(AccountingJournal.company_id == principal.company_id,
                    access.run_scope_clause(principal, permission, PayrollRun.legal_entity_id, PayrollRun.pay_group_id)))


def _lines(db: Session, journal: AccountingJournal) -> List[AccountingJournalLine]:
    return (db.query(AccountingJournalLine)
            .filter(AccountingJournalLine.company_id == journal.company_id,
                    AccountingJournalLine.journal_id == journal.id)
            .order_by(AccountingJournalLine.line_no).all())


def verify(journal: AccountingJournal, lines: Sequence[AccountingJournalLine]) -> None:
    if journal.status != "POSTED":
        return
    if len(lines) != journal.line_count or entries_hash(journal.run_id, lines) != journal.content_hash:
        raise conflict("The journal's lines do not match its recorded hash.", "JOURNAL_INTEGRITY")


def serialize(db: Session, journal: AccountingJournal, *, detail: bool = False) -> dict:
    run = db.query(PayrollRun.number).filter(PayrollRun.company_id == journal.company_id,
                                             PayrollRun.id == journal.run_id).scalar()
    data = {
        "id": journal.id, "number": journal.number, "run_id": journal.run_id, "run_number": run,
        "legal_entity_id": journal.legal_entity_id, "trigger": journal.trigger, "format": journal.format,
        "status": journal.status, "posting_date": journal.posting_date, "totals": journal.totals,
        "line_count": journal.line_count, "content_hash": journal.content_hash, "errors": journal.errors,
        "attempts": journal.attempts, "fixture": journal.fixture, "posted_at": journal.posted_at,
        "row_version": journal.row_version,
    }
    if detail:
        lines = _lines(db, journal)
        verify(journal, lines)
        data["lines"] = [
            {"line_no": l.line_no, "account": l.account, "side": l.side,
             "amount": {"amount": amount_str(Decimal(l.amount), l.currency), "currency": l.currency},
             "cost_center_id": l.cost_center_id, "cost_center_code": l.cost_center_code,
             "component_code": l.component_code, "description": l.description}
            for l in lines
        ]
    return data


def list_journals(db: Session, principal: Principal, *, run_id: Optional[int] = None, status: Optional[str] = None,
                  legal_entity_id: Optional[int] = None, skip: int = 0, limit: int = 50) -> Tuple[int, List[dict]]:
    query = _journal_query(db, principal)
    if run_id is not None:
        query = query.filter(AccountingJournal.run_id == run_id)
    if status:
        query = query.filter(AccountingJournal.status == status)
    if legal_entity_id is not None:
        query = query.filter(AccountingJournal.legal_entity_id == legal_entity_id)
    total = query.count()
    rows = query.order_by(AccountingJournal.id.desc()).offset(skip).limit(limit).all()
    return total, [serialize(db, j) for j in rows]


def get_journal(db: Session, principal: Principal, journal_id: int, permission: str = READ) -> AccountingJournal:
    found = _journal_query(db, principal, permission).filter(AccountingJournal.id == journal_id).first()
    if found is None:
        raise not_found("Journal")
    return found


def render_csv(db: Session, journal: AccountingJournal) -> bytes:
    if journal.status != "POSTED":
        raise conflict("Only posted journals can be exported.", "JOURNAL_NOT_POSTED")
    lines = _lines(db, journal)
    verify(journal, lines)
    run_number = db.query(PayrollRun.number).filter(PayrollRun.company_id == journal.company_id,
                                                    PayrollRun.id == journal.run_id).scalar()
    entity_code = db.query(LegalEntity.code).filter(LegalEntity.company_id == journal.company_id,
                                                    LegalEntity.id == journal.legal_entity_id).scalar()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for line in lines:
        amount = amount_str(Decimal(line.amount), line.currency)
        values = (journal.number, journal.posting_date.isoformat(), entity_code, run_number, line.line_no,
                  line.account, line.cost_center_code or "", line.currency,
                  amount if line.side == "DEBIT" else "", amount if line.side == "CREDIT" else "",
                  line.component_code, line.description)
        writer.writerow([escape_csv_cell("" if v is None else str(v)) for v in values])
    return out.getvalue().encode("utf-8")


def export_journal(db: Session, principal: Principal, journal_id: int) -> Tuple[bytes, AccountingJournal]:
    journal = get_journal(db, principal, journal_id, EXPORT)
    data = render_csv(db, journal)
    _audit(db, principal, "JOURNAL_EXPORTED", f"Exported journal {journal.number}", journal,
           sha256=hashlib.sha256(data).hexdigest(), format=journal.format)
    return data, journal


def post_manually(db: Session, principal: Principal, run_id: int) -> AccountingJournal:
    """Post or retry a run's journal by hand (``payroll:run:finalize``); accounting must be ON."""
    if not principal.has(POST):
        raise forbidden()
    run = (db.query(PayrollRun)
           .filter(PayrollRun.company_id == principal.company_id, PayrollRun.id == run_id,
                   access.run_scope_clause(principal, POST, PayrollRun.legal_entity_id, PayrollRun.pay_group_id))
           .first())
    if run is None:
        raise not_found("Payroll run")
    period = db.query(PayPeriod).filter(PayPeriod.company_id == run.company_id, PayPeriod.id == run.pay_period_id).one()
    try:
        values = _settings(db, run, period)
    except settings_engine.SettingConfigError as exc:
        raise conflict(exc.message, exc.code)
    if values.get(ENABLED_KEY) is not True:
        raise conflict("Accounting journals are off for this legal entity.", "ACCOUNTING_DISABLED")
    trigger = "MANUAL"
    if values.get(POST_ON_KEY) == "PAYMENT_SETTLED" and run.status not in PAID_RUN_STATUSES:
        raise conflict("Journals are posted when payments settle; this run is not paid yet.", "RUN_NOT_PAID")
    return post_journal(db, principal, run, trigger)


# --------------------------------------------------------------------------- GL mappings


def mapping_json(row: GlAccountMapping) -> dict:
    return {"id": row.id, "legal_entity_id": row.legal_entity_id, "component_code": row.component_code,
            "debit_account": row.debit_account, "credit_account": row.credit_account,
            "allocate_by_cost_center": row.allocate_by_cost_center, "row_version": row.row_version,
            "updated_at": row.updated_at}


def list_mappings(db: Session, principal: Principal, *, legal_entity_id: Optional[int] = None) -> List[dict]:
    if not (principal.has(CONFIGURE) or principal.has("payroll:configuration:read")):
        raise forbidden()
    permission = CONFIGURE if principal.has(CONFIGURE) else "payroll:configuration:read"
    scope = access.target_scope(principal, permission)
    query = db.query(GlAccountMapping).filter(GlAccountMapping.company_id == principal.company_id)
    if not scope.all_company:
        query = query.filter(GlAccountMapping.legal_entity_id.in_(sorted(scope.legal_entity_ids) or [-1]))
    if legal_entity_id is not None:
        query = query.filter(GlAccountMapping.legal_entity_id == legal_entity_id)
    return [mapping_json(r) for r in query.order_by(GlAccountMapping.component_code, GlAccountMapping.id)]


def upsert_mapping(db: Session, principal: Principal, *, component_code: str, debit_account: str,
                   credit_account: str, allocate_by_cost_center: bool, legal_entity_id: Optional[int] = None,
                   expected_version: Optional[int] = None) -> GlAccountMapping:
    """Create or replace the mapping of a component code (entity rows need the entity in scope; tenant-wide
    rows need ``payroll:configuration:write`` at COMPANY scope)."""
    if not principal.has(CONFIGURE):
        raise forbidden()
    if legal_entity_id is None:
        if not principal.has_company_scope(CONFIGURE):
            raise forbidden()
    else:
        access.ensure_in_scope(principal, CONFIGURE, legal_entity_id, None, "Legal entity")
        if db.query(LegalEntity.id).filter(LegalEntity.company_id == principal.company_id,
                                           LegalEntity.id == legal_entity_id).first() is None:
            raise not_found("Legal entity")
    code = (component_code or "").strip().upper()
    debit, credit = (debit_account or "").strip(), (credit_account or "").strip()
    if not debit or not credit:
        raise bad_request("Give both the debit and the credit account.", "GL_ACCOUNT_REQUIRED")
    query = db.query(GlAccountMapping).filter(GlAccountMapping.company_id == principal.company_id,
                                              GlAccountMapping.component_code == code)
    query = query.filter(GlAccountMapping.legal_entity_id.is_(None) if legal_entity_id is None
                         else GlAccountMapping.legal_entity_id == legal_entity_id)
    row = query.with_for_update().first()
    before = mapping_json(row) if row is not None else None
    if row is None:
        row = GlAccountMapping(company_id=principal.company_id, legal_entity_id=legal_entity_id, component_code=code,
                               debit_account=debit, credit_account=credit,
                               allocate_by_cost_center=allocate_by_cost_center,
                               updated_by_user_id=principal.user_id, row_version=1)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            raise conflict("This component was mapped by someone else; reload and try again.", "ROW_VERSION_CONFLICT")
    else:
        if expected_version is not None and expected_version != row.row_version:
            raise conflict("The mapping was changed by someone else; reload it and try again.",
                           "ROW_VERSION_CONFLICT")
        row.debit_account, row.credit_account = debit, credit
        row.allocate_by_cost_center = allocate_by_cost_center
        row.updated_by_user_id = principal.user_id
        row.row_version += 1
        db.flush()
    AuditLogService.log(db, principal, payroll_audit.PAYROLL_SETTINGS, "GL_MAPPING_SAVED",
                        f"GL accounts of {code} saved", MAPPING_SUBJECT, row.id, company_id=principal.company_id,
                        details={"before": before, "after": mapping_json(row)})
    return row


def delete_mapping(db: Session, principal: Principal, mapping_id: int) -> None:
    if not principal.has(CONFIGURE):
        raise forbidden()
    row = (db.query(GlAccountMapping)
           .filter(GlAccountMapping.company_id == principal.company_id, GlAccountMapping.id == mapping_id)
           .with_for_update().first())
    if row is None or (row.legal_entity_id is None and not principal.has_company_scope(CONFIGURE)) or (
            row.legal_entity_id is not None
            and not access.target_scope(principal, CONFIGURE).covers(row.legal_entity_id)):
        raise not_found("GL mapping")
    before = mapping_json(row)
    db.delete(row)
    db.flush()
    AuditLogService.log(db, principal, payroll_audit.PAYROLL_SETTINGS, "GL_MAPPING_DELETED",
                        f"GL accounts of {before['component_code']} removed", MAPPING_SUBJECT, mapping_id,
                        company_id=principal.company_id, details={"before": before})


__all__ = [
    "FINALIZED_TOPIC", "SETTLED_TOPIC", "Entry", "Mapping", "SourceLine", "balances", "build_entries",
    "delete_mapping", "entries_hash", "export_journal", "get_journal", "handle_event", "list_journals",
    "list_mappings", "on_payment_settled", "post_journal", "post_manually", "render_csv", "resolve_mapping",
    "serialize", "upsert_mapping",
]

