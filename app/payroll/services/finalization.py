"""Finalize and close pay runs (3.5 §4.1, §5.8, §5.10, §8.4, §16; decision I-15; FOLLOW-UPS F-15, F-34).

``finalize_run`` (payroll:run:finalize in the run's scope, step-up MFA, an ``Idempotency-Key`` and the
run label typed in the body) moves an APPROVED run to FINALIZED in ONE transaction, through
``runs.transition`` (row lock, ``row_version``, the RUN machine, audit RUN_FINALIZED). Guards, in order:

1. The run is APPROVED (409 RUN_FINALIZED with the correction hint when it already is, EC-RUN-04/17),
   the typed label matches, the session stepped up recently (403 STEP_UP_REQUIRED), a key is present.
2. ``runs.finalization_blockers``: not stale (EC-RUN-09), no open blocking exception, the statutory gate
   (no verified pack and no ACTIVE maker-checker attestation -> STATUTORY_NOT_VERIFIED, I-15, EC-STA-02)
   and a confirmed entity country (EC-ORG-23).
3. Every approver's own pay line was approved by someone else (EC-RUN-06); every included employee is
   calculated (an ERROR or PENDING employee must be held or excluded first).
4. Statutory lines (EC-STA-01/02/03): a non-fixture statutory line needs a VERIFIED_FOR_PERIOD pack for
   the entity and period; a fixture line needs a fixture pack outside production (never in production,
   I-07). The pack frozen in an employee's results must still be the shipped version (same fingerprint),
   otherwise the run is stale (EC-STA-03).

Effects (each exactly once; the transaction either does all of them or none), for every CALCULATED run
employee (a FULL hold keeps the employee out; a PAYMENT hold is finalized and its payment held):

* Engine side results: ``stage_outputs`` stored by the calculation (``calculation.*_of`` accessors). A run
  employee with a frozen snapshot but no stored outputs is replayed from the snapshot (``engine.replay``,
  3.5 §8.4); a different ``calc_hash`` refuses finalization (REPLAY_MISMATCH). Service-prepared employees
  (payment-only off-cycle, reversal) have no engine outputs.
* Consumption through ``payroll_line_sources`` (EC-VAR-01, EC-VAR-08): every consumable source a line
  pays (wage input, overtime entry, task, loan instalment, claim, arrears instalment, leave encashment) gets
  its row (the calculation writes them; prefixed ids such as ``overtime:12`` are parsed here) and is marked
  ``consumed``; the partial unique index makes a second consumption impossible (409
  SOURCE_ALREADY_CONSUMED). Then the owning services record it: ``wage_inputs.consume_inputs`` (unpaid
  reservations are released), ``overtime.consume_entries`` plus the comp-off credit of entries taken as time
  off (EC-OT-09, a COMP_OFF leave balance transaction), ``tasks.mark_task_shares_paid`` (one share per
  assignee), ``claims.schedule_claims`` + ``mark_claims_paid`` (claims scheduled but not paid are released), ``loans.post_instalment`` (once per
  loan and run employee), and ENCASH leave-balance transactions for leave-encashment lines. Arrears
  instalments are settled by retro's own ``payroll.run.finalized`` handler. REVERSAL runs consume nothing
  (their lines reference the reversed lines).
* YTD (``ytd_balances`` per employee, legal entity, tax year, bucket and currency; EC-STA-08/22): one bucket
  per component code (pack YTD keys from the statutory stage replace the component sum of the same name)
  and the totals ``TOTAL:GROSS``, ``TOTAL:DEDUCTIONS``, ``TOTAL:REIMBURSEMENTS``, ``TOTAL:NET``,
  ``TOTAL:EMPLOYER_COST``. A REVERSAL run negates the reversed employee's pack increments.
* Statutory liabilities (``statutory_liabilities`` per scheme, payee and currency, employee and employer
  portions apart, EC-STA-10); ``fixture`` is kept for fixture packs.
* Pack state and deduction carry-forwards stay in the finalized employee's ``stage_outputs``;
  ``carried_state`` hands them to the next snapshot (F-15), ``ytd_for_snapshot`` the YTD.
* The status FINALIZED, ``finalized_by`` / ``finalized_at``, the regular period CLOSED (by the transition).
* ONE outbox event ``payroll.run.finalized`` {"run_id"}: payment obligations, documents, journals,
  publication and corrections subscribe to it. Nothing is paid or generated inline.

``close_run`` (payroll:run:close): RECONCILED -> CLOSED (no open items but carried holds) or FINALIZED ->
CLOSED when nothing is payable. Payment states are owned by the payments services; the guard reads the
run's state and ``payment_summary`` (a hook the payments milestone may replace).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import and_, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict
from app.core.principal import Principal
from app.models.leave import LeaveType
from app.models.org import LegalEntity
from app.models.pay_runs import (
    PayPeriod,
    PayrollInputSnapshot,
    PayrollLine,
    PayrollLineSource,
    PayrollRun,
    PayrollRunEmployee,
    StatutoryLiability,
    YtdBalance,
)
from app.payroll import packs
from app.payroll import state_machines as sm
from app.payroll.money import CALC_CONTEXT, amount_str
from app.payroll.services import approvals, outbox, run_approvals, runs, step_up

FINALIZE = sm.RUN_FINALIZE
CLOSE = sm.RUN_CLOSE
FINALIZED_TOPIC = "payroll.run.finalized"
FINALIZABLE = ("CALCULATED",)
UNFINISHED = ("PENDING", "ERROR")
YTD_KINDS = ("EARNING", "DEDUCTION", "EMPLOYER_CONTRIBUTION", "REIMBURSEMENT", "BENEFIT_IN_KIND")
TOTAL_BUCKETS = (("gross", "TOTAL:GROSS"), ("deductions", "TOTAL:DEDUCTIONS"),
                 ("reimbursements", "TOTAL:REIMBURSEMENTS"), ("net", "TOTAL:NET"),
                 ("employer_cost", "TOTAL:EMPLOYER_COST"))
# Engine source types -> payroll_line_sources.source_type (as the calculation records them).
CONSUMABLE_SOURCES = {
    "wage_input": "WAGE_INPUT", "overtime_entry": "OVERTIME", "task": "TASK", "loan": "LOAN", "advance": "LOAN",
    "claim": "CLAIM", "arrears": "ARREARS", "leave_encashment": "LEAVE_ENCASHMENT",
}
LEAVE_ENCASHMENT_CATEGORY = "LEAVE_ENCASHMENT"
ZERO = Decimal(0)



def _now():
    return clock.utcnow()


def _fail(status: int, code: str, message: str) -> AppError:
    return AppError(status, message, code)


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


# ---------------------------------------------------------------------------
# Engine side results of one run employee
# ---------------------------------------------------------------------------
def _snapshot_content(db: Session, row: PayrollRunEmployee) -> Optional[dict]:
    if row.snapshot_id is None:
        return None
    content = db.query(PayrollInputSnapshot.content).filter(
        PayrollInputSnapshot.company_id == row.company_id, PayrollInputSnapshot.id == row.snapshot_id).scalar()
    return dict(content) if content is not None else None


def stage_outputs(db: Session, row: PayrollRunEmployee) -> dict:
    """The stored engine side results, or a replay of the frozen snapshot when none were stored.

    A replay that does not reproduce the stored ``calc_hash`` refuses finalization (3.5 §8.4)."""
    stored = dict(row.stage_outputs or {})
    if stored:
        if stored.get("calc_hash") not in (None, row.calc_hash):
            raise _fail(409, "STAGE_OUTPUTS_MISMATCH", f"Run employee #{row.id}: the stored results do not match "
                        "the calculation; recalculate the employee.")
        return stored
    content = _snapshot_content(db, row)
    if content is None:
        return {}
    from app.payroll import engine

    same, result = engine.replay(content, row.calc_hash or "")
    if not same:
        raise _fail(409, "REPLAY_MISMATCH", f"Run employee #{row.id}: replaying the frozen snapshot does not reproduce "
                    "the stored calculation; recalculate before finalizing.")
    outputs = dict(result.stage_outputs or {})
    replayed = {"engine_version": result.engine_version, "content_hash": result.content_hash,
                "calc_hash": result.calc_hash, "statutory": outputs.get("statutory") or {},
                "deductions": outputs.get("deductions") or {}, "pack": content.get("pack"), "replayed": True}
    row.stage_outputs = replayed
    return replayed


def _statutory(outputs: Mapping[str, Any]) -> Mapping[str, Any]:
    return outputs.get("statutory") or {}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _entity(db: Session, run: PayrollRun) -> LegalEntity:
    return db.query(LegalEntity).filter(LegalEntity.company_id == run.company_id,
                                        LegalEntity.id == run.legal_entity_id).one()


def _check_statutory_lines(db: Session, run: PayrollRun, period: PayPeriod, lines: Sequence[PayrollLine]) -> dict:
    """EC-STA-01/02, I-07: statutory lines only from a pack that is final for the entity and period."""
    statutory = [line for line in lines if line.statutory]
    status = runs.statutory_status(db, run, period, _entity(db, run))
    if not statutory:
        return status
    modes = {p["mode"] for p in status["packs"] if p["final"]}
    production = packs.environment_name(run.environment) == packs.PRODUCTION
    if any(line.fixture for line in statutory) and (production or packs.FIXTURE not in modes):
        raise _fail(409, "STATUTORY_FIXTURE_NOT_ALLOWED", "Fixture statutory lines (\"FIXTURE — not law\") never "
                    "finalize in production or without a fixture pack for the period.")
    if any(not line.fixture for line in statutory) and packs.VERIFIED not in modes:
        raise _fail(409, "STATUTORY_NOT_VERIFIED", "Statutory lines need a country pack verified for the period; "
                    "an attestation covers statutory obligations handled outside this system, not these lines.")
    return status


def _check_pack_versions(run: PayrollRun, outputs: Mapping[int, Mapping[str, Any]]) -> List[dict]:
    """EC-STA-03: the pack frozen in each result must still be the shipped version (same fingerprint)."""
    env = packs.environment_name(run.environment)
    seen: Dict[Tuple[str, str], dict] = {}
    for data in outputs.values():
        ref = data.get("pack") or {}
        code, version = ref.get("code"), ref.get("version")
        if not code or not version:
            continue
        seen[(code, version)] = ref
    changed = []
    for (code, version), ref in sorted(seen.items()):
        current = packs.find_pack(code, version, environment=env)
        if current is None or (ref.get("fingerprint") and current.fingerprint() != ref.get("fingerprint")):
            changed.append({"pack": code, "version": version})
    if changed:
        raise _fail(409, "RUN_STALE", "The country pack changed after calculation (" + ", ".join(
            f"{c['pack']} {c['version']}" for c in changed) + "); recalculate first.")
    return [{"pack": code, "version": version} for code, version in sorted(seen)]


# ---------------------------------------------------------------------------
# Consumption through payroll_line_sources (EC-VAR-01, EC-VAR-08)
# ---------------------------------------------------------------------------
def _ident(value: Any) -> Optional[int]:
    """``12``, ``"12"`` or a prefixed id such as ``"overtime:12"``."""
    text = str(value if value is not None else "").strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number > 0 else None


def parse_sources(line: PayrollLine, employee_id: int) -> List[Tuple[str, int, str, dict]]:
    """(source_type, source_id, source_key, raw) of the consumable records a line pays."""
    found = []
    for raw in line.sources or ():
        if not isinstance(raw, Mapping):
            continue
        kind = CONSUMABLE_SOURCES.get(str(raw.get("type") or ""))
        source_id = _ident(raw.get("id"))
        if kind is None or source_id is None:
            continue
        if kind == "LOAN" and line.kind != "DEDUCTION":
            continue  # only recoveries consume an instalment
        key = ""
        number = raw.get("instalment_number", raw.get("instalment"))
        if kind == "LOAN" and number is not None:
            key = f"instalment:{number}"
        elif kind == "TASK":
            key = f"employee:{employee_id}"   # a task shared by assignees is paid once per assignee
        found.append((kind, source_id, key[:80], dict(raw)))
    return found


@dataclass
class _Consumed:
    """Consumed source ids per run employee and type, with the paying lines."""

    by_row: Dict[int, Dict[str, Set[int]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(set)))
    loans: Dict[Tuple[int, int], Decimal] = field(default_factory=dict)      # (row id, loan id) -> amount
    loan_due: Dict[Tuple[int, int], Optional[Decimal]] = field(default_factory=dict)
    encashments: List[Tuple[PayrollRunEmployee, PayrollLine, dict]] = field(default_factory=list)
    count: int = 0


def _consume_sources(db: Session, run: PayrollRun, rows: Sequence[PayrollRunEmployee],
                     lines_by_row: Mapping[int, Sequence[PayrollLine]]) -> _Consumed:
    cid = run.company_id
    result = _Consumed()
    existing: Dict[Tuple[int, str, int], PayrollLineSource] = {}
    for src in db.query(PayrollLineSource).filter(PayrollLineSource.company_id == cid,
                                                  PayrollLineSource.run_id == run.id).with_for_update():
        existing.setdefault((src.line_id, src.source_type, src.source_id), src)
    wanted: List[PayrollLineSource] = []
    for row in rows:
        for line in lines_by_row.get(row.id, ()):
            for kind, source_id, key, raw in parse_sources(line, row.employee_id):
                src = existing.get((line.id, kind, source_id))
                if src is not None and kind == "TASK" and not src.consumed and src.source_key != key:
                    src.source_key = key   # a task line calculated before per-assignee keys (EC-TSK-*, EC-VAR-01)
                if src is None:
                    src = PayrollLineSource(company_id=cid, line_id=line.id, run_id=run.id, run_employee_id=row.id,
                                            source_type=kind, source_id=source_id, source_key=key, consumed=False)
                    db.add(src)
                    existing[(line.id, kind, source_id)] = src
                wanted.append(src)
                result.by_row[row.id][kind].add(source_id)
                if kind == "LOAN":
                    pair = (row.id, source_id)
                    result.loans[pair] = result.loans.get(pair, ZERO) + Decimal(line.amount)
                    due = _dec(raw.get("scheduled")) or _dec((line.trace or {}).get("due"))
                    result.loan_due[pair] = due
                if kind == "LEAVE_ENCASHMENT":
                    result.encashments.append((row, line, raw))
    db.flush()
    now = _now()
    taken: Set[Tuple[str, int, str]] = set()
    for src in sorted(wanted, key=lambda s: s.id):
        identity = (src.source_type, src.source_id, src.source_key)
        if identity in taken or src.consumed:
            taken.add(identity)
            continue
        other = db.query(PayrollLineSource.run_id).filter(
            PayrollLineSource.company_id == cid, PayrollLineSource.source_type == src.source_type,
            PayrollLineSource.source_id == src.source_id, PayrollLineSource.source_key == src.source_key,
            PayrollLineSource.consumed.is_(True), PayrollLineSource.id != src.id).first()
        if other is not None:
            raise _fail(409, "SOURCE_ALREADY_CONSUMED", f"{src.source_type} #{src.source_id} was already paid by run "
                        f"#{other[0]}; it is paid exactly once.")
        src.consumed, src.consumed_at = True, now
        taken.add(identity)
        result.count += 1
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise _fail(409, "SOURCE_ALREADY_CONSUMED", "A source of this run was paid by another run meanwhile.")
    return result


def _check_frozen_loans(db: Session, run: PayrollRun, period: PayPeriod, rows: Sequence[PayrollRunEmployee]) -> int:
    """EC-RUN-09 for loans and advances: their changes are not in the source change log, so a loan prepaid,
    rescheduled, paused, closed or cancelled after the inputs were locked would otherwise be recovered at the
    frozen instalment. Every loan a snapshot froze must still have the version it froze (``loans`` version hash,
    the snapshot's ``loan:<id>`` source hash); otherwise the run is stale (409 RUN_STALE, recalculate)."""
    from app.payroll.services import loans as loan_service

    frozen: Dict[int, Tuple[PayrollRunEmployee, Dict[str, str]]] = {}
    ids = [r.snapshot_id for r in rows if r.snapshot_id is not None]
    hashes = dict(db.query(PayrollInputSnapshot.id, PayrollInputSnapshot.source_hashes).filter(
        PayrollInputSnapshot.company_id == run.company_id, PayrollInputSnapshot.id.in_(ids or [0])).all())
    for row in rows:
        loans = {k: v for k, v in (hashes.get(row.snapshot_id) or {}).items() if k.startswith("loan:")}
        if loans:
            frozen[row.id] = (row, loans)
    changed: List[str] = []
    for row, loans in frozen.values():
        current = {f"loan:{item['loan_id']}": item.get("version_hash") for item in loan_service.loan_instalments_due(
            db, run.company_id, row.employee_id, period.period_start, period.period_end)}
        changed.extend(f"{key} (employee #{row.employee_id})" for key, digest in sorted(loans.items())
                       if current.get(key) != digest)
    if changed:
        raise _fail(409, "RUN_STALE", "A loan or advance changed after the inputs were locked (" + ", ".join(
            changed[:10]) + "); recalculate the employees before finalizing.")
    return len(frozen)


def _comp_off_credits(content: Mapping[str, Any]) -> List[Tuple[int, date, Decimal, Decimal]]:
    """(entry id, date, credit hours, standard hours per day) of overtime taken as time off (EC-OT-09),
    re-derived from the frozen snapshot only."""
    if not any((e or {}).get("comp_off") for e in content.get("overtime_entries") or ()):
        return []
    from app.payroll.engine.context import build_context
    from app.payroll.engine.earnings import standard_hours
    from app.payroll.engine.pipeline import payable_units
    from app.payroll.engine.snapshot import load

    ctx = build_context(load(content))
    units = payable_units(ctx, [])
    overtime = getattr(units, "overtime", None) if units is not None else None
    if overtime is None:
        return []
    per_day = standard_hours(units)
    found = []
    for credit in overtime.comp_off:
        entry_id = _ident(credit.entry_id)
        if entry_id is not None and credit.credit_hours > 0:
            found.append((entry_id, credit.date, credit.credit_hours, per_day))
    return found


def _comp_off_type(db: Session, company_id: int) -> str:
    code = (db.query(LeaveType.code).filter(LeaveType.company_id == company_id, LeaveType.kind == "COMP_OFF",
                                            LeaveType.is_active.is_(True))
            .order_by(LeaveType.id).limit(1).scalar())
    if code is None:
        raise _fail(409, "COMP_OFF_TYPE_MISSING", "Overtime taken as time off needs an active COMP_OFF leave type to "
                    "credit; create one before finalizing.")
    return code


def _days(hours: Decimal, per_day: Decimal) -> Decimal:
    if not per_day or per_day <= 0:
        raise _fail(409, "COMP_OFF_NOT_CONVERTIBLE", "The standard hours per day are unknown; comp-off cannot be credited.")
    with localcontext(CALC_CONTEXT):
        return (hours / per_day).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _encashment_target(db: Session, row: PayrollRunEmployee, line: PayrollLine, raw: Mapping[str, Any]
                       ) -> Tuple[str, Decimal]:
    leave_type = raw.get("leave_type")
    days = _dec(raw.get("days")) or _dec(line.units)
    if leave_type and days:
        return str(leave_type), days
    raise _fail(409, "ENCASHMENT_UNRESOLVED", f"The leave-encashment line {line.component_code} of employee "
                f"#{row.employee_id} does not name its leave type and days.")


def _final_settlement_encashments(db: Session, row: PayrollRunEmployee, wage_input_ids: Iterable[int]
                                  ) -> List[Tuple[str, Decimal, int]]:
    """Final-settlement encashments are paid through wage inputs: (leave type, days, item id)."""
    from app.models.corrections import FinalSettlementItem
    from app.models.variable_inputs import WageInput

    ids = sorted(set(wage_input_ids))
    if not ids:
        return []
    items = (db.query(FinalSettlementItem.source_key, FinalSettlementItem.units, FinalSettlementItem.id)
             .join(WageInput, and_(WageInput.company_id == FinalSettlementItem.company_id,
                                   WageInput.id == FinalSettlementItem.wage_input_id))
             .filter(FinalSettlementItem.company_id == row.company_id, FinalSettlementItem.wage_input_id.in_(ids),
                     FinalSettlementItem.item_type == "LEAVE_ENCASHMENT").all())
    return [(code, Decimal(units), item_id) for code, units, item_id in items if code and units]


def _apply_consumption(db: Session, run: PayrollRun, period: PayPeriod, rows: Sequence[PayrollRunEmployee],
                       lines_by_row: Mapping[int, Sequence[PayrollLine]], consumed: _Consumed,
                       actor_user_id: Optional[int]) -> dict:
    from app.payroll.services import claims as claims_service
    from app.payroll.services import loans as loan_service
    from app.payroll.services import overtime as overtime_service
    from app.payroll.services import tasks as task_service
    from app.payroll.services import time_inputs
    from app.payroll.services import wage_inputs as wage_input_service

    cid = run.company_id
    done = defaultdict(int)
    tasks: Set[Tuple[int, int]] = set()   # (task id, employee id): each assignee's share is paid once
    comp_off_type: Optional[str] = None
    for row in rows:
        kinds = consumed.by_row.get(row.id, {})
        if kinds.get("WAGE_INPUT"):
            done["wage_inputs"] += wage_input_service.consume_inputs(db, cid, row.id, sorted(kinds["WAGE_INPUT"]))
        entries = set(kinds.get("OVERTIME", ()))
        content = _snapshot_content(db, row)
        credits = _comp_off_credits(content) if content else []
        for entry_id, day, hours, per_day in credits:
            comp_off_type = comp_off_type or _comp_off_type(db, cid)
            if time_inputs._transaction(db, cid, employee_id=row.employee_id, leave_type=comp_off_type,
                                        delta=_days(hours, per_day), reason="ACCRUAL",
                                        key=f"payroll:comp_off:{entry_id}", effective_date=day,
                                        ref_type="overtime_entry", ref_id=entry_id, actor_user_id=actor_user_id,
                                        note=f"Comp-off for overtime, run {run.number}"):
                done["comp_off_credits"] += 1
            entries.add(entry_id)
        if entries:
            done["overtime_entries"] += overtime_service.consume_entries(db, cid, sorted(entries), row.id)
        tasks.update((task_id, row.employee_id) for task_id in kinds.get("TASK", ()))
        if kinds.get("CLAIM"):
            ids = sorted(kinds["CLAIM"])
            claims_service.schedule_claims(db, cid, ids, runs.claims_ref(row.id))
            done["claims_paid"] += claims_service.mark_claims_paid(db, cid, ids, f"run:{run.number}")
        for (row_id, loan_id), amount in sorted(consumed.loans.items()):
            if row_id != row.id:
                continue
            loan_service.post_instalment(db, cid, loan_id, row.id, amount,
                                         scheduled=consumed.loan_due.get((row_id, loan_id)),
                                         effective_on=period.pay_date)
            done["loan_instalments"] += 1
        encashed: List[Tuple[str, Decimal, str]] = []
        for owner, line, raw in consumed.encashments:
            if owner.id == row.id:
                code, days = _encashment_target(db, row, line, raw)
                encashed.append((code, days, f"line:{line.id}"))
        settlement_lines = [line for line in lines_by_row.get(row.id, ()) if line.category == LEAVE_ENCASHMENT_CATEGORY
                            and not any(s[0] == "LEAVE_ENCASHMENT" for s in parse_sources(line, row.employee_id))]
        if settlement_lines:
            paid_inputs = {sid for line in settlement_lines for kind, sid, _, _ in parse_sources(line, row.employee_id)
                           if kind == "WAGE_INPUT"}
            found = _final_settlement_encashments(db, row, paid_inputs)
            # Leave v2 encashment requests paid through their system wage input (LH SPINE §5.4: PAID once).
            from app.services import leave_encashment

            requested = leave_encashment.paid_requests_for_inputs(db, cid, paid_inputs)
            if not found and not requested:
                raise _fail(409, "ENCASHMENT_UNRESOLVED", f"The leave-encashment line of employee #{row.employee_id} "
                            "does not name its leave type and days.")
            encashed.extend((code, days, f"fs_item:{item_id}") for code, days, item_id in found)
            encashed.extend(requested)
        for code, days, ref in encashed:
            if time_inputs._transaction(db, cid, employee_id=row.employee_id, leave_type=code, delta=-abs(days),
                                        reason="ENCASH", key=f"payroll:encash:{row.id}:{code}:{ref}"[:120],
                                        effective_date=period.pay_date, ref_type="payroll_run_employee",
                                        ref_id=row.id, actor_user_id=actor_user_id,
                                        note=f"Encashed in run {run.number}"):
                done["leave_encashments"] += 1
    if tasks:
        done["tasks_paid"] = task_service.mark_task_shares_paid(db, cid, sorted(tasks), run.id)
    every = runs._run_employee_ids(db, run)
    done["wage_inputs_released"] = wage_input_service.release_inputs(db, cid, every)
    done["claims_released"] = sum(claims_service.release_claims(db, cid, runs.claims_ref(rid)) for rid in every)
    return dict(done)


# ---------------------------------------------------------------------------
# YTD and statutory liabilities
# ---------------------------------------------------------------------------
def _original_outputs(db: Session, run: PayrollRun) -> Dict[Tuple[int, int], dict]:
    """Stored results of the reversed run's employees, keyed by (employee, assignment)."""
    if run.reverses_run_id is None:
        return {}
    rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                               PayrollRunEmployee.run_id == run.reverses_run_id,
                                               PayrollRunEmployee.status == "CALCULATED").all()
    return {(r.employee_id, r.employee_assignment_id): dict(r.stage_outputs or {}) for r in rows}


def _negated(values: Mapping[str, Any]) -> Dict[str, str]:
    return {k: format(-Decimal(str(v)), "f") for k, v in values.items() if _dec(v) is not None}


def _ytd_rows(run: PayrollRun, period: PayPeriod, row: PayrollRunEmployee, lines: Sequence[PayrollLine],
              increments: Mapping[str, Any]) -> List[dict]:
    buckets: Dict[str, Decimal] = {}
    component_ids: Dict[str, Optional[int]] = {}
    with localcontext(CALC_CONTEXT):
        for line in lines:
            if line.kind not in YTD_KINDS:
                continue
            buckets[line.component_code] = buckets.get(line.component_code, ZERO) + Decimal(line.amount)
            component_ids.setdefault(line.component_code, line.component_id)
        for key, value in increments.items():
            amount = _dec(value)
            if amount is not None:
                buckets[str(key)[:60]] = amount       # the pack's own YTD key wins over a component sum
        for attr, bucket in TOTAL_BUCKETS:
            value = getattr(row, attr)
            if value is not None:
                buckets[bucket] = Decimal(value)
    return [{"company_id": run.company_id, "employee_id": row.employee_id, "legal_entity_id": run.legal_entity_id,
             "tax_year": period.tax_year, "bucket": bucket, "component_id": component_ids.get(bucket),
             "amount": amount, "currency": row.currency, "last_run_id": run.id, "row_version": 1}
            for bucket, amount in sorted(buckets.items()) if amount != 0]


def _write_ytd(db: Session, values: List[dict]) -> int:
    """Add the increments (one row per key: concurrent assignments of one employee are summed first)."""
    merged: Dict[Tuple[int, int, str, str, str], dict] = {}
    for value in values:
        key = (value["employee_id"], value["legal_entity_id"], value["tax_year"], value["bucket"], value["currency"])
        if key in merged:
            merged[key]["amount"] += value["amount"]
            merged[key]["component_id"] = merged[key]["component_id"] or value["component_id"]
        else:
            merged[key] = dict(value)
    values = [v for v in merged.values() if v["amount"] != 0]
    if not values:
        return 0
    now = _now()
    statement = insert(YtdBalance).values([{**v, "created_at": now, "updated_at": now} for v in values])
    statement = statement.on_conflict_do_update(
        constraint="uq_ytd_balances_key",
        set_={"amount": YtdBalance.amount + statement.excluded.amount, "last_run_id": statement.excluded.last_run_id,
              "row_version": YtdBalance.row_version + 1, "updated_at": now})
    db.execute(statement)
    return len(values)


def _liability_rows(run: PayrollRun, items: Iterable[Tuple[Mapping[str, Any], bool]]) -> List[dict]:
    grouped: Dict[Tuple[str, str, str], dict] = {}
    for item, fixture in items:
        amount = _dec(item.get("amount"))
        scheme, payee, currency = item.get("scheme"), item.get("payee"), item.get("currency") or run.currency
        if amount is None or amount == 0 or not scheme or not payee:
            continue
        key = (str(scheme)[:40], str(payee)[:150], str(currency))
        entry = grouped.setdefault(key, {"employee": ZERO, "employer": ZERO, "due": None, "components": set(),
                                         "fixture": False, "count": 0})
        portion = str(item.get("portion") or "EMPLOYEE").upper()
        entry["employer" if portion == "EMPLOYER" else "employee"] += amount
        entry["components"].add(str(item.get("component") or ""))
        entry["fixture"] = entry["fixture"] or fixture
        entry["count"] += 1
        due = item.get("due_date")
        if due and (entry["due"] is None or due < entry["due"]):
            entry["due"] = due
    return [{"company_id": run.company_id, "run_id": run.id, "legal_entity_id": run.legal_entity_id,
             "scheme": scheme, "payee_authority": payee, "currency": currency,
             "employee_amount": e["employee"], "employer_amount": e["employer"],
             "due_date": date.fromisoformat(e["due"]) if e["due"] else None, "status": "OPEN",
             "fixture": e["fixture"], "row_version": 1,
             "details": {"components": sorted(c for c in e["components"] if c), "items": e["count"],
                         "fixture_label": "FIXTURE — not law" if e["fixture"] else None}}
            for (scheme, payee, currency), e in sorted(grouped.items())]


def _write_liabilities(db: Session, values: List[dict]) -> int:
    if not values:
        return 0
    now = _now()
    statement = insert(StatutoryLiability).values([{**v, "created_at": now, "updated_at": now} for v in values])
    statement = statement.on_conflict_do_update(
        constraint="uq_statutory_liabilities_key",
        set_={"employee_amount": StatutoryLiability.employee_amount + statement.excluded.employee_amount,
              "employer_amount": StatutoryLiability.employer_amount + statement.excluded.employer_amount,
              "row_version": StatutoryLiability.row_version + 1, "updated_at": now})
    db.execute(statement)
    return len(values)


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------
def _stale_other_runs(db: Session, run: PayrollRun, rows: Sequence[PayrollRunEmployee]) -> int:
    """EC-RUN-09: this finalization changed what other runs froze for the same employees in the legal entity
    (their YTD balances, previous finalized period, pack state and carry-forwards, loan balances). Those runs
    were locked before and are marked stale (``runs.mark_stale``, the one stale writer; this run is FINALIZED
    already, so it is not among them): they recalculate the employees before submission or finalization
    instead of paying from out-of-date year-to-date figures. ``employee_ids`` lets a recalculation of just
    these employees clear the reason."""
    employee_ids = sorted({r.employee_id for r in rows})
    if not employee_ids:
        return 0
    return runs.mark_stale(
        db, run.company_id, f"Run {run.number} was finalized for {len(employee_ids)} of this run's employee(s): "
        "their year-to-date figures changed after these inputs were locked.",
        legal_entity_id=run.legal_entity_id, employee_ids=employee_ids,
        details={"source": "finalization", "finalized_run_id": run.id, "employee_ids": employee_ids})


def _ensure_action(run: PayrollRun, action: str) -> None:
    try:
        sm.transition_spec(sm.RUN, run.status, action)
    except sm.InvalidTransition as exc:
        raise runs.transition_error(exc)


def finalize_run(db: Session, principal: Principal, run_id: int, *, confirm_label: str,
                 idempotency_key: Optional[str], expected_version: Optional[int] = None) -> dict:
    """APPROVED -> FINALIZED with every side effect in this transaction (3.5 §5.8; see the module docstring)."""
    run = runs.get_run_for(db, principal, run_id, FINALIZE, lock=True)
    _ensure_action(run, "FINALIZE")
    if expected_version is not None and run.row_version != expected_version:
        raise conflict("The run was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    if (confirm_label or "").strip() != run.label:
        raise bad_request("Type the run label exactly to confirm finalization.", "CONFIRMATION_MISMATCH")
    step_up.ensure_step_up(db, principal)
    if not idempotency_key:
        raise bad_request("Finalization needs an Idempotency-Key header, so a retry never finalizes twice.",
                          "IDEMPOTENCY_KEY_REQUIRED")
    blockers = runs.finalization_blockers(db, run)
    if blockers:
        raise _fail(409, blockers[0]["code"], " ".join(b["message"] for b in blockers))
    pending_lines = run_approvals.self_lines_pending(db, run)
    if pending_lines:
        raise _fail(409, "SELF_LINE_APPROVAL_PENDING", f"{len(pending_lines)} approver's own pay line(s) still need "
                    "the approval of another approver.")
    request = approvals.latest_request(db, run_approvals.MODULE, run_approvals.SUBJECT, run.id)
    if run.approval_request_id is not None and (request is None or request.status != approvals.APPROVED):
        raise _fail(409, "APPROVAL_INCOMPLETE", "The approval chain of this run is not complete.")
    # The full change-detection pass before anything is paid (change_detection.gate_full_check): a source changed
    # since the inputs were locked marks the run stale and raises 409 RUN_STALE.
    from app.payroll.services import change_detection
    change_detection.gate_full_check(db, run, "finalization")

    cid = run.company_id
    period = runs.period_of(db, run)
    all_rows = (db.query(PayrollRunEmployee)
                .filter(PayrollRunEmployee.company_id == cid, PayrollRunEmployee.run_id == run.id)
                .order_by(PayrollRunEmployee.id).with_for_update(of=PayrollRunEmployee).populate_existing().all())
    unfinished = [r.id for r in all_rows if r.status in UNFINISHED]
    if unfinished:
        raise _fail(409, "EMPLOYEES_NOT_CALCULATED", f"{len(unfinished)} employee(s) are not calculated; hold or "
                    "exclude them, or recalculate.")
    rows = [r for r in all_rows if r.status in FINALIZABLE]
    lines = (db.query(PayrollLine)
             .filter(PayrollLine.company_id == cid, PayrollLine.run_id == run.id,
                     PayrollLine.run_employee_id.in_([r.id for r in rows] or [0]))
             .order_by(PayrollLine.run_employee_id, PayrollLine.sort_order, PayrollLine.id).all())
    lines_by_row: Dict[int, List[PayrollLine]] = defaultdict(list)
    for line in lines:
        lines_by_row[line.run_employee_id].append(line)

    outputs = {row.id: stage_outputs(db, row) for row in rows}
    statutory = _check_statutory_lines(db, run, period, lines)
    pack_versions = _check_pack_versions(run, outputs)

    effects: Dict[str, Any] = {"employees": len(rows), "held_full": sum(1 for r in all_rows if r.status == "ON_HOLD"),
                               "payment_holds": sum(1 for r in rows if r.hold_type == "PAYMENT"),
                               "packs": pack_versions, "statutory_verified": statutory["verified"]}
    if run.run_type != sm.RunType.REVERSAL.value:
        _check_frozen_loans(db, run, period, rows)
        consumed = _consume_sources(db, run, rows, lines_by_row)
        effects["sources_consumed"] = consumed.count
        effects.update(_apply_consumption(db, run, period, rows, lines_by_row, consumed, principal.user_id))

    originals = _original_outputs(db, run)
    ytd_values: List[dict] = []
    liabilities: List[Tuple[Mapping[str, Any], bool]] = []
    fixture_run = any(line.fixture for line in lines if line.statutory)
    for row in rows:
        data = outputs[row.id]
        increments = _statutory(data).get("ytd_increments") or {}
        items = list(_statutory(data).get("liabilities") or [])
        if run.run_type == sm.RunType.REVERSAL.value and not data:
            original = originals.get((row.employee_id, row.employee_assignment_id), {})
            increments = _negated(_statutory(original).get("ytd_increments") or {})
            items = [{**item, "amount": format(-Decimal(str(item["amount"])), "f")}
                     for item in _statutory(original).get("liabilities") or [] if _dec(item.get("amount")) is not None]
        pack_fixture = bool((data.get("pack") or {}).get("is_fixture")) or fixture_run
        liabilities.extend((item, pack_fixture) for item in items)
        ytd_values.extend(_ytd_rows(run, period, row, lines_by_row.get(row.id, ()), increments))
    effects["ytd_buckets"] = _write_ytd(db, ytd_values)
    effects["liabilities"] = _write_liabilities(db, _liability_rows(run, liabilities))
    db.flush()

    run = runs.transition(db, principal, run, "FINALIZE", STEP_UP_MFA=True, STATUTORY_PACK_VERIFIED=True,
                          IDEMPOTENCY_KEY=True, details={"effects": effects})
    _stale_other_runs(db, run, rows)
    event = outbox.emit(db, FINALIZED_TOPIC, {"run_id": run.id})
    return {"run": run, "effects": effects, "outbox_event_id": event.id}


# ---------------------------------------------------------------------------
# Close (3.5 §5.10)
# ---------------------------------------------------------------------------
def _default_payment_summary(db: Session, run: PayrollRun) -> dict:
    """The run's payment position from its own state: a RECONCILED run has no open item but carried holds; a
    FINALIZED run has something to pay while a calculated employee without a hold has a positive net."""
    payable = db.query(func.count(PayrollRunEmployee.id)).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.status == "CALCULATED", PayrollRunEmployee.hold_type.is_(None),
        PayrollRunEmployee.net > 0).scalar()
    return {"status": run.status, "payable_employees": payable,
            "open_items": 0 if run.status == sm.RunStatus.RECONCILED.value else None}


# Hook: the payments milestone may replace it with the instruction-based summary (same keys).
payment_summary = _default_payment_summary


CARRIED_HOLD_AUDIT_FIELDS = ("run_employee_id", "employee_id", "reason_code")
# The payment summary keys the CLOSE audit keeps: counts and states only. ``payment_summary`` is a hook the
# payments milestone may replace; an amount it adds never reaches the audit (no salary data in logs).
PAYMENT_SUMMARY_AUDIT_FIELDS = ("status", "payable_employees", "open_items")


def carried_holds(db: Session, run: PayrollRun) -> List[dict]:
    rows = db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.hold_type == "PAYMENT").order_by(PayrollRunEmployee.id).all()
    return [{"run_employee_id": r.id, "employee_id": r.employee_id,
             "reason_code": (r.hold or {}).get("reason_code"),
             "net": amount_str(Decimal(r.net), r.currency) if r.net is not None else None} for r in rows]


def close_run(db: Session, principal: Principal, run_id: int, *, note: Optional[str] = None,
              expected_version: Optional[int] = None) -> dict:
    """RECONCILED -> CLOSED (or FINALIZED -> CLOSED when nothing is payable); holds are carried (3.5 §9)."""
    run = runs.get_run_for(db, principal, run_id, CLOSE, lock=True)
    _ensure_action(run, "CLOSE")
    if expected_version is not None and run.row_version != expected_version:
        raise conflict("The run was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    summary = payment_summary(db, run)
    holds = carried_holds(db, run)
    if run.status == sm.RunStatus.RECONCILED.value:
        clear = summary.get("open_items") == 0
        if not clear:
            raise _fail(409, "OPEN_PAYMENT_ITEMS", "Payments of this run still have open items.")
        guards = {"NO_OPEN_ITEMS_EXCEPT_HOLDS": True}
    else:
        if summary.get("payable_employees"):
            raise _fail(409, "PAYMENTS_OPEN", "This run still has payments to make; it closes once they are "
                        "reconciled.")
        guards = {"NOTHING_TO_PAY": True}
    # The audit keeps ids and reason codes only: no salary data in logs or job payloads (the net amounts
    # stay in the run employees and the caller's response).
    audited = [{k: h.get(k) for k in CARRIED_HOLD_AUDIT_FIELDS} for h in holds]
    audited_summary = {k: summary.get(k) for k in PAYMENT_SUMMARY_AUDIT_FIELDS}
    run = runs.transition(db, principal, run, "CLOSE", reason=(note or "").strip() or None,
                          details={"carried_holds": audited, "payment_summary": audited_summary}, **guards)
    return {"run": run, "carried_holds": holds}


# ---------------------------------------------------------------------------
# Reads for the next snapshot (F-15) and the run views
# ---------------------------------------------------------------------------
def carried_state(db: Session, company_id: int, employee_id: int, legal_entity_id: int, before: date) -> dict:
    """Pack state and deduction carry-forwards of the employee's latest finalized run employee in the legal
    entity whose period ends before ``before`` (the next snapshot's ``pack_state``, EC-STA-17)."""
    row = (db.query(PayrollRunEmployee, PayrollRun.id, PayPeriod.period_end)
           .join(PayrollRun, and_(PayrollRun.company_id == PayrollRunEmployee.company_id,
                                  PayrollRun.id == PayrollRunEmployee.run_id))
           .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id,
                                 PayPeriod.id == PayrollRun.pay_period_id))
           .filter(PayrollRunEmployee.company_id == company_id, PayrollRunEmployee.employee_id == employee_id,
                   PayrollRunEmployee.status == "CALCULATED", PayrollRun.legal_entity_id == legal_entity_id,
                   PayrollRun.status.in_(sm.RUN_FINALIZED_STATES), PayrollRun.run_type != sm.RunType.REVERSAL.value,
                   PayPeriod.period_end < before)
           .order_by(PayPeriod.period_end.desc(), PayrollRun.finalized_at.desc(), PayrollRunEmployee.id.desc())
           .first())
    if row is None:
        return {"pack_state": {}, "carry_forwards": [], "run_id": None, "run_employee_id": None, "period_end": None}
    employee, run_id, period_end = row
    data = dict(employee.stage_outputs or {})
    return {"pack_state": dict(_statutory(data).get("pack_state") or {}),
            "carry_forwards": list((data.get("deductions") or {}).get("carry_forwards") or []),
            "run_id": run_id, "run_employee_id": employee.id, "period_end": period_end.isoformat()}


def ytd_for_snapshot(db: Session, company_id: int, employee_id: int, legal_entity_id: int, tax_year: str,
                     currency: str) -> Dict[str, str]:
    """{bucket: decimal string} of the employee's YTD in the entity and tax year (the snapshot's ``ytd``)."""
    rows = db.query(YtdBalance.bucket, YtdBalance.amount).filter(
        YtdBalance.company_id == company_id, YtdBalance.employee_id == employee_id,
        YtdBalance.legal_entity_id == legal_entity_id, YtdBalance.tax_year == tax_year,
        YtdBalance.currency == currency).order_by(YtdBalance.bucket).all()
    return {bucket: amount_str(Decimal(amount), currency) for bucket, amount in rows}


def finalization_status(db: Session, principal: Principal, run_id: int) -> dict:
    """What still stands between the run and finalization (the finalize dialog's checklist)."""
    run = runs.get_run_for(db, principal, run_id, runs.READ)
    period = runs.period_of(db, run)
    blockers = [] if sm.is_run_finalized(run.status) else list(runs.finalization_blockers(db, run))
    pending = run_approvals.self_lines_pending(db, run)
    visible = {rid for (rid,) in db.query(PayrollRunEmployee.id).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        runs.employee_scope_clause(db, principal, runs.READ, run, PayrollRunEmployee.employee_id))}
    return {
        "run_id": run.id, "status": run.status, "label": run.label, "row_version": run.row_version,
        "can_finalize": run.status == sm.RunStatus.APPROVED.value and not blockers and not pending,
        "blockers": [{k: v for k, v in b.items() if k != "details"} for b in blockers],
        "self_lines_pending": [p for p in pending if p["run_employee_id"] in visible],
        "statutory": runs.statutory_status(db, run, period, _entity(db, run)),
        "finalized_at": run.finalized_at.isoformat() if run.finalized_at else None,
        "finalized_by_user_id": run.finalized_by_user_id,
    }

