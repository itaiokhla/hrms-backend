"""Submission and approval of pay runs (3.5 §4.1, §5.6, §5.7, §11, §12 first row; 3.9 §4 Runs).

Every status change goes through ``runs.transition`` (row lock, ``row_version``, the RUN machine, audit).

* ``submit_run`` (payroll:run:review): REVIEW_REQUIRED -> APPROVAL_PENDING when no blocking exception is
  open, every variance item that must be acknowledged is (``unacknowledged_variance``) and the run is not
  stale (EC-RUN-09). The approval request is opened in the generic approvals engine (module and subject
  ``payroll_run``); the engine instantiates the tenant's ``payroll_run`` chain, or its default (holders of
  payroll:run:approve). ``payroll.approval.run_chain_id`` is recorded in the request context. The run's
  scope (legal entity, pay group) and its net total route and limit the approvers.
* Separation of duties (``payroll.approval.separation_of_duties``, EC-RUN-05): when ON, whoever calculated,
  recalculated or submitted the run cannot approve or reject it (403 SEPARATION_OF_DUTIES). The engine
  enforces the same through ``maker_user_ids`` (the generic inbox included). When OFF the request is raised
  by the system, so the submitter may approve (never their own pay line).
* Self-pay (EC-RUN-06): an approver whose own employee record (or whose delegator's) is in the run approves
  the run, but that one employee line needs another approver (``approve_self_line``, which records
  ``self_line_approved_by_user_id`` and a line decision in ``payroll_approvals``). Finalization waits for
  every such line (``self_lines_pending``); the rest of the run proceeds.
* ``approve_run`` / ``reject_run`` (payroll:run:approve) re-check permission, scope and delegation inside
  the transaction (EC-RUN-07; the engine re-reads the actor's grants under the request lock), lock the run
  row and check its ``row_version``: of two concurrent approvers one wins, the other gets 409 (EC-RUN-08).
  A stale run cannot be approved. Every decision is recorded in ``payroll_approvals`` with its step and
  capacity (approver or delegate on behalf of the delegator). The last step moves the run to APPROVED; a
  rejection (reason required) to REJECTED. Decisions taken in the generic approvals inbox reach the run
  through the engine callback (``_on_decided``), with the same checks.
* ``rework_run`` (payroll:run:calculate): REJECTED -> REVIEW_REQUIRED; the rejected round's decisions are
  cleared (kept as history).
* ``reopen_run`` (payroll:run:finalize, reason): any state before FINALIZED -> REOPENED_FOR_CORRECTION, then
  optionally on to REVIEW_REQUIRED (calculation results exist) or DRAFT (inputs released); approvals are
  cleared and audited by the transition (EC-RUN-16). A finalized run answers 409 RUN_FINALIZED (EC-RUN-17).

Services take (db, principal, ...) and never commit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, forbidden
from app.core.principal import Principal
from app.models.approvals import ApprovalRequest
from app.models.audit_log import AuditLog
from app.models.employee import Employee
from app.models.pay_runs import PayrollApproval, PayrollException, PayrollRun, PayrollRunEmployee
from app.payroll import state_machines as sm
from app.payroll.money import to_decimal
from app.payroll.money_errors import MoneyError
from app.payroll.services import approvals
from app.payroll.services import runs
from app.payroll.services import settings as settings_engine
from app.services import org_service
from app.services.audit_log_service import AuditLogService

APPROVE = sm.RUN_APPROVE
REVIEW = sm.RUN_REVIEW
CALCULATE = sm.RUN_CALCULATE
FINALIZE = sm.RUN_FINALIZE
MODULE = runs.APPROVAL_MODULE
SUBJECT = runs.APPROVAL_SUBJECT
SOD_KEY = "payroll.approval.separation_of_duties"
CHAIN_KEY = "payroll.approval.run_chain_id"
# Calculation and validation findings an approver must see acknowledged (waived with a comment) before the
# run is submitted (3.5 §5.6, §10). Exceptions whose details carry requires_acknowledgment=True count too.
ACKNOWLEDGE_CODES = ("NET_VARIANCE",)
REOPEN_TARGETS = ("REVIEW_REQUIRED", "DRAFT")
SELF_LINE_STATES = (sm.RunStatus.APPROVAL_PENDING.value, sm.RunStatus.APPROVED.value)
# Run employees whose line is finalized (a FULL hold keeps the employee out, EC-RUN-13).
FINALIZABLE = ("CALCULATED",)

RUN_SELF_LINE_APPROVED = "RUN_SELF_LINE_APPROVED"
STALE_MESSAGE = "Inputs, settings or the country pack changed after the inputs were locked; recalculate first."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return clock.utcnow()


def _audit(db: Session, principal: Optional[Principal], event: str, description: str, entity_type: str,
           entity_id: int, company_id: int, **details: Any) -> None:
    AuditLogService.log(db, principal, runs.AUDIT_MODULE, event, description[:500], entity_type, entity_id,
                        company_id=company_id, actor_type="user" if principal is not None else "system",
                        details=details)


def _check_version(run: PayrollRun, expected_version: Optional[int]) -> None:
    if expected_version is not None and run.row_version != expected_version:
        raise conflict("The run was changed by someone else. Reload and try again.", "ROW_VERSION_CONFLICT")


def _ensure_action(run: PayrollRun, action: str) -> None:
    """409 (RUN_FINALIZED with the correction hint, RUN_CANCELLED, INVALID_TRANSITION) when ``action`` is not
    allowed from the run's state, before any other work."""
    try:
        sm.transition_spec(sm.RUN, run.status, action)
    except sm.InvalidTransition as exc:
        raise runs.transition_error(exc)


def _ensure_not_stale(run: PayrollRun) -> None:
    if run.stale:
        raise AppError(409, STALE_MESSAGE, "RUN_STALE")


def sod_enabled(db: Session, run: PayrollRun) -> bool:
    """``payroll.approval.separation_of_duties`` for the run's scope on its period end; ON when unreadable."""
    period = runs.period_of(db, run)
    try:
        value = settings_engine.resolve(db, run.company_id, SOD_KEY, pay_group_id=run.pay_group_id,
                                        legal_entity_id=run.legal_entity_id, on=period.period_end).value
    except settings_engine.SettingConfigError:
        return True
    return value is not False


def _chain_id(db: Session, run: PayrollRun) -> Any:
    period = runs.period_of(db, run)
    try:
        return settings_engine.resolve(db, run.company_id, CHAIN_KEY, pay_group_id=run.pay_group_id,
                                       legal_entity_id=run.legal_entity_id, on=period.period_end).value
    except settings_engine.SettingConfigError:
        return None


def preparer_user_ids(db: Session, run: PayrollRun) -> Set[int]:
    """Users who calculated or recalculated (every INPUTS_LOCKED of the run) or submitted it (EC-RUN-05)."""
    found = {uid for uid in (run.calculated_by_user_id, run.submitted_by_user_id) if uid is not None}
    found.update(uid for (uid,) in db.query(AuditLog.actor_id).filter(
        AuditLog.company_id == run.company_id, AuditLog.module == runs.AUDIT_MODULE,
        AuditLog.entity_type == "payroll_run", AuditLog.entity_id == str(run.id),
        AuditLog.action == "INPUTS_LOCKED", AuditLog.actor_type == "user", AuditLog.actor_id.isnot(None)))
    return found


def _ensure_separation(db: Session, principal: Principal, run: PayrollRun) -> None:
    if sod_enabled(db, run) and principal.user_id in preparer_user_ids(db, run):
        raise AppError(403, "Separation of duties: whoever calculated or submitted this run cannot approve it.",
                       "SEPARATION_OF_DUTIES")


def unacknowledged_variance(db: Session, run: PayrollRun) -> int:
    """OPEN findings that must be acknowledged before submission, for employees still paid by the run.

    The review service may own acknowledgment (``review.unacknowledged_count(db, run)``); without it, an
    acknowledgment is the waiver (with a comment) of the finding."""
    try:
        from app.payroll.services import review as review_service
    except ImportError:  # the review service is optional
        review_service = None
    custom = getattr(review_service, "unacknowledged_count", None)
    if callable(custom):
        return int(custom(db, run))
    return _variance_count(db, run)


def _variance_count(db: Session, run: PayrollRun) -> int:
    return (db.query(func.count(PayrollException.id))
            .outerjoin(PayrollRunEmployee, and_(PayrollRunEmployee.company_id == PayrollException.company_id,
                                                PayrollRunEmployee.id == PayrollException.run_employee_id))
            .filter(PayrollException.company_id == run.company_id, PayrollException.run_id == run.id,
                    PayrollException.status == "OPEN",
                    or_(PayrollException.code.in_(ACKNOWLEDGE_CODES),
                        PayrollException.details["requires_acknowledgment"].astext == "true"),
                    or_(PayrollException.run_employee_id.is_(None),
                        PayrollRunEmployee.status.notin_(("EXCLUDED", "ON_HOLD"))))
            .scalar())


def _net_total(run: PayrollRun) -> Optional[str]:
    figures = (run.totals or {}).get(run.currency) or {}
    value = figures.get("net")
    if value is None:
        return None
    try:
        to_decimal(str(value))
    except MoneyError:
        return None
    return str(value)


def _pending_request(db: Session, run: PayrollRun, *, lock: bool = False) -> ApprovalRequest:
    request = approvals.pending_request(db, MODULE, SUBJECT, run.id, lock=lock)
    if request is None:
        raise conflict("This run has no pending approval request; submit it again.", "APPROVAL_NOT_PENDING")
    return request


# ---------------------------------------------------------------------------
# Recording decisions (payroll_approvals) and the self-pay lines (EC-RUN-06)
# ---------------------------------------------------------------------------
def _user_ids_of_employees(db: Session, company_id: int, employee_ids: Iterable[int]) -> Dict[int, int]:
    ids = sorted(set(employee_ids))
    if not ids:
        return {}
    return {e: u for e, u in db.query(Employee.id, Employee.user_id).filter(
        Employee.company_id == company_id, Employee.id.in_(ids), Employee.user_id.isnot(None))}


def _delegator_user_id(db: Session, request: ApprovalRequest, step, actor_user_id: int) -> Optional[int]:
    """The delegator on whose behalf a delegate decided ``step`` (an eligible approver of that step)."""
    actor = db.query(Employee).filter(Employee.company_id == request.company_id,
                                      Employee.user_id == actor_user_id).first()
    if actor is None:
        return None
    ctx = approvals._Context(db, request.company_id)
    delegators = org_service.active_delegators_for(db, actor.id, on=ctx.today, module=request.module)
    users = _user_ids_of_employees(db, request.company_id, delegators)
    for employee_id in delegators:
        delegator = ctx.principal_for_employee(employee_id)
        if delegator is not None and approvals._is_step_approver(ctx, request, step, delegator):
            return delegator.user_id
    return next((users[e] for e in delegators if e in users), None)


def _record_decisions(db: Session, run: PayrollRun, request: ApprovalRequest) -> List[PayrollApproval]:
    """Write a ``payroll_approvals`` row for every decided step of ``request`` not recorded yet (idempotent)."""
    live = {(a.step_order, a.approver_user_id) for a in db.query(PayrollApproval).filter(
        PayrollApproval.company_id == run.company_id, PayrollApproval.run_id == run.id,
        PayrollApproval.run_employee_id.is_(None), PayrollApproval.cleared_at.is_(None),
        PayrollApproval.approval_request_id == request.id)}
    written: List[PayrollApproval] = []
    for step in request.steps:
        if step.status not in ("approved", "rejected") or step.acted_by_user_id is None:
            continue
        if (step.step_order, step.acted_by_user_id) in live:
            continue
        acted_as = step.acted_as if step.acted_as in ("approver", "delegate") else "approver"
        on_behalf = _delegator_user_id(db, request, step, step.acted_by_user_id) if acted_as == "delegate" else None
        if acted_as == "delegate" and on_behalf is None:
            acted_as = "approver"
        decision = "APPROVED" if step.status == "approved" else "REJECTED"
        row = PayrollApproval(
            company_id=run.company_id, run_id=run.id, run_employee_id=None, approval_request_id=request.id,
            step_order=step.step_order, approver_user_id=step.acted_by_user_id, acted_as=acted_as,
            on_behalf_of_user_id=on_behalf, decision=decision,
            comment=step.remarks or ("Rejected" if decision == "REJECTED" else None),
            acted_at=step.acted_at or _now())
        db.add(row)
        written.append(row)
    db.flush()
    return written


def _live_approvers(db: Session, run: PayrollRun) -> Set[int]:
    """Users whose live run-level approval (directly or on their behalf) is recorded."""
    found: Set[int] = set()
    for approver, behalf in db.query(PayrollApproval.approver_user_id, PayrollApproval.on_behalf_of_user_id).filter(
            PayrollApproval.company_id == run.company_id, PayrollApproval.run_id == run.id,
            PayrollApproval.run_employee_id.is_(None), PayrollApproval.cleared_at.is_(None),
            PayrollApproval.decision == "APPROVED"):
        found.add(approver)
        if behalf is not None:
            found.add(behalf)
    return found


def self_lines_pending(db: Session, run: PayrollRun) -> List[dict]:
    """Lines of the run's approvers (EC-RUN-06) that still wait for another approver."""
    users = _live_approvers(db, run)
    if not users:
        return []
    employees = {e: u for e, u in db.query(Employee.id, Employee.user_id).filter(
        Employee.company_id == run.company_id, Employee.user_id.in_(sorted(users)))}
    if not employees:
        return []
    rows = db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.employee_id.in_(sorted(employees)), PayrollRunEmployee.status.in_(FINALIZABLE),
        PayrollRunEmployee.self_line_approved_by_user_id.is_(None)).order_by(PayrollRunEmployee.id).all()
    return [{"run_employee_id": r.id, "employee_id": r.employee_id, "approver_user_id": employees[r.employee_id]}
            for r in rows]


def _own_lines(db: Session, run: PayrollRun, employee_ids: Iterable[Optional[int]]) -> List[int]:
    ids = sorted({e for e in employee_ids if e is not None})
    if not ids:
        return []
    return [rid for (rid,) in db.query(PayrollRunEmployee.id).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        PayrollRunEmployee.employee_id.in_(ids), PayrollRunEmployee.status != "EXCLUDED")]


def _apply_terminal(db: Session, principal: Optional[Principal], run: PayrollRun, request: ApprovalRequest) -> None:
    """The engine reached a terminal outcome for the run's request: record it and move the run."""
    if run.status != sm.RunStatus.APPROVAL_PENDING.value or run.approval_request_id not in (None, request.id):
        raise conflict(f"The run is {run.status}; this approval request no longer applies.", "APPROVAL_STALE")
    if principal is None:
        raise forbidden("A pay run approval needs an approver.")
    if request.status == approvals.APPROVED:
        _ensure_not_stale(run)
        _ensure_separation(db, principal, run)
        recorded = _record_decisions(db, run, request)
        own = _own_lines(db, run, (principal.employee_id,
                                   *[_employee_of_user(db, run.company_id, r.on_behalf_of_user_id) for r in recorded]))
        runs.transition(db, principal, run, "APPROVE_FINAL", SEPARATION_OF_DUTIES=True, NO_SELF_PAY_APPROVAL=True,
                        LAST_STEP=True, details={"approval_request_id": request.id,
                                                 "steps": [r.step_order for r in recorded],
                                                 "self_line_pending": own})
    elif request.status == approvals.REJECTED:
        recorded = _record_decisions(db, run, request)
        reason = next((r.comment for r in recorded if r.decision == "REJECTED"), None) or "Rejected"
        runs.transition(db, principal, run, "REJECT", reason=reason,
                        details={"approval_request_id": request.id})
    else:
        raise conflict("A pay run approval is withdrawn by reopening or cancelling the run.", "USE_REOPEN")


def _employee_of_user(db: Session, company_id: int, user_id: Optional[int]) -> Optional[int]:
    if user_id is None:
        return None
    return db.query(Employee.id).filter(Employee.company_id == company_id, Employee.user_id == user_id).scalar()


@approvals.on_decided(MODULE, SUBJECT)
def _on_decided(db: Session, principal: Optional[Principal], request: ApprovalRequest) -> None:
    """Terminal decisions on a run's request (from approve/reject here or from the generic inbox)."""
    run = runs.load_run(db, request.company_id, request.subject_id, lock=True)
    _apply_terminal(db, principal, run, request)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def submit_run(db: Session, principal: Principal, run_id: int, *, comment: Optional[str] = None,
               expected_version: Optional[int] = None) -> PayrollRun:
    """REVIEW_REQUIRED -> APPROVAL_PENDING with the approval request opened (3.5 §5.6)."""
    run = runs.get_run_for(db, principal, run_id, REVIEW, lock=True)
    _ensure_action(run, "SUBMIT_FOR_APPROVAL")
    _check_version(run, expected_version)
    _ensure_not_stale(run)
    pending = unacknowledged_variance(db, run)
    if pending:
        raise AppError(409, f"{pending} variance item(s) must be acknowledged (with a comment) before submission.",
                       "VARIANCE_NOT_ACKNOWLEDGED")
    cleared = runs._clear_approvals(db, principal, run, "Resubmitted for approval") \
        if db.query(PayrollApproval.id).filter(PayrollApproval.company_id == run.company_id,
                                               PayrollApproval.run_id == run.id,
                                               PayrollApproval.cleared_at.is_(None)).first() else 0
    db.flush()  # the transition reloads the locked run
    run = runs.transition(db, principal, run, "SUBMIT_FOR_APPROVAL", VARIANCE_FLAGS_ACKNOWLEDGED=True,
                          details={"comment": (comment or "").strip() or None, "previous_round_cleared": cleared})
    separated = sod_enabled(db, run)
    makers = sorted(preparer_user_ids(db, run)) if separated else []
    net = _net_total(run)
    request = approvals.submit(
        db, principal if separated else None, module=MODULE, subject_type=SUBJECT, subject_id=run.id,
        amount=net, currency=run.currency if net is not None else None,
        context={"legal_entity_id": run.legal_entity_id, "pay_group_id": run.pay_group_id, "run_number": run.number,
                 "label": run.label, "maker_user_ids": makers, "run_chain_id": _chain_id(db, run),
                 "separation_of_duties": separated, "submitted_by_user_id": principal.user_id,
                 "comment": (comment or "").strip() or None})
    run.approval_request_id = request.id
    db.flush()
    _audit(db, principal, "RUN_APPROVAL_REQUESTED", f"Run {run.number} sent for approval (request #{request.id})",
           "payroll_run", run.id, run.company_id, approval_request_id=request.id,
           pending_reason=(request.context or {}).get("pending_reason"), separation_of_duties=separated)
    return run


def approve_run(db: Session, principal: Principal, run_id: int, *, comment: Optional[str] = None,
                expected_version: Optional[int] = None, expected_step: Optional[int] = None) -> dict:
    """Approve the current step; the last step moves the run to APPROVED (3.5 §5.7, §11)."""
    run = runs.get_run_for(db, principal, run_id, APPROVE, lock=True)
    _ensure_action(run, "APPROVE_STEP")
    _check_version(run, expected_version)
    _ensure_not_stale(run)
    _ensure_separation(db, principal, run)
    request = _pending_request(db, run)
    decided = approvals.decide(db, principal, request.id, "APPROVE", comment, expected_step)
    run = runs.load_run(db, run.company_id, run.id, lock=True)
    own = _own_lines(db, run, (principal.employee_id,))
    if decided.status == approvals.PENDING:
        recorded = _record_decisions(db, run, decided)
        own = _own_lines(db, run, (principal.employee_id, *[
            _employee_of_user(db, run.company_id, r.on_behalf_of_user_id) for r in recorded]))
        run = runs.transition(db, principal, run, "APPROVE_STEP", SEPARATION_OF_DUTIES=True,
                              NO_SELF_PAY_APPROVAL=True, NOT_LAST_STEP=True,
                              details={"approval_request_id": decided.id,
                                       "steps": [r.step_order for r in recorded], "self_line_pending": own,
                                       "next_step": decided.current_step})
    return {"run": run, "request": decided, "self_line_pending": own}


def reject_run(db: Session, principal: Principal, run_id: int, *, reason: str,
               expected_version: Optional[int] = None, expected_step: Optional[int] = None) -> PayrollRun:
    """Reject with a reason: APPROVAL_PENDING -> REJECTED (3.5 §5.7)."""
    reason = runs._reason(reason)
    run = runs.get_run_for(db, principal, run_id, APPROVE, lock=True)
    _ensure_action(run, "REJECT")
    _check_version(run, expected_version)
    _ensure_separation(db, principal, run)
    request = _pending_request(db, run)
    approvals.decide(db, principal, request.id, "REJECT", reason, expected_step)
    return runs.load_run(db, run.company_id, run.id)


def rework_run(db: Session, principal: Principal, run_id: int, *, note: Optional[str] = None,
               expected_version: Optional[int] = None) -> PayrollRun:
    """REJECTED -> REVIEW_REQUIRED; the rejected round's decisions are cleared (kept as history)."""
    run = runs.get_run_for(db, principal, run_id, CALCULATE, lock=True)
    _ensure_action(run, "REWORK")
    _check_version(run, expected_version)
    cleared = runs._clear_approvals(db, principal, run, "Rejected; returned to review")
    db.flush()  # the transition reloads the locked run
    return runs.transition(db, principal, run, "REWORK", reason=(note or "").strip() or None,
                           details={"approvals_cleared": cleared})


def reopen_run(db: Session, principal: Principal, run_id: int, *, reason: str, target: Optional[str] = None,
               expected_version: Optional[int] = None) -> PayrollRun:
    """Reopen for correction (EC-RUN-16): -> REOPENED_FOR_CORRECTION, then optionally on to ``target``
    (REVIEW_REQUIRED when calculation results exist, or DRAFT to lock the inputs again)."""
    if target is not None and target not in REOPEN_TARGETS:
        raise bad_request("target is REVIEW_REQUIRED or DRAFT.", "INVALID_REOPEN_TARGET")
    reason = runs._reason(reason)
    run = runs.get_run_for(db, principal, run_id, FINALIZE, lock=True)
    _check_version(run, expected_version)
    if run.status != sm.RunStatus.REOPENED_FOR_CORRECTION.value:
        _ensure_action(run, "REOPEN")
        run = runs.transition(db, principal, run, "REOPEN", reason=reason)
    elif target is None:
        raise bad_request("The run is already reopened: choose REVIEW_REQUIRED or DRAFT.", "REOPEN_TARGET_REQUIRED")
    if target == "REVIEW_REQUIRED":
        run = runs.transition(db, principal, run, "RESUME_REVIEW", reason=reason)
    elif target == "DRAFT":
        run = runs.transition(db, principal, run, "RETURN_TO_DRAFT", reason=reason)
    return run


def approve_self_line(db: Session, principal: Principal, run_id: int, run_employee_id: int, *,
                      comment: Optional[str] = None, expected_version: Optional[int] = None) -> PayrollRunEmployee:
    """A different approver approves the line of an approver's own pay (EC-RUN-06)."""
    run = runs.get_run_for(db, principal, run_id, APPROVE, lock=True)
    runs.ensure_editable(run)
    if run.status not in SELF_LINE_STATES:
        raise conflict(f"Lines are approved while the run is in approval; the run is {run.status}.", "RUN_BUSY")
    _ensure_not_stale(run)
    row = runs.get_run_employee_for(db, principal, run, run_employee_id, APPROVE, lock=True)
    if principal.employee_id is not None and principal.employee_id == row.employee_id:
        raise AppError(403, "Nobody approves their own pay.", "SELF_PAY_APPROVAL")
    _ensure_separation(db, principal, run)
    if expected_version is not None and row.row_version != expected_version:
        raise conflict("The employee line was changed by someone else.", "ROW_VERSION_CONFLICT")
    if row.self_line_approved_by_user_id is not None:
        if row.self_line_approved_by_user_id == principal.user_id:
            return row
        raise conflict("Another approver already approved this line.", "SELF_LINE_ALREADY_APPROVED")
    if row.id not in {p["run_employee_id"] for p in self_lines_pending(db, run)}:
        raise conflict("This line does not need a separate approval.", "SELF_LINE_NOT_REQUIRED")
    request = approvals.latest_request(db, MODULE, SUBJECT, run.id)
    now = _now()
    row.self_line_approved_by_user_id = principal.user_id
    row.row_version += 1
    db.add(PayrollApproval(company_id=run.company_id, run_id=run.id, run_employee_id=row.id,
                           approval_request_id=request.id if request is not None else None, step_order=1,
                           approver_user_id=principal.user_id, acted_as="approver", decision="APPROVED",
                           comment=(comment or "").strip() or None, acted_at=now))
    run.row_version += 1
    db.flush()
    _audit(db, principal, RUN_SELF_LINE_APPROVED, f"Approved the own-pay line of employee #{row.employee_id} in run "
           f"{run.number}", "payroll_run", run.id, run.company_id, run_employee_id=row.id,
           employee_id=row.employee_id, comment=(comment or "").strip() or None)
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def approval_json(row: PayrollApproval) -> dict:
    return {"id": row.id, "run_employee_id": row.run_employee_id, "approval_request_id": row.approval_request_id,
            "step_order": row.step_order, "approver_user_id": row.approver_user_id, "acted_as": row.acted_as,
            "on_behalf_of_user_id": row.on_behalf_of_user_id, "decision": row.decision, "comment": row.comment,
            "acted_at": row.acted_at.isoformat() if row.acted_at else None,
            "cleared_at": row.cleared_at.isoformat() if row.cleared_at else None,
            "cleared_reason": row.cleared_reason}


def approvals_of(db: Session, principal: Principal, run_id: int) -> dict:
    """The run's approval history (cleared rounds included) and the lines waiting for another approver."""
    run = runs.get_run_for(db, principal, run_id, runs.READ)
    rows = (db.query(PayrollApproval).filter(PayrollApproval.company_id == run.company_id,
                                             PayrollApproval.run_id == run.id)
            .order_by(PayrollApproval.acted_at, PayrollApproval.id).all())
    visible_lines = {rid for (rid,) in db.query(PayrollRunEmployee.id).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
        runs.employee_scope_clause(db, principal, runs.READ, run, PayrollRunEmployee.employee_id))}
    request = approvals.latest_request(db, MODULE, SUBJECT, run.id)
    return {
        "run_id": run.id, "status": run.status, "row_version": run.row_version,
        "approval_request": approvals.action_result(request) if request is not None else None,
        "separation_of_duties": sod_enabled(db, run),
        "decisions": [approval_json(r) for r in rows if r.run_employee_id is None or r.run_employee_id in visible_lines],
        "self_lines_pending": [p for p in self_lines_pending(db, run) if p["run_employee_id"] in visible_lines],
    }
