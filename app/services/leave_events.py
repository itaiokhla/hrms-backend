"""EVENT leaves (03-leave/06 §6.1; SPINE §6.1, V3-018, V3-047, V3-053(7)) — V2_ACTIVE tenants.

An EVENT type (``leave_types.kind = 'EVENT'``, type rule ``balance_scope EVENT`` with an ``event`` block) is granted
per occurrence of a life event (expected delivery, birth, adoption, death, marriage…), not per year. The employee (or
HR / a manager on their behalf, with the same permission checks as leave requests) records the event; the plan rule of
the type on the event date decides whether it is accepted (``event_types``, eligibility, ``max_occurrences`` per
employment) and how much it gives (``entitlement_units``; WEEKS are stored as 7 calendar days each) and until when
(``take_within_days`` → ``take_until``). Requests reference it through ``leaves.event_id``; the request validator
(``leave_requests``) checks the window and the remaining units.

**Ledger decision.** EVENT leave posts no GRANT and no USE rows: the event row is the balance. ``used_units`` is the
charged units of the active ``leave_days`` of the event's requests (recomputed from scratch by ``sync_used`` whenever
``leave_postings`` writes, voids or regenerates a request's days — approval, cancel, partial cancel, modify, convert,
recount — so it is never double counted); PENDING requests reserve their units; remaining = entitled − used − pending.
Payroll reads only ``leave_days`` (generated under the type rule's counting basis, e.g. CALENDAR_DAYS for maternity),
exactly as for any other leave. EVENT units never take part in year end (no carry-forward, lapse or encashment).
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import authz, concurrency
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.leave import Leave, LeaveType
from app.models.leave_v2 import LeaveEvent
from app.models.time_inputs import LeaveDay
from app.services import leave_access, leave_plans
from app.services.audit_log_service import AuditLogService

ZERO = Decimal(0)
DUPLICATE_WINDOW_DAYS = 180  # EVENT_DUPLICATE_WINDOW_DAYS (03-leave/06 §6.1 step 2, LH-EVT-04)
CONTINUOUS_UNITS = ("CALENDAR_DAYS", "WEEKS")  # one continuous request per event (SPINE §6.1)
EVENT_STATUS = {"NOT_ELIGIBLE": 422, "MAX_INSTANCES": 422, "INVALID_FIELDS": 422, "PLAN_NOT_ASSIGNED": 422,
                "EVENT_WINDOW_CLOSED": 422, "INVALID_RANGE": 422, "PLAN_CONFLICT": 409, "EVENT_IN_USE": 409,
                "INVALID_TRANSITION": 409}


def _num(value: Optional[Decimal]):
    from app.services.leave_ledger import _num as num

    return num(value)


def is_event_type(leave_type: Optional[LeaveType]) -> bool:
    return leave_type is not None and leave_type.kind == "EVENT"


def event_rules(rule: Optional[dict]) -> dict:
    return (rule or {}).get("event") or {}


def entitled_units(rules: dict) -> Decimal:
    units = Decimal(str(rules.get("entitlement_units") or "0"))
    return units * 7 if (rules.get("duration_unit") or "CALENDAR_DAYS") == "WEEKS" else units


def earliest_start(event: LeaveEvent, rules: dict) -> date:
    return event.event_date + timedelta(days=int(rules.get("earliest_start_offset_days") or 0))


# --------------------------------------------------------------------------- usage (the EVENT balance)

def usage(db: Session, event: LeaveEvent, *, exclude_leave_id: Optional[int] = None) -> Tuple[Decimal, Decimal]:
    """(used, pending) units of an event, read live: charged active ``leave_days`` of its requests of the event's type,
    and the units of its PENDING requests; ``exclude_leave_id`` leaves one request out (its own edit or change)."""
    used_q = (db.query(func.coalesce(func.sum(LeaveDay.units), 0))
              .join(Leave, (Leave.company_id == LeaveDay.company_id) & (Leave.id == LeaveDay.leave_id))
              .filter(LeaveDay.company_id == event.company_id, LeaveDay.voided_at.is_(None),
                      LeaveDay.counts_against_balance.is_(True), Leave.event_id == event.id,
                      Leave.leave_type == event.leave_type_code))
    pending_q = db.query(func.coalesce(func.sum(Leave.number_of_days), 0)).filter(
        Leave.company_id == event.company_id, Leave.event_id == event.id, Leave.leave_type == event.leave_type_code,
        Leave.status == "pending")
    if exclude_leave_id is not None:
        used_q = used_q.filter(Leave.id != exclude_leave_id)
        pending_q = pending_q.filter(Leave.id != exclude_leave_id)
    return Decimal(used_q.scalar() or 0), Decimal(pending_q.scalar() or 0)


def remaining(db: Session, event: LeaveEvent, *, exclude_leave_id: Optional[int] = None) -> Decimal:
    used, pending = usage(db, event, exclude_leave_id=exclude_leave_id)
    return Decimal(event.entitled_units) - used - pending


def sync_used(db: Session, company_id: int, event_id: Optional[int]) -> None:
    """Keep ``leave_events.used_units`` equal to the approved charge (called by ``leave_postings`` after every write)."""
    if event_id is None:
        return
    event = db.query(LeaveEvent).filter(LeaveEvent.company_id == company_id, LeaveEvent.id == event_id).first()
    if event is None:
        return
    used, _ = usage(db, event)
    if Decimal(event.used_units or 0) != used:
        event.used_units = used
        event.row_version = (event.row_version or 1) + 1
        db.flush()


def has_active_requests(db: Session, event: LeaveEvent, *, exclude_leave_id: Optional[int] = None) -> bool:
    query = db.query(Leave.id).filter(Leave.company_id == event.company_id, Leave.event_id == event.id,
                                      Leave.leave_type == event.leave_type_code,
                                      Leave.status.in_(("pending", "approved")))
    if exclude_leave_id is not None:
        query = query.filter(Leave.id != exclude_leave_id)
    return query.first() is not None


def open_events(db: Session, company_id: int, employee_id: int, type_code: str) -> List[LeaveEvent]:
    return (db.query(LeaveEvent).filter(LeaveEvent.company_id == company_id, LeaveEvent.employee_id == employee_id,
                                        LeaveEvent.leave_type_code == type_code, LeaveEvent.status == "OPEN")
            .order_by(LeaveEvent.event_date, LeaveEvent.id).all())


def card(db: Session, event: LeaveEvent, *, exclude_leave_id: Optional[int] = None) -> dict:
    """One event of the EVENT balance card: entitled / used / pending / remaining."""
    used, pending = usage(db, event, exclude_leave_id=exclude_leave_id)
    entitled = Decimal(event.entitled_units)
    return {"event_id": event.id, "event_type": event.event_type, "event_date": event.event_date.isoformat(),
            "take_until": event.take_until.isoformat() if event.take_until else None, "status": event.status,
            "entitled": _num(entitled), "used": _num(used), "pending": _num(pending),
            "remaining": _num(entitled - used - pending)}


# --------------------------------------------------------------------------- reads

def _masked(db: Session, principal: Principal, event: LeaveEvent, leave_type: Optional[LeaveType]) -> bool:
    """SENSITIVE event types (bereavement…) show as protected leave to anyone but the employee and medical viewers."""
    if leave_type is None or leave_type.sensitivity != "SENSITIVE" or event.employee_id == principal.employee_id:
        return False
    return not (principal.has("leave:view_medical") and authz.can_access_employee(
        db, principal, "leave:view_medical", event.employee_id))


def event_json(db: Session, event: LeaveEvent, *, principal: Optional[Principal] = None,
               employee: Optional[Employee] = None, leave_type: Optional[LeaveType] = None) -> dict:
    leave_type = leave_type or db.query(LeaveType).filter(LeaveType.company_id == event.company_id,
                                                          LeaveType.code == event.leave_type_code).first()
    employee = employee or db.get(Employee, event.employee_id)
    rule = leave_plans.rules_for(db, event.company_id, event.employee_id, event.leave_type_code, event.event_date)
    rules = event_rules(rule)
    data = card(db, event) | {
        "id": event.id, "employee_id": event.employee_id, "employee_name": employee.full_name if employee else None,
        "leave_type": event.leave_type_code, "leave_type_name": leave_type.name if leave_type else None,
        "duration_unit": rules.get("duration_unit"), "earliest_start": earliest_start(event, rules).isoformat(),
        "note": event.note, "row_version": event.row_version,
        "created_at": event.created_at.isoformat() if event.created_at else None}
    if principal is not None and _masked(db, principal, event, leave_type):
        data.update(leave_type="protected", leave_type_name="Protected leave", event_type=None, note=None)
    return data


def list_events(db: Session, principal: Principal, *, scope: str = "mine", status: Optional[str] = None,
                employee_id: Optional[int] = None) -> List[dict]:
    leave_access.require_v2(db, principal.company_id)
    query = db.query(LeaveEvent).filter(LeaveEvent.company_id == principal.company_id)
    if scope == "mine":
        if principal.employee_id is None:
            return []
        query = query.filter(LeaveEvent.employee_id == principal.employee_id)
    else:
        if not principal.has("leave:read"):
            raise forbidden()
        query = query.filter(authz.employee_scope(db, principal, "leave:read").clause(LeaveEvent.employee_id))
        if principal.employee_id is not None:
            query = query.filter(LeaveEvent.employee_id != principal.employee_id)
    if status:
        query = query.filter(LeaveEvent.status == status.upper())
    if employee_id:
        query = query.filter(LeaveEvent.employee_id == employee_id)
    rows = query.order_by(LeaveEvent.event_date.desc(), LeaveEvent.id.desc()).limit(200).all()
    types = {t.code: t for t in db.query(LeaveType).filter(LeaveType.company_id == principal.company_id)}
    return [event_json(db, row, principal=principal, leave_type=types.get(row.leave_type_code)) for row in rows]


# --------------------------------------------------------------------------- commands

def _subject(db: Session, principal: Principal, employee_id: Optional[int]) -> Tuple[Employee, str]:
    """Self (``leave:apply``), on behalf (``leave:apply_on_behalf`` + setting) or HR (``leave:record``), in scope."""
    from app.services import leave_requests

    if employee_id is None or employee_id == principal.employee_id:
        return leave_requests.resolve_subject(db, principal, None, mode="APPLY")
    if principal.has("leave:apply_on_behalf"):
        return leave_requests.resolve_subject(db, principal, employee_id, mode="APPLY")
    return leave_requests.resolve_subject(db, principal, employee_id, mode="RECORD")


def _error(code: str, message: str, **details) -> AppError:
    return AppError(EVENT_STATUS.get(code, 422), message, code, extra={"details": details} if details else None)


def create(db: Session, principal: Principal, *, leave_type: str, event_type: str, event_date: date,
           employee_id: Optional[int] = None, note: Optional[str] = None) -> LeaveEvent:
    """Record an event (03-leave/06 §6.1 steps 1-3): validated against the employee's plan rule on the event date."""
    from app.services import leave_requests

    cid = principal.company_id
    leave_access.require_v2(db, cid)
    employee, source = _subject(db, principal, employee_id)
    concurrency.lock_employees(db, [employee.id])
    lt = db.query(LeaveType).filter(LeaveType.company_id == cid, LeaveType.code == leave_type).first()
    if lt is None or not lt.is_active or not is_event_type(lt):
        raise bad_request("Choose an event leave type (birth, adoption, bereavement…).", "INVALID_LEAVE_TYPE")
    resolution = leave_plans.resolve(db, cid, employee.id, event_date)
    rule = resolution.rule(lt.code)
    if rule is None:
        if resolution.status not in ("RESOLVED", "LEGACY"):
            raise _error("PLAN_NOT_ASSIGNED", "No leave plan applies to you on the event date; contact HR.")
        raise _error("NOT_ELIGIBLE", "This leave type is not available to you.")
    rules = event_rules(rule)
    if not rules:
        raise _error("NOT_ELIGIBLE", "This leave type is not available to you.")
    if event_type not in (rules.get("event_types") or []):
        raise _error("INVALID_FIELDS", f"{lt.name} is not given for this kind of event.", field="event_type",
                     allowed=list(rules.get("event_types") or []))
    today = leave_requests.local_today(db, employee)
    if abs((event_date - today).days) > 366:
        raise _error("INVALID_RANGE", "The event date must be within a year of today.", field="event_date")
    elig = leave_plans.eligibility(db, cid, employee, lt, rule, event_date)
    if elig.status in ("NOT_ELIGIBLE", "ACCRUE_ONLY"):
        failure = elig.first_failure() or {}
        if failure.get("code") in leave_plans.STRUCTURAL_CHECKS:  # never explain a protected attribute (§7.1)
            raise _error("NOT_ELIGIBLE", "This leave type is not available to you.")
        details = {"check": failure.get("code"), "date": event_date.isoformat()}
        if elig.available_from:
            details["available_from"] = elig.available_from.isoformat()
        raise _error("NOT_ELIGIBLE", f"Not available on {event_date:%d %b %Y}.", **details)
    events = db.query(LeaveEvent).filter(LeaveEvent.company_id == cid, LeaveEvent.employee_id == employee.id,
                                         LeaveEvent.leave_type_code == lt.code,
                                         LeaveEvent.status != "CANCELLED").all()
    limit = rules.get("max_occurrences")
    in_employment = [e for e in events if e.event_date >= employee.hire_date]  # occurrence_scope EMPLOYMENT
    if limit is not None and len(in_employment) + 1 > int(limit):
        raise _error("MAX_INSTANCES", f"{lt.name} can be taken for at most {limit} event(s) per employment.",
                     limit=int(limit), used=len(in_employment))
    for other in events:
        if other.status == "OPEN" and other.event_type == event_type and \
                abs((event_date - other.event_date).days) < DUPLICATE_WINDOW_DAYS:
            raise _error("PLAN_CONFLICT", "An open event of this kind is already recorded; close or cancel it first.",
                         reason="DUPLICATE_EVENT", event_id=other.id)
    take_within = rules.get("take_within_days")
    take_until = event_date + timedelta(days=int(take_within)) if take_within else None
    if take_until is not None and take_until < today and source != "HR_RECORDED":
        raise _error("EVENT_WINDOW_CLOSED", f"Leave for this event had to be taken by {take_until:%d %b %Y}.",
                     reason="TOO_LATE", take_until=take_until.isoformat())
    row = LeaveEvent(company_id=cid, employee_id=employee.id, leave_type_code=lt.code, event_type=event_type,
                     event_date=event_date, entitled_units=entitled_units(rules), used_units=ZERO,
                     take_until=take_until, status="OPEN", note=(note or "").strip() or None,
                     created_by_user_id=principal.user_id, row_version=1)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, "Leave", "EVENT_CREATED",
                        f"Recorded a leave event #{row.id} for {employee.full_name}", "leave_event", row.id,
                        details={"source": source, "entitled_units": str(row.entitled_units),
                                 "eligibility": elig.status})
    return row


def _load(db: Session, principal: Principal, event_id: int, *, lock: bool = False) -> LeaveEvent:
    query = db.query(LeaveEvent).filter(LeaveEvent.company_id == principal.company_id, LeaveEvent.id == event_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Leave event")
    return row


def _can_manage(db: Session, principal: Principal, event: LeaveEvent) -> bool:
    if event.employee_id == principal.employee_id:
        return principal.has("leave:apply")
    return any(principal.has(code) and authz.can_access_employee(db, principal, code, event.employee_id)
               for code in ("leave:apply_on_behalf", "leave:record", "leave:change_any"))


def cancel(db: Session, principal: Principal, event_id: int, reason: Optional[str] = None) -> LeaveEvent:
    """An event recorded in error (§6.1 step 7): OPEN, no PENDING / APPROVED request and nothing used."""
    leave_access.require_v2(db, principal.company_id)
    event = _load(db, principal, event_id, lock=True)
    if not _can_manage(db, principal, event):
        if event.employee_id != principal.employee_id and not authz.can_access_employee(
                db, principal, "leave:read", event.employee_id):
            raise not_found("Leave event")
        raise forbidden("You cannot cancel this event.")
    concurrency.lock_employees(db, [event.employee_id])
    if event.status != "OPEN":
        raise _error("INVALID_TRANSITION", "Only an open event can be cancelled.")
    used, pending = usage(db, event)
    if used > 0 or pending > 0 or has_active_requests(db, event):
        raise _error("EVENT_IN_USE", "Leave was booked for this event: cancel or convert that leave first.",
                     used=_num(used), pending=_num(pending))
    event.status = "CANCELLED"
    event.row_version = (event.row_version or 1) + 1
    if (reason or "").strip():
        event.note = ((event.note + " | ") if event.note else "") + f"Cancelled: {reason.strip()}"[:200]
    db.flush()
    AuditLogService.log(db, principal, "Leave", "EVENT_CANCELLED", f"Cancelled leave event #{event.id}",
                        "leave_event", event.id)
    return event


def close_finished(db: Session, company_id: int, today: date) -> int:
    """Daily housekeeping (LH-EVT-02): an OPEN event whose window has passed, or whose units are all used by leave that
    has ended, becomes CLOSED. Unused event units simply end (no ledger row)."""
    closed = 0
    for event in db.query(LeaveEvent).filter(LeaveEvent.company_id == company_id, LeaveEvent.status == "OPEN").all():
        used, pending = usage(db, event)
        window_over = event.take_until is not None and event.take_until < today
        last = db.query(func.max(Leave.end_date)).filter(
            Leave.company_id == company_id, Leave.event_id == event.id,
            Leave.status.in_(("pending", "approved"))).scalar()
        used_up = used >= Decimal(event.entitled_units) and pending == 0 and (last is None or last < today)
        if (window_over and pending == 0 and (last is None or last < today)) or used_up:
            event.status = "CLOSED"
            event.row_version = (event.row_version or 1) + 1
            closed += 1
    if closed:
        db.flush()
    return closed
