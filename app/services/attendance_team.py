"""Team attendance: a manager's board of the team's day, punching for a team member, and deleting a session
(IG-AS-98..100; owner request 2026-10-28; AS 04-attendance/01 §5 supervisor capture, 04-attendance/04 §10 direct edit).

* **Who** — the hierarchy and team scopes of the permission registry: ``attendance:read`` for the board,
  ``attendance:proxy_punch`` to punch (system ``manager`` at DIRECT_REPORTS, ``hr_admin`` at COMPANY; custom roles
  at TEAM, ASSIGNED_TEAMS, BRANCH ...), ``attendance:regularize`` to correct or delete. Out-of-scope ids answer 404;
  nobody acts on their own attendance (403 ``SELF_EDIT_FORBIDDEN``).
* **Settings** (Settings › Attendance › Team; ``attendance.team.*``): team punches on/off, location REQUIRED /
  OPTIONAL / OFF, location entered by hand allowed, reason required, managers may edit / delete. Written by holders of
  ``attendance:manage_policy`` at COMPANY through ``settings.write_tenant_value`` (change control kept).
* **Punch for someone** — the same transitions as a self punch (``attendance_service._punch_in`` / ``_punch_out``)
  with source ``supervisor`` / capture ``SUPERVISOR``, the acting user and reason on the raw punch, the location (the
  manager's device reading or one entered by hand) as a CAPTURED location sample with ``exception_reason`` saying who
  placed it, an in-app notice to the employee, audit ``PROXY_PUNCH``; v2 tenants re-evaluate the day.
* **Delete** — v2 tenants: a DIRECT ``VOID_SESSION`` request (original punches kept, day re-evaluated, payroll change
  log); v1 tenants: the session is voided with reason ``ADMIN`` (readers exclude it, spine §15.4), its punches become
  ``IGNORED_BY_ADMIN`` and the change log versions it. Never a physical delete.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Mapping, Optional

from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.principal import Principal
from app.models.attendance import Attendance, AttendancePunch, LocationSample
from app.models.employee import Employee
from app.services import attendance_requests as ar
from app.services.audit_log_service import AuditLogService

KEYS = {
    "proxy_punch_enabled": "attendance.team.proxy_punch.enabled",
    "proxy_punch_location": "attendance.team.proxy_punch.location",
    "allow_manual_location": "attendance.team.proxy_punch.allow_manual_location",
    "reason_required": "attendance.team.proxy_punch.reason_required",
    "edit_enabled": "attendance.team.edit_enabled",
}
DEFAULTS = {"proxy_punch_enabled": False, "proxy_punch_location": "REQUIRED", "allow_manual_location": True,
            "reason_required": True, "edit_enabled": True}
MANAGE = "attendance:manage_policy"


# --------------------------------------------------------------------------- settings


def settings_for(db: Session, company_id: int, *, employee_id: Optional[int] = None,
                 on: Optional[date] = None) -> Dict[str, Any]:
    from app.payroll.services import settings as settings_engine
    from app.payroll.settings_registry import UNSET

    on = on or clock.company_today(db, company_id)
    out = {}
    for name, key in KEYS.items():
        try:
            value = settings_engine.resolve(db, company_id, key, employee_id=employee_id, on=on).value
        except Exception:  # noqa: BLE001 - registry default when nothing resolves
            value = None
        out[name] = DEFAULTS[name] if value is None or value is UNSET else value
    return out


def get_settings(db: Session, principal: Principal) -> dict:
    if not (principal.has(MANAGE) or principal.has("attendance:proxy_punch") or principal.has("attendance:read")):
        raise forbidden()
    return {**settings_for(db, principal.company_id), "can_manage": principal.has_company_scope(MANAGE)}


def update_settings(db: Session, principal: Principal, data: Mapping[str, Any], *, reason: str) -> dict:
    from app.payroll.services import settings as settings_engine

    if not principal.has_company_scope(MANAGE):
        raise forbidden()
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise bad_request(f"Unknown settings: {', '.join(unknown)}.", "INVALID_FIELDS")
    if data.get("proxy_punch_location") not in (None, "REQUIRED", "OPTIONAL", "OFF"):
        raise bad_request("Location must be REQUIRED, OPTIONAL or OFF.", "INVALID_FIELDS")
    before = settings_for(db, principal.company_id)
    for name, value in data.items():
        if value is None or value == before[name]:
            continue
        settings_engine.write_tenant_value(db, principal, KEYS[name], value, reason=reason)
    db.flush()
    after = settings_for(db, principal.company_id)
    AuditLogService.log(db, principal, ar.AUDIT_MODULE, "POLICY_SAVED", "Changed team attendance settings",
                        "company_setting", None, details={"before": before, "after": after})
    return {**after, "can_manage": True}


# --------------------------------------------------------------------------- team board


def team_today(db: Session, principal: Principal, *, q: Optional[str] = None, limit: int = 200) -> dict:
    """People in the caller's ``attendance:read`` / ``attendance:proxy_punch`` scope with today's state."""
    from app.services import attendance_service

    if not (principal.has("attendance:proxy_punch") or principal.has("attendance:read")):
        raise forbidden()
    code = "attendance:proxy_punch" if principal.has("attendance:proxy_punch") else "attendance:read"
    scope = authz.employee_scope(db, principal, code)
    query = db.query(Employee).filter(Employee.company_id == principal.company_id, Employee.is_active.is_(True),
                                      scope.clause(Employee.id))
    if principal.employee_id is not None:
        query = query.filter(Employee.id != principal.employee_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((Employee.first_name.ilike(like)) | (Employee.last_name.ilike(like))
                             | (Employee.employee_code.ilike(like)))
    employees = query.order_by(Employee.first_name, Employee.last_name).limit(min(limit, 500)).all()
    ids = [e.id for e in employees] or [0]
    live = {s.employee_id: s for s in db.query(Attendance).filter(
        Attendance.company_id == principal.company_id, Attendance.employee_id.in_(ids),
        Attendance.voided_at.is_(None), Attendance.check_out_time.is_(None), Attendance.closed_at.is_(None))}
    company = db.get(attendance_service.Company, principal.company_id)
    now = attendance_service._now()
    today = attendance_service.local_today(company, now)
    yesterday = today - timedelta(days=1)
    done: Dict[int, Attendance] = {}
    for s in (db.query(Attendance)
              .filter(Attendance.company_id == principal.company_id, Attendance.employee_id.in_(ids),
                      Attendance.voided_at.is_(None), Attendance.date.in_((today, yesterday)),
                      Attendance.check_out_time.isnot(None))
              .order_by(Attendance.check_out_time)):
        done[s.employee_id] = s  # the latest completed session of today or yesterday
    settings = settings_for(db, principal.company_id)
    can_punch = principal.has("attendance:proxy_punch") and settings["proxy_punch_enabled"]
    tracking = _tracking(db, principal, [s for s in live.values()], now)
    rows = []
    for e in employees:
        session, shift_date = live.get(e.id), None
        if session is not None:
            state, actions, shift_date = "WORKING", ["OUT"], session.date
        else:
            state, actions = "NOT_PUNCHED_IN", ["IN"]
            finished = done.get(e.id)
            if finished is not None:
                # FU-AS-43: an IN now belongs to the shift date the matching window gives (after midnight that can be
                # yesterday's shift); with one session per date and that date complete, nobody can punch in.
                shift_date = _next_in_date(db, company, e, now)
                if shift_date == finished.date:
                    session, state = finished, "PUNCHED_OUT"
                    actions = ["IN"] if _multiple_allowed(db, company.id, e.id, shift_date) else []
        rows.append({"employee_id": e.id, "name": e.full_name, "employee_code": e.employee_code,
                     "position": e.position, "state": state, "session_id": session.id if session else None,
                     "shift_date": shift_date.isoformat() if shift_date else None,
                     "check_in_time": _iso(session.check_in_time) if session else None,
                     "check_out_time": _iso(session.check_out_time) if session else None,
                     "capture_source": session.capture_source if session else None,
                     "actions": actions if can_punch else [], "can_punch": can_punch and bool(actions),
                     "tracking": tracking.get(session.id) if session is not None and e.id in live else None})
    return {"date": today.isoformat(), "settings": settings, "can_punch": can_punch,
            "can_edit": settings["edit_enabled"] and principal.has("attendance:regularize"),
            "server_now": now.isoformat(), "items": rows}


def _next_in_date(db: Session, company, employee: Employee, now: datetime) -> date:
    from app.services import attendance_sessions

    try:
        return attendance_sessions.resolve_shift_date(db, company.id, employee, now, "IN").date
    except Exception:  # noqa: BLE001 - the board never fails on one person's shift data
        from app.services import attendance_service

        return attendance_service.local_today(company, now)


def _multiple_allowed(db: Session, company_id: int, employee_id: int, day: date) -> bool:
    from app.services import attendance_access, attendance_capture_policy

    if not attendance_access.v2_enabled(db, company_id):
        return False
    return attendance_capture_policy.effective(db, company_id, employee_id, day).multiple_sessions_allowed


def _tracking(db: Session, principal: Principal, sessions: list, now: datetime) -> Dict[int, dict]:
    """Periodic location sampling of live sessions for the board: state, next check, last reading and misses. The
    reading's coordinates only for people in the caller's ``location:read`` scope (never partner-delegable)."""
    from sqlalchemy import func

    from app.services import location_service

    if not sessions:
        return {}
    ids = [s.id for s in sessions]
    latest: Dict[int, LocationSample] = {}
    for sample in (db.query(LocationSample)
                   .filter(LocationSample.company_id == principal.company_id, LocationSample.attendance_id.in_(ids),
                           LocationSample.status == "CAPTURED")
                   .order_by(LocationSample.received_at, LocationSample.id)):
        latest[sample.attendance_id] = sample
    missed = dict(db.query(LocationSample.attendance_id, func.count(LocationSample.id))
                  .filter(LocationSample.company_id == principal.company_id, LocationSample.attendance_id.in_(ids),
                          LocationSample.status == "MISSED")
                  .group_by(LocationSample.attendance_id).all())
    readable: set = set()
    if principal.has("location:read"):
        scope = authz.employee_scope(db, principal, "location:read")
        if not scope.is_empty:
            readable = {row[0] for row in db.query(Employee.id).filter(
                Employee.company_id == principal.company_id, Employee.id.in_([s.employee_id for s in sessions]),
                scope.clause(Employee.id))}
    out: Dict[int, dict] = {}
    for s in sessions:
        sample = latest.get(s.id)
        item = {"state": s.tracking_state, "stop_reason": s.tracking_stop_reason,
                "interval_minutes": s.tracking_interval_minutes,
                "schedule_state": location_service.schedule_state(s, now),
                "next_due_at": _iso(s.tracking_next_due_at), "missed": int(missed.get(s.id, 0)),
                "last_at": _iso(sample.captured_at or sample.received_at) if sample else None,
                "last_type": sample.sample_type if sample else None,
                "accuracy_m": sample.accuracy_m if sample else None, "can_see_location": s.employee_id in readable}
        if sample is not None and s.employee_id in readable and sample.redacted_at is None:
            item.update({"latitude": sample.latitude, "longitude": sample.longitude})
        out[s.id] = item
    return out


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------- punch for someone


def proxy_punch(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    from app.schemas.attendance import EventMeta
    from app.services import attendance_access, attendance_capture_policy, attendance_days, attendance_policy
    from app.services import attendance_service as svc
    from app.services.notification_service import NotificationService

    employee_id = data.get("employee_id")
    direction = data.get("direction")
    if direction not in ("IN", "OUT"):
        raise bad_request("Choose punch in or punch out.", "INVALID_FIELDS")
    if not isinstance(employee_id, int) or isinstance(employee_id, bool):
        raise bad_request("Choose the employee.", "INVALID_FIELDS")
    if authz.is_self(principal, employee_id):
        raise ar.self_edit_forbidden()
    employee = authz.ensure_employee_access(db, principal, "attendance:proxy_punch", employee_id)
    if not employee.is_active:
        raise conflict(f"{employee.full_name} is not active.", "EMPLOYEE_INACTIVE")
    company = db.get(svc.Company, principal.company_id)
    settings = settings_for(db, principal.company_id, employee_id=employee.id)
    if not settings["proxy_punch_enabled"]:
        raise ar.unprocessable("Team punches are turned off in the attendance settings.", "FEATURE_DISABLED")
    reason = (data.get("reason") or "").strip()
    if settings["reason_required"] and len(reason) < 3:
        raise bad_request("Say why you are punching for this person.", "REASON_REQUIRED")
    location = _location(data.get("location"), settings)

    svc.lock_employee(db, employee.id)
    key = data.get("idempotency_key")
    if key:
        previous = (db.query(svc.AttendanceEvent)
                    .filter(svc.AttendanceEvent.employee_id == employee.id,
                            svc.AttendanceEvent.idempotency_key == key).first())
        if previous is not None:
            return {"replayed": True, "attendance_id": previous.attendance_id, "employee_id": employee.id}
    now = svc._now()
    policy = attendance_policy.load(db, company.id)
    v2 = attendance_access.v2_enabled(db, company.id)
    multiple = attendance_capture_policy.effective(db, company.id, employee.id, now.date()).multiple_sessions_allowed \
        if v2 else False
    capture = svc.Capture(source="SUPERVISOR", multiple_sessions_allowed=multiple, actor_user_id=principal.user_id,
                          reason=(reason or "Team punch")[:500],
                          event_location_status="CAPTURED" if location is not None else "NOT_PROVIDED")
    meta = EventMeta(idempotency_key=key, client_sent_at=data.get("client_sent_at"))
    live = svc.live_session(db, employee.id, lock=True)
    if direction == "IN":
        record = svc._punch_in(db, principal, company, employee, policy, meta, None, now, source="supervisor",
                               capture=capture)
    else:
        record = svc._punch_out(db, principal, company, employee, policy, meta, None, now, live, source="supervisor",
                                capture=capture)
    event = (db.query(svc.AttendanceEvent)
             .filter(svc.AttendanceEvent.attendance_id == record.id,
                     svc.AttendanceEvent.event_type == ("PUNCH_IN" if direction == "IN" else "PUNCH_OUT"))
             .order_by(svc.AttendanceEvent.id.desc()).first())
    if location is not None:
        db.add(LocationSample(
            company_id=company.id, attendance_id=record.id, employee_id=employee.id,
            event_id=event.id if event else None, sample_type="PUNCH_IN" if direction == "IN" else "PUNCH_OUT",
            status="CAPTURED", latitude=location["latitude"], longitude=location["longitude"],
            accuracy_m=location.get("accuracy_m"), captured_at=now, received_at=now, punch_id=capture.punch_id,
            exception_reason=(f"{'Entered by hand' if location['source'] == 'MANUAL' else 'Device of'} "
                              f"{principal.user_id and _actor_name(db, principal)}"
                              + (f": {location['address']}" if location.get("address") else ""))[:500]))
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "PROXY_PUNCH",
                        f"{_actor_name(db, principal)} punched {employee.full_name} "
                        f"{'in' if direction == 'IN' else 'out'}", "attendance", record.id,
                        details={"employee_id": employee.id, "direction": direction,
                                 "location_source": location["source"] if location else None})
    if employee.user_id:
        NotificationService.create(
            db, employee.user_id, f"Punched {'in' if direction == 'IN' else 'out'} by your manager",
            f"{_actor_name(db, principal)} punched you {'in' if direction == 'IN' else 'out'} at "
            f"{now.astimezone(svc._tz(company)).strftime('%H:%M')}." + (f" Reason: {reason}" if reason else ""),
            "attendance", "/attendance")
    attendance_days.evaluate_dates(db, company.id, employee.id, [record.date],
                                   trigger="SESSION_CLOSED" if direction == "OUT" else "PUNCH", now=now)
    return {"attendance_id": record.id, "employee_id": employee.id, "direction": direction,
            "check_in_time": _iso(record.check_in_time), "check_out_time": _iso(record.check_out_time),
            "location_recorded": location is not None}


BULK_MAX = 50


def bulk_punch(db: Session, principal: Principal, data: Mapping[str, Any]) -> dict:
    """Punch several team members in or out at once (crew start / end of a shift): one reason and one location for
    the batch; each person is its own savepoint, so one refusal (already in, day complete, out of scope) never undoes
    the others. Returns a result per person and the counts."""
    from app.core.errors import AppError

    ids = data.get("employee_ids") or []
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise bad_request("Choose the people to punch.", "INVALID_FIELDS")
    if len(set(ids)) > BULK_MAX:
        raise bad_request(f"Punch at most {BULK_MAX} people at once.", "INVALID_FIELDS")
    batch = (data.get("idempotency_key") or "").strip()
    results = []
    for employee_id in dict.fromkeys(ids):  # once each, in the order chosen
        one = {"employee_id": employee_id, "direction": data.get("direction"), "reason": data.get("reason"),
               "location": data.get("location"), "client_sent_at": data.get("client_sent_at"),
               "idempotency_key": f"{batch[:40]}-{employee_id}" if batch else None}
        try:
            with db.begin_nested():
                outcome = proxy_punch(db, principal, one)
            results.append({"employee_id": employee_id, "ok": True, "attendance_id": outcome.get("attendance_id"),
                            "replayed": bool(outcome.get("replayed"))})
        except AppError as exc:
            if exc.status_code in (400, 422) and exc.code in ("INVALID_FIELDS", "REASON_REQUIRED", "LOCATION_REQUIRED",
                                                                "LOCATION_MANUAL_NOT_ALLOWED", "FEATURE_DISABLED"):
                raise  # a batch-level problem: nobody is punched
            results.append({"employee_id": employee_id, "ok": False, "code": exc.code, "message": exc.detail})
    done = sum(1 for r in results if r["ok"])
    return {"direction": data.get("direction"), "done": done, "failed": len(results) - done, "results": results}


def _actor_name(db: Session, principal: Principal) -> str:
    from app.models.user import User

    user = db.get(User, principal.user_id) if principal.user_id else None
    return user.full_name if user else "A manager"


def _location(raw: Any, settings: Mapping[str, Any]) -> Optional[dict]:
    mode = settings["proxy_punch_location"]
    if mode == "OFF":
        return None
    if not raw:
        if mode == "REQUIRED":
            raise ar.unprocessable("A location is required for team punches.", "LOCATION_REQUIRED")
        return None
    source = raw.get("source") or "DEVICE"
    if source not in ("DEVICE", "MANUAL"):
        raise bad_request("Location source must be DEVICE or MANUAL.", "INVALID_FIELDS")
    if source == "MANUAL" and not settings["allow_manual_location"]:
        raise ar.unprocessable("Only the device location is accepted.", "LOCATION_MANUAL_NOT_ALLOWED")
    try:
        lat, lng = float(raw.get("latitude")), float(raw.get("longitude"))
    except (TypeError, ValueError):
        raise bad_request("Give a latitude and a longitude.", "INVALID_FIELDS")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        raise bad_request("The coordinates are not valid.", "INVALID_FIELDS")
    accuracy = raw.get("accuracy_m")
    if accuracy is not None:
        try:
            accuracy = max(1.0, min(float(accuracy), 100_000.0))
        except (TypeError, ValueError):
            raise bad_request("Accuracy must be a number of metres.", "INVALID_FIELDS")
    address = (raw.get("address") or "").strip()[:200] or None
    # A position typed by hand has no measured accuracy; the sample stores a nominal 1 km (flagged as hand-entered).
    return {"latitude": lat, "longitude": lng, "accuracy_m": (accuracy or 100.0) if source == "DEVICE" else 1000.0,
            "source": source, "address": address}


# --------------------------------------------------------------------------- delete (void)


def delete_session(db: Session, principal: Principal, attendance_id: int, *, reason: str) -> dict:
    from app.services import attendance_access
    from app.services import attendance_service as svc

    reason = (reason or "").strip()
    if len(reason) < 5:
        raise bad_request("Say why this attendance is deleted (at least 5 characters).", "REASON_REQUIRED")
    record = db.query(Attendance).filter(Attendance.company_id == principal.company_id,
                                         Attendance.id == attendance_id).first()
    if record is None:
        raise not_found("Attendance record")
    if authz.is_self(principal, record.employee_id):
        raise ar.self_edit_forbidden()
    employee = authz.ensure_employee_access(db, principal, "attendance:regularize", record.employee_id)
    settings = settings_for(db, principal.company_id, employee_id=employee.id)
    if not settings["edit_enabled"] and not principal.has_company_scope("attendance:regularize"):
        raise forbidden("Managers cannot delete attendance in this company; ask HR.")
    if record.voided_at is not None:
        raise conflict("This attendance was already deleted.", "ALREADY_VOIDED")
    if record.check_out_time is None and record.closed_at is None:
        raise conflict("Punch the person out (or correct the session) before deleting a running session.",
                       "SESSION_LIVE")
    if attendance_access.v2_enabled(db, principal.company_id):
        row = ar.create(db, principal, {"employee_id": employee.id, "shift_date": record.date.isoformat(),
                                        "kind": "VOID_SESSION", "mode": "DIRECT", "session_id": record.id,
                                        "payload": {}, "reason_code": "DATA_CORRECTION", "comment": reason})
        return {"attendance_id": record.id, "voided": True, "request_id": row.id}
    svc.lock_employee(db, employee.id)
    record.voided_at, record.voided_by_user_id, record.void_reason = clock.utcnow(), principal.user_id, "ADMIN"
    (db.query(AttendancePunch)
     .filter(AttendancePunch.company_id == record.company_id, AttendancePunch.session_id == record.id)
     .update({AttendancePunch.disposition: "IGNORED_BY_ADMIN"}, synchronize_session=False))
    svc.record_version(db, record, "VOIDED", principal.user_id)
    svc._event(db, record, "VOIDED", svc._now(), "manual", principal.user_id, note=reason[:500])
    AuditLogService.log(db, principal, "Attendance", "SESSION_VOIDED",
                        f"Deleted attendance of {employee.full_name} on {record.date.isoformat()}", "attendance",
                        record.id, details={"employee_id": employee.id, "date": record.date.isoformat()})
    return {"attendance_id": record.id, "voided": True, "request_id": None}


__all__ = ["get_settings", "update_settings", "team_today", "proxy_punch", "bulk_punch", "delete_session",
           "settings_for"]
