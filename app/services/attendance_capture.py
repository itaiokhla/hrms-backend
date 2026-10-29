"""Capture v2: the interactive punch and the device-token kiosk (04-attendance/01 §4, §5; spine §5.5.1, §5.5.6;
D-AS-04, D-AS-25; BUG-AS-02, -06, -62).

* ``punch`` — ``POST /attendance/me/punch`` (WEB / MOBILE): capture policy for the employee-local date (source
  allowed, S-1), consent gate (step 4: 422 ``CONSENT_REQUIRED`` only for PUNCH_PHOTO / LOCATION_AT_PUNCH; a missing
  face consent skips the face step, F-2), site checks (``attendance_sites``), then today's pipeline
  (``attendance_service.perform``: face before the lock, employee lock, idempotency, stale tab, transition) which writes
  the raw punch and the session (shift date of the matched instance, ``session_seq``, overlap rule).
* ``kiosk_pair`` — claim code -> device token (``attendance_devices.pair``). Pairing itself needs no user session
  (SP-AS-207); step-up belongs to the signed-in registration step (``POST /attendance/devices``), which issues the
  claim code (there is no step-up helper in this codebase yet: FOLLOW-UPS).
* ``kiosk_identify`` — method allowed on this kiosk (else 422 ``KIOSK_METHOD_NOT_ALLOWED``); PIN / QR / CARD look
  up the canonical value tenant-wide (K-11), FACE 1:N among the kiosk branch's enrolled employees; returns a signed
  ``kiosk_ticket`` valid 60 s, bound to the device (K-6..K-8).
* ``kiosk_punch`` — ticket valid (410 ``KIOSK_TICKET_EXPIRED``), same device, method re-check against the employee's
  own policy, KIOSK allowed by the kiosk branch's policy, then an explicit direction (never a toggle, BUG-AS-06);
  the ticket nonce is the idempotency key (replay -> first result, other direction -> 409 ``IDEMPOTENCY_CONFLICT``).

Services never commit; routes commit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import authz, clock
from app.core.errors import AppError, bad_request, conflict, forbidden
from app.core.principal import Principal
from app.core.rate_limit import limiter
from app.models.attendance import AttendanceEvent
from app.models.employee import Employee, FaceEncoding
from app.models.org import EmployeeAssignment
from app.modules.face_recognition import engine as face_engine
from app.schemas.attendance import EventMeta
from app.services import (
    attendance_capture_policy,
    attendance_consents,
    attendance_devices,
    attendance_policy,
    attendance_review,
    attendance_service,
    attendance_sites,
    location_service,
    org_structure_service,
)
from app.services.attendance_devices import DevicePrincipal
from app.services.audit_log_service import AuditLogService

TICKET_TTL = timedelta(seconds=60)
TICKET_VERSION = "kt1"
# Rate limits (spine §8.1, SR-AS-11; BUG-AS-62; K-7a). The per-device identify budget is the spine value (60/min);
# failures have their own budget and a per-value cooldown so one person mistyping cannot lock the crew out.
PUNCH_PER_MINUTE = 20
IDENTIFY_PER_MINUTE = 60
IDENTIFY_FAILURES_PER_MINUTE = 20
VALUE_FAILURES_BEFORE_COOLDOWN = 5
VALUE_COOLDOWN_SECONDS = 30
BREAK_ACTIONS = ("BREAK_START", "BREAK_END")


def _now() -> datetime:
    return clock.utcnow().replace(microsecond=0)


# ------------------------------------------------------------------------------------------------- web / mobile
EVENT_OF = {"IN": "PUNCH_IN", "OUT": "PUNCH_OUT", "BREAK_START": "LUNCH_IN", "BREAK_END": "LUNCH_OUT"}


def punch(db: Session, principal: Principal, data, image: Optional[bytes], ip: Optional[str],
          offline_flags: Optional[tuple] = None) -> dict:
    """One interactive punch (``data`` = ``PunchMeta``); returns the acted-on session, ``my_today``, ``punch_id`` and
    ``location_check`` (the site result the employee may see: result, band, site name — never coordinates).

    ``offline_flags`` (AS-M12 offline queue, §6.2 O-6): the item is replayed at its capture instant (``clock.at``);
    the face, QR and photo steps are skipped (a queue holds none of them) and the punch carries the flags, plus
    ``OFFLINE_UNVERIFIED`` when the policy asked for that evidence."""
    from dataclasses import replace as _replace

    if not principal.has("attendance:mark"):
        raise forbidden()
    employee = authz.own_employee(db, principal)
    if not employee.is_active:
        raise forbidden("Your employment record is inactive.")
    day = org_structure_service.employee_local_today(db, employee)
    policy = attendance_capture_policy.effective(db, principal.company_id, employee.id, day)
    if offline_flags is not None:
        if policy.face_on_punch or policy.face_on_break or policy.qr_mode == "REQUIRED" or policy.punch_photo:
            offline_flags = tuple(offline_flags) + ("OFFLINE_UNVERIFIED",)
        policy = _replace(policy, face_on_punch=False, face_on_break=False, qr_mode="OFF", punch_photo=False)
        image = None
    if not policy.allows(data.capture_source):
        raise AppError(422, "Punching from here is not enabled for you.", "FEATURE_DISABLED")
    if data.direction in BREAK_ACTIONS and (data.break_kind or "LUNCH") != "LUNCH":
        raise AppError(422, "Only lunch breaks can be punched yet.", "FEATURE_DISABLED")
    on_break = data.direction in BREAK_ACTIONS
    location_mode = policy.location_on_breaks if on_break else policy.location_mode
    acknowledged = data.location_consent
    if location_mode != "OFF":
        attendance_consents.require_consent(db, principal.company_id, employee.id, "LOCATION_AT_PUNCH", day,
                                            acknowledged=data.location_consent)
        acknowledged = True  # recorded in the ledger (now or earlier)
    if policy.punch_photo and image:
        attendance_consents.require_consent(db, principal.company_id, employee.id, "PUNCH_PHOTO", day)
    face_wanted = policy.face_on_break if on_break else policy.face_on_punch
    skip_face = face_wanted and not attendance_consents.face_allowed(db, principal.company_id, employee.id, day,
                                                                     profile=policy.profile)
    work_mode = data.work_location_choice or "ON_SITE"  # P-6
    pipeline = attendance_capture_policy.as_attendance_policy(policy, attendance_policy.load(db, principal.company_id))
    if pipeline.tracking_enabled and data.direction == "IN":
        # Periodic location during the shift has its own notice (07 L-2): a refusal keeps tracking off; the punch
        # screen's location notice, once acknowledged, is recorded as the tracking consent.
        if attendance_capture_policy.tracking_refused(db, principal.company_id, employee.id, day):
            from dataclasses import replace as _replace

            pipeline = _replace(pipeline, tracking_enabled=False)
        elif data.location_consent and not attendance_consents.has_consent(
                db, principal.company_id, employee.id, "LOCATION_TRACKING", day):
            attendance_consents.record(db, principal.company_id, employee.id, "LOCATION_TRACKING", "GRANTED",
                                       channel="SELF_SERVICE", on=day, principal=principal)
    # Step 15 before the site check (AS-GEO-03): freshness, accuracy and REQUIRED refusals of today's pipeline.
    location = location_service.evaluate_event(pipeline, EVENT_OF[data.direction], data.location,
                                               data.exception_reason, _now(), data.client_sent_at, "self")
    reading = location.reading
    outcome = attendance_sites.evaluate_punch(
        db, principal.company_id, employee.id,
        attendance_sites.LocationFix(
            latitude=reading.latitude if reading else None, longitude=reading.longitude if reading else None,
            accuracy_m=reading.accuracy_m if reading else None,
            fix_age_seconds=reading.fix_age_seconds if reading else None, client_ip=ip,
            qr_payload=data.qr_payload, work_mode=work_mode, exception_reason=data.exception_reason),
        policy, day)
    capture = attendance_service.Capture(
        source=data.capture_source, multiple_sessions_allowed=policy.multiple_sessions_allowed,
        verify_mode="NONE" if skip_face or offline_flags is not None else None, site_id=outcome.site_id,
        geofence_result=outcome.result, evidence_flags=tuple(outcome.flags) + tuple(offline_flags or ()), ip=ip,
        work_mode=work_mode, skip_face=skip_face or offline_flags is not None, attendance_policy=pipeline)
    meta = EventMeta(**{**data.model_dump(include=set(EventMeta.model_fields)), "location_consent": acknowledged})
    result = attendance_service.perform(db, principal, attendance_service.ACTION_OF[data.direction], image, meta, ip,
                                        capture=capture)
    if capture.punch_id is not None:
        _link_sample(db, capture.punch_id, outcome, data.exception_reason)
        for kind, severity in outcome.review_kinds:
            attendance_review.open_item(db, principal.company_id, kind,
                                        {"employee_id": employee.id, "shift_date": result.get("date"),
                                         "punch_id": capture.punch_id}, severity=severity)
    return {**result, "punch_id": capture.punch_id if capture.punch_id is not None else _replayed_punch(db, meta),
            "direction": data.direction, "capture_source": data.capture_source,
            "location_check": outcome.employee_view(),
            "face_step": "SKIPPED" if skip_face else ("REQUIRED" if face_wanted else "OFF")}


def _link_sample(db: Session, punch_id: int, outcome, reason: Optional[str]) -> None:
    """The event's location sample carries the punch and its site check (01 §4.2 step 19; AS 11 §3.4)."""
    from app.models.attendance import LocationSample

    sample = (db.query(LocationSample).join(AttendanceEvent, AttendanceEvent.id == LocationSample.event_id)
              .filter(AttendanceEvent.punch_id == punch_id).first())
    if sample is None:
        return
    sample.punch_id = punch_id
    sample.site_id = outcome.site_id
    sample.distance_to_site_m = outcome.distance_m
    sample.geofence_result = outcome.result
    if reason and "OUTSIDE_GEOFENCE" in outcome.flags and not sample.exception_reason:
        sample.exception_reason = reason
    db.flush()


def _replayed_punch(db: Session, meta: EventMeta) -> Optional[int]:
    if not meta.idempotency_key:
        return None
    event = db.query(AttendanceEvent).filter(AttendanceEvent.idempotency_key == meta.idempotency_key).first()
    return event.punch_id if event is not None else None


# ------------------------------------------------------------------------------------------------- kiosk tickets
def _sign(payload: bytes) -> str:
    return hmac.new(app_settings.secret_key.encode(), b"kiosk-ticket|" + payload, hashlib.sha256).hexdigest()


def issue_ticket(device: DevicePrincipal, employee_id: int, method: str, score: Optional[float], now: datetime) -> dict:
    body = {"c": device.company_id, "d": device.device_id, "e": employee_id, "m": method,
            "s": None if score is None else round(score, 4), "n": secrets.token_hex(16),
            "x": int((now + TICKET_TTL).timestamp())}
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return {"ticket": f"{TICKET_VERSION}.{encoded}.{_sign(raw)}", "expires_at": now + TICKET_TTL, "body": body}


@dataclass(frozen=True)
class Ticket:
    company_id: int
    device_id: int
    employee_id: int
    method: str
    score: Optional[float]
    nonce: str
    expires_at: datetime


def read_ticket(device: DevicePrincipal, raw: str, now: datetime) -> Ticket:
    """Signature, device binding (else 401 ``DEVICE_AUTH_FAILED``) and expiry (410 ``KIOSK_TICKET_EXPIRED``)."""
    invalid = AppError(401, "This ticket is not valid on this kiosk.", "DEVICE_AUTH_FAILED")
    try:
        version, encoded, signature = raw.split(".")
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        body = json.loads(payload)
    except (ValueError, TypeError):
        raise invalid
    if version != TICKET_VERSION or not hmac.compare_digest(signature, _sign(payload)):
        raise invalid
    if body.get("c") != device.company_id or body.get("d") != device.device_id:
        raise invalid
    expires = datetime.fromtimestamp(int(body["x"]), tz=now.tzinfo)
    if now >= expires:
        raise AppError(410, "Time's up. Enter your PIN again.", "KIOSK_TICKET_EXPIRED")
    return Ticket(body["c"], body["d"], int(body["e"]), body["m"], body.get("s"), body["n"], expires)


# ------------------------------------------------------------------------------------------------- kiosk
def kiosk_pair(db: Session, company_id: int, device_code: str, claim_code: str, ip: Optional[str]):
    """(body, device token) — see ``attendance_devices.pair``."""
    return attendance_devices.pair(db, company_id, device_code, claim_code, ip)


def canonical_value(value: str) -> str:
    """M-11 canonical form of a PIN / card / QR value: trim, NFKC, upper case; digits without leading zeros."""
    text = unicodedata.normalize("NFKC", value or "").strip().upper()
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


def _local_date(device: DevicePrincipal, now: datetime):
    import pytz

    return now.astimezone(pytz.timezone(device.timezone or "UTC")).date()


def screen_methods(db: Session, device: DevicePrincipal, now: datetime) -> frozenset:
    """Methods this kiosk offers: the branch-level policy's ``kiosk_methods`` within the device settings (AS-CAP-09)."""
    from app.models.attendance import AttendanceDevice

    policy = attendance_capture_policy.for_branch(db, device.company_id, device.branch_id, _local_date(device, now))
    row = db.get(AttendanceDevice, device.device_id)
    allowed = (row.settings or {}).get("methods") if row is not None else None
    methods = policy.kiosk_methods
    if allowed:
        methods = methods & frozenset(allowed)
    return frozenset(methods)


def _employee_by_value(db: Session, company_id: int, value: str) -> Optional[Employee]:
    """PIN / QR / CARD: tenant-wide lookup of the canonical value (K-11). Phase 1 maps it to ``employee_code``; the
    device user mappings of AS-M7 take over (FOLLOW-UPS)."""
    canon = canonical_value(value)
    code = func.upper(func.trim(Employee.employee_code))
    if canon.isdigit():
        code = func.coalesce(func.nullif(func.ltrim(code, "0"), ""), "0")
    rows = db.query(Employee).filter(Employee.company_id == company_id, Employee.is_active.is_(True),
                                     code == canon).limit(2).all()
    return rows[0] if len(rows) == 1 else None


def _employee_by_face(db: Session, device: DevicePrincipal, image: bytes, now: datetime):
    from app.services import attendance_faces

    probe = attendance_service._encode_or_400(image)
    query = (db.query(FaceEncoding)
             .join(Employee, Employee.id == FaceEncoding.employee_id)
             .filter(FaceEncoding.company_id == device.company_id, FaceEncoding.is_primary.is_(True),
                     FaceEncoding.deleted_at.is_(None), Employee.is_active.is_(True)))
    day = _local_date(device, now)
    if device.branch_id is not None:
        placed = (db.query(EmployeeAssignment.employee_id)
                  .filter(EmployeeAssignment.company_id == device.company_id,
                          EmployeeAssignment.branch_id == device.branch_id, EmployeeAssignment.is_primary.is_(True),
                          EmployeeAssignment.effective_from <= day,
                          (EmployeeAssignment.effective_to.is_(None)) | (EmployeeAssignment.effective_to >= day)))
        query = query.filter(FaceEncoding.employee_id.in_(placed))
    # F-4 / B-7: 1:N only among employees whose face step is allowed (consent, or a legacy enrolment in its grace
    # under a LEGACY_V1 kiosk policy).
    profile = attendance_capture_policy.for_branch(db, device.company_id, device.branch_id, day).profile
    rows = [r for r in query.all()
            if attendance_consents.face_allowed(db, device.company_id, r.employee_id, day, profile=profile)]
    candidates = attendance_faces.candidates(db, rows)
    if not candidates:
        return None, None
    match = face_engine.best_match(probe, candidates, app_settings.face_recognition_threshold)
    if match is None:
        return None, None
    return db.get(Employee, match[0]), face_engine.similarity(match[1])


def _value_key(device: DevicePrincipal, value: str) -> str:
    digest = hashlib.sha256(canonical_value(value).encode()).hexdigest()[:24]
    return f"kiosk-value:{device.company_id}:{device.device_id}:{digest}"


def _allowed_actions(db: Session, employee: Employee, policy) -> list:
    live = attendance_service.live_session(db, employee.id)
    if live is None:
        return ["IN"]
    breaks = attendance_service.breaks_by_session(db, [live.id])[live.id]
    if any(b.ended_at is None for b in breaks):
        return ["BREAK_END", "OUT"]
    return (["BREAK_START"] if policy.allow_multiple_breaks or not breaks else []) + ["OUT"]


def kiosk_identify(db: Session, device: DevicePrincipal, data, image: Optional[bytes] = None) -> dict:
    """Identify the person in front of the kiosk and hand out a 60 s ticket (no login)."""
    now = _now()
    limiter.check(f"kiosk-identify:{device.company_id}:{device.device_id}", IDENTIFY_PER_MINUTE, 60)
    if data.method not in screen_methods(db, device, now):
        raise AppError(422, "This kiosk does not offer that method.", "KIOSK_METHOD_NOT_ALLOWED")
    failures_key = f"kiosk-identify-failed:{device.company_id}:{device.device_id}"
    if limiter.count(failures_key, 60) >= IDENTIFY_FAILURES_PER_MINUTE:
        raise AppError(429, "The kiosk is busy - try again in a few seconds.", "RATE_LIMITED",
                       headers={"Retry-After": "10"})
    score = None
    if data.method == "FACE":
        if not image:
            raise bad_request("A face capture is required.", "FACE_REQUIRED")
        employee, score = _employee_by_face(db, device, image, now)
        value_key = None
    else:
        if not data.value:
            raise bad_request("Enter your PIN.", "VALUE_REQUIRED")
        value_key = _value_key(device, data.value)
        if limiter.count(value_key, VALUE_COOLDOWN_SECONDS) >= VALUE_FAILURES_BEFORE_COOLDOWN:
            raise AppError(429, "The kiosk is busy - try again in a few seconds.", "RATE_LIMITED",
                           headers={"Retry-After": str(VALUE_COOLDOWN_SECONDS)})
        employee = _employee_by_value(db, device.company_id, data.value)
    if employee is None:
        limiter.check(failures_key, 10 ** 6, 60)  # records one failure (K-7: no ticket, no probe kept)
        if value_key:
            limiter.check(value_key, 10 ** 6, VALUE_COOLDOWN_SECONDS)
        message = "We couldn't recognise you. Use your PIN." if data.method == "FACE" else "We couldn't find that PIN."
        return {"recognised": False, "message": message}
    if value_key:
        limiter.clear(value_key)
    policy = attendance_capture_policy.effective(db, device.company_id, employee.id, _local_date(device, now))
    ticket = issue_ticket(device, employee.id, data.method, score, now)
    initials = "".join(p[:1] for p in (employee.first_name, employee.last_name) if p).upper()
    return {
        "recognised": True, "kiosk_ticket": ticket["ticket"], "expires_at": ticket["expires_at"].isoformat(),
        "employee": {"display_name": employee.full_name, "initials": initials},
        "allowed_actions": _allowed_actions(db, employee, policy), "photo_allowed": False, "method": data.method,
    }


class _KioskActor:
    """The actor of a kiosk transition: no user (events and audit name the device)."""

    def __init__(self, company_id: int):
        self.user_id = None
        self.company_id = company_id


def kiosk_punch(db: Session, device: DevicePrincipal, ticket: str, direction: str, ip: Optional[str] = None) -> dict:
    """Punch the ticket's employee with an explicit direction (BUG-AS-06)."""
    now = _now()
    t = read_ticket(device, ticket, now)
    employee = db.query(Employee).filter(Employee.company_id == device.company_id, Employee.id == t.employee_id).first()
    if employee is None or not employee.is_active:
        raise AppError(401, "This ticket is not valid on this kiosk.", "DEVICE_AUTH_FAILED")
    local_day = _local_date(device, now)
    own = attendance_capture_policy.effective(db, device.company_id, employee.id, local_day)
    if t.method not in own.kiosk_methods:
        raise AppError(422, "Use another method.", "KIOSK_METHOD_NOT_ALLOWED")
    kiosk_policy = attendance_capture_policy.for_branch(db, device.company_id, device.branch_id, local_day)
    if not kiosk_policy.allows("KIOSK"):
        raise AppError(422, "This kiosk is not enabled for punching.", "FEATURE_DISABLED")
    action = attendance_service.ACTION_OF[direction]
    company = db.get(attendance_service.Company, device.company_id)
    policy = attendance_policy.load(db, device.company_id)

    attendance_service.lock_employee(db, employee.id)
    previous = db.query(AttendanceEvent).filter(AttendanceEvent.employee_id == employee.id,
                                                AttendanceEvent.idempotency_key == t.nonce).first()
    if previous is not None:
        if previous.event_type != action:
            raise conflict("This ticket was already used for a different action.", "IDEMPOTENCY_CONFLICT")
        return {**_kiosk_result(db, previous.attendance_id, employee, direction, previous.punch_id, device),
                "replayed": True}
    live = attendance_service.live_session(db, employee.id, lock=True)
    if live is not None and attendance_service.expire_if_needed(db, live, policy, now):
        live = None
    flags = ()
    placement = attendance_service._placement(db, employee, local_day)
    if device.branch_id is not None and placement is not None and placement.branch_id != device.branch_id:
        flags = ("OTHER_BRANCH_KIOSK",)  # K-11, attendance.kiosk.cross_branch default FLAG
    capture = attendance_service.Capture(
        source="KIOSK", multiple_sessions_allowed=own.multiple_sessions_allowed, verify_mode=t.method,
        device_id=device.device_id, site_id=device.site_id, evidence_flags=flags, ip=ip)
    meta = EventMeta(idempotency_key=t.nonce)
    actor = _KioskActor(device.company_id)
    if action == "PUNCH_IN":
        record = attendance_service._punch_in(db, actor, company, employee, policy, meta, t.score, now,
                                              source="kiosk", capture=capture)
    else:
        if action == "PUNCH_OUT" and live is not None and now - live.check_in_time < attendance_service.MIN_SESSION:
            raise bad_request("Please wait a minute before checking out.", "TOO_SOON")
        handler = attendance_service._HANDLERS[action]
        if action == "PUNCH_OUT":
            # The explicit Check out is the confirmation: an open lunch ends with the shift (today's kiosk rule).
            meta = EventMeta(idempotency_key=t.nonce, confirm_close_lunch=True)
            record = handler(db, actor, company, employee, policy, meta, t.score, now, live, source="kiosk",
                             capture=capture)
        else:
            record = handler(db, actor, company, employee, policy, meta, t.score, now, live, capture=capture)
    audit = {"PUNCH_IN": "KIOSK_CHECK_IN", "PUNCH_OUT": "KIOSK_CHECK_OUT", "LUNCH_IN": "KIOSK_BREAK_START",
             "LUNCH_OUT": "KIOSK_BREAK_END"}[action]
    AuditLogService.log(db, None, "Attendance", audit, f"Kiosk {device.device_code}: {employee.full_name} {direction}",
                        "attendance", record.id, ip, company_id=device.company_id, actor_type="device",
                        actor_id=device.device_id)
    return _kiosk_result(db, record.id, employee, direction, capture.punch_id, device)


def _kiosk_result(db: Session, attendance_id: int, employee: Employee, direction: str, punch_id: Optional[int],
                  device: DevicePrincipal) -> dict:
    record = db.get(attendance_service.Attendance, attendance_id)
    import pytz

    zone = pytz.timezone(device.timezone or "UTC")
    at = record.check_out_time if direction == "OUT" and record.check_out_time else clock.utcnow()
    return {"success": True, "direction": direction, "employee_name": employee.full_name,
            "session_id": record.id, "shift_date": record.date.isoformat(), "session_seq": record.session_seq,
            "punch_id": punch_id, "local_time": at.astimezone(zone).strftime("%H:%M")}
