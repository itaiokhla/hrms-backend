"""Pay runs: creation, population, pre-checks, exceptions, holds, cancellation and the run state
transition (3.5 §3-§5, §9, §16; 3.9 §3.6, §4 Runs, §5.4).

* ``transition(db, principal, run, action, *, reason=None, expected_version=None, system=False,
  **guards)`` is the ONLY writer of ``payroll_runs.status``. It locks the run (``FOR UPDATE``), checks
  the optimistic ``row_version`` (409 ROW_VERSION_CONFLICT), looks the move up in
  ``state_machines.RUN`` (409 with the machine's code: RUN_FINALIZED with the hint CREATE_CORRECTION,
  RUN_CANCELLED, INVALID_TRANSITION), checks the transition's permission inside the transaction
  (403 without it, 404 when the run is outside the caller's scope; system actions need
  ``system=True``), requires a reason where the table says so, evaluates every declared guard
  (built in: BLOCKING_EXCEPTIONS_OPEN, NO_BLOCKING_EXCEPTIONS, NOT_STALE, NO_CALCULATION_RUNNING,
  CALCULATION_RESULTS_EXIST; every other guard must be passed by the caller as ``GUARD=True``,
  an unchecked guard fails closed with 409 GUARD_NOT_CHECKED), stamps the actor columns, applies the
  effects it owns (RELEASE_INPUTS, CLEAR_APPROVALS, STOP_CALCULATION, pay period status) and writes
  the audit event. Effects owned by other services (LOCK_INPUTS, ENQUEUE_CALCULATION, WRITE_YTD ...)
  are the caller's, in the same transaction.
* ``create_run``: the period is OPEN (regular runs), the pay group ACTIVE, the legal entity in the
  caller's scope, one live regular run per pay group and period (409 DUPLICATE_RUN, EC-RUN-01; the
  partial unique index settles races). The population is stored with the run (EC-RUN-11): every
  eligible employee (an assignment in the pay group overlapping the period) is included or excluded
  with a reason (NO_SALARY, INVALID_ASSIGNMENT, JOINED_AFTER_PERIOD, LEFT_BEFORE_PERIOD,
  SUSPENDED_ALL_PERIOD), never silently dropped. One run employee per employee for the primary
  placement (the assignment covering the last day it is in the pay group; segments come from the
  snapshot), plus one per concurrent assignment.
* ``precheck``: refreshes the population and records the findings of 3.5 §5.2 as
  ``payroll_exceptions`` (a re-check updates, closes what is gone, never reopens a waived or deferred
  finding), then moves DRAFT/INPUTS_PENDING to INPUTS_PENDING or READY. A BLOCKING finding blocks
  the run unless it belongs to an excluded employee ("blocking for that employee": NO_SALARY,
  INVALID_ASSIGNMENT exclude the employee). STATUTORY_NOT_VERIFIED and UNCONFIRMED_COUNTRY never block
  calculation; they block finalization (``finalization_blockers``) unless an ACTIVE attestation covers
  the period (decision I-15, EC-ORG-23).
* Exceptions: waive (waivable only, with a reason), defer (to the next period or retro, where allowed),
  fix (server-side fixes such as removing an ineligible employee); audited EXCEPTION_WAIVED / _DEFERRED / _FIXED.
* Holds (3.5 §9): a PAYMENT hold keeps the employee calculated and finalized but holds the payment
  instruction; a FULL hold (status ON_HOLD) keeps the employee out of finalization. Releasing a
  payment hold after finalization creates a pending payment intent (hook for the payments milestone,
  EC-RUN-13). Nobody holds or releases their own pay line.
* Approvals (decision, documented here): the generic approvals engine routes the chain (module
  ``payroll_run``, subject ``payroll_run``); every decision is also recorded in ``payroll_approvals``
  by the approval service, which ``CLEAR_APPROVALS`` marks cleared (EC-RUN-16).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, false, func, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.company import Company
from app.models.compensation import IN_FORCE_STATUSES, CompensationAssignment
from app.models.employee import Employee
from app.models.jobs import Job
from app.models.org import EmployeeAssignment, LegalEntity
from app.models.pay_org import PayGroup
from app.models.pay_runs import (
    PayPeriod,
    PayrollApproval,
    PayrollException,
    PayrollRun,
    PayrollRunEmployee,
)
from app.models.time_inputs import EmployeeSuspension
from app.models.variable_inputs import WageInput
from app.models.work_items import OvertimeEntry, Task, TaskAssignee
from app.payroll import audit as payroll_audit
from app.payroll import packs
from app.payroll import policy_types as pt
from app.payroll import settings_registry as reg
from app.payroll import state_machines as sm
from app.payroll.services import access, approvals, attestations, calendars, jobs, outbox
from app.payroll.services import fx as fx_service
from app.payroll.services import policies as policy_service
from app.payroll.services import settings as settings_engine
from app.payroll.services import time_inputs
from app.services.audit_log_service import AuditLogService

READ = "payroll:run:read"
CREATE = sm.RUN_CREATE
CALCULATE = sm.RUN_CALCULATE
REVIEW = sm.RUN_REVIEW
FINALIZE = sm.RUN_FINALIZE
PAYMENT_APPROVE = sm.PAYMENT_APPROVE
AUDIT_MODULE = payroll_audit.PAYROLL_RUNS
APPROVAL_MODULE = "payroll_run"
APPROVAL_SUBJECT = "payroll_run"
PAYMENT_HOLD_RELEASED_TOPIC = "payroll.payment_hold_released"
ALLOW_MULTIPLE_KEY = "payroll.run.allow_multiple_regular_runs"
LEGACY_ACK_KEY = "payroll.migration.legacy_review_acknowledged"
LEAVE_PENDING_KEY = "payroll.leave.unapproved_at_cutoff"

RUN_CREATED = "RUN_CREATED"
EXCEPTION_WAIVED = "EXCEPTION_WAIVED"
EXCEPTION_DEFERRED = "EXCEPTION_DEFERRED"
EXCEPTION_FIXED = "EXCEPTION_FIXED"
EMPLOYEE_HELD = "EMPLOYEE_HELD"
EMPLOYEE_RELEASED = "EMPLOYEE_RELEASED"
RUN_MARKED_STALE = "RUN_MARKED_STALE"

CREATABLE_TYPES = (sm.RunType.REGULAR, sm.RunType.OFF_CYCLE, sm.RunType.RETRO_ADJUSTMENT, sm.RunType.FINAL_SETTLEMENT)
LIVE_STATUSES = tuple(s.value for s in sm.RunStatus if s is not sm.RunStatus.CANCELLED)
# States in which locked inputs exist and a source change makes the run stale (EC-RUN-09).
LOCKED_STATES = ("CALCULATING", "CALCULATED", "VALIDATION_FAILED", "REVIEW_REQUIRED", "APPROVAL_PENDING", "APPROVED",
                 "REJECTED", "REOPENED_FOR_CORRECTION")
PRECHECK_STATES = (sm.RunStatus.DRAFT.value, sm.RunStatus.INPUTS_PENDING.value)
FULL_HOLD_STATES = ("DRAFT", "INPUTS_PENDING", "READY", "CALCULATED", "VALIDATION_FAILED", "REVIEW_REQUIRED",
                    "REJECTED", "REOPENED_FOR_CORRECTION")
PAYMENT_HOLD_STATES = FULL_HOLD_STATES + ("APPROVAL_PENDING", "APPROVED", "FINALIZED", "PAYMENT_READY")
RESOLVABLE_STATES = tuple(s for s in sm.RUN_PRE_FINALIZATION if s != "CALCULATING") + ("REOPENED_FOR_CORRECTION",)
AUTOMATIC_EXCLUSIONS = ("NO_SALARY", "INVALID_ASSIGNMENT", "JOINED_AFTER_PERIOD", "LEFT_BEFORE_PERIOD",
                        "SUSPENDED_ALL_PERIOD")

# Errors a failed guard maps to: (HTTP status, code).
_GUARD_ERRORS: Dict[str, Tuple[int, str]] = {
    "BLOCKING_EXCEPTIONS_OPEN": (409, "NO_BLOCKING_EXCEPTIONS"),
    "NO_BLOCKING_EXCEPTIONS": (409, "BLOCKING_EXCEPTIONS_OPEN"),
    "NOT_STALE": (409, "RUN_STALE"),
    "NO_CALCULATION_RUNNING": (409, "CALCULATION_RUNNING"),
    "IDEMPOTENCY_KEY": (400, "IDEMPOTENCY_KEY_REQUIRED"),
    "STEP_UP_MFA": (403, "STEP_UP_REQUIRED"),
    "SEPARATION_OF_DUTIES": (403, "SEPARATION_OF_DUTIES"),
    "NO_SELF_PAY_APPROVAL": (403, "SELF_PAY_APPROVAL"),
    "STATUTORY_PACK_VERIFIED": (409, "STATUTORY_NOT_VERIFIED"),
    "VARIANCE_FLAGS_ACKNOWLEDGED": (409, "VARIANCE_NOT_ACKNOWLEDGED"),
    "CALCULATION_RESULTS_EXIST": (409, "NO_CALCULATION_RESULTS"),
    "TWO_APPROVERS": (403, "TWO_APPROVERS_REQUIRED"),
}

FINALIZED_MESSAGE = ("This run is finalized and can no longer change. Create a correction (a reversal or a retro "
                     "adjustment) instead.")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now():
    return clock.utcnow()


def _audit(db: Session, principal: Optional[Principal], event: str, description: str, entity_type: str,
           entity_id: int, company_id: int, /, **details: Any) -> None:
    """Audit event ``event``; ``details`` may carry any key (``action``, ``reason`` ...)."""
    AuditLogService.log(db, principal, AUDIT_MODULE, event, description[:500], entity_type, entity_id,
                        company_id=company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def _text(value: Optional[str]) -> Optional[str]:
    return (value or "").strip() or None


def _reason(value: Optional[str]) -> str:
    reason = _text(value)
    if reason is None:
        raise bad_request("Give a reason.", "REASON_REQUIRED")
    return reason[:1000]


def transition_error(exc: sm.InvalidTransition) -> AppError:
    """409 for a refused move; a finalized run carries the hint to create a correction (EC-RUN-17)."""
    if exc.code == "RUN_FINALIZED":
        return AppError(409, FINALIZED_MESSAGE, "RUN_FINALIZED", headers={"X-Payroll-Hint": "CREATE_CORRECTION"})
    if exc.code == "RUN_CANCELLED":
        return conflict("This run is cancelled.", "RUN_CANCELLED")
    return AppError(409, exc.message, exc.code,
                    headers={"X-Payroll-Hint": exc.hint} if exc.hint else None)


def ensure_editable(run: PayrollRun) -> None:
    """409 RUN_FINALIZED (with the correction hint) or RUN_CANCELLED for runs that can no longer change."""
    try:
        sm.ensure_run_editable(run.status)
    except sm.InvalidTransition as exc:
        raise transition_error(exc)


def _environment() -> str:
    return packs.environment_name(app_settings.environment)


# ---------------------------------------------------------------------------
# Loading with scope
# ---------------------------------------------------------------------------
def load_run(db: Session, company_id: int, run_id: int, *, lock: bool = False) -> PayrollRun:
    query = db.query(PayrollRun).filter(PayrollRun.company_id == company_id, PayrollRun.id == run_id)
    if lock:
        query = query.with_for_update(of=PayrollRun).populate_existing()
    run = query.first()
    if run is None:
        raise not_found("Payroll run")
    return run


def get_run_for(db: Session, principal: Principal, run_id: int, permission: str = READ, *,
                lock: bool = False) -> PayrollRun:
    """A run of the caller's tenant inside the scope of ``permission`` (403 without it, 404 otherwise)."""
    if not principal.has(permission):
        raise forbidden()
    run = load_run(db, principal.company_id, run_id, lock=lock)
    if not access.target_scope(principal, permission).covers(run.legal_entity_id, run.pay_group_id):
        raise not_found("Payroll run")
    return run


def period_of(db: Session, run: PayrollRun) -> PayPeriod:
    return calendars.load_period(db, run.company_id, run.pay_period_id)


def employee_scope_clause(db: Session, principal: Principal, permission: str, run: PayrollRun, column):
    """Run employees the caller may see: the employee was inside the scope during the run's period (3.9 §5.4)."""
    period = period_of(db, run)
    return access.payroll_employee_scope(db, principal, permission, period.period_start,
                                         period.period_end).clause(column)


def sees_whole_run(db: Session, principal: Principal, run: PayrollRun, permission: str = READ) -> bool:
    """Whether every employee of the run lies inside the caller's employee scope for the run's period. When
    not, run-level totals, counts and stale reasons are recomputed over the visible employees (EC-SEC-05)."""
    if access.target_scope(principal, permission).all_company:
        return True
    visible = employee_scope_clause(db, principal, permission, run, PayrollRunEmployee.employee_id)
    total, seen = (db.query(func.count(PayrollRunEmployee.id), func.count(PayrollRunEmployee.id).filter(visible))
                   .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id)
                   .one())
    return total == seen


def visible_stale_reasons(reasons: Iterable[Mapping[str, Any]], visible_ids: Optional[set]) -> List[dict]:
    """Stale reasons without employees outside the caller's scope (``visible_ids`` None: no restriction).
    A reason about one hidden employee keeps only its time; lists and counts are reduced to visible ones."""
    if visible_ids is None:
        return [dict(r) for r in reasons or []]
    shown: List[dict] = []
    for reason in reasons or []:
        item = dict(reason)
        if item.get("employee_id") is not None and item["employee_id"] not in visible_ids:
            shown.append({"reason": "Inputs of an employee outside your scope changed after they were locked.",
                          "at": item.get("at"), "restricted": True})
            continue
        if isinstance(item.get("employee_ids"), list):
            item["employee_ids"] = [e for e in item["employee_ids"] if e in visible_ids]
        if isinstance(item.get("changes"), list):
            kept = [c for c in item["changes"] if isinstance(c, Mapping) and c.get("employee_id") in visible_ids]
            if len(kept) != len(item["changes"]):
                item["reason"] = ("Inputs changed after they were locked; recalculate the affected employees or "
                                  "defer the change to retro.")
            item["changes"] = kept
        item.pop("employees", None)
        shown.append(item)
    return shown


def visible_run_figures(db: Session, principal: Principal, run: PayrollRun) -> Tuple[dict, dict, set]:
    """(totals per currency, population counts, visible employee ids) over the employees inside the caller's
    scope, in the shapes the run stores (``calculation.run_totals`` / ``calculation.population``)."""
    from app.payroll.services import calculation

    rows = db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        employee_scope_clause(db, principal, READ, run, PayrollRunEmployee.employee_id)).all()
    totals = calculation.run_totals(rows) if run.totals else {}
    population = calculation.population(rows) if "calculated" in (run.population or {}) else _summary(rows)
    return totals, population, {r.employee_id for r in rows}


def get_run_employee_for(db: Session, principal: Principal, run: PayrollRun, run_employee_id: int, permission: str,
                         *, lock: bool = False) -> PayrollRunEmployee:
    query = db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == principal.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.id == run_employee_id,
        employee_scope_clause(db, principal, permission, run, PayrollRunEmployee.employee_id))
    if lock:
        query = query.with_for_update(of=PayrollRunEmployee).populate_existing()
    row = query.first()
    if row is None:
        raise not_found("Run employee")
    return row


# ---------------------------------------------------------------------------
# The run state transition (the only writer of payroll_runs.status)
# ---------------------------------------------------------------------------
def count_blocking(db: Session, run: PayrollRun) -> int:
    """OPEN blocking exceptions that block the run: run-level ones and those of employees still in it."""
    return (db.query(func.count(PayrollException.id))
            .outerjoin(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollException.company_id,
                                                PayrollRunEmployee.id == PayrollException.run_employee_id))
            .filter(PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
                    PayrollException.status == "OPEN", PayrollException.severity == "BLOCKING",
                    or_(PayrollException.run_employee_id.is_(None), PayrollRunEmployee.status != "EXCLUDED"))
            .scalar())


def _calculation_running(db: Session, run: PayrollRun) -> bool:
    if run.calculation_job_id is None:
        return False
    status = db.query(Job.status).filter(Job.id == run.calculation_job_id).scalar()
    return status in (jobs.QUEUED, jobs.RUNNING)


def _has_results(db: Session, run: PayrollRun) -> bool:
    return db.query(PayrollRunEmployee.id).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.status.in_(("CALCULATED", "ON_HOLD")), PayrollRunEmployee.calc_hash.isnot(None),
    ).first() is not None


_BUILTIN_GUARDS = {
    "BLOCKING_EXCEPTIONS_OPEN": lambda db, run: count_blocking(db, run) > 0,
    "NO_BLOCKING_EXCEPTIONS": lambda db, run: count_blocking(db, run) == 0,
    "NOT_STALE": lambda db, run: not run.stale,
    "NO_CALCULATION_RUNNING": lambda db, run: not _calculation_running(db, run),
    "CALCULATION_RESULTS_EXIST": _has_results,
}


def _guard_failed(guard: str) -> AppError:
    status, code = _GUARD_ERRORS.get(guard, (409, f"GUARD_{guard}"))
    return AppError(status, sm.GUARDS.get(guard, guard), code)


def _run_employee_ids(db: Session, run: PayrollRun) -> List[int]:
    return [rid for (rid,) in db.query(PayrollRunEmployee.id).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id)]


def claims_ref(run_employee_id: int) -> str:
    """The ``scheduled_ref`` the snapshot builder uses when it schedules claims for a run employee."""
    return f"run_employee:{run_employee_id}"


def _release_inputs(db: Session, run: PayrollRun) -> dict:
    """RELEASE_INPUTS (EC-RUN-15, EC-VAR-01): wage-input reservations and scheduled claims of the run's
    employees go back to their pools. Overtime, tasks, loan instalments and line sources are only
    consumed at finalization, so nothing of theirs is held by an unfinalized run."""
    from app.payroll.services import claims as claims_service
    from app.payroll.services import wage_inputs as wage_input_service

    ids = _run_employee_ids(db, run)
    released_inputs = wage_input_service.release_inputs(db, run.company_id, ids)
    released_claims = sum(claims_service.release_claims(db, run.company_id, claims_ref(rid)) for rid in ids)
    return {"wage_inputs": released_inputs, "claims": released_claims}


def _clear_approvals(db: Session, principal: Optional[Principal], run: PayrollRun, reason: Optional[str]) -> int:
    """CLEAR_APPROVALS (EC-RUN-16): recorded decisions are kept but marked cleared; a pending chain is withdrawn."""
    now = _now()
    cleared = db.execute(
        update(PayrollApproval)
        .where(PayrollApproval.company_id == run.company_id, PayrollApproval.run_id == run.id,
               PayrollApproval.cleared_at.is_(None))
        .values(cleared_at=now, cleared_reason=reason or "Approvals cleared", updated_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.execute(
        update(PayrollRunEmployee)
        .where(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
               PayrollRunEmployee.self_line_approved_by_user_id.isnot(None))
        .values(self_line_approved_by_user_id=None, row_version=PayrollRunEmployee.row_version + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    approvals.withdraw(db, principal, module=APPROVAL_MODULE, subject_type=APPROVAL_SUBJECT, subject_id=run.id,
                       reason=reason or "Approvals cleared")
    run.approval_request_id = None
    return cleared


def _stop_calculation(db: Session, run: PayrollRun) -> None:
    if run.calculation_job_id is not None and _calculation_running(db, run):
        try:
            jobs.cancel(db, run.calculation_job_id, company_id=run.company_id)
        except AppError as exc:  # finished meanwhile
            if exc.code != "JOB_FINISHED":
                raise


def _live_regular_runs(db: Session, company_id: int, period_id: int, *, exclude: Optional[int] = None) -> int:
    query = db.query(func.count(PayrollRun.id)).filter(
        PayrollRun.company_id == company_id, PayrollRun.pay_period_id == period_id,
        PayrollRun.run_type == sm.RunType.REGULAR.value, PayrollRun.status != sm.RunStatus.CANCELLED.value)
    if exclude is not None:
        query = query.filter(PayrollRun.id != exclude)
    return query.scalar()


def _period_effects(db: Session, run: PayrollRun, target: str) -> None:
    if run.run_type != sm.RunType.REGULAR.value:
        return
    period = calendars.load_period(db, run.company_id, run.pay_period_id, lock=True)
    if target == sm.RunStatus.CANCELLED.value and period.status == sm.PeriodStatus.PROCESSING.value \
            and _live_regular_runs(db, run.company_id, period.id, exclude=run.id) == 0:
        calendars.set_period_status(db, period, "RELEASE")
    elif target == sm.RunStatus.FINALIZED.value and period.status == sm.PeriodStatus.PROCESSING.value:
        calendars.set_period_status(db, period, "CLOSE")


def transition(db: Session, principal: Optional[Principal], run: PayrollRun | int, action: str, *,
               reason: Optional[str] = None, expected_version: Optional[int] = None, system: bool = False,
               details: Optional[Mapping[str, Any]] = None, **guards: bool) -> PayrollRun:
    """Move a run by ``action`` of ``state_machines.RUN``; the only writer of ``payroll_runs.status``.

    ``guards`` are the caller's verdicts on guards this function cannot evaluate itself (for example
    ``STEP_UP_MFA=True``, ``IDEMPOTENCY_KEY=True``, ``SEPARATION_OF_DUTIES=True``). Returns the locked,
    updated run.
    """
    unknown = [g for g in guards if g not in sm.GUARDS]
    if unknown:
        raise ValueError(f"unknown run guards {unknown}")
    run_id = run if isinstance(run, int) else run.id
    company_id = principal.company_id if principal is not None else (
        run.company_id if isinstance(run, PayrollRun) else None)
    if company_id is None:
        raise ValueError("a system transition of a run id needs the run object or a principal")
    row = load_run(db, company_id, run_id, lock=True)
    try:
        spec = sm.transition_spec(sm.RUN, row.status, action)
    except sm.InvalidTransition as exc:
        raise transition_error(exc)
    if spec.permission is None:
        if not system:
            raise forbidden("This step is performed by the system.")
    else:
        if principal is None:
            raise forbidden()
        access.ensure_run_access(principal, spec.permission, row)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The run was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    if spec.reason_required:
        reason = _reason(reason)
    for guard in spec.guards:
        if guard in guards:
            ok = bool(guards[guard])
        elif guard in _BUILTIN_GUARDS:
            ok = _BUILTIN_GUARDS[guard](db, row)
        else:
            raise conflict(f"The guard {guard} was not checked for {action}.", "GUARD_NOT_CHECKED")
        if not ok:
            raise _guard_failed(guard)

    previous = row.status
    now = _now()
    actor = principal.user_id if principal is not None else None
    effects: Dict[str, Any] = {}
    if "STOP_CALCULATION" in spec.effects:
        _stop_calculation(db, row)
    row.status = spec.target
    row.row_version += 1
    if action in ("PRECHECK_BLOCKED", "PRECHECK_PASSED"):
        row.prechecked_at = now
    elif action in ("CALCULATE", "RECALCULATE"):
        row.calculated_by_user_id = actor
    elif action == "SUBMIT_FOR_APPROVAL":
        row.submitted_by_user_id, row.submitted_at = actor, now
    elif action == "APPROVE_FINAL":
        row.approved_at = now
    elif action == "FINALIZE":
        row.finalized_by_user_id, row.finalized_at = actor, now
    elif action == "CANCEL":
        row.cancelled_by_user_id, row.cancelled_at = actor, now
    if spec.reason_required or reason:
        row.status_reason = reason
    if "CLEAR_APPROVALS" in spec.effects:
        effects["approvals_cleared"] = _clear_approvals(db, principal, row, reason)
    if "RELEASE_INPUTS" in spec.effects:
        effects["released"] = _release_inputs(db, row)
    if action == "CANCEL" and previous == sm.RunStatus.APPROVAL_PENDING.value:
        approvals.withdraw(db, principal, module=APPROVAL_MODULE, subject_type=APPROVAL_SUBJECT, subject_id=row.id,
                           reason=reason or "Run cancelled")
        row.approval_request_id = None
    db.flush()
    _period_effects(db, row, spec.target)
    event = spec.audit_event or f"RUN_{action}"
    _audit(db, principal, event, f"Run {row.number}: {previous} -> {spec.target} ({action})"
           + (f": {reason}" if reason else ""), "payroll_run", row.id, row.company_id,
           action=action, from_status=previous, to_status=spec.target, reason=reason, row_version=row.row_version,
           **effects, **dict(details or {}))
    return row


# ---------------------------------------------------------------------------
# Population (EC-RUN-11, EC-RUN-12, EC-RUN-20)
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    employee_id: int
    assignment_id: int
    included: bool
    reason: Optional[str] = None
    note: Optional[str] = None
    primary: bool = True
    details: Dict[str, Any] = field(default_factory=dict)


def _overlaps(start: date, end: Optional[date], period_start: date, period_end: date) -> bool:
    return start <= period_end and (end is None or end >= period_start)


def _suspended_all(db: Session, company_id: int, employee_id: int, start: date, end: date) -> bool:
    rows = (db.query(EmployeeSuspension.start_date, EmployeeSuspension.end_date)
            .filter(EmployeeSuspension.company_id == company_id, EmployeeSuspension.employee_id == employee_id,
                    EmployeeSuspension.status == "ACTIVE", EmployeeSuspension.paid.is_(False),
                    EmployeeSuspension.start_date <= end, EmployeeSuspension.end_date >= start)
            .order_by(EmployeeSuspension.start_date).all())
    cursor = start
    for s_start, s_end in rows:
        if s_start > cursor:
            return False
        cursor = max(cursor, s_end + timedelta(days=1))
        if cursor > end:
            return True
    return cursor > end


def _has_salary(db: Session, company_id: int, employee_id: int, start: date, end: date) -> bool:
    return db.query(CompensationAssignment.id).filter(
        CompensationAssignment.company_id == company_id, CompensationAssignment.employee_id == employee_id,
        CompensationAssignment.status.in_(IN_FORCE_STATUSES), CompensationAssignment.effective_from <= end,
        or_(CompensationAssignment.effective_to.is_(None), CompensationAssignment.effective_to >= start),
    ).first() is not None


def compute_population(db: Session, run: PayrollRun, period: PayPeriod, group: PayGroup,
                       selection: Optional[Iterable[int]] = None) -> List[Candidate]:
    """Every eligible employee of the run with its inclusion verdict (eligible = included + excluded)."""
    cid = run.company_id
    start, end = period.period_start, period.period_end
    query = db.query(EmployeeAssignment).filter(
        EmployeeAssignment.company_id == cid, EmployeeAssignment.pay_group_id == group.id,
        EmployeeAssignment.effective_from <= end,
        or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= start))
    wanted = sorted(set(selection)) if selection is not None else None
    if wanted is not None:
        query = query.filter(EmployeeAssignment.employee_id.in_(wanted))
    rows = query.order_by(EmployeeAssignment.employee_id, EmployeeAssignment.effective_from).all()
    employees = {e.id: e for e in db.query(Employee).filter(
        Employee.company_id == cid, Employee.id.in_(sorted({r.employee_id for r in rows}) or [0]))}
    by_employee: Dict[int, List[EmployeeAssignment]] = {}
    for row in rows:
        by_employee.setdefault(row.employee_id, []).append(row)

    candidates: List[Candidate] = []
    for employee_id, assignments in sorted(by_employee.items()):
        employee = employees[employee_id]
        primary = [a for a in assignments if a.is_primary]
        concurrent = [a for a in assignments if not a.is_primary]
        groups: List[Tuple[EmployeeAssignment, bool]] = []
        if primary:
            groups.append((max(primary, key=lambda a: (a.effective_from, a.id)), True))
        groups.extend((a, False) for a in concurrent)
        for assignment, is_primary in groups:
            candidates.append(_verdict(db, cid, employee, assignment, is_primary, start, end, group))
    return candidates


def _verdict(db: Session, cid: int, employee: Employee, assignment: EmployeeAssignment, is_primary: bool,
             start: date, end: date, group: PayGroup) -> Candidate:
    def excluded(reason: str, note: str, **details: Any) -> Candidate:
        return Candidate(employee.id, assignment.id, False, reason, note, is_primary, details)

    if employee.hire_date is not None and employee.hire_date > end:
        return excluded("JOINED_AFTER_PERIOD", f"Joins on {employee.hire_date.isoformat()}, after the period.")
    if employee.termination_date is not None and employee.termination_date < start:
        return excluded("LEFT_BEFORE_PERIOD", f"Left on {employee.termination_date.isoformat()}, before the period.")
    first = max(start, employee.hire_date or start, assignment.effective_from)
    last = min(end, employee.termination_date or end, assignment.effective_to or end)
    if last < first:
        return excluded("LEFT_BEFORE_PERIOD", "Not employed in this pay group during the period.")
    if is_primary:
        # The period is paid by the pay group of the period end (3.5 §14) unless the frequencies differ.
        last_day = min(end, employee.termination_date or end)
        current = settings_engine.primary_assignment(db, cid, employee.id, last_day)
        if current is None:
            return excluded("INVALID_ASSIGNMENT", f"No primary placement on {last_day.isoformat()}.")
        if current.pay_group_id != group.id:
            other = db.get(PayGroup, current.pay_group_id) if current.pay_group_id else None
            if other is None:
                return excluded("INVALID_ASSIGNMENT", f"Placed in no pay group on {last_day.isoformat()}.")
            if other.pay_frequency == group.pay_frequency:
                return excluded("INVALID_ASSIGNMENT",
                                f"Paid by pay group {other.code} (placement on {last_day.isoformat()}).",
                                paid_by_pay_group_id=other.id)
    if _suspended_all(db, cid, employee.id, first, last):
        return excluded("SUSPENDED_ALL_PERIOD", "On unpaid suspension for the whole period.")
    if not _has_salary(db, cid, employee.id, first, last):
        return excluded("NO_SALARY", "No active compensation assignment in the period.")
    return Candidate(employee.id, assignment.id, True, None, None, is_primary)


def _summary(rows: Sequence[PayrollRunEmployee]) -> dict:
    reasons = Counter(r.exclusion_reason for r in rows if r.status == "EXCLUDED")
    return {
        "eligible": len(rows),
        "included": sum(1 for r in rows if r.status != "EXCLUDED"),
        "excluded": sum(reasons.values()),
        "on_hold": sum(1 for r in rows if r.hold_type is not None),
        "excluded_by_reason": dict(sorted(reasons.items())),
    }


def _refresh_summary(db: Session, run: PayrollRun) -> None:
    if sm.is_run_finalized(run.status):
        return
    rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                               PayrollRunEmployee.run_id == run.id).all()
    run.population = _summary(rows)


def _refresh_totals(db: Session, run: PayrollRun) -> None:
    """A FULL hold placed or released after calculation moves the employee out of (or back into) the run
    totals (``calculation.in_totals``: a FULL hold is never paid in this run); totals stay per currency."""
    if sm.is_run_finalized(run.status) or run.calculated_at is None:
        return
    from app.payroll.services import calculation

    rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                               PayrollRunEmployee.run_id == run.id).all()
    totals = calculation.run_totals(rows)
    if totals != dict(run.totals or {}):
        run.totals = totals
        run.row_version += 1


def _sync_population(db: Session, run: PayrollRun, period: PayPeriod, group: PayGroup) -> List[Candidate]:
    """Add new eligible employees, follow a changed primary placement, re-include employees whose automatic
    exclusion no longer applies, and report included employees that are no longer eligible
    (INELIGIBLE_INCLUDED, EC-RUN-12)."""
    selection = (run.selection or {}).get("employee_ids")
    candidates = compute_population(db, run, period, group, selection)
    rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                               PayrollRunEmployee.run_id == run.id).all()
    existing = {(r.employee_id, r.employee_assignment_id): r for r in rows}
    concurrent = {(c.employee_id, c.assignment_id) for c in candidates if not c.primary}
    matched: set = set()
    ineligible: List[Candidate] = []
    for cand in candidates:
        row = existing.get((cand.employee_id, cand.assignment_id))
        if row is None and cand.primary:
            # The placement covering the period end changed since creation: the employee's primary row follows it.
            row = next((r for r in rows if r.employee_id == cand.employee_id and r.id not in matched
                        and (r.employee_id, r.employee_assignment_id) not in concurrent
                        and (r.employee_id, r.employee_assignment_id) not in {(c.employee_id, c.assignment_id)
                                                                              for c in candidates}), None)
            if row is not None:
                row.employee_assignment_id = cand.assignment_id
                row.row_version += 1
        if row is None:
            db.add(_run_employee(run, cand))
            continue
        matched.add(row.id)
        automatic = row.status == "EXCLUDED" and row.exclusion_reason in AUTOMATIC_EXCLUSIONS
        if cand.included and automatic:
            row.status, row.exclusion_reason, row.exclusion_note = "PENDING", None, None
            row.row_version += 1
        elif not cand.included and automatic:
            row.exclusion_reason, row.exclusion_note = cand.reason, (cand.note or "")[:500] or None
        elif not cand.included and row.status != "EXCLUDED":
            if cand.reason in ("NO_SALARY", "INVALID_ASSIGNMENT"):
                row.status, row.exclusion_reason = "EXCLUDED", cand.reason
                row.exclusion_note = (cand.note or "")[:500] or None
                row.hold_type, row.hold = None, None
                row.row_version += 1
            else:
                ineligible.append(cand)
    for row in rows:
        if row.id not in matched and row.status != "EXCLUDED":
            ineligible.append(Candidate(row.employee_id, row.employee_assignment_id, False, "INELIGIBLE",
                                        "No longer placed in this pay group during the period."))
    db.flush()
    return ineligible


def _run_employee(run: PayrollRun, cand: Candidate) -> PayrollRunEmployee:
    return PayrollRunEmployee(
        company_id=run.company_id, run_id=run.id, employee_id=cand.employee_id,
        employee_assignment_id=cand.assignment_id, status="PENDING" if cand.included else "EXCLUDED",
        exclusion_reason=None if cand.included else cand.reason,
        exclusion_note=None if cand.included else (cand.note or "")[:500] or None,
        currency=run.currency, flags=[], row_version=1)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunRequest:
    run_type: str
    pay_group_id: int
    pay_period_id: int
    label: Optional[str] = None
    employee_ids: Tuple[int, ...] = ()
    original_period_ids: Tuple[int, ...] = ()
    reverses_run_id: Optional[int] = None


def _allow_multiple(db: Session, group: PayGroup, period: PayPeriod) -> bool:
    try:
        return bool(settings_engine.resolve(db, group.company_id, ALLOW_MULTIPLE_KEY, pay_group_id=group.id,
                                            on=period.period_end).value)
    except settings_engine.SettingConfigError:
        return False


def _next_number(db: Session, company_id: int, year: int) -> str:
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"payroll_run_number:{company_id}"})
    prefix = f"PR-{year:04d}-"
    last = db.query(func.max(PayrollRun.number)).filter(PayrollRun.company_id == company_id,
                                                        PayrollRun.number.like(prefix + "%")).scalar()
    sequence = int(last[len(prefix):]) + 1 if last else 1
    if sequence > 999_999:
        raise conflict("The yearly run number range is exhausted.", "RUN_NUMBER_EXHAUSTED")
    return f"{prefix}{sequence:06d}"


def employment_ref_of(employee: Employee) -> int:
    """A stable number for one employment period (employee + hire date): one final settlement each, and a
    rehire after settlement is a new employment period (EC-COR-09)."""
    return employee.id * 1_000_000 + employee.hire_date.toordinal()


def default_label(period: PayPeriod, group: PayGroup, run_type: str) -> str:
    if period.frequency == "MONTHLY" and period.period_start.day == 1:
        when = period.period_start.strftime("%b %Y")
    else:
        when = f"{period.period_start.strftime('%d %b')}–{period.period_end.strftime('%d %b %Y')}"
    kind = "" if run_type == sm.RunType.REGULAR.value else f" · {run_type.replace('_', ' ').title()}"
    return f"{when} · {group.name}{kind}"[:150]


def create_run(db: Session, principal: Principal, data: RunRequest, *, allow_reversal: bool = False) -> PayrollRun:
    """Create a run in DRAFT with its population (3.5 §4.1 Create, §5.1)."""
    if not principal.has(CREATE):
        raise forbidden()
    try:
        run_type = sm.RunType(str(data.run_type))
    except ValueError:
        raise bad_request(f"{data.run_type!r} is not a run type.", "INVALID_RUN_TYPE") from None
    if run_type is sm.RunType.REVERSAL and not allow_reversal:
        raise bad_request("A reversal is created from the finalized run (POST runs/{id}/reverse).", "USE_REVERSE")
    cid = principal.company_id
    group = calendars.load_pay_group(db, cid, data.pay_group_id, lock=True)
    if not access.target_scope(principal, CREATE).covers(group.legal_entity_id, group.id):
        raise not_found("Pay group")
    if group.status != "ACTIVE":
        raise conflict("The pay group is inactive.", "PAY_GROUP_INACTIVE")
    period = calendars.load_period(db, cid, data.pay_period_id, lock=True)
    if period.pay_group_id != group.id:
        raise not_found("Pay period")
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == cid, LegalEntity.id == group.legal_entity_id).one()
    # payroll.enabled OFF: "Payroll menus are hidden and no runs can be created" (registry). Existing runs,
    # documents and payments are untouched (EC-RUN-18). Resolved for today: the switch is about availability now.
    if not settings_engine.resolve(db, cid, "payroll.enabled", on=clock.company_today(db, cid)).value:
        raise conflict("Payroll is switched off for this company; no run can be created.", "PAYROLL_DISABLED")

    existing = [sm.RunRef(id=r.id, run_type=sm.RunType(r.run_type), status=r.status, pay_group_id=r.pay_group_id,
                          period_id=r.pay_period_id, reverses_run_id=r.reverses_run_id)
                for r in db.query(PayrollRun).filter(PayrollRun.company_id == cid,
                                                     or_(PayrollRun.pay_period_id == period.id,
                                                         PayrollRun.id == (data.reverses_run_id or 0)))]
    allow_multiple = run_type is sm.RunType.REGULAR and _allow_multiple(db, group, period)
    period_status = period.status
    if period.status == sm.PeriodStatus.PROCESSING.value and _live_regular_runs(db, cid, period.id):
        period_status = None  # the live regular run decides: DUPLICATE_RUN, or a further run when allowed
    selection: Dict[str, Any] = {}
    employment_ref = None
    if run_type is sm.RunType.FINAL_SETTLEMENT:
        if len(data.employee_ids) != 1:
            raise bad_request("A final settlement is for exactly one employee.", "ONE_EMPLOYEE_REQUIRED")
        employee = db.query(Employee).filter(Employee.company_id == cid, Employee.id == data.employee_ids[0]).first()
        if employee is None:
            raise not_found("Employee")
        # Outside the caller's employee scope the employee does not exist for this request: no 409 below may
        # reveal its employment dates (EC-SEC-02). The window reaches back to the last working day.
        window_start = min(period.period_start, employee.termination_date or period.period_start)
        access.ensure_employee_in_scope(db, principal, CREATE, employee.id, window_start, period.period_end)
        if employee.termination_date is None or employee.hire_date is None:
            raise conflict("The employee has no complete employment period (hire and end dates) recorded.",
                           "EMPLOYMENT_NOT_ENDED")
        employment_ref = employment_ref_of(employee)
        selection["employment"] = {"employee_id": employee.id, "hire_date": employee.hire_date.isoformat(),
                                   "termination_date": employee.termination_date.isoformat(),
                                   "ref": employment_ref}
        existing += [sm.RunRef(id=r.id, run_type=sm.RunType.FINAL_SETTLEMENT, status=r.status,
                               pay_group_id=r.pay_group_id, period_id=r.pay_period_id,
                               employment_period_id=(r.selection or {}).get("employment", {}).get("ref"))
                     for r in db.query(PayrollRun).filter(PayrollRun.company_id == cid,
                                                          PayrollRun.run_type == "FINAL_SETTLEMENT")]
    if run_type is sm.RunType.OFF_CYCLE and not data.employee_ids:
        raise bad_request("Select the employees of an off-cycle run.", "EMPLOYEES_REQUIRED")
    try:
        sm.check_run_identity(
            sm.NewRun(run_type=run_type, pay_group_id=group.id, period_id=period.id,
                      original_period_ids=tuple(data.original_period_ids), employment_period_id=employment_ref,
                      reverses_run_id=data.reverses_run_id),
            existing, allow_multiple_regular_runs=allow_multiple, period_status=period_status)
    except sm.RunIdentityError as exc:
        raise conflict(exc.message, exc.code)
    if run_type is sm.RunType.REGULAR and period.status != sm.PeriodStatus.OPEN.value and not allow_multiple:
        raise conflict("A regular run needs an OPEN period.", "PERIOD_NOT_OPEN")
    if data.original_period_ids:
        found = {pid for (pid,) in db.query(PayPeriod.id).filter(
            PayPeriod.company_id == cid, PayPeriod.pay_group_id == group.id,
            PayPeriod.id.in_(list(data.original_period_ids)))}
        if found != set(data.original_period_ids):
            raise not_found("Pay period")
        selection["original_period_ids"] = sorted(found)
    regular_sequence = None
    if run_type is sm.RunType.REGULAR:
        live = _live_regular_runs(db, cid, period.id)
        regular_sequence = 1 if not live else (
            (db.query(func.max(PayrollRun.regular_sequence)).filter(
                PayrollRun.company_id == cid, PayrollRun.pay_period_id == period.id,
                PayrollRun.run_type == "REGULAR", PayrollRun.status != "CANCELLED").scalar() or 0) + 1)
        if regular_sequence > 1 and not data.employee_ids:
            raise bad_request("A further regular run of the period needs selected employees.", "EMPLOYEES_REQUIRED")
    if data.employee_ids:
        selection["employee_ids"] = sorted(set(data.employee_ids))
    today = clock.company_today(db, cid)
    run = PayrollRun(
        company_id=cid, number=_next_number(db, cid, today.year),
        label=(_text(data.label) or default_label(period, group, run_type.value))[:150],
        legal_entity_id=group.legal_entity_id, pay_group_id=group.id, pay_period_id=period.id,
        run_type=run_type.value, status=sm.RunStatus.DRAFT.value, regular_sequence=regular_sequence,
        currency=group.currency, reverses_run_id=data.reverses_run_id, selection=selection, pack_version_ids=[],
        totals={}, population={}, stale=False, stale_reasons=[], environment=_environment(),
        created_by_user_id=principal.user_id, row_version=1)
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError as exc:
        if "uq_payroll_runs_regular" in str(exc.orig):
            raise conflict("A regular run already exists for this pay group and period.", "DUPLICATE_RUN")
        raise
    candidates = compute_population(db, run, period, group, selection.get("employee_ids"))
    if selection.get("employee_ids"):
        missing = set(selection["employee_ids"]) - {c.employee_id for c in candidates}
        if missing:
            raise conflict(f"Employees {sorted(missing)} are not placed in this pay group during the period.",
                           "EMPLOYEE_NOT_IN_PAY_GROUP")
    rows = [_run_employee(run, cand) for cand in candidates]
    try:
        with db.begin_nested():
            db.add_all(rows)
            db.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "pgcode", None) == "23505":
            raise conflict("A selected employee is already paid by another regular run of the period.",
                           "EMPLOYEE_ALREADY_IN_RUN")
        raise
    run.population = _summary(rows)
    if run_type is sm.RunType.REGULAR and period.status == sm.PeriodStatus.OPEN.value:
        calendars.set_period_status(db, period, "START_PROCESSING")
    db.flush()
    _audit(db, principal, RUN_CREATED, f"Created run {run.number} ({run.label})", "payroll_run", run.id, cid,
           number=run.number, run_type=run.run_type, pay_group_id=group.id, pay_period_id=period.id,
           legal_entity_id=entity.id, population=run.population)
    return run


def population_preview(db: Session, principal: Principal, pay_group_id: int, pay_period_id: int,
                       employee_ids: Sequence[int] = ()) -> dict:
    """The population a new run would have, without creating it (the create modal, 3.5 §5.1)."""
    if not principal.has(CREATE):
        raise forbidden()
    group = calendars.load_pay_group(db, principal.company_id, pay_group_id)
    if not access.target_scope(principal, CREATE).covers(group.legal_entity_id, group.id):
        raise not_found("Pay group")
    period = calendars.load_period(db, principal.company_id, pay_period_id)
    if period.pay_group_id != group.id:
        raise not_found("Pay period")
    probe = PayrollRun(company_id=principal.company_id, currency=group.currency)
    candidates = compute_population(db, probe, period, group, list(employee_ids) or None)
    return _population_json(db, principal.company_id, [
        {"employee_id": c.employee_id, "assignment_id": c.assignment_id, "status": "INCLUDED" if c.included else
         "EXCLUDED", "exclusion_reason": c.reason, "note": c.note} for c in candidates])


def _population_json(db: Session, company_id: int, items: List[dict]) -> dict:
    names = {e.id: e for e in db.query(Employee).filter(
        Employee.company_id == company_id, Employee.id.in_(sorted({i["employee_id"] for i in items}) or [0]))}
    for item in items:
        employee = names.get(item["employee_id"])
        item["employee_code"] = employee.employee_code if employee else None
        item["employee_name"] = f"{employee.first_name} {employee.last_name}" if employee else None
    reasons = Counter(i["exclusion_reason"] for i in items if i["status"] == "EXCLUDED")
    return {"eligible": len(items), "included": sum(1 for i in items if i["status"] != "EXCLUDED"),
            "excluded": sum(reasons.values()), "excluded_by_reason": dict(sorted(reasons.items())), "items": items}


# ---------------------------------------------------------------------------
# Pre-checks (3.5 §5.2)
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    code: str
    severity: str
    message: str
    key: str
    run_employee_id: Optional[int] = None
    employee_id: Optional[int] = None
    details: Dict[str, Any] = field(default_factory=dict)
    waivable: bool = False
    resolutions: Tuple[str, ...] = ("FIX",)
    fix_link: Optional[str] = None
    blocks_finalization: bool = False


@dataclass
class _Ctx:
    db: Session
    run: PayrollRun
    period: PayPeriod
    group: PayGroup
    entity: LegalEntity
    company: Company
    included: List[PayrollRunEmployee]
    excluded: List[PayrollRunEmployee]


def bank_account_findings(ctx: _Ctx) -> List[Finding]:
    """MISSING_BANK / UNVERIFIED_BANK warnings (3.5 §5.2, EC-BNK-01/02): per included employee, no account
    serving on the pay date (or only one still in its cooling-off), and, when
    ``payroll.bank.require_verification`` is ON, no verified account. Warnings never block calculation:
    payments hold or mark the instruction NOT_READY instead."""
    from app.payroll.services import bank_accounts as bank_service

    if not ctx.included:
        return []
    try:
        resolved = settings_engine.resolve_many(ctx.db, ctx.run.company_id, ["payroll.bank.require_verification"],
                                                pay_group_id=ctx.group.id, on=ctx.period.period_end, strict=False)
        require = resolved["payroll.bank.require_verification"].value is True
    except settings_engine.SettingConfigError:
        require = True  # fail closed: an unreadable setting still warns about unverified accounts
    by_employee = {r.employee_id: r for r in ctx.included}
    findings: List[Finding] = []
    for item in bank_service.precheck_findings(ctx.db, ctx.run.company_id, by_employee, ctx.period.pay_date,
                                               require_verification=require):
        row = by_employee[item["employee_id"]]
        findings.append(Finding(
            item["code"], "WARNING", item["message"], f"{item['code']}:{row.id}", run_employee_id=row.id,
            employee_id=row.employee_id, details=item["details"],
            fix_link=f"/payroll/bank-accounts?employee_id={row.employee_id}"))
    return findings


# Settings sections that never change an amount: an invalid value there is reported as a WARNING (the
# payments, documents, accounting and notification services fail closed on it themselves) instead of
# blocking the calculation.
AFTER_CALCULATION_SECTIONS = frozenset({
    reg.S_PAYMENTS, reg.S_PAYSLIPS, reg.S_NOTIFICATIONS, reg.S_ACCOUNTING, reg.S_PRIVACY,
})


def _settings_findings(ctx: _Ctx) -> List[Finding]:
    """CONFIG_INVALID for stored payroll values that fail validation (EC-CFG-09) and cross-rule errors:
    BLOCKING for anything the calculation uses, WARNING for after-calculation sections."""
    db, run = ctx.db, ctx.run
    findings: List[Finding] = []
    scope = settings_engine.build_context(db, run.company_id, on=ctx.period.period_end, pay_group_id=ctx.group.id)
    keys = [k for k, d in reg.REGISTRY.items() if d.fails_closed]
    resolved = settings_engine.resolve_in_context(db, scope, keys, strict=False)
    for key, found in sorted(resolved.items()):
        errors = [i for i in found.issues if i.severity == "ERROR"]
        if not errors:
            continue
        section = reg.REGISTRY[key].section
        later = section in AFTER_CALCULATION_SECTIONS
        message = f"Setting {key} is invalid: " + "; ".join(i.message for i in errors)
        if later:
            message += f" It does not change any amount; fix it before {section.lower()} use it."
        findings.append(Finding(
            "CONFIG_INVALID", "WARNING" if later else "BLOCKING", message, f"CONFIG_INVALID:{key}",
            details={"key": key, "level": found.level, "section": section,
                     "issues": [{"rule": i.rule, "message": i.message} for i in errors]},
            fix_link=f"/settings/payroll?key={key}"))
    return findings


def _gate_switches_on(ctx: _Ctx, keys: Sequence[str]) -> List[str]:
    if not keys:
        return []
    try:
        resolved = settings_engine.resolve_many(ctx.db, ctx.run.company_id, keys, pay_group_id=ctx.group.id,
                                                on=ctx.period.period_end, strict=False)
    except settings_engine.SettingConfigError:
        return []
    return [k for k, r in resolved.items() if r.value is True]


def _policy_findings(ctx: _Ctx) -> List[Finding]:
    """V-11 (EC-CFG-07): a switch ON while included employees match no active policy. Under the STARTER_V1
    profile a "decision required" switch turned ON without its policy values is CONFIG_DECISION_REQUIRED."""
    included = {r.employee_id: r for r in ctx.included}
    if not included:
        return []
    decisions = set(reg.PROFILES["STARTER_V1"].decisions) if ctx.company.payroll_profile_version == "STARTER_V1" \
        else set()
    findings: List[Finding] = []
    for code, ptype in sorted(pt.POLICY_TYPES.items()):
        gates = tuple(getattr(ptype, "gate_switches", ()) or ())
        on = [k for k in gates if k in reg.REGISTRY]
        on = _gate_switches_on(ctx, on)
        if not on:
            continue
        missing = [m for m in policy_service.employees_without_policy(
            ctx.db, ctx.run.company_id, code, on=ctx.period.period_end, pay_group_id=ctx.group.id)
            if m["employee_id"] in included]
        if not missing:
            continue
        decision = sorted(set(on) & decisions)
        finding_code = "CONFIG_DECISION_REQUIRED" if decision else "CONFIG_INVALID"
        message = (f"{', '.join(on)} is ON but {len(missing)} employee(s) match no active {code} policy"
                   + (" (a decision is required before the first run)." if decision else "."))
        findings.append(Finding(
            finding_code, "BLOCKING", message, f"{finding_code}:V-11:{code}",
            details={"rule": "V-11", "policy_type": code, "switches": on,
                     "employees": [{"employee_id": m["employee_id"], "status": m["status"]} for m in missing[:200]],
                     "count": len(missing)},
            fix_link=f"/settings/payroll/policies?type={code}"))
    return findings


def _legacy_finding(ctx: _Ctx) -> List[Finding]:
    """I-14: an existing tenant's first v2 run waits for the legacy-review acknowledgment."""
    if ctx.company.payroll_profile_version != "LEGACY_V1":
        return []
    try:
        acknowledged = settings_engine.resolve(ctx.db, ctx.run.company_id, LEGACY_ACK_KEY,
                                               on=ctx.period.period_end).value
    except settings_engine.SettingConfigError:
        acknowledged = None
    if acknowledged:
        return []
    return [Finding("LEGACY_REVIEW_PENDING", "BLOCKING",
                    "Acknowledge the differences between legacy payroll and payroll v2 before the first v2 run: "
                    + " ".join(reg.PROFILES["LEGACY_V1"].acknowledgments), "LEGACY_REVIEW_PENDING",
                    details={"acknowledgments": list(reg.PROFILES["LEGACY_V1"].acknowledgments),
                             "setting": LEGACY_ACK_KEY},
                    fix_link="/settings/payroll/approvals")]


def statutory_status(db: Session, run: PayrollRun, period: PayPeriod, entity: LegalEntity) -> dict:
    """Whether a pack version may produce final statutory lines for the run's entity and period (I-07)."""
    env = run.environment or _environment()
    verdicts = []
    for pack in packs.packs_for_country(entity.country_code, on=period.period_end, environment=env):
        verdict = packs.usage(pack.meta, environment=env, period=(period.period_start, period.period_end))
        verdicts.append({"pack": pack.code, "version": pack.version, "mode": verdict.mode, "reason": verdict.reason,
                         "final": verdict.final})
    return {"country": entity.country_code, "verified": any(v["final"] for v in verdicts), "packs": verdicts}


def _statutory_findings(ctx: _Ctx) -> List[Finding]:
    findings: List[Finding] = []
    status = statutory_status(ctx.db, ctx.run, ctx.period, ctx.entity)
    if not status["verified"]:
        attestation = attestations.active_attestation(ctx.db, ctx.entity.id,
                                                      (ctx.period.period_start, ctx.period.period_end))
        findings.append(Finding(
            "STATUTORY_NOT_VERIFIED", "FLAG" if attestation else "WARNING",
            "Statutory deductions not calculated — compliance not verified. "
            + ("An active statutory attestation covers this period." if attestation else
               "The run cannot be finalized until an approved statutory attestation covers the period."),
            "STATUTORY_NOT_VERIFIED", details={**status, "attestation_id": attestation.id if attestation else None},
            resolutions=("FIX",), fix_link=f"/payroll/compliance/attestations?legal_entity_id={ctx.entity.id}",
            blocks_finalization=attestation is None))
    if not ctx.entity.country_confirmed:
        findings.append(Finding(
            "UNCONFIRMED_COUNTRY", "WARNING",
            f"The country of {ctx.entity.legal_name} ({ctx.entity.country_code}) is not confirmed; the run cannot "
            "be finalized until it is.", "UNCONFIRMED_COUNTRY", details={"legal_entity_id": ctx.entity.id},
            fix_link=f"/settings/organization/legal-entities/{ctx.entity.id}", blocks_finalization=True))
    return findings


def _leave_recalc_findings(ctx: _Ctx) -> List[Finding]:
    """LEAVE_RECALC_PENDING (LH-REL-05, FM-09): a leave recount job (a big holiday amendment, a work-week change)
    still covers the attendance window; the run waits for it instead of paying stale leave charges."""
    from app.services import leave_jobs

    start, end = ctx.period.attendance_start, ctx.period.attendance_end
    if end < start:
        start = end = ctx.period.period_end
    pending = leave_jobs.pending_recalculations(ctx.db, ctx.run.company_id, start, end)
    if not pending:
        return []
    return [Finding("LEAVE_RECALC_PENDING", "BLOCKING",
                    f"{len(pending)} leave recount job(s) still running for this period; run the check again when "
                    "they finish.", "leave_recalc", details={"jobs": pending})]


def _pending_findings(ctx: _Ctx) -> List[Finding]:
    """PENDING_INPUTS: leave, overtime, tasks and wage inputs still awaiting a decision (3.4 §16, EC-CAL-13)."""
    db, cid = ctx.db, ctx.run.company_id
    start, end = ctx.period.attendance_start, ctx.period.attendance_end
    if end < start:
        start = end = ctx.period.period_end
    try:
        leave_mode = settings_engine.resolve(db, cid, LEAVE_PENDING_KEY, pay_group_id=ctx.group.id,
                                             on=ctx.period.period_end).value
    except settings_engine.SettingConfigError:
        leave_mode = "EXCEPTION"
    ids = [r.employee_id for r in ctx.included]
    by_employee = {r.employee_id: r for r in ctx.included}
    findings: List[Finding] = []
    pending_ot = Counter(e for (e,) in db.query(OvertimeEntry.employee_id).filter(
        OvertimeEntry.company_id == cid, OvertimeEntry.employee_id.in_(ids or [0]),
        OvertimeEntry.status == "PENDING_APPROVAL", OvertimeEntry.date >= start, OvertimeEntry.date <= end))
    pending_tasks = Counter(e for (e,) in db.query(TaskAssignee.employee_id).join(
        Task, and_(Task.company_id == TaskAssignee.company_id, Task.id == TaskAssignee.task_id)).filter(
        TaskAssignee.company_id == cid, TaskAssignee.employee_id.in_(ids or [0]), Task.status == "SUBMITTED"))
    pending_wage = Counter(e for (e,) in db.query(WageInput.employee_id).filter(
        WageInput.company_id == cid, WageInput.employee_id.in_(ids or [0]), WageInput.status == "PENDING_APPROVAL",
        WageInput.target_period_start >= ctx.period.period_start, WageInput.target_period_end <= ctx.period.period_end))
    for employee_id in sorted(ids):
        row = by_employee[employee_id]
        leave = time_inputs.pending_inputs(db, cid, employee_id, start, end)
        if leave:
            blocking = leave_mode == "EXCEPTION"
            findings.append(Finding(
                "PENDING_INPUTS", "BLOCKING" if blocking else "WARNING",
                f"{len(leave)} leave or attendance request(s) still pending"
                + (" (payroll.leave.unapproved_at_cutoff = EXCEPTION)." if blocking else f"; treated as {leave_mode}."),
                f"PENDING_INPUTS:LEAVE:{row.id}", run_employee_id=row.id, employee_id=employee_id,
                details={"kind": "LEAVE", "items": leave[:50], "policy": leave_mode}, waivable=blocking,
                # A page that exists today (LH BUG-10 part 1; /leaves/approvals arrives with LH-M5a).
                resolutions=("FIX", "WAIVE", "DEFER"), fix_link="/leaves?tab=approvals"))
        others = {"OVERTIME": pending_ot.get(employee_id, 0), "TASK": pending_tasks.get(employee_id, 0),
                  "WAGE_INPUT": pending_wage.get(employee_id, 0)}
        for kind, count in others.items():
            if count:
                # A final settlement is the employee's last pay: its confirmed settlement items are wage inputs, so
                # one still awaiting approval would be silently left out of a run that settles the case.
                final = kind == "WAGE_INPUT" and ctx.run.run_type == sm.RunType.FINAL_SETTLEMENT.value
                findings.append(Finding(
                    "PENDING_INPUTS", "BLOCKING" if final else "WARNING",
                    f"{count} {kind.lower().replace('_', ' ')} item(s) awaiting approval; "
                    + ("a final settlement is calculated only once they are approved or rejected." if final else
                       "they are not paid in this run unless approved before calculation."),
                    f"PENDING_INPUTS:{kind}:{row.id}", run_employee_id=row.id, employee_id=employee_id,
                    details={"kind": kind, "count": count}, waivable=not final,
                    resolutions=("FIX",) if final else ("FIX", "WAIVE", "DEFER")))
    return findings


def _fx_findings(ctx: _Ctx) -> List[Finding]:
    """FX_MISSING: a compensation in another currency than the run's without a usable rate (3.2 §4.5)."""
    db, cid = ctx.db, ctx.run.company_id
    ids = [r.employee_id for r in ctx.included]
    by_employee = {r.employee_id: r for r in ctx.included}
    rows = db.query(CompensationAssignment.employee_id, CompensationAssignment.currency).filter(
        CompensationAssignment.company_id == cid, CompensationAssignment.employee_id.in_(ids or [0]),
        CompensationAssignment.status.in_(IN_FORCE_STATUSES),
        CompensationAssignment.effective_from <= ctx.period.period_end,
        or_(CompensationAssignment.effective_to.is_(None),
            CompensationAssignment.effective_to >= ctx.period.period_start),
        CompensationAssignment.currency != ctx.run.currency).distinct().all()
    findings: List[Finding] = []
    checked: Dict[str, Any] = {}
    for employee_id, currency in sorted(rows):
        if currency not in checked:
            checked[currency] = fx_service.lookup(db, cid, currency, ctx.run.currency, ctx.period.period_end)
        found = checked[currency]
        if found.ok:
            continue
        row = by_employee[employee_id]
        findings.append(Finding(
            "FX_MISSING", "BLOCKING", f"No usable {currency}->{ctx.run.currency} rate for "
            f"{ctx.period.period_end.isoformat()} ({found.issue.code}).", f"FX_MISSING:{row.id}:{currency}",
            run_employee_id=row.id, employee_id=employee_id,
            details={"base": currency, "quote": ctx.run.currency, "issue": found.issue.code},
            fix_link=f"/payroll/fx-rates?base={currency}&quote={ctx.run.currency}"))
    return findings


def _population_findings(ctx: _Ctx, ineligible: Sequence[Candidate]) -> List[Finding]:
    findings: List[Finding] = []
    for row in ctx.excluded:
        if row.exclusion_reason == "NO_SALARY":
            findings.append(Finding(
                "NO_SALARY", "BLOCKING", "No active compensation assignment in the period: excluded from "
                "calculation (never a zero payslip).", f"NO_SALARY:{row.id}", run_employee_id=row.id,
                employee_id=row.employee_id, fix_link=f"/payroll/compensation?employee_id={row.employee_id}"))
        elif row.exclusion_reason == "INVALID_ASSIGNMENT":
            findings.append(Finding(
                "INVALID_ASSIGNMENT", "BLOCKING", f"{row.exclusion_note or 'Invalid placement'}: excluded.",
                f"INVALID_ASSIGNMENT:{row.id}", run_employee_id=row.id, employee_id=row.employee_id,
                fix_link=f"/employees/{row.employee_id}/placements"))
    by_key = {(r.employee_id, r.employee_assignment_id): r for r in ctx.included}
    for cand in ineligible:
        row = by_key.get((cand.employee_id, cand.assignment_id))
        if row is None:
            continue
        findings.append(Finding(
            "INELIGIBLE_INCLUDED", "BLOCKING", f"{cand.note or 'Not eligible for this period'} Remove the employee "
            "from the run.", f"INELIGIBLE_INCLUDED:{row.id}", run_employee_id=row.id, employee_id=row.employee_id,
            details={"reason": cand.reason}, resolutions=("FIX",)))
    if not ctx.included:
        findings.append(Finding(
            "EMPTY_POPULATION", "BLOCKING", "No employee is included in this run; nothing can be calculated.",
            "EMPTY_POPULATION", fix_link=f"/payroll/runs/{ctx.run.id}/population"))
    return findings


def _store_findings(db: Session, run: PayrollRun, findings: Sequence[Finding]) -> dict:
    now = _now()
    current = {e.dedupe_key: e for e in db.query(PayrollException).filter(
        PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
        PayrollException.source == "PRECHECK", PayrollException.status == "OPEN").with_for_update()}
    resolved_keys = {k for (k,) in db.query(PayrollException.dedupe_key).filter(
        PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
        PayrollException.status.in_(("WAIVED", "DEFERRED")))}
    wanted = {f.key: f for f in findings}
    created = updated = fixed = 0
    for key, row in current.items():
        if key not in wanted:
            row.status, row.resolved_at = "FIXED", now
            row.resolution_note = "No longer found by the pre-check."
            row.row_version += 1
            fixed += 1
    for key, finding in wanted.items():
        if key in resolved_keys and key not in current:
            continue
        values = dict(code=finding.code, severity=finding.severity, message=finding.message[:1000],
                      details=finding.details, resolutions=list(finding.resolutions), fix_link=finding.fix_link,
                      waivable=finding.waivable, blocks_finalization=finding.blocks_finalization,
                      run_employee_id=finding.run_employee_id, employee_id=finding.employee_id)
        row = current.get(key)
        if row is None:
            db.add(PayrollException(company_id=run.company_id, run_id=run.id, source="PRECHECK", dedupe_key=key,
                                    status="OPEN", row_version=1, **values))
            created += 1
        elif any(getattr(row, k) != v for k, v in values.items()):
            for k, v in values.items():
                setattr(row, k, v)
            row.row_version += 1
            updated += 1
    db.flush()
    return {"created": created, "updated": updated, "fixed": fixed}


def precheck(db: Session, principal: Principal, run_id: int, *, expected_version: Optional[int] = None) -> dict:
    """Run the pre-checks and move the run to INPUTS_PENDING or READY (3.5 §4.1, §5.2)."""
    run = get_run_for(db, principal, run_id, CREATE, lock=True)
    try:
        sm.transition_spec(sm.RUN, run.status, "PRECHECK_PASSED")
    except sm.InvalidTransition as exc:
        raise transition_error(exc)
    if expected_version is not None and run.row_version != expected_version:
        raise conflict("The run was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")
    period = calendars.load_period(db, run.company_id, run.pay_period_id)
    group = calendars.load_pay_group(db, run.company_id, run.pay_group_id)
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == run.company_id,
                                          LegalEntity.id == run.legal_entity_id).one()
    company = db.get(Company, run.company_id)
    ineligible = _sync_population(db, run, period, group)
    rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == run.company_id,
                                               PayrollRunEmployee.run_id == run.id).all()
    ctx = _Ctx(db, run, period, group, entity, company, [r for r in rows if r.status != "EXCLUDED"],
               [r for r in rows if r.status == "EXCLUDED"])
    findings: List[Finding] = []
    for check in (lambda c: _population_findings(c, ineligible), _settings_findings, _policy_findings,
                  _legacy_finding, _statutory_findings, _pending_findings, _leave_recalc_findings, _fx_findings,
                  bank_account_findings):
        findings.extend(check(ctx))
    stored = _store_findings(db, run, findings)
    run.population = _summary(rows)
    blocking = count_blocking(db, run)
    action = "PRECHECK_BLOCKED" if blocking else "PRECHECK_PASSED"
    counts = Counter(f.code for f in findings)
    run = transition(db, principal, run, action, details={"findings": dict(sorted(counts.items())), **stored,
                                                          "blocking": blocking})
    return {"run": run_json(db, principal, run), "blocking": blocking, "findings": dict(sorted(counts.items()))}


def finalization_blockers(db: Session, run: PayrollRun) -> List[dict]:
    """Reasons the run may not finalize besides the state machine: stale, open blocking exceptions, the
    statutory gate without an active attestation (I-15, EC-STA-02) and an unconfirmed country (EC-ORG-23).
    Re-evaluated live: an attestation revoked after the pre-check blocks again."""
    period = period_of(db, run)
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == run.company_id,
                                          LegalEntity.id == run.legal_entity_id).one()
    blockers: List[dict] = []
    if run.stale:
        blockers.append({"code": "RUN_STALE", "message": "Inputs changed after lock; recalculate first.",
                         "reasons": list(run.stale_reasons or [])})
    blocking = count_blocking(db, run)
    if blocking:
        blockers.append({"code": "BLOCKING_EXCEPTIONS_OPEN", "message": f"{blocking} blocking exception(s) are open."})
    status = statutory_status(db, run, period, entity)
    if not status["verified"] and attestations.active_attestation(
            db, entity.id, (period.period_start, period.period_end)) is None:
        blockers.append({"code": "STATUTORY_NOT_VERIFIED", "message": "No verified country pack covers the period "
                         "and no approved statutory attestation exists.", "details": status})
    if not entity.country_confirmed:
        blockers.append({"code": "UNCONFIRMED_COUNTRY", "message": "Confirm the legal entity's country first."})
    return blockers


# ---------------------------------------------------------------------------
# Exceptions: waive, defer, fix
# ---------------------------------------------------------------------------
def _exception_for(db: Session, principal: Principal, exception_id: int, permission: str = CREATE
                   ) -> Tuple[PayrollException, PayrollRun]:
    if not principal.has(permission):
        raise forbidden()
    row = db.query(PayrollException).filter(PayrollException.company_id == principal.company_id,
                                            PayrollException.id == exception_id).first()
    if row is None:
        raise not_found("Exception")
    run = load_run(db, principal.company_id, row.run_id, lock=True)
    if not access.target_scope(principal, permission).covers(run.legal_entity_id, run.pay_group_id):
        raise not_found("Exception")
    if row.employee_id is not None:
        visible = db.query(Employee.id).filter(
            Employee.company_id == principal.company_id, Employee.id == row.employee_id,
            employee_scope_clause(db, principal, permission, run, Employee.id)).first()
        if visible is None:
            raise not_found("Exception")
    row = (db.query(PayrollException).filter(PayrollException.id == row.id)
           .with_for_update().populate_existing().one())
    ensure_editable(run)
    if run.status not in RESOLVABLE_STATES:
        raise conflict(f"Exceptions cannot be resolved while the run is {run.status}.", "RUN_BUSY")
    if row.status != "OPEN":
        raise conflict(f"This exception is already {row.status.lower()}.", "EXCEPTION_RESOLVED")
    return row, run


def _resolve(row: PayrollException, status: str, principal: Principal, note: str) -> None:
    row.status, row.resolved_at = status, _now()
    row.resolved_by_user_id, row.resolution_note = principal.user_id, note
    row.row_version += 1


def _self_check(principal: Principal, row: PayrollException) -> None:
    if row.employee_id is not None and principal.employee_id == row.employee_id:
        raise forbidden("You cannot resolve an exception about your own pay.")


def waive_exception(db: Session, principal: Principal, exception_id: int, reason: str, *,
                    expected_version: Optional[int] = None) -> PayrollException:
    row, run = _exception_for(db, principal, exception_id)
    _self_check(principal, row)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The exception was changed by someone else.", "ROW_VERSION_CONFLICT")
    if not row.waivable or "WAIVE" not in (row.resolutions or []):
        raise conflict(f"{row.code} cannot be waived; fix the data or defer it where allowed.", "NOT_WAIVABLE")
    _resolve(row, "WAIVED", principal, _reason(reason))
    db.flush()
    _audit(db, principal, EXCEPTION_WAIVED, f"Waived {row.code} on run {run.number}: {row.resolution_note}",
           "payroll_exception", row.id, run.company_id, run_id=run.id, code=row.code, severity=row.severity,
           employee_id=row.employee_id, reason=row.resolution_note)
    return row


def defer_exception(db: Session, principal: Principal, exception_id: int, to: str, reason: str, *,
                    expected_version: Optional[int] = None) -> PayrollException:
    row, run = _exception_for(db, principal, exception_id)
    _self_check(principal, row)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The exception was changed by someone else.", "ROW_VERSION_CONFLICT")
    if to not in ("NEXT_PERIOD", "RETRO"):
        raise bad_request("Defer to NEXT_PERIOD or RETRO.", "INVALID_DEFER_TARGET")
    if "DEFER" not in (row.resolutions or []):
        raise conflict(f"{row.code} cannot be deferred.", "NOT_DEFERRABLE")
    note = _reason(reason)
    target_period = None
    if to == "NEXT_PERIOD":
        period = period_of(db, run)
        target_period = (db.query(PayPeriod).filter(
            PayPeriod.company_id == run.company_id, PayPeriod.pay_group_id == run.pay_group_id,
            PayPeriod.period_start > period.period_end).order_by(PayPeriod.period_start).first())
        if target_period is None:
            raise conflict("The next pay period is not generated yet.", "NO_NEXT_PERIOD")
    _resolve(row, "DEFERRED", principal, note)
    row.deferred_to = to
    row.deferred_to_period_id = target_period.id if target_period is not None else None
    db.flush()
    _audit(db, principal, EXCEPTION_DEFERRED, f"Deferred {row.code} on run {run.number} to {to}: {note}",
           "payroll_exception", row.id, run.company_id, run_id=run.id, code=row.code, deferred_to=to,
           deferred_to_period_id=row.deferred_to_period_id, employee_id=row.employee_id, reason=note)
    return row


def fix_exception(db: Session, principal: Principal, exception_id: int, note: Optional[str] = None) -> PayrollException:
    """Server-side fixes: INELIGIBLE_INCLUDED removes the employee from the run (EC-RUN-12). Other findings
    are fixed in their source data (``fix_link``) and closed by the next pre-check."""
    row, run = _exception_for(db, principal, exception_id)
    _self_check(principal, row)
    if row.code != "INELIGIBLE_INCLUDED" or row.run_employee_id is None:
        raise conflict("Fix this in its source data and run the pre-checks again.", "FIX_IN_SOURCE")
    if run.status not in PRECHECK_STATES:
        raise conflict("Employees are removed before calculation (reopen the run to return it to draft).",
                       "RUN_BUSY")
    employee = (db.query(PayrollRunEmployee).filter(PayrollRunEmployee.id == row.run_employee_id)
                .with_for_update().populate_existing().one())
    employee.status, employee.exclusion_reason = "EXCLUDED", "INELIGIBLE"
    employee.exclusion_note = (_text(note) or "Removed: not eligible for the period.")[:500]
    employee.hold_type, employee.hold = None, None
    employee.row_version += 1
    _resolve(row, "FIXED", principal, _text(note) or "Employee removed from the run.")
    db.flush()
    _refresh_summary(db, run)
    run.row_version += 1
    db.flush()
    _audit(db, principal, EXCEPTION_FIXED, f"Removed employee #{employee.employee_id} from run {run.number}",
           "payroll_exception", row.id, run.company_id, run_id=run.id, code=row.code,
           employee_id=employee.employee_id, run_employee_id=employee.id)
    return row


# ---------------------------------------------------------------------------
# Holds (3.5 §9, EC-RUN-13)
# ---------------------------------------------------------------------------
HOLD_REASONS = ("ABSCONDING", "DISPUTE", "BANK_ISSUE", "INVESTIGATION", "OTHER")


def _hold_permission(run: PayrollRun) -> str:
    return PAYMENT_APPROVE if sm.is_run_finalized(run.status) else REVIEW


def hold_employee(db: Session, principal: Principal, run_id: int, run_employee_id: int, *, hold_type: str,
                  reason_code: str, note: str, expected_version: Optional[int] = None) -> PayrollRunEmployee:
    if hold_type not in ("PAYMENT", "FULL"):
        raise bad_request("hold_type is PAYMENT or FULL.", "INVALID_HOLD_TYPE")
    if reason_code not in HOLD_REASONS:
        raise bad_request(f"reason_code is one of {', '.join(HOLD_REASONS)}.", "INVALID_HOLD_REASON")
    run = get_run_for(db, principal, run_id, READ if principal.has(READ) else REVIEW, lock=True)
    permission = _hold_permission(run)
    access.ensure_run_access(principal, permission, run)
    if run.status == sm.RunStatus.CALCULATING.value:
        raise conflict("Wait for the calculation to finish.", "CALCULATION_RUNNING")
    if hold_type == "FULL" and run.status not in FULL_HOLD_STATES:
        if sm.is_run_finalized(run.status) or run.status == "CANCELLED":
            ensure_editable(run)
        raise conflict(f"A full hold is placed before approval; the run is {run.status}.", "RUN_BUSY")
    if hold_type == "PAYMENT" and run.status not in PAYMENT_HOLD_STATES:
        if run.status == "CANCELLED":
            ensure_editable(run)
        raise conflict(f"Payments of a {run.status} run can no longer be held.", "RUN_BUSY")
    row = get_run_employee_for(db, principal, run, run_employee_id, permission, lock=True)
    if principal.employee_id is not None and principal.employee_id == row.employee_id:
        raise forbidden("You cannot hold or release your own pay.")
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The employee line was changed by someone else.", "ROW_VERSION_CONFLICT")
    if row.status == "EXCLUDED":
        raise conflict("The employee is excluded from this run.", "EMPLOYEE_EXCLUDED")
    if row.hold_type is not None:
        raise conflict("The employee is already on hold.", "ALREADY_ON_HOLD")
    text_note = _reason(note)
    row.hold = {"type": hold_type, "reason_code": reason_code, "note": text_note, "by_user_id": principal.user_id,
                "at": _now().isoformat(), "status_before": row.status}
    row.hold_type = hold_type
    if hold_type == "FULL":
        row.status = "ON_HOLD"
    row.row_version += 1
    db.flush()
    _refresh_summary(db, run)
    _refresh_totals(db, run)
    db.flush()
    _audit(db, principal, EMPLOYEE_HELD, f"{hold_type.title()} hold on employee #{row.employee_id} in run "
           f"{run.number} ({reason_code})", "payroll_run_employee", row.id, run.company_id, run_id=run.id,
           employee_id=row.employee_id, hold_type=hold_type, reason_code=reason_code, note=text_note)
    return row


def create_payment_intent(db: Session, principal: Principal, run: PayrollRun, row: PayrollRunEmployee) -> dict:
    """Hook (payments milestone, EC-RUN-13): a released payment hold of a finalized run becomes a pending
    payment intent paid in the next batch or off-cycle, with approval. Emits the outbox event when a
    subscriber exists; until the payments milestone subscribes, the intent is reported as pending."""
    payload = {"run_id": run.id, "run_employee_id": row.id}
    if outbox.subscribers(PAYMENT_HOLD_RELEASED_TOPIC):
        event = outbox.emit(db, PAYMENT_HOLD_RELEASED_TOPIC, payload)
        return {"status": "QUEUED", "outbox_event_id": event.id}
    return {"status": "PENDING_PAYMENTS_MILESTONE"}


def release_employee(db: Session, principal: Principal, run_id: int, run_employee_id: int, *, note: str,
                     expected_version: Optional[int] = None) -> Tuple[PayrollRunEmployee, Optional[dict]]:
    run = get_run_for(db, principal, run_id, READ if principal.has(READ) else REVIEW, lock=True)
    permission = _hold_permission(run)
    access.ensure_run_access(principal, permission, run)
    if run.status == "CANCELLED":
        ensure_editable(run)
    if run.status == "CLOSED":
        raise conflict("Holds of a closed run are carried to the carried-holds list.", "RUN_BUSY")
    if run.status == sm.RunStatus.CALCULATING.value:
        raise conflict("Wait for the calculation to finish.", "CALCULATION_RUNNING")
    row = get_run_employee_for(db, principal, run, run_employee_id, permission, lock=True)
    if principal.employee_id is not None and principal.employee_id == row.employee_id:
        raise forbidden("You cannot hold or release your own pay.")
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The employee line was changed by someone else.", "ROW_VERSION_CONFLICT")
    if row.hold_type is None:
        raise conflict("The employee is not on hold.", "NOT_ON_HOLD")
    if row.hold_type == "FULL" and sm.is_run_finalized(run.status):
        raise transition_error(sm.InvalidTransition("RUN", run.status, "RELEASE_HOLD", code="RUN_FINALIZED",
                                                    hint="CREATE_CORRECTION"))
    if row.hold_type == "FULL" and run.status not in FULL_HOLD_STATES:
        # The approvers approved the run without this employee: releasing now would pay an unapproved amount.
        raise conflict(f"The run is {run.status}: a full hold is released before submission (reject or reopen "
                       "the run first, or pay the employee off-cycle).", "RUN_BUSY")
    text_note = _reason(note)
    hold = dict(row.hold or {})
    intent = None
    if row.hold_type == "FULL":
        before = hold.get("status_before")
        row.status = before if before in ("PENDING", "CALCULATED", "ERROR") else "PENDING"
    if row.hold_type == "PAYMENT" and sm.is_run_finalized(run.status):
        intent = create_payment_intent(db, principal, run, row)
    released_type = row.hold_type
    row.hold_type, row.hold = None, None
    row.row_version += 1
    db.flush()
    _refresh_summary(db, run)
    _refresh_totals(db, run)
    db.flush()
    _audit(db, principal, EMPLOYEE_RELEASED, f"Released the {released_type.lower()} hold on employee "
           f"#{row.employee_id} in run {run.number}", "payroll_run_employee", row.id, run.company_id, run_id=run.id,
           employee_id=row.employee_id, hold_type=released_type, previous_hold=hold, note=text_note,
           payment_intent=intent)
    return row, intent


# ---------------------------------------------------------------------------
# Cancel (EC-RUN-15) and staleness (EC-RUN-09)
# ---------------------------------------------------------------------------
def cancel_run(db: Session, principal: Principal, run_id: int, reason: str, *,
               expected_version: Optional[int] = None) -> PayrollRun:
    """Cancel a run before finalization: inputs released, no documents or instructions exist (EC-RUN-15)."""
    run = get_run_for(db, principal, run_id, FINALIZE, lock=True)
    return transition(db, principal, run, "CANCEL", reason=reason, expected_version=expected_version)


def mark_stale(db: Session, company_id: int, reason: str, *, legal_entity_id: Optional[int] = None,
               pay_group_id: Optional[int] = None, employee_ids: Optional[Iterable[int]] = None,
               from_date: Optional[date] = None, details: Optional[Mapping[str, Any]] = None,
               run_ids: Optional[Iterable[int]] = None) -> int:
    """Mark runs with locked, unfinalized inputs stale (EC-RUN-09, EC-CFG-14, EC-CMP-13): submit and
    finalize are then blocked until recalculation. The hooks in settings, policies and compensation
    call this once wired; returns how many runs became (or stayed) stale with the new reason.
    ``run_ids`` limits the marking to runs whose own snapshots were compared (change detection)."""
    query = db.query(PayrollRun).filter(PayrollRun.company_id == company_id, PayrollRun.status.in_(LOCKED_STATES))
    if run_ids is not None:
        query = query.filter(PayrollRun.id.in_(sorted(set(run_ids)) or [0]))
    if legal_entity_id is not None:
        query = query.filter(PayrollRun.legal_entity_id == legal_entity_id)
    if pay_group_id is not None:
        query = query.filter(PayrollRun.pay_group_id == pay_group_id)
    if from_date is not None:
        query = query.join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id,
                                           PayPeriod.id == PayrollRun.pay_period_id)).filter(
            PayPeriod.period_end >= from_date)
    if employee_ids is not None:
        ids = sorted(set(employee_ids))
        query = query.filter(PayrollRun.id.in_(
            db.query(PayrollRunEmployee.run_id).filter(PayrollRunEmployee.company_id == company_id,
                                                       PayrollRunEmployee.employee_id.in_(ids or [0]),
                                                       PayrollRunEmployee.status != "EXCLUDED")))
    count = 0
    for run in query.with_for_update(of=PayrollRun).populate_existing().all():
        entry = {"reason": reason[:300], "at": _now().isoformat(), **dict(details or {})}
        run.stale = True
        run.stale_reasons = list(run.stale_reasons or []) + [entry]
        run.row_version += 1
        count += 1
        _audit(db, None, RUN_MARKED_STALE, f"Run {run.number} is stale: {reason}", "payroll_run", run.id, company_id,
               reason=reason, **dict(details or {}))
    db.flush()
    return count


# ---------------------------------------------------------------------------
# Hooks of earlier milestones answered by pay runs (FOLLOW-UPS F-35, F-37; EC-CFG-03, EC-CFG-14)
# ---------------------------------------------------------------------------
def finalized_periods(db: Session, company_id: int, employee_id: int, from_date: date) -> List[dict]:
    """Finalized regular/off-cycle periods paying the employee from ``from_date`` on (EC-CMP-12):
    ``{run_id, run_employee_id, period_id, start, end}`` ordered by period."""
    rows = (db.query(PayrollRun.id, PayrollRunEmployee.id, PayPeriod.id, PayPeriod.period_start, PayPeriod.period_end)
            .join(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollRun.company_id,
                                           PayrollRunEmployee.run_id == PayrollRun.id))
            .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id,
                                  PayPeriod.id == PayrollRun.pay_period_id))
            .filter(PayrollRun.company_id == company_id, PayrollRun.status.in_(sm.RUN_FINALIZED_STATES),
                    PayrollRun.run_type != sm.RunType.REVERSAL.value, PayrollRunEmployee.employee_id == employee_id,
                    PayrollRunEmployee.status == "CALCULATED", PayPeriod.period_end >= from_date)
            .order_by(PayPeriod.period_start, PayrollRun.id).all())
    return [{"run_id": run_id, "run_employee_id": re_id, "period_id": period_id, "start": start.isoformat(),
             "end": end.isoformat()} for run_id, re_id, period_id, start, end in rows]


def component_in_runs(db: Session, company_id: int, component_id: int) -> bool:
    """Whether any run line used the component (EC-CMP-06: its code is then immutable)."""
    from app.models.pay_runs import PayrollLine

    return db.query(PayrollLine.id).filter(PayrollLine.company_id == company_id,
                                           PayrollLine.component_id == component_id).first() is not None


def period_accepts_inputs(db: Session, company_id: int, pay_group_id: int, start: date, end: date) -> bool:
    """``wage_inputs.period_is_open``: the stored pay periods of the range accept inputs (EC-VAR-03)."""
    return calendars.period_accepts_inputs(db, company_id, pay_group_id, start, end)


def _compensation_stale(db: Session, company_id: int, employee_id: int, from_date: date) -> int:
    return mark_stale(db, company_id, "An approved compensation change affects locked inputs.",
                      employee_ids=[employee_id], from_date=from_date,
                      details={"source": "compensation", "employee_id": employee_id})


def _scope_filters(level: Any, scope_id: Optional[int]) -> Dict[str, Any]:
    """mark_stale filters for a settings/policy scope; broader scopes (tenant, branch) mark every run
    with locked inputs: an over-cautious stale flag only asks for a recalculation (fail closed)."""
    name = getattr(level, "value", level)
    if scope_id is None:
        return {}
    if name == "LEGAL_ENTITY":
        return {"legal_entity_id": scope_id}
    if name == "PAY_GROUP":
        return {"pay_group_id": scope_id}
    if name == "EMPLOYEE":
        return {"employee_ids": [scope_id]}
    return {}


def _settings_stale(db: Session, company_id: int, keys: Iterable[str], level: Any, scope_id: Optional[int],
                    effective_from: date) -> int:
    keys = sorted(set(keys))
    return mark_stale(db, company_id, f"Payroll setting changed: {', '.join(keys)}.", from_date=effective_from,
                      details={"source": "settings", "keys": keys, "level": getattr(level, "value", level),
                               "scope_id": scope_id}, **_scope_filters(level, scope_id))


def _policy_stale(db: Session, company_id: int, policy_id: int, effective_from: date) -> int:
    return mark_stale(db, company_id, "A new policy version came into force.", from_date=effective_from,
                      details={"source": "policies", "policy_id": policy_id})


def _finalized_period_overlaps(db: Session, company_id: int, key: str, level: Any, scope_id: Optional[int],
                               effective_from: date) -> bool:
    """EC-CFG-03: ``effective_from`` falls in (or before) a finalized period of the scope. A branch
    scope matches the finalized runs that paid an assignment of that branch."""
    filters = _scope_filters(level, scope_id)
    query = (db.query(PayrollRun.id)
             .join(PayPeriod, and_(PayPeriod.company_id == PayrollRun.company_id,
                                   PayPeriod.id == PayrollRun.pay_period_id))
             .filter(PayrollRun.company_id == company_id, PayrollRun.status.in_(sm.RUN_FINALIZED_STATES),
                     PayPeriod.period_end >= effective_from))
    if "legal_entity_id" in filters:
        query = query.filter(PayrollRun.legal_entity_id == filters["legal_entity_id"])
    if "pay_group_id" in filters:
        query = query.filter(PayrollRun.pay_group_id == filters["pay_group_id"])
    if "employee_ids" in filters:
        query = query.filter(PayrollRun.id.in_(db.query(PayrollRunEmployee.run_id).filter(
            PayrollRunEmployee.company_id == company_id, PayrollRunEmployee.employee_id.in_(filters["employee_ids"]))))
    if getattr(level, "value", level) == "BRANCH" and scope_id is not None:
        query = query.filter(PayrollRun.id.in_(
            db.query(PayrollRunEmployee.run_id)
            .join(EmployeeAssignment, and_(EmployeeAssignment.company_id == PayrollRunEmployee.company_id,
                                           EmployeeAssignment.id == PayrollRunEmployee.employee_assignment_id))
            .filter(PayrollRunEmployee.company_id == company_id, EmployeeAssignment.branch_id == scope_id)))
    return query.first() is not None


def install_hooks() -> None:
    """Replace the "until pay runs exist" hooks of earlier milestones with the run-backed answers.

    The hooks are module-level functions their modules call by name, so rebinding the module attribute
    replaces them for every caller. Idempotent; runs when this module is imported (the API includes the
    runs router, the worker imports every service module through ``jobs.load_handlers``).
    """
    from app.payroll.services import compensation, components, policies, wage_inputs

    wage_inputs.period_is_open = period_accepts_inputs                 # F-35, EC-VAR-03
    compensation.finalized_periods = finalized_periods                # F-37, EC-CMP-12
    compensation.mark_runs_stale = _compensation_stale                # F-37, EC-CMP-13
    components.component_in_runs = component_in_runs                  # F-37, EC-CMP-06
    settings_engine.mark_runs_stale = _settings_stale                 # EC-CFG-14
    settings_engine.finalized_period_overlaps = _finalized_period_overlaps  # EC-CFG-03
    policies.mark_runs_stale = _policy_stale                          # EC-CFG-14


install_hooks()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _period_brief(period: Optional[PayPeriod]) -> Optional[dict]:
    if period is None:
        return None
    return {"id": period.id, "start": period.period_start.isoformat(), "end": period.period_end.isoformat(),
            "pay_date": period.pay_date.isoformat(), "status": period.status, "tax_year": period.tax_year,
            "input_cutoff": period.input_cutoff_on.isoformat(),
            "approval_deadline": period.approval_deadline_on.isoformat()}


def _exception_counts(db: Session, company_id: int, run_ids: Sequence[int], scope=None) -> Dict[int, dict]:
    """Open and blocking exception counts per run; ``scope`` limits them to the caller's employees."""
    result: Dict[int, dict] = {rid: {"open": 0, "blocking": 0} for rid in run_ids}
    if not run_ids:
        return result
    query = db.query(PayrollException.run_id, PayrollException.severity, func.count(PayrollException.id)).filter(
        PayrollException.company_id == company_id, PayrollException.run_id.in_(list(run_ids)),
        PayrollException.status == "OPEN")
    if scope is not None:
        query = query.filter(scope)
    for run_id, severity, count in query.group_by(PayrollException.run_id, PayrollException.severity):
        result[run_id]["open"] += count
        if severity == "BLOCKING":
            result[run_id]["blocking"] += count
    return result


def run_json(db: Session, principal: Optional[Principal], run: PayrollRun, *, period: Optional[PayPeriod] = None,
             group: Optional[PayGroup] = None, counts: Optional[dict] = None, detail: bool = True) -> dict:
    period = period or calendars.load_period(db, run.company_id, run.pay_period_id)
    group = group or calendars.load_pay_group(db, run.company_id, run.pay_group_id)
    held = [code for code in principal.grants] if principal is not None else []
    actions = [spec.action for spec in sm.RUN.available(run.status, held)
               if access.target_scope(principal, spec.permission).covers(run.legal_entity_id, run.pay_group_id)] \
        if principal is not None else []
    totals, population = dict(run.totals or {}), dict(run.population or {})
    reasons = list(run.stale_reasons or [])
    if principal is not None and not sees_whole_run(db, principal, run):
        # Totals, counts and stale reasons never reveal employees outside the caller's scope (EC-SEC-05).
        totals, population, visible = visible_run_figures(db, principal, run)
        reasons = visible_stale_reasons(reasons, visible)
        counts = _exception_counts(db, run.company_id, [run.id], scope=or_(
            PayrollException.employee_id.is_(None),
            employee_scope_clause(db, principal, READ, run, PayrollException.employee_id)))[run.id]
    data = {
        "id": run.id, "number": run.number, "label": run.label, "run_type": run.run_type, "status": run.status,
        "stage": sm.ui_stage(run.status), "legal_entity_id": run.legal_entity_id,
        "pay_group": {"id": group.id, "code": group.code, "name": group.name},
        "period": _period_brief(period), "currency": run.currency, "stale": run.stale,
        "stale_reasons": reasons, "population": population,
        "totals": totals, "reverses_run_id": run.reverses_run_id, "environment": run.environment,
        "row_version": run.row_version, "created_at": run.created_at.isoformat() if run.created_at else None,
        "available_actions": actions, "exceptions": counts or _exception_counts(db, run.company_id, [run.id])[run.id],
    }
    if detail:
        data.update({
            "selection": dict(run.selection or {}), "pack_version_ids": list(run.pack_version_ids or []),
            "fx_snapshot_id": run.fx_snapshot_id, "engine_version": run.engine_version,
            "calculation_job_id": run.calculation_job_id, "approval_request_id": run.approval_request_id,
            "prechecked_at": run.prechecked_at.isoformat() if run.prechecked_at else None,
            "created_by_user_id": run.created_by_user_id, "calculated_by_user_id": run.calculated_by_user_id,
            "calculated_at": run.calculated_at.isoformat() if run.calculated_at else None,
            "submitted_by_user_id": run.submitted_by_user_id,
            "submitted_at": run.submitted_at.isoformat() if run.submitted_at else None,
            "approved_at": run.approved_at.isoformat() if run.approved_at else None,
            "finalized_by_user_id": run.finalized_by_user_id,
            "finalized_at": run.finalized_at.isoformat() if run.finalized_at else None,
            "cancelled_at": run.cancelled_at.isoformat() if run.cancelled_at else None,
            "status_reason": run.status_reason,
        })
    return data


# The step that moves a run forward from each state ("needs my action", 3.8 §5.3). Exits (CANCEL, REOPEN,
# REVERSE) and system moves are never a pending step; approval steps count only for the current approver
# of the run's approval request (approvals.subjects_awaiting: the step's approvers and their delegates,
# never a barred preparer). A finalized run waiting for payments is the payment users' step, not CLOSE.
NEXT_STEPS: Dict[str, Tuple[str, ...]] = {
    "DRAFT": ("PRECHECK_PASSED", "PRECHECK_BLOCKED"),
    "INPUTS_PENDING": ("PRECHECK_PASSED", "PRECHECK_BLOCKED"),
    "READY": ("CALCULATE",),
    "VALIDATION_FAILED": ("RECALCULATE",),
    "REVIEW_REQUIRED": ("SUBMIT_FOR_APPROVAL",),
    "APPROVAL_PENDING": ("APPROVE_STEP", "APPROVE_FINAL", "REJECT"),
    "REJECTED": ("REWORK", "RECALCULATE"),
    "REOPENED_FOR_CORRECTION": ("RESUME_REVIEW", "RETURN_TO_DRAFT"),
    "APPROVED": ("FINALIZE",),
    "FINALIZED": ("APPROVE_PAYMENTS",),
    "PAYMENT_READY": ("SUBMIT_PAYMENTS",),
    "PAID": ("RECONCILE",),
    "PARTIALLY_PAID": ("RECONCILE",),
    "RECONCILED": ("CLOSE",),
}
APPROVAL_STEP_STATES = ("APPROVAL_PENDING",)


def next_step_permissions(status: str) -> Tuple[str, ...]:
    """The permissions of the user actions that move a run on from ``status`` (from the state machine)."""
    return tuple(dict.fromkeys(sm.RUN.spec(status, action).permission for action in NEXT_STEPS.get(status, ())
                               if sm.RUN.spec(status, action).permission is not None))


def needs_action_clause(db: Session, principal: Principal):
    """SQL filter: runs whose next step the caller can take now, inside the scope of that step's permission
    (the same rule as ``available_actions``) and, for approval steps, only as the current approver."""
    clauses = []
    for status in NEXT_STEPS:
        for permission in next_step_permissions(status):
            if not principal.has(permission):
                continue
            scope = access.run_scope_clause(principal, permission, PayrollRun.legal_entity_id, PayrollRun.pay_group_id)
            if status in APPROVAL_STEP_STATES:
                awaiting = approvals.subjects_awaiting(db, principal, APPROVAL_MODULE, APPROVAL_SUBJECT)
                if not awaiting:
                    continue
                clauses.append(and_(PayrollRun.status == status, PayrollRun.id.in_(sorted(set(awaiting))), scope))
            else:
                clauses.append(and_(PayrollRun.status == status, scope))
    return or_(*clauses) if clauses else false()


def list_runs(db: Session, principal: Principal, *, pay_period_id: Optional[int] = None,
              legal_entity_id: Optional[int] = None, pay_group_id: Optional[int] = None,
              run_type: Optional[str] = None, status: Optional[str] = None, needs_my_action: bool = False,
              skip: int = 0, limit: int = 50) -> Tuple[int, List[dict]]:
    if not principal.has(READ):
        raise forbidden()
    query = db.query(PayrollRun).filter(
        PayrollRun.company_id == principal.company_id,
        access.run_scope_clause(principal, READ, PayrollRun.legal_entity_id, PayrollRun.pay_group_id))
    if needs_my_action:
        query = query.filter(needs_action_clause(db, principal))
    for column, value in ((PayrollRun.pay_period_id, pay_period_id), (PayrollRun.legal_entity_id, legal_entity_id),
                          (PayrollRun.pay_group_id, pay_group_id), (PayrollRun.run_type, run_type),
                          (PayrollRun.status, status)):
        if value is not None:
            query = query.filter(column == value)
    total = query.count()
    runs = query.order_by(PayrollRun.id.desc()).offset(skip).limit(limit).all()
    counts = _exception_counts(db, principal.company_id, [r.id for r in runs])
    periods = {p.id: p for p in db.query(PayPeriod).filter(PayPeriod.company_id == principal.company_id,
                                                           PayPeriod.id.in_([r.pay_period_id for r in runs] or [0]))}
    groups = {g.id: g for g in db.query(PayGroup).filter(PayGroup.company_id == principal.company_id,
                                                         PayGroup.id.in_([r.pay_group_id for r in runs] or [0]))}
    return total, [run_json(db, principal, r, period=periods[r.pay_period_id], group=groups[r.pay_group_id],
                            counts=counts[r.id], detail=False) for r in runs]


def get_run(db: Session, principal: Principal, run_id: int) -> dict:
    run = get_run_for(db, principal, run_id, READ)
    return run_json(db, principal, run)


def run_population(db: Session, principal: Principal, run_id: int, *, status: Optional[str] = None,
                   skip: int = 0, limit: int = 200) -> dict:
    """The run's employees inside the caller's employee scope, with the reconciliation counts (EC-RUN-11)."""
    run = get_run_for(db, principal, run_id, READ)
    scope = employee_scope_clause(db, principal, READ, run, PayrollRunEmployee.employee_id)
    base = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == principal.company_id,
                                               PayrollRunEmployee.run_id == run.id, scope)
    rows = base.all()
    summary = _summary(rows)
    query = base.filter(PayrollRunEmployee.status == status) if status else base
    page = query.order_by(PayrollRunEmployee.id).offset(skip).limit(limit).all()
    names = {e.id: e for e in db.query(Employee).filter(
        Employee.company_id == principal.company_id, Employee.id.in_([r.employee_id for r in page] or [0]))}
    items = []
    for row in page:
        employee = names.get(row.employee_id)
        items.append({
            "id": row.id, "employee_id": row.employee_id, "employee_assignment_id": row.employee_assignment_id,
            "employee_code": employee.employee_code if employee else None,
            "employee_name": f"{employee.first_name} {employee.last_name}" if employee else None,
            "status": row.status, "exclusion_reason": row.exclusion_reason, "exclusion_note": row.exclusion_note,
            "hold_type": row.hold_type,
            "hold": ({k: v for k, v in (row.hold or {}).items() if k != "status_before"} if row.hold else None),
            "row_version": row.row_version,
        })
    return {"run_id": run.id, **summary, "total": query.count(), "items": items}


def exception_json(row: PayrollException) -> dict:
    return {
        "id": row.id, "run_id": row.run_id, "run_employee_id": row.run_employee_id, "employee_id": row.employee_id,
        "code": row.code, "severity": row.severity, "source": row.source, "message": row.message,
        "details": dict(row.details or {}), "resolutions": list(row.resolutions or []), "fix_link": row.fix_link,
        "waivable": row.waivable, "blocks_finalization": row.blocks_finalization, "status": row.status,
        "resolved_by_user_id": row.resolved_by_user_id,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "resolution_note": row.resolution_note, "deferred_to": row.deferred_to,
        "deferred_to_period_id": row.deferred_to_period_id, "row_version": row.row_version,
    }


HR_ONLY_FINDING = "A component only HR may see needs review."


def restrict_hr_only(item: dict, hidden_components: Iterable[str]) -> dict:
    """A finding about a component only HR may see (EC-UX-08: its line is hidden from this viewer) keeps
    its code and status but neither the component nor its amounts."""
    component = (item.get("details") or {}).get("component")
    if component and component in set(hidden_components):
        return {**item, "message": HR_ONLY_FINDING, "details": {"restricted": True}}
    return item


def hr_only_components(db: Session, principal: Principal, run: PayrollRun, run_employee_ids: Iterable[int]
                       ) -> Dict[int, set]:
    """{run employee id: components hidden from the viewer}; empty for viewers who see HR_ONLY lines."""
    from app.payroll.services import review

    ids = sorted({i for i in run_employee_ids if i is not None})
    if not ids or review.sees_hr_only(principal, run):
        return {}
    return {rid: set(codes or []) for rid, codes in db.query(
        PayrollRunEmployee.id, PayrollRunEmployee.stage_outputs["hr_only"]).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.id.in_(ids))}


def list_exceptions(db: Session, principal: Principal, run_id: int, *, status: Optional[str] = None,
                    severity: Optional[str] = None, code: Optional[str] = None, skip: int = 0,
                    limit: int = 200) -> Tuple[int, List[dict]]:
    run = get_run_for(db, principal, run_id, READ)
    query = db.query(PayrollException).filter(
        PayrollException.company_id == principal.company_id, PayrollException.run_id == run.id,
        or_(PayrollException.employee_id.is_(None),
            employee_scope_clause(db, principal, READ, run, PayrollException.employee_id)))
    for column, value in ((PayrollException.status, status), (PayrollException.severity, severity),
                          (PayrollException.code, code)):
        if value is not None:
            query = query.filter(column == value)
    total = query.count()
    rows = query.order_by(PayrollException.id).offset(skip).limit(limit).all()
    hidden = hr_only_components(db, principal, run, [r.run_employee_id for r in rows])
    return total, [restrict_hr_only(exception_json(r), hidden.get(r.run_employee_id, ())) for r in rows]
