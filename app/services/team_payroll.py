"""Team payroll for managers and team leads (owner request 2026-10-29; owner choice "prepare + submit").

* **Who** — ``team_pay:read`` (view the team's payroll status, batches and invoice drafts) and ``team_pay:submit``
  (prepare / submit batches) over people in the manager's scope (direct reports, reporting tree, teams led ...).
  Exact pay amounts need ``team_pay:read_amounts`` (explicit-only, never implied by full access). Payroll's own
  confidential codes are untouched: a manager never runs, approves or commits payroll (EC-SEC-09 kept).
* **Batches** — DRAFT (lines: employee, one-time input component, amount or units, reason; files ≤ 5 MB each) ->
  SUBMITTED (approvals engine, module ``team_inputs``, approvers hold ``payroll:inputs:approve``) -> APPROVED
  (each line becomes an APPROVED wage input in payroll, checked by payroll's own rules under the approver's scope)
  / REJECTED / RETURNED (back to the manager to edit and resubmit) / CANCELLED (withdrawn).
* Nobody approves a batch they prepared or that pays them (engine SoD); every step is audited.

Services never commit; routes commit.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.team_payroll import TeamInputAttachment, TeamInputBatch, TeamInputLine
from app.payroll.services import approvals
from app.services.audit_log_service import AuditLogService

READ, SUBMIT, AMOUNTS = "team_pay:read", "team_pay:submit", "team_pay:read_amounts"
MODULE = SUBJECT = "team_inputs"
AUDIT_MODULE = "Team payroll"
MAX_LINES = 200
MAX_FILE = 5 * 1024 * 1024
ALLOWED_TYPES = ("application/pdf", "image/png", "image/jpeg", "text/csv", "application/vnd.ms-excel",
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _now() -> datetime:
    return clock.utcnow()


def _scope_ids(db: Session, principal: Principal, code: str) -> Optional[set]:
    """Employee ids in the caller's scope of ``code`` (None = everyone in the company)."""
    if not principal.has(code):
        return set()
    if principal.has_company_scope(code):
        return None
    scope = authz.employee_scope(db, principal, code)
    if scope.is_empty:
        return set()
    return {i for (i,) in db.query(Employee.id).filter(Employee.company_id == principal.company_id,
                                                       scope.clause(Employee.id))}


def _covers(db: Session, principal: Principal, code: str, employee_id: int) -> bool:
    return authz.can_access_employee(db, principal, code, employee_id)


def _decimal(value: Any, name: str) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise bad_request(f"{name} must be a number.", "INVALID_FIELDS")


# =============================================================================================== components
def components(db: Session, principal: Principal, on: Optional[date] = None) -> List[dict]:
    """One-time input components a manager may use (INPUT / RATE_X_UNITS, active on ``on``)."""
    from app.models.compensation import PayComponent, PayComponentVersion
    from app.payroll.services.wage_inputs import INPUT_METHODS, UNITS_METHODS

    if not (principal.has(SUBMIT) or principal.has(READ)):
        raise forbidden()
    on = on or clock.company_today(db, principal.company_id)
    rows = (db.query(PayComponent.code, PayComponent.kind, PayComponentVersion.name, PayComponentVersion.calc_method)
            .join(PayComponentVersion, and_(PayComponentVersion.company_id == PayComponent.company_id,
                                            PayComponentVersion.component_id == PayComponent.id))
            .filter(PayComponent.company_id == principal.company_id, PayComponent.status == "ACTIVE",
                    PayComponentVersion.status == "ACTIVE", PayComponentVersion.effective_from <= on,
                    or_(PayComponentVersion.effective_to.is_(None), PayComponentVersion.effective_to >= on),
                    PayComponentVersion.calc_method.in_(INPUT_METHODS + UNITS_METHODS))
            .order_by(PayComponent.code).all())
    seen, out = set(), []
    for code, kind, name, method in rows:
        if code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name, "kind": kind, "units_based": method in UNITS_METHODS})
    return out


# =============================================================================================== team view
def team_view(db: Session, principal: Principal) -> dict:
    """The team's payroll status: each person, their latest run line (status; amounts only with read_amounts),
    open batches and lines waiting, payslip published or not."""
    from app.models.pay_runs import PayrollRun, PayrollRunEmployee

    ids = _scope_ids(db, principal, READ)
    if ids is not None and not ids:
        if not principal.has(READ):
            if principal.has("payroll:inputs:approve"):  # payroll approvers without a team of their own
                return {"items": [], "can_submit": False, "amounts": False, "can_approve": True}
            raise forbidden()
    query = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.is_active.is_(True))
    if ids is not None:
        query = query.filter(Employee.id.in_(ids or [0]))
    if principal.employee_id is not None:
        query = query.filter(Employee.id != principal.employee_id)
    people = query.order_by(Employee.first_name, Employee.last_name).limit(500).all()
    pid = [p.id for p in people] or [0]
    latest: Dict[int, Any] = {}
    for row, run in (db.query(PayrollRunEmployee, PayrollRun)
                     .join(PayrollRun, and_(PayrollRun.company_id == PayrollRunEmployee.company_id,
                                            PayrollRun.id == PayrollRunEmployee.run_id))
                     .filter(PayrollRunEmployee.company_id == principal.company_id,
                             PayrollRunEmployee.employee_id.in_(pid))
                     .order_by(PayrollRun.id)):
        latest[row.employee_id] = (row, run)
    amounts_ids = _scope_ids(db, principal, AMOUNTS)
    pending = dict(db.query(TeamInputLine.employee_id, func.count(TeamInputLine.id))
                   .join(TeamInputBatch, and_(TeamInputBatch.company_id == TeamInputLine.company_id,
                                              TeamInputBatch.id == TeamInputLine.batch_id))
                   .filter(TeamInputLine.company_id == principal.company_id, TeamInputLine.employee_id.in_(pid),
                           TeamInputBatch.status.in_(("DRAFT", "SUBMITTED", "RETURNED")))
                   .group_by(TeamInputLine.employee_id).all())
    items = []
    for person in people:
        entry = latest.get(person.id)
        can_amounts = amounts_ids is None or person.id in (amounts_ids or set())
        item = {"employee_id": person.id, "name": person.full_name, "employee_code": person.employee_code,
                "position": person.position, "pending_lines": int(pending.get(person.id, 0)),
                "last_run": None, "amounts_visible": bool(can_amounts and principal.has(AMOUNTS))}
        if entry is not None:
            row, run = entry
            item["last_run"] = {"run_id": run.id, "run_status": run.status, "line_status": row.status,
                                "currency": row.currency, "finalized": run.finalized_at is not None}
            if item["amounts_visible"]:
                item["last_run"].update({"gross": str(row.gross) if row.gross is not None else None,
                                         "net": str(row.net) if row.net is not None else None})
        items.append(item)
    return {"items": items, "can_submit": principal.has(SUBMIT), "amounts": principal.has(AMOUNTS),
            "can_approve": principal.has("payroll:inputs:approve")}


# =============================================================================================== batches
def _serialize(db: Session, row: TeamInputBatch, principal: Optional[Principal] = None, *, detail: bool = False) -> dict:
    lines = (db.query(TeamInputLine).filter(TeamInputLine.company_id == row.company_id,
                                            TeamInputLine.batch_id == row.id).order_by(TeamInputLine.id).all())
    names = {e.id: e.full_name for e in db.query(Employee).filter(
        Employee.company_id == row.company_id, Employee.id.in_([l.employee_id for l in lines] or [0]))}
    files = (db.query(TeamInputAttachment.id, TeamInputAttachment.file_name, TeamInputAttachment.size_bytes,
                      TeamInputAttachment.content_type)
             .filter(TeamInputAttachment.company_id == row.company_id, TeamInputAttachment.batch_id == row.id).all())
    total = sum((l.amount or Decimal(0)) for l in lines)
    out = {"id": row.id, "title": row.title, "period_start": row.period_start.isoformat(),
           "period_end": row.period_end.isoformat(), "status": row.status, "notes": row.notes,
           "prepared_by_user_id": row.prepared_by_user_id, "approval_request_id": row.approval_request_id,
           "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
           "decided_at": row.decided_at.isoformat() if row.decided_at else None, "decision_note": row.decision_note,
           "line_count": len(lines), "total_amount": str(total), "file_count": len(files),
           "row_version": row.row_version}
    if detail:
        out["lines"] = [{"id": l.id, "employee_id": l.employee_id, "employee_name": names.get(l.employee_id),
                         "component_code": l.component_code,
                         "amount": str(l.amount) if l.amount is not None else None,
                         "units": str(l.units) if l.units is not None else None,
                         "rate": str(l.rate) if l.rate is not None else None, "reason": l.reason,
                         "wage_input_id": l.wage_input_id, "problem": l.problem} for l in lines]
        out["files"] = [{"id": f.id, "file_name": f.file_name, "size_bytes": f.size_bytes, "content_type": f.content_type}
                        for f in files]
    if principal is not None:
        mine = row.prepared_by_user_id == principal.user_id
        out["can"] = {"edit": mine and row.status in ("DRAFT", "RETURNED"),
                      "submit": mine and row.status in ("DRAFT", "RETURNED") and bool(lines),
                      "withdraw": mine and row.status == "SUBMITTED",
                      "decide": (not mine) and row.status == "SUBMITTED" and principal.has("payroll:inputs:approve")}
    return out


def _load(db: Session, principal: Principal, batch_id: int, *, lock: bool = False) -> TeamInputBatch:
    query = db.query(TeamInputBatch).filter(TeamInputBatch.company_id == principal.company_id,
                                            TeamInputBatch.id == batch_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Batch")
    is_mine = row.prepared_by_user_id == principal.user_id
    if not (is_mine or principal.has("payroll:inputs:approve") or principal.has_company_scope(READ)):
        if not principal.has(READ):
            raise not_found("Batch")
        lines = [l for (l,) in db.query(TeamInputLine.employee_id).filter(TeamInputLine.batch_id == row.id)]
        if not lines or not all(_covers(db, principal, READ, e) for e in lines):
            raise not_found("Batch")
    return row


def list_batches(db: Session, principal: Principal, *, box: str = "mine", status: Optional[str] = None) -> dict:
    query = db.query(TeamInputBatch).filter(TeamInputBatch.company_id == principal.company_id)
    if box == "mine":
        if not (principal.has(SUBMIT) or principal.has(READ)):
            raise forbidden()
        query = query.filter(TeamInputBatch.prepared_by_user_id == principal.user_id)
    elif box == "approvals":
        if not principal.has("payroll:inputs:approve"):
            raise forbidden()
        query = query.filter(TeamInputBatch.prepared_by_user_id != principal.user_id)
        if not status:
            query = query.filter(TeamInputBatch.status == "SUBMITTED")
    elif box == "all":
        if not (principal.has_company_scope(READ) or principal.has("payroll:inputs:approve")):
            raise forbidden()
    else:
        raise bad_request("Unknown list.", "INVALID_FIELDS")
    if status:
        query = query.filter(TeamInputBatch.status == status)
    rows = query.order_by(TeamInputBatch.id.desc()).limit(200).all()
    return {"items": [_serialize(db, r, principal) for r in rows]}


def get(db: Session, principal: Principal, batch_id: int) -> dict:
    return _serialize(db, _load(db, principal, batch_id), principal, detail=True)


def _check_period(start: Any, end: Any) -> tuple:
    try:
        s, e = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except ValueError:
        raise bad_request("Use dates as YYYY-MM-DD.", "INVALID_FIELDS")
    if e < s or (e - s).days > 62:
        raise bad_request("The period is one pay period (at most 62 days).", "INVALID_RANGE")
    return s, e


def create(db: Session, principal: Principal, body: Mapping[str, Any]) -> TeamInputBatch:
    if not principal.has(SUBMIT):
        raise forbidden()
    title = (body.get("title") or "").strip()
    if len(title) < 3:
        raise bad_request("Name the batch.", "INVALID_FIELDS")
    start, end = _check_period(body.get("period_start"), body.get("period_end"))
    row = TeamInputBatch(company_id=principal.company_id, title=title[:120], period_start=start, period_end=end,
                         status="DRAFT", notes=(body.get("notes") or "")[:2000] or None,
                         prepared_by_user_id=principal.user_id, prepared_by_employee_id=principal.employee_id)
    db.add(row)
    db.flush()
    if body.get("lines"):
        set_lines(db, principal, row.id, body["lines"])
    AuditLogService.log(db, principal, AUDIT_MODULE, "TEAM_BATCH_CREATED", f"Team payroll batch \"{row.title}\" created",
                        "team_input_batch", row.id)
    return row


def _editable(db: Session, principal: Principal, batch_id: int) -> TeamInputBatch:
    row = _load(db, principal, batch_id, lock=True)
    if row.prepared_by_user_id != principal.user_id:
        raise forbidden("Only the person who prepared the batch edits it.")
    if row.status not in ("DRAFT", "RETURNED"):
        raise AppError(409, "Only a draft or returned batch can change.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": row.status}})
    return row


def update(db: Session, principal: Principal, batch_id: int, body: Mapping[str, Any]) -> TeamInputBatch:
    row = _editable(db, principal, batch_id)
    if body.get("title") is not None:
        title = str(body["title"]).strip()
        if len(title) < 3:
            raise bad_request("Name the batch.", "INVALID_FIELDS")
        row.title = title[:120]
    if body.get("period_start") or body.get("period_end"):
        row.period_start, row.period_end = _check_period(body.get("period_start") or row.period_start,
                                                         body.get("period_end") or row.period_end)
    if "notes" in body:
        row.notes = (body.get("notes") or "")[:2000] or None
    if body.get("lines") is not None:
        set_lines(db, principal, row.id, body["lines"])
    row.row_version += 1
    db.flush()
    return row


def set_lines(db: Session, principal: Principal, batch_id: int, lines: Iterable[Mapping[str, Any]]) -> None:
    """Replace the lines (checked: people in the caller's team_pay:submit scope, known one-time components)."""
    row = _editable(db, principal, batch_id)
    lines = list(lines)
    if len(lines) > MAX_LINES:
        raise bad_request(f"At most {MAX_LINES} lines per batch.", "TOO_MANY_ITEMS")
    allowed = {c["code"]: c for c in components(db, principal, row.period_end)}
    clean = []
    for n, line in enumerate(lines, start=1):
        try:
            employee_id = int(line.get("employee_id"))
        except (TypeError, ValueError):
            raise bad_request(f"Line {n}: choose the employee.", "INVALID_FIELDS")
        if principal.employee_id == employee_id:
            raise forbidden(f"Line {n}: you cannot prepare your own pay.")
        if not _covers(db, principal, SUBMIT, employee_id):
            raise not_found("Employee")
        code = str(line.get("component_code") or "")
        if code not in allowed:
            raise bad_request(f"Line {n}: {code or 'the component'} is not a one-time input component.",
                              "COMPONENT_NOT_FOUND")
        amount, units, rate = (_decimal(line.get("amount"), "Amount"), _decimal(line.get("units"), "Units"),
                               _decimal(line.get("rate"), "Rate"))
        if allowed[code]["units_based"] and units is None:
            raise bad_request(f"Line {n}: {code} needs units.", "INVALID_FIELDS")
        if not allowed[code]["units_based"] and amount is None:
            raise bad_request(f"Line {n}: {code} needs an amount.", "INVALID_FIELDS")
        reason = (line.get("reason") or "").strip()
        if len(reason) < 3:
            raise bad_request(f"Line {n}: give a reason.", "REASON_REQUIRED")
        clean.append(TeamInputLine(company_id=row.company_id, batch_id=row.id, employee_id=employee_id,
                                   component_code=code, amount=amount, units=units, rate=rate, reason=reason[:500]))
    db.query(TeamInputLine).filter(TeamInputLine.company_id == row.company_id,
                                   TeamInputLine.batch_id == row.id).delete(synchronize_session=False)
    for line in clean:
        db.add(line)
    db.flush()


def add_file(db: Session, principal: Principal, batch_id: int, name: str, content_type: str, data: bytes
             ) -> TeamInputAttachment:
    row = _editable(db, principal, batch_id)
    if not data or len(data) > MAX_FILE:
        raise bad_request("Files are up to 5 MB.", "FILE_TOO_LARGE")
    if content_type not in ALLOWED_TYPES:
        raise bad_request("Attach PDF, PNG, JPEG, CSV or Excel files.", "FILE_TYPE")
    count = db.query(func.count(TeamInputAttachment.id)).filter(TeamInputAttachment.batch_id == row.id).scalar()
    if count >= 10:
        raise bad_request("At most 10 files per batch.", "TOO_MANY_ITEMS")
    file = TeamInputAttachment(company_id=row.company_id, batch_id=row.id, file_name=(name or "file")[:200],
                               content_type=content_type, size_bytes=len(data),
                               sha256=hashlib.sha256(data).hexdigest(), content=data,
                               uploaded_by_user_id=principal.user_id)
    db.add(file)
    db.flush()
    AuditLogService.log(db, principal, AUDIT_MODULE, "TEAM_BATCH_FILE_ADDED", f"File {file.file_name} added",
                        "team_input_batch", row.id, details={"sha256": file.sha256, "bytes": file.size_bytes})
    return file


def get_file(db: Session, principal: Principal, batch_id: int, file_id: int) -> TeamInputAttachment:
    _load(db, principal, batch_id)
    file = db.query(TeamInputAttachment).filter(TeamInputAttachment.company_id == principal.company_id,
                                                TeamInputAttachment.batch_id == batch_id,
                                                TeamInputAttachment.id == file_id).first()
    if file is None:
        raise not_found("File")
    return file


def remove_file(db: Session, principal: Principal, batch_id: int, file_id: int) -> None:
    row = _editable(db, principal, batch_id)
    db.query(TeamInputAttachment).filter(TeamInputAttachment.company_id == row.company_id,
                                         TeamInputAttachment.batch_id == row.id,
                                         TeamInputAttachment.id == file_id).delete(synchronize_session=False)
    db.flush()


def submit(db: Session, principal: Principal, batch_id: int) -> TeamInputBatch:
    row = _editable(db, principal, batch_id)
    lines = db.query(TeamInputLine).filter(TeamInputLine.batch_id == row.id).all()
    if not lines:
        raise bad_request("Add at least one line before submitting.", "LINES_REQUIRED")
    for line in lines:  # scope may have changed since drafting
        if not _covers(db, principal, SUBMIT, line.employee_id):
            raise not_found("Employee")
    total = sum((l.amount or Decimal(0)) for l in lines)
    subject = lines[0].employee_id if len({l.employee_id for l in lines}) == 1 else None
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=SUBJECT, subject_id=row.id, subject_employee_id=subject,
        context={"title": row.title, "lines": len(lines), "people": len({l.employee_id for l in lines}),
                 "period": f"{row.period_start.isoformat()}..{row.period_end.isoformat()}",
                 "total_amount": str(total), "maker_user_ids": [principal.user_id]})
    row.status, row.submitted_at, row.approval_request_id = "SUBMITTED", _now(), request.id
    row.decision_note = None
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, AUDIT_MODULE, "TEAM_BATCH_SUBMITTED", f"Team payroll batch \"{row.title}\" "
                        f"submitted ({len(lines)} lines)", "team_input_batch", row.id,
                        details={"approval_request_id": request.id})
    from app.services import integrations

    integrations.emit(db, row.company_id, "team_batch.submitted", f"{row.id}:{request.id}",
                      "Team payroll batch submitted",
                      f"\"{row.title}\" ({len(lines)} lines) is waiting for payroll approval.",
                      "/payroll/team-inputs?tab=approvals")
    return row


def withdraw(db: Session, principal: Principal, batch_id: int) -> TeamInputBatch:
    row = _load(db, principal, batch_id, lock=True)
    if row.prepared_by_user_id != principal.user_id:
        raise forbidden("Only the person who prepared the batch withdraws it.")
    if row.status != "SUBMITTED":
        raise AppError(409, "Only a submitted batch can be withdrawn.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": row.status}})
    if row.approval_request_id:
        approvals.cancel(db, principal, row.approval_request_id, "Withdrawn by the preparer")
    row.status = "CANCELLED"
    row.row_version += 1
    db.flush()
    return row


def decide(db: Session, principal: Principal, batch_id: int, decision: str, note: Optional[str]) -> TeamInputBatch:
    """Approve / reject / return through the engine (the callback applies the result)."""
    row = _load(db, principal, batch_id)
    if row.status != "SUBMITTED" or not row.approval_request_id:
        raise AppError(409, "This batch is not awaiting a decision.", "INVALID_TRANSITION",
                       extra={"details": {"current_status": row.status}})
    if row.prepared_by_user_id == principal.user_id:
        raise forbidden("You cannot decide a batch you prepared.")
    paid = {e for (e,) in db.query(TeamInputLine.employee_id).filter(TeamInputLine.batch_id == row.id)}
    if principal.employee_id is not None and principal.employee_id in paid:
        raise forbidden("You cannot decide a batch that pays you.")
    approvals.decide(db, principal, row.approval_request_id, decision, note)
    db.refresh(row)
    return row


def _apply(db: Session, principal: Principal, row: TeamInputBatch) -> int:
    """Write each line as an APPROVED wage input (payroll's own checks, the approver's scope)."""
    from app.payroll import state_machines as sm
    from app.payroll.services import wage_inputs

    created = 0
    for line in db.query(TeamInputLine).filter(TeamInputLine.batch_id == row.id).order_by(TeamInputLine.id):
        data = wage_inputs.InputData(employee_id=line.employee_id, component_code=line.component_code,
                                     period_start=row.period_start, period_end=row.period_end, amount=line.amount,
                                     units=line.units, rate=line.rate, reason=f"{line.reason} (team batch {row.id})",
                                     external_ref=f"team-batch-{row.id}-{line.id}", source="MANUAL")
        checked = wage_inputs.check_input(db, principal, data, permission=wage_inputs.APPROVE)
        inputs = wage_inputs._insert(db, principal, checked, company_id=row.company_id)
        now = _now()
        for item in inputs:
            item.status, item.submitted_at, item.decided_at = sm.WageInputStatus.APPROVED, now, now
            item.row_version += 1
        line.wage_input_id = inputs[0].id
        created += len(inputs)
    db.flush()
    return created


@approvals.on_decided(MODULE, SUBJECT)
def _decided(db: Session, principal: Optional[Principal], request) -> None:
    row = (db.query(TeamInputBatch).filter(TeamInputBatch.company_id == request.company_id,
                                           TeamInputBatch.id == request.subject_id).with_for_update().first())
    if row is None or row.approval_request_id != request.id or row.status != "SUBMITTED":
        return
    status = request.status
    row.decided_at = _now()
    row.decided_by_user_id = principal.user_id if principal is not None else None
    acted = [s for s in request.steps if s.acted_at is not None and s.remarks]
    row.decision_note = (max(acted, key=lambda s: s.acted_at).remarks[:500] if acted else None)
    if status == "APPROVED":
        if principal is None:
            raise conflict("A team batch is committed by a person.", "APPROVER_REQUIRED")
        count = _apply(db, principal, row)
        row.status = "APPROVED"
        AuditLogService.log(db, principal, AUDIT_MODULE, "TEAM_BATCH_APPROVED",
                            f"Team payroll batch \"{row.title}\" committed to payroll ({count} inputs)",
                            "team_input_batch", row.id)
    elif status == "REJECTED":
        row.status = "REJECTED"
    elif status == "RETURNED":
        row.status = "RETURNED"
    elif status == "CANCELLED":
        row.status = "CANCELLED"
    row.row_version += 1
    db.flush()
    from app.models.user import User
    from app.services.notification_service import NotificationService

    if status in ("APPROVED", "REJECTED", "RETURNED"):
        from app.services import integrations

        integrations.emit(db, row.company_id, "team_batch.decided", f"{row.id}:{request.id}",
                          f"Team payroll batch {status.lower()}", f"\"{row.title}\" was {status.lower()}.",
                          f"/payroll/team-inputs?batch={row.id}")
    preparer = db.get(User, row.prepared_by_user_id)
    if preparer is not None and status in ("APPROVED", "REJECTED", "RETURNED"):
        NotificationService.create(db, preparer.id, f"Team payroll batch {status.lower()}",
                                   f"\"{row.title}\" was {status.lower()}.", "payroll",
                                   f"/payroll/team-inputs?batch={row.id}")
