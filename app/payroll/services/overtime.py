"""Overtime entries: filing, approval, versions, attendance proposals and payroll reads (3.4 §9, 3.1 §5.5).

* An employee files their own overtime (overtime:request, SELF) and a manager files for an employee in
  their overtime:approve scope: date, minutes (0-1440), reason, an optional span, comp-off. A future
  date is a pre-approval; a past one is after the fact. Negative minutes, more than 24 hours on a date
  and overlapping spans are refused on entry (EC-OT-10; a trigger backs the check under concurrency).
* Every entry is approved through the approvals engine (module "overtime"): approvers are holders of
  overtime:approve covering the employee, never the requester or the employee (no self-approval). An
  approver may edit the minutes down and decide comp-off where the policy allows it (EC-OT-09).
* Editing a pending entry re-routes its approval; editing an approved entry creates a new version that
  supersedes the approved one once approved (the approved version stays in force until then). An entry
  consumed by a finalized run is corrected the same way and becomes a retro item (EC-OT-07).
* With OVERTIME_POLICY.source ATTENDANCE_WITH_APPROVAL, proposals come from canonical worked hours above
  the thresholds (app.payroll.time_pay_overtime.threshold_overtime) and wait for a manager; with
  ATTENDANCE_AUTO (never in a starter profile) they are approved automatically and audited.
* Entries are recorded whatever payroll.overtime.enabled says (§9.9); only APPROVED minutes reach payroll
  through approved_overtime() (EC-OT-02). Every decision appends to source_change_log (record type
  "overtime", 3.4 §16) so draft runs see it. Run finalization calls consume_entries() and credits the
  engine's comp-off hours.

Services never commit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytz
from sqlalchemy import and_, exists, or_
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, aliased

from app.core import authz, clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.approvals import ApprovalRequest
from app.models.attendance import Attendance
from app.models.company import Company
from app.models.employee import Employee, Shift
from app.models.work_items import ACTIVE_OVERTIME_STATUSES, MAX_OVERTIME_MINUTES, OvertimeEntry
from app.payroll import audit as payroll_audit
from app.payroll import time_pay_overtime as tpo
from app.payroll.services import approvals
from app.payroll.services import policies as policy_service
from app.payroll.services import time_inputs
from app.payroll.time_pay_common import TimePayError
from app.services import attendance_service
from app.services.audit_log_service import AuditLogService

MODULE = "overtime"
SUBJECT_TYPE = "overtime_entry"
REQUEST = "overtime:request"
APPROVE = "overtime:approve"
INPUTS_READ = "payroll:inputs:read"
INPUTS_EDIT = "payroll:inputs:edit"
READ_PERMISSIONS = (REQUEST, APPROVE, INPUTS_READ)
PROPOSAL_PERMISSIONS = (APPROVE, INPUTS_EDIT)

PENDING, APPROVED, REJECTED, CANCELLED, SUPERSEDED = (
    "PENDING_APPROVAL", "APPROVED", "REJECTED", "CANCELLED", "SUPERSEDED")
REQUESTS, WITH_APPROVAL, AUTO = "APPROVED_REQUESTS", "ATTENDANCE_WITH_APPROVAL", "ATTENDANCE_AUTO"
ATTENDANCE_SOURCES = (WITH_APPROVAL, AUTO)

MAX_DAYS_FROM_TODAY = 400
MAX_PROPOSAL_DAYS = 62
MAX_PROPOSAL_EMPLOYEES = 500
MAX_LIST = 200

OVERTIME_REQUESTED = "OVERTIME_REQUESTED"
OVERTIME_UPDATED = "OVERTIME_UPDATED"
OVERTIME_VERSION_CREATED = "OVERTIME_VERSION_CREATED"
OVERTIME_EDITED_BY_APPROVER = "OVERTIME_EDITED_BY_APPROVER"
OVERTIME_APPROVED = "OVERTIME_APPROVED"
OVERTIME_REJECTED = "OVERTIME_REJECTED"
OVERTIME_CANCELLED = "OVERTIME_CANCELLED"
OVERTIME_SUPERSEDED = "OVERTIME_SUPERSEDED"
OVERTIME_PROPOSED = "OVERTIME_PROPOSED"
OVERTIME_AUTO_APPROVED = "OVERTIME_AUTO_APPROVED"
OVERTIME_CONSUMED = "OVERTIME_CONSUMED"

_TIMEPAY_STATUS = {
    tpo.OVERLAPPING_ENTRIES: 409,
}


# --------------------------------------------------------------------------- helpers


def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def _tz(db: Session, company_id: int):
    company = db.get(Company, company_id)
    return pytz.timezone(company.timezone or "UTC")


def _audit(db: Session, principal: Principal | None, action: str, entry: OvertimeEntry, description: str,
           **details: Any) -> None:
    AuditLogService.log(
        db, principal, payroll_audit.PAYROLL_INPUTS, action, description, "overtime_entry", entry.id,
        company_id=entry.company_id, actor_type="user" if principal else "system",
        details={"entry_id": entry.id, "employee_id": entry.employee_id, "date": entry.date.isoformat(),
                 "status": entry.status, "version": entry.version, **details},
    )


def _log_change(db: Session, principal: Principal | None, entry: OvertimeEntry, change: str) -> None:
    time_inputs.log_change(
        db, entry.company_id, record_type="overtime", record_id=entry.id, change=change, content=snapshot_json(entry),
        employee_id=entry.employee_id, date_from=entry.date, actor_user_id=principal.user_id if principal else None,
        details={"status": entry.status, "version": entry.version, "supersedes_id": entry.supersedes_id},
    )


def _flush(db: Session) -> None:
    """Flush, turning the guard trigger's refusals into API errors (EC-OT-10)."""
    try:
        with db.begin_nested():
            db.flush()
    except DBAPIError as exc:
        code = getattr(exc.orig, "pgcode", None)
        if code == "23P01":
            raise conflict("This overtime overlaps another overtime entry of the employee.", tpo.OVERLAPPING_ENTRIES)
        if code == "23505":
            raise conflict("This overtime already has a newer version awaiting approval.", "VERSION_PENDING")
        if code == "23514":
            raise bad_request("The overtime entry is not valid (at most 24 hours on one date).", tpo.INVALID_MINUTES)
        if isinstance(exc, IntegrityError):
            raise bad_request("The overtime entry is not valid.", "INVALID_OVERTIME")
        raise


def _timepay_error(exc: TimePayError) -> AppError:
    return AppError(_TIMEPAY_STATUS.get(exc.code, 400), exc.message, exc.code)


def _held(principal: Principal, codes: Iterable[str]) -> list[str]:
    return [code for code in codes if principal.has(code)]


def _visible_clause(db: Session, principal: Principal, column):
    """Employees whose overtime the caller may see: own (request), team (approve), payroll inputs readers."""
    clauses = [authz.employee_scope(db, principal, code).clause(column) for code in _held(principal, READ_PERMISSIONS)]
    if principal.employee_id is not None:
        clauses.append(column == principal.employee_id)
    return or_(*clauses) if clauses else None


def _can_see(db: Session, principal: Principal, employee_id: int) -> bool:
    if authz.is_self(principal, employee_id):
        return True
    return any(authz.can_access_employee(db, principal, code, employee_id)
               for code in _held(principal, (APPROVE, INPUTS_READ)))


def _ensure_can_file(db: Session, principal: Principal, employee_id: int) -> Employee:
    """Own overtime needs overtime:request; someone else's needs overtime:approve covering them (else 404)."""
    if authz.is_self(principal, employee_id):
        if not principal.has(REQUEST):
            raise forbidden()
        return authz.own_employee(db, principal)
    if not principal.has(APPROVE):
        raise forbidden()
    return authz.ensure_employee_access(db, principal, APPROVE, employee_id)


def _load(db: Session, principal: Principal, entry_id: int, *, lock: bool = False) -> OvertimeEntry:
    query = db.query(OvertimeEntry).filter(OvertimeEntry.company_id == principal.company_id,
                                           OvertimeEntry.id == entry_id)
    if lock:
        query = query.with_for_update(of=OvertimeEntry).populate_existing()
    entry = query.one_or_none()
    if entry is None or not _can_see(db, principal, entry.employee_id):
        raise not_found("Overtime entry")
    return entry


@dataclass(frozen=True)
class PolicyView:
    status: str
    rules: tpo.OvertimeRules | None
    policy_hash: str | None = None
    fields: Mapping[str, Any] | None = None


def resolve_policy(db: Session, company_id: int, employee_id: int, on: date) -> PolicyView:
    """The employee's OVERTIME_POLICY on ``on``; rules None when none is active or it cannot be parsed."""
    found = policy_service.resolve_for_employee(db, company_id, "OVERTIME_POLICY", employee_id, on)
    if found.get("status") != "RESOLVED":
        return PolicyView(found.get("status") or "NO_POLICY", None)
    version = found["version"]
    try:
        return PolicyView("RESOLVED", tpo.parse_overtime_policy(version["fields"]), version.get("policy_hash"),
                          version["fields"])
    except TimePayError:
        return PolicyView("INVALID_POLICY", None, version.get("policy_hash"))


def _check_comp_off(view: PolicyView) -> None:
    if view.rules is None or view.rules.exempt or not view.rules.comp_off_allowed:
        raise bad_request("The overtime policy does not allow comp-off instead of pay.", tpo.COMP_OFF_NOT_ALLOWED)


def _local(value: datetime | None, tz) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise bad_request("Give start and end with a timezone offset.", "TIMEZONE_REQUIRED")
    return value.astimezone(tz)


def _live_entries(db: Session, company_id: int, employee_id: int, first: date, last: date,
                  exclude_ids: Iterable[int]) -> list[OvertimeEntry]:
    """Live entries of the employee in [first, last]; a predecessor with a live successor counts once."""
    successor = aliased(OvertimeEntry)
    excluded = [i for i in exclude_ids if i is not None]
    query = db.query(OvertimeEntry).filter(
        OvertimeEntry.company_id == company_id,
        OvertimeEntry.employee_id == employee_id,
        OvertimeEntry.date >= first,
        OvertimeEntry.date <= last,
        OvertimeEntry.status.in_(ACTIVE_OVERTIME_STATUSES),
        ~exists().where(and_(successor.company_id == OvertimeEntry.company_id,
                             successor.supersedes_id == OvertimeEntry.id,
                             successor.status.in_(ACTIVE_OVERTIME_STATUSES),
                             successor.id.notin_(excluded or [0]))),
    )
    if excluded:
        query = query.filter(OvertimeEntry.id.notin_(excluded))
    return query.order_by(OvertimeEntry.date, OvertimeEntry.id).all()


def _as_input(entry: OvertimeEntry, tz) -> tpo.OvertimeEntry:
    return tpo.OvertimeEntry(
        entry_id=f"e{entry.id}" if entry.id is not None else "new",
        date=entry.date,
        minutes=entry.minutes_approved if entry.minutes_approved is not None else entry.minutes_requested,
        status=entry.status,
        comp_off=bool(entry.comp_off),
        start=_local(entry.start_at, tz),
        end=_local(entry.end_at, tz),
    )


def _validate(db: Session, company_id: int, candidate: OvertimeEntry, *, exclude_ids: Iterable[int] = ()) -> None:
    """EC-OT-10: the candidate together with the employee's other live entries around its date."""
    tz = _tz(db, company_id)
    others = _live_entries(db, company_id, candidate.employee_id, candidate.date - timedelta(days=1),
                           candidate.date + timedelta(days=1), exclude_ids)
    try:
        tpo.validate_overtime_entries([_as_input(o, tz) for o in others] + [_as_input(candidate, tz)])
    except TimePayError as exc:
        raise _timepay_error(exc) from None


def _check_minutes(value: Any, name: str = "minutes") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad_request(f"{name} must be a whole number of minutes.", tpo.INVALID_MINUTES)
    if value < 0:
        raise bad_request(f"{name} cannot be negative.", tpo.INVALID_MINUTES)
    if value > MAX_OVERTIME_MINUTES:
        raise bad_request(f"{name} cannot exceed 24 hours.", tpo.INVALID_MINUTES)
    return value


def _check_date(db: Session, company_id: int, day: date) -> date:
    today = _today(db, company_id)
    if abs((day - today).days) > MAX_DAYS_FROM_TODAY:
        raise bad_request(f"The overtime date must be within {MAX_DAYS_FROM_TODAY} days of today.",
                          "DATE_OUT_OF_RANGE")
    return day


def _span(start_at: datetime | None, end_at: datetime | None) -> tuple[datetime | None, datetime | None]:
    if (start_at is None) != (end_at is None):
        raise bad_request("Give both the start and the end of the overtime, or neither.", tpo.INVALID_INPUT)
    for value in (start_at, end_at):
        if value is not None and value.tzinfo is None:
            raise bad_request("Give start and end with a timezone offset.", "TIMEZONE_REQUIRED")
    return start_at, end_at


def _submit(db: Session, principal: Principal | None, entry: OvertimeEntry) -> ApprovalRequest:
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=SUBJECT_TYPE, subject_id=entry.id,
        subject_employee_id=entry.employee_id,
        context={"date": entry.date.isoformat(), "minutes": entry.minutes_requested, "comp_off": bool(entry.comp_off),
                 "source": entry.source, "version": entry.version},
    )
    entry.approval_request_id = request.id
    return request


# --------------------------------------------------------------------------- serialization


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def serialize(entry: OvertimeEntry, *, employee: Employee | None = None, request: ApprovalRequest | None = None
              ) -> dict[str, Any]:
    data = {
        "id": entry.id,
        "employee_id": entry.employee_id,
        "employee_name": employee.full_name if employee is not None else None,
        "employee_code": employee.employee_code if employee is not None else None,
        "date": entry.date.isoformat(),
        "start_at": _iso(entry.start_at),
        "end_at": _iso(entry.end_at),
        "minutes_requested": entry.minutes_requested,
        "minutes_computed": entry.minutes_computed,
        "minutes_approved": entry.minutes_approved,
        "bucket": entry.bucket,
        "source": entry.source,
        "status": entry.status,
        "comp_off": bool(entry.comp_off),
        "pre_approval": bool(entry.pre_approval),
        "reason": entry.reason,
        "version": entry.version,
        "supersedes_id": entry.supersedes_id,
        "approval_request_id": entry.approval_request_id,
        "requested_by_user_id": entry.requested_by_user_id,
        "decided_at": _iso(entry.decided_at),
        "consumed_by_run_employee_id": entry.consumed_by_run_employee_id,
        "row_version": entry.row_version,
        "created_at": _iso(entry.created_at),
    }
    if request is not None:
        data["approval"] = {"status": request.status, "current_step": request.current_step,
                            "pending_reason": (request.context or {}).get("pending_reason")}
    return data


def action_result(entry: OvertimeEntry) -> dict[str, Any]:
    return {"id": entry.id, "status": entry.status, "version": entry.version, "row_version": entry.row_version,
            "approval_request_id": entry.approval_request_id, "supersedes_id": entry.supersedes_id}


# --------------------------------------------------------------------------- queries


def list_entries(
    db: Session,
    principal: Principal,
    *,
    employee_id: int | None = None,
    status: str | None = None,
    start: date | None = None,
    end: date | None = None,
    mine: bool = False,
    awaiting: bool = False,
    skip: int = 0,
    limit: int = 50,
) -> tuple[int, list[dict[str, Any]]]:
    """Entries the caller may see; totals count only those (EC-SEC-05)."""
    clause = _visible_clause(db, principal, OvertimeEntry.employee_id)
    if clause is None:
        raise forbidden()
    query = db.query(OvertimeEntry).filter(OvertimeEntry.company_id == principal.company_id, clause)
    if mine:
        if principal.employee_id is None:
            return 0, []
        query = query.filter(OvertimeEntry.employee_id == principal.employee_id)
    if employee_id is not None:
        query = query.filter(OvertimeEntry.employee_id == employee_id)
    if status is not None:
        query = query.filter(OvertimeEntry.status == status)
    if start is not None:
        query = query.filter(OvertimeEntry.date >= start)
    if end is not None:
        query = query.filter(OvertimeEntry.date <= end)
    if awaiting:
        ids = approvals.subjects_awaiting(db, principal, MODULE, SUBJECT_TYPE)
        query = query.filter(OvertimeEntry.id.in_(ids or [0]))
    total = query.count()
    rows = (query.order_by(OvertimeEntry.date.desc(), OvertimeEntry.id.desc())
            .offset(skip).limit(min(limit, MAX_LIST)).all())
    employees = {e.id: e for e in db.query(Employee).filter(
        Employee.company_id == principal.company_id, Employee.id.in_({r.employee_id for r in rows} or {0}))}
    requests = {r.id: r for r in db.query(ApprovalRequest).filter(
        ApprovalRequest.company_id == principal.company_id,
        ApprovalRequest.id.in_({r.approval_request_id for r in rows if r.approval_request_id} or {0}))}
    return total, [serialize(r, employee=employees.get(r.employee_id), request=requests.get(r.approval_request_id))
                   for r in rows]


def get_entry(db: Session, principal: Principal, entry_id: int) -> dict[str, Any]:
    entry = _load(db, principal, entry_id)
    request = db.get(ApprovalRequest, entry.approval_request_id) if entry.approval_request_id else None
    data = serialize(entry, employee=db.get(Employee, entry.employee_id), request=request)
    versions = (db.query(OvertimeEntry.id, OvertimeEntry.version, OvertimeEntry.status)
                .filter(OvertimeEntry.company_id == entry.company_id,
                        or_(OvertimeEntry.supersedes_id == entry.id, OvertimeEntry.id == entry.supersedes_id))
                .order_by(OvertimeEntry.version).all())
    data["related_versions"] = [{"id": v.id, "version": v.version, "status": v.status} for v in versions]
    return data


# --------------------------------------------------------------------------- commands


def create_entry(
    db: Session,
    principal: Principal,
    *,
    employee_id: int | None,
    day: date,
    minutes: int,
    reason: str,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    comp_off: bool = False,
) -> OvertimeEntry:
    """File overtime for oneself or (as a manager) for an employee in scope, and route it for approval."""
    if employee_id is None:
        if principal.employee_id is None:
            raise not_found("Employee profile")
        employee_id = principal.employee_id
    employee = _ensure_can_file(db, principal, employee_id)
    if not employee.is_active:
        raise bad_request("Overtime cannot be filed for an inactive employee.", "EMPLOYEE_INACTIVE")
    _check_date(db, principal.company_id, day)
    minutes = _check_minutes(minutes)
    start_at, end_at = _span(start_at, end_at)
    if comp_off:
        _check_comp_off(resolve_policy(db, principal.company_id, employee_id, day))
    entry = OvertimeEntry(
        company_id=principal.company_id, employee_id=employee_id, date=day, start_at=start_at, end_at=end_at,
        minutes_requested=minutes, source=REQUESTS, status=PENDING, comp_off=bool(comp_off),
        pre_approval=day > _today(db, principal.company_id), reason=reason, version=1,
        requested_by_user_id=principal.user_id, row_version=1,
    )
    _validate(db, principal.company_id, entry)
    db.add(entry)
    _flush(db)
    _submit(db, principal, entry)
    _flush(db)
    _audit(db, principal, OVERTIME_REQUESTED,
           entry, f"Requested {minutes} minutes of overtime on {day.isoformat()} (entry #{entry.id})",
           minutes=minutes, comp_off=bool(comp_off), pre_approval=entry.pre_approval, reason=reason,
           filed_for_other=not authz.is_self(principal, employee_id))
    return entry


EDITABLE_FIELDS = ("date", "minutes", "reason", "start_at", "end_at", "comp_off")


def update_entry(db: Session, principal: Principal, entry_id: int, changes: Mapping[str, Any], *,
                 row_version: int | None = None) -> OvertimeEntry:
    """Edit an entry: a pending one in place (its approval restarts), an approved one as a new version."""
    entry = _load(db, principal, entry_id, lock=True)
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise bad_request(f"Unknown fields: {', '.join(sorted(unknown))}.", "INVALID_FIELDS")
    _ensure_can_file(db, principal, entry.employee_id)
    if row_version is not None and row_version != entry.row_version:
        raise conflict("This overtime entry changed since you loaded it.", "STALE_ROW_VERSION")
    if entry.status not in (PENDING, APPROVED):
        raise conflict(f"A {entry.status.lower().replace('_', ' ')} overtime entry cannot be edited.",
                       "OVERTIME_NOT_EDITABLE")
    if not changes:
        raise bad_request("Nothing to change.", "NO_CHANGES")

    day = changes.get("date", entry.date)
    minutes = changes.get("minutes", entry.minutes_requested)
    start_at = changes.get("start_at", entry.start_at)
    end_at = changes.get("end_at", entry.end_at)
    comp_off = changes.get("comp_off", entry.comp_off)
    reason = changes.get("reason", entry.reason)
    _check_date(db, principal.company_id, day)
    minutes = _check_minutes(minutes)
    start_at, end_at = _span(start_at, end_at)
    if comp_off:
        _check_comp_off(resolve_policy(db, principal.company_id, entry.employee_id, day))
    before = serialize(entry)

    if entry.status == PENDING:
        approvals.withdraw(db, principal, module=MODULE, subject_type=SUBJECT_TYPE, subject_id=entry.id,
                           reason="The overtime entry was edited")
        entry.date, entry.minutes_requested, entry.start_at, entry.end_at = day, minutes, start_at, end_at
        entry.comp_off, entry.reason = bool(comp_off), reason
        entry.minutes_approved = None
        entry.pre_approval = day > _today(db, principal.company_id)
        entry.row_version += 1
        _validate(db, principal.company_id, entry, exclude_ids=[entry.id, entry.supersedes_id])
        _flush(db)
        _submit(db, principal, entry)
        _flush(db)
        _audit(db, principal, OVERTIME_UPDATED, entry, f"Edited pending overtime entry #{entry.id}",
               before=before, after=serialize(entry))
        return entry

    pending_successor = db.query(OvertimeEntry.id).filter(
        OvertimeEntry.company_id == entry.company_id, OvertimeEntry.supersedes_id == entry.id,
        OvertimeEntry.status.in_(ACTIVE_OVERTIME_STATUSES)).first()
    if pending_successor is not None:
        raise conflict("This overtime already has a newer version awaiting approval.", "VERSION_PENDING")
    successor = OvertimeEntry(
        company_id=entry.company_id, employee_id=entry.employee_id, date=day, start_at=start_at, end_at=end_at,
        minutes_requested=minutes, minutes_computed=entry.minutes_computed if day == entry.date else None,
        bucket=entry.bucket if day == entry.date else None, source=entry.source, status=PENDING,
        comp_off=bool(comp_off), pre_approval=day > _today(db, principal.company_id), reason=reason,
        version=entry.version + 1, supersedes_id=entry.id, requested_by_user_id=principal.user_id, row_version=1,
    )
    _validate(db, principal.company_id, successor, exclude_ids=[entry.id])
    db.add(successor)
    _flush(db)
    _submit(db, principal, successor)
    _flush(db)
    _audit(db, principal, OVERTIME_VERSION_CREATED, successor,
           f"Created version {successor.version} of approved overtime entry #{entry.id} (entry #{successor.id})",
           supersedes_id=entry.id, before=before, after=serialize(successor),
           consumed_by_run_employee_id=entry.consumed_by_run_employee_id)
    return successor


def cancel_entry(db: Session, principal: Principal, entry_id: int, remarks: str | None = None) -> OvertimeEntry:
    """Withdraw a pending entry: the requester, the employee, or a manager who could file it."""
    entry = _load(db, principal, entry_id, lock=True)
    if entry.requested_by_user_id != principal.user_id:
        _ensure_can_file(db, principal, entry.employee_id)
    if entry.status != PENDING:
        raise conflict("Only a pending overtime entry can be cancelled.", "OVERTIME_NOT_PENDING")
    approvals.withdraw(db, principal, module=MODULE, subject_type=SUBJECT_TYPE, subject_id=entry.id,
                       reason=remarks or "Cancelled by the requester")
    entry.status = CANCELLED
    entry.decided_at = utcnow()
    entry.row_version += 1
    _flush(db)
    _audit(db, principal, OVERTIME_CANCELLED, entry, f"Cancelled overtime entry #{entry.id}", remarks=remarks)
    return entry


def decide_entry(
    db: Session,
    principal: Principal,
    entry_id: int,
    decision: str,
    *,
    minutes_approved: int | None = None,
    comp_off: bool | None = None,
    remarks: str | None = None,
    expected_step: int | None = None,
) -> OvertimeEntry:
    """Approve (optionally editing the minutes down or deciding comp-off) or reject the current step."""
    entry = _load(db, principal, entry_id, lock=True)
    if entry.status != PENDING or entry.approval_request_id is None:
        raise conflict("This overtime entry is not awaiting approval.", "OVERTIME_NOT_PENDING")
    approve = str(decision).upper() == "APPROVE"
    edits: dict[str, Any] = {}
    if approve and minutes_approved is not None:
        _check_minutes(minutes_approved, "minutes_approved")
        if minutes_approved > entry.minutes_requested:
            raise bad_request("Approved minutes cannot exceed the requested minutes.", "MINUTES_ABOVE_REQUESTED")
        edits["minutes_approved"] = (entry.minutes_approved, minutes_approved)
        entry.minutes_approved = minutes_approved
    if approve and comp_off is not None and bool(comp_off) != bool(entry.comp_off):
        if comp_off:
            _check_comp_off(resolve_policy(db, entry.company_id, entry.employee_id, entry.date))
        edits["comp_off"] = (bool(entry.comp_off), bool(comp_off))
        entry.comp_off = bool(comp_off)
    if edits:
        entry.row_version += 1
        _flush(db)
    approvals.decide(db, principal, entry.approval_request_id, "APPROVE" if approve else "REJECT", remarks,
                     expected_step)
    if edits:
        _audit(db, principal, OVERTIME_EDITED_BY_APPROVER, entry, f"Approver edited overtime entry #{entry.id}",
               changes={k: {"before": b, "after": a} for k, (b, a) in edits.items()})
    return entry


@approvals.on_decided(MODULE, SUBJECT_TYPE)
def _on_decided(db: Session, principal: Principal | None, request: ApprovalRequest) -> None:
    entry = (db.query(OvertimeEntry)
             .filter(OvertimeEntry.company_id == request.company_id, OvertimeEntry.id == request.subject_id)
             .with_for_update(of=OvertimeEntry).populate_existing().one_or_none())
    if entry is None or entry.approval_request_id != request.id:
        return
    if entry.status != PENDING:
        raise conflict("This overtime entry is no longer pending.", "OVERTIME_NOT_PENDING")
    entry.decided_at = utcnow()
    entry.row_version += 1
    if request.status == approvals.APPROVED:
        entry.status = APPROVED
        if entry.minutes_approved is None:
            entry.minutes_approved = entry.minutes_requested
        if entry.supersedes_id is not None:
            previous = (db.query(OvertimeEntry)
                        .filter(OvertimeEntry.company_id == entry.company_id, OvertimeEntry.id == entry.supersedes_id)
                        .with_for_update(of=OvertimeEntry).one())
            previous.status = SUPERSEDED
            previous.row_version += 1
            _audit(db, principal, OVERTIME_SUPERSEDED, previous,
                   f"Overtime entry #{previous.id} superseded by version {entry.version} (#{entry.id})",
                   superseded_by=entry.id)
        _flush(db)
        if entry.supersedes_id is not None:
            _log_change(db, principal, previous, "VOIDED")
        _audit(db, principal, OVERTIME_APPROVED, entry, f"Approved overtime entry #{entry.id}",
               minutes_approved=entry.minutes_approved, comp_off=bool(entry.comp_off), request_id=request.id)
    elif request.status == approvals.REJECTED:
        entry.status = REJECTED
        entry.minutes_approved = None
        _flush(db)
        _audit(db, principal, OVERTIME_REJECTED, entry, f"Rejected overtime entry #{entry.id}", request_id=request.id)
    else:
        entry.status = CANCELLED
        entry.minutes_approved = None
        _flush(db)
        _audit(db, principal, OVERTIME_CANCELLED, entry, f"Cancelled overtime entry #{entry.id}",
               request_id=request.id)
    _log_change(db, principal, entry, "UPDATED")


# --------------------------------------------------------------------------- attendance proposals


def _dates(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _shift_hours(db: Session, company_id: int, shift_id: int | None) -> Decimal | None:
    if shift_id is None:
        return None
    value = db.query(Shift.duration_hours).filter(Shift.company_id == company_id, Shift.id == shift_id).scalar()
    return Decimal(str(value)) if value else None


@dataclass(frozen=True)
class _Day:
    day_type: str
    shift_id: int | None


def _day_types(db: Session, company_id: int, employee: Employee, first: date, last: date) -> dict[date, _Day]:
    """Day type (WORKDAY, WEEKLY_OFF, HOLIDAY) and placement shift per employed date in [first, last].

    Uses the day facts of the time inputs (branch calendar lineage, work week, chosen optional holidays).
    """
    found: dict[date, _Day] = {}
    for facts in time_inputs.day_facts(db, company_id, employee.id, first, last):
        if not facts.employed:
            continue
        kind = tpo.HOLIDAY if facts.holiday is not None and facts.holiday.paid else (
            tpo.WEEKLY_OFF if facts.weekly_off else tpo.WORKDAY)
        shift_id = (facts.assignment.shift_id if facts.assignment is not None else None) or employee.shift_id
        found[facts.date] = _Day(kind, shift_id)
    return found


def _worked(db: Session, company_id: int, employee_id: int, first: date, last: date) -> dict[date, tuple[int, int | None]]:
    """Canonical net worked seconds of completed sessions per date, with the session's shift."""
    records = (db.query(Attendance)
               .filter(Attendance.company_id == company_id, Attendance.employee_id == employee_id,
                       Attendance.date >= first, Attendance.date <= last).all())
    breaks = attendance_service.breaks_by_session(db, [r.id for r in records])
    now = utcnow()
    worked: dict[date, tuple[int, int | None]] = {}
    for record in records:
        hours = attendance_service.hours_for(record, breaks.get(record.id, []), now)
        if hours.provisional or not hours.complete or hours.net_seconds is None:
            continue
        seconds, shift = worked.get(record.date, (0, record.shift_id))
        worked[record.date] = (min(seconds + int(hours.net_seconds), tpo.MAX_SECONDS_PER_DAY), shift or record.shift_id)
    return worked


def _proposal_employees(db: Session, principal: Principal, employee_ids: Sequence[int] | None) -> list[Employee]:
    held = _held(principal, PROPOSAL_PERMISSIONS)
    if not held:
        raise forbidden()
    clauses = [authz.employee_scope(db, principal, code).clause(Employee.id) for code in held]
    query = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.is_active.is_(True),
                                      or_(*clauses))
    if employee_ids is not None:
        wanted = sorted(set(employee_ids))
        rows = query.filter(Employee.id.in_(wanted or [0])).all()
        if len(rows) != len(wanted):
            raise not_found("One or more employees")
        return rows
    rows = query.order_by(Employee.id).limit(MAX_PROPOSAL_EMPLOYEES + 1).all()
    if len(rows) > MAX_PROPOSAL_EMPLOYEES:
        raise bad_request(f"Generate proposals for at most {MAX_PROPOSAL_EMPLOYEES} employees at a time.",
                          "TOO_MANY_EMPLOYEES")
    return rows


def generate_proposals(db: Session, principal: Principal, *, start: date, end: date,
                       employee_ids: Sequence[int] | None = None) -> dict[str, Any]:
    """Propose overtime from worked hours for employees whose policy source is attendance-based.

    Weeks are evaluated whole (Monday first) so weekly thresholds count each hour once (EC-OT-03), but
    only dates in [start, end] up to yesterday get proposals; a date with a live entry is skipped, so
    running it again is idempotent. ATTENDANCE_AUTO proposals are approved at once (audited).
    """
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days + 1 > MAX_PROPOSAL_DAYS:
        raise bad_request(f"Generate proposals for at most {MAX_PROPOSAL_DAYS} days at a time.", "RANGE_TOO_LONG")
    company_id = principal.company_id
    today = _today(db, company_id)
    end = min(end, today - timedelta(days=1))
    summary: dict[str, Any] = {"start": start.isoformat(), "end": end.isoformat(), "created": [], "skipped": []}
    if end < start:
        return summary
    first = tpo.week_start_of(start, 0)
    last = tpo.week_start_of(end, 0) + timedelta(days=6)
    for employee in _proposal_employees(db, principal, employee_ids):
        days = _day_types(db, company_id, employee, first, last)
        worked = _worked(db, company_id, employee.id, first, last)
        for week in sorted({tpo.week_start_of(d, 0) for d in days}):
            week_days = [d for d in _dates(week, week + timedelta(days=6)) if d in days]
            anchor = min(week + timedelta(days=6), end)
            view = resolve_policy(db, company_id, employee.id, max(anchor, start))
            if view.rules is None or view.rules.exempt or view.rules.source not in ATTENDANCE_SOURCES:
                reason = "EXEMPT" if view.rules is not None and view.rules.exempt else (
                    view.status if view.rules is None else "SOURCE_NOT_ATTENDANCE")
                summary["skipped"].append({"employee_id": employee.id, "week": week.isoformat(), "reason": reason})
                continue
            inputs = []
            for day in week_days:
                seconds, session_shift = worked.get(day, (0, None))
                inputs.append(tpo.WorkedDay(date=day, worked_seconds=seconds, day_type=days[day].day_type,
                                            shift_hours=_shift_hours(db, company_id, session_shift or days[day].shift_id)))
            try:
                result = tpo.threshold_overtime(inputs, view.fields, week_start=0)
            except TimePayError as exc:
                summary["skipped"].append({"employee_id": employee.id, "week": week.isoformat(), "reason": exc.code,
                                           "message": exc.message})
                continue
            for proposal in result.entries:
                if not start <= proposal.date <= end:
                    continue
                if _live_entries(db, company_id, employee.id, proposal.date, proposal.date, ()):
                    summary["skipped"].append({"employee_id": employee.id, "date": proposal.date.isoformat(),
                                               "reason": "ALREADY_HAS_ENTRY"})
                    continue
                entry = _propose(db, principal, employee, proposal, days[proposal.date].day_type, view)
                summary["created"].append(action_result(entry) | {"employee_id": employee.id,
                                                                  "date": proposal.date.isoformat(),
                                                                  "minutes": entry.minutes_requested})
    return summary


def _propose(db: Session, principal: Principal, employee: Employee, proposal: tpo.OvertimeEntry, day_type: str,
             view: PolicyView) -> OvertimeEntry:
    auto = view.rules.source == AUTO
    minutes = min(proposal.minutes, MAX_OVERTIME_MINUTES)
    entry = OvertimeEntry(
        company_id=employee.company_id, employee_id=employee.id, date=proposal.date, minutes_requested=minutes,
        minutes_computed=minutes, bucket=day_type, source=view.rules.source, status=APPROVED if auto else PENDING,
        minutes_approved=minutes if auto else None, decided_at=utcnow() if auto else None, comp_off=False,
        pre_approval=False, reason="Proposed from attendance above the overtime thresholds", version=1,
        requested_by_user_id=None, row_version=1,
    )
    _validate(db, employee.company_id, entry)
    db.add(entry)
    _flush(db)
    if auto:
        _audit(db, None, OVERTIME_AUTO_APPROVED, entry,
               f"Approved {minutes} minutes of overtime from attendance automatically (entry #{entry.id})",
               minutes=minutes, policy_hash=view.policy_hash, generated_by_user_id=principal.user_id)
        _log_change(db, principal, entry, "CREATED")
    else:
        _submit(db, None, entry)
        _flush(db)
        _audit(db, principal, OVERTIME_PROPOSED, entry,
               f"Proposed {minutes} minutes of overtime from attendance (entry #{entry.id})",
               minutes=minutes, policy_hash=view.policy_hash)
    return entry


# --------------------------------------------------------------------------- payroll consumption


def content_hash(data: Mapping[str, Any]) -> str:
    canonical = json.dumps({k: v for k, v in data.items() if k != "content_hash"}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_json(entry: OvertimeEntry) -> dict[str, Any]:
    data = {
        "entry_id": f"overtime:{entry.id}",
        "id": entry.id,
        "employee_id": entry.employee_id,
        "date": entry.date.isoformat(),
        "minutes": entry.minutes_approved,
        "status": entry.status,
        "comp_off": bool(entry.comp_off),
        "start": _iso(entry.start_at),
        "end": _iso(entry.end_at),
        "origin": tpo.ORIGIN_REQUEST if entry.source == REQUESTS else tpo.ORIGIN_THRESHOLD,
        "source": entry.source,
        "bucket": entry.bucket,
        "version": entry.version,
        "supersedes_id": entry.supersedes_id,
        "consumed_by_run_employee_id": entry.consumed_by_run_employee_id,
    }
    data["content_hash"] = content_hash(data)
    return data


def approved_overtime(db: Session, company_id: int, employee_id: int, start: date, end: date) -> list[dict[str, Any]]:
    """APPROVED entries of the employee dated in [start, end], as plain dicts for the run snapshot.

    ``start``/``end`` are ISO strings in the result; ``origin`` and ``minutes`` match
    time_pay_overtime.OvertimeEntry. Entries already consumed carry consumed_by_run_employee_id so a run
    can tell a retro correction from new overtime (EC-OT-07). Rejected, cancelled, pending and superseded
    entries never appear (EC-OT-02).
    """
    if end < start:
        raise ValueError("end is before start")
    rows = (db.query(OvertimeEntry)
            .filter(OvertimeEntry.company_id == company_id, OvertimeEntry.employee_id == employee_id,
                    OvertimeEntry.status == APPROVED, OvertimeEntry.date >= start, OvertimeEntry.date <= end)
            .order_by(OvertimeEntry.date, OvertimeEntry.id).all())
    return [snapshot_json(r) for r in rows]


def consume_entries(db: Session, company_id: int, entry_ids: Iterable[int], run_employee_id: int) -> int:
    """Mark approved entries as paid by a finalized run employee (exactly once); returns how many changed."""
    ids = sorted(set(entry_ids))
    if not ids:
        return 0
    rows = (db.query(OvertimeEntry)
            .filter(OvertimeEntry.company_id == company_id, OvertimeEntry.id.in_(ids))
            .with_for_update(of=OvertimeEntry).all())
    if len(rows) != len(ids):
        raise not_found("Overtime entry")
    changed = 0
    for entry in rows:
        if entry.consumed_by_run_employee_id == run_employee_id:
            continue
        if entry.status != APPROVED or entry.consumed_by_run_employee_id is not None:
            raise conflict(f"Overtime entry #{entry.id} cannot be consumed.", "OVERTIME_NOT_CONSUMABLE")
        entry.consumed_by_run_employee_id = run_employee_id
        entry.row_version += 1
        changed += 1
        _audit(db, None, OVERTIME_CONSUMED, entry, f"Overtime entry #{entry.id} paid by run employee #{run_employee_id}",
               run_employee_id=run_employee_id)
    db.flush()
    return changed
