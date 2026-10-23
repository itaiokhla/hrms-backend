"""Lock inputs and calculate a pay run as background jobs (3.5 §4.1, §5.3-§5.4, §7-§8, §10, §16; 3.9 §6).

* ``calculate(db, principal, run_id, *, employee_ids=None, idempotency_key=None, expected_version=None)``
  (``payroll:run:calculate`` in the run's scope): READY -> CALCULATING (action CALCULATE), or
  VALIDATION_FAILED / REVIEW_REQUIRED / REJECTED -> CALCULATING (RECALCULATE, all or the selected
  employees). In ONE transaction it moves the run through ``runs.transition`` (row lock, row_version,
  guards IDEMPOTENCY_KEY and NO_CALCULATION_RUNNING, audit INPUTS_LOCKED), freezes the inputs of the
  employees to calculate with ``snapshot.lock_inputs`` (LOCK_INPUTS / RESNAPSHOT_SELECTED) and enqueues
  the parent job ``payroll.calculate_run`` (ENQUEUE_CALCULATION). A repeat with the same Idempotency-Key
  replays the first response (route level) and the job's idempotency key is bound to the run's row
  version, so a calculation round has exactly one job (EC-RUN-02). Payment-only off-cycle and reversal
  runs are prepared by their services and never calculated here.
* ``payroll.calculate_run`` splits the employees into chunks of ``CHUNK_SIZE`` (200) and enqueues one
  ``payroll.calculate_chunk`` child per chunk. A chunk calculates each employee from its frozen snapshot
  only (``engine.calculate``; nothing live is read, 3.5 §8.2) inside a savepoint, so an error is
  isolated to that employee (status ERROR + a BLOCKING CALCULATION_ERROR exception, EC-RUN-14). For each
  employee it replaces the lines (and their consumable ``payroll_line_sources``) atomically, stores the
  totals, ``content_hash``, ``calc_hash``, the engine's side results (``stage_outputs``: statutory
  liabilities, YTD increments, pack state, deduction carry-forwards, payment split, day ledger; read by
  finalization, F-15) and records the engine's issues as ``payroll_exceptions`` (source CALCULATION or
  VALIDATION). A chunk is one transaction: a crashed chunk is rolled back and retried by the job runner
  without duplicate lines (EC-RUN-03); finished chunks never rerun.
* The last chunk to finish (serialised on the run row lock) completes the run: totals per currency (never
  summed across currencies, EC-RUN-19), population counts (eligible = included + excluded, EC-RUN-11),
  a reconciliation of the stored lines against the employee totals (EC-RUN-24), engine and pack versions,
  ``calculated_at``; then COMPLETE_CALCULATION and FAIL_VALIDATION (blocking issues, invariant failures or
  employee errors) or PASS_VALIDATION (3.5 §10). A chunk of a calculation that was stopped (run reopened or
  cancelled) discards its work.
* Variance (3.5 §10): the engine's NET_VARIANCE (net change above
  ``payroll.approval.variance_review_threshold_percent`` against the previous finalized period frozen in
  the snapshot) plus COMPONENT_VARIANCE per component are review exceptions that must be acknowledged
  before submission (``review.acknowledge_variance``; guard VARIANCE_FLAGS_ACKNOWLEDGED).

Services take (db, principal, ...) and never commit; job handlers run in the job's tenant-bound session.
"""

from __future__ import annotations

import logging
from collections import Counter
from decimal import Decimal, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, func, update
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import bad_request, conflict, not_found
from app.core.principal import Principal
from app.models.compensation import PayComponent, PayComponentVersion
from app.models.jobs import Job
from app.models.pay_runs import (
    LINE_SOURCE_TYPES,
    PayrollException,
    PayrollInputSnapshot,
    PayrollLine,
    PayrollLineSource,
    PayrollRun,
    PayrollRunEmployee,
)
from app.payroll import engine
from app.payroll import settings_registry as reg
from app.payroll import state_machines as sm
from app.payroll.money import CALC_CONTEXT, amount_str
from app.payroll.services import access, jobs, runs
# Imported for its hooks: policies.policy_in_use (EC-CFG-16), settings.pack_constraints (F-14) and the loans
# pay-figures provider (F-36) must be installed in every process that serves the API, not only after the
# first inputs were locked there (the route modules import this module at startup).
from app.payroll.services import snapshot as _snapshot_hooks  # noqa: F401

logger = logging.getLogger(__name__)

CALCULATE = runs.CALCULATE
READ = runs.READ
CALCULATE_RUN_JOB = "payroll.calculate_run"
CALCULATE_CHUNK_JOB = "payroll.calculate_chunk"
CHUNK_SIZE = 200
HEARTBEAT_EVERY = 25
JOB_MAX_ATTEMPTS = 5
VARIANCE_KEY = "payroll.approval.variance_review_threshold_percent"

FIRST_CALCULATION_STATES = (sm.RunStatus.READY.value,)
RECALCULATION_STATES = (sm.RunStatus.VALIDATION_FAILED.value, sm.RunStatus.REVIEW_REQUIRED.value,
                        sm.RunStatus.REJECTED.value)
# Run employees that are calculated (a FULL hold is calculated but never finalized, 3.5 §9).
CALCULABLE = ("PENDING", "CALCULATED", "ERROR", "ON_HOLD")
# Issue codes of the validation stage (3.5 §10); every other engine issue is a calculation finding.
VALIDATION_CODES = frozenset({
    "INVARIANT_VIOLATION", "NEGATIVE_NET", "NET_VARIANCE", "COMPONENT_VARIANCE", "NEW_JOINER", "LEAVER",
    "NEW_BANK_ACCOUNT", "PAYMENT_CURRENCY_CHANGED", "COMPONENT_OUTLIER", "BONUS_WITHOUT_INPUT",
    "DUPLICATE_INPUT_AMOUNT",
})
# Variance findings that must be acknowledged before submission; a NET change needs a comment.
VARIANCE_CODES = ("NET_VARIANCE", "COMPONENT_VARIANCE")
COMMENT_REQUIRED_CODES = ("NET_VARIANCE",)
# Engine findings that gate finalization of the whole run, not the calculation: the statutory gate is the
# run's (pre-check finding + ``runs.finalization_blockers``, re-evaluated live so an attestation approved
# after the calculation counts, decision I-15). Recorded per employee as a FLAG that blocks finalization.
RUN_GATED_CODES = frozenset({"STATUTORY_NOT_VERIFIED"})
# Line kinds compared per component in the variance view (employee perspective).
VARIANCE_KINDS = ("EARNING", "DEDUCTION", "REIMBURSEMENT")
# Engine source types -> payroll_line_sources.source_type (consumable records, consumed exactly once).
CONSUMABLE_SOURCES = {
    "wage_input": "WAGE_INPUT", "overtime_entry": "OVERTIME", "task": "TASK", "loan": "LOAN", "advance": "LOAN",
    "claim": "CLAIM", "arrears": "ARREARS", "leave_encashment": "LEAVE_ENCASHMENT",
}
assert set(CONSUMABLE_SOURCES.values()) <= set(LINE_SOURCE_TYPES)

ZERO = Decimal(0)
HUNDRED = Decimal(100)
FIGURES = ("gross", "deductions", "reimbursements", "net", "employer_cost")


def _now():
    return clock.utcnow()


# ---------------------------------------------------------------------------
# Accessors for finalization, payments, documents (the stored engine side results, F-15)
# ---------------------------------------------------------------------------
def stage_outputs_of(row: PayrollRunEmployee) -> dict:
    return dict(row.stage_outputs or {})


def liabilities_of(row: PayrollRunEmployee) -> List[dict]:
    """Statutory liabilities of one run employee: [{component, scheme, portion, payee, amount, currency,
    due_date, period_end, remittance_status}] (``packs.base`` Liability.to_json)."""
    return list(((row.stage_outputs or {}).get("statutory") or {}).get("liabilities") or [])


def ytd_increments_of(row: PayrollRunEmployee) -> Dict[str, str]:
    """{bucket or component code: decimal string} to add to ``ytd_balances`` at finalization."""
    return dict(((row.stage_outputs or {}).get("statutory") or {}).get("ytd_increments") or {})


def pack_state_of(row: PayrollRunEmployee) -> dict:
    """Pack determinations (contribution periods ...) the next snapshot's ``pack_state`` reads (EC-STA-17)."""
    return dict(((row.stage_outputs or {}).get("statutory") or {}).get("pack_state") or {})


def carry_forwards_of(row: PayrollRunEmployee) -> List[dict]:
    """Deductions deferred by net-pay protection, carried to the next run (3.5 §6)."""
    return list(((row.stage_outputs or {}).get("deductions") or {}).get("carry_forwards") or [])


def payments_of(row: PayrollRunEmployee) -> List[dict]:
    """The payment split of the net pay across the payee's bank accounts (3.5 §7 stage 11)."""
    return list((row.stage_outputs or {}).get("payments") or [])


# ---------------------------------------------------------------------------
# Lock inputs & calculate (the API action)
# ---------------------------------------------------------------------------
def calculable(run: PayrollRun) -> bool:
    """Payment-only off-cycle runs and reversal runs carry service-prepared results, never the engine's."""
    if run.run_type == sm.RunType.REVERSAL.value:
        return False
    from app.payroll.services import off_cycle

    return off_cycle.selected_components(run) != ()


def _selected_rows(db: Session, principal: Principal, run: PayrollRun,
                   employee_ids: Optional[Sequence[int]]) -> List[PayrollRunEmployee]:
    query = db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.status.in_(CALCULABLE))
    if employee_ids is None:
        return query.order_by(PayrollRunEmployee.id).with_for_update(of=PayrollRunEmployee).populate_existing().all()
    wanted = sorted(set(int(e) for e in employee_ids))
    rows = (query.filter(PayrollRunEmployee.employee_id.in_(wanted or [0]),
                         runs.employee_scope_clause(db, principal, CALCULATE, run, PayrollRunEmployee.employee_id))
            .order_by(PayrollRunEmployee.id).with_for_update(of=PayrollRunEmployee).populate_existing().all())
    if {r.employee_id for r in rows} != set(wanted):
        raise not_found("Run employee")
    return rows


def _lock_inputs(db: Session, run: PayrollRun, rows: Sequence[PayrollRunEmployee],
                 principal: Principal) -> Dict[int, str]:
    """LOCK_INPUTS / RESNAPSHOT_SELECTED: freeze each row's inputs (``snapshot.lock_inputs``) in this transaction.

    Returns ``{run_employee_id: message}`` for employees whose inputs could not be frozen: the builder records
    a blocking SNAPSHOT_FAILED finding for them and they are not calculated (never from an older snapshot)."""
    from app.payroll.services import snapshot as snapshot_service

    result = snapshot_service.lock_inputs(db, run, list(rows), actor_user_id=principal.user_id)
    db.flush()
    failed = dict(getattr(result, "failed", {}) or {})
    stored = dict(getattr(result, "snapshots", {}) or {})
    for row in rows:
        if row.id in failed:
            row.snapshot_id = None
            continue
        snapshot = stored.get(row.id)
        if snapshot is None:
            raise conflict(f"No input snapshot was taken for run employee {row.id}.", "SNAPSHOT_MISSING")
        row.snapshot_id = snapshot.id
    return failed


def _covered(reason: Mapping[str, Any], employee_ids: set) -> bool:
    if reason.get("employee_id") is not None:
        return reason.get("employee_id") in employee_ids
    many = reason.get("employee_ids")
    return bool(many) and set(many) <= employee_ids


def calculate(db: Session, principal: Principal, run_id: int, *, employee_ids: Optional[Sequence[int]] = None,
              idempotency_key: Optional[str] = None, expected_version: Optional[int] = None) -> dict:
    """Lock inputs and start the calculation job (READY), or recalculate all or selected employees."""
    run = runs.get_run_for(db, principal, run_id, CALCULATE, lock=True)
    runs.ensure_editable(run)
    if not calculable(run):
        raise conflict("This run's results are prepared by its correction service; it is not calculated.",
                       "RUN_NOT_CALCULABLE")
    if run.status in FIRST_CALCULATION_STATES:
        action = "CALCULATE"
        if employee_ids:
            raise bad_request("The first calculation covers every included employee; select employees when "
                              "recalculating.", "SELECTION_NOT_ALLOWED")
    elif run.status in RECALCULATION_STATES:
        action = "RECALCULATE"
    else:
        action = "CALCULATE"   # the state machine refuses it with the right code (CALCULATING, DRAFT ...)
    try:
        sm.transition_spec(sm.RUN, run.status, action)
    except sm.InvalidTransition as exc:
        if run.status == sm.RunStatus.CALCULATING.value:
            raise conflict("A calculation is already running for this run.", "CALCULATION_RUNNING") from None
        raise runs.transition_error(exc)
    if expected_version is not None and run.row_version != expected_version:
        raise conflict("The run was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    rows = _selected_rows(db, principal, run, None if action == "CALCULATE" else employee_ids)
    if not rows:
        raise conflict("No included employee to calculate (EC-RUN-20).", "EMPTY_POPULATION")
    selected = employee_ids is not None and action == "RECALCULATE"
    run = runs.transition(db, principal, run, action, expected_version=expected_version,
                          IDEMPOTENCY_KEY=bool(idempotency_key),
                          details={"employees": len(rows), "selected": selected})
    failed = _lock_inputs(db, run, rows, principal)
    now = _now()
    for row in rows:
        # The previous results of these employees no longer count until their chunk replaces them.
        row.calc_hash, row.calculated_at = None, None
        row.error = {"code": "SNAPSHOT_FAILED", "message": str(failed[row.id])[:500]} if row.id in failed else None
        if row.status != "ON_HOLD":
            row.status = "PENDING"
        row.row_version += 1
    ids = {r.employee_id for r in rows}
    if not selected:
        run.stale, run.stale_reasons = False, []
    elif run.stale:
        remaining = [r for r in (run.stale_reasons or []) if not _covered(r, ids)]
        run.stale, run.stale_reasons = bool(remaining), remaining
    run.engine_version = engine.ENGINE_VERSION
    job = jobs.enqueue(
        db, company_id=run.company_id, type=CALCULATE_RUN_JOB,
        payload={"run_id": run.id, "run_employee_ids": [r.id for r in rows], "round": run.row_version},
        idempotency_key=f"run:{run.id}:round:{run.row_version}", max_attempts=JOB_MAX_ATTEMPTS,
        created_by_user_id=principal.user_id, read_permission=READ)
    run.calculation_job_id = job.id
    db.flush()
    logger.info("Run %s: calculation job %s queued for %s employee(s) at %s", run.id, job.id, len(rows),
                now.isoformat())
    return {"run": {"id": run.id, "number": run.number, "status": run.status, "row_version": run.row_version},
            "job": {"id": job.id, "status": job.status}, "employees": len(rows), "action": action}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _job_access(db: Session, principal: Principal, job: Job) -> bool:
    """Run readers in the run's scope may follow its calculation progress (JobProgress)."""
    run_id = (job.payload or {}).get("run_id")
    if not isinstance(run_id, int) or not principal.has(READ):
        return False
    run = db.query(PayrollRun).filter(PayrollRun.company_id == principal.company_id, PayrollRun.id == run_id).first()
    return run is not None and access.target_scope(principal, READ).covers(run.legal_entity_id, run.pay_group_id)


def _is_current(run: Optional[PayrollRun], parent_job_id: int) -> bool:
    return run is not None and run.status == sm.RunStatus.CALCULATING.value and run.calculation_job_id == parent_job_id


@jobs.register(CALCULATE_RUN_JOB, access=_job_access)
def _calculate_run(ctx: jobs.JobContext) -> dict:
    """Parent job: one child per chunk of CHUNK_SIZE run employees (3.9 §6)."""
    db = ctx.db
    run = db.query(PayrollRun).filter(PayrollRun.company_id == ctx.company_id,
                                      PayrollRun.id == ctx.payload.get("run_id")).with_for_update().first()
    if not _is_current(run, ctx.job_id):
        return {"skipped": "The run is no longer calculating with this job."}
    ids = [int(i) for i in ctx.payload.get("run_employee_ids") or []]
    ctx.progress(0, len(ids))
    chunks = [ids[i:i + CHUNK_SIZE] for i in range(0, len(ids), CHUNK_SIZE)]
    for number, chunk in enumerate(chunks):
        jobs.enqueue(db, company_id=ctx.company_id, type=CALCULATE_CHUNK_JOB,
                     payload={"run_id": run.id, "parent_job_id": ctx.job_id, "chunk": number,
                              "run_employee_ids": chunk},
                     idempotency_key=f"job:{ctx.job_id}:chunk:{number}", parent_job_id=ctx.job_id,
                     max_attempts=JOB_MAX_ATTEMPTS, created_by_user_id=ctx.created_by_user_id, read_permission=READ)
    if not chunks:
        finish_calculation(db, run, ids)
    return {"chunks": len(chunks), "employees": len(ids)}


@jobs.register(CALCULATE_CHUNK_JOB, access=_job_access)
def _calculate_chunk(ctx: jobs.JobContext) -> dict:
    """Child job: calculate one chunk; the last chunk to finish completes the run."""
    db = ctx.db
    cid = ctx.company_id
    parent_id = int(ctx.payload["parent_job_id"])
    run_id = int(ctx.payload["run_id"])
    run = db.query(PayrollRun).filter(PayrollRun.company_id == cid, PayrollRun.id == run_id).first()
    if not _is_current(run, parent_id):
        return {"skipped": "The run is no longer calculating with this job."}
    ids = [int(i) for i in ctx.payload.get("run_employee_ids") or []]
    rows = (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == cid, PayrollRunEmployee.run_id == run.id,
                    PayrollRunEmployee.id.in_(ids or [0]))
            .order_by(PayrollRunEmployee.id).with_for_update(of=PayrollRunEmployee).populate_existing().all())
    catalogue = _Catalogue(db, cid)
    outcome: Counter = Counter()
    for index, row in enumerate(rows):
        if index and index % HEARTBEAT_EVERY == 0:
            ctx.heartbeat()
        outcome[calculate_employee(db, run, row, catalogue)] += 1
    db.flush()
    # Serialise completion on the run row: the last chunk to commit sees every sibling finished.
    run = (db.query(PayrollRun).filter(PayrollRun.company_id == cid, PayrollRun.id == run_id)
           .with_for_update().populate_existing().first())
    if not _is_current(run, parent_id):
        db.rollback()
        return {"discarded": "The calculation was stopped (run reopened or cancelled)."}
    db.execute(update(Job).where(Job.id == parent_id, Job.company_id == cid)
               .values(progress_done=Job.progress_done + len(rows), updated_at=_now())
               .execution_options(synchronize_session=False))
    pending = db.query(func.count(Job.id)).filter(
        Job.company_id == cid, Job.parent_job_id == parent_id, Job.id != ctx.job_id,
        Job.status.notin_(jobs.TERMINAL)).scalar()
    if pending == 0:
        parent = db.get(Job, parent_id)
        finish_calculation(db, run, [int(i) for i in (parent.payload or {}).get("run_employee_ids") or []])
    return {"chunk": ctx.payload.get("chunk"), "employees": len(rows), **dict(sorted(outcome.items()))}


# ---------------------------------------------------------------------------
# One employee
# ---------------------------------------------------------------------------
class _Catalogue:
    """The tenant's component ids by code and its component version ids (FK-safe line references)."""

    def __init__(self, db: Session, company_id: int) -> None:
        self.ids = dict(db.query(PayComponent.code, PayComponent.id).filter(PayComponent.company_id == company_id))
        self._db, self._company_id = db, company_id
        self._versions: Dict[int, Optional[int]] = {}

    def version(self, version_id: Optional[int]) -> Optional[int]:
        """The component of a version id of this tenant, or None when the id is not one of its versions."""
        if version_id is None:
            return None
        if version_id not in self._versions:
            found = self._db.query(PayComponentVersion.component_id).filter(
                PayComponentVersion.company_id == self._company_id, PayComponentVersion.id == version_id).scalar()
            self._versions[version_id] = found
        return self._versions[version_id]


def _period_ref(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None
    start, _, end = str(value).partition("..")
    return {"ref": value, "start": start or None, "end": end or None}


def _source_rows(line: Mapping[str, Any], employee_id: int) -> List[Tuple[str, int, str]]:
    """(source_type, source_id, source_key) of the consumable records a line pays; the keys match
    finalization.parse_sources (a task shared by assignees is paid once per assignee: 'employee:<id>',
    EC-TSK-*, EC-VAR-01). Ids are parsed like finalization's: ``12``, ``"12"`` or prefixed (``"task:12"``,
    ``"overtime:12"``, as the engine writes them)."""
    found = []
    for source in line.get("sources") or []:
        kind = CONSUMABLE_SOURCES.get(str(source.get("type") or ""))
        ident = source.get("id")
        if kind is None or ident is None:
            continue
        try:
            source_id = int(str(ident).strip().rsplit(":", 1)[-1])
        except ValueError:
            continue
        if source_id <= 0:
            continue
        key = ""
        if source.get("instalment_number") is not None:
            key = f"instalment:{source['instalment_number']}"
        elif kind == "TASK":
            key = f"employee:{employee_id}"
        found.append((kind, source_id, key[:80]))
    return sorted(set(found))


def _write_lines(db: Session, run: PayrollRun, row: PayrollRunEmployee, result: Any, catalogue: _Catalogue) -> int:
    """Replace the employee's lines and line sources (idempotent: never duplicates, EC-RUN-03)."""
    db.query(PayrollLine).filter(PayrollLine.company_id == run.company_id,
                                 PayrollLine.run_employee_id == row.id).delete(synchronize_session=False)
    created: List[Tuple[PayrollLine, dict]] = []
    for index, line in enumerate(result.lines):
        data = line.to_json()
        version_id = line.component_version_id
        component_id = catalogue.version(version_id)
        if component_id is None:
            version_id = None   # pack lines and system lines carry no tenant component version
            component_id = catalogue.ids.get(line.component)
        record = PayrollLine(
            company_id=run.company_id, run_id=run.id, run_employee_id=row.id, component_code=line.component[:40],
            component_id=component_id, component_version_id=version_id, kind=line.kind, category=line.category,
            stage=line.stage, amount=line.amount, currency=line.currency, units=line.units, rate=line.rate,
            period_ref=_period_ref(line.period_ref), segment=None if line.segment is None else {"index": line.segment},
            tags=list(line.tags), visibility=line.visibility, sources=data["sources"], trace=data["trace"],
            explanation=line.explanation or None, sort_order=line.sort_order or (index + 1) * 10,
            statutory=line.statutory, fixture=line.fixture)
        db.add(record)
        created.append((record, data))
    db.flush()
    for record, data in created:
        for kind, source_id, key in _source_rows(data, row.employee_id):
            db.add(PayrollLineSource(company_id=run.company_id, line_id=record.id, run_id=run.id,
                                     run_employee_id=row.id, source_type=kind, source_id=source_id, source_key=key,
                                     consumed=False))
    return len(created)


def _supersede_findings(db: Session, row: PayrollRunEmployee) -> None:
    """Earlier calculation findings of the employee no longer apply once it is recalculated."""
    now = _now()
    db.execute(
        update(PayrollException)
        .where(PayrollException.company_id == row.company_id, PayrollException.run_id == row.run_id,
               PayrollException.run_employee_id == row.id, PayrollException.status == "OPEN",
               PayrollException.source.in_(("CALCULATION", "VALIDATION")))
        .values(status="FIXED", resolved_at=now, resolution_note="Superseded by a new calculation.",
                row_version=PayrollException.row_version + 1, updated_at=now)
        .execution_options(synchronize_session=False))


def _record_findings(db: Session, row: PayrollRunEmployee, issues: Sequence[Mapping[str, Any]]) -> None:
    seen: Counter = Counter()
    for issue in issues:
        code = str(issue["code"])[:40]
        component = issue.get("component") or ""
        seen[(code, component)] += 1
        severity = issue["severity"] if issue["severity"] in ("BLOCKING", "WARNING", "FLAG") else "BLOCKING"
        gated = code in RUN_GATED_CODES
        if gated:
            severity = "FLAG"
        resolutions = ["ACKNOWLEDGE"] if code in VARIANCE_CODES else (
            ["FIX", "WAIVE"] if issue.get("waivable") else ["FIX"])
        db.add(PayrollException(
            company_id=row.company_id, run_id=row.run_id, run_employee_id=row.id, employee_id=row.employee_id,
            code=code, severity=severity, source="VALIDATION" if code in VALIDATION_CODES else "CALCULATION",
            message=str(issue.get("message") or code)[:1000],
            details={**dict(issue.get("details") or {}), **({"component": component} if component else {})},
            resolutions=resolutions, fix_link=f"/payroll/runs/{row.run_id}/employees/{row.id}",
            waivable=bool(issue.get("waivable")) or code in VARIANCE_CODES,
            blocks_finalization=severity == "BLOCKING" or gated,
            dedupe_key=f"CALC:{row.id}:{code}:{component}:{seen[(code, component)]}"[:200], status="OPEN",
            row_version=1))


def _threshold(content: Mapping[str, Any]) -> Optional[Decimal]:
    value = (content.get("settings") or {}).get(VARIANCE_KEY, reg.REGISTRY[VARIANCE_KEY].default
                                                if VARIANCE_KEY in reg.REGISTRY else None)
    try:
        return None if value is None else Decimal(str(value))
    except ArithmeticError:
        return None


def change_percent(previous: Optional[Decimal], current: Decimal) -> Optional[Decimal]:
    """|current − previous| ÷ |previous| × 100, rounded to 0.01; None without a previous figure; 100 from zero."""
    if previous is None:
        return None
    with localcontext(CALC_CONTEXT):
        if previous == 0:
            return ZERO if current == 0 else HUNDRED
        return (abs(current - previous) / abs(previous) * HUNDRED).quantize(Decimal("0.01"))


def component_amounts(lines: Iterable[Any]) -> Dict[str, Decimal]:
    """Σ amount per component of the kinds compared in the variance view."""
    found: Dict[str, Decimal] = {}
    with localcontext(CALC_CONTEXT):
        for line in lines:
            kind = line["kind"] if isinstance(line, Mapping) else line.kind
            if kind not in VARIANCE_KINDS:
                continue
            code = line["component"] if isinstance(line, Mapping) else line.component
            amount = Decimal(str(line["amount"])) if isinstance(line, Mapping) else line.amount
            found[code] = found.get(code, ZERO) + amount
    return found


def _not_compared(content: Mapping[str, Any], result: Any) -> set:
    """Component codes outside the compared kinds (employer contributions, rounding ...): the previous period's
    figures in the snapshot cover every line kind, the variance view only the employee's pay."""
    codes = {str(c.get("code")) for c in content.get("components") or []
             if isinstance(c, Mapping) and c.get("kind") not in VARIANCE_KINDS}
    return codes | {line.component for line in result.lines if line.kind not in VARIANCE_KINDS}


def previous_components(content: Mapping[str, Any], result: Any) -> Dict[str, Decimal]:
    """The previous finalized period's amounts per compared component (frozen in the snapshot)."""
    previous = content.get("previous") or {}
    skip = _not_compared(content, result)
    return {str(k): Decimal(str(v)) for k, v in (previous.get("components") or {}).items() if str(k) not in skip}


def _component_variance(content: Mapping[str, Any], result: Any) -> List[dict]:
    """COMPONENT_VARIANCE findings: a component changed by more than the threshold against the previous
    finalized period frozen in the snapshot (3.5 §5.5 variance per component)."""
    previous = content.get("previous") or {}
    if not previous.get("period_ref") or previous.get("currency") not in (None, result.totals.currency):
        return []
    threshold = _threshold(content)
    if threshold is None:
        return []
    before = previous_components(content, result)
    now = component_amounts(result.lines)
    found = []
    for code in sorted(set(before) | set(now)):
        old, new = before.get(code, ZERO), now.get(code, ZERO)
        change = change_percent(old, new)
        if change is None or change <= threshold or old == new:
            continue
        found.append({
            "code": "COMPONENT_VARIANCE", "severity": "WARNING", "component": code, "waivable": True,
            "message": f"{code} changed by {change}% against {previous.get('period_ref')} (above the {threshold}% "
                       "review threshold); acknowledge before approval.",
            "details": {"previous": format(old, "f"), "current": format(new, "f"), "change_percent": format(change, "f"),
                        "threshold_percent": format(threshold, "f")}})
    return found


def _stage_outputs(content: Mapping[str, Any], result: Any) -> dict:
    outputs = dict(result.stage_outputs or {})
    previous = content.get("previous") or {}
    return {
        "engine_version": result.engine_version, "content_hash": result.content_hash, "calc_hash": result.calc_hash,
        "totals": result.totals.to_json(),
        "statutory": outputs.get("statutory") or {}, "deductions": outputs.get("deductions") or {},
        **({"gross_up": outputs["gross_up"]} if outputs.get("gross_up") else {}),
        "payments": [p.to_json() for p in result.payments],
        "ledger": result.ledger,
        "rates": {k: format(v, "f") for k, v in sorted((result.rates or {}).items())},
        "previous": {**{k: previous.get(k) for k in ("period_ref", "currency", "gross", "net")
                        if previous.get(k) is not None},
                     **({"components": {k: format(v, "f") for k, v in previous_components(content, result).items()}}
                        if previous.get("components") else {})},
        "variance_threshold_percent": None if _threshold(content) is None else format(_threshold(content), "f"),
        "pack": content.get("pack"), "fx_snapshot_id": (content.get("fx") or {}).get("snapshot_id"),
        # Components only HR may see (their previous figures are hidden from other viewers too).
        "hr_only": sorted({str(c.get("code")) for c in content.get("components") or []
                           if c.get("visibility") == "HR_ONLY"} | {l.component for l in result.lines
                                                                   if l.visibility == "HR_ONLY"}),
    }


def _mark_error(db: Session, row: PayrollRunEmployee, code: str, message: str, *, record: bool = True) -> None:
    """The employee has no result: no lines, ERROR, and (unless the snapshot builder already recorded the
    reason) a BLOCKING finding."""
    db.query(PayrollLine).filter(PayrollLine.company_id == row.company_id,
                                 PayrollLine.run_employee_id == row.id).delete(synchronize_session=False)
    for name in FIGURES:
        setattr(row, name, None)
    row.content_hash = row.calc_hash = None
    row.calculated_at = _now()
    row.error = {"code": code, "message": message[:500]}
    row.stage_outputs = {}
    row.flags = []
    _set_status(row, "ERROR")
    _supersede_findings(db, row)
    if record:
        _record_findings(db, row, [{"code": code, "severity": "BLOCKING", "message": message,
                                    "details": {"run_employee_id": row.id}}])


def _set_status(row: PayrollRunEmployee, status: str) -> None:
    """A FULL hold keeps ON_HOLD (it is calculated, never finalized); its release returns to ``status``."""
    if row.status == "ON_HOLD":
        hold = dict(row.hold or {})
        hold["status_before"] = status
        row.hold = hold
    else:
        row.status = status
    row.row_version += 1


def calculate_employee(db: Session, run: PayrollRun, row: PayrollRunEmployee, catalogue: _Catalogue) -> str:
    """Calculate one run employee from its frozen snapshot; returns CALCULATED (a result, possibly with blocking
    findings), ERROR (no result: the engine failed or no snapshot) or EXCLUDED (no salary, EC-CMP-14).

    Errors are isolated to the employee (EC-RUN-14); a database outage is re-raised so the chunk retries."""
    snapshot = None
    if row.snapshot_id is not None:
        snapshot = db.query(PayrollInputSnapshot).filter(PayrollInputSnapshot.company_id == run.company_id,
                                                         PayrollInputSnapshot.id == row.snapshot_id).first()
    if snapshot is None:
        failed = (row.error or {}).get("code") == "SNAPSHOT_FAILED"
        _mark_error(db, row, "SNAPSHOT_FAILED" if failed else "SNAPSHOT_MISSING",
                    (row.error or {}).get("message") if failed else
                    "No frozen input snapshot exists for this employee; lock the inputs again by recalculating.",
                    record=not failed)
        return "ERROR"
    content = dict(snapshot.content or {})
    try:
        with db.begin_nested():
            result = engine.calculate(content)
            if result.content_hash != snapshot.content_hash:
                raise engine.SnapshotError("CONTENT_HASH_MISMATCH", "", "the stored snapshot does not match its hash")
            issues = [i.to_json() for i in (*result.exceptions, *result.flags)]
            if result.excluded:
                db.query(PayrollLine).filter(PayrollLine.company_id == run.company_id,
                                             PayrollLine.run_employee_id == row.id).delete(synchronize_session=False)
                for name in FIGURES:
                    setattr(row, name, None)
                row.status, row.exclusion_reason = "EXCLUDED", "NO_SALARY"
                row.exclusion_note = "No active compensation in the period (engine)."
                row.hold_type, row.hold = None, None
                row.row_version += 1
                outcome = "EXCLUDED"
            else:
                _write_lines(db, run, row, result, catalogue)
                issues += _component_variance(content, result)
                figures = result.totals
                row.gross, row.deductions, row.reimbursements = figures.gross, figures.deductions, figures.reimbursements
                row.net, row.employer_cost = figures.net, figures.employer_cost
                # A result exists: blocking findings stay OPEN exceptions (VALIDATION_FAILED until resolved);
                # ERROR is reserved for employees without a result (3.5 §10).
                outcome = "CALCULATED"
                _set_status(row, outcome)
            row.content_hash, row.calc_hash = result.content_hash, result.calc_hash
            row.calculated_at, row.error = _now(), None
            row.flags = sorted({i["code"] for i in issues if i["severity"] == "FLAG" or i["code"] in RUN_GATED_CODES})
            row.stage_outputs = _stage_outputs(content, result)
            _supersede_findings(db, row)
            _record_findings(db, row, issues)
            db.flush()
        return outcome
    except (OperationalError, InterfaceError, jobs.JobInterrupted):
        raise
    except Exception as exc:  # isolated to this employee (EC-RUN-14); never logged with amounts (EC-SEC-08)
        logger.warning("Run %s: employee row %s could not be calculated (%s)", run.id, row.id, type(exc).__name__)
        code = getattr(exc, "code", None) if isinstance(exc, engine.SnapshotError) else None
        _mark_error(db, row, "CALCULATION_ERROR",
                    f"The calculation failed ({code or type(exc).__name__}): {str(exc)[:300]}")
        return "ERROR"


# ---------------------------------------------------------------------------
# Completing the run
# ---------------------------------------------------------------------------
def in_totals(row: PayrollRunEmployee) -> bool:
    """A run employee counted in the run's totals: calculated with a result and finalized with the run. A FULL
    hold (ON_HOLD) is calculated but never finalized or paid in this run (3.5 §9), so it stays out of the totals
    the approvers approve, like an excluded or failed employee (run totals = Σ finalized employees)."""
    return row.status == "CALCULATED" and row.calc_hash is not None


def run_totals(rows: Iterable[PayrollRunEmployee]) -> Dict[str, dict]:
    """{currency: {gross, deductions, reimbursements, net, employer_cost, employees}} over the employees
    ``in_totals``; never summed across currencies (EC-RUN-19)."""
    sums: Dict[str, Dict[str, Decimal]] = {}
    counts: Counter = Counter()
    with localcontext(CALC_CONTEXT):
        for row in rows:
            if not in_totals(row):
                continue
            bucket = sums.setdefault(row.currency, {name: ZERO for name in FIGURES})
            for name in FIGURES:
                bucket[name] += Decimal(getattr(row, name) or 0)
            counts[row.currency] += 1
    return {cur: {**{k: amount_str(v, cur) for k, v in figures.items()}, "employees": counts[cur]}
            for cur, figures in sorted(sums.items())}


def population(rows: Sequence[PayrollRunEmployee]) -> dict:
    """``runs`` population summary plus calculation counts (eligible = included + excluded, EC-RUN-11)."""
    summary = runs._summary(rows)
    summary["calculated"] = sum(1 for r in rows if r.status != "EXCLUDED" and r.calc_hash is not None
                                and r.error is None)
    summary["errors"] = sum(1 for r in rows if r.status == "ERROR" or (r.status == "ON_HOLD" and r.error))
    return summary


def _reconcile_lines(db: Session, run: PayrollRun, rows: Sequence[PayrollRunEmployee]) -> List[str]:
    """EC-RUN-24: the stored lines add up to the stored figures of EVERY employee in the totals (in the
    employee's currency; net = gross + reimbursements − deductions + rounding), and the employees add up to the
    run totals per currency. Checked per employee, so two errors that cancel out in a sum are still found."""
    counted = {r.id: r for r in rows if in_totals(r)}
    sums: Dict[Tuple[int, str, str], Decimal] = {}
    for row_id, currency, kind, total in (
            db.query(PayrollLine.run_employee_id, PayrollLine.currency, PayrollLine.kind, func.sum(PayrollLine.amount))
            .filter(PayrollLine.company_id == run.company_id, PayrollLine.run_id == run.id,
                    PayrollLine.run_employee_id.in_(sorted(counted) or [0]))
            .group_by(PayrollLine.run_employee_id, PayrollLine.currency, PayrollLine.kind)):
        sums[(row_id, currency, kind)] = Decimal(total)
    problems = []
    for row_id, currency in sorted({(k[0], k[1]) for k in sums}):
        if currency != counted[row_id].currency:
            problems.append(f"run employee #{row_id}: a line in {currency}, the employee is paid in "
                            f"{counted[row_id].currency}")
    with localcontext(CALC_CONTEXT):
        per_currency: Dict[str, Dict[str, Decimal]] = {}
        for row_id, row in sorted(counted.items()):
            def line_sum(*kinds: str) -> Decimal:
                return sum((sums.get((row_id, row.currency, k), ZERO) for k in kinds), ZERO)

            expected = {"gross": line_sum("EARNING"), "deductions": line_sum("DEDUCTION"),
                        "reimbursements": line_sum("REIMBURSEMENT"),
                        "net": line_sum("EARNING", "REIMBURSEMENT", "ROUNDING") - line_sum("DEDUCTION"),
                        "employer_cost": line_sum("EARNING", "EMPLOYER_CONTRIBUTION")}
            bucket = per_currency.setdefault(row.currency, {name: ZERO for name in FIGURES})
            for name, value in expected.items():
                bucket[name] += value
                stored = Decimal(getattr(row, name) or 0)
                if stored != value:
                    problems.append(f"run employee #{row_id} {name}: stored {format(stored, 'f')} vs lines "
                                    f"{format(value, 'f')}")
        totals = run_totals(rows)
        for currency in sorted(set(totals) | set(per_currency)):
            for name in FIGURES:
                stored = Decimal((totals.get(currency) or {}).get(name) or 0)
                value = (per_currency.get(currency) or {}).get(name, ZERO)
                if stored != value:
                    problems.append(f"{currency} {name}: employees {format(stored, 'f')} vs lines {format(value, 'f')}")
    return problems


def _pack_versions(db: Session, run: PayrollRun) -> Tuple[List[dict], Optional[int]]:
    packs_seen: Dict[Tuple[str, str], dict] = {}
    fx_ids = set()
    rows = db.query(PayrollRunEmployee.stage_outputs).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.status != "EXCLUDED", PayrollRunEmployee.calc_hash.isnot(None)).all()
    for (outputs,) in rows:
        pack = (outputs or {}).get("pack")
        if pack and pack.get("code"):
            packs_seen[(pack["code"], str(pack.get("version")))] = {
                "code": pack["code"], "version": pack.get("version"), "status": pack.get("status"),
                "is_fixture": bool(pack.get("is_fixture")), "fingerprint": pack.get("fingerprint")}
        if (outputs or {}).get("fx_snapshot_id") is not None:
            fx_ids.add((outputs or {})["fx_snapshot_id"])
    fx_id = next(iter(fx_ids)) if len(fx_ids) == 1 else None
    return [packs_seen[k] for k in sorted(packs_seen)], fx_id if isinstance(fx_id, int) else None


def finish_calculation(db: Session, run: PayrollRun, round_ids: Sequence[int]) -> PayrollRun:
    """Totals, population, reconciliation, versions; then CALCULATED -> VALIDATION_FAILED | REVIEW_REQUIRED."""
    rows = (db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                                PayrollRunEmployee.run_id == run.id)
            .with_for_update(of=PayrollRunEmployee).populate_existing().all())
    wanted = set(round_ids)
    for row in rows:
        if row.id in wanted and row.status in CALCULABLE and row.calc_hash is None and row.error is None:
            _mark_error(db, row, "CALCULATION_INCOMPLETE", "This employee's calculation did not finish (its job "
                        "failed); recalculate the employee.")
    db.flush()
    run.totals = run_totals(rows)
    run.population = population(rows)
    run.pack_version_ids, run.fx_snapshot_id = _pack_versions(db, run)
    run.engine_version = engine.ENGINE_VERSION
    run.calculated_at = _now()
    problems = _reconcile_lines(db, run, rows)
    # The previous round's reconciliation finding is superseded by this one (it is never waivable: without
    # this, a recalculation that fixed the defect could never pass, and one that did not would collide).
    now = _now()
    db.execute(
        update(PayrollException)
        .where(PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
               PayrollException.run_employee_id.is_(None), PayrollException.code == "RUN_TOTALS_MISMATCH",
               PayrollException.status == "OPEN")
        .values(status="FIXED", resolved_at=now, resolution_note="Superseded by a new calculation.",
                row_version=PayrollException.row_version + 1, updated_at=now)
        .execution_options(synchronize_session=False))
    if problems:
        db.add(PayrollException(
            company_id=run.company_id, run_id=run.id, code="RUN_TOTALS_MISMATCH", severity="BLOCKING",
            source="VALIDATION", message="The stored lines do not add up to the employee totals (a defect; never "
            "waivable): " + "; ".join(problems)[:800], details={"problems": problems[:50]}, resolutions=["FIX"],
            waivable=False, blocks_finalization=True, dedupe_key="RUN_TOTALS_MISMATCH", status="OPEN", row_version=1))
    db.flush()
    errors = sum(1 for r in rows if r.status == "ERROR")
    run = runs.transition(db, None, run, "COMPLETE_CALCULATION", system=True,
                          details={"employees": run.population.get("calculated"), "errors": errors,
                                   "currencies": sorted(run.totals)})
    blocking = runs.count_blocking(db, run)
    if blocking or errors or problems:
        return runs.transition(db, None, run, "FAIL_VALIDATION", system=True, BLOCKING_CHECK_FAILED=True,
                               details={"blocking": blocking, "errors": errors})
    return runs.transition(db, None, run, "PASS_VALIDATION", system=True, NO_BLOCKING_CHECK_FAILED=True,
                           details={"blocking": 0})


# ---------------------------------------------------------------------------
# Progress (read by the run page while CALCULATING)
# ---------------------------------------------------------------------------
def progress(db: Session, run: PayrollRun) -> Optional[dict]:
    if run.calculation_job_id is None:
        return None
    job = db.query(Job).filter(Job.id == run.calculation_job_id, Job.company_id == run.company_id).first()
    if job is None:
        return None
    children = Counter(status for (status,) in db.query(Job.status).filter(
        Job.company_id == run.company_id, Job.parent_job_id == job.id))
    return {"job_id": job.id, "status": job.status, "done": job.progress_done, "total": job.progress_total,
            "chunks": dict(sorted(children.items())), "running": run.status == sm.RunStatus.CALCULATING.value}


__all__ = [
    "CALCULATE_CHUNK_JOB", "CALCULATE_RUN_JOB", "CHUNK_SIZE", "COMMENT_REQUIRED_CODES", "VARIANCE_CODES", "calculable",
    "calculate", "calculate_employee", "carry_forwards_of", "change_percent", "component_amounts",
    "finish_calculation", "in_totals", "liabilities_of", "pack_state_of", "payments_of", "population", "progress",
    "run_totals", "stage_outputs_of", "ytd_increments_of",
]
