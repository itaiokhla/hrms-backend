"""Offline punch queue (AS-M12 / G-24; AS 04-attendance/01 §6.2 O-1..O-10; spine §5.5.5).

``POST /attendance/me/offline-punches`` uploads up to 50 items the phone queued without network:

* **O-1** the employee's capture policy must have ``offline_punches = ALLOW`` (else 422 ``OFFLINE_PUNCHES_DISABLED``;
  v1 tenants are clamped to OFF).
* **O-2 / O-5** the capture instant is ``server_time_at_sync + monotonic_elapsed_ms``; the device wall clock is used
  only without a monotonic reconstruction and then flags ``CLOCK_SKEW``, as does a wall clock that disagrees with the
  reconstruction by more than ``MAX_CLOCK_SKEW_SECONDS``. Every item is flagged ``OFFLINE_CAPTURED``.
* **O-3** items are processed in capture order (ties by queue position), so an OUT sent before its IN still follows it.
* **O-4** age ``received_at − captured_at`` must be within ``offline_max_age_hours`` (else per-item 422
  ``OFFLINE_PUNCH_TOO_OLD``: file a missed-punch request); more than 120 s in the future is ``OFFLINE_PUNCH_FUTURE``.
* **O-6** each item runs the punch pipeline as if at its capture instant (``clock.at``), face / QR / photo skipped
  (``OFFLINE_UNVERIFIED`` when the policy asked for them); a failing item gets its own issue, the others continue.
* **O-8** each item's idempotency key makes a re-upload a replay; **O-9** phone skew never opens a review item.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, bad_request, forbidden
from app.core.principal import Principal

MAX_ITEMS = 50
MAX_CLOCK_SKEW_SECONDS = 120
FUTURE_TOLERANCE = timedelta(seconds=120)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def captured_at(item: Mapping[str, Any]) -> tuple:
    """(capture instant, flags) of one queued item (O-2, O-5)."""
    flags = ["OFFLINE_CAPTURED"]
    sync = _aware(item.get("server_time_at_sync"))
    elapsed = item.get("monotonic_elapsed_ms")
    wall = _aware(item.get("device_wall_clock_at_capture") or item.get("client_captured_at"))
    if sync is not None and isinstance(elapsed, (int, float)) and elapsed >= 0:
        at = sync + timedelta(milliseconds=int(elapsed))
        if wall is not None and abs((wall - at).total_seconds()) > MAX_CLOCK_SKEW_SECONDS:
            flags.append("CLOCK_SKEW")
    elif wall is not None:
        at = wall
        flags.append("CLOCK_SKEW")  # no monotonic reconstruction: the phone clock alone is not trusted
    else:
        raise bad_request("Each item needs its capture time.", "INVALID_FIELDS")
    return at.replace(microsecond=0), tuple(flags)


def upload(db: Session, principal: Principal, items: List[Mapping[str, Any]], client_sent_at: Optional[datetime],
           ip: Optional[str]) -> dict:
    from app.schemas.attendance_capture import PunchMeta
    from app.services import attendance_capture, attendance_capture_policy, org_structure_service

    if not principal.has("attendance:mark"):
        raise forbidden()
    if len(items) > MAX_ITEMS:
        raise bad_request(f"Send at most {MAX_ITEMS} queued punches at once.", "TOO_MANY_ITEMS")
    employee = authz.own_employee(db, principal)
    today = org_structure_service.employee_local_today(db, employee)
    policy = attendance_capture_policy.effective(db, principal.company_id, employee.id, today)
    if policy.offline_punches != "ALLOW":
        raise AppError(422, "Offline punches are not enabled for you.", "OFFLINE_PUNCHES_DISABLED")
    received = clock.utcnow().replace(microsecond=0)
    max_age = timedelta(hours=policy.offline_max_age_hours)
    prepared = []
    for position, item in enumerate(items):
        try:
            at, flags = captured_at(item)
        except AppError as exc:
            prepared.append((None, position, item, (), exc))
            continue
        prepared.append((at, position, item, flags, None))
    # O-3: capture order, ties by queue position (failed parses last, in queue order).
    prepared.sort(key=lambda p: (p[0] is None, p[0] or received, p[1]))
    results: List[Dict[str, Any]] = []
    for at, position, item, flags, problem in prepared:
        key = item.get("idempotency_key")
        base = {"position": position, "idempotency_key": key, "direction": item.get("direction"),
                "captured_at": at.isoformat() if at else None, "flags": list(flags)}
        if problem is not None:
            results.append({**base, "ok": False, "code": problem.code, "message": problem.detail})
            continue
        if at > received + FUTURE_TOLERANCE:
            results.append({**base, "ok": False, "code": "OFFLINE_PUNCH_FUTURE",
                            "message": "This punch is dated in the future: check the phone's clock."})
            continue
        if received - at > max_age:
            results.append({**base, "ok": False, "code": "OFFLINE_PUNCH_TOO_OLD",
                            "message": "Too old to sync. Raise a missed-punch request.",
                            "next_action": {"kind": "MISSED_PUNCH", "shift_date": at.date().isoformat(),
                                            "direction": item.get("direction")}})
            continue
        try:
            meta = PunchMeta(direction=item.get("direction"), capture_source=item.get("capture_source") or "MOBILE",
                             idempotency_key=key, location=item.get("location"),
                             location_consent=bool(item.get("location_consent", True)),
                             exception_reason=item.get("exception_reason"),
                             work_location_choice=item.get("work_location_choice"),
                             break_kind=item.get("break_kind"), confirm_close_lunch=True)
        except Exception as exc:  # noqa: BLE001 - pydantic: a malformed item is that item's issue
            results.append({**base, "ok": False, "code": "INVALID_FIELDS", "message": str(exc)[:300]})
            continue
        try:
            with db.begin_nested(), clock.at(at):
                outcome = attendance_capture.punch(db, principal, meta, None, ip, offline_flags=flags)
            results.append({**base, "ok": True, "punch_id": outcome.get("punch_id"),
                            "attendance_id": (outcome.get("record") or {}).get("id") or outcome.get("id"),
                            "replayed": outcome.get("punch_id") is not None and bool(outcome.get("replayed"))})
        except AppError as exc:
            results.append({**base, "ok": False, "code": exc.code, "message": exc.detail if isinstance(exc.detail, str)
                            else (exc.detail or {}).get("message", "Refused")})
    db.flush()
    accepted = sum(1 for r in results if r["ok"])
    results.sort(key=lambda r: r["position"])
    return {"received_at": received.isoformat(), "accepted": accepted, "failed": len(results) - accepted,
            "client_sent_at": client_sent_at.isoformat() if client_sent_at else None, "items": results}
