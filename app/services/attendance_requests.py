"""Attendance requests: missed punch, added session, correction, void, late / early excuse, status override (AS-M4).

Owner: docs/attendance-shift/04-attendance/04-regularization-requests-approvals.md (§3 kinds and payloads, §4 states,
§6 limits and windows, §7 reason codes, §9 routing on the generic engine, §10 HR direct edit, §14 payroll states).

* Three modes: SELF (``attendance:request``; kinds in the resolved ``ATTENDANCE_REQUEST_POLICY.self_kinds``, windows
  and limits checked), ON_BEHALF (``attendance:request_on_behalf`` covering E; limits are an acknowledged warning)
  and DIRECT (``attendance:regularize``, ``STATUS_OVERRIDE``: ``attendance:override``; applied at once, never on
  oneself — 403 ``SELF_EDIT_FORBIDDEN`` checked before any scope lookup).
* SELF / ON_BEHALF go through the generic engine, module ``attendance``, subject ``ATTENDANCE_REQUEST``; the route is
  selected at submit from the employee's attributes **on the shift date** (team ids, legal entity, branch, kind,
  reason, days back, minutes, payroll state). Nothing auto-approves (D-AS-72).
* Raw punches are never edited: an approved request writes REQUEST punches (DIRECT: MANUAL), new sessions, voids
  (``void_reason = REQUEST``) and break rows (§3.4); MARK_EXCUSE and STATUS_OVERRIDE are evaluation inputs read by
  ``attendance_days.load_inputs_many``. Every change re-evaluates the day synchronously (trigger
  ``REQUEST_DECIDED``) and writes ``log_change('attendance_request')`` at apply (JC-AS-11).
* v2 tenants only (spine §15.7): v1 tenants get 404 on every path here.

Services never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import status as http_status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.attendance import Attendance, AttendanceBreak, AttendancePunch
from app.models.attendance_requests import (
    ATTENDANCE_REQUEST_KINDS,
    OPEN_REQUEST_STATUSES,
    REQUEST_MODES,
    AttendanceAttachment,
    AttendancePeriodClose,
    AttendanceReasonCode,
    AttendanceRequest,
)
from app.models.employee import Employee
from app.payroll.services import approvals
from app.services.audit_log_service import AuditLogService

MODULE = "attendance"
SUBJECT = "ATTENDANCE_REQUEST"
AUDIT_MODULE = "Attendance"
POLICY_TYPE = "ATTENDANCE_REQUEST_POLICY"
SESSION_BOUND = ("CORRECTION", "VOID_SESSION")
EXCUSE_MARKS = ("LATE", "EARLY")
HALF_VALUES = ("PRESENT", "ABSENT")
MAX_BREAKS = 4
MAX_LIST = 200

KIND_LABELS = {"MISSED_PUNCH": "Missed punch", "ADD_SESSION": "Add a session", "CORRECTION": "Correct times",
               "VOID_SESSION": "Remove a session", "MARK_EXCUSE": "Excuse late / early",
               "STATUS_OVERRIDE": "Mark the day"}

# §7.1 seeds + SP-AS-018 decision codes (frozen copy in migration 0052): code, label, applies_to, counts, sensitive.
REASON_SEEDS: Tuple[Tuple[str, str, Tuple[str, ...], bool, bool], ...] = (
    ("FORGOT_TO_PUNCH", "Forgot to punch", ("MISSED_PUNCH", "ADD_SESSION"), True, False),
    ("DEVICE_FAILURE", "Device not working", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "VOID_SESSION", "REVIEW"),
     False, False),
    ("NETWORK_ISSUE", "Network problem", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION"), False, False),
    ("APP_ISSUE", "App problem", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION"), False, False),
    ("POWER_OUTAGE", "Power cut", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION"), False, False),
    ("ON_CLIENT_SITE", "Worked at a client site", ("MISSED_PUNCH", "ADD_SESSION", "CORRECTION", "ON_DUTY"), True,
     False),
    ("OFFICIAL_WORK", "Official work", ("MISSED_PUNCH", "ADD_SESSION", "MARK_EXCUSE", "ON_DUTY"), True, False),
    ("TRANSPORT_DELAY", "Transport delay", ("MARK_EXCUSE",), True, False),
    ("MEDICAL", "Medical", ("MARK_EXCUSE",), True, True),
    ("PERSONAL", "Personal", ("MARK_EXCUSE",), True, False),
    ("WRONG_PUNCH", "Wrong punch recorded", ("CORRECTION", "VOID_SESSION"), True, False),
    ("SUPERVISOR_INSTRUCTION", "Supervisor's instruction", ("ADD_SESSION", "STATUS_OVERRIDE", "ROSTER_CHANGE"),
     False, False),
    ("DATA_CORRECTION", "Data correction", ("CORRECTION", "VOID_SESSION", "STATUS_OVERRIDE", "REVIEW"), False, False),
    ("EMPLOYER_IDLE", "Work stopped by employer", ("STATUS_OVERRIDE",), False, False),
    ("RAIN_OFF", "Rain / weather stoppage", ("STATUS_OVERRIDE",), False, False),
    ("OTHER", "Other (explain)", ATTENDANCE_REQUEST_KINDS + ("ON_DUTY",), True, False),
    ("NEEDS_DETAILS", "More details needed", ("DECISION",), False, False),
    ("NOT_VERIFIABLE", "Could not be verified", ("DECISION",), False, False),
    ("OUTSIDE_POLICY", "Not allowed by policy", ("DECISION",), False, False),
    ("DUPLICATE_REQUEST", "Duplicate request", ("DECISION",), False, False),
)


def unprocessable(detail: str, code: str, details: Optional[Mapping[str, Any]] = None) -> AppError:
    return AppError(http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail, code,
                    extra={"details": dict(details)} if details else None)


def self_edit_forbidden() -> AppError:
    return AppError(http_status.HTTP_403_FORBIDDEN, "You cannot correct your own attendance.", "SELF_EDIT_FORBIDDEN")


def _now() -> datetime:
    return clock.utcnow().astimezone(timezone.utc)


# --------------------------------------------------------------------------- setup (signup, migration 0052)


def seed_company(db: Session, company_id: int) -> None:
    """Reason codes and the ``default`` routes of modules attendance / timesheet (1 primary_manager step)."""
    from app.models.attendance_requests import ApprovalRoute
    from app.models.leave import ApprovalChainStep

    have = {c for (c,) in db.query(AttendanceReasonCode.code).filter(AttendanceReasonCode.company_id == company_id)}
    for code, label, applies, counts, sensitive in REASON_SEEDS:
        if code not in have:
            db.add(AttendanceReasonCode(company_id=company_id, code=code, label=label, applies_to=list(applies),
                                        counts_toward_limit=counts, sensitive=sensitive, is_seed=True))
    for module in ("attendance", "timesheet"):
        exists = db.query(ApprovalRoute.id).filter(ApprovalRoute.company_id == company_id,
                                                   ApprovalRoute.module == module,
                                                   ApprovalRoute.route_key == "default").first()
        if exists is None:
            route = ApprovalRoute(company_id=company_id, module=module, route_key="default", name="Default",
                                  priority=0, criteria={})
            db.add(route)
            db.flush()
            db.add(ApprovalChainStep(company_id=company_id, module=module, route_id=route.id, step_order=1,
                                     approver_type="primary_manager"))
    db.flush()


# --------------------------------------------------------------------------- policy and context


def policy_fields(db: Session, company_id: int, employee_id: int, d: date) -> Dict[str, Any]:
    """The resolved ATTENDANCE_REQUEST_POLICY for E on D merged over the STANDARD defaults; nothing resolved (or a
    LEGACY_V1 profile) means today's behaviour: no self-service kinds and no limits (§6.1 bracketed values)."""
    from app.payroll.policy_types import AttendanceRequestPolicy
    from app.services import attendance_policies

    fields = AttendanceRequestPolicy().model_dump(mode="json")
    found = attendance_policies.resolve(db, company_id, POLICY_TYPE, employee_id, d)
    if found is None:
        fields.update(profile="LEGACY_V1", self_kinds=[], limits=[])
        return fields
    fields.update(found.fields or {})
    if fields.get("profile") == "LEGACY_V1":
        fields.update(self_kinds=[], limits=[])
    fields["policy_id"], fields["policy_version"] = found.policy_id, found.version
    return fields


@dataclass
class Placement:
    legal_entity_id: Optional[int]
    branch_id: Optional[int]
    pay_group_id: Optional[int]


def placement_on(db: Session, company_id: int, employee_id: int, d: date) -> Placement:
    from app.models.org import EmployeeAssignment

    row = (db.query(EmployeeAssignment)
           .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                   EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= d,
                   or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= d))
           .order_by(EmployeeAssignment.effective_from.desc()).first())
    if row is None:
        return Placement(None, None, None)
    return Placement(row.legal_entity_id, row.branch_id, row.pay_group_id)


def employee_today(db: Session, employee: Employee) -> date:
    from app.services import org_structure_service

    return org_structure_service.employee_local_date(db, employee, _now())


def payroll_state(db: Session, company_id: int, employee_id: int, d: date) -> str:
    from app.payroll.services import calendars

    return calendars.period_state_on(db, company_id, employee_id, d, now=_now())


def cycle_window(db: Session, company_id: int, employee_id: int, d: date) -> Tuple[date, date, bool]:
    """The attendance cycle containing D (D-AS-31): the pay period's attendance window of E's pay group, else the
    calendar month (``False`` = "calendar month (no pay period)")."""
    from app.payroll.services import calendars

    from app.services import payroll_sync

    where = placement_on(db, company_id, employee_id, d)
    if where.pay_group_id:
        # JC-AS-10: the period whose attendance window holds D (limits count per window, D-AS-31).
        period = payroll_sync.period_of_window(db, company_id, where.pay_group_id, d)
        if period is not None:
            start, end = period.attendance_start, period.attendance_end
            if start <= d <= end:
                return start, end, True
            return period.period_start, period.period_end, True
    first = d.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first, last, False


def _limit_window(db: Session, company_id: int, employee_id: int, d: date, period: str) -> Tuple[date, date]:
    if period == "DAY":
        return d, d
    if period == "WEEK":
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=6)
    start, end, _ = cycle_window(db, company_id, employee_id, d)
    return start, end


def team_ids_on(db: Session, company_id: int, employee_id: int, d: date) -> List[int]:
    from app.services import team_service

    rows = team_service.active_memberships(db, company_id, employee_id, d)
    primary = [r.team_id for r in rows if r.is_primary_team]
    # The owning team for routing is the primary team (§9.3 step 1); without one every team counts for criteria.
    return primary or sorted({r.team_id for r in rows})


# --------------------------------------------------------------------------- reason codes


def reason_codes(db: Session, principal: Principal, *, kind: Optional[str] = None, include_inactive: bool = False,
                 include_system: bool = False) -> List[dict]:
    query = db.query(AttendanceReasonCode).filter(AttendanceReasonCode.company_id == principal.company_id)
    if not include_inactive:
        query = query.filter(AttendanceReasonCode.status == "ACTIVE")
    if not include_system:
        query = query.filter(AttendanceReasonCode.system_only.is_(False))
    rows = query.order_by(AttendanceReasonCode.id).all()
    if kind:
        rows = [r for r in rows if kind in (r.applies_to or ())]
    return [reason_json(r) for r in rows]


def reason_json(row: AttendanceReasonCode) -> dict:
    return {"code": row.code, "label": row.label, "applies_to": list(row.applies_to or ()),
            "requires_attachment": row.requires_attachment, "counts_toward_limit": row.counts_toward_limit,
            "sensitive": row.sensitive, "status": row.status, "is_seed": row.is_seed, "row_version": row.row_version}


def _reason(db: Session, company_id: int, code: Optional[str], kind: str) -> AttendanceReasonCode:
    if not code:
        raise bad_request("Choose a reason.", "REASON_REQUIRED")
    row = db.query(AttendanceReasonCode).filter(AttendanceReasonCode.company_id == company_id,
                                                AttendanceReasonCode.code == code).first()
    if row is None or row.status != "ACTIVE" or kind not in (row.applies_to or ()):
        raise bad_request("This reason cannot be used for this kind of request.", "INVALID_REASON")
    return row


# --------------------------------------------------------------------------- payload validation (§3.3)


def parse_instant(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise bad_request(f"{field} is not a valid date and time.", "INVALID_FIELDS")
    else:
        raise bad_request(f"Enter {field}.", "INVALID_FIELDS")
    if moment.tzinfo is None:
        raise bad_request("Give every time with a timezone offset (for example +05:30).", "TIMEZONE_REQUIRED")
    return moment.astimezone(timezone.utc)


ALLOWED_KEYS = {
    "MISSED_PUNCH": {"direction", "at", "break_kind", "pair_out_at"},
    "ADD_SESSION": {"in_at", "out_at", "work_mode", "breaks"},
    "CORRECTION": {"in_at", "out_at", "breaks", "worked_through_breaks"},
    "VOID_SESSION": set(),
    "MARK_EXCUSE": {"mark", "minutes"},
    "STATUS_OVERRIDE": {"first_half", "second_half"},
}


@dataclass
class Prepared:
    """A validated request: normalized payload, target session, minutes (§6.2) and the base versions."""

    payload: Dict[str, Any]
    session: Optional[Attendance]
    minutes: int
    base_session_row_version: Optional[int]
    base_day_version: Optional[int]


def _session(db: Session, company_id: int, employee_id: int, session_id: Optional[int], d: date) -> Attendance:
    if not session_id:
        raise bad_request("Choose the session.", "SESSION_REQUIRED")
    row = (db.query(Attendance)
           .filter(Attendance.company_id == company_id, Attendance.id == session_id,
                   Attendance.employee_id == employee_id).first())
    if row is None:
        raise not_found("Attendance session")
    if row.voided_at is not None or row.date != d:
        raise unprocessable("This session is not on the request's date or was removed.", "NOT_ELIGIBLE",
                            {"session_id": session_id})
    return row


def _breaks(payload_breaks: Any, start: datetime, end: datetime) -> List[dict]:
    if payload_breaks is None:
        return []
    if not isinstance(payload_breaks, list) or len(payload_breaks) > MAX_BREAKS:
        raise bad_request(f"Give at most {MAX_BREAKS} breaks.", "INVALID_FIELDS")
    out = []
    for item in payload_breaks:
        if not isinstance(item, Mapping):
            raise bad_request("Each break needs a start and an end.", "INVALID_FIELDS")
        b_start = parse_instant(item.get("start_at"), "the break start")
        b_end = parse_instant(item.get("end_at"), "the break end")
        kind = item.get("kind") or "LUNCH"
        if kind not in ("LUNCH", "REST", "PERSONAL", "OTHER"):
            raise bad_request("Unknown break kind.", "INVALID_FIELDS")
        if not (start <= b_start < b_end <= end):
            raise bad_request("Breaks must fall inside the session.", "INVALID_RANGE")
        out.append({"kind": kind, "start_at": b_start, "end_at": b_end})
    out.sort(key=lambda b: b["start_at"])
    for a, b in zip(out, out[1:]):
        if b["start_at"] < a["end_at"]:
            raise bad_request("Breaks cannot overlap.", "INVALID_RANGE")
    return out


def _check_interval(db: Session, company_id: int, employee: Employee, d: date, start: datetime, end: datetime,
                    now: datetime, exclude_id: Optional[int] = None) -> None:
    from app.services import attendance_capture_policy, attendance_sessions

    if end <= start:
        raise bad_request("The end must be after the start.", "INVALID_RANGE")
    if end > now:
        raise unprocessable("Times cannot be in the future.", "REQUEST_WINDOW_CLOSED", {"reason": "FUTURE"})
    policy = attendance_capture_policy.effective(db, company_id, employee.id, d)
    if end - start > timedelta(hours=policy.max_session_hours):
        raise bad_request(f"A session is at most {policy.max_session_hours} hours.", "INVALID_RANGE")
    shift_date = attendance_sessions.resolve_shift_date(db, company_id, employee, start, "IN")
    if shift_date.date != d:
        raise unprocessable(f"This time belongs to {shift_date.date.isoformat()}.", "NOT_ELIGIBLE",
                            {"belongs_to": shift_date.date.isoformat()})
    attendance_sessions.check_overlap(db, company_id, employee.id, start, end, exclude_id=exclude_id)


def _day_row(db: Session, company_id: int, employee_id: int, d: date):
    from app.models.attendance import AttendanceDay

    return db.query(AttendanceDay).filter(AttendanceDay.company_id == company_id,
                                          AttendanceDay.employee_id == employee_id,
                                          AttendanceDay.shift_date == d).first()


def prepare(db: Session, company_id: int, employee: Employee, d: date, kind: str, payload: Mapping[str, Any],
            session_id: Optional[int], *, mode: str) -> Prepared:
    """P-1..P-9 for one request (validate_payload of §3.3); unknown keys are refused (400)."""
    from app.payroll.services import time_inputs as ti

    if kind not in ATTENDANCE_REQUEST_KINDS:
        raise bad_request("Unknown request kind.", "INVALID_KIND")
    payload = dict(payload or {})
    unknown = sorted(set(payload) - ALLOWED_KEYS[kind])
    if unknown:
        raise bad_request(f"Unknown fields: {', '.join(unknown)}.", "INVALID_FIELDS")
    facts = ti.day_facts(db, company_id, employee.id, d, d)[0]
    if not facts.employed:
        raise bad_request("The date is outside the employment.", "OUTSIDE_EMPLOYMENT")
    now = _now()
    today = employee_today(db, employee)
    day = _day_row(db, company_id, employee.id, d)
    base_day_version = day.version if day is not None else None
    if kind != "MARK_EXCUSE" and d > today:
        raise unprocessable("Requests for future dates are not possible.", "REQUEST_WINDOW_CLOSED",
                            {"reason": "FUTURE"})

    if kind == "MISSED_PUNCH":
        direction = payload.get("direction")
        if direction not in ("IN", "OUT", "BREAK_END"):
            raise bad_request("Choose which punch is missing (IN, OUT or BREAK_END).", "INVALID_FIELDS")
        at = parse_instant(payload.get("at"), "the punch time")
        if at > now:
            raise unprocessable("Times cannot be in the future.", "REQUEST_WINDOW_CLOSED", {"reason": "FUTURE"})
        clean: Dict[str, Any] = {"direction": direction, "at": at.isoformat()}
        if direction == "IN":
            out_at = parse_instant(payload.get("pair_out_at"), "the punch-out time")
            _check_interval(db, company_id, employee, d, at, out_at, now)
            clean["pair_out_at"] = out_at.isoformat()
            return Prepared(clean, None, int((out_at - at).total_seconds() // 60), None, base_day_version)
        session = _session_for_missing(db, company_id, employee.id, d, session_id)
        if direction == "OUT":
            if session.check_out_time is not None:
                raise unprocessable("This session already has a punch-out.", "NOT_ELIGIBLE")
            if at <= session.check_in_time:
                raise bad_request("The punch-out must be after the punch-in.", "INVALID_RANGE")
            _check_interval(db, company_id, employee, d, session.check_in_time, at, now, exclude_id=session.id)
            minutes = int((at - session.check_in_time).total_seconds() // 60)
        else:
            open_break = (db.query(AttendanceBreak)
                          .filter(AttendanceBreak.attendance_id == session.id, AttendanceBreak.ended_at.is_(None))
                          .first())
            if open_break is None:
                raise unprocessable("This session has no open break.", "NOT_ELIGIBLE")
            end = session.check_out_time or now
            if not open_break.started_at < at <= end:
                raise bad_request("The break end must fall after the break start and inside the session.",
                                  "INVALID_RANGE")
            minutes = int((at - open_break.started_at).total_seconds() // 60)
        return Prepared(clean, session, minutes, session.row_version, base_day_version)

    if kind == "ADD_SESSION":
        from app.services import attendance_capture_policy, attendance_sessions

        in_at = parse_instant(payload.get("in_at"), "the start")
        out_at = parse_instant(payload.get("out_at"), "the end")
        _check_interval(db, company_id, employee, d, in_at, out_at, now)
        policy = attendance_capture_policy.effective(db, company_id, employee.id, d)
        if attendance_sessions.sessions_on(db, company_id, employee.id, d) and not policy.multiple_sessions_allowed:
            raise unprocessable("Only one session per day is allowed here; correct the existing session instead.",
                                "REQUEST_KIND_DISABLED")
        work_mode = payload.get("work_mode") or "ON_SITE"
        if work_mode not in ("ON_SITE", "REMOTE", "FIELD"):
            raise bad_request("Unknown work mode.", "INVALID_FIELDS")
        breaks = _breaks(payload.get("breaks"), in_at, out_at)
        clean = {"in_at": in_at.isoformat(), "out_at": out_at.isoformat(), "work_mode": work_mode,
                 "breaks": [_break_json(b) for b in breaks]}
        return Prepared(clean, None, int((out_at - in_at).total_seconds() // 60), None, base_day_version)

    if kind == "CORRECTION":
        session = _session(db, company_id, employee.id, session_id, d)
        if not any(k in payload for k in ("in_at", "out_at", "breaks", "worked_through_breaks")):
            raise bad_request("Change the start, the end or the breaks.", "INVALID_FIELDS")
        in_at = parse_instant(payload["in_at"], "the start") if payload.get("in_at") else session.check_in_time
        out_at = parse_instant(payload["out_at"], "the end") if payload.get("out_at") else session.check_out_time
        if out_at is None:
            raise bad_request("Give the end time (the session has no punch-out).", "INVALID_FIELDS")
        _check_interval(db, company_id, employee, d, in_at, out_at, now, exclude_id=session.id)
        breaks = _breaks(payload.get("breaks"), in_at, out_at) if payload.get("breaks") is not None else None
        clean = {"in_at": in_at.isoformat(), "out_at": out_at.isoformat(),
                 "breaks": None if breaks is None else [_break_json(b) for b in breaks]}
        if payload.get("worked_through_breaks"):
            clean["worked_through_breaks"] = [str(k) for k in payload["worked_through_breaks"]]
        moved = abs((in_at - session.check_in_time).total_seconds())
        if session.check_out_time is not None:
            moved += abs((out_at - session.check_out_time).total_seconds())
        return Prepared(clean, session, int(moved // 60), session.row_version, base_day_version)

    if kind == "VOID_SESSION":
        session = _session(db, company_id, employee.id, session_id, d)
        end = session.check_out_time or session.closed_at or session.check_in_time
        return Prepared({}, session, int(max(0, (end - session.check_in_time).total_seconds()) // 60),
                        session.row_version, base_day_version)

    if kind == "MARK_EXCUSE":
        mark = payload.get("mark")
        if mark not in EXCUSE_MARKS:
            raise bad_request("Choose LATE or EARLY.", "INVALID_FIELDS")
        if d > today:  # planned permission: a standing input of the date (SP-AS-067)
            minutes = payload.get("minutes")
            if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
                raise bad_request("Enter the minutes to excuse (at least 1).", "INVALID_FIELDS")
            return Prepared({"mark": mark, "minutes": minutes, "future_days": (d - today).days}, None,
                            minutes, None, base_day_version)
        if day is None:
            raise unprocessable("This day has not been evaluated yet.", "NOT_ELIGIBLE", {"reason": "NOT_EVALUATED"})
        has_mark = (day.late_mark or day.late_converted) if mark == "LATE" else (day.early_mark or day.early_converted)
        if not has_mark:
            raise unprocessable(f"There is no {mark.lower()} mark on this day.", "NOT_ELIGIBLE", {"mark": mark})
        # P-7: the minutes of the evaluation, rounded so that every mark can be excused (the evaluator compares
        # whole minutes); never typed by the requester on a past date.
        recorded = day.late_minutes if mark == "LATE" else day.early_minutes
        minutes = max(1, int(recorded or 0))
        return Prepared({"mark": mark, "minutes": minutes}, None, minutes, None, base_day_version)

    # STATUS_OVERRIDE
    halves = {h: payload.get(h) for h in ("first_half", "second_half") if payload.get(h) is not None}
    if not halves or any(v not in HALF_VALUES for v in halves.values()):
        raise bad_request("Mark at least one half PRESENT or ABSENT.", "INVALID_FIELDS")
    expected = day.expected_portion if day is not None else (facts.working_portion or "NONE")
    allowed = {"FULL": {"first_half", "second_half"}, "FIRST_HALF": {"first_half"},
               "SECOND_HALF": {"second_half"}}.get(expected, set())
    if not set(halves) <= allowed:
        raise unprocessable("Only expected halves can be marked.", "NOT_ELIGIBLE", {"expected_portion": expected})
    return Prepared(halves, None, 0, None, base_day_version)


def _session_for_missing(db: Session, company_id: int, employee_id: int, d: date,
                         session_id: Optional[int]) -> Attendance:
    if session_id:
        return _session(db, company_id, employee_id, session_id, d)
    rows = (db.query(Attendance)
            .filter(Attendance.company_id == company_id, Attendance.employee_id == employee_id, Attendance.date == d,
                    Attendance.voided_at.is_(None), Attendance.check_out_time.is_(None))
            .order_by(Attendance.check_in_time.desc()).all())
    if not rows:
        raise unprocessable("There is no session without a punch-out on this date.", "NOT_ELIGIBLE")
    return rows[0]


def _break_json(b: Mapping[str, Any]) -> dict:
    return {"kind": b["kind"], "start_at": b["start_at"].isoformat(), "end_at": b["end_at"].isoformat()}


# --------------------------------------------------------------------------- windows and limits (§6)


def period_close_for(db: Session, company_id: int, employee_id: int, d: date) -> Optional[AttendancePeriodClose]:
    where = placement_on(db, company_id, employee_id, d)
    if where.legal_entity_id is None:
        return None
    rows = (db.query(AttendancePeriodClose)
            .filter(AttendancePeriodClose.company_id == company_id,
                    AttendancePeriodClose.legal_entity_id == where.legal_entity_id,
                    AttendancePeriodClose.cycle_start <= d, AttendancePeriodClose.cycle_end >= d).all())
    matching = [r for r in rows if (r.branch_id is None or r.branch_id == where.branch_id)
                and (r.pay_group_id is None or r.pay_group_id == where.pay_group_id)]
    if not matching:
        return None
    return max(matching, key=lambda r: ((r.branch_id is not None) + (r.pay_group_id is not None) * 2, r.id))


def self_window(db: Session, company_id: int, employee: Employee, d: date, kind: str, fields: Mapping[str, Any], *,
                rolling_from: Optional[date] = None, backdate_key: str = "backdate_days",
                future_key: str = "future_days") -> dict:
    """§6.3: whether a SELF request for D may be filed now; raises 422 with the reason, else returns the window."""
    today = employee_today(db, employee)
    mode = fields.get("window_mode") or "ROLLING_AND_CUTOFF"
    backdate = int(fields.get(backdate_key) if fields.get(backdate_key) is not None else 7)
    future = int(fields.get(future_key) or 0)
    last_day = d + timedelta(days=backdate)
    if d > today:
        if kind not in ("MARK_EXCUSE", "ON_DUTY") or (d - today).days > future:
            raise unprocessable("Requests this far ahead are not possible.", "REQUEST_WINDOW_CLOSED",
                                {"reason": "FUTURE", "window_mode": mode})
    reference = rolling_from or today
    rolling_ok = (reference - d).days <= backdate
    state = payroll_state(db, company_id, employee.id, d)
    cutoff_ok = state == "OPEN"
    if mode in ("ROLLING", "ROLLING_AND_CUTOFF") and not rolling_ok:
        raise unprocessable(f"Requests for {d.isoformat()} closed on {last_day.isoformat()}. Ask HR to correct it.",
                            "REQUEST_WINDOW_CLOSED",
                            {"reason": "ROLLING", "last_day": last_day.isoformat(), "window_mode": mode})
    if mode in ("UNTIL_CUTOFF", "ROLLING_AND_CUTOFF") and not cutoff_ok:
        from app.payroll.services import calendars

        where = placement_on(db, company_id, employee.id, d)
        period = calendars.period_containing(db, company_id, where.pay_group_id, d) if where.pay_group_id else None
        cutoff = period.input_cutoff_on.isoformat() if period is not None else None
        raise unprocessable(f"Requests for {d.isoformat()} closed when payroll inputs locked. Ask HR to correct it.",
                            "REQUEST_WINDOW_CLOSED", {"reason": "CUTOFF", "last_day": cutoff, "window_mode": mode})
    closed = period_close_for(db, company_id, employee.id, d)
    if closed is not None and closed.status == "CLOSED":
        raise unprocessable("Attendance for this cycle was closed. Ask HR to correct it.", "ATTENDANCE_PERIOD_CLOSED",
                            {"close_id": closed.id, "cycle_start": closed.cycle_start.isoformat(),
                             "cycle_end": closed.cycle_end.isoformat()})
    return {"mode": mode, "last_day": last_day.isoformat(), "open": True, "payroll_state": state}


def limit_usage(db: Session, company_id: int, employee_id: int, d: date, kind: str, minutes: int,
                fields: Mapping[str, Any], *, counts: bool, exclude_id: Optional[int] = None) -> List[dict]:
    """§6.2: usage of every limit item naming ``kind`` (SELF and ON_BEHALF requests with counting reasons)."""
    usage = []
    for index, item in enumerate(fields.get("limits") or []):
        if kind not in (item.get("kinds") or ()):
            continue
        start, end = _limit_window(db, company_id, employee_id, d, item.get("period") or "CYCLE")
        query = db.query(AttendanceRequest).filter(
            AttendanceRequest.company_id == company_id, AttendanceRequest.employee_id == employee_id,
            AttendanceRequest.kind.in_(list(item.get("kinds") or ())),
            AttendanceRequest.status.in_(("PENDING", "RETURNED", "APPROVED")),
            AttendanceRequest.mode.in_(("SELF", "ON_BEHALF")), AttendanceRequest.counts_toward_limit.is_(True),
            AttendanceRequest.shift_date >= start, AttendanceRequest.shift_date <= end)
        if exclude_id is not None:
            query = query.filter(AttendanceRequest.id != exclude_id)
        rows = query.all()
        used, used_minutes = len(rows), sum(r.minutes or 0 for r in rows)
        max_count, max_minutes = item.get("max_count"), item.get("max_minutes")
        reached = counts and ((max_count is not None and used + 1 > max_count)
                              or (max_minutes is not None and used_minutes + minutes > max_minutes))
        usage.append({"limit_index": index, "kinds": list(item.get("kinds") or ()), "period": item.get("period"),
                      "used": used, "limit": max_count, "used_minutes": used_minutes, "limit_minutes": max_minutes,
                      "window_from": start.isoformat(), "window_to": end.isoformat(), "reached": bool(reached)})
    return usage


# --------------------------------------------------------------------------- access


@dataclass
class Target:
    employee: Employee
    mode: str


def _target(db: Session, principal: Principal, employee_id: Optional[int], mode: str, kind: str, *,
            via_muster: bool = False) -> Target:
    if mode not in REQUEST_MODES:
        raise bad_request("Unknown request mode.", "INVALID_MODE")
    own = employee_id is None or authz.is_self(principal, employee_id)
    if mode == "SELF":
        if not own:
            raise bad_request("A request for someone else is filed on their behalf.", "INVALID_MODE")
        if not principal.has("attendance:request") or principal.employee_id is None:
            raise forbidden()
        return Target(authz.own_employee(db, principal), mode)
    if own:  # never on oneself, checked before any scope lookup (§13)
        raise self_edit_forbidden()
    if mode == "ON_BEHALF":
        if kind == "STATUS_OVERRIDE":
            raise unprocessable("A day mark is recorded directly by HR.", "REQUEST_KIND_DISABLED")
        return Target(authz.ensure_employee_access(db, principal, "attendance:request_on_behalf", employee_id), mode)
    code = "attendance:muster" if via_muster else (
        "attendance:override" if kind == "STATUS_OVERRIDE" else "attendance:regularize")
    return Target(authz.ensure_employee_access(db, principal, code, employee_id), mode)


def _visible(db: Session, principal: Principal, row: AttendanceRequest) -> bool:
    if row.requested_by_user_id == principal.user_id or authz.is_self(principal, row.employee_id):
        return True
    for code in ("attendance:read", "attendance:approve", "attendance:regularize"):
        if principal.has(code) and authz.can_access_employee(db, principal, code, row.employee_id):
            return True
    if row.approval_request_id:
        request = db.get(approvals.ApprovalRequest, row.approval_request_id)
        if request is not None:
            ctx = approvals._Context(db, principal.company_id)
            ctx.seed(principal)
            return approvals._access(ctx, request, principal)[0]
    return False


def load(db: Session, principal: Principal, request_id: int, *, lock: bool = False) -> AttendanceRequest:
    from app.services import attendance_access

    attendance_access.require_v2(db, principal.company_id)
    query = db.query(AttendanceRequest).filter(AttendanceRequest.company_id == principal.company_id,
                                               AttendanceRequest.id == request_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None or not _visible(db, principal, row):
        raise not_found("Attendance request")
    return row


# --------------------------------------------------------------------------- preview / create / submit


def _route_attributes(db: Session, company_id: int, employee: Employee, d: date, kind: str, reason_code: str,
                      minutes: int, state: str) -> dict:
    where = placement_on(db, company_id, employee.id, d)
    today = employee_today(db, employee)
    return {"team_ids": team_ids_on(db, company_id, employee.id, d), "legal_entity_id": where.legal_entity_id,
            "branch_id": where.branch_id, "request_kind": kind, "reason_code": reason_code,
            "days_back": max(0, (today - d).days), "minutes": minutes, "payroll_state": state,
            "subject_kind": SUBJECT, "employee_id": employee.id, "work_date": d.isoformat()}


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise bad_request("Give the date as YYYY-MM-DD.", "INVALID_FIELDS")


def preview(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    """Issues, limit usage, window, payroll state, route and the current evaluation, without writing (§16)."""
    from app.services import attendance_access

    attendance_access.require_v2(db, principal.company_id)
    kind, mode = data.get("kind"), data.get("mode") or "SELF"
    d = _as_date(data.get("shift_date"))
    target = _target(db, principal, data.get("employee_id"), mode, kind)
    employee, cid = target.employee, principal.company_id
    issues: List[dict] = []
    fields = policy_fields(db, cid, employee.id, d)
    try:
        prepared = prepare(db, cid, employee, d, kind, data.get("payload") or {}, data.get("session_id"), mode=mode)
    except AppError as exc:
        issues.append({"code": exc.code, "message": exc.detail, "severity": "BLOCKING", **exc.extra})
        prepared = None
    reason_row = None
    if data.get("reason_code"):
        try:
            reason_row = _reason(db, cid, data.get("reason_code"), kind)
        except AppError as exc:
            issues.append({"code": exc.code, "message": exc.detail, "severity": "BLOCKING"})
    window = None
    if mode == "SELF":
        if kind not in (fields.get("self_kinds") or ()):
            issues.append({"code": "REQUEST_KIND_DISABLED", "severity": "BLOCKING",
                           "message": "This kind of request is not available to you."})
        try:
            window = self_window(db, cid, employee, d, kind, fields)
        except AppError as exc:
            issues.append({"code": exc.code, "message": exc.detail, "severity": "BLOCKING", **exc.extra})
    minutes = prepared.minutes if prepared else 0
    counts = reason_row.counts_toward_limit if reason_row is not None else True
    usage = limit_usage(db, cid, employee.id, d, kind, minutes, fields, counts=counts and mode != "DIRECT")
    for item in usage:
        if item["reached"]:
            issues.append({"code": "REQUEST_LIMIT_REACHED", "severity": "BLOCKING" if mode == "SELF" else "WARNING",
                           "message": f"Over the limit ({item['used'] + 1} of {item['limit']} this period).",
                           "details": item})
            break
    state = payroll_state(db, cid, employee.id, d)
    if state != "OPEN" and mode != "SELF":
        issues.append({"code": "PERIOD_LOCKED", "severity": "WARNING", "message": STATE_TEXT[state]})
    route = None
    if mode != "DIRECT":
        attributes = _route_attributes(db, cid, employee, d, kind, data.get("reason_code") or "", minutes, state)
        route = approvals.preview_route(db, cid, MODULE, employee.id, attributes, requester_user_id=principal.user_id)
    day = _day_row(db, cid, employee.id, d)
    return {"issues": issues, "limit_usage": usage, "window": window,
            "payroll_state": {"state": state, "message": STATE_TEXT.get(state)}, "route_preview": route,
            "evaluation_before": _day_summary(day), "minutes": minutes,
            "payload": prepared.payload if prepared else None}


def form_context(db: Session, principal: Principal, *, employee_id: Optional[int], d: date, mode: str) -> dict:
    """What the request form needs before the person types times: the work zone, the day's sessions (with local
    times), the evaluation, the kinds the mode allows and the payroll state."""
    from app.services import attendance_access, attendance_service

    attendance_access.require_v2(db, principal.company_id)
    target = _target(db, principal, employee_id, mode, "MISSED_PUNCH")
    employee, cid = target.employee, principal.company_id
    zone = attendance_service.work_zone(db, employee, d)
    fields = policy_fields(db, cid, employee.id, d)
    sessions = (db.query(Attendance)
                .filter(Attendance.company_id == cid, Attendance.employee_id == employee.id, Attendance.date == d,
                        Attendance.voided_at.is_(None)).order_by(Attendance.check_in_time).all())

    def hm(moment: Optional[datetime]) -> Optional[str]:
        return moment.astimezone(zone).strftime("%H:%M") if moment else None

    if mode == "SELF":
        kinds = [k for k in ATTENDANCE_REQUEST_KINDS if k in (fields.get("self_kinds") or ())]
    elif mode == "ON_BEHALF":
        kinds = [k for k in ATTENDANCE_REQUEST_KINDS if k != "STATUS_OVERRIDE"]
    else:
        kinds = list(ATTENDANCE_REQUEST_KINDS)
    return {"employee_id": employee.id, "employee_name": employee.full_name, "work_timezone": zone.zone,
            "date": d.isoformat(), "kinds": kinds,
            "sessions": [{"id": s.id, "in": hm(s.check_in_time), "out": hm(s.check_out_time),
                          "source": s.capture_source, "row_version": s.row_version} for s in sessions],
            "day": _day_summary(_day_row(db, cid, employee.id, d)),
            "payroll_state": payroll_state(db, cid, employee.id, d), "limits": fields.get("limits") or [],
            "window_mode": fields.get("window_mode"), "backdate_days": fields.get("backdate_days")}


STATE_TEXT = {"OPEN": None,
              "INPUT_LOCKED": "Payroll is locked for this date: this will be handled as a change before payment.",
              "FINALIZED": "Payroll for this date is paid: a correction will be made in the next payroll."}


def _day_summary(day) -> Optional[dict]:
    if day is None:
        return None
    return {"day_result": day.day_result, "evaluation_state": day.evaluation_state, "version": day.version,
            "net_minutes": day.net_minutes, "late_mark": day.late_mark, "late_excused": day.late_excused,
            "early_mark": day.early_mark, "early_excused": day.early_excused,
            "exception_codes": list(day.exception_codes or ())}


def create(db: Session, principal: Principal, data: Mapping[str, Any], *, via_muster: bool = False,
           maker_user_ids: Sequence[int] = ()) -> AttendanceRequest:
    """POST /attendance/requests: SELF / ON_BEHALF → DRAFT (``submit = false``, SELF only) or PENDING on the engine;
    DIRECT → applied at once (APPROVED)."""
    from app.services import attendance_access, attendance_service

    cid = principal.company_id
    attendance_access.require_v2(db, cid)
    kind, mode = data.get("kind"), data.get("mode") or "SELF"
    if kind not in ATTENDANCE_REQUEST_KINDS:
        raise bad_request("Unknown request kind.", "INVALID_KIND")
    if kind == "STATUS_OVERRIDE" and mode == "SELF":
        raise unprocessable("A day mark is recorded directly by HR.", "REQUEST_KIND_DISABLED")
    d = _as_date(data.get("shift_date"))
    target = _target(db, principal, data.get("employee_id"), mode, kind, via_muster=via_muster)
    employee = target.employee
    attendance_service.lock_employee(db, employee.id)
    reason = _reason(db, cid, data.get("reason_code"), kind)
    comment = (data.get("comment") or "").strip() or None
    if comment is not None and len(comment) > 1000:
        raise bad_request("The comment is at most 1000 characters.", "INVALID_FIELDS")
    fields = policy_fields(db, cid, employee.id, d)
    if mode != "DIRECT" and (reason.code == "OTHER" or fields.get("comment_required")) and not comment:
        raise bad_request("Add a comment.", "COMMENT_REQUIRED")
    if mode == "SELF" and kind not in (fields.get("self_kinds") or ()):
        raise unprocessable("This kind of request is not available to you.", "REQUEST_KIND_DISABLED",
                            {"kind": kind})
    prepared = prepare(db, cid, employee, d, kind, data.get("payload") or {}, data.get("session_id"), mode=mode)
    state = payroll_state(db, cid, employee.id, d)
    if mode == "DIRECT" and state == "FINALIZED" and (not comment or len(comment) < 10):
        raise unprocessable("Explain the correction (at least 10 characters): payroll for this date is paid.",
                            "NOT_ELIGIBLE", {"missing": "COMMENT"})
    _check_open_duplicate(db, cid, employee.id, d, kind, prepared.session)
    row = AttendanceRequest(
        company_id=cid, employee_id=employee.id, shift_date=d, kind=kind, mode=mode, status="PENDING",
        session_id=prepared.session.id if prepared.session is not None else None, payload=prepared.payload,
        base_day_version=prepared.base_day_version, base_session_row_version=prepared.base_session_row_version,
        reason_code=reason.code, comment=comment, minutes=prepared.minutes,
        counts_toward_limit=reason.counts_toward_limit, requested_by_user_id=principal.user_id,
    )
    if mode == "DIRECT":
        row.status, row.decided_by_user_id, row.decided_at = "APPROVED", principal.user_id, _now()
        db.add(row)
        db.flush()
        _link_attachments(db, principal, row, data.get("attachment_ids") or ())
        apply(db, row, principal, direct=True)
        _audit(db, principal, row, "DIRECT_EDIT", f"Direct {KIND_LABELS[kind].lower()} for employee "
               f"#{employee.id} on {d.isoformat()}", payroll_state=state)
        return row
    submit_now = data.get("submit", True)
    if not submit_now:
        if mode != "SELF":
            raise bad_request("Only your own requests can be saved as drafts.", "INVALID_MODE")
        row.status = "DRAFT"
        db.add(row)
        db.flush()
        _link_attachments(db, principal, row, data.get("attachment_ids") or ())
        return row
    db.add(row)
    db.flush()
    _link_attachments(db, principal, row, data.get("attachment_ids") or ())
    _submit(db, principal, row, employee, fields, acknowledged=data.get("acknowledged_warnings") or ())
    return row


def _check_open_duplicate(db: Session, company_id: int, employee_id: int, d: date, kind: str,
                          session: Optional[Attendance], exclude_id: Optional[int] = None) -> None:
    query = db.query(AttendanceRequest.id).filter(
        AttendanceRequest.company_id == company_id, AttendanceRequest.employee_id == employee_id,
        AttendanceRequest.shift_date == d, AttendanceRequest.kind == kind,
        AttendanceRequest.status.in_(OPEN_REQUEST_STATUSES),
        AttendanceRequest.session_id == session.id if session is not None else AttendanceRequest.session_id.is_(None))
    if exclude_id is not None:
        query = query.filter(AttendanceRequest.id != exclude_id)
    found = query.first()
    if found is not None:
        raise AppError(http_status.HTTP_409_CONFLICT, "A request for this is already open.", "ATTENDANCE_REQUEST_OPEN",
                       extra={"details": {"request_id": found[0]}})


def _submit(db: Session, principal: Principal, row: AttendanceRequest, employee: Employee, fields: Mapping[str, Any],
            *, acknowledged: Sequence[str] = ()) -> None:
    """Windows (SELF), limits, required attachments, then the engine request with the route of the shift date."""
    cid = principal.company_id
    if row.mode == "SELF":
        self_window(db, cid, employee, row.shift_date, row.kind, fields, rolling_from=row.first_submitted_on)
    usage = limit_usage(db, cid, employee.id, row.shift_date, row.kind, row.minutes, fields,
                        counts=row.counts_toward_limit, exclude_id=row.id)
    reached = next((u for u in usage if u["reached"]), None)
    if reached is not None:
        details = {k: reached[k] for k in ("limit_index", "used", "limit", "period", "window_from", "window_to")}
        if row.mode == "SELF":
            raise unprocessable(f"You have used {reached['used']} of {reached['limit']} requests this period.",
                                "REQUEST_LIMIT_REACHED", details)
        if "REQUEST_LIMIT_REACHED" not in acknowledged:
            raise unprocessable("The employee is over the limit; acknowledge the warning to file anyway.",
                                "REQUEST_LIMIT_REACHED", {**details, "acknowledgeable": True})
        row.issues = [{"code": "REQUEST_LIMIT_REACHED", **details}]
    reason = _reason(db, cid, row.reason_code, row.kind)
    needs_file = reason.requires_attachment or row.kind in (fields.get("attachment_required_kinds") or ())
    if needs_file and not db.query(AttendanceAttachment.id).filter(
            AttendanceAttachment.company_id == cid, AttendanceAttachment.request_id == row.id).first():
        raise unprocessable("Attach a supporting document.", "NOT_ELIGIBLE", {"missing": "ATTACHMENT"})
    state = payroll_state(db, cid, employee.id, row.shift_date)
    attributes = _route_attributes(db, cid, employee, row.shift_date, row.kind, row.reason_code, row.minutes, state)
    request = approvals.submit(
        db, principal, module=MODULE, subject_type=SUBJECT, subject_id=row.id, subject_employee_id=employee.id,
        context={"work_date": row.shift_date.isoformat(), "kind": row.kind, "mode": row.mode,
                 "payroll_state_at_submit": state},
        attributes=attributes)
    row.approval_request_id = request.id
    row.status = "PENDING"
    row.submitted_at = _now()
    row.first_submitted_on = row.first_submitted_on or employee_today(db, employee)
    db.flush()
    _audit(db, principal, row, "REQUEST_SUBMITTED",
           f"{KIND_LABELS[row.kind]} request #{row.id} for {row.shift_date.isoformat()} submitted",
           route_key=(request.context or {}).get("route_key"))
    reevaluate(db, row, principal.user_id)
    from app.services import integrations

    integrations.emit(db, cid, "attendance_request.submitted", f"{row.id}:{request.id}",
                      f"{KIND_LABELS[row.kind]} request waiting for approval",
                      f"A {KIND_LABELS[row.kind].lower()} request for {row.shift_date.strftime('%a %d %b')} needs a "
                      "decision.", "/approvals")


def submit(db: Session, principal: Principal, request_id: int, expected_version: Optional[int] = None,
           acknowledged: Sequence[str] = ()) -> AttendanceRequest:
    """DRAFT or RETURNED → PENDING (resubmit: version + 1, a new engine request; §4.1, §6.4)."""
    from app.services import attendance_service

    row = load(db, principal, request_id, lock=True)
    if row.requested_by_user_id != principal.user_id:
        raise forbidden("Only the requester can submit this request.")
    if row.status not in ("DRAFT", "RETURNED"):
        raise conflict(f"This request is already {row.status.lower()}.", "INVALID_TRANSITION")
    _check_version(row, expected_version)
    employee = db.get(Employee, row.employee_id)
    attendance_service.lock_employee(db, employee.id)
    fields = policy_fields(db, principal.company_id, employee.id, row.shift_date)
    if row.mode == "SELF" and row.kind not in (fields.get("self_kinds") or ()):
        raise unprocessable("This kind of request is not available to you.", "REQUEST_KIND_DISABLED")
    session = db.get(Attendance, row.session_id) if row.session_id else None
    _check_open_duplicate(db, principal.company_id, row.employee_id, row.shift_date, row.kind, session,
                          exclude_id=row.id)
    if row.status == "RETURNED":
        row.version += 1
    _submit(db, principal, row, employee, fields, acknowledged=acknowledged)
    row.row_version += 1
    return row


def update(db: Session, principal: Principal, request_id: int, data: Mapping[str, Any],
           expected_version: Optional[int] = None) -> AttendanceRequest:
    """PATCH while DRAFT or RETURNED by the requester: payload, session, reason, comment."""
    row = load(db, principal, request_id, lock=True)
    if row.requested_by_user_id != principal.user_id:
        raise forbidden("Only the requester can edit this request.")
    if row.status not in ("DRAFT", "RETURNED"):
        raise conflict(f"This request is already {row.status.lower()}.", "INVALID_TRANSITION")
    _check_version(row, expected_version)
    employee = db.get(Employee, row.employee_id)
    if "reason_code" in data:
        reason = _reason(db, principal.company_id, data["reason_code"], row.kind)
        row.reason_code, row.counts_toward_limit = reason.code, reason.counts_toward_limit
    if "comment" in data:
        comment = (data.get("comment") or "").strip() or None
        if comment is not None and len(comment) > 1000:
            raise bad_request("The comment is at most 1000 characters.", "INVALID_FIELDS")
        row.comment = comment
    if "payload" in data or "session_id" in data:
        prepared = prepare(db, principal.company_id, employee, row.shift_date, row.kind,
                           data.get("payload", row.payload), data.get("session_id", row.session_id), mode=row.mode)
        row.payload, row.minutes = prepared.payload, prepared.minutes
        row.session_id = prepared.session.id if prepared.session is not None else None
        row.base_session_row_version = prepared.base_session_row_version
        row.base_day_version = prepared.base_day_version
    _link_attachments(db, principal, row, data.get("attachment_ids") or ())
    row.row_version += 1
    db.flush()
    return row


def withdraw(db: Session, principal: Principal, request_id: int, expected_version: Optional[int] = None
             ) -> AttendanceRequest:
    """DRAFT / PENDING / RETURNED → CANCELLED by the requester (ON_BEHALF: the filer or the employee)."""
    row = load(db, principal, request_id, lock=True)
    if row.requested_by_user_id != principal.user_id and not authz.is_self(principal, row.employee_id):
        raise forbidden("Only the requester can withdraw this request.")
    if row.status not in ("DRAFT", "PENDING", "RETURNED"):
        raise conflict(f"This request is already {row.status.lower()}.", "INVALID_TRANSITION")
    _check_version(row, expected_version)
    if row.status == "PENDING":
        approvals.withdraw(db, principal, module=MODULE, subject_type=SUBJECT, subject_id=row.id,
                           reason="Withdrawn by the requester")
    row.status, row.cancelled_at = "CANCELLED", _now()
    row.row_version += 1
    db.flush()
    _audit(db, principal, row, "REQUEST_WITHDRAWN", f"Attendance request #{row.id} withdrawn")
    reevaluate(db, row, principal.user_id)
    return row


def _check_version(row: AttendanceRequest, expected: Optional[int]) -> None:
    if expected is not None and expected != row.row_version:
        raise conflict("This request changed since you loaded it.", "ROW_VERSION_CONFLICT")


# --------------------------------------------------------------------------- decision callback and apply (§3.5)


@approvals.on_decided(MODULE, SUBJECT)
def _decided(db: Session, principal: Optional[Principal], request) -> None:
    row = (db.query(AttendanceRequest)
           .filter(AttendanceRequest.company_id == request.company_id, AttendanceRequest.id == request.subject_id)
           .with_for_update().first())
    if row is None or row.status != "PENDING" or row.approval_request_id != request.id:
        return
    step = next((s for s in reversed(request.steps) if s.acted_by_user_id is not None), None)
    row.decided_by_user_id = principal.user_id if principal else None
    row.decided_at = _now()
    row.decision_remarks = step.remarks if step is not None else None
    if request.status == "APPROVED":
        row.status = "APPROVED"
        apply(db, row, principal, direct=False)
    elif request.status == "RETURNED":
        row.status = "RETURNED"
    elif request.status == "REJECTED":
        row.status = "REJECTED"
    else:
        row.status, row.cancelled_at = "CANCELLED", _now()
    row.row_version += 1
    db.flush()
    if row.status in ("APPROVED", "REJECTED", "RETURNED"):
        from app.services import integrations

        integrations.emit(db, row.company_id, "attendance_request.decided", f"{row.id}:{request.id}",
                          f"Attendance request {row.status.lower()}",
                          f"The {KIND_LABELS[row.kind].lower()} request for {row.shift_date.strftime('%a %d %b')} was "
                          f"{row.status.lower()}.", "/attendance")
    _audit(db, principal, row, "REQUEST_DECIDED", f"Attendance request #{row.id} {row.status.lower()}",
           decision=row.status)
    if row.status != "APPROVED":
        reevaluate(db, row, principal.user_id if principal else None)


def apply(db: Session, row: AttendanceRequest, actor: Optional[Principal], *, direct: bool) -> None:
    """Atomic apply: staleness, punches / sessions / voids / breaks, re-evaluation, change log, supersede (§3.5)."""
    from app.payroll.services import time_inputs as ti
    from app.services import attendance_service

    attendance_service.lock_employee(db, row.employee_id)
    actor_id = actor.user_id if actor else None
    source = "MANUAL" if direct else "REQUEST"
    session = db.get(Attendance, row.session_id) if row.session_id else None
    if session is not None and (session.voided_at is not None
                                or (row.base_session_row_version is not None
                                    and session.row_version != row.base_session_row_version)):
        raise conflict("This day changed since the request was filed.", "REQUEST_STALE")
    payload = row.payload or {}
    if row.kind == "MARK_EXCUSE" and payload.get("future_days") is None:
        day = _day_row(db, row.company_id, row.employee_id, row.shift_date)
        mark = payload.get("mark")
        if day is not None and not ((day.late_mark or day.late_converted) if mark == "LATE"
                                    else (day.early_mark or day.early_converted)):
            raise conflict("This day changed since the request was filed.", "REQUEST_STALE")
    if row.kind == "MISSED_PUNCH":
        _apply_missed_punch(db, row, session, payload, source, actor_id)
    elif row.kind == "ADD_SESSION":
        _new_session(db, row, parse_instant(payload["in_at"], "in"), parse_instant(payload["out_at"], "out"),
                     payload.get("breaks") or [], source, actor_id, work_mode=payload.get("work_mode") or "ON_SITE")
    elif row.kind == "CORRECTION":
        breaks = payload.get("breaks")
        if breaks is None:
            breaks = [_break_json({"kind": b.kind, "start_at": b.started_at, "end_at": b.ended_at})
                      for b in attendance_service.breaks_by_session(db, [session.id])[session.id]
                      if b.ended_at is not None]
        in_at, out_at = parse_instant(payload["in_at"], "in"), parse_instant(payload["out_at"], "out")
        breaks = [b for b in breaks if in_at <= parse_instant(b["start_at"], "b") and
                  parse_instant(b["end_at"], "b") <= out_at]
        _void(db, session, actor_id)
        _new_session(db, row, in_at, out_at, breaks, source, actor_id, seq=session.session_seq,
                     work_mode=session.work_mode)
    elif row.kind == "VOID_SESSION":
        _void(db, session, actor_id)
    row.applied_at = _now()
    if row.status != "APPROVED":
        row.status = "APPROVED"
    db.flush()
    ti.log_change(db, row.company_id, record_type="attendance_request", record_id=row.id, change="CREATED",
                  content={"kind": row.kind, "mode": row.mode, "payload": payload, "status": row.status},
                  employee_id=row.employee_id, date_from=row.shift_date, date_to=row.shift_date,
                  actor_user_id=actor_id, details={"kind": row.kind, "mode": row.mode})
    reevaluate(db, row, actor_id)
    supersede_open(db, row.company_id, row.employee_id, row.shift_date, by_request_id=row.id, actor=actor)
    action = {"STATUS_OVERRIDE": "STATUS_OVERRIDDEN", "MARK_EXCUSE": "MARK_EXCUSED",
              "VOID_SESSION": "SESSION_VOIDED"}.get(row.kind, "REQUEST_APPLIED")
    _audit(db, actor, row, action, f"{KIND_LABELS[row.kind]} #{row.id} applied to {row.shift_date.isoformat()}")


def _apply_missed_punch(db: Session, row: AttendanceRequest, session: Optional[Attendance], payload: Mapping,
                        source: str, actor_id: Optional[int]) -> None:
    from app.services import attendance_capture_policy, attendance_service, attendance_sessions

    at = parse_instant(payload["at"], "at")
    direction = payload["direction"]
    if direction == "IN":
        _new_session(db, row, at, parse_instant(payload["pair_out_at"], "out"), [], source, actor_id)
        return
    if direction == "OUT":
        attendance_sessions.check_overlap(db, row.company_id, row.employee_id, session.check_in_time, at,
                                          exclude_id=session.id)
        policy = attendance_capture_policy.effective(db, row.company_id, row.employee_id, row.shift_date)
        session.check_out_time = at
        session.closed_at, session.closed_reason = None, None
        session.occupied_until = attendance_sessions.occupied_until(session, policy.max_session_hours)
        for b in attendance_service.breaks_by_session(db, [session.id])[session.id]:
            if b.ended_at is None:
                b.ended_at, b.end_reason = at, "CORRECTED"
        punch = attendance_sessions.record_punch(
            db, company_id=row.company_id, employee_id=row.employee_id, source=source, direction="OUT",
            occurred_at=at, session=session, shift_date=row.shift_date, actor_user_id=actor_id, reason=row.reason_code,
            request_id=row.id)
        session.last_punch_id = punch.id
        session.request_id = session.request_id or row.id
        attendance_service.record_version(db, session, "UPDATED", actor_id)
        return
    open_break = (db.query(AttendanceBreak)
                  .filter(AttendanceBreak.attendance_id == session.id, AttendanceBreak.ended_at.is_(None)).first())
    if open_break is not None:
        open_break.ended_at, open_break.end_reason = at, "CORRECTED"
        open_break.source = "CORRECTED" if source == "MANUAL" else "REQUEST"
    attendance_service.record_version(db, session, "UPDATED", actor_id)


def _new_session(db: Session, row: AttendanceRequest, in_at: datetime, out_at: datetime, breaks: Sequence[Mapping],
                 source: str, actor_id: Optional[int], *, seq: Optional[int] = None,
                 work_mode: str = "ON_SITE") -> Attendance:
    from app.services import attendance_capture_policy, attendance_service, attendance_sessions

    employee = db.get(Employee, row.employee_id)
    policy = attendance_capture_policy.effective(db, row.company_id, row.employee_id, row.shift_date)
    attendance_sessions.check_overlap(db, row.company_id, row.employee_id, in_at, out_at)
    if seq is None:
        seq = attendance_sessions.next_session_seq(db, row.company_id, row.employee_id, row.shift_date, True)
    zone = attendance_service.work_zone(db, employee, row.shift_date)
    where = placement_on(db, row.company_id, row.employee_id, row.shift_date)
    session = Attendance(
        company_id=row.company_id, employee_id=row.employee_id, date=row.shift_date, check_in_time=in_at,
        check_out_time=out_at, shift_id=employee.shift_id, is_manual=True,
        source="manual" if source == "MANUAL" else "request", capture_source=source, status="present",
        recorded_by_user_id=actor_id, session_seq=seq, work_mode=work_mode, request_id=row.id,
        legal_entity_id=where.legal_entity_id, branch_id=where.branch_id, work_timezone=zone.zone,
    )
    session.occupied_until = attendance_sessions.occupied_until(session, policy.max_session_hours)
    attendance_service._insert_day(db, session)
    for direction, at in (("IN", in_at), ("OUT", out_at)):
        punch = attendance_sessions.record_punch(
            db, company_id=row.company_id, employee_id=row.employee_id, source=source, direction=direction,
            occurred_at=at, session=session, shift_date=row.shift_date, actor_user_id=actor_id,
            reason=row.reason_code, idempotency_key=f"req{row.id}v{row.version}{direction}", request_id=row.id)
        if direction == "IN":
            session.first_punch_id = punch.id
        else:
            session.last_punch_id = punch.id
    for b in breaks:
        db.add(AttendanceBreak(
            company_id=row.company_id, attendance_id=session.id, employee_id=row.employee_id,
            kind=b.get("kind") or "LUNCH", started_at=parse_instant(b["start_at"], "b"),
            ended_at=parse_instant(b["end_at"], "b"), is_paid=bool(policy_lunch_paid(db, row.company_id)),
            end_reason="CORRECTED", created_by_user_id=actor_id,
            source="CORRECTED" if source == "MANUAL" else "REQUEST"))
    db.flush()
    attendance_service.record_version(db, session, "CREATED", actor_id, bump=False)
    return session


def policy_lunch_paid(db: Session, company_id: int) -> bool:
    from app.services import attendance_policy

    return attendance_policy.load(db, company_id).lunch_is_paid


def _void(db: Session, session: Attendance, actor_id: Optional[int]) -> None:
    from app.services import attendance_service

    session.voided_at, session.voided_by_user_id, session.void_reason = _now(), actor_id, "REQUEST"
    (db.query(AttendancePunch)
     .filter(AttendancePunch.company_id == session.company_id, AttendancePunch.session_id == session.id)
     .update({AttendancePunch.disposition: "IGNORED_BY_ADMIN"}, synchronize_session=False))
    attendance_service.record_version(db, session, "VOIDED", actor_id)


def supersede_open(db: Session, company_id: int, employee_id: int, shift_date: date, *, by_request_id: int,
                   actor: Optional[Principal] = None) -> List[int]:
    """Open session-bound requests whose session was voided or changed since filing become SUPERSEDED (§3.5 step 6)."""
    rows = (db.query(AttendanceRequest)
            .filter(AttendanceRequest.company_id == company_id, AttendanceRequest.employee_id == employee_id,
                    AttendanceRequest.shift_date == shift_date, AttendanceRequest.id != by_request_id,
                    AttendanceRequest.status.in_(OPEN_REQUEST_STATUSES), AttendanceRequest.session_id.isnot(None))
            .all())
    superseded = []
    for row in rows:
        session = db.get(Attendance, row.session_id)
        if session is not None and session.voided_at is None and session.row_version == row.base_session_row_version:
            continue
        if row.status == "PENDING":
            approvals.withdraw(db, actor, module=MODULE, subject_type=SUBJECT, subject_id=row.id,
                               reason=f"Superseded by request #{by_request_id}")
        row.status, row.cancelled_at = "SUPERSEDED", _now()
        row.row_version += 1
        superseded.append(row.id)
    db.flush()
    return superseded


def reevaluate(db: Session, row: AttendanceRequest, actor_user_id: Optional[int]) -> None:
    """Re-evaluate (E, shift date) now (trigger REQUEST_DECIDED); future dates are evaluated by their day close."""
    from app.services import attendance_days

    employee = db.get(Employee, row.employee_id)
    if row.shift_date > employee_today(db, employee):
        return
    attendance_days.evaluate_range(db, row.company_id, [row.employee_id], row.shift_date, row.shift_date,
                                   trigger="REQUEST_DECIDED", actor_user_id=actor_user_id)


# --------------------------------------------------------------------------- attachments (§8.4, SP-AS-076)

ATTACHMENT_TYPES = ((b"%PDF-", "pdf", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
                    (b"\xff\xd8\xff", "jpg", "image/jpeg"))
ATTACHMENT_CATEGORY = "attendance-documents"
MAX_ATTACHMENTS_PER_SUBJECT = 20


def upload_attachment(db: Session, principal: Principal, *, employee_id: Optional[int], file_name: str, data: bytes,
                      category: str = "SUPPORTING_DOCUMENT") -> AttendanceAttachment:
    """Upload first, link at submit; requester side or HR in scope of the subject."""
    import hashlib
    from pathlib import Path

    from app.config import settings as app_settings
    from app.core import storage

    subject_id = employee_id or principal.employee_id
    if subject_id is None:
        raise forbidden()
    if not authz.is_self(principal, subject_id):
        allowed = any(principal.has(c) and authz.can_access_employee(db, principal, c, subject_id)
                      for c in ("attendance:request_on_behalf", "attendance:regularize", "attendance:approve"))
        if not allowed:
            raise not_found("Employee")
    elif not principal.has("attendance:request"):
        raise forbidden()
    if not data:
        raise bad_request("No file provided.", "ATTACHMENT_EMPTY")
    limit = min(5 * 1024 * 1024, app_settings.max_upload_size)
    if len(data) > limit:
        raise bad_request(f"The file is too large (max {limit // (1024 * 1024)} MB).", "ATTACHMENT_TOO_LARGE")
    kind = next(((ext, mime) for magic, ext, mime in ATTACHMENT_TYPES if data.startswith(magic)), None)
    if kind is None:
        raise bad_request("Attach a PDF, PNG or JPEG file.", "ATTACHMENT_TYPE")
    if category not in ("SUPPORTING_DOCUMENT", "TRAVEL_DOCUMENT"):
        raise bad_request("Unknown document category.", "INVALID_FIELDS")
    ext, mime = kind
    path = storage.save_bytes(principal.company_id, ATTACHMENT_CATEGORY, data, ext)
    row = AttendanceAttachment(company_id=principal.company_id, employee_id=subject_id,
                               uploaded_by_user_id=principal.user_id,
                               file_name=Path(file_name or f"document.{ext}").name[:200], mime=mime, bytes=len(data),
                               sha256=hashlib.sha256(data).hexdigest(), storage_path=path, category=category)
    db.add(row)
    db.flush()
    return row


def _link_attachments(db: Session, principal: Principal, row: AttendanceRequest, ids: Sequence[int]) -> None:
    if not ids:
        return
    reason = db.query(AttendanceReasonCode).filter(AttendanceReasonCode.company_id == row.company_id,
                                                   AttendanceReasonCode.code == row.reason_code).first()
    files = (db.query(AttendanceAttachment)
             .filter(AttendanceAttachment.company_id == row.company_id, AttendanceAttachment.id.in_(list(ids))).all())
    if len(files) != len(set(ids)):
        raise not_found("Attachment")
    count = db.query(AttendanceAttachment.id).filter(AttendanceAttachment.company_id == row.company_id,
                                                     AttendanceAttachment.request_id == row.id).count()
    if count + len(files) > MAX_ATTACHMENTS_PER_SUBJECT:
        raise conflict(f"A request has at most {MAX_ATTACHMENTS_PER_SUBJECT} files.", "TOO_MANY_ATTACHMENTS")
    for f in files:
        if f.uploaded_by_user_id != principal.user_id or f.employee_id != row.employee_id:
            raise not_found("Attachment")
        if f.request_id is not None and f.request_id != row.id:
            raise conflict("This file is already attached to another request.", "ATTACHMENT_IN_USE")
        f.request_id = row.id
        # AS-REG-13: a file the requester side attaches to a sensitive reason is MEDICAL (HR and requester only).
        requester_side = f.uploaded_by_user_id == row.requested_by_user_id
        if reason is not None and reason.sensitive and requester_side:
            f.sensitivity = "MEDICAL"
    db.flush()


def attachment_for_download(db: Session, principal: Principal, attachment_id: int):
    """The file and its metadata; MEDICAL files only for the requester side and ``attendance:regularize`` holders
    covering E (404 for everyone else, §8.2 matrix)."""
    from app.core import storage

    row = db.query(AttendanceAttachment).filter(AttendanceAttachment.company_id == principal.company_id,
                                                AttendanceAttachment.id == attachment_id).first()
    if row is None:
        raise not_found("Attachment")
    request = db.get(AttendanceRequest, row.request_id) if row.request_id else None
    requester_side = (row.uploaded_by_user_id == principal.user_id or authz.is_self(principal, row.employee_id)
                      or (request is not None and request.requested_by_user_id == principal.user_id))
    hr = principal.has("attendance:regularize") and authz.can_access_employee(
        db, principal, "attendance:regularize", row.employee_id)
    if row.sensitivity == "MEDICAL":
        allowed = requester_side or hr
    else:
        allowed = requester_side or hr or (request is not None and _visible(db, principal, request))
    if not allowed:
        raise not_found("Attachment")
    AuditLogService.log(db, principal, AUDIT_MODULE, "ATTACHMENT_DOWNLOADED",
                        f"Downloaded attendance attachment #{row.id}", "attendance_attachment", row.id,
                        details={"request_id": row.request_id, "sensitivity": row.sensitivity})
    return storage.resolve(principal.company_id, row.storage_path), row


def attachment_json(row: AttendanceAttachment, *, visible: bool = True) -> dict:
    return {"id": row.id, "file_name": row.file_name if visible else None, "mime": row.mime, "bytes": row.bytes,
            "category": row.category, "sensitivity": row.sensitivity, "hidden": not visible,
            "created_at": row.created_at.isoformat() if row.created_at else None}


# --------------------------------------------------------------------------- reads


def _audit(db: Session, principal: Optional[Principal], row: AttendanceRequest, action: str, description: str,
           **details: Any) -> None:
    AuditLogService.log(db, principal, AUDIT_MODULE, action, description, "attendance_request", row.id,
                        company_id=row.company_id, actor_type="user" if principal else "system",
                        details={"request_id": row.id, "employee_id": row.employee_id, "kind": row.kind,
                                 "mode": row.mode, "status": row.status, "shift_date": row.shift_date.isoformat(),
                                 **details})


def summary(row: AttendanceRequest, zone: Optional[str] = None) -> str:
    """"Punch-out 18:30", "Add 20:00–22:30", "Excuse late 25 m" (§17.2 Request column)."""
    import pytz

    tz = pytz.timezone(zone) if zone else pytz.utc
    p = row.payload or {}

    def hm(value: Optional[str]) -> str:
        return parse_instant(value, "t").astimezone(tz).strftime("%H:%M") if value else "–"

    if row.kind == "MISSED_PUNCH":
        label = {"IN": "Punch-in", "OUT": "Punch-out", "BREAK_END": "Break end"}.get(p.get("direction"), "Punch")
        return f"{label} {hm(p.get('at'))}"
    if row.kind in ("ADD_SESSION", "CORRECTION"):
        verb = "Add" if row.kind == "ADD_SESSION" else "Correct to"
        return f"{verb} {hm(p.get('in_at'))}–{hm(p.get('out_at'))}"
    if row.kind == "MARK_EXCUSE":
        return f"Excuse {str(p.get('mark', '')).lower()} {p.get('minutes')} m"
    if row.kind == "STATUS_OVERRIDE":
        halves = [f"{h.replace('_', ' ')} {v.lower()}" for h, v in p.items()]
        return "Mark " + ", ".join(halves)
    return "Remove session"


def serialize(db: Session, principal: Principal, row: AttendanceRequest, *, detail: bool = False) -> dict:
    from app.services import attendance_service

    employee = db.get(Employee, row.employee_id)
    zone = attendance_service.work_zone(db, employee, row.shift_date).zone if employee is not None else None
    reason = db.query(AttendanceReasonCode).filter(AttendanceReasonCode.company_id == row.company_id,
                                                   AttendanceReasonCode.code == row.reason_code).first()
    approval = None
    actions: List[str] = []
    own = row.requested_by_user_id == principal.user_id
    if row.approval_request_id:
        request = db.get(approvals.ApprovalRequest, row.approval_request_id)
        if request is not None:
            ctx = approvals._Context(db, principal.company_id)
            ctx.seed(principal)
            visible, acting = approvals._access(ctx, request, principal)
            current = next((s for s in request.steps if s.status == "pending"), None)
            approval = {"id": request.id, "status": request.status, "route_key": (request.context or {}).get("route_key"),
                        "current_step": request.current_step,
                        "due_at": current.due_at.isoformat() if current is not None and current.due_at else None,
                        "pending_reason": (request.context or {}).get("pending_reason"), "can_act": acting is not None,
                        "steps": [approvals._step_view(ctx, request, s) for s in request.steps] if detail else None}
            if acting is not None and row.status == "PENDING":
                actions += ["APPROVE", "REJECT", "RETURN"]
    if own and row.status in ("DRAFT", "RETURNED"):
        actions += ["EDIT", "SUBMIT"]
    if (own or authz.is_self(principal, row.employee_id)) and row.status in ("DRAFT", "PENDING", "RETURNED"):
        actions.append("WITHDRAW")
    body = {
        "id": row.id, "employee_id": row.employee_id, "employee_name": employee.full_name if employee else None,
        "employee_code": getattr(employee, "employee_code", None), "shift_date": row.shift_date.isoformat(),
        "kind": row.kind, "kind_label": KIND_LABELS[row.kind], "mode": row.mode, "status": row.status,
        "session_id": row.session_id, "payload": row.payload, "summary": summary(row, zone), "work_timezone": zone,
        "reason_code": row.reason_code, "reason_label": reason.label if reason else row.reason_code,
        "comment": row.comment if (own or authz.is_self(principal, row.employee_id) or
                                   (approval or {}).get("can_act") or principal.has("attendance:regularize")) else None,
        "minutes": row.minutes, "counts_toward_limit": row.counts_toward_limit, "issues": row.issues or [],
        "requested_by_user_id": row.requested_by_user_id, "submitted_at": _iso(row.submitted_at),
        "decided_at": _iso(row.decided_at), "applied_at": _iso(row.applied_at),
        "decision_remarks": row.decision_remarks, "version": row.version, "row_version": row.row_version,
        "created_at": _iso(row.created_at), "approval": approval, "allowed_actions": actions,
    }
    if detail:
        files = (db.query(AttendanceAttachment)
                 .filter(AttendanceAttachment.company_id == row.company_id, AttendanceAttachment.request_id == row.id)
                 .order_by(AttendanceAttachment.id).all())
        hr = principal.has("attendance:regularize") and authz.can_access_employee(
            db, principal, "attendance:regularize", row.employee_id)
        requester = own or authz.is_self(principal, row.employee_id)
        body["attachments"] = [attachment_json(f, visible=f.sensitivity != "MEDICAL" or requester or hr)
                               for f in files]
        body["day"] = _day_summary(_day_row(db, row.company_id, row.employee_id, row.shift_date))
        body["payroll_state"] = payroll_state(db, row.company_id, row.employee_id, row.shift_date)
    return body


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def list_requests(db: Session, principal: Principal, *, mine: bool = False, employee_id: Optional[int] = None,
                  status: Optional[str] = None, kind: Optional[str] = None, start: Optional[date] = None,
                  end: Optional[date] = None, skip: int = 0, limit: int = 50) -> Tuple[int, List[dict]]:
    """Own requests (``attendance:request``) and, with ``attendance:read``, those of people in scope."""
    from app.services import attendance_access

    attendance_access.require_v2(db, principal.company_id)
    query = db.query(AttendanceRequest).filter(AttendanceRequest.company_id == principal.company_id)
    own = or_(AttendanceRequest.requested_by_user_id == principal.user_id,
              AttendanceRequest.employee_id == (principal.employee_id or 0))
    if mine or not principal.has("attendance:read"):
        query = query.filter(own)
    else:
        scope = authz.employee_scope(db, principal, "attendance:read")
        query = query.filter(or_(own, scope.clause(AttendanceRequest.employee_id)))
    if employee_id is not None:
        query = query.filter(AttendanceRequest.employee_id == employee_id)
    if status:
        query = query.filter(AttendanceRequest.status.in_(status.split(",")))
    if kind:
        query = query.filter(AttendanceRequest.kind == kind)
    if start is not None:
        query = query.filter(AttendanceRequest.shift_date >= start)
    if end is not None:
        query = query.filter(AttendanceRequest.shift_date <= end)
    total = query.count()
    rows = (query.order_by(AttendanceRequest.created_at.desc(), AttendanceRequest.id.desc())
            .offset(skip).limit(min(limit, MAX_LIST)).all())
    return total, [serialize(db, principal, r) for r in rows]


def for_approval(db: Session, principal: Principal, approval_request_id: int) -> Optional[dict]:
    """Subject summary of an engine request for the module-neutral inbox."""
    row = db.query(AttendanceRequest).filter(AttendanceRequest.company_id == principal.company_id,
                                             AttendanceRequest.approval_request_id == approval_request_id).first()
    return serialize(db, principal, row) if row is not None else None


from app.payroll.services import jobs as _jobs  # noqa: E402


@_jobs.register("attendance.sla_scan")
def sla_scan_job(ctx) -> dict:
    """HOURLY: reminders and escalations of overdue attendance / timesheet approval steps (§9.5); never approves."""
    return approvals.sla_scan(ctx.db, ctx.company_id)


__all__ = ["create", "preview", "submit", "update", "withdraw", "apply", "list_requests", "serialize", "load",
           "reason_codes", "seed_company", "policy_fields", "upload_attachment", "attachment_for_download",
           "unprocessable", "self_edit_forbidden"]
