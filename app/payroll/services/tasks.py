"""The minimal tasks module: tasks, assignees, evidence, approval, rate cards and payroll reads (3.4 §10).

* ``tasks.enabled`` (tenant switch) gates the module's API. Task pay itself (payroll.task_pay.enabled)
  is applied by the engine; tasks are tracked either way (§10.9).
* Lifecycle per the TASK machine of app.payroll.state_machines: DRAFT -> SUBMITTED -> APPROVED
  (-> PAID by a finalized run -> REVERSED) or REJECTED or DISPUTED (excluded until resolved).
* task:manage covering every assignee creates, edits (DRAFT only), adds evidence and submits; task:read,
  task:manage or task:approve covering any assignee sees a task. Several assignees need the policy's
  allow_split and percentages totalling exactly 100.00 (EC-TSK-05). Evidence is required before submit
  and approval when any assignee's TASK_PAY_POLICY says requires_evidence; files live in private storage.
* Approval goes through the approvals engine (module "task", task:approve). Assignees are barred from
  approving (maker list), and the final approver must cover every assignee. An approver may accept part
  of the quantity: accepted_quantity <= quantity (EC-TSK-04). external_ref is unique per tenant
  (EC-TSK-01). A submitted task cannot be withdrawn (the machine has no such transition).
* Rate cards: task_type x criteria (grade code, skill, branch code as location) x effective dates ->
  rate per unit + currency; rows with the same type and criteria never overlap.
* Pay figures (the per-unit rate) are shown only to viewers holding payroll:configuration:read/write, or
  payroll:inputs:read covering every assignee; managers approve quantities, not pay (contract §25, F-42).
  The same rule guards setting a rate on create/edit (PAY_FIELDS_NOT_PERMITTED).
* approved_tasks() and rate_card_rows() return plain dicts for the run snapshot; mark_task_shares_paid() is
  called by run finalization. Approvals and reversals append to source_change_log (record type "task",
  one row per assignee, 3.4 §16) so draft runs see them.

Services never commit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import exists, func, or_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, selectinload

from app.config import settings as app_settings
from app.core import authz, clock, storage
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.approvals import ApprovalRequest
from app.models.employee import Employee
from app.models.org import Branch, EmployeeAssignment
from app.models.pay_org import Grade, PayGroup
from app.models.work_items import RATE_CARD_CRITERIA, Task, TaskAssignee, TaskEvidence, TaskRateCard
from app.payroll import audit as payroll_audit
from app.payroll import currencies
from app.payroll import state_machines as sm
from app.payroll import time_pay_tasks as tpt
from app.payroll.money import decimal_str, is_storable, minor_unit, to_decimal
from app.payroll.money_errors import MoneyError
from app.payroll.services import approvals
from app.payroll.services import policies as policy_service
from app.payroll.services import settings as settings_engine
from app.payroll.services import time_inputs
from app.payroll.time_pay_common import TimePayError
from app.services.audit_log_service import AuditLogService

MODULE = "task"
SUBJECT_TYPE = "task"
READ, MANAGE, APPROVE = "task:read", "task:manage", "task:approve"
CONFIG_READ, CONFIG_WRITE = "payroll:configuration:read", "payroll:configuration:write"
INPUTS_READ = "payroll:inputs:read"
# Who sees task rows (read only): task permissions, and payroll:inputs:read ("View wage inputs, overtime, task pay
# and arrears that feed payroll") within its scope, like overtime.READ_PERMISSIONS.
VISIBLE_PERMISSIONS = (READ, MANAGE, APPROVE, INPUTS_READ)
RATE_CARD_READ_PERMISSIONS = (CONFIG_READ, CONFIG_WRITE, INPUTS_READ, MANAGE)
# Viewers who may see per-unit rates at any scope (configuration holders read the rate cards anyway).
PAY_CONFIG_PERMISSIONS = (CONFIG_READ, CONFIG_WRITE)
PAY_FIELDS_NOT_PERMITTED = "PAY_FIELDS_NOT_PERMITTED"
SWITCH = "tasks.enabled"
EVIDENCE_CATEGORY = "task-evidence"
MAX_ASSIGNEES = 20
MAX_EVIDENCE = 20
MAX_LIST = 200
DATE_FIELDS = ("completed_on", "approved_on", "due_date", "created_at")
HUNDRED = Decimal(100)
QUANTITY_STEP = Decimal("0.0001")
ATTRIBUTIONS = tpt.ATTRIBUTIONS

DRAFT, SUBMITTED, APPROVED, REJECTED, DISPUTED, PAID, REVERSED = (s.value for s in sm.TaskStatus)

# File signatures accepted as evidence: (prefix, extension, media type).
EVIDENCE_TYPES = (
    (b"%PDF-", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
)

TASK_CREATED = "TASK_CREATED"
TASK_UPDATED = "TASK_UPDATED"
TASK_SUBMITTED = "TASK_SUBMITTED"
TASK_APPROVED = "TASK_APPROVED"
TASK_REJECTED = "TASK_REJECTED"
TASK_DISPUTED = "TASK_DISPUTED"
TASK_PAID = "TASK_PAID"
TASK_REVERSED = "TASK_REVERSED"
TASK_PARTIALLY_ACCEPTED = "TASK_PARTIALLY_ACCEPTED"
TASK_EVIDENCE_ADDED = "TASK_EVIDENCE_ADDED"
TASK_EVIDENCE_REMOVED = "TASK_EVIDENCE_REMOVED"
TASK_RATE_CARD_CREATED = "TASK_RATE_CARD_CREATED"
TASK_RATE_CARD_ENDED = "TASK_RATE_CARD_ENDED"


# --------------------------------------------------------------------------- helpers


def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def module_enabled(db: Session, company_id: int, on: date | None = None) -> bool:
    try:
        resolved = settings_engine.resolve(db, company_id, SWITCH, on=on or _today(db, company_id))
    except settings_engine.SettingConfigError:
        return False
    return resolved.value is True


def require_enabled(db: Session, company_id: int) -> None:
    if not module_enabled(db, company_id):
        raise AppError(403, "The tasks module is turned off for this company.", "TASKS_DISABLED")


def _audit(db: Session, principal: Principal | None, action: str, entity_type: str, entity_id: int, company_id: int,
           description: str, **details: Any) -> None:
    AuditLogService.log(
        db, principal, payroll_audit.PAYROLL_INPUTS, action, description, entity_type, entity_id,
        company_id=company_id, actor_type="user" if principal else "system", details=details,
    )


def _task_audit(db: Session, principal: Principal | None, action: str, task: Task, description: str,
                **details: Any) -> None:
    _audit(db, principal, action, "task", task.id, task.company_id, description,
           task_id=task.id, status=task.status, employee_ids=[a.employee_id for a in task.assignees], **details)


def _log_change(db: Session, principal: Principal | None, task: Task, change: str) -> None:
    dates = [d for d in (task.completed_on, task.approved_on) if d is not None]
    for assignee in task.assignees:
        time_inputs.log_change(
            db, task.company_id, record_type="task", record_id=task.id, change=change,
            content=snapshot_json(task, assignee.employee_id), employee_id=assignee.employee_id,
            date_from=min(dates) if dates else None, date_to=max(dates) if dates else None,
            actor_user_id=principal.user_id if principal else None, details={"status": task.status},
        )


def _transition(task: Task, action: str) -> str:
    try:
        return sm.TASK.next_state(task.status, action)
    except sm.InvalidTransition as exc:
        raise conflict(f"This task is {task.status.lower()}; {action.lower().replace('_', ' ')} is not allowed.",
                       exc.code)


def _decimal(value: Any, name: str) -> Decimal:
    try:
        return to_decimal(value)
    except MoneyError as exc:
        raise bad_request(f"{name}: {exc.message}", exc.code)


def _quantity(value: Any, name: str = "quantity", *, positive: bool = True) -> Decimal:
    amount = _decimal(value, name)
    if amount < 0 or (positive and amount == 0):
        raise bad_request(f"{name} must be {'greater than' if positive else 'at least'} zero.", "INVALID_QUANTITY")
    if amount != amount.quantize(QUANTITY_STEP) or amount >= Decimal("100000000"):
        raise bad_request(f"{name} allows at most 4 decimals and 8 digits.", "INVALID_QUANTITY")
    return amount


def _rate(amount: Any, currency: str) -> tuple[Decimal, str]:
    try:
        code = currencies.require_payroll_currency(currency).code
    except MoneyError as exc:
        raise bad_request(exc.message, exc.code)
    value = _decimal(amount, "rate")
    if value < 0:
        raise bad_request("A rate cannot be negative.", "AMOUNT_NEGATIVE")
    if not is_storable(value):
        raise bad_request("A rate allows at most 4 decimals.", "AMOUNT_NOT_STORABLE")
    return value, code


def rate_json(rate: Decimal | None, currency: str | None) -> dict[str, str] | None:
    """A per-unit rate: at least the currency's minor-unit digits, up to the 4 stored decimals."""
    if rate is None or currency is None:
        return None
    exponent = Decimal(rate).normalize().as_tuple().exponent
    places = max(minor_unit(currency), min(4, -exponent) if exponent < 0 else 0)
    return {"amount": format(Decimal(rate).quantize(Decimal(1).scaleb(-places)), "f"), "currency": currency}


def _quantity_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return decimal_str(normalized if normalized.as_tuple().exponent <= 0 else value.quantize(Decimal(1)))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class TaskRules:
    allow_split: bool
    requires_evidence: bool


def task_rules(db: Session, company_id: int, employee_ids: Iterable[int], on: date) -> TaskRules:
    """Combined TASK_PAY_POLICY rules of the assignees: splitting needs every policy to allow it, evidence is
    required when any policy requires it. Without an active policy the registry defaults apply."""
    defaults = tpt.parse_task_pay_policy({}, "USD")
    allow, evidence, seen = True, False, False
    for employee_id in sorted(set(employee_ids)):
        found = policy_service.resolve_for_employee(db, company_id, "TASK_PAY_POLICY", employee_id, on)
        fields = found["version"]["fields"] if found.get("status") == "RESOLVED" else {}
        seen = True
        allow = allow and bool(fields.get("allow_split", defaults.allow_split))
        evidence = evidence or bool(fields.get("requires_evidence", defaults.requires_evidence))
    return TaskRules(allow_split=allow if seen else defaults.allow_split, requires_evidence=evidence)


def _check_split(assignees: Sequence[tuple[int, Decimal]], rules: TaskRules) -> None:
    try:
        tpt.validate_split([tpt.TaskAssignee(str(e), p) for e, p in assignees], allow_split=rules.allow_split)
    except TimePayError as exc:
        raise bad_request(exc.message, exc.code)


def _assignee_input(raw: Sequence[Mapping[str, Any]]) -> list[tuple[int, Decimal]]:
    if not raw:
        raise bad_request("A task needs at least one assignee.", tpt.INVALID_SPLIT)
    if len(raw) > MAX_ASSIGNEES:
        raise bad_request(f"A task has at most {MAX_ASSIGNEES} assignees.", tpt.INVALID_SPLIT)
    result = []
    for item in raw:
        percent = item.get("split_percent")
        result.append((int(item["employee_id"]), _decimal(percent, "split_percent") if percent is not None else HUNDRED))
    return result


def _visible_clause(db: Session, principal: Principal):
    """Tasks with at least one assignee the caller may see."""
    clauses = [authz.employee_scope(db, principal, code).clause(TaskAssignee.employee_id)
               for code in VISIBLE_PERMISSIONS if principal.has(code)]
    if principal.employee_id is not None:
        clauses.append(TaskAssignee.employee_id == principal.employee_id)
    if not clauses:
        return None
    return exists().where(TaskAssignee.company_id == Task.company_id, TaskAssignee.task_id == Task.id, or_(*clauses))


def _load(db: Session, principal: Principal, task_id: int, *, lock: bool = False) -> Task:
    clause = _visible_clause(db, principal)
    if clause is None:
        raise not_found("Task")
    query = (db.query(Task).options(selectinload(Task.assignees), selectinload(Task.evidence))
             .filter(Task.company_id == principal.company_id, Task.id == task_id, clause))
    if lock:
        query = query.with_for_update(of=Task).populate_existing()
    task = query.one_or_none()
    if task is None:
        raise not_found("Task")
    return task


def _ensure_manage(db: Session, principal: Principal, employee_ids: Iterable[int]) -> list[Employee]:
    if not principal.has(MANAGE):
        raise forbidden()
    return authz.ensure_all_employees_access(db, principal, MANAGE, employee_ids)


def _covers_all(db: Session, principal: Principal, permission: str, employee_ids: Iterable[int]) -> bool:
    return all(authz.can_access_employee(db, principal, permission, e) for e in set(employee_ids))


def _not_assignee(principal: Principal, task: Task, verb: str) -> None:
    """Nobody decides on a task that pays them (maker-checker), whatever their approval rights."""
    if principal.employee_id is not None and principal.employee_id in {a.employee_id for a in task.assignees}:
        raise forbidden(f"You cannot {verb} a task you are paid for.")


def _user_ids(db: Session, company_id: int, employee_ids: Iterable[int]) -> list[int]:
    rows = db.query(Employee.user_id).filter(Employee.company_id == company_id,
                                             Employee.id.in_(list(employee_ids) or [0])).all()
    return sorted({u for (u,) in rows if u is not None})


def _flush(db: Session) -> None:
    try:
        with db.begin_nested():
            db.flush()
    except DBAPIError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None) or ""
        code = getattr(exc.orig, "pgcode", None)
        if code == "23505" and "external_ref" in constraint:
            raise conflict("A task with this external reference already exists.", tpt.DUPLICATE_EXTERNAL_REF)
        if code == "23505" and "evidence" in constraint:
            raise conflict("This file is already attached to the task.", "DUPLICATE_EVIDENCE")
        if code == "23P01":
            raise conflict("A rate card row for this task type and criteria overlaps these dates.", "RATE_CARD_OVERLAP")
        if code in ("23514", "23503", "23505"):
            raise bad_request("The task data is not valid.", "INVALID_TASK")
        raise


# --------------------------------------------------------------------------- pay visibility (F-42)


class PayVisibility:
    """Who sees task rates: payroll configuration holders, or payroll:inputs:read covering every assignee.

    Built once per request; ``covers(ids)`` answers with at most one query per call.
    """

    def __init__(self, db: Session, principal: Principal):
        self.db = db
        self.principal = principal
        self.all = any(principal.has(code) for code in PAY_CONFIG_PERMISSIONS)
        self.scope = None
        if not self.all and principal.has(INPUTS_READ):
            scope = authz.employee_scope(db, principal, INPUTS_READ)
            self.all = scope.all_company
            self.scope = None if scope.is_empty or scope.all_company else scope
        self._known: dict[int, bool] = {}

    def preload(self, employee_ids: Iterable[int]) -> None:
        missing = {e for e in employee_ids if e not in self._known}
        if self.all or self.scope is None or not missing:
            return
        found = {e for (e,) in self.db.query(Employee.id).filter(
            Employee.company_id == self.principal.company_id, Employee.id.in_(missing),
            self.scope.clause(Employee.id))}
        self._known.update({e: e in found for e in missing})

    def covers(self, employee_ids: Iterable[int]) -> bool:
        ids = set(employee_ids)
        if self.all:
            return True
        if self.scope is None or not ids:
            return False
        self.preload(ids)
        return all(self._known.get(e, False) for e in ids)


def _require_pay_write(db: Session, principal: Principal, employee_ids: Iterable[int]) -> None:
    if not PayVisibility(db, principal).covers(employee_ids):
        raise AppError(403, "Setting a task rate needs payroll:inputs:read or payroll:configuration:read for every "
                            "assignee; managers record quantities, payroll sets pay.", PAY_FIELDS_NOT_PERMITTED)


# --------------------------------------------------------------------------- serialization


def _rate_warnings(db: Session, task: Task) -> list[dict[str, Any]]:
    """A task rate in another currency than an assignee's pay group needs an FX rule at payroll (EC-TSK-10)."""
    if task.rate_currency is None:
        return []
    today = _today(db, task.company_id)
    ids = [a.employee_id for a in task.assignees]
    rows = (db.query(EmployeeAssignment.employee_id, PayGroup.currency)
            .join(PayGroup, (PayGroup.company_id == EmployeeAssignment.company_id)
                  & (PayGroup.id == EmployeeAssignment.pay_group_id))
            .filter(EmployeeAssignment.company_id == task.company_id, EmployeeAssignment.employee_id.in_(ids or [0]),
                    EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= today,
                    or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= today)).all())
    return [{"code": tpt.CURRENCY_MISMATCH, "employee_id": employee_id,
             "message": f"The task rate is in {task.rate_currency} but the pay currency is {currency}; payroll needs "
                        "an FX rate, otherwise the task is blocked."}
            for employee_id, currency in rows if currency and currency != task.rate_currency]


def serialize(db: Session, task: Task, *, request: ApprovalRequest | None = None,
              names: Mapping[int, str] | None = None, show_pay: bool = True) -> dict[str, Any]:
    """The task as JSON; ``show_pay=False`` strips the rate and its currency warnings (F-42)."""
    names = names or {}
    data = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "project": task.project,
        "category": task.category,
        "task_type": task.task_type,
        "quantity": _quantity_str(task.quantity),
        "accepted_quantity": _quantity_str(task.accepted_quantity),
        "unit": task.unit,
        "rate": rate_json(task.rate, task.rate_currency),
        "due_date": _iso(task.due_date),
        "completed_on": _iso(task.completed_on),
        "status": task.status,
        "external_ref": task.external_ref,
        "approval_request_id": task.approval_request_id,
        "approved_on": _iso(task.approved_on),
        "paid_in_run_id": task.paid_in_run_id,
        "created_by_user_id": task.created_by_user_id,
        "submitted_at": _iso(task.submitted_at),
        "decided_at": _iso(task.decided_at),
        "reversed_at": _iso(task.reversed_at),
        "status_reason": task.status_reason,
        "row_version": task.row_version,
        "assignees": [{"employee_id": a.employee_id, "employee_name": names.get(a.employee_id),
                       "split_percent": format(Decimal(a.split_percent).quantize(Decimal("0.01")), "f"),
                       "paid_in_run_id": a.paid_in_run_id}
                      for a in task.assignees],
        "evidence": [{"id": e.id, "file_name": e.file_name, "mime": e.mime, "bytes": e.bytes, "sha256": e.sha256,
                      "uploaded_at": _iso(e.created_at)} for e in task.evidence],
        "warnings": _rate_warnings(db, task) if show_pay else [],
        "created_at": _iso(task.created_at),
    }
    if not show_pay:
        data["rate"] = None
        data["pay_hidden"] = True
    if request is not None:
        data["approval"] = {"status": request.status, "current_step": request.current_step,
                            "pending_reason": (request.context or {}).get("pending_reason")}
    return data


def action_result(task: Task) -> dict[str, Any]:
    return {"id": task.id, "status": task.status, "row_version": task.row_version,
            "approval_request_id": task.approval_request_id,
            "accepted_quantity": _quantity_str(task.accepted_quantity)}


def _names(db: Session, company_id: int, ids: Iterable[int]) -> dict[int, str]:
    return {e.id: e.full_name for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                                  Employee.id.in_(set(ids) or {0}))}


def _assignee_refs(db: Session, principal: Principal, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Name and code of the assignees the caller may see: themselves, or inside the scope of a task
    permission they hold. A co-assignee outside that scope stays an id only (F-44: no name leaks)."""
    wanted = set(ids)
    clauses = [authz.employee_scope(db, principal, code).clause(Employee.id)
               for code in VISIBLE_PERMISSIONS if principal.has(code)]
    if principal.employee_id is not None:
        clauses.append(Employee.id == principal.employee_id)
    if not wanted or not clauses:
        return {}
    rows = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.id.in_(wanted),
                                     or_(*clauses))
    return {e.id: {"employee_name": e.full_name, "employee_code": e.employee_code} for e in rows}


def _with_refs(item: dict[str, Any], refs: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    for assignee in item["assignees"]:
        ref = refs.get(assignee["employee_id"]) or {}
        assignee["employee_name"] = ref.get("employee_name")
        assignee["employee_code"] = ref.get("employee_code")
    return item


# --------------------------------------------------------------------------- queries


def list_tasks(
    db: Session,
    principal: Principal,
    *,
    status: str | None = None,
    employee_id: int | None = None,
    task_type: str | None = None,
    awaiting: bool = False,
    skip: int = 0,
    limit: int = 50,
    date_from: date | None = None,
    date_to: date | None = None,
    date_field: str = "completed_on",
) -> tuple[int, list[dict[str, Any]]]:
    require_enabled(db, principal.company_id)
    clause = _visible_clause(db, principal)
    if clause is None:
        raise forbidden()
    query = db.query(Task).filter(Task.company_id == principal.company_id, clause)
    if date_from is not None or date_to is not None:
        # Period filter (F-44): tasks whose date_field lies in [date_from, date_to]; no date, no match.
        if date_field not in DATE_FIELDS:
            raise bad_request(f"date_field is one of {', '.join(DATE_FIELDS)}.", "INVALID_DATE_FIELD")
        if date_from is not None and date_to is not None and date_to < date_from:
            raise bad_request("The period ends before it starts.", "INVALID_PERIOD")
        column = getattr(Task, date_field)
        if date_field == "created_at":
            column = func.date(column)
        if date_from is not None:
            query = query.filter(column >= date_from)
        if date_to is not None:
            query = query.filter(column <= date_to)
    if status is not None:
        query = query.filter(Task.status == status)
    if task_type is not None:
        query = query.filter(Task.task_type == task_type)
    if employee_id is not None:
        query = query.filter(exists().where(TaskAssignee.company_id == Task.company_id,
                                            TaskAssignee.task_id == Task.id, TaskAssignee.employee_id == employee_id))
    if awaiting:
        ids = approvals.subjects_awaiting(db, principal, MODULE, SUBJECT_TYPE)
        query = query.filter(Task.id.in_(ids or [0]))
    total = query.count()
    rows = (query.options(selectinload(Task.assignees), selectinload(Task.evidence))
            .order_by(Task.created_at.desc(), Task.id.desc()).offset(skip).limit(min(limit, MAX_LIST)).all())
    refs = _assignee_refs(db, principal, (a.employee_id for t in rows for a in t.assignees))
    requests = {r.id: r for r in db.query(ApprovalRequest).filter(
        ApprovalRequest.company_id == principal.company_id,
        ApprovalRequest.id.in_({t.approval_request_id for t in rows if t.approval_request_id} or {0}))}
    pay = PayVisibility(db, principal)
    pay.preload(a.employee_id for t in rows for a in t.assignees)
    return total, [_with_refs(serialize(db, t, request=requests.get(t.approval_request_id),
                                        show_pay=pay.covers(a.employee_id for a in t.assignees)), refs) for t in rows]


def get_task(db: Session, principal: Principal, task_id: int) -> dict[str, Any]:
    require_enabled(db, principal.company_id)
    task = _load(db, principal, task_id)
    request = db.get(ApprovalRequest, task.approval_request_id) if task.approval_request_id else None
    return _with_refs(serialize(db, task, request=request,
                                show_pay=PayVisibility(db, principal).covers(a.employee_id for a in task.assignees)),
                      _assignee_refs(db, principal, (a.employee_id for a in task.assignees)))


# --------------------------------------------------------------------------- commands


TASK_FIELDS = ("title", "description", "project", "category", "task_type", "quantity", "unit", "rate", "due_date",
               "completed_on", "external_ref", "assignees")


def _apply(db: Session, principal: Principal, task: Task, data: Mapping[str, Any]) -> None:
    for name in ("title", "description", "project", "category", "task_type", "unit", "due_date", "completed_on"):
        if name in data:
            setattr(task, name, data[name])
    if "quantity" in data:
        task.quantity = _quantity(data["quantity"])
    if "rate" in data:
        rate = data["rate"]
        task.rate, task.rate_currency = _rate(rate["amount"], rate["currency"]) if rate is not None else (None, None)
    if "external_ref" in data:
        ref = data["external_ref"]
        if ref is not None and db.query(Task.id).filter(Task.company_id == principal.company_id,
                                                        Task.external_ref == ref, Task.id != task.id).first():
            raise conflict("A task with this external reference already exists.", tpt.DUPLICATE_EXTERNAL_REF)
        task.external_ref = ref
    if task.completed_on is not None and task.completed_on > _today(db, principal.company_id):
        raise bad_request("The completion date cannot be in the future.", "COMPLETED_IN_FUTURE")


def _set_assignees(db: Session, principal: Principal, task: Task, assignees: Sequence[tuple[int, Decimal]]) -> None:
    ids = [e for e, _ in assignees]
    _ensure_manage(db, principal, ids)
    _check_split(assignees, task_rules(db, principal.company_id, ids, _today(db, principal.company_id)))
    current = {a.employee_id: a for a in task.assignees}
    wanted = dict(assignees)
    for employee_id, row in list(current.items()):
        if employee_id not in wanted:
            task.assignees.remove(row)
    for employee_id, percent in assignees:
        if employee_id in current:
            current[employee_id].split_percent = percent
        else:
            task.assignees.append(TaskAssignee(company_id=task.company_id, employee_id=employee_id,
                                               split_percent=percent))


def create_task(db: Session, principal: Principal, data: Mapping[str, Any]) -> Task:
    """Create a DRAFT task for assignees all inside the caller's task:manage scope."""
    require_enabled(db, principal.company_id)
    assignees = _assignee_input(data.get("assignees") or [])
    _ensure_manage(db, principal, [e for e, _ in assignees])
    if data.get("rate") is not None:
        _require_pay_write(db, principal, [e for e, _ in assignees])
    task = Task(company_id=principal.company_id, status=DRAFT, created_by_user_id=principal.user_id, row_version=1)
    _apply(db, principal, task, data)
    _set_assignees(db, principal, task, assignees)
    db.add(task)
    _flush(db)
    _task_audit(db, principal, TASK_CREATED, task, f"Created task #{task.id} \"{task.title}\"",
                after=serialize(db, task))
    return task


def update_task(db: Session, principal: Principal, task_id: int, changes: Mapping[str, Any], *,
                row_version: int | None = None) -> Task:
    require_enabled(db, principal.company_id)
    task = _load(db, principal, task_id, lock=True)
    _ensure_manage(db, principal, [a.employee_id for a in task.assignees])
    if row_version is not None and row_version != task.row_version:
        raise conflict("This task changed since you loaded it.", "STALE_ROW_VERSION")
    if task.status != DRAFT:
        raise conflict("Only a draft task can be edited.", "TASK_NOT_EDITABLE")
    unknown = set(changes) - set(TASK_FIELDS)
    if unknown or not changes:
        raise bad_request("Nothing to change." if not changes else f"Unknown fields: {', '.join(sorted(unknown))}.",
                          "INVALID_FIELDS")
    if "rate" in changes:
        ids = {a.employee_id for a in task.assignees}
        if "assignees" in changes:
            ids |= {e for e, _ in _assignee_input(changes["assignees"] or [])}
        _require_pay_write(db, principal, ids)
    before = serialize(db, task)
    _apply(db, principal, task, changes)
    if "assignees" in changes:
        _set_assignees(db, principal, task, _assignee_input(changes["assignees"] or []))
    task.row_version += 1
    _flush(db)
    _task_audit(db, principal, TASK_UPDATED, task, f"Edited task #{task.id}", before=before,
                after=serialize(db, task))
    return task


def _require_evidence(db: Session, task: Task) -> None:
    rules = task_rules(db, task.company_id, [a.employee_id for a in task.assignees], _today(db, task.company_id))
    if rules.requires_evidence and not task.evidence:
        raise bad_request("Attach evidence before the task is submitted or approved.", "EVIDENCE_REQUIRED")


def _primary_assignee(task: Task) -> int:
    return max(task.assignees, key=lambda a: (Decimal(a.split_percent), -a.employee_id)).employee_id


def submit_task(db: Session, principal: Principal, task_id: int) -> Task:
    """DRAFT -> SUBMITTED (split and evidence guards) and route the task for approval."""
    require_enabled(db, principal.company_id)
    task = _load(db, principal, task_id, lock=True)
    ids = [a.employee_id for a in task.assignees]
    _ensure_manage(db, principal, ids)
    target = _transition(task, "SUBMIT")
    _check_split([(a.employee_id, Decimal(a.split_percent)) for a in task.assignees],
                 task_rules(db, task.company_id, ids, _today(db, task.company_id)))
    _require_evidence(db, task)
    task.status = target
    task.submitted_at = utcnow()
    task.row_version += 1
    _flush(db)
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=SUBJECT_TYPE, subject_id=task.id,
        subject_employee_id=_primary_assignee(task),
        context={"maker_user_ids": _user_ids(db, task.company_id, ids), "task_type": task.task_type,
                 "quantity": _quantity_str(task.quantity), "unit": task.unit, "employee_ids": ids},
    )
    task.approval_request_id = request.id
    _flush(db)
    _task_audit(db, principal, TASK_SUBMITTED, task, f"Submitted task #{task.id} for approval", request_id=request.id)
    return task


def decide_task(
    db: Session,
    principal: Principal,
    task_id: int,
    decision: str,
    *,
    accepted_quantity: Any = None,
    remarks: str | None = None,
    expected_step: int | None = None,
) -> Task:
    """Approve (optionally accepting only part of the quantity, EC-TSK-04) or reject the current step."""
    require_enabled(db, principal.company_id)
    task = _load(db, principal, task_id, lock=True)
    if task.status not in (SUBMITTED, DISPUTED) or task.approval_request_id is None:
        raise conflict("This task is not awaiting approval.", "TASK_NOT_PENDING")
    approve = str(decision).upper() == "APPROVE"
    _not_assignee(principal, task, "approve" if approve else "reject")
    if approve and accepted_quantity is not None:
        accepted = _quantity(accepted_quantity, "accepted_quantity", positive=False)
        if accepted > task.quantity:
            raise bad_request(f"The accepted quantity {_quantity_str(accepted)} exceeds the submitted "
                              f"{_quantity_str(task.quantity)}.", tpt.ACCEPTED_EXCEEDS_SUBMITTED)
        before = task.accepted_quantity
        task.accepted_quantity = accepted
        task.row_version += 1
        _flush(db)
        _task_audit(db, principal, TASK_PARTIALLY_ACCEPTED, task, f"Accepted {_quantity_str(accepted)} of "
                    f"{_quantity_str(task.quantity)} {task.unit} on task #{task.id}",
                    before=_quantity_str(before), after=_quantity_str(accepted))
    if approve:
        _require_evidence(db, task)
    approvals.decide(db, principal, task.approval_request_id, "APPROVE" if approve else "REJECT", remarks,
                     expected_step)
    return task


def _awaiting_caller(db: Session, principal: Principal, task: Task) -> None:
    if task.id not in approvals.subjects_awaiting(db, principal, MODULE, SUBJECT_TYPE):
        raise forbidden("This task is not awaiting your approval.")


def dispute_task(db: Session, principal: Principal, task_id: int, reason: str) -> Task:
    """An approver holds a submitted task as DISPUTED (excluded from pay until approved or rejected)."""
    require_enabled(db, principal.company_id)
    task = _load(db, principal, task_id, lock=True)
    target = _transition(task, "DISPUTE")
    _not_assignee(principal, task, "dispute")
    _awaiting_caller(db, principal, task)
    task.status = target
    task.status_reason = reason
    task.row_version += 1
    _flush(db)
    _task_audit(db, principal, TASK_DISPUTED, task, f"Disputed task #{task.id}", reason=reason)
    return task


def reverse_task(db: Session, principal: Principal, task_id: int, reason: str) -> Task:
    """PAID -> REVERSED (clawback in the next run per policy, EC-TSK-08); never by an assignee."""
    require_enabled(db, principal.company_id)
    task = _load(db, principal, task_id, lock=True)
    ids = [a.employee_id for a in task.assignees]
    if not principal.has(APPROVE):
        raise forbidden()
    _not_assignee(principal, task, "reverse")
    if not _covers_all(db, principal, APPROVE, ids):
        raise not_found("Task")
    target = _transition(task, "REVERSE")
    task.status = target
    task.reversed_at = utcnow()
    task.status_reason = reason
    task.row_version += 1
    _flush(db)
    _task_audit(db, principal, TASK_REVERSED, task, f"Reversed paid task #{task.id}", reason=reason,
                paid_in_run_id=task.paid_in_run_id)
    _log_change(db, principal, task, "UPDATED")
    return task


@approvals.on_decided(MODULE, SUBJECT_TYPE)
def _on_decided(db: Session, principal: Principal | None, request: ApprovalRequest) -> None:
    task = (db.query(Task).options(selectinload(Task.assignees), selectinload(Task.evidence))
            .filter(Task.company_id == request.company_id, Task.id == request.subject_id)
            .with_for_update(of=Task).one_or_none())
    if task is None or task.approval_request_id != request.id:
        return
    if request.status == approvals.CANCELLED:
        raise conflict("A submitted task cannot be withdrawn; ask an approver to reject it.",
                       "TASK_CANNOT_BE_WITHDRAWN")
    ids = [a.employee_id for a in task.assignees]
    if request.status == approvals.APPROVED:
        target = _transition(task, "APPROVE")
        last = max((s for s in request.steps if s.status == "approved"), key=lambda s: s.step_order, default=None)
        if principal is not None and last is not None and last.acted_as == "approver" \
                and not _covers_all(db, principal, APPROVE, ids):
            raise forbidden("Approving this task needs task approval rights over every assignee.")
        _require_evidence(db, task)
        if task.accepted_quantity is None:
            task.accepted_quantity = task.quantity
        task.status = target
        task.approved_on = _today(db, task.company_id)
        task.decided_at = utcnow()
        task.row_version += 1
        _flush(db)
        _task_audit(db, principal, TASK_APPROVED, task, f"Approved task #{task.id}", request_id=request.id,
                    accepted_quantity=_quantity_str(task.accepted_quantity), quantity=_quantity_str(task.quantity))
        _log_change(db, principal, task, "UPDATED")
    else:
        target = _transition(task, "REJECT")
        remarks = next((s.remarks for s in sorted(request.steps, key=lambda s: -s.step_order)
                        if s.status == "rejected"), None)
        task.status = target
        task.status_reason = remarks
        task.decided_at = utcnow()
        task.row_version += 1
        _flush(db)
        _task_audit(db, principal, TASK_REJECTED, task, f"Rejected task #{task.id}", request_id=request.id,
                    remarks=remarks)


# --------------------------------------------------------------------------- evidence


def _evidence_type(data: bytes) -> tuple[str, str]:
    for prefix, ext, mime in EVIDENCE_TYPES:
        if data.startswith(prefix):
            return ext, mime
    raise bad_request("Evidence must be a PDF, PNG or JPG file.", "UNSUPPORTED_FILE_TYPE")


def _file_name(value: str | None, ext: str) -> str:
    name = Path(value or "").name.strip()[:200]
    return name or f"evidence.{ext}"


def add_evidence(db: Session, principal: Principal, task_id: int, *, file_name: str | None, data: bytes
                 ) -> TaskEvidence:
    """Attach a proof file (private storage). Allowed while the task is a draft, submitted or disputed."""
    require_enabled(db, principal.company_id)
    task = _load(db, principal, task_id, lock=True)
    _ensure_manage(db, principal, [a.employee_id for a in task.assignees])
    if task.status not in (DRAFT, SUBMITTED, DISPUTED):
        raise conflict("Evidence can no longer be added to this task.", "TASK_NOT_EDITABLE")
    if not data:
        raise bad_request("No file provided.", "EMPTY_FILE")
    if len(data) > app_settings.max_upload_size:
        raise bad_request(f"File is too large (max {app_settings.max_upload_size // (1024 * 1024)} MB).",
                          "FILE_TOO_LARGE")
    if len(task.evidence) >= MAX_EVIDENCE:
        raise bad_request(f"A task has at most {MAX_EVIDENCE} evidence files.", "TOO_MANY_FILES")
    ext, mime = _evidence_type(data)
    digest = hashlib.sha256(data).hexdigest()
    if any(e.sha256 == digest for e in task.evidence):
        raise conflict("This file is already attached to the task.", "DUPLICATE_EVIDENCE")
    path = storage.save_bytes(principal.company_id, EVIDENCE_CATEGORY, data, ext)
    evidence = TaskEvidence(company_id=task.company_id, task_id=task.id, file_name=_file_name(file_name, ext),
                            storage_path=path, mime=mime, bytes=len(data), sha256=digest,
                            uploaded_by_user_id=principal.user_id)
    task.evidence.append(evidence)
    try:
        _flush(db)
    except AppError:
        storage.delete(principal.company_id, path)
        raise
    _task_audit(db, principal, TASK_EVIDENCE_ADDED, task, f"Added evidence to task #{task.id}",
                evidence_id=evidence.id, file_name=evidence.file_name, sha256=digest, bytes=len(data))
    return evidence


def _evidence(db: Session, principal: Principal, task_id: int, evidence_id: int, *, lock: bool = False
              ) -> tuple[Task, TaskEvidence]:
    task = _load(db, principal, task_id, lock=lock)
    found = next((e for e in task.evidence if e.id == evidence_id), None)
    if found is None:
        raise not_found("Evidence")
    return task, found


def evidence_file(db: Session, principal: Principal, task_id: int, evidence_id: int) -> tuple[Path, str, str]:
    """(path, media type, file name) of an evidence file the caller may see."""
    require_enabled(db, principal.company_id)
    _, evidence = _evidence(db, principal, task_id, evidence_id)
    return storage.resolve(principal.company_id, evidence.storage_path), evidence.mime, evidence.file_name


def remove_evidence(db: Session, principal: Principal, task_id: int, evidence_id: int) -> str:
    """Detach evidence from a draft task; returns the storage path for the caller to delete after commit."""
    require_enabled(db, principal.company_id)
    task, evidence = _evidence(db, principal, task_id, evidence_id, lock=True)
    _ensure_manage(db, principal, [a.employee_id for a in task.assignees])
    if task.status != DRAFT:
        raise conflict("Evidence of a submitted task cannot be removed.", "TASK_NOT_EDITABLE")
    path = evidence.storage_path
    task.evidence.remove(evidence)
    _flush(db)
    _task_audit(db, principal, TASK_EVIDENCE_REMOVED, task, f"Removed evidence from task #{task.id}",
                evidence_id=evidence_id, file_name=evidence.file_name, sha256=evidence.sha256)
    return path


# --------------------------------------------------------------------------- rate cards


def _criteria(db: Session, company_id: int, raw: Mapping[str, Any] | None) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if key not in RATE_CARD_CRITERIA:
            raise bad_request(f"Unknown rate card criterion {key!r}.", "INVALID_CRITERIA")
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 50:
            raise bad_request(f"{key} must be 1-50 characters.", "INVALID_CRITERIA")
        criteria[key] = value.strip()
    if "grade" in criteria and db.query(Grade.id).filter(Grade.company_id == company_id,
                                                         Grade.code == criteria["grade"]).first() is None:
        raise bad_request(f"Unknown grade {criteria['grade']!r}.", "UNKNOWN_GRADE")
    if "location" in criteria and db.query(Branch.id).filter(Branch.company_id == company_id,
                                                             Branch.code == criteria["location"]).first() is None:
        raise bad_request(f"Unknown branch {criteria['location']!r}.", "UNKNOWN_BRANCH")
    return dict(sorted(criteria.items()))


def _require_rate_card_read(principal: Principal) -> None:
    if not any(principal.has(code) for code in RATE_CARD_READ_PERMISSIONS):
        raise forbidden()


def _require_rate_card_write(principal: Principal) -> None:
    if not principal.has_company_scope(CONFIG_WRITE):
        raise forbidden()


def rate_card_json(row: TaskRateCard) -> dict[str, Any]:
    return {
        "id": row.id, "task_type": row.task_type, "criteria": dict(row.criteria or {}),
        "rate": rate_json(row.rate, row.currency), "effective_from": row.effective_from.isoformat(),
        "effective_to": _iso(row.effective_to), "created_by_user_id": row.created_by_user_id,
        "created_at": _iso(row.created_at),
    }


def list_rate_cards(db: Session, principal: Principal, *, task_type: str | None = None, on: date | None = None
                    ) -> list[dict[str, Any]]:
    _require_rate_card_read(principal)
    query = db.query(TaskRateCard).filter(TaskRateCard.company_id == principal.company_id)
    if task_type is not None:
        query = query.filter(TaskRateCard.task_type == task_type)
    if on is not None:
        query = query.filter(TaskRateCard.effective_from <= on,
                             or_(TaskRateCard.effective_to.is_(None), TaskRateCard.effective_to >= on))
    return [rate_card_json(r) for r in query.order_by(TaskRateCard.task_type, TaskRateCard.effective_from,
                                                      TaskRateCard.id).limit(1000)]


def create_rate_card(db: Session, principal: Principal, data: Mapping[str, Any]) -> TaskRateCard:
    _require_rate_card_write(principal)
    rate, currency = _rate(data["rate"]["amount"], data["rate"]["currency"])
    effective_from, effective_to = data["effective_from"], data.get("effective_to")
    if effective_to is not None and effective_to < effective_from:
        raise bad_request("effective_to is before effective_from.", "INVALID_RANGE")
    row = TaskRateCard(company_id=principal.company_id, task_type=data["task_type"],
                       criteria=_criteria(db, principal.company_id, data.get("criteria")), rate=rate, currency=currency,
                       effective_from=effective_from, effective_to=effective_to, created_by_user_id=principal.user_id)
    db.add(row)
    _flush(db)
    _audit(db, principal, TASK_RATE_CARD_CREATED, "task_rate_card", row.id, row.company_id,
           f"Added rate card row #{row.id} for {row.task_type}", after=rate_card_json(row))
    return row


def end_rate_card(db: Session, principal: Principal, rate_card_id: int, effective_to: date) -> TaskRateCard:
    """Close a rate card row (rows are never edited otherwise, so runs that used them stay explainable)."""
    _require_rate_card_write(principal)
    row = (db.query(TaskRateCard).filter(TaskRateCard.company_id == principal.company_id,
                                         TaskRateCard.id == rate_card_id)
           .with_for_update(of=TaskRateCard).one_or_none())
    if row is None:
        raise not_found("Rate card row")
    if effective_to < row.effective_from:
        raise bad_request("effective_to is before effective_from.", "INVALID_RANGE")
    if row.effective_to is not None and effective_to > row.effective_to:
        raise conflict("A rate card row can only be shortened.", "RATE_CARD_EXTENSION")
    before = rate_card_json(row)
    row.effective_to = effective_to
    _flush(db)
    _audit(db, principal, TASK_RATE_CARD_ENDED, "task_rate_card", row.id, row.company_id,
           f"Rate card row #{row.id} ends on {effective_to.isoformat()}", before=before, after=rate_card_json(row))
    return row


# --------------------------------------------------------------------------- payroll consumption


def content_hash(data: Mapping[str, Any]) -> str:
    canonical = json.dumps({k: v for k, v in data.items() if k != "content_hash"}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_json(task: Task, employee_id: int | None = None, attribution: str = "APPROVAL_DATE") -> dict[str, Any]:
    """The task as one assignee's run sees it. ``paid_in_run_id`` is the run that paid the task, or, while
    other shares are still unpaid, the run that paid THIS assignee's share (EC-TSK-*, EC-VAR-01): a share
    paid once is excluded as ALREADY_PAID, and another assignee being paid never changes this assignee's view
    (no stale marking of the other assignee's run)."""
    mine = next((a for a in task.assignees if a.employee_id == employee_id), None)
    share = mine.split_percent if mine is not None else None
    paid_in = task.paid_in_run_id if task.paid_in_run_id is not None else (
        mine.paid_in_run_id if mine is not None else None)
    attributed = task.approved_on if attribution == "APPROVAL_DATE" else (task.completed_on or task.approved_on)
    data = {
        "task_id": f"task:{task.id}",
        "id": task.id,
        "task_type": task.task_type,
        "title": task.title,
        "status": task.status,
        "quantity": decimal_str(Decimal(task.quantity)),
        "accepted_quantity": decimal_str(Decimal(task.accepted_quantity)) if task.accepted_quantity is not None else None,
        "unit": task.unit,
        "rate": decimal_str(Decimal(task.rate)) if task.rate is not None else None,
        "rate_currency": task.rate_currency,
        "approved_on": _iso(task.approved_on),
        "completed_on": _iso(task.completed_on),
        "attribution": attribution,
        "attribution_date": _iso(attributed),
        "evidence_count": len(task.evidence),
        "external_ref": task.external_ref,
        "paid_in_run_id": paid_in,
        "assignees": [{"employee_id": str(a.employee_id),
                       "split_percent": format(Decimal(a.split_percent).quantize(Decimal("0.01")), "f")}
                      for a in task.assignees],
        "split_percent": format(Decimal(share).quantize(Decimal("0.01")), "f") if share is not None else None,
    }
    data["content_hash"] = content_hash(data)
    return data


def approved_tasks(db: Session, company_id: int, employee_id: int, attribution_start: date, attribution_end: date,
                   attribution: str) -> list[dict[str, Any]]:
    """APPROVED tasks with this assignee's share unpaid, attributed to [attribution_start, attribution_end], for
    the snapshot. A task shared with assignees of other pay groups stays APPROVED until every share is paid, so
    each assignee's own run picks up its share exactly once (TaskAssignee.paid_in_run_id, EC-TSK-*, EC-VAR-01).

    ``attribution`` is APPROVAL_DATE (approved_on) or COMPLETION_DATE (completed_on, or approved_on for a
    task without a completion date). Rejected, disputed, submitted and draft tasks never appear (EC-TSK-02);
    the amounts use accepted_quantity (EC-TSK-04). Field names match time_pay_tasks.TaskRecord.
    """
    if attribution not in ATTRIBUTIONS:
        raise ValueError(f"attribution must be one of {ATTRIBUTIONS}")
    if attribution_end < attribution_start:
        raise ValueError("attribution_end is before attribution_start")
    query = (db.query(Task).options(selectinload(Task.assignees), selectinload(Task.evidence))
             .filter(Task.company_id == company_id, Task.status == APPROVED, Task.paid_in_run_id.is_(None),
                     exists().where(TaskAssignee.company_id == Task.company_id, TaskAssignee.task_id == Task.id,
                                    TaskAssignee.employee_id == employee_id, TaskAssignee.paid_in_run_id.is_(None))))
    if attribution == "APPROVAL_DATE":
        query = query.filter(Task.approved_on >= attribution_start, Task.approved_on <= attribution_end)
    else:
        query = query.filter(or_(
            (Task.completed_on >= attribution_start) & (Task.completed_on <= attribution_end),
            Task.completed_on.is_(None) & (Task.approved_on >= attribution_start)
            & (Task.approved_on <= attribution_end)))
    rows = query.order_by(Task.approved_on, Task.id).all()
    return [snapshot_json(t, employee_id, attribution) for t in rows]


def rate_card_rows(db: Session, company_id: int, start: date, end: date, task_types: Iterable[str] | None = None
                   ) -> list[dict[str, Any]]:
    """Rate card rows effective at some point in [start, end], shaped like time_pay_tasks.RateCardRow."""
    query = db.query(TaskRateCard).filter(TaskRateCard.company_id == company_id, TaskRateCard.effective_from <= end,
                                          or_(TaskRateCard.effective_to.is_(None), TaskRateCard.effective_to >= start))
    if task_types is not None:
        query = query.filter(TaskRateCard.task_type.in_(sorted(set(task_types)) or [""]))
    result = []
    for row in query.order_by(TaskRateCard.task_type, TaskRateCard.effective_from, TaskRateCard.id):
        criteria = row.criteria or {}
        data = {"id": row.id, "task_type": row.task_type, "rate": decimal_str(Decimal(row.rate)),
                "currency": row.currency, "effective_from": row.effective_from.isoformat(),
                "effective_to": _iso(row.effective_to), "grade": criteria.get("grade"),
                "skill": criteria.get("skill"), "location": criteria.get("location")}
        data["content_hash"] = content_hash(data)
        result.append(data)
    return result


def _mark_paid(db: Session, task: Task, assignees: Sequence[TaskAssignee], run_id: int) -> int:
    """Pay ``assignees``' shares of a locked task by ``run_id``; the task turns PAID (paid_in_run_id = this run)
    once every share is paid. A share is paid exactly once: paying it again by the same run is a no-op, by
    another run a conflict (TASK_SHARE_ALREADY_PAID). Returns how many shares changed."""
    changed = 0
    for assignee in assignees:
        if assignee.paid_in_run_id == run_id:
            continue
        if assignee.paid_in_run_id is not None:
            raise conflict(f"The share of employee #{assignee.employee_id} in task #{task.id} was already paid by "
                           f"run #{assignee.paid_in_run_id}; it is paid exactly once.", "TASK_SHARE_ALREADY_PAID")
        if task.status != APPROVED:
            _transition(task, "MARK_PAID")   # raises: only an approved task's shares are paid
        assignee.paid_in_run_id = run_id
        changed += 1
        _task_audit(db, None, TASK_PAID, task, f"Share of employee #{assignee.employee_id} in task #{task.id} "
                    f"paid by run #{run_id}", run_id=run_id, share_employee_id=assignee.employee_id)
    turned_paid = task.status == APPROVED and all(a.paid_in_run_id is not None for a in task.assignees)
    if turned_paid:
        task.status = _transition(task, "MARK_PAID")
        task.paid_in_run_id = run_id
        _task_audit(db, None, TASK_PAID, task, f"Task #{task.id} paid by run #{run_id}", run_id=run_id)
    if changed or turned_paid:
        task.row_version += 1
    return changed


def _locked_tasks(db: Session, company_id: int, ids: Sequence[int]) -> dict[int, Task]:
    # populate_existing: a Task (and its assignees) this session loaded before the lock, e.g. by the snapshot
    # rebuild of the finalize gate, is re-read under the lock, so a share another run paid meanwhile is seen
    # (never paid twice, and the task turns PAID with the last share; EC-TSK-*, EC-VAR-01).
    rows = (db.query(Task).options(selectinload(Task.assignees))
            .filter(Task.company_id == company_id, Task.id.in_(ids)).order_by(Task.id).with_for_update(of=Task)
            .populate_existing().all())
    if len(rows) != len(ids):
        raise not_found("Task")
    return {t.id: t for t in rows}


def mark_task_shares_paid(db: Session, company_id: int, shares: Iterable[tuple[int, int]], run_id: int) -> int:
    """Pay the (task id, employee id) shares a finalized run paid (each exactly once, EC-TSK-*, EC-VAR-01);
    returns how many shares changed. A task turns PAID when its last share is paid."""
    wanted: dict[int, set[int]] = {}
    for task_id, employee_id in shares:
        wanted.setdefault(int(task_id), set()).add(int(employee_id))
    if not wanted:
        return 0
    tasks = _locked_tasks(db, company_id, sorted(wanted))
    changed = 0
    for task_id, employee_ids in sorted(wanted.items()):
        task = tasks[task_id]
        assignees = [a for a in task.assignees if a.employee_id in employee_ids]
        if len(assignees) != len(employee_ids):
            raise conflict(f"Task #{task_id} has no share for employee(s) "
                           f"{sorted(employee_ids - {a.employee_id for a in assignees})}.", "TASK_SHARE_NOT_FOUND")
        changed += _mark_paid(db, task, sorted(assignees, key=lambda a: a.employee_id), run_id)
    db.flush()
    return changed


def mark_tasks_paid(db: Session, company_id: int, task_ids: Iterable[int], run_id: int) -> int:
    """APPROVED -> PAID for whole tasks paid by a finalized run (exactly once): every unpaid share is paid by
    ``run_id``. Returns how many tasks changed. Run finalization pays per assignee (mark_task_shares_paid)."""
    ids = sorted(set(task_ids))
    if not ids:
        return 0
    tasks = _locked_tasks(db, company_id, ids)
    changed = 0
    for task in tasks.values():
        if task.status == PAID and task.paid_in_run_id == run_id:
            continue
        if task.status != APPROVED:
            _transition(task, "MARK_PAID")   # raises
        _mark_paid(db, task, [a for a in task.assignees if a.paid_in_run_id is None], run_id)   # -> PAID
        changed += 1
    db.flush()
    return changed
