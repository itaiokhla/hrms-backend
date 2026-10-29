"""On-duty and remote work requests from the attendance area (AS-M4; 04-attendance/04 §5, LH 03-leave/06 §4 OD-1..OD-8
as amended by JC-AS-37).

* ``POST /attendance/on-duty`` files a range: one ``on_duty_requests`` row per **working** date with a shared
  ``batch_id`` and **one** approval (engine module ``attendance``, subject ``on_duty``); non-working dates are skipped
  and listed (OD-5). A single non-working date stays a row for ``ON_DUTY`` (holiday / weekly-off work, OD-3);
  ``REMOTE_WORK`` on non-working dates needs ``on_duty.remote_work_counts_as_holiday_work`` (else 422 ``NOT_ELIGIBLE``
  ``REMOTE_ON_NON_WORKING_DAY`` when every date is non-working).
* A time window (``start_time`` / ``end_time``) covers the half or halves it intersects (OD-5 amended).
* Leave and on-duty never share a slot (OD-1: 409 ``ON_DUTY_OVERLAP`` ``cause = LEAVE``); an existing on-duty row on
  the slot is ``cause = ON_DUTY``.
* SELF needs ``attendance:request`` and the request policy's ``on_duty.*`` window; others need
  ``attendance:request_on_behalf`` covering the employee. The legacy ``/payroll/on-duty`` routes are unchanged.
* Approved rows count as presence in evaluation step 11 (``attendance_days``) and in payroll (JC-AS-16): an approved
  ``REMOTE_WORK`` satisfies expected presence without geofence (JC-AS-35).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from app.core import authz
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.time_inputs import OnDutyRequest
from app.payroll.services import approvals
from app.payroll.services import time_inputs as ti
from app.services import attendance_requests as ar
from app.services.audit_log_service import AuditLogService

KINDS = ("ON_DUTY", "REMOTE_WORK")
PORTIONS = ("FULL", "FIRST_HALF", "SECOND_HALF")
MAX_RANGE_DAYS = 62
SUBJECT = ti.ON_DUTY_SUBJECT


def _time(value: Any, field: str) -> Optional[time]:
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        raise bad_request(f"{field} must be HH:MM.", "INVALID_FIELDS")


def _target(db: Session, principal: Principal, employee_id: Optional[int]) -> Tuple[Employee, str]:
    if employee_id is None or authz.is_self(principal, employee_id):
        if not principal.has("attendance:request") or principal.employee_id is None:
            raise forbidden()
        return authz.own_employee(db, principal), "SELF"
    return authz.ensure_employee_access(db, principal, "attendance:request_on_behalf", employee_id), "ON_BEHALF"


def _window_portion(db: Session, company_id: int, employee_id: int, d: date, start: time, end: time,
                    zone: str) -> str:
    """The half or halves a time window intersects: halves split at the midpoint of the shift instance (no shift:
    of the working day 09:00–18:00)."""
    import pytz

    tz = pytz.timezone(zone)
    facts = ti.day_facts(db, company_id, employee_id, d, d)[0]
    if facts.instances:
        first, last = facts.instances[0]
    else:
        first, last = tz.localize(datetime.combine(d, time(9))), tz.localize(datetime.combine(d, time(18)))
    middle = first + (last - first) / 2
    w_start = tz.localize(datetime.combine(d, start))
    w_end = tz.localize(datetime.combine(d, end))
    in_first = w_start < middle and w_end > first
    in_second = w_end > middle and w_start < last
    if in_first and in_second:
        return "FULL"
    if in_second:
        return "SECOND_HALF"
    if in_first:
        return "FIRST_HALF"
    raise ar.unprocessable("The time window is outside the working day.", "NOT_ELIGIBLE", {"reason": "OUTSIDE_SHIFT"})


def _leave_clash(db: Session, company_id: int, employee_id: int, d: date) -> bool:
    return db.query(Leave.id).filter(Leave.company_id == company_id, Leave.employee_id == employee_id,
                                     Leave.start_date <= d, Leave.end_date >= d,
                                     Leave.status.in_(("pending", "approved"))).first() is not None


def file(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    """Create the batch (``dry_run`` returns the plan without writing)."""
    from app.services import attendance_service

    cid = principal.company_id
    kind = data.get("kind") or "ON_DUTY"
    if kind not in KINDS:
        raise bad_request("Choose on duty or remote work.", "INVALID_KIND")
    start, end = ar._as_date(data.get("start")), ar._as_date(data.get("end") or data.get("start"))
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days >= MAX_RANGE_DAYS:
        raise bad_request(f"Choose at most {MAX_RANGE_DAYS} days.", "RANGE_TOO_LONG")
    portion = data.get("portion") or "FULL"
    if portion not in PORTIONS:
        raise bad_request("Unknown portion.", "INVALID_FIELDS")
    start_time, end_time = _time(data.get("start_time"), "start_time"), _time(data.get("end_time"), "end_time")
    if (start_time is None) != (end_time is None) or (start_time and end_time <= start_time):
        raise bad_request("Give a start and an end time, the end after the start.", "INVALID_RANGE")
    reason_text = (data.get("reason") or "").strip()
    if not reason_text:
        raise bad_request("Say where or why you work away from your workplace.", "REASON_REQUIRED")
    if len(reason_text) > 500:
        raise bad_request("The reason is at most 500 characters.", "INVALID_FIELDS")
    employee, mode = _target(db, principal, data.get("employee_id"))
    reason_code = data.get("reason_code") or "OFFICIAL_WORK"
    ar._reason(db, cid, reason_code, "ON_DUTY")
    fields = ar.policy_fields(db, cid, employee.id, start)
    od = dict(fields.get("on_duty") or {})
    if not od.get("enabled", True):
        raise ar.unprocessable("On-duty requests are turned off.", "FEATURE_DISABLED")
    if kind == "REMOTE_WORK" and not od.get("remote_work_enabled"):
        raise ar.unprocessable("Remote work requests are not available.", "REQUEST_KIND_DISABLED", {"kind": kind})
    if start_time is not None and not od.get("partial_day_allowed", True):
        raise bad_request("Part-day on-duty is not allowed.", "INVALID_FIELDS")
    if mode == "SELF":
        window_fields = {"window_mode": fields.get("window_mode"), "backdate_days": od.get("backdate_days", 7),
                         "future_days": od.get("future_days", 60)}
        for d in (start, end):
            ar.self_window(db, cid, employee, d, "ON_DUTY", window_fields)

    attendance_service.lock_employee(db, employee.id)
    facts = ti.day_facts(db, cid, employee.id, start, end)
    single = start == end
    holiday_work = od.get("remote_work_counts_as_holiday_work", False)
    zone = attendance_service.work_zone(db, employee, start).zone
    dates: List[Tuple[date, str]] = []
    skipped: List[dict] = []
    clashes: List[str] = []
    for fact in facts:
        if not fact.employed:
            skipped.append({"date": fact.date.isoformat(), "reason": "NOT_EMPLOYED"})
            continue
        if not fact.working and not (single and kind == "ON_DUTY") and not (kind == "REMOTE_WORK" and holiday_work):
            skipped.append({"date": fact.date.isoformat(),
                            "reason": "HOLIDAY" if fact.holiday is not None else "WEEKLY_OFF"})
            continue
        slot = _window_portion(db, cid, employee.id, fact.date, start_time, end_time, zone) if start_time else portion
        if _leave_clash(db, cid, employee.id, fact.date):
            clashes.append(fact.date.isoformat())
            continue
        existing = db.query(OnDutyRequest).filter(
            OnDutyRequest.company_id == cid, OnDutyRequest.employee_id == employee.id,
            OnDutyRequest.date == fact.date, OnDutyRequest.status.in_(("PENDING", "APPROVED"))).all()
        if ti._overlapping(existing, slot):
            raise AppError(409, "On-duty work is already recorded for this slot.", "ON_DUTY_OVERLAP",
                           extra={"details": {"cause": "ON_DUTY", "dates": [fact.date.isoformat()]}})
        dates.append((fact.date, slot))
    if clashes:
        raise AppError(409, "Leave already covers these dates: change the leave first.", "ON_DUTY_OVERLAP",
                       extra={"details": {"cause": "LEAVE", "dates": clashes}})
    if not dates:
        if kind == "REMOTE_WORK":
            raise ar.unprocessable("Remote work cannot be requested for days off.", "NOT_ELIGIBLE",
                                   {"reason": "REMOTE_ON_NON_WORKING_DAY", "skipped_dates": skipped})
        raise ar.unprocessable("None of these dates is a working day.", "NOT_ELIGIBLE", {"skipped_dates": skipped})
    max_days = od.get("max_days_per_cycle")
    if max_days:
        cycle_start, cycle_end, _ = ar.cycle_window(db, cid, employee.id, start)
        used = sum(1 if r.portion == "FULL" else 0.5 for r in db.query(OnDutyRequest).filter(
            OnDutyRequest.company_id == cid, OnDutyRequest.employee_id == employee.id,
            OnDutyRequest.status.in_(("PENDING", "APPROVED")), OnDutyRequest.date >= cycle_start,
            OnDutyRequest.date <= cycle_end))
        adding = sum(1 if p == "FULL" else 0.5 for _, p in dates)
        if used + adding > max_days and mode == "SELF":
            raise ar.unprocessable(f"On-duty this cycle: {used:g} of {max_days} days used.", "REQUEST_LIMIT_REACHED",
                                   {"limit": "on_duty.max_days_per_cycle", "used": used, "limit_value": max_days})
    plan = {"kind": kind, "employee_id": employee.id, "dates": [{"date": d.isoformat(), "portion": p} for d, p in dates],
            "skipped_dates": skipped}
    if data.get("dry_run"):
        state = ar.payroll_state(db, cid, employee.id, start)
        attributes = {"team_ids": ar.team_ids_on(db, cid, employee.id, start), "on_duty_kind": kind,
                      "reason_code": reason_code, "subject_kind": "ON_DUTY", "payroll_state": state,
                      "work_date": start.isoformat()}
        plan["route_preview"] = approvals.preview_route(db, cid, ar.MODULE, employee.id, attributes,
                                                        requester_user_id=principal.user_id)
        return plan

    batch_id = str(uuid.uuid4())
    rows = []
    for d, slot in dates:
        row = OnDutyRequest(company_id=cid, employee_id=employee.id, date=d, portion=slot, reason=reason_text,
                            status="PENDING", requested_by_user_id=principal.user_id, kind=kind,
                            start_time=start_time, end_time=end_time, batch_id=batch_id,
                            requires_punch=bool(od.get("requires_punch")), reason_code=reason_code)
        db.add(row)
        rows.append(row)
    db.flush()
    state = ar.payroll_state(db, cid, employee.id, start)
    attributes = {"team_ids": ar.team_ids_on(db, cid, employee.id, start), "on_duty_kind": kind,
                  "reason_code": reason_code, "subject_kind": "ON_DUTY", "payroll_state": state,
                  "legal_entity_id": ar.placement_on(db, cid, employee.id, start).legal_entity_id}
    request = approvals.submit(db, principal, module=ar.MODULE, subject_type=SUBJECT, subject_id=rows[0].id,
                               subject_employee_id=employee.id,
                               context={"work_date": start.isoformat(), "date": start, "end": end, "kind": kind,
                                        "batch_id": batch_id, "days": len(rows), "portion": rows[0].portion},
                               attributes=attributes)
    for row in rows:
        row.approval_request_id = request.id
    db.flush()
    AuditLogService.log(db, principal, ar.AUDIT_MODULE, "ON_DUTY_REQUESTED",
                        f"{'Remote work' if kind == 'REMOTE_WORK' else 'On-duty'} request for employee "
                        f"#{employee.id}, {start.isoformat()} to {end.isoformat()} ({len(rows)} day(s))",
                        "on_duty_request", rows[0].id,
                        details={"batch_id": batch_id, "kind": kind, "dates": [d.isoformat() for d, _ in dates],
                                 "approval_request_id": request.id})
    return {**plan, "batch_id": batch_id, "approval_request_id": request.id,
            "rows": [on_duty_json(r) for r in rows]}


def on_duty_json(row: OnDutyRequest, employee: Optional[Employee] = None) -> dict:
    return {**ti.on_duty_json(row), "kind": row.kind, "batch_id": row.batch_id, "reason_code": row.reason_code,
            "start_time": row.start_time.strftime("%H:%M") if row.start_time else None,
            "end_time": row.end_time.strftime("%H:%M") if row.end_time else None,
            "requires_punch": row.requires_punch, "employee_name": employee.full_name if employee else None}


def _batch(db: Session, company_id: int, row: OnDutyRequest) -> List[OnDutyRequest]:
    if not row.batch_id:
        return [row]
    return (db.query(OnDutyRequest)
            .filter(OnDutyRequest.company_id == company_id, OnDutyRequest.batch_id == row.batch_id)
            .order_by(OnDutyRequest.date).with_for_update().all())


def cancel(db: Session, principal: Principal, on_duty_id: int, *, row_version: Optional[int] = None) -> dict:
    """Cancel the batch: the requester (or the employee) before the first date, else ``attendance:regularize`` in
    scope (OD-8 replaced); a PENDING batch withdraws its one approval."""
    row = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == principal.company_id,
                                         OnDutyRequest.id == on_duty_id).with_for_update().first()
    if row is None:
        raise not_found("On-duty request")
    own = authz.is_self(principal, row.employee_id) or row.requested_by_user_id == principal.user_id
    hr = authz.can_access_employee(db, principal, "attendance:regularize", row.employee_id)
    if not own and not hr:
        raise not_found("On-duty request")
    ti._check_row_version(row, row_version)
    rows = [r for r in _batch(db, principal.company_id, row) if r.status in ("PENDING", "APPROVED")]
    if not rows:
        raise conflict(f"This request is already {row.status.lower()}.", "ON_DUTY_CLOSED")
    employee = db.get(Employee, row.employee_id)
    today = ar.employee_today(db, employee)
    if any(r.status == "APPROVED" for r in rows) and not hr and min(r.date for r in rows) <= today:
        raise forbidden("Ask HR to withdraw on-duty work that has started.")
    if any(r.status == "PENDING" for r in rows) and row.approval_request_id:
        request = db.get(approvals.ApprovalRequest, row.approval_request_id)
        if request is not None and request.status == "PENDING":
            approvals.withdraw(db, principal, module=ar.MODULE, subject_type=request.subject_type,
                               subject_id=request.subject_id, reason="On-duty request cancelled")
    for r in rows:
        r.status, r.cancelled_at = "CANCELLED", ar._now()
        r.row_version += 1
        db.flush()
        ti.log_change(db, r.company_id, record_type="on_duty", record_id=r.id, change="VOIDED",
                      content=ti.on_duty_content(r), employee_id=r.employee_id, date_from=r.date,
                      actor_user_id=principal.user_id)
    AuditLogService.log(db, principal, ar.AUDIT_MODULE, "ON_DUTY_CANCELLED", f"Cancelled on-duty request #{row.id}",
                        "on_duty_request", row.id, details={"batch_id": row.batch_id, "rows": [r.id for r in rows]})
    return on_duty_json(row, employee)


def list_on_duty(db: Session, principal: Principal, *, mine: bool = False, kind: Optional[str] = None,
                 employee_id: Optional[int] = None, status: Optional[str] = None, start: Optional[date] = None,
                 end: Optional[date] = None, limit: int = 200) -> List[dict]:
    query = db.query(OnDutyRequest).filter(OnDutyRequest.company_id == principal.company_id)
    if mine or not principal.has("attendance:read"):
        if principal.employee_id is None:
            raise forbidden()
        query = query.filter((OnDutyRequest.employee_id == principal.employee_id)
                             | (OnDutyRequest.requested_by_user_id == principal.user_id))
    else:
        scope = authz.employee_scope(db, principal, "attendance:read")
        query = query.filter(scope.clause(OnDutyRequest.employee_id)
                             | (OnDutyRequest.requested_by_user_id == principal.user_id))
    if kind:
        query = query.filter(OnDutyRequest.kind == kind)
    if employee_id is not None:
        query = query.filter(OnDutyRequest.employee_id == employee_id)
    if status:
        query = query.filter(OnDutyRequest.status.in_(status.split(",")))
    if start is not None:
        query = query.filter(OnDutyRequest.date >= start)
    if end is not None:
        query = query.filter(OnDutyRequest.date <= end)
    rows = query.order_by(OnDutyRequest.date.desc(), OnDutyRequest.id.desc()).limit(min(limit, 500)).all()
    names: Dict[int, Employee] = {e.id: e for e in db.query(Employee).filter(
        Employee.company_id == principal.company_id, Employee.id.in_({r.employee_id for r in rows} or {0}))}
    return [on_duty_json(r, names.get(r.employee_id)) for r in rows]
