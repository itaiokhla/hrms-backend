"""Off-cycle runs (3.5 §13; 3.7 §10; EC-COR-06, EC-COR-11, EC-PAY-04) and the service-prepared results that
correction runs share.

Kinds (``run.selection["off_cycle"]``):

* ``BONUS`` / ``CORRECTION``: an OFF_CYCLE run over selected employees and selected components, referencing a
  regular pay period of the pay group for YTD and statutory purposes (``ytd_period_id``). It is calculated
  by the normal pipeline; ``selected_components(run)`` tells the snapshot builder and the calculation which
  components the run may produce (everything else is left out), so a bonus run pays only the bonus inputs
  approved for the period and the pack applies its supplemental withholding (EC-COR-06). Finalizing it
  updates YTD like any run.
* ``REISSUE`` (payment-only): failed, rejected, returned or bank-reversed payment instructions of a finalized
  run are reissued through an off-cycle run that references the original run and lines. No earnings are
  created: the run's employees carry zero figures, and when the run is finalized each instruction is
  reissued on its original obligation (``obligations`` reissue: a new instruction to the payee's account
  as it is now; the old one becomes REISSUED). The run's own approval chain is the control (maker ≠
  approver), so no second reissue approval is asked.
* ``LOAN_DISBURSEMENT`` (payment-only): approved loans or advances not yet disbursed are paid through an
  off-cycle run with a non-wage line (kind REIMBURSEMENT, category LOAN_DISBURSEMENT) equal to the
  principal; the payment obligation comes from the run's net like any run, and finalization records the
  disbursement on the loan (APPROVED -> ACTIVE) with the run number as the reference.

Payment-only runs, like reversal runs, are not calculated by the engine: ``prepare_results`` writes their
lines and figures and moves the run DRAFT -> READY -> CALCULATING -> CALCULATED -> REVIEW_REQUIRED through
``runs.transition`` (the only writer of the status); submission, approval and finalization then follow the
normal lifecycle.

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.compensation import PayComponent
from app.models.pay_runs import PayrollLine, PayrollRun, PayrollRunEmployee
from app.payroll import audit as payroll_audit
from app.payroll import state_machines as sm
from app.payroll.money import amount_str
from app.payroll.services import access, calendars
from app.services.audit_log_service import AuditLogService

READ = "payroll:run:read"
CREATE = sm.RUN_CREATE
CALCULATE = sm.RUN_CALCULATE
AUDIT_MODULE = payroll_audit.PAYROLL_RUNS
KINDS = ("BONUS", "CORRECTION", "REISSUE", "LOAN_DISBURSEMENT")
PAYMENT_ONLY = ("REISSUE", "LOAN_DISBURSEMENT")
REISSUABLE = ("FAILED", "REJECTED", "RETURNED", "REVERSED")
OFF_CYCLE_CREATED = "OFF_CYCLE_RUN_CREATED"
OFF_CYCLE_EFFECTS = "OFF_CYCLE_RUN_EFFECTS"
LOAN_DISBURSEMENT_CODE = "LOAN_DISBURSEMENT"
ZERO = Decimal(0)


def _text(value: Optional[str], limit: int = 1000) -> Optional[str]:
    cleaned = " ".join((value or "").split())
    return cleaned[:limit] or None


def _audit(db: Session, principal: Optional[Principal], action: str, description: str, run: PayrollRun,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description[:500], "payroll_run", run.id,
                        company_id=run.company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


def off_cycle_of(run: PayrollRun) -> dict:
    return dict((run.selection or {}).get("off_cycle") or {})


def selected_components(run: PayrollRun) -> Optional[Tuple[str, ...]]:
    """The components a BONUS / CORRECTION off-cycle run may produce (None: no restriction). The snapshot
    builder keeps only these components' inputs and lines; payment-only runs produce no earnings at all."""
    data = off_cycle_of(run)
    if run.run_type != sm.RunType.OFF_CYCLE.value or not data:
        return None
    if data.get("payment_only"):
        return ()
    return tuple(data.get("components") or ())


# ---------------------------------------------------------------------------
# Service-prepared results (payment-only and reversal runs)
# ---------------------------------------------------------------------------
@dataclass
class Prepared:
    """The figures and lines of one run employee written by a correction service (no engine)."""

    gross: Decimal = ZERO
    deductions: Decimal = ZERO
    reimbursements: Decimal = ZERO
    net: Decimal = ZERO
    employer_cost: Decimal = ZERO
    lines: List[dict] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


def _run_totals(rows: Sequence[PayrollRunEmployee]) -> dict:
    totals: Dict[str, Dict[str, Decimal]] = {}
    counts: Dict[str, int] = {}
    for row in rows:
        if row.status != "CALCULATED":
            continue
        bucket = totals.setdefault(row.currency, {k: ZERO for k in ("gross", "deductions", "reimbursements", "net",
                                                                     "employer_cost")})
        for name in bucket:
            bucket[name] += Decimal(getattr(row, name) or 0)
        counts[row.currency] = counts.get(row.currency, 0) + 1
    return {cur: {**{k: amount_str(v, cur) for k, v in figures.items()}, "employees": counts[cur]}
            for cur, figures in sorted(totals.items())}


def prepare_results(db: Session, principal: Principal, run: PayrollRun, results: Mapping[int, Prepared], *,
                    source: str) -> PayrollRun:
    """Write service-made results and move the run to REVIEW_REQUIRED through the run transition.

    ``results`` maps run-employee ids to their figures; other run employees of the run are EXCLUDED
    (NOT_SELECTED). The caller needs payroll:run:create and payroll:run:calculate in the run's scope."""
    from app.payroll.services import runs

    run = runs.transition(db, principal, run, "PRECHECK_PASSED", details={"prepared_by": source})
    run = runs.transition(db, principal, run, "CALCULATE", IDEMPOTENCY_KEY=True, details={"prepared_by": source})
    now = clock.utcnow()
    rows = (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id)
            .with_for_update(of=PayrollRunEmployee).populate_existing().all())
    for row in rows:
        prepared = results.get(row.id)
        if prepared is None:
            if row.status != "EXCLUDED":
                row.status, row.exclusion_reason = "EXCLUDED", "NOT_SELECTED"
                row.row_version += 1
            continue
        db.query(PayrollLine).filter(PayrollLine.company_id == run.company_id,
                                     PayrollLine.run_employee_id == row.id).delete(synchronize_session=False)
        for index, line in enumerate(prepared.lines):
            db.add(PayrollLine(company_id=run.company_id, run_id=run.id, run_employee_id=row.id,
                               sort_order=line.pop("sort_order", index * 10), **line))
        row.status, row.exclusion_reason, row.exclusion_note = "CALCULATED", None, None
        row.gross, row.deductions, row.reimbursements = prepared.gross, prepared.deductions, prepared.reimbursements
        row.net, row.employer_cost = prepared.net, prepared.employer_cost
        content = {"source": source, "run_id": run.id, "run_employee_id": row.id,
                   "lines": [{k: str(v) for k, v in sorted(l.items())} for l in prepared.lines]}
        row.content_hash = _hash(content)
        row.calc_hash = _hash({"content": row.content_hash, "net": str(prepared.net), "gross": str(prepared.gross)})
        row.calculated_at, row.error = now, None
        row.flags = sorted(set([*(row.flags or []), *prepared.flags]))
        row.row_version += 1
    db.flush()
    run.totals = _run_totals(rows)
    run.calculated_at = now
    runs._refresh_summary(db, run)
    db.flush()
    run = runs.transition(db, None, run, "COMPLETE_CALCULATION", system=True, details={"prepared_by": source})
    return runs.transition(db, None, run, "PASS_VALIDATION", system=True, NO_BLOCKING_CHECK_FAILED=True,
                           details={"prepared_by": source})


def ensure_can_prepare(principal: Principal, legal_entity_id: int, pay_group_id: int) -> None:
    """Correction runs are created and 'calculated' on the caller's behalf: both rights are needed."""
    for code in (CREATE, CALCULATE):
        if not principal.has(code):
            raise forbidden(f"Creating this correction run needs {code}.")
        if not access.target_scope(principal, code).covers(legal_entity_id, pay_group_id):
            raise not_found("Pay group")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OffCycleRequest:
    kind: str
    pay_group_id: Optional[int] = None
    pay_period_id: Optional[int] = None
    reason: Optional[str] = None
    employee_ids: Tuple[int, ...] = ()
    component_codes: Tuple[str, ...] = ()
    original_run_id: Optional[int] = None
    instruction_ids: Tuple[int, ...] = ()
    loan_ids: Tuple[int, ...] = ()
    label: Optional[str] = None


def _component_rows(db: Session, company_id: int, codes: Sequence[str], on: dt.date) -> List[PayComponent]:
    from app.payroll.services import components as comp

    catalogue = comp.catalogue(db, company_id, on)
    missing = [c for c in codes if c not in catalogue]
    if missing:
        raise bad_request(f"Unknown or inactive components on {on.isoformat()}: {', '.join(missing)}.",
                          "UNKNOWN_COMPONENT")
    rows = []
    for code in codes:
        entry = catalogue[code]
        if entry.component.kind not in ("EARNING", "DEDUCTION"):
            raise bad_request(f"{code} is a {entry.component.kind.lower()} component; an off-cycle run pays earnings "
                              "or corrects deductions.", "COMPONENT_NOT_ALLOWED")
        if entry.component.owner != "COMPANY":
            raise bad_request(f"{code} belongs to a country pack and is never selected by hand.",
                              "COMPONENT_NOT_ALLOWED")
        rows.append(entry.component)
    return rows


def _earnings_run(db: Session, principal: Principal, data: OffCycleRequest, reason: str) -> PayrollRun:
    from app.payroll.services import runs

    if data.pay_group_id is None or data.pay_period_id is None:
        raise bad_request("An off-cycle run references a pay group and a regular period (for YTD).", "PERIOD_REQUIRED")
    if not data.employee_ids:
        raise bad_request("Select the employees of an off-cycle run.", "EMPLOYEES_REQUIRED")
    codes = sorted(set(c.strip().upper() for c in data.component_codes if c and c.strip()))
    if not codes:
        raise bad_request("Select the components the off-cycle run pays.", "COMPONENTS_REQUIRED")
    if data.kind == "BONUS" and data.loan_ids:
        raise bad_request("A bonus run pays no loans.", "INVALID_SELECTION")
    if principal.employee_id is not None and principal.employee_id in data.employee_ids:
        raise forbidden("You cannot create an off-cycle payment for yourself.")
    group = calendars.scoped_pay_group(db, principal, CREATE, data.pay_group_id)
    period = calendars.load_period(db, principal.company_id, data.pay_period_id)
    if period.pay_group_id != group.id:
        raise not_found("Pay period")
    components = _component_rows(db, principal.company_id, codes, period.period_end)
    if data.kind == "BONUS" and any(c.kind != "EARNING" for c in components):
        raise bad_request("A bonus run pays earnings only.", "COMPONENT_NOT_ALLOWED")
    run = runs.create_run(db, principal, runs.RunRequest(
        run_type=sm.RunType.OFF_CYCLE.value, pay_group_id=group.id, pay_period_id=period.id, label=data.label,
        employee_ids=tuple(sorted(set(data.employee_ids)))))
    run.selection = {**(run.selection or {}), "off_cycle": {
        "kind": data.kind, "payment_only": False, "components": codes, "ytd_period_id": period.id,
        "tax_year": period.tax_year, "reason": reason}}
    db.flush()
    return run


def _reissue_run(db: Session, principal: Principal, data: OffCycleRequest, reason: str) -> PayrollRun:
    from app.models.payment_flows import PaymentInstruction, PaymentObligation
    from app.payroll.services import runs

    if data.original_run_id is None or not data.instruction_ids:
        raise bad_request("A reissue run names the original run and the failed or returned payments.",
                          "INSTRUCTIONS_REQUIRED")
    original = runs.get_run_for(db, principal, data.original_run_id, READ)
    if not sm.is_run_finalized(original.status):
        raise conflict("Only payments of a finalized run are reissued.", "RUN_NOT_FINALIZED")
    ensure_can_prepare(principal, original.legal_entity_id, original.pay_group_id)
    if (data.pay_group_id not in (None, original.pay_group_id)) or (
            data.pay_period_id not in (None, original.pay_period_id)):
        raise bad_request("A reissue run uses the original run's pay group and period.", "PERIOD_MISMATCH")
    ids = sorted(set(data.instruction_ids))
    rows = (db.query(PaymentInstruction, PaymentObligation)
            .join(PaymentObligation, and_(PaymentObligation.company_id == PaymentInstruction.company_id,
                                          PaymentObligation.id == PaymentInstruction.obligation_id))
            .filter(PaymentInstruction.company_id == principal.company_id, PaymentInstruction.id.in_(ids),
                    PaymentObligation.run_id == original.id)
            .with_for_update(of=PaymentInstruction).all())
    if len(rows) != len(ids):
        raise not_found("Payment instruction")
    period = calendars.load_period(db, principal.company_id, original.pay_period_id)
    from app.payroll.services.retro import employee_visible

    for instruction, obligation in rows:
        if not employee_visible(db, principal, READ, instruction.employee_id, period.period_start, period.period_end):
            raise not_found("Payment instruction")
        if instruction.status not in REISSUABLE:
            raise conflict(f"Payment {instruction.reference or instruction.id} is {instruction.status}; only failed or "
                           "returned payments are reissued.", "NOT_REISSUABLE")
        if db.query(PaymentInstruction.id).filter(PaymentInstruction.company_id == principal.company_id,
                                                  PaymentInstruction.reissue_of_instruction_id == instruction.id).first():
            raise conflict(f"Payment {instruction.reference or instruction.id} was already reissued.",
                           "ALREADY_REISSUED")
    claimed = _claimed_instructions(db, principal.company_id, exclude_run_id=None)
    taken = sorted(set(ids) & claimed)
    if taken:
        raise conflict(f"Payments {taken} are already in another reissue run.", "ALREADY_IN_REISSUE_RUN")
    employee_ids = sorted({i.employee_id for i, _ in rows})
    if principal.employee_id is not None and principal.employee_id in employee_ids:
        raise forbidden("You cannot reissue your own payment.")
    run = runs.create_run(db, principal, runs.RunRequest(
        run_type=sm.RunType.OFF_CYCLE.value, pay_group_id=original.pay_group_id, pay_period_id=original.pay_period_id,
        label=data.label or f"Reissue · {original.label}"[:150], employee_ids=tuple(employee_ids)))
    run.selection = {**(run.selection or {}), "off_cycle": {
        "kind": "REISSUE", "payment_only": True, "original_run_id": original.id, "reason": reason,
        "ytd_period_id": original.pay_period_id,
        "instructions": [{"instruction_id": i.id, "obligation_id": o.id, "employee_id": i.employee_id,
                          "original_run_employee_id": o.run_employee_id,
                          "amount": amount_str(Decimal(i.amount), i.currency), "currency": i.currency}
                         for i, o in sorted(rows, key=lambda r: r[0].id)]}}
    db.flush()
    results: Dict[int, Prepared] = {}
    by_employee: Dict[int, list] = {i.employee_id: [] for i, _ in rows}
    for i, o in sorted(rows, key=lambda r: r[0].id):
        by_employee[i.employee_id].append((i, o))
    for row in _run_rows(db, run):
        if row.employee_id in by_employee:
            results[row.id] = Prepared(lines=[{
                "component_code": "PAYMENT_REISSUE", "kind": "INFORMATIONAL", "category": "PAYMENT", "stage": 9,
                "amount": Decimal(i.amount), "currency": i.currency,
                "period_ref": {"original_run_id": original.id, "original_run_employee_id": o.run_employee_id,
                               "instruction_id": i.id},
                "tags": [], "visibility": "EMPLOYEE_VISIBLE",
                "sources": [{"type": "payment_instruction", "id": i.id, "obligation_id": o.id}],
                "trace": {"method": "PAYMENT_ONLY"},
                "explanation": f"Reissue of payment {i.reference or i.id} ({amount_str(Decimal(i.amount), i.currency)} "
                               f"{i.currency}) from run {original.number}; no new earnings.",
            } for i, o in by_employee[row.employee_id]], flags=["PAYMENT_ONLY"])
            by_employee.pop(row.employee_id)
    return prepare_results(db, principal, run, results, source="OFF_CYCLE_REISSUE")


def _run_rows(db: Session, run: PayrollRun) -> List[PayrollRunEmployee]:
    """The run's employees, the primary placement first (one prepared result per employee)."""
    return (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id)
            .order_by(PayrollRunEmployee.status == "EXCLUDED", PayrollRunEmployee.id).all())


def _claimed_instructions(db: Session, company_id: int, *, exclude_run_id: Optional[int]) -> set:
    """Instructions named by live (not cancelled, not yet effected) reissue runs."""
    claimed = set()
    rows = db.query(PayrollRun).filter(PayrollRun.company_id == company_id,
                                       PayrollRun.run_type == sm.RunType.OFF_CYCLE.value,
                                       PayrollRun.status != sm.RunStatus.CANCELLED.value).all()
    for run in rows:
        data = off_cycle_of(run)
        if run.id == exclude_run_id or data.get("kind") != "REISSUE":
            continue
        claimed.update(int(i["instruction_id"]) for i in data.get("instructions") or ())
    return claimed


def _loan_run(db: Session, principal: Principal, data: OffCycleRequest, reason: str) -> PayrollRun:
    from app.models.variable_inputs import Loan
    from app.payroll.services import runs

    if not data.loan_ids:
        raise bad_request("Select the approved loans or advances to disburse.", "LOANS_REQUIRED")
    if data.pay_group_id is None or data.pay_period_id is None:
        raise bad_request("A disbursement run references a pay group and a period.", "PERIOD_REQUIRED")
    group = calendars.scoped_pay_group(db, principal, CREATE, data.pay_group_id)
    ensure_can_prepare(principal, group.legal_entity_id, group.id)
    period = calendars.load_period(db, principal.company_id, data.pay_period_id)
    if period.pay_group_id != group.id:
        raise not_found("Pay period")
    ids = sorted(set(data.loan_ids))
    loans = (db.query(Loan).filter(Loan.company_id == principal.company_id, Loan.id.in_(ids))
             .with_for_update(of=Loan).all())
    if len(loans) != len(ids):
        raise not_found("Loan")
    from app.payroll.services.retro import employee_visible
    for loan in loans:
        if loan.pay_group_id != group.id or not employee_visible(db, principal, CREATE, loan.employee_id,
                                                                 period.period_start, period.period_end):
            raise not_found("Loan")
        if loan.status != sm.LoanStatus.APPROVED.value or loan.disbursed_on is not None:
            raise conflict(f"Loan #{loan.id} is {loan.status.lower()}; only approved, undisbursed loans are paid.",
                           "LOAN_NOT_DISBURSABLE")
        if loan.currency != group.currency:
            raise conflict(f"Loan #{loan.id} is in {loan.currency}; the pay group pays {group.currency}.",
                           "CURRENCY_MISMATCH")
    claimed = set()
    for run in db.query(PayrollRun).filter(PayrollRun.company_id == principal.company_id,
                                           PayrollRun.run_type == sm.RunType.OFF_CYCLE.value,
                                           PayrollRun.status != sm.RunStatus.CANCELLED.value).all():
        if off_cycle_of(run).get("kind") == "LOAN_DISBURSEMENT":
            claimed.update(int(i) for i in off_cycle_of(run).get("loan_ids") or ())
    if set(ids) & claimed:
        raise conflict(f"Loans {sorted(set(ids) & claimed)} are already in a disbursement run.",
                       "ALREADY_IN_DISBURSEMENT_RUN")
    employee_ids = sorted({l.employee_id for l in loans})
    if principal.employee_id is not None and principal.employee_id in employee_ids:
        raise forbidden("You cannot disburse your own loan.")
    run = runs.create_run(db, principal, runs.RunRequest(
        run_type=sm.RunType.OFF_CYCLE.value, pay_group_id=group.id, pay_period_id=period.id,
        label=data.label or f"Loan disbursement · {group.name}"[:150], employee_ids=tuple(employee_ids)))
    run.selection = {**(run.selection or {}), "off_cycle": {
        "kind": "LOAN_DISBURSEMENT", "payment_only": True, "loan_ids": ids, "reason": reason,
        "ytd_period_id": period.id}}
    db.flush()
    results: Dict[int, Prepared] = {}
    for row in _run_rows(db, run):
        mine = [l for l in loans if l.employee_id == row.employee_id]
        if not mine:
            continue
        total = sum((Decimal(l.principal) for l in mine), ZERO)
        lines = [{"component_code": LOAN_DISBURSEMENT_CODE, "kind": "REIMBURSEMENT", "category": "LOAN_DISBURSEMENT",
                  "stage": 4, "amount": Decimal(l.principal), "currency": l.currency, "tags": [],
                  "visibility": "EMPLOYEE_VISIBLE", "sources": [{"type": "loan_disbursement", "id": l.id}],
                  "trace": {"method": "PAYMENT_ONLY", "loan_id": l.id, "kind": l.kind},
                  "explanation": f"{l.kind.title()} #{l.id} disbursed: {amount_str(Decimal(l.principal), l.currency)} "
                                 f"{l.currency} (not wages; recovered by its schedule)."}
                 for l in mine]
        results[row.id] = Prepared(reimbursements=total, net=total, lines=lines, flags=["PAYMENT_ONLY"])
        loans = [l for l in loans if l.employee_id != row.employee_id]
    return prepare_results(db, principal, run, results, source="OFF_CYCLE_LOAN_DISBURSEMENT")


def create_off_cycle_run(db: Session, principal: Principal, data: OffCycleRequest) -> PayrollRun:
    if not principal.has(CREATE):
        raise forbidden()
    if data.kind not in KINDS:
        raise bad_request(f"kind is one of {', '.join(KINDS)}.", "INVALID_KIND")
    reason = _text(data.reason)
    if reason is None:
        raise bad_request("Give the reason for the off-cycle run.", "REASON_REQUIRED")
    if data.kind in ("BONUS", "CORRECTION"):
        if data.instruction_ids or data.loan_ids or data.original_run_id:
            raise bad_request("Bonus and correction runs select employees and components only.", "INVALID_SELECTION")
        run = _earnings_run(db, principal, data, reason)
    elif data.kind == "REISSUE":
        if data.component_codes or data.loan_ids or data.employee_ids:
            raise bad_request("A reissue run names the original run and its payments only.", "INVALID_SELECTION")
        run = _reissue_run(db, principal, data, reason)
    else:
        if data.component_codes or data.instruction_ids or data.original_run_id or data.employee_ids:
            raise bad_request("A disbursement run names the loans only.", "INVALID_SELECTION")
        run = _loan_run(db, principal, data, reason)
    _audit(db, principal, OFF_CYCLE_CREATED, f"Created off-cycle run {run.number} ({data.kind.lower()})", run,
           kind=data.kind, reason=reason, selection={k: v for k, v in off_cycle_of(run).items() if k != "reason"})
    return run


# ---------------------------------------------------------------------------
# Effects of a finalized payment-only run (outbox payroll.run.finalized)
# ---------------------------------------------------------------------------
def on_run_finalized(db: Session, run: PayrollRun) -> dict:
    """REISSUE: reissue each named instruction on its obligation (skipped when already reissued or no usable
    account: reported, never guessed). LOAN_DISBURSEMENT: record the disbursements. Idempotent."""
    data = off_cycle_of(run)
    if run.run_type != sm.RunType.OFF_CYCLE.value or not data.get("payment_only") or not sm.is_run_finalized(run.status):
        return {}
    from app.payroll.services import wage_inputs

    finalizer = wage_inputs.principal_for_user(db, run.company_id, run.finalized_by_user_id)
    if data.get("kind") == "REISSUE":
        result = _reissue_effects(db, run, data, finalizer)
    else:
        result = _disbursement_effects(db, run, data, finalizer)
    _audit(db, None, OFF_CYCLE_EFFECTS, f"Payment-only run {run.number} effects applied", run, **result)
    return result


def _reissue_effects(db: Session, run: PayrollRun, data: dict, finalizer: Optional[Principal]) -> dict:
    from app.models.payment_flows import PaymentInstruction
    from app.payroll.services import obligations

    done, skipped = [], []
    for item in data.get("instructions") or ():
        row = (db.query(PaymentInstruction)
               .filter(PaymentInstruction.company_id == run.company_id,
                       PaymentInstruction.id == int(item["instruction_id"]))
               .with_for_update(of=PaymentInstruction).populate_existing().first())
        if row is None or row.status not in REISSUABLE:
            skipped.append({"instruction_id": item["instruction_id"], "reason": "NOT_REISSUABLE",
                            "status": row.status if row else None})
            continue
        if finalizer is None:
            skipped.append({"instruction_id": row.id, "reason": "FINALIZER_INACTIVE"})
            continue
        if obligations._usable_account(db, row) is None:
            skipped.append({"instruction_id": row.id, "reason": "BANK_ACCOUNT_NOT_USABLE"})
            continue
        created = obligations._perform_reissue(db, finalizer, row, f"Reissued by off-cycle run {run.number}",
                                               run.created_by_user_id)
        done.append({"instruction_id": row.id, "new_instruction_ids": [c.id for c in created]})
    return {"reissued": done, "skipped": skipped}


def _disbursement_effects(db: Session, run: PayrollRun, data: dict, finalizer: Optional[Principal]) -> dict:
    from app.core.errors import AppError
    from app.payroll.services import loans

    paid_on = (run.finalized_at or clock.utcnow()).date()
    done, skipped = [], []
    for loan_id in data.get("loan_ids") or ():
        try:
            with db.begin_nested():
                loans.record_disbursement(db, finalizer, run.company_id, int(loan_id), paid_on=paid_on,
                                          reference=f"{run.number}:loan:{loan_id}")
            done.append(int(loan_id))
        except AppError as exc:
            skipped.append({"loan_id": int(loan_id), "code": exc.code, "message": exc.detail})
    return {"disbursed": done, "skipped": skipped}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def off_cycle_json(run: PayrollRun) -> dict:
    data = off_cycle_of(run)
    return {"id": run.id, "number": run.number, "label": run.label, "status": run.status, "run_type": run.run_type,
            "pay_group_id": run.pay_group_id, "pay_period_id": run.pay_period_id, "kind": data.get("kind"),
            "payment_only": bool(data.get("payment_only")), "components": data.get("components") or [],
            "ytd_period_id": data.get("ytd_period_id"), "original_run_id": data.get("original_run_id"),
            "instruction_ids": [i["instruction_id"] for i in data.get("instructions") or ()],
            "loan_ids": data.get("loan_ids") or [], "row_version": run.row_version}


def list_off_cycle_runs(db: Session, principal: Principal, *, kind: Optional[str] = None, skip: int = 0,
                        limit: int = 50) -> dict:
    """Off-cycle runs in the caller's run scope, newest first."""
    if not principal.has(READ):
        raise forbidden()
    query = db.query(PayrollRun).filter(
        PayrollRun.company_id == principal.company_id, PayrollRun.run_type == sm.RunType.OFF_CYCLE.value,
        access.run_scope_clause(principal, READ, PayrollRun.legal_entity_id, PayrollRun.pay_group_id))
    rows = [r for r in query.order_by(PayrollRun.id.desc()).all()
            if kind is None or off_cycle_of(r).get("kind") == kind]
    return {"total": len(rows), "items": [off_cycle_json(r) for r in rows[skip:skip + limit]]}
