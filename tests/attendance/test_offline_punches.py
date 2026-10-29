"""Offline punch queue (AS-M12 / G-24; 04-attendance/01 §6.2; GV-AS-CAP-08).

* A queued OUT sent before its IN is processed after it (O-3); both get ``OFFLINE_CAPTURED`` and their reconstructed
  instants (server sync + monotonic delta), the session runs from the IN's capture time.
* An item older than ``offline_max_age_hours`` -> per-item ``OFFLINE_PUNCH_TOO_OLD`` with the missed-punch next action;
  one more than 120 s in the future -> ``OFFLINE_PUNCH_FUTURE``; the other items still go through.
* A wall clock that disagrees with the reconstruction flags ``CLOCK_SKEW``; a re-upload is a replay (no new punch).
* ``offline_punches = OFF`` -> 422 ``OFFLINE_PUNCHES_DISABLED``; more than 50 items -> 400 ``TOO_MANY_ITEMS``.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.attendance.capture_v2_helpers import activate_capture, punches, rows
from tests.attendance.test_attendance_days import enable_v2
from tests.attendance_helpers import employee_with_login, key
from tests.helpers import signup_company

FIELDS = {"allowed_sources": ["WEB", "MOBILE"], "location_mode": "OFF", "offline_punches": "ALLOW",
          "offline_max_age_hours": 24, "multiple_sessions_allowed": True}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


@pytest.fixture
def worker(app):
    admin = signup_company(app, "Offline Works")
    enable_v2(admin.company_id)
    activate_capture(admin, FIELDS, name="Field crew")
    emp = employee_with_login(app, admin, "Prakash")
    return {"admin": admin, "emp": emp, "id": emp.me()["employee"]["id"]}


def item(direction: str, captured: datetime, **extra) -> dict:
    sync = captured - timedelta(minutes=20)
    return {"idempotency_key": key(), "direction": direction, "capture_source": "MOBILE",
            "server_time_at_sync": _iso(sync), "monotonic_elapsed_ms": 20 * 60 * 1000,
            "device_wall_clock_at_capture": _iso(captured), **extra}


def test_gv_as_cap_08_order_age_future_and_replay(worker):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    started, ended = now - timedelta(hours=3, minutes=6), now - timedelta(minutes=30)
    out_first, in_second = item("OUT", ended), item("IN", started)
    too_old = item("IN", now - timedelta(hours=63, minutes=50))
    future = item("OUT", now + timedelta(minutes=10))
    r = worker["emp"].post("/api/v1/attendance/me/offline-punches",
                           json={"items": [out_first, in_second, too_old, future], "client_sent_at": _iso(now)})
    assert r.status_code == 200, r.text
    body = r.json()
    by_key = {i["idempotency_key"]: i for i in body["items"]}
    assert by_key[in_second["idempotency_key"]]["ok"] and by_key[out_first["idempotency_key"]]["ok"], body
    old = by_key[too_old["idempotency_key"]]
    assert old["ok"] is False and old["code"] == "OFFLINE_PUNCH_TOO_OLD" and old["next_action"]["kind"] == "MISSED_PUNCH"
    assert by_key[future["idempotency_key"]]["code"] == "OFFLINE_PUNCH_FUTURE"
    assert body["accepted"] == 2 and body["failed"] == 2
    stored = punches(worker["id"])
    assert [p["direction"] for p in stored] == ["IN", "OUT"]
    assert all("OFFLINE_CAPTURED" in (p["evidence_flags"] or []) for p in stored)
    session = rows("SELECT check_in_time, check_out_time FROM attendance WHERE employee_id = :e", e=worker["id"])[0]
    assert session["check_in_time"] == started and session["check_out_time"] == ended
    # O-8: the same queue uploaded again changes nothing.
    again = worker["emp"].post("/api/v1/attendance/me/offline-punches", json={"items": [out_first, in_second]})
    assert again.status_code == 200, again.text
    assert len(punches(worker["id"])) == 2


def test_clock_skew_flag_disabled_policy_and_batch_limit(app, worker):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    skewed = item("IN", now - timedelta(hours=1), device_wall_clock_at_capture=_iso(now - timedelta(hours=3)))
    r = worker["emp"].post("/api/v1/attendance/me/offline-punches", json={"items": [skewed]})
    assert r.status_code == 200 and r.json()["items"][0]["ok"], r.text
    assert "CLOCK_SKEW" in r.json()["items"][0]["flags"]
    assert "CLOCK_SKEW" in (punches(worker["id"])[0]["evidence_flags"] or [])
    assert rows("SELECT count(*) AS n FROM attendance_review_items WHERE kind = 'DEVICE_CLOCK_SKEW'")[0]["n"] == 0
    r = worker["emp"].post("/api/v1/attendance/me/offline-punches",
                           json={"items": [item("IN", now - timedelta(minutes=i + 1)) for i in range(51)]})
    assert r.status_code in (400, 422)
    other = signup_company(app, "Online Only Co")
    enable_v2(other.company_id)
    emp = employee_with_login(app, other, "Nitin")
    r = emp.post("/api/v1/attendance/me/offline-punches", json={"items": [item("IN", now - timedelta(minutes=5))]})
    assert r.status_code == 422 and r.json()["code"] == "OFFLINE_PUNCHES_DISABLED"
