"""Location evidence for attendance events and best-effort periodic tracking.

Browsers report an *estimated* position with a 95% accuracy radius. Nothing
here treats it as proof of presence, invents a position for a failed attempt
or back-fills a reading the browser never sent. Every attempt is stored with
its honest outcome: a reading (with accuracy and both clocks), a browser
failure (permission denied, timeout, unavailable, insecure context), or a
MISSED scheduled slot detected by the server.

Periodic schedule (server-authoritative):
  * tracking starts at a self-service punch-in when the company enables it;
  * the next reading is due one interval after the latest anchor: the
    punch-in, the end of lunch, or the latest captured reading (event
    readings count too, so an event fix never duplicates a periodic one);
  * a slot with no reading by due + grace is recorded as MISSED and the
    schedule moves on; after a miss the session is OVERDUE and the browser
    should take one current reading as soon as it can (a "recovery" reading),
    which re-anchors the schedule - missed slots are never back-filled;
  * tracking pauses on lunch (by default), resumes one interval after lunch
    ends, and stops at punch-out, expiry, or when the company disables it;
    readings for a closed or paused session are refused.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from fastapi import status as http_status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.attendance import Attendance, AttendanceBreak, AttendanceEvent, LocationSample
from app.models.employee import Employee
from app.schemas.attendance import LocationAttempt, ReviewDecision, SampleSubmission
from app.services import attendance_policy, attendance_review, geo  # noqa: F401 (review mirror listeners)
from app.services.attendance_policy import AttendancePolicy
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

CAPTURED, MISSED = "CAPTURED", "MISSED"
ACTIVE, PAUSED, STOPPED, NOT_APPLICABLE = "ACTIVE", "PAUSED", "STOPPED", "NOT_APPLICABLE"
SCHEDULED, DUE, OVERDUE = "SCHEDULED", "DUE", "OVERDUE"

FUTURE_TOLERANCE = timedelta(seconds=5)
MAX_CLOCK_SKEW = timedelta(hours=24)
EARLY_TOLERANCE = timedelta(minutes=2)
MAX_SLOTS_PER_RECONCILE = 2000

FAILURE_MESSAGES = {
    "PERMISSION_DENIED": "location permission was denied",
    "POSITION_UNAVAILABLE": "the device could not determine a position",
    "TIMEOUT": "no position fix arrived in time",
    "INSECURE_CONTEXT": "the page was not opened over HTTPS",
    "UNSUPPORTED": "the browser does not support location",
    "ERROR": "the location attempt failed",
}


def unprocessable(detail: str, code: str) -> AppError:
    return AppError(http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail, code)


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reading:
    latitude: float
    longitude: float
    accuracy_m: float
    captured_at: datetime
    fix_age_seconds: int
    clock_skew_seconds: int
    accuracy_class: str
    attempts: Optional[int]


def validate_reading(attempt: LocationAttempt, policy: AttendancePolicy, received_at: datetime,
                     client_sent_at: Optional[datetime]) -> Reading:
    """Check a captured reading for freshness and clock sanity (ranges are checked by the schema).

    The fix age is measured on the device's own clock (sent - captured), so a
    device whose clock is wrong but whose fix is fresh is accepted with its skew
    recorded, while a cached or replayed fix is refused.
    """
    if client_sent_at is not None and client_sent_at.tzinfo is None:
        raise unprocessable("client_sent_at must include a timezone.", "LOCATION_INVALID")
    skew = (received_at - client_sent_at) if client_sent_at else timedelta(0)
    if abs(skew) > MAX_CLOCK_SKEW:
        raise unprocessable("Your device clock is more than a day off. Fix the date and time, then try again.",
                            "LOCATION_INVALID")
    reference = client_sent_at or received_at
    age = reference - attempt.captured_at
    if age < -FUTURE_TOLERANCE:
        raise unprocessable("The location reading is timestamped in the future.", "LOCATION_INVALID")
    if age > timedelta(seconds=policy.max_fix_age_seconds):
        raise unprocessable("The location reading is too old. Take a fresh reading and try again.", "LOCATION_STALE")
    return Reading(
        latitude=attempt.latitude,
        longitude=attempt.longitude,
        accuracy_m=attempt.accuracy_m,
        captured_at=attempt.captured_at,
        fix_age_seconds=max(int(age.total_seconds()), 0),
        clock_skew_seconds=int(skew.total_seconds()),
        accuracy_class=geo.accuracy_class(attempt.accuracy_m, policy.preferred_accuracy_m, policy.acceptable_accuracy_m),
        attempts=attempt.attempts,
    )


@dataclass
class EventLocation:
    event_status: str  # attendance_events.location_status
    sample_status: Optional[str] = None  # None: nothing is stored
    reading: Optional[Reading] = None
    attempt: Optional[LocationAttempt] = None
    exception_reason: Optional[str] = None


def evaluate_event(policy: AttendancePolicy, event_type: str, attempt: Optional[LocationAttempt],
                   exception_reason: Optional[str], received_at: datetime, client_sent_at: Optional[datetime],
                   source: str) -> EventLocation:
    """Apply the company's location policy to a punch or lunch event. Raises 422 when it must be refused."""
    if source != "self":
        return EventLocation("NOT_APPLICABLE")  # kiosks and HR corrections are not the employee's device
    mode = policy.location_mode(event_type)
    if mode == "OFF":
        return EventLocation("NOT_REQUIRED")  # nothing is collected, even if the browser sent a reading
    required = mode == "REQUIRED"
    exceptions_allowed = policy.location_exception == "ALLOW_WITH_REASON"

    def refuse(detail: str, code: str = "LOCATION_REQUIRED"):
        if exceptions_allowed:
            detail += " You can try again, or continue with a reason for your manager to review."
        else:
            detail += " Ask HR to record this attendance for you."
        raise unprocessable(detail, code)

    if attempt is not None and attempt.status == CAPTURED:
        reading = validate_reading(attempt, policy, received_at, client_sent_at)
        if required and policy.reject_low_accuracy and reading.accuracy_class == geo.LOW:
            if exception_reason and exceptions_allowed:
                return EventLocation("EXCEPTION", CAPTURED, reading, attempt, exception_reason)
            refuse(
                f"Your location is only accurate to about {round(reading.accuracy_m)} m; "
                f"this company needs {policy.acceptable_accuracy_m} m or better.",
                "LOCATION_LOW_ACCURACY",
            )
        return EventLocation("CAPTURED", CAPTURED, reading, attempt)

    failure = attempt.status if attempt is not None else None
    if not required:
        if attempt is None:
            return EventLocation("NOT_PROVIDED")
        return EventLocation("FAILED", failure, None, attempt)
    if exception_reason and exceptions_allowed:
        return EventLocation("EXCEPTION", failure or "ERROR", None, attempt, exception_reason)
    reason = FAILURE_MESSAGES.get(failure or "", "no location reading was sent")
    refuse(f"A location check is required for this action, but {reason}.")


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------
def _captured(db: Session, attendance_id: int):
    return db.query(LocationSample).filter(
        LocationSample.attendance_id == attendance_id,
        LocationSample.status == CAPTURED,
        LocationSample.redacted_at.is_(None),
    )


def _classify(db: Session, att: Attendance, reading: Reading, policy: AttendancePolicy) -> dict:
    """Straight-line displacement from the punch-in fix (or the shift's first fix) and the previous fix."""
    reference = (
        _captured(db, att.id).filter(LocationSample.sample_type == "PUNCH_IN").first()
        or _captured(db, att.id).order_by(LocationSample.received_at, LocationSample.id).first()
    )
    previous = _captured(db, att.id).order_by(LocationSample.received_at.desc(), LocationSample.id.desc()).first()
    if reference is None:
        return {"movement_status": geo.BASELINE}
    d_ref = geo.distance_m(reference.latitude, reference.longitude, reading.latitude, reading.longitude)
    d_prev = geo.distance_m(previous.latitude, previous.longitude, reading.latitude, reading.longitude)
    movement = geo.classify_movement(d_ref, reading.accuracy_m, reference.accuracy_m, policy.movement_threshold_m,
                                     policy.acceptable_accuracy_m)
    return {
        "movement_status": movement,
        "distance_from_punch_in_m": d_ref,
        "distance_from_previous_m": d_prev,
        "uncertainty_m": round(reading.accuracy_m + reference.accuracy_m, 2),
    }


def _new_sample(db: Session, att: Attendance, sample_type: str, status: str, now: datetime, policy: AttendancePolicy,
                *, reading: Optional[Reading] = None, attempt: Optional[LocationAttempt] = None,
                client_sent_at: Optional[datetime] = None, **fields) -> LocationSample:
    sample = LocationSample(
        company_id=att.company_id, attendance_id=att.id, employee_id=att.employee_id,
        sample_type=sample_type, status=status, received_at=now, client_sent_at=client_sent_at, **fields,
    )
    if attempt is not None:
        sample.attempts = attempt.attempts
        if status != CAPTURED and attempt.error_message:
            sample.error_message = attempt.error_message[:200]
    if reading is not None:
        sample.latitude, sample.longitude, sample.accuracy_m = reading.latitude, reading.longitude, reading.accuracy_m
        sample.captured_at = reading.captured_at
        sample.fix_age_seconds = reading.fix_age_seconds
        sample.clock_skew_seconds = reading.clock_skew_seconds
        sample.accuracy_class = reading.accuracy_class
        for key, value in _classify(db, att, reading, policy).items():
            setattr(sample, key, value)
        if sample.movement_status == geo.SIGNIFICANT_MOVEMENT:
            sample.review_status = "PENDING"
        att.last_location_at = now
    db.add(sample)
    return sample


def record_event_sample(db: Session, att: Attendance, event: AttendanceEvent, location: EventLocation,
                        policy: AttendancePolicy, now: datetime, client_sent_at: Optional[datetime]) -> Optional[LocationSample]:
    if location.sample_status is None:
        return None
    sample = _new_sample(
        db, att, event.event_type, location.sample_status, now, policy,
        reading=location.reading, attempt=location.attempt, client_sent_at=client_sent_at, event_id=event.id,
    )
    if location.exception_reason:
        sample.exception_reason = location.exception_reason
        sample.review_status = "PENDING"
    if location.reading is not None and att.tracking_state == ACTIVE:
        reconcile(db, att, policy, now)  # slots that already passed stay MISSED; a reading never erases them
        _re_anchor(att, now)
    db.flush()
    return sample


# ---------------------------------------------------------------------------
# Tracking schedule
# ---------------------------------------------------------------------------
def _re_anchor(att: Attendance, now: datetime) -> None:
    att.tracking_anchor_at = now
    att.tracking_next_due_at = now + timedelta(minutes=att.tracking_interval_minutes)
    att.tracking_overdue_since = None


def start_tracking(att: Attendance, policy: AttendancePolicy, now: datetime) -> None:
    att.tracking_state = ACTIVE
    att.tracking_stop_reason = None
    att.tracking_interval_minutes = policy.tracking_interval_minutes
    att.tracking_consent_at = now
    _re_anchor(att, now)


def reconcile(db: Session, att: Attendance, policy: AttendancePolicy, now: datetime) -> int:
    """Record every slot whose grace period has passed without a reading as MISSED."""
    if att.tracking_state != ACTIVE or att.tracking_next_due_at is None:
        return 0
    interval = timedelta(minutes=att.tracking_interval_minutes or policy.tracking_interval_minutes)
    missed = 0
    while att.tracking_next_due_at + policy.grace < now and missed < MAX_SLOTS_PER_RECONCILE:
        db.add(LocationSample(
            company_id=att.company_id, attendance_id=att.id, employee_id=att.employee_id, sample_type="PERIODIC",
            status=MISSED, received_at=now, slot_due_at=att.tracking_next_due_at,
        ))
        att.tracking_overdue_since = att.tracking_overdue_since or att.tracking_next_due_at
        att.tracking_next_due_at = att.tracking_next_due_at + interval
        missed += 1
    if missed:
        db.flush()
    return missed


def pause_tracking(db: Session, att: Attendance, policy: AttendancePolicy, now: datetime) -> None:
    if att.tracking_state != ACTIVE:
        return
    reconcile(db, att, policy, now)
    att.tracking_state = PAUSED
    att.tracking_stop_reason = "ON_LUNCH"
    att.tracking_next_due_at = None
    att.tracking_overdue_since = None


def resume_tracking(att: Attendance, policy: AttendancePolicy, now: datetime) -> None:
    if att.tracking_state != PAUSED:
        return
    att.tracking_state = ACTIVE
    att.tracking_stop_reason = None
    att.tracking_interval_minutes = policy.tracking_interval_minutes
    _re_anchor(att, now)


def stop_tracking(db: Session, att: Attendance, policy: AttendancePolicy, now: datetime, reason: str,
                  settle: bool = True) -> None:
    """Stop collecting; with `settle`, slots that already passed are first recorded as MISSED."""
    if att.tracking_state not in (ACTIVE, PAUSED):
        return
    if settle:
        reconcile(db, att, policy, now)
    att.tracking_state = STOPPED
    att.tracking_stop_reason = reason
    att.tracking_next_due_at = None
    att.tracking_overdue_since = None


def schedule_state(att: Attendance, now: datetime) -> Optional[str]:
    if att.tracking_state != ACTIVE or att.tracking_next_due_at is None:
        return None
    if att.tracking_overdue_since is not None:
        return OVERDUE
    return DUE if now >= att.tracking_next_due_at else SCHEDULED


def _pending_misses(att: Attendance, policy: AttendancePolicy, now: datetime) -> int:
    """Slots already past grace that a read-only view has not recorded yet."""
    if att.tracking_state != ACTIVE or att.tracking_next_due_at is None:
        return 0
    late = now - (att.tracking_next_due_at + policy.grace)
    if late <= timedelta(0):
        return 0
    interval = timedelta(minutes=att.tracking_interval_minutes or policy.tracking_interval_minutes)
    return int(late / interval) + 1


def _sample_brief(sample: Optional[LocationSample], coordinates: bool = False) -> Optional[dict]:
    if sample is None:
        return None
    data = {
        "id": sample.id,
        "sample_type": sample.sample_type,
        "status": sample.status,
        "received_at": _iso(sample.received_at),
        "captured_at": _iso(sample.captured_at),
        "accuracy_m": sample.accuracy_m,
        "accuracy_class": sample.accuracy_class,
        "movement_status": sample.movement_status,
        "distance_from_punch_in_m": sample.distance_from_punch_in_m,
        "uncertainty_m": sample.uncertainty_m,
        "is_recovery": sample.is_recovery,
        "slot_due_at": _iso(sample.slot_due_at),
        "review_status": sample.review_status,
        "exception_reason": sample.exception_reason,
        "error_message": sample.error_message,
    }
    if coordinates:
        data.update({"latitude": sample.latitude, "longitude": sample.longitude, "redacted": sample.redacted_at is not None})
    return data


def without_location_details(status: dict) -> dict:
    """The schedule for viewers without location:read: when and whether, never where or why."""
    def brief(sample: Optional[dict]) -> Optional[dict]:
        return {k: sample[k] for k in ("id", "sample_type", "status", "received_at")} if sample else None

    return {**status, "last_capture": brief(status["last_capture"]), "last_attempt": brief(status["last_attempt"])}


def status_payload(db: Session, att: Optional[Attendance], policy: AttendancePolicy, now: datetime) -> dict:
    payload = {
        "attendance_id": None,
        "state": "NO_SESSION",
        "stop_reason": None,
        "schedule_state": None,
        "next_due_at": None,
        "grace_until": None,
        "overdue_since": None,
        "interval_minutes": policy.tracking_interval_minutes,
        "grace_minutes": policy.tracking_grace_minutes,
        "missed_count": 0,
        "pending_missed": 0,
        "last_capture": None,
        "last_attempt": None,
        "policy_version": policy.version,
        "capture": policy.client_view()["location"],
        "server_now": now.isoformat(),
    }
    if att is None:
        return payload
    last_capture = (
        db.query(LocationSample)
        .filter(LocationSample.attendance_id == att.id, LocationSample.status == CAPTURED)
        .order_by(LocationSample.received_at.desc(), LocationSample.id.desc())
        .first()
    )
    last_attempt = (
        db.query(LocationSample)
        .filter(LocationSample.attendance_id == att.id, LocationSample.status != MISSED)
        .order_by(LocationSample.received_at.desc(), LocationSample.id.desc())
        .first()
    )
    missed = db.query(func.count(LocationSample.id)).filter(
        LocationSample.attendance_id == att.id, LocationSample.status == MISSED
    ).scalar()
    payload.update({
        "attendance_id": att.id,
        "state": att.tracking_state,
        "stop_reason": att.tracking_stop_reason,
        "schedule_state": schedule_state(att, now),
        "next_due_at": _iso(att.tracking_next_due_at),
        "grace_until": _iso(att.tracking_next_due_at + policy.grace) if att.tracking_next_due_at else None,
        "overdue_since": _iso(att.tracking_overdue_since),
        "interval_minutes": att.tracking_interval_minutes or policy.tracking_interval_minutes,
        "missed_count": missed,
        "pending_missed": _pending_misses(att, policy, now),
        "last_capture": _sample_brief(last_capture),
        "last_attempt": _sample_brief(last_attempt),
    })
    return payload


# ---------------------------------------------------------------------------
# Self-service endpoints
# ---------------------------------------------------------------------------
def session_policy(db: Session, att: Attendance) -> AttendancePolicy:
    """The policy a session is tracked under: the employee's capture policy (interval, grace, pause on break) on its
    shift date over the company settings; v1 tenants resolve LEGACY_V1, i.e. the company settings themselves."""
    from app.services import attendance_capture_policy

    return attendance_capture_policy.for_session(db, att)


def _own_live_session(db: Session, principal: Principal, now: datetime):
    """(employee, live session or None, the policy that session is tracked under)."""
    from app.services import attendance_service

    if not principal.has("attendance:mark"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    if not employee.is_active:
        raise forbidden("Your employment record is inactive.")
    attendance_service.lock_employee(db, employee.id)
    att = attendance_service.live_session(db, employee.id, lock=True)
    policy = session_policy(db, att) if att is not None else attendance_policy.load(db, principal.company_id)
    if att is not None and attendance_service.expire_if_needed(db, att, policy, now):
        att = None
    if att is not None:
        enforce_policy(db, att, policy, now)
    return employee, att, policy


def enforce_policy(db: Session, att: Attendance, policy: AttendancePolicy, now: datetime) -> None:
    """The current policy wins over whatever a session started with (e.g. a race with a punch-in): tracking switched
    off stops it; a changed interval re-anchors the next check (slots under the old interval are settled first)."""
    if not policy.tracking_enabled and att.tracking_state in (ACTIVE, PAUSED):
        stop_tracking(db, att, policy, now, "POLICY_DISABLED")
    elif att.tracking_state == ACTIVE and att.tracking_interval_minutes != policy.tracking_interval_minutes:
        reconcile(db, att, policy, now)
        overdue = att.tracking_overdue_since
        att.tracking_interval_minutes = policy.tracking_interval_minutes
        _re_anchor(att, now)
        att.tracking_overdue_since = overdue


def tracking_status(db: Session, principal: Principal) -> dict:
    now = clock.utcnow()
    _, att, policy = _own_live_session(db, principal, now)
    if att is not None:
        reconcile(db, att, policy, now)
    return status_payload(db, att, policy, now)


def submit_sample(db: Session, principal: Principal, data: SampleSubmission) -> dict:
    now = clock.utcnow()
    employee, att, policy = _own_live_session(db, principal, now)

    previous = db.query(LocationSample).filter(
        LocationSample.employee_id == employee.id, LocationSample.idempotency_key == data.idempotency_key
    ).first()
    if previous is not None:
        if previous.attendance_id != data.attendance_id:
            raise conflict("This request id was already used for another reading.", "IDEMPOTENCY_CONFLICT")
        payload = status_payload(db, att if att and att.id == previous.attendance_id else None, policy, now)
        payload["replayed"] = True
        return payload

    if att is None or att.id != data.attendance_id:
        # Same answer whether the id is closed, someone else's or unknown: nothing leaks.
        raise conflict("This shift is no longer active, so no location is collected.", "SESSION_NOT_ACTIVE")
    reconcile(db, att, policy, now)
    if att.tracking_state != ACTIVE:
        raise conflict(
            "Location tracking is paused during lunch." if att.tracking_state == PAUSED
            else "Location tracking is not active for this shift.",
            "TRACKING_NOT_ACTIVE",
        )
    state = schedule_state(att, now)
    if state == SCHEDULED and now < att.tracking_next_due_at - EARLY_TOLERANCE:
        raise conflict("The next location check is not due yet.", "NOT_DUE")

    attempt = data.location
    slot = att.tracking_next_due_at
    if attempt.status == CAPTURED:
        reading = validate_reading(attempt, policy, now, data.client_sent_at)
        _new_sample(db, att, "PERIODIC", CAPTURED, now, policy, reading=reading, attempt=attempt,
                    client_sent_at=data.client_sent_at, slot_due_at=slot, is_recovery=state == OVERDUE,
                    idempotency_key=data.idempotency_key)
        _re_anchor(att, now)
    else:
        already = db.query(LocationSample.id).filter(
            LocationSample.attendance_id == att.id, LocationSample.slot_due_at == slot,
            LocationSample.status == attempt.status,
        ).first()
        if already is None:  # one failure row per slot and reason; retries add nothing new
            _new_sample(db, att, "PERIODIC", attempt.status, now, policy, attempt=attempt,
                        client_sent_at=data.client_sent_at, slot_due_at=slot, idempotency_key=data.idempotency_key)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise conflict("A reading for this check was already recorded.", "NOT_DUE")
    return status_payload(db, att, policy, now)


# ---------------------------------------------------------------------------
# Manager / HR views
# ---------------------------------------------------------------------------
def can_read_locations(db: Session, principal: Principal, employee_id: int) -> bool:
    return authz.can_access_employee(db, principal, "location:read", employee_id)


def session_samples(db: Session, principal: Principal, att: Attendance, employee: Employee, coordinates: bool) -> List[dict]:
    rows = (
        db.query(LocationSample)
        .filter(LocationSample.attendance_id == att.id)
        .order_by(LocationSample.received_at, LocationSample.id)
        .all()
    )
    if coordinates and not authz.is_self(principal, employee.id):
        AuditLogService.log(
            db, principal, "Location", "VIEW",
            f"Viewed location readings of {employee.full_name} for {att.date}", "attendance", att.id,
        )
    return [_sample_brief(r, coordinates) for r in rows]


def summaries(db: Session, attendance_ids: Iterable[int]) -> Dict[int, dict]:
    """Per-session counts for list views (no coordinates)."""
    ids = list(attendance_ids)
    result: Dict[int, dict] = {
        i: {"captured": 0, "failed": 0, "missed": 0, "low_accuracy": 0, "movement_flags": 0, "exceptions": 0,
            "review_pending": 0}
        for i in ids
    }
    if not ids:
        return result
    rows = (
        db.query(
            LocationSample.attendance_id, LocationSample.status, LocationSample.accuracy_class,
            LocationSample.movement_status, LocationSample.exception_reason.isnot(None), LocationSample.review_status,
            func.count(LocationSample.id),
        )
        .filter(LocationSample.attendance_id.in_(ids))
        .group_by(
            LocationSample.attendance_id, LocationSample.status, LocationSample.accuracy_class,
            LocationSample.movement_status, LocationSample.exception_reason.isnot(None), LocationSample.review_status,
        )
        .all()
    )
    for att_id, status, accuracy, movement, is_exception, review, count in rows:
        item = result[att_id]
        if status == CAPTURED:
            item["captured"] += count
        elif status == MISSED:
            item["missed"] += count
        else:
            item["failed"] += count
        if accuracy == geo.LOW:
            item["low_accuracy"] += count
        if movement == geo.SIGNIFICANT_MOVEMENT:
            item["movement_flags"] += count
        if is_exception:
            item["exceptions"] += count
        if review == "PENDING":
            item["review_pending"] += count
    return result


def review_queue(db: Session, principal: Principal, status: str = "PENDING", limit: int = 100) -> List[dict]:
    scope = authz.employee_scope(db, principal, "location:review")
    if scope.is_empty:
        raise forbidden()
    query = (
        db.query(LocationSample, Employee, Attendance.date)
        .join(Employee, Employee.id == LocationSample.employee_id)
        .join(Attendance, Attendance.id == LocationSample.attendance_id)
        .filter(
            LocationSample.company_id == principal.company_id,
            LocationSample.review_status == status,
            scope.clause(LocationSample.employee_id),
        )
    )
    if principal.employee_id is not None:
        query = query.filter(LocationSample.employee_id != principal.employee_id)  # nobody reviews themselves
    rows = query.order_by(LocationSample.received_at.desc()).limit(limit).all()
    return [
        {
            **_sample_brief(sample),
            "kind": "EXCEPTION" if sample.exception_reason else "MOVEMENT",
            "attendance_id": sample.attendance_id,
            "employee_id": employee.id,
            "employee_name": employee.full_name,
            "date": day.isoformat(),
            "reviewed_at": _iso(sample.reviewed_at),
            "review_note": sample.review_note,
        }
        for sample, employee, day in rows
    ]


def review(db: Session, principal: Principal, sample_id: int, data: ReviewDecision) -> dict:
    sample = db.query(LocationSample).filter(
        LocationSample.company_id == principal.company_id, LocationSample.id == sample_id
    ).first()
    if sample is None or sample.review_status is None:
        raise not_found("Location review item")
    if authz.is_self(principal, sample.employee_id):
        raise forbidden("You cannot review your own location records.")
    if not authz.can_access_employee(db, principal, "location:review", sample.employee_id):
        raise not_found("Location review item")  # same answer as an unknown id: nothing to enumerate
    employee = db.get(Employee, sample.employee_id)
    if sample.review_status != "PENDING":
        raise conflict("This item has already been reviewed.", "ALREADY_REVIEWED")
    allowed = ("APPROVED", "REJECTED") if sample.exception_reason else ("ACKNOWLEDGED",)
    if data.decision not in allowed:
        raise AppError(http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                       f"Choose one of: {', '.join(allowed)}.", "INVALID_DECISION")
    sample.review_status = data.decision
    sample.reviewed_by_user_id = principal.user_id
    sample.reviewed_at = clock.utcnow()
    sample.review_note = data.note
    kind = "location exception" if sample.exception_reason else "movement flag"
    AuditLogService.log(
        db, principal, "Location", "REVIEW",
        f"{data.decision.title()} {kind} of {employee.full_name} ({sample.sample_type})", "location_sample", sample.id,
    )
    return _sample_brief(sample)


def stop_for_employee(db: Session, company_id: int, employee_id: Optional[int], reason: str) -> bool:
    """Stop collecting immediately when access is revoked mid-shift (the shift itself stays open)."""
    from app.services import attendance_service

    if employee_id is None:
        return False
    attendance_service.lock_employee(db, employee_id)
    att = attendance_service.live_session(db, employee_id, lock=True)
    if att is None or att.tracking_state not in (ACTIVE, PAUSED):
        return False
    stop_tracking(db, att, session_policy(db, att), clock.utcnow(), reason)
    return True


# ---------------------------------------------------------------------------
# Policy changes, retention and the scheduled sweep
# ---------------------------------------------------------------------------
TRACKING_KEYS = {"trackingEnabled", "trackingIntervalMinutes", "trackingPauseOnLunch", "trackingGraceMinutes"}


def apply_policy_change(db: Session, company_id: int, changed: set) -> int:
    """Apply a changed tracking policy to running shifts immediately (not at their next punch)."""
    if not changed & TRACKING_KEYS:
        return 0
    now = clock.utcnow()
    sessions = (
        db.query(Attendance)
        .filter(
            Attendance.company_id == company_id,
            Attendance.check_out_time.is_(None),
            Attendance.closed_at.is_(None),
            Attendance.tracking_state.in_((ACTIVE, PAUSED)),
        )
        .with_for_update()
        .all()
    )
    for att in sessions:
        policy = session_policy(db, att)
        if not policy.tracking_enabled:
            stop_tracking(db, att, policy, now, "POLICY_DISABLED")
            continue
        on_lunch = db.query(AttendanceBreak.id).filter(
            AttendanceBreak.attendance_id == att.id, AttendanceBreak.ended_at.is_(None)
        ).first() is not None
        if att.tracking_state == PAUSED and not (on_lunch and policy.pause_on_lunch):
            resume_tracking(att, policy, now)
        elif att.tracking_state == ACTIVE and on_lunch and policy.pause_on_lunch:
            pause_tracking(db, att, policy, now)
        elif att.tracking_state == ACTIVE and att.tracking_interval_minutes != policy.tracking_interval_minutes:
            reconcile(db, att, policy, now)  # slots under the old interval are settled first
            overdue = att.tracking_overdue_since
            att.tracking_interval_minutes = policy.tracking_interval_minutes
            _re_anchor(att, now)
            att.tracking_overdue_since = overdue
    return len(sessions)


def purge_expired(db: Session, company_id: int, now: Optional[datetime] = None) -> int:
    """Redact coordinates older than the company's retention period (the outcome rows stay for audit)."""
    policy = attendance_policy.load(db, company_id)
    now = now or clock.utcnow()
    cutoff = now - timedelta(days=policy.retention_days)
    count = (
        db.query(LocationSample)
        .filter(
            LocationSample.company_id == company_id,
            LocationSample.received_at < cutoff,
            LocationSample.redacted_at.is_(None),
            LocationSample.latitude.isnot(None),
        )
        .update(
            {
                LocationSample.latitude: None, LocationSample.longitude: None,
                LocationSample.distance_from_punch_in_m: None, LocationSample.distance_from_previous_m: None,
                LocationSample.redacted_at: now,
            },
            synchronize_session=False,
        )
    )
    if count:
        AuditLogService.log(db, None, "Location", "RETENTION",
                            f"Redacted {count} location reading(s) older than {policy.retention_days} days",
                            company_id=company_id, actor_type="system")
    return count


def sweep(db: Session, company_id: int) -> dict:
    """Close expired shifts and record missed slots for one company (run on a schedule, e.g. every 15 min).

    A scheduler only *detects* missed readings; it cannot ask a closed or
    suspended web page for a position.
    """
    from app.services import attendance_service

    policy = attendance_policy.load(db, company_id)
    now = clock.utcnow()
    live = (
        db.query(Attendance)
        .filter(Attendance.company_id == company_id, Attendance.check_out_time.is_(None), Attendance.closed_at.is_(None))
        .with_for_update(skip_locked=True)
        .all()
    )
    expired = missed = stopped = 0
    for att in live:
        if att.tracking_state in (ACTIVE, PAUSED):
            att_policy = session_policy(db, att)
            enforce_policy(db, att, att_policy, now)
        else:
            att_policy = policy
        if attendance_service.expire_if_needed(db, att, att_policy, now):
            expired += 1
            continue
        if att.tracking_state in (ACTIVE, PAUSED):
            reason = _no_longer_collectable(db, att, att_policy)
            if reason:
                # Whoever can no longer submit readings must not accumulate misses either.
                stop_tracking(db, att, att_policy, now, reason, settle=False)
                stopped += 1
                continue
        missed += reconcile(db, att, att_policy, now)
    redacted = purge_expired(db, company_id, now)
    return {"company_id": company_id, "live_sessions": len(live), "expired": expired, "missed_slots": missed,
            "stopped": stopped, "redacted": redacted}


def _no_longer_collectable(db: Session, att: Attendance, policy: AttendancePolicy) -> Optional[str]:
    from app.core.principal import load_grants, subscription_is_active
    from app.models.company import Company
    from app.models.user import User

    if not policy.tracking_enabled:
        return "POLICY_DISABLED"
    employee = db.get(Employee, att.employee_id)
    if employee is None or not employee.is_active:
        return "EMPLOYMENT_ENDED"
    user = db.get(User, employee.user_id) if employee.user_id else None
    if user is None or user.status != "active":
        return "ACCOUNT_INACTIVE"
    if not subscription_is_active(db.get(Company, att.company_id)):
        return "SUBSCRIPTION_INACTIVE"
    if "attendance:mark" not in load_grants(db, user.id)[0]:
        return "PERMISSION_REVOKED"
    return None


def event_details(location: EventLocation) -> Optional[str]:
    if location.exception_reason:
        return json.dumps({"location_exception": True})
    return None
