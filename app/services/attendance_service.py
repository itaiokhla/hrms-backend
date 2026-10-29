"""Attendance: self-service punches and lunch, company kiosk, manual corrections, analytics.

The employee for self-service actions always comes from the session (never
from a request parameter), face verification happens on the server, and
kiosk matching only ever compares against faces of the caller's company.

State machine (server-enforced, one live session per employee):

    NOT_PUNCHED_IN -> WORKING -> ON_LUNCH -> WORKING -> ... -> PUNCHED_OUT
                      WORKING -> PUNCHED_OUT

Every transition takes a per-employee transaction lock, records an event with
the server's time in the append-only event log, and accepts an idempotency key
so a retried or double-clicked request returns the first result instead of
acting twice. Hours are always computed by attendance_hours.compute.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import pytz
from fastapi import status as http_status
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import authz, clock
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.attendance import Attendance, AttendanceBreak, AttendanceEvent
from app.models.company import Company
from app.models.employee import Department, Employee, FaceEncoding, Shift
from app.modules.face_recognition import engine as face_engine
from app.schemas.attendance import EventMeta
from app.services import attendance_hours, attendance_policy, attendance_sessions, location_service, settings_service
from app.services.attendance_policy import AttendancePolicy
from app.services.audit_log_service import AuditLogService

MIN_SESSION = timedelta(minutes=1)
ATTENDANCE_LOCK_NAMESPACE = 4243

NOT_PUNCHED_IN, WORKING, ON_LUNCH, PUNCHED_OUT = "NOT_PUNCHED_IN", "WORKING", "ON_LUNCH", "PUNCHED_OUT"
LEGACY_STATE = {NOT_PUNCHED_IN: "not_checked_in", WORKING: "working", ON_LUNCH: "working", PUNCHED_OUT: "completed"}
ACTIONS = ("PUNCH_IN", "LUNCH_IN", "LUNCH_OUT", "PUNCH_OUT")
LIVE_CONSTRAINTS = ("uq_attendance_employee_day_seq", "uq_attendance_one_live_session")
# Punch direction of each legacy action (spine §3.13 PUNCH_DIRECTIONS; 01 §4.1).
DIRECTION_OF = {"PUNCH_IN": "IN", "PUNCH_OUT": "OUT", "LUNCH_IN": "BREAK_START", "LUNCH_OUT": "BREAK_END"}
ACTION_OF = {v: k for k, v in DIRECTION_OF.items()}


@dataclass
class Capture:
    """How a punch was captured (AS-M2): written to attendance_punches and the session (spine §3.1, §3.2).

    Legacy callers get the defaults of their lowercase source; ``attendance_capture`` fills the v2 fields.
    ``punch_id`` is set by the transition that wrote the punch.
    """

    source: str = "WEB"  # CAPTURE_SOURCES
    multiple_sessions_allowed: bool = False
    verify_mode: Optional[str] = None
    device_id: Optional[int] = None
    site_id: Optional[int] = None
    geofence_result: str = "NOT_CHECKED"
    evidence_flags: Tuple[str, ...] = ()
    ip: Optional[str] = None
    work_mode: str = "ON_SITE"
    skip_face: bool = False
    # The capture policy overlaid on the company settings (attendance_capture_policy.as_attendance_policy); None = the
    # company attendance policy, as for the legacy endpoints.
    attendance_policy: object = field(default=None, repr=False)
    punch_id: Optional[int] = None
    shift_instance: object = field(default=None, repr=False)
    # Team punch (a manager punching for a team member): who acted and why, on the raw punch (internal, SR-AS-19).
    actor_user_id: Optional[int] = None
    reason: Optional[str] = None
    # Team punch: the location outcome of the punch event (the event row is append-only, so it is set at insert).
    event_location_status: Optional[str] = None

    @classmethod
    def legacy(cls, source: str, ip: Optional[str] = None) -> "Capture":
        from app.models.attendance import CAPTURE_OF_SOURCE

        return cls(source=CAPTURE_OF_SOURCE.get(source, "WEB"), ip=ip)


def _company(db: Session, principal: Principal) -> Company:
    return db.get(Company, principal.company_id)


def _tz(company: Company):
    return pytz.timezone(company.timezone or "UTC")


def _now() -> datetime:
    # Whole seconds: stored instants and every computed total then agree exactly.
    return clock.utcnow().replace(microsecond=0)


def local_today(company: Company, now: Optional[datetime] = None) -> date:
    return (now or clock.utcnow()).astimezone(_tz(company)).date()


def _status_for_check_in(db: Session, company: Company, employee: Employee, when: datetime) -> str:
    shift = db.get(Shift, employee.shift_id) if employee.shift_id else None
    if shift is None or shift.is_flexible:
        return "present"
    grace = settings_service.get_value(db, company.id, "graceTime")
    hour, minute = (int(x) for x in shift.start_time.split(":"))
    local = when.astimezone(_tz(company))
    start = _tz(company).localize(datetime.combine(local.date(), time(hour, minute)))
    return "late" if local > start + timedelta(minutes=int(grace)) else "present"


def lock_employee(db: Session, employee_id: int) -> None:
    """Serialise attendance changes for one employee (transaction scoped, re-entrant)."""
    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :id)"), {"ns": ATTENDANCE_LOCK_NAMESPACE, "id": employee_id})


def live_session(db: Session, employee_id: int, lock: bool = False) -> Optional[Attendance]:
    query = db.query(Attendance).filter(
        Attendance.employee_id == employee_id, Attendance.check_out_time.is_(None), Attendance.closed_at.is_(None),
        Attendance.voided_at.is_(None),
    )
    if lock:
        query = query.with_for_update()
    return query.order_by(Attendance.check_in_time.desc()).first()


def _record_for_day(db: Session, employee_id: int, day: date) -> Optional[Attendance]:
    return (
        db.query(Attendance)
        .filter(Attendance.employee_id == employee_id, Attendance.date == day, Attendance.voided_at.is_(None))
        .order_by(Attendance.check_in_time.desc())
        .first()
    )


# Kept for callers (and tests) that look up a person's record for a day.
_open_record = _record_for_day


def _open_break(db: Session, attendance_id: int) -> Optional[AttendanceBreak]:
    return db.query(AttendanceBreak).filter(
        AttendanceBreak.attendance_id == attendance_id, AttendanceBreak.ended_at.is_(None)
    ).first()


def breaks_by_session(db: Session, attendance_ids) -> Dict[int, List[AttendanceBreak]]:
    ids = list(attendance_ids)
    result: Dict[int, List[AttendanceBreak]] = {i: [] for i in ids}
    if ids:
        for b in (
            db.query(AttendanceBreak)
            .filter(AttendanceBreak.attendance_id.in_(ids))
            .order_by(AttendanceBreak.started_at, AttendanceBreak.id)
        ):
            result[b.attendance_id].append(b)
    return result


def is_live(record: Attendance) -> bool:
    return record.check_out_time is None and record.closed_at is None and record.voided_at is None


def hours_for(record: Attendance, breaks: List[AttendanceBreak], now: datetime) -> attendance_hours.Hours:
    intervals = [attendance_hours.BreakInterval(b.started_at, b.ended_at, b.is_paid, b.kind) for b in breaks]
    return attendance_hours.compute(record.check_in_time, record.check_out_time, intervals, now=now, live=is_live(record))


def content_hash(record: Attendance, breaks: List[AttendanceBreak]) -> str:
    """Version hash of a session with its breaks, as payroll snapshots and the source change log store it."""
    from app.payroll.services.time_inputs import content_hash as _hash

    return _hash(content(record, breaks))


def content(record: Attendance, breaks: List[AttendanceBreak]) -> dict:
    """The pay-relevant fields of a session (break ids change on correction, so they are left out)."""
    return {
        "id": record.id, "employee_id": record.employee_id, "date": record.date, "status": record.status,
        "check_in_time": record.check_in_time, "check_out_time": record.check_out_time,
        "closed_at": record.closed_at, "closed_reason": record.closed_reason, "shift_id": record.shift_id,
        "late_minutes": record.late_minutes, "early_exit_minutes": record.early_exit_minutes,
        # AS-M2 (spine §3.1, A-18): a void, a second session or a source change is a new version.
        "voided_at": record.voided_at, "session_seq": record.session_seq, "capture_source": record.capture_source,
        "breaks": sorted(
            [{"kind": b.kind, "started_at": b.started_at, "ended_at": b.ended_at, "is_paid": bool(b.is_paid)} for b in breaks],
            key=lambda b: (b["started_at"], b["ended_at"] or b["started_at"])),
    }


def shift_window(shift: Optional[Shift], session_date: date, check_in: datetime, tz) -> Optional[tuple]:
    """Scheduled (start, end) of the shift occurrence a session belongs to; None without a fixed shift.

    Overnight shifts end the next day; the occurrence is the one whose start is nearest the punch-in, so
    a night shift punched in after midnight is still measured against the start of the evening before.
    """
    if shift is None or shift.is_flexible:
        return None
    try:
        sh, sm = (int(x) for x in shift.start_time.split(":"))
        eh, em = (int(x) for x in shift.end_time.split(":"))
    except (AttributeError, ValueError):
        return None
    candidates = []
    for offset in (-1, 0, 1):
        base = session_date + timedelta(days=offset)
        start = tz.localize(datetime.combine(base, time(sh, sm)))
        end_day = base + timedelta(days=1) if (eh, em) <= (sh, sm) else base
        end = tz.localize(datetime.combine(end_day, time(eh, em)))
        candidates.append((abs((check_in - start).total_seconds()), start, end))
    _, start, end = min(candidates, key=lambda c: c[0])
    return start, end


def apply_timing(db: Session, company: Company, record: Attendance) -> None:
    """Raw late and early-exit minutes against the session's shift (3.4 §11); grace is payroll policy."""
    shift = db.get(Shift, record.shift_id) if record.shift_id else None
    window = shift_window(shift, record.date, record.check_in_time, _tz(company)) if record.status != "absent" else None
    if window is None:
        record.late_minutes, record.early_exit_minutes, record.early_exit = None, None, False
        return
    start, end = window
    record.late_minutes = max(0, int((record.check_in_time - start).total_seconds() // 60))
    if record.check_out_time is None:
        record.early_exit_minutes, record.early_exit = None, False
        return
    minutes = max(0, int((end - record.check_out_time).total_seconds() // 60))
    record.early_exit_minutes, record.early_exit = minutes, minutes > 0


def record_version(db: Session, record: Attendance, change: str, actor_user_id: Optional[int], *, bump: bool = True) -> None:
    """A new version of the session: bump row_version and append it to the source change log (3.4 §16)."""
    from app.payroll.services import time_inputs

    if bump:
        record.row_version = (record.row_version or 1) + 1
    db.flush()
    breaks = breaks_by_session(db, [record.id])[record.id]
    time_inputs.log_change(
        db, record.company_id, record_type="attendance", record_id=record.id, change=change,
        content=content(record, breaks),
        employee_id=record.employee_id, date_from=record.date, actor_user_id=actor_user_id,
        details={"row_version": record.row_version, "status": record.status},
    )


def session_state(record: Optional[Attendance], breaks: List[AttendanceBreak]) -> str:
    if record is None:
        return NOT_PUNCHED_IN
    if is_live(record):
        return ON_LUNCH if any(b.ended_at is None for b in breaks) else WORKING
    return PUNCHED_OUT


def _insert_day(db: Session, record: Attendance) -> Attendance:
    """Insert a person's session; of two concurrent inserts the second gets a 409 (``ALREADY_CHECKED_IN`` for the
    same date and number or a second live session, ``SESSION_OVERLAP`` from the overlap trigger), never a 500."""
    from app.core import db_errors

    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError as exc:
        error = db_errors.map_integrity(exc)
        if error is not None:
            raise error
        raise
    return record


def _event(db: Session, record: Attendance, event_type: str, now: datetime, source: str, actor: Optional[int],
           *, key: Optional[str] = None, score: Optional[float] = None, location_status: str = "NOT_APPLICABLE",
           note: Optional[str] = None, details: Optional[dict] = None, punch_id: Optional[int] = None) -> AttendanceEvent:
    event = AttendanceEvent(
        company_id=record.company_id, attendance_id=record.id, employee_id=record.employee_id,
        event_type=event_type, occurred_at=now, source=source, actor_user_id=actor, idempotency_key=key,
        face_match_score=score, location_status=location_status, note=note,
        details=json.dumps(details, default=str) if details else None, punch_id=punch_id,
    )
    db.add(event)
    db.flush()
    return event


def expire_if_needed(db: Session, record: Attendance, policy: AttendancePolicy, now: datetime) -> bool:
    """Close a session left open past the company's maximum shift length, without inventing a punch-out."""
    if not is_live(record) or now < record.check_in_time + policy.max_shift:
        return False
    location_service.stop_tracking(db, record, policy, min(now, record.check_in_time + policy.max_shift), "SHIFT_EXPIRED")
    record.closed_at = now
    record.closed_reason = "MISSING_PUNCH_OUT"
    record.occupied_until = attendance_sessions.occupied_until(record, policy.max_shift_hours)
    _event(db, record, "SESSION_CLOSED", now, "system", None,
           note=f"No punch-out within {policy.max_shift_hours} hours; closed for HR correction")
    record_version(db, record, "UPDATED", None)
    AuditLogService.log(db, None, "Attendance", "AUTO_CLOSE",
                        f"Shift of employee #{record.employee_id} from {record.date} closed without a punch-out",
                        "attendance", record.id, company_id=record.company_id, actor_type="system")
    return True


# ---------------------------------------------------------------------------
# Face verification
# ---------------------------------------------------------------------------
def _require_engine() -> None:
    if not face_engine.available():
        raise AppError(
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            "Face recognition is not available on this server.",
            "FACE_ENGINE_UNAVAILABLE",
        )


def _encode_or_400(image: bytes) -> str:
    _require_engine()
    try:
        return face_engine.encode(image)
    except face_engine.FaceNotDetected as exc:
        raise bad_request(str(exc), "FACE_NOT_DETECTED")


def verify_own_face(db: Session, employee: Employee, image: bytes) -> float:
    """1:1 verification of the signed-in employee against their own enrolment."""
    probe = _encode_or_400(image)
    from app.services import attendance_faces

    candidates = attendance_faces.candidates(
        db, db.query(FaceEncoding).filter(FaceEncoding.employee_id == employee.id).all())
    if not candidates:
        raise bad_request("Your face is not enrolled yet. Ask HR to register it.", "FACE_NOT_ENROLLED")
    match = face_engine.best_match(probe, candidates, app_settings.face_recognition_threshold)
    if match is None:
        raise AppError(http_status.HTTP_403_FORBIDDEN, "Face did not match your profile.", "FACE_MISMATCH")
    return face_engine.similarity(match[1])


def _face_score(db: Session, employee: Employee, action: str, image: Optional[bytes], policy: AttendancePolicy):
    required = policy.require_face if action in ("PUNCH_IN", "PUNCH_OUT") else policy.require_face_for_lunch
    if required:
        if not image:
            raise bad_request("A face capture is required to mark attendance.", "FACE_REQUIRED")
        return verify_own_face(db, employee, image)
    if image and face_engine.available():
        return verify_own_face(db, employee, image)
    return None


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
def _placement(db: Session, employee: Employee, day: date):
    from app.models.org import EmployeeAssignment

    return (db.query(EmployeeAssignment)
            .filter(EmployeeAssignment.company_id == employee.company_id, EmployeeAssignment.employee_id == employee.id,
                    EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= day)
            .order_by(EmployeeAssignment.effective_from.desc(), EmployeeAssignment.id.desc()).first())


def _write_punch(db: Session, record: Attendance, employee: Employee, capture: "Capture", direction: str,
                 now: datetime, meta: EventMeta, score) -> int:
    """The raw punch of an interactive transition (spine §3.2, 01 §3.1), linked to its session."""
    verify = capture.verify_mode or ("FACE" if score is not None else "NONE")
    punch = attendance_sessions.record_punch(
        db, company_id=record.company_id, employee_id=employee.id, source=capture.source, direction=direction,
        occurred_at=now, session=record, shift_date=record.date, idempotency_key=meta.idempotency_key,
        client_sent_at=meta.client_sent_at, verify_mode=verify, face_match_score=score, device_id=capture.device_id,
        site_id=capture.site_id, geofence_result=capture.geofence_result, evidence_flags=capture.evidence_flags,
        ip=capture.ip, actor_user_id=capture.actor_user_id, reason=capture.reason,
    )
    attendance_sessions.apply_interactive(db, record.company_id, punch, capture.shift_instance, record)
    capture.punch_id = punch.id
    return punch.id


def _punch_in(db, principal, company, employee, policy, meta: EventMeta, score, now, source="self",
              capture: Optional[Capture] = None) -> Attendance:
    capture = capture or Capture.legacy(source)
    if live_session(db, employee.id) is not None:
        raise conflict("Already checked in today.", "ALREADY_CHECKED_IN")
    # Shift date = start date of the matched instance in the work zone (BUG-AS-02); one session per date unless the
    # capture policy allows several (v1 tenants never do, spine §3.1 legacy guard).
    shift = attendance_sessions.resolve_shift_date(db, company.id, employee, now, "IN")
    seq = attendance_sessions.next_session_seq(db, company.id, employee.id, shift.date,
                                               capture.multiple_sessions_allowed)
    attendance_sessions.check_overlap(db, company.id, employee.id, now, None)
    capture.shift_instance = shift.instance
    if source == "self" and (policy.tracking_enabled or policy.location_punch != "OFF") and not meta.location_consent:
        raise bad_request("Please acknowledge the location notice before punching in.", "LOCATION_NOTICE_REQUIRED")
    location = location_service.evaluate_event(policy, "PUNCH_IN", meta.location, meta.exception_reason, now,
                                               meta.client_sent_at, source)
    placement = _placement(db, employee, shift.date)
    record = _insert_day(db, Attendance(
        company_id=company.id, employee_id=employee.id, check_in_time=now, date=shift.date, shift_id=employee.shift_id,
        face_match_score=score, is_manual=False, source=source, capture_source=capture.source, session_seq=seq,
        work_timezone=shift.work_timezone, shift_version_id=shift.shift_version_id, work_mode=capture.work_mode,
        legal_entity_id=getattr(placement, "legal_entity_id", None), branch_id=getattr(placement, "branch_id", None),
        status=_status_for_check_in(db, company, employee, now), recorded_by_user_id=principal.user_id,
    ))
    if policy.tracking_enabled:
        if source == "self":
            location_service.start_tracking(record, policy, now)
        else:
            record.tracking_stop_reason = "KIOSK_SESSION"
    punch_id = _write_punch(db, record, employee, capture, "IN", now, meta, score)
    event = _event(db, record, "PUNCH_IN", now, source, principal.user_id, key=meta.idempotency_key, score=score,
                   location_status=capture.event_location_status or location.event_status, details=location_service.event_details(location),
                   punch_id=punch_id)
    location_service.record_event_sample(db, record, event, location, policy, now, meta.client_sent_at)
    return record


def _lunch_in(db, principal, company, employee, policy, meta: EventMeta, score, now, live,
              capture: Optional[Capture] = None) -> Attendance:
    capture = capture or Capture.legacy("self")
    source = "kiosk" if capture.source == "KIOSK" else "self"
    if live is None:
        raise conflict("Punch in before starting lunch.", "NOT_CHECKED_IN")
    breaks = breaks_by_session(db, [live.id])[live.id]
    if any(b.ended_at is None for b in breaks):
        raise conflict("You are already on lunch.", "ALREADY_ON_LUNCH")
    if breaks and not policy.allow_multiple_lunches:
        raise conflict("Your company allows one lunch break per shift.", "LUNCH_LIMIT")
    location = location_service.evaluate_event(policy, "LUNCH_IN", meta.location, meta.exception_reason, now,
                                               meta.client_sent_at, source)
    punch_id = _write_punch(db, live, employee, capture, "BREAK_START", now, meta, score)
    db.add(AttendanceBreak(
        company_id=live.company_id, attendance_id=live.id, employee_id=employee.id, kind="LUNCH", started_at=now,
        is_paid=policy.lunch_is_paid, created_by_user_id=principal.user_id, start_punch_id=punch_id,
    ))
    db.flush()
    event = _event(db, live, "LUNCH_IN", now, source, principal.user_id, key=meta.idempotency_key, score=score,
                   location_status=capture.event_location_status or location.event_status, details=location_service.event_details(location),
                   punch_id=punch_id)
    location_service.record_event_sample(db, live, event, location, policy, now, meta.client_sent_at)
    if policy.pause_on_lunch:
        location_service.pause_tracking(db, live, policy, now)
    return live


def _lunch_out(db, principal, company, employee, policy, meta: EventMeta, score, now, live,
               capture: Optional[Capture] = None) -> Attendance:
    capture = capture or Capture.legacy("self")
    source = "kiosk" if capture.source == "KIOSK" else "self"
    if live is None:
        raise conflict("Punch in before ending lunch.", "NOT_CHECKED_IN")
    open_break = _open_break(db, live.id)
    if open_break is None:
        raise conflict("You are not on lunch.", "NOT_ON_LUNCH")
    location = location_service.evaluate_event(policy, "LUNCH_OUT", meta.location, meta.exception_reason, now,
                                               meta.client_sent_at, source)
    punch_id = _write_punch(db, live, employee, capture, "BREAK_END", now, meta, score)
    open_break.ended_at = now
    open_break.end_reason = "LUNCH_OUT"
    open_break.end_punch_id = punch_id
    db.flush()
    location_service.resume_tracking(live, policy, now)
    event = _event(db, live, "LUNCH_OUT", now, source, principal.user_id, key=meta.idempotency_key, score=score,
                   location_status=capture.event_location_status or location.event_status, details=location_service.event_details(location),
                   punch_id=punch_id)
    location_service.record_event_sample(db, live, event, location, policy, now, meta.client_sent_at)
    return live


def _punch_out(db, principal, company, employee, policy, meta: EventMeta, score, now, live, source="self",
               capture: Optional[Capture] = None) -> Attendance:
    capture = capture or Capture.legacy(source)
    if live is None:
        raise bad_request("No active check-in found for today.", "NOT_CHECKED_IN")
    if now - live.check_in_time < MIN_SESSION:
        raise bad_request("Please wait a minute before checking out.", "TOO_SOON")
    open_break = _open_break(db, live.id)
    if open_break is not None and source == "self":
        if policy.lunch_open_at_punch_out == "BLOCK":
            raise conflict("End your lunch before punching out.", "LUNCH_OPEN")
        if not meta.confirm_close_lunch:
            raise conflict("You're still on lunch. Confirm to end lunch now and punch out.", "LUNCH_OPEN_CONFIRM")
    location = location_service.evaluate_event(policy, "PUNCH_OUT", meta.location, meta.exception_reason, now,
                                               meta.client_sent_at, source)
    note = None
    if open_break is not None:
        open_break.ended_at = now
        open_break.end_reason = "AUTO_CLOSED_AT_PUNCH_OUT"
        note = "Lunch ended at punch-out"
        db.flush()  # the break closes before the session does (both are checked by triggers)
    location_service.stop_tracking(db, live, policy, now, "PUNCHED_OUT")
    live.check_out_time = now
    live.occupied_until = now
    apply_timing(db, company, live)
    db.flush()
    punch_id = _write_punch(db, live, employee, capture, "OUT", now, meta, score)
    record_version(db, live, "UPDATED", principal.user_id)
    details = location_service.event_details(location)
    if meta.no_break_taken:  # step 5 (d): read by the evaluation when the rules ask for the attestation
        details = {**(details or {}), "no_break_taken": True}
    event = _event(db, live, "PUNCH_OUT", now, source, principal.user_id, key=meta.idempotency_key, score=score,
                   location_status=capture.event_location_status or location.event_status, note=note, details=details, punch_id=punch_id)
    location_service.record_event_sample(db, live, event, location, policy, now, meta.client_sent_at)
    return live


_HANDLERS = {"LUNCH_IN": _lunch_in, "LUNCH_OUT": _lunch_out, "PUNCH_OUT": _punch_out}
_AUDIT = {"PUNCH_IN": "CHECK_IN", "PUNCH_OUT": "CHECK_OUT", "LUNCH_IN": "LUNCH_IN", "LUNCH_OUT": "LUNCH_OUT"}
_VERBS = {"PUNCH_IN": "punched in", "PUNCH_OUT": "punched out", "LUNCH_IN": "started lunch", "LUNCH_OUT": "ended lunch"}


def perform(db: Session, principal: Principal, action: str, image: Optional[bytes], meta: Optional[EventMeta],
            ip: Optional[str], *, capture: Optional[Capture] = None) -> dict:
    """Run one self-service transition and return the employee's fresh attendance view.

    Every transition writes one attendance_punches row (source WEB for the legacy endpoints; ``capture`` from
    ``attendance_capture.punch`` for ``POST /attendance/me/punch``)."""
    if action not in ACTIONS:
        raise bad_request("Unknown attendance action.")
    if not principal.has("attendance:mark"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    if not employee.is_active:
        raise forbidden("Your employment record is inactive.")
    company = _company(db, principal)
    capture = capture or Capture.legacy("self", ip)
    policy = capture.attendance_policy or attendance_policy.load(db, company.id)
    meta = meta or EventMeta()
    score = None if capture.skip_face else _face_score(db, employee, action, image, policy)

    lock_employee(db, employee.id)
    # Re-read under the lock: a policy changed while the face was being checked applies to this action.
    policy = capture.attendance_policy or attendance_policy.load(db, company.id)
    if meta.idempotency_key:
        previous = db.query(AttendanceEvent).filter(
            AttendanceEvent.employee_id == employee.id, AttendanceEvent.idempotency_key == meta.idempotency_key
        ).first()
        if previous is not None:
            if previous.event_type != action:
                raise conflict("This request id was already used for a different action.", "IDEMPOTENCY_CONFLICT")
            return {**_acted_on(db, previous.attendance_id, employee), **my_today(db, principal), "replayed": True}

    now = _now()
    live = live_session(db, employee.id, lock=True)
    if live is not None and expire_if_needed(db, live, policy, now):
        live = None
    # Step 9 of 01 §4.2: the stale-tab guard applies only while a live session exists; without one the transition
    # answers 400 NOT_CHECKED_IN (EC AS-CAP-01 (b)).
    if action != "PUNCH_IN" and meta.attendance_id is not None and live is not None and live.id != meta.attendance_id:
        raise conflict("Your attendance changed in another tab or device. The page has been refreshed.", "STALE_SESSION")

    if action == "PUNCH_IN":
        record = _punch_in(db, principal, company, employee, policy, meta, score, now, capture=capture)
    else:
        record = _HANDLERS[action](db, principal, company, employee, policy, meta, score, now, live, capture=capture)
    AuditLogService.log(db, principal, "Attendance", _AUDIT[action], f"{employee.full_name} {_VERBS[action]}",
                        "attendance", record.id, ip)
    # AS-M3 (04-attendance/03 §8.1): the punch transaction re-evaluates its shift date (v2 tenants only).
    from app.services import attendance_days

    attendance_days.evaluate_dates(db, company.id, employee.id, [record.date],
                                   trigger="SESSION_CLOSED" if action == "PUNCH_OUT" else "PUNCH", now=now)
    # Backwards compatible: the record acted on at the top level (as before), plus the fresh view.
    return {**_acted_on(db, record.id, employee), **my_today(db, principal)}


def _acted_on(db: Session, attendance_id: int, employee: Employee) -> dict:
    record = db.get(Attendance, attendance_id)
    return serialize(record, employee, breaks=breaks_by_session(db, [record.id])[record.id])


def self_check(db: Session, principal: Principal, action: str, image: Optional[bytes], ip: Optional[str],
               meta: Optional[EventMeta] = None) -> dict:
    """Legacy entry point: check_in / check_out are Punch In / Punch Out."""
    return perform(db, principal, "PUNCH_IN" if action == "check_in" else "PUNCH_OUT", image, meta, ip)


def kiosk_scan(db: Session, principal: Principal, image: bytes, ip: Optional[str]) -> dict:
    """Shared kiosk: identify within this company, then punch in or out."""
    if not principal.has_company_scope("attendance:kiosk"):
        raise forbidden()
    probe = _encode_or_400(image)
    from app.services import attendance_consents, attendance_faces

    rows = (db.query(FaceEncoding).join(Employee, Employee.id == FaceEncoding.employee_id)
            .filter(FaceEncoding.company_id == principal.company_id, FaceEncoding.is_primary.is_(True),
                    Employee.is_active.is_(True)).all())
    # B-7: 1:N only among employees whose face step is allowed today (consent, or a legacy enrolment in its grace
    # under the LEGACY_V1 policy of the operator kiosk).
    today = local_today(_company(db, principal), _now())
    rows = [r for r in rows if attendance_consents.face_allowed(db, principal.company_id, r.employee_id, today,
                                                                 profile="LEGACY_V1")]
    candidates = attendance_faces.candidates(db, rows)
    if not candidates:
        raise bad_request("No faces are enrolled for this company yet.", "NO_ENROLMENTS")
    match = face_engine.best_match(probe, candidates, app_settings.face_recognition_threshold)
    if match is None:
        return {"success": False, "action": "unknown", "message": "Face not recognised."}
    employee = db.get(Employee, match[0])
    company = _company(db, principal)
    policy = attendance_policy.load(db, company.id)
    score = face_engine.similarity(match[1])
    lock_employee(db, employee.id)
    now = _now()
    live = live_session(db, employee.id, lock=True)
    if live is not None and expire_if_needed(db, live, policy, now):
        live = None
    if live is None:
        shift = attendance_sessions.resolve_shift_date(db, company.id, employee, now, "IN")
        if attendance_sessions.sessions_on(db, company.id, employee.id, shift.date):
            return {"success": True, "action": "completed", "employee_name": employee.full_name,
                    "message": "Attendance already completed today."}
        record = _punch_in(db, principal, company, employee, policy, EventMeta(), score, now, source="kiosk",
                           capture=Capture(source="KIOSK", verify_mode="FACE", ip=ip))
        action, message = "check_in", "Check-in successful"
    else:
        if now - live.check_in_time < MIN_SESSION:
            return {"success": False, "action": "wait", "employee_name": employee.full_name,
                    "message": "Please wait a minute before checking out."}
        # Scanning out at the kiosk is the confirmation: an open lunch ends with the shift.
        record = _punch_out(db, principal, company, employee, policy, EventMeta(), score, now, live, source="kiosk",
                            capture=Capture(source="KIOSK", verify_mode="FACE", ip=ip))
        action, message = "check_out", "Check-out successful"
    AuditLogService.log(
        db, principal, "Attendance", f"KIOSK_{action.upper()}", f"Kiosk {action.replace('_', ' ')}: {employee.full_name}",
        "attendance", record.id, ip,
    )
    return {"success": True, "action": action, "employee_name": employee.full_name, "message": message,
            "confidence": score, "time": now.isoformat()}


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------
def _snapshot(record: Attendance, breaks: List[AttendanceBreak]) -> dict:
    return {
        "check_in_time": record.check_in_time, "check_out_time": record.check_out_time, "status": record.status,
        "closed_reason": record.closed_reason,
        "breaks": [{"started_at": b.started_at, "ended_at": b.ended_at, "is_paid": b.is_paid} for b in breaks],
    }


def _validate_breaks(items, start: datetime, end: Optional[datetime]) -> None:
    ordered = sorted(items, key=lambda b: b.started_at)
    for item in ordered:
        if item.started_at.tzinfo is None or item.ended_at.tzinfo is None:
            raise bad_request("Break times must include a timezone.")
        if item.ended_at < item.started_at:
            raise bad_request("A break cannot end before it starts.")
        if item.started_at < start or (end is not None and item.ended_at > end):
            raise conflict("Lunch breaks must fall within the punch-in and punch-out.", "BREAKS_OUTSIDE_SESSION")
    for earlier, later in zip(ordered, ordered[1:]):
        if later.started_at < earlier.ended_at:
            raise conflict("Lunch breaks cannot overlap.", "BREAKS_OVERLAP")


LOCAL_TIME_FIELDS = ("check_in_local", "check_out_local", "lunch_start_local", "lunch_end_local")


def work_zone(db: Session, employee: Employee, day: date):
    """The employee's work time zone on ``day``: the primary placement's branch, then its legal entity, then the
    company (LH-M1 adds employee_assignments.work_timezone in front)."""
    from app.models.org import Branch, EmployeeAssignment, LegalEntity

    placement = (
        db.query(EmployeeAssignment)
        .filter(EmployeeAssignment.company_id == employee.company_id, EmployeeAssignment.employee_id == employee.id,
                EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= day)
        .order_by(EmployeeAssignment.effective_from.desc(), EmployeeAssignment.id.desc())
        .first()
    )
    zones = []
    if placement is not None:
        branch = db.get(Branch, placement.branch_id) if placement.branch_id else None
        entity = db.get(LegalEntity, placement.legal_entity_id) if placement.legal_entity_id else None
        zones = [getattr(branch, "timezone", None), getattr(entity, "timezone", None)]
    company = db.get(Company, employee.company_id)
    for zone in [*zones, company.timezone if company else None, "UTC"]:
        if zone and zone in pytz.all_timezones_set:
            return pytz.timezone(zone)
    return pytz.utc


def _wall(tz, day: date, hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return tz.localize(datetime.combine(day, time(hour, minute)))


def _manual_times(db: Session, employee: Employee, data):
    """Instants of a manual entry: wall times converted in the work zone (BUG-AS-05), naive instants refused
    (BUG-AS-14), an absence without times recorded as a zero-length mark at the start of its day (BUG-AS-04)."""
    local = any(getattr(data, f, None) for f in LOCAL_TIME_FIELDS)
    if local and (data.check_in_time or data.check_out_time or data.breaks is not None):
        raise bad_request("Send either instants with an offset or wall times, not both.", "MIXED_TIME_FORMATS")
    instants = [data.check_in_time, data.check_out_time]
    for item in data.breaks or []:
        instants += [item.started_at, item.ended_at]
    if any(value is not None and value.tzinfo is None for value in instants):
        raise bad_request("Give every time with a timezone offset (for example +05:30).", "TIMEZONE_REQUIRED")
    update: dict = {}
    tz = work_zone(db, employee, data.date) if local or data.check_in_time is None else None
    if local:
        if not data.check_in_local:
            raise bad_request("Enter the check-in time.", "CHECK_IN_REQUIRED")
        check_in = _wall(tz, data.date, data.check_in_local)
        check_out = None
        if data.check_out_local:
            check_out = _wall(tz, data.date, data.check_out_local)
            if check_out <= check_in:  # ends the next day
                check_out = _wall(tz, data.date + timedelta(days=1), data.check_out_local)
        update.update(check_in_time=check_in, check_out_time=check_out)
        if data.lunch_start_local or data.lunch_end_local:
            if not (data.lunch_start_local and data.lunch_end_local):
                raise bad_request("Give both the lunch start and end, or neither.", "INVALID_BREAK")
            started = _wall(tz, data.date, data.lunch_start_local)
            if started < check_in:
                started = _wall(tz, data.date + timedelta(days=1), data.lunch_start_local)
            ended = _wall(tz, started.date(), data.lunch_end_local)
            if ended <= started:
                ended = _wall(tz, started.date() + timedelta(days=1), data.lunch_end_local)
            from app.schemas.admin import BreakWrite

            update["breaks"] = [BreakWrite(started_at=started, ended_at=ended)]
    elif data.check_in_time is None:
        if data.status != "absent":
            raise bad_request("Enter the check-in time.", "CHECK_IN_REQUIRED")
        start = _wall(tz, data.date, "00:00")
        update.update(check_in_time=start, check_out_time=start)
    return data.model_copy(update=update) if update else data


def record_manual(db: Session, principal: Principal, data) -> Attendance:
    # Checked before the scope lookup, which would otherwise answer a manager's attempt on
    # their own attendance with a misleading 404. It only compares ids, so it reveals nothing.
    if authz.is_self(principal, data.employee_id):
        raise forbidden("You cannot correct your own attendance.")
    employee = authz.ensure_employee_access(db, principal, "attendance:regularize", data.employee_id)
    data = _manual_times(db, employee, data)
    if data.check_out_time and data.check_out_time < data.check_in_time:
        raise bad_request("Check-out must be after check-in.")
    company = _company(db, principal)
    policy = attendance_policy.load(db, company.id)
    now = _now()
    lock_employee(db, employee.id)
    same_day = _open_record(db, employee.id, data.date)
    if data.attendance_id:
        record = db.query(Attendance).filter(
            Attendance.id == data.attendance_id, Attendance.employee_id == employee.id
        ).first()
        if record is None:
            raise not_found("Attendance record")
        if same_day is not None and same_day.id != record.id:
            raise conflict(f"{employee.full_name} already has attendance on {data.date}.", "ATTENDANCE_EXISTS")
    else:
        # One record per person per day: recording a day that already has one corrects it.
        record = same_day
    existing_breaks = breaks_by_session(db, [record.id])[record.id] if record is not None else []
    previous = _summary(record, company) if record is not None else None
    before = _snapshot(record, existing_breaks) if record is not None else None
    if record is not None and record.check_out_time is not None and data.check_out_time is None:
        raise conflict("A recorded punch-out can be corrected but not removed.", "CANNOT_REOPEN")

    if data.breaks is not None:
        _validate_breaks(data.breaks, data.check_in_time, data.check_out_time or now)
        for old in existing_breaks:
            db.delete(old)  # the original lunch events stay in the append-only log
        db.flush()
        existing_breaks = []
    else:
        for b in existing_breaks:
            end = b.ended_at or data.check_out_time
            if (b.started_at < data.check_in_time or (data.check_out_time and end and end > data.check_out_time)
                    or (data.check_out_time and b.started_at > data.check_out_time)):
                raise conflict("Lunch breaks fall outside the corrected times. Correct the breaks too.",
                               "BREAKS_OUTSIDE_SESSION")
        for b in existing_breaks:
            if b.ended_at is None and data.check_out_time is not None:
                b.ended_at = data.check_out_time
                b.end_reason = "CORRECTED"
        db.flush()

    was_live = record is not None and is_live(record)
    created = record is None
    if record is None:
        record = Attendance(company_id=principal.company_id, employee_id=employee.id, date=data.date,
                            shift_id=employee.shift_id)
    record.date = data.date
    record.check_in_time = data.check_in_time
    record.check_out_time = data.check_out_time
    record.status = data.status
    record.notes = data.notes
    record.is_manual = True
    record.source = "manual"
    record.capture_source = "MANUAL"
    record.recorded_by_user_id = principal.user_id
    if data.check_out_time is not None:
        record.closed_at, record.closed_reason = None, None
    elif record.closed_at is None and data.date < local_today(company, now):
        # A past day without a punch-out is recorded as such, not left running.
        record.closed_at, record.closed_reason = now, "MANUAL_NO_PUNCH_OUT"
    if was_live and not is_live(record):
        settle_until = min(now, data.check_out_time) if data.check_out_time else now
        location_service.stop_tracking(db, record, policy, settle_until, "CORRECTED")
    record.occupied_until = attendance_sessions.occupied_until(record, policy.max_shift_hours)
    if record.id is None:
        _insert_day(db, record)
    else:
        from app.core import db_errors

        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError as exc:
            error = db_errors.map_integrity(exc)
            if error is not None and error.code == "SESSION_OVERLAP":
                raise error
            raise conflict(f"{employee.full_name} already has a shift in progress.", "ALREADY_CHECKED_IN")
    for item in data.breaks or []:
        db.add(AttendanceBreak(
            company_id=record.company_id, attendance_id=record.id, employee_id=employee.id, kind="LUNCH",
            started_at=item.started_at, ended_at=item.ended_at,
            is_paid=policy.lunch_is_paid if item.is_paid is None else item.is_paid,
            end_reason="CORRECTED", created_by_user_id=principal.user_id,
        ))
    db.flush()
    if is_live(record) and record.tracking_state == location_service.PAUSED and _open_break(db, record.id) is None:
        location_service.resume_tracking(record, policy, now)  # the lunch it was paused for no longer exists
    apply_timing(db, company, record)
    _manual_punches(db, principal, record, data)
    record_version(db, record, "CREATED" if created else "UPDATED", principal.user_id, bump=not created)
    after = _snapshot(record, breaks_by_session(db, [record.id])[record.id])
    _event(db, record, "CORRECTION", now, "manual", principal.user_id,
           note=(data.notes or "Manual attendance entry")[:500], details={"before": before, "after": after})
    description = f"Recorded attendance for {employee.full_name} on {data.date}"
    if previous:
        # The audit trail keeps what the correction replaced.
        description = f"Corrected attendance for {employee.full_name} on {data.date} (was {previous})"
    AuditLogService.log(db, principal, "Attendance", "REGULARIZE", description, "attendance", record.id)
    return record


def _manual_punches(db: Session, principal: Principal, record: Attendance, data) -> None:
    """MANUAL raw punches of an HR entry (v1 shadow input, 01 S-3); the same times again collapse (P-5a)."""
    if record.status == "absent":
        return
    reason = (data.notes or "Manual attendance entry")[:500]
    for direction, at in (("IN", record.check_in_time), ("OUT", record.check_out_time)):
        if at is None:
            continue
        attendance_sessions.record_punch(
            db, company_id=record.company_id, employee_id=record.employee_id, source="MANUAL", direction=direction,
            occurred_at=at, session=record, shift_date=record.date, actor_user_id=principal.user_id or None,
            reason=reason,
        )


def _summary(record: Attendance, company: Company) -> str:
    tz = _tz(company)

    def at(moment: Optional[datetime]) -> str:
        return moment.astimezone(tz).strftime("%H:%M") if moment else "-"

    return f"{record.status}, in {at(record.check_in_time)}, out {at(record.check_out_time)}"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def _break_payload(b: AttendanceBreak) -> dict:
    return {"id": b.id, "kind": b.kind, "started_at": b.started_at.isoformat(),
            "ended_at": b.ended_at.isoformat() if b.ended_at else None, "is_paid": b.is_paid, "end_reason": b.end_reason}


def serialize(record: Attendance, employee: Optional[Employee] = None, *, breaks: Optional[List[AttendanceBreak]] = None,
              now: Optional[datetime] = None, location: Optional[dict] = None) -> dict:
    breaks = breaks or []
    now = now or _now()
    hours = hours_for(record, breaks, now)
    return {
        "id": record.id,
        "employee_id": record.employee_id,
        "employee_name": employee.full_name if employee else None,
        "date": record.date.isoformat(),
        "check_in_time": record.check_in_time.isoformat() if record.check_in_time else None,
        "check_out_time": record.check_out_time.isoformat() if record.check_out_time else None,
        # Net working time (attendance minus unpaid breaks); 0 while a punch-out is missing.
        "working_seconds": hours.net_seconds or 0,
        "status": record.status,
        "source": record.source,
        "is_manual": record.is_manual,
        # E3 evidence (07 §8): numeric scores only through the audited reveal (attendance:evidence:read + step-up);
        # everyone else sees whether a face match ran (E1).
        "face_match_score": None,
        "face_verified": record.face_match_score is not None,
        "notes": record.notes,
        "attendance_state": session_state(record, breaks),
        "closed_reason": record.closed_reason,
        "late_minutes": record.late_minutes,
        "early_exit": bool(record.early_exit),
        "early_exit_minutes": record.early_exit_minutes,
        "row_version": record.row_version,
        "hours": hours.as_dict(),
        "breaks": [_break_payload(b) for b in breaks],
        "tracking_state": record.tracking_state,
        "location": location,
    }


def serialize_many(db: Session, rows, now: Optional[datetime] = None) -> List[dict]:
    """Serialize (record, employee[, ...]) rows with two batched queries, not one per row."""
    now = now or _now()
    ids = [row[0].id for row in rows]
    breaks = breaks_by_session(db, ids)
    locations = location_service.summaries(db, ids)
    return [serialize(row[0], row[1], breaks=breaks[row[0].id], now=now, location=locations[row[0].id]) for row in rows]


def _current_record(db: Session, employee: Employee, company: Company, policy: AttendancePolicy, now: datetime):
    live = live_session(db, employee.id)
    if live is not None and now >= live.check_in_time + policy.max_shift:
        lock_employee(db, employee.id)
        live = live_session(db, employee.id, lock=True)
        if live is not None and expire_if_needed(db, live, policy, now):
            live = None
    return live or _record_for_day(db, employee.id, local_today(company, now))


def _actions(state: str, breaks: List[AttendanceBreak], policy: AttendancePolicy) -> List[str]:
    if state == NOT_PUNCHED_IN:
        return ["PUNCH_IN"]
    if state == WORKING:
        return (["LUNCH_IN"] if policy.allow_multiple_lunches or not breaks else []) + ["PUNCH_OUT"]
    if state == ON_LUNCH:
        return ["LUNCH_OUT"] + (["PUNCH_OUT"] if policy.lunch_open_at_punch_out == "CONFIRM_AUTO_CLOSE" else [])
    return []


def my_today(db: Session, principal: Principal) -> dict:
    employee = authz.own_employee(db, principal)
    company = _company(db, principal)
    policy = attendance_policy.load(db, company.id)
    now = _now()
    record = _current_record(db, employee, company, policy, now)
    # The browser schedules location checks from this policy: the employee's capture policy (interval, grace, pause)
    # of the live session's shift date, else of today, over the company settings.
    from app.services import attendance_capture_policy, org_structure_service

    if record is not None and is_live(record):
        policy = attendance_capture_policy.for_session(db, record)
    else:
        policy = attendance_capture_policy.as_attendance_policy(
            attendance_capture_policy.effective(db, company.id, employee.id,
                                                org_structure_service.employee_local_today(db, employee)), policy)
    breaks = breaks_by_session(db, [record.id])[record.id] if record is not None else []
    state = session_state(record, breaks)
    events = []
    if record is not None:
        events = [
            {"id": e.id, "event_type": e.event_type, "occurred_at": e.occurred_at.isoformat(), "source": e.source,
             "location_status": e.location_status, "note": e.note}
            for e in db.query(AttendanceEvent)
            .filter(AttendanceEvent.attendance_id == record.id)
            .order_by(AttendanceEvent.occurred_at, AttendanceEvent.id)
        ]
    tracking = location_service.status_payload(db, record if record is not None and is_live(record) else None, policy, now)
    last = None
    if record is None:
        # A shift that started on an earlier day (overnight) and ended recently.
        recent = (
            db.query(Attendance)
            .filter(Attendance.employee_id == employee.id, Attendance.check_out_time >= now - timedelta(hours=16))
            .order_by(Attendance.check_out_time.desc())
            .first()
        )
        if recent is not None:
            last = serialize(recent, employee, breaks=breaks_by_session(db, [recent.id])[recent.id], now=now)
    return {
        "state": LEGACY_STATE[state],
        "attendance_state": state,
        "record": serialize(record, employee, breaks=breaks, now=now) if record is not None else None,
        "last_session": last,
        "events": events,
        "actions": _actions(state, breaks, policy),
        "overnight": bool(record is not None and is_live(record) and record.date != local_today(company, now)),
        "tracking": tracking,
        "policy": policy.client_view(),
        "server_now": now.isoformat(),
    }


def session_detail(db: Session, principal: Principal, attendance_id: int, include_locations: bool = False) -> dict:
    record = db.query(Attendance).filter(
        Attendance.company_id == principal.company_id, Attendance.id == attendance_id
    ).first()
    if record is None:
        raise not_found("Attendance record")
    if not authz.can_access_employee(db, principal, "attendance:read", record.employee_id):
        raise not_found("Attendance record")
    employee = db.get(Employee, record.employee_id)
    now = _now()
    policy = attendance_policy.load(db, principal.company_id)
    breaks = breaks_by_session(db, [record.id])[record.id]
    payload = serialize(record, employee, breaks=breaks, now=now,
                        location=location_service.summaries(db, [record.id])[record.id])
    payload["events"] = [
        {"id": e.id, "event_type": e.event_type, "occurred_at": e.occurred_at.isoformat(), "source": e.source,
         "location_status": e.location_status, "note": e.note, "face_verified": e.face_match_score is not None}
        for e in db.query(AttendanceEvent)
        .filter(AttendanceEvent.attendance_id == record.id)
        .order_by(AttendanceEvent.occurred_at, AttendanceEvent.id)
    ]
    # Precise location needs its own grant; attendance access alone shows only counts and statuses.
    can_locate = authz.can_access_employee(db, principal, "location:read", record.employee_id)
    payload["location_access"] = can_locate
    payload["samples"] = (
        location_service.session_samples(db, principal, record, employee, coordinates=include_locations)
        if can_locate else None
    )
    tracking = location_service.status_payload(db, record if is_live(record) else None, policy, now)
    # Without location access only the schedule is shown: no distances, accuracy or exception text.
    payload["tracking"] = tracking if can_locate else location_service.without_location_details(tracking)
    return payload


def list_records(
    db: Session,
    principal: Principal,
    start: date,
    end: date,
    employee_id: Optional[int] = None,
    department_id: Optional[int] = None,
    skip: int = 0,
    limit: int = 100,
):
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days > 366:
        raise bad_request("Choose a range of at most one year.")
    scope = authz.employee_scope(db, principal, "attendance:read")
    query = (
        db.query(Attendance, Employee)
        .join(Employee, Employee.id == Attendance.employee_id)
        .filter(
            Attendance.company_id == principal.company_id,
            Attendance.date >= start,
            Attendance.date <= end,
            Attendance.voided_at.is_(None),  # spine §15.4: deleted (voided) sessions are kept for audit only
            scope.clause(Attendance.employee_id),
        )
    )
    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    total = query.order_by(None).count()
    rows = query.order_by(Attendance.date.desc(), Attendance.check_in_time.desc()).offset(skip).limit(limit).all()
    return total, serialize_many(db, rows)


def hours_summary(db: Session, principal: Principal, start: date, end: date, employee_id: Optional[int] = None,
                  department_id: Optional[int] = None) -> dict:
    """Per-employee gross / lunch / net totals from the canonical calculator."""
    if end < start or (end - start).days > 366:
        raise bad_request("Choose a range of at most one year.")
    scope = authz.employee_scope(db, principal, "attendance:read")
    query = (
        db.query(Attendance, Employee)
        .join(Employee, Employee.id == Attendance.employee_id)
        .filter(Attendance.company_id == principal.company_id, Attendance.date >= start, Attendance.date <= end,
                scope.clause(Attendance.employee_id))
    )
    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)
    if department_id:
        query = query.filter(Employee.department_id == department_id)
    rows = query.order_by(Employee.first_name, Attendance.date).limit(20000).all()
    now = _now()
    breaks = breaks_by_session(db, [a.id for a, _ in rows])
    per_employee: Dict[int, dict] = {}
    results = {}
    for record, employee in rows:
        hours = hours_for(record, breaks[record.id], now)
        results.setdefault(employee.id, (employee, []))[1].append(hours)
    for emp_id, (employee, items) in results.items():
        per_employee[emp_id] = {"employee_id": emp_id, "employee_name": employee.full_name,
                                "employee_code": employee.employee_code, **attendance_hours.total(items)}
    all_hours = [h for _, items in results.values() for h in items]
    return {"start_date": start.isoformat(), "end_date": end.isoformat(),
            "employees": list(per_employee.values()), "totals": attendance_hours.total(all_hours)}


def month_hours(db: Session, employee_id: int, first: date, last: date, include_provisional: bool = True) -> dict:
    """Canonical hours of one employee for a month (payroll counts finished shifts only)."""
    records = db.query(Attendance).filter(
        Attendance.employee_id == employee_id, Attendance.date >= first, Attendance.date <= last
    ).all()
    now = _now()
    breaks = breaks_by_session(db, [r.id for r in records])
    return attendance_hours.total((hours_for(r, breaks[r.id], now) for r in records), include_provisional)


def _units(value) -> float:
    number = float(value)
    return int(number) if number == int(number) else number


def _expectations(db: Session, company_id: int, employee_ids, start: date, end: date) -> dict:
    """``{(employee_id, date): Expectation}`` from the day resolver (LH 05 §4.2, BUG-12)."""
    from app.payroll.services import time_inputs

    if not employee_ids:
        return {}
    # Dashboards read portions only: the light resolver skips shift minutes and the calendar version.
    by_employee = time_inputs.expected_presence(db, company_id, sorted(employee_ids), start, end, light=True)
    return {(employee_id, e.date): e for employee_id, items in by_employee.items() for e in items}


def _day_measures(expectations: dict, ids, day: date, present_ids: set) -> dict:
    """Portion-weighted board figures of one date (LH 05 §4.9; AS K-AS-02 / K-AS-03)."""
    from app.payroll.services.time_inputs import portion_units

    expected = present_units = on_leave = on_duty = 0
    present_people = off = worked_off = expected_people = 0
    for employee_id in ids:
        e = expectations.get((employee_id, day))
        if e is None or "NOT_EMPLOYED" in e.reasons:
            continue
        units = portion_units(e.expected_portion)
        expected += units
        on_leave += portion_units(e.on_leave_portion)
        if e.working_portion == "NONE":
            off += 1
        if units:
            expected_people += 1
        if employee_id in present_ids:
            if units:
                present_people += 1
                present_units += units
            else:
                worked_off += 1  # worked on approved leave or a non-working day: footnote, not "present"
        elif units:
            on_duty += portion_units(e.on_duty_portion)
    absent = max(expected - present_units - on_duty, 0)
    return {"expected": expected, "present_people": present_people, "present_units": present_units,
            "on_leave": on_leave, "on_duty": on_duty, "off": off, "absent": absent,
            "expected_people": expected_people, "worked_non_working_day": worked_off}


def today_summary(db: Session, principal: Principal) -> dict:
    """Live board for the caller's attendance scope (company, team or self)."""
    scope = authz.employee_scope(db, principal, "attendance:read")
    company = _company(db, principal)
    now = _now()
    today = local_today(company, now)
    employees_q = db.query(Employee).filter(
        Employee.company_id == principal.company_id, Employee.is_active.is_(True), scope.clause(Employee.id)
    )
    total = employees_q.count()
    rows = (
        db.query(Attendance, Employee, Department.name)
        .join(Employee, Employee.id == Attendance.employee_id)
        .outerjoin(Department, Department.id == Employee.department_id)
        .filter(
            Attendance.company_id == principal.company_id,
            Attendance.date == today,
            Employee.is_active.is_(True),
            scope.clause(Employee.id),
        )
        .order_by(Attendance.check_in_time.desc())
        .all()
    )
    serialized = serialize_many(db, rows, now)
    present_ids = {a.employee_id for a, _, _ in rows}
    on_lunch = sum(1 for item in serialized if item["attendance_state"] == ON_LUNCH)
    working = sum(1 for item in serialized if item["attendance_state"] == WORKING)
    late = sum(1 for a, _, _ in rows if a.status == "late")
    # BUG-12 (LH 05 §4.9): expected, absent and on-leave come from the day resolver, portion-weighted; people off
    # today (weekly off, holiday) are neither expected nor absent.
    ids = [e.id for e in employees_q.with_entities(Employee.id)]
    m = _day_measures(_expectations(db, principal.company_id, ids, today, today), ids, today, present_ids)
    return {
        "date": today.isoformat(),
        "total_employees": total,
        "expected_today": _units(m["expected"]),
        "present_today": m["present_people"],
        "present_units": _units(m["present_units"]),
        "absent_today": _units(m["absent"]),
        "on_leave": _units(m["on_leave"]),
        "on_duty_today": _units(m["on_duty"]),
        "off_today": m["off"],
        "worked_non_working_day": m["worked_non_working_day"],
        "working_now": working,
        "on_lunch_now": on_lunch,
        "completed_today": len(present_ids) - working - on_lunch,
        "late_today": late,
        "attendance_percentage": round(m["present_people"] / m["expected_people"] * 100, 1)
        if m["expected_people"] else 0,
        "employees": [
            {**item, "department": dept or "-"} for item, (_, _, dept) in list(zip(serialized, rows))[:200]
        ],
    }


def trend(db: Session, principal: Principal, days: int = 7) -> List[dict]:
    scope = authz.employee_scope(db, principal, "attendance:read")
    company = _company(db, principal)
    today = local_today(company)
    start = today - timedelta(days=days - 1)
    ids = [i for (i,) in db.query(Employee.id).filter(
        Employee.company_id == principal.company_id, Employee.is_active.is_(True), scope.clause(Employee.id))]
    present_by_day: Dict[date, set] = defaultdict(set)
    for day, employee_id in db.query(Attendance.date, Attendance.employee_id).filter(
            Attendance.company_id == principal.company_id, Attendance.date >= start, Attendance.date <= today,
            scope.clause(Attendance.employee_id)):
        present_by_day[day].add(employee_id)
    expectations = _expectations(db, principal.company_id, ids, start, today)
    result = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        # BUG-12: absent = expected − present − on-duty per date (weekly offs and holidays are not absences).
        m = _day_measures(expectations, ids, day, present_by_day.get(day, set()))
        result.append({"date": day.isoformat(), "label": day.strftime("%d %b"), "present": m["present_people"],
                       "absent": _units(m["absent"]), "expected": _units(m["expected"]),
                       "off": m["off"]})
    return result


def monthly_stats(db: Session, principal: Principal, employee_id: int, year: int, month: int) -> dict:
    employee = authz.ensure_employee_access(db, principal, "attendance:read", employee_id)
    first = date(year, month, 1)
    last = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    rows = db.query(Attendance.status, func.count(Attendance.id)).filter(
        Attendance.employee_id == employee.id, Attendance.date >= first, Attendance.date <= last
    ).group_by(Attendance.status).all()
    counts = {s: c for s, c in rows}
    # BUG-12: what the month expected of the employee (portion units) and what stayed unaccounted for.
    worked = {d for (d,) in db.query(Attendance.date).filter(
        Attendance.employee_id == employee.id, Attendance.date >= first, Attendance.date <= last)}
    expectations = _expectations(db, employee.company_id, [employee.id], first, last)
    from app.payroll.services.time_inputs import portion_units

    expected = unaccounted = 0
    off = 0
    for (_, day), e in expectations.items():
        units = portion_units(e.expected_portion)
        expected += units
        if e.working_portion == "NONE" and "NOT_EMPLOYED" not in e.reasons:
            off += 1
        if units and day not in worked:
            unaccounted += max(units - portion_units(e.on_duty_portion), 0)
    return {
        "employee_id": employee.id,
        "year": year,
        "month": month,
        "present": counts.get("present", 0),
        "late": counts.get("late", 0),
        "half_day": counts.get("half-day", 0),
        "absent": counts.get("absent", 0),
        "hours": month_hours(db, employee.id, first, last),
        "expected_days": _units(expected),
        "off_days": off,
        "unaccounted_days": _units(unaccounted),
    }


def recent_activity(db: Session, principal: Principal, limit: int = 10) -> List[dict]:
    scope = authz.employee_scope(db, principal, "attendance:read")
    rows = (
        db.query(Attendance, Employee)
        .join(Employee, Employee.id == Attendance.employee_id)
        .filter(Attendance.company_id == principal.company_id, scope.clause(Attendance.employee_id))
        .order_by(Attendance.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "employee_name": e.full_name,
            "type": "checkout" if a.check_out_time else "checkin",
            "time": (a.check_out_time or a.check_in_time).isoformat(),
            "message": f"{e.full_name} {'checked out' if a.check_out_time else 'checked in'}",
        }
        for a, e in rows
    ]


# ---------------------------------------------------------------------------
# Face enrolment
# ---------------------------------------------------------------------------
def enroll_face(db: Session, principal: Principal, employee_id: int, image: bytes, ext: str, make_primary: bool = True) -> dict:
    employee = authz.ensure_employee_access(db, principal, "face:enroll", employee_id)
    from datetime import timedelta as _days

    from app.services import attendance_consents, attendance_faces, org_structure_service

    # Consent gate on enrolment (BUG-AS-19, 07 §5.1, T-09): a GRANTED BIOMETRIC_FACE consent, or under the LEGACY_V1
    # capture policy a legacy enrolment with the grace clock (07 §5.5; zero diff for v1 tenants).
    today = org_structure_service.employee_local_today(db, employee)
    basis = attendance_consents.can_enrol(db, principal.company_id, employee.id, today)
    if basis is None:
        raise AppError(422, "Record the employee's face consent before enrolling a face.", "CONSENT_REQUIRED",
                       extra={"details": {"purpose": "BIOMETRIC_FACE"}})
    encoding = _encode_or_400(image)
    from app.core import storage

    if make_primary:
        db.query(FaceEncoding).filter(FaceEncoding.employee_id == employee.id).update(
            {FaceEncoding.is_primary: False}, synchronize_session=False
        )
    path = storage.save_bytes(principal.company_id, "faces", image, ext)
    consent = attendance_consents.state_on(db, principal.company_id, employee.id, "BIOMETRIC_FACE", today)
    legacy = basis == "LEGACY"
    record = attendance_faces.new_row(
        db, principal.company_id, employee.id, encoding, image_path=path, is_primary=make_primary,
        consent_id=consent.id if basis == "CONSENT" else None, legacy_enrolled=legacy,
        legacy_grace_until=today + _days(days=attendance_consents.LEGACY_FACE_GRACE_DAYS) if legacy else None,
    )
    db.add(record)
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "FACE_ENROLLED", f"Enrolled face for {employee.full_name}", "employee", employee.id)
    return {"id": record.id, "employee_id": employee.id, "is_primary": record.is_primary}


def face_status(db: Session, principal: Principal, employee_id: int) -> dict:
    employee = authz.ensure_employee_access(db, principal, "employee:read", employee_id)
    rows = db.query(FaceEncoding).filter(FaceEncoding.employee_id == employee.id,
                                         FaceEncoding.deleted_at.is_(None)).all()
    return {
        "employee_id": employee.id,
        "total_encodings": len(rows),
        "primary_encoding": any(r.is_primary for r in rows),
        "engine_available": face_engine.available(),
    }


def remove_faces(db: Session, principal: Principal, employee_id: int) -> int:
    employee = authz.ensure_employee_access(db, principal, "face:enroll", employee_id)
    from app.core import storage

    rows = db.query(FaceEncoding).filter(FaceEncoding.employee_id == employee.id).all()
    for row in rows:
        storage.delete(principal.company_id, row.image_path)
        db.delete(row)
    AuditLogService.log(db, principal, "Attendance", "FACE_REMOVED", f"Removed face data for {employee.full_name}", "employee", employee.id)
    return len(rows)
