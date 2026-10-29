"""Team attendance (IG-AS-98..100): settings, the team board, punching for a team member with a location, and
deleting (voiding) a session — scoped by the reporting line and roles, never on oneself.
"""

import uuid
from datetime import date

import pytest

from tests.attendance.capture_v2_helpers import rows, sql
from tests.attendance.test_requests import enable_v2
from tests.attendance_helpers import employee_with_login, fake_clock  # noqa: F401
from tests.helpers import create_employee, invite_and_activate, signup_company

TEAM = "/api/v1/attendance/team"


@pytest.fixture
def team(app, fake_clock):
    fake_clock.at_ist("2026-11-02", "08:00")
    admin = signup_company(app, "Team Co")
    arjun_e = create_employee(admin, "Arjun", "Mehta")
    arjun = invite_and_activate(app, admin, arjun_e, ["manager"])
    priya = employee_with_login(app, admin, "Priya", manager_id=arjun_e["id"])
    other = employee_with_login(app, admin, "Omar")  # not Arjun's report
    return {"app": app, "admin": admin, "cid": admin.company_id, "arjun": arjun, "arjun_id": arjun_e["id"],
            "priya": priya, "priya_id": priya.employee["id"], "other": other, "other_id": other.employee["id"],
            "clock": fake_clock}


def enable(admin, **extra):
    r = admin.put(f"{TEAM}/settings", json={"proxy_punch_enabled": True, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def punch(client, employee_id, direction="IN", **extra):
    body = {"employee_id": employee_id, "direction": direction, "reason": "Phone battery died",
            "idempotency_key": uuid.uuid4().hex,
            "location": {"source": "DEVICE", "latitude": 12.9716, "longitude": 77.5946, "accuracy_m": 18}, **extra}
    return client.post(f"{TEAM}/punch", json=body)


def test_settings_default_off_and_only_policy_managers_change_them(team):
    admin, arjun = team["admin"], team["arjun"]
    current = arjun.get(f"{TEAM}/settings").json()
    assert current["proxy_punch_enabled"] is False and current["proxy_punch_location"] == "REQUIRED"
    assert current["can_manage"] is False
    assert arjun.put(f"{TEAM}/settings", json={"proxy_punch_enabled": True}).status_code == 403
    saved = enable(admin, proxy_punch_location="OPTIONAL", reason_required=False)
    assert saved["proxy_punch_enabled"] and saved["proxy_punch_location"] == "OPTIONAL" and not saved["reason_required"]
    assert arjun.get(f"{TEAM}/settings").json()["proxy_punch_enabled"] is True
    assert admin.put(f"{TEAM}/settings", json={"proxy_punch_location": "SOMETIMES"}).status_code == 422


def test_manager_punches_a_report_in_and_out_with_device_and_manual_location(team):
    admin, arjun, priya_id = team["admin"], team["arjun"], team["priya_id"]
    r = punch(arjun, priya_id)
    assert r.status_code == 422 and r.json()["code"] == "FEATURE_DISABLED"
    enable(admin)
    board = arjun.get(f"{TEAM}/today").json()
    assert board["can_punch"] and [i["employee_id"] for i in board["items"]] == [priya_id]  # only the report
    assert board["items"][0]["state"] == "NOT_PUNCHED_IN"

    r = punch(arjun, priya_id, location=None)
    assert r.status_code == 422 and r.json()["code"] == "LOCATION_REQUIRED"
    r = punch(arjun, priya_id, reason="")
    assert r.status_code == 400 and r.json()["code"] == "REASON_REQUIRED"
    r = punch(arjun, priya_id)
    assert r.status_code == 200, r.text
    session_id = r.json()["attendance_id"]
    session = rows("SELECT * FROM attendance WHERE id = :i", i=session_id)[0]
    assert session["capture_source"] == "SUPERVISOR" and session["recorded_by_user_id"] == arjun.user_id
    raw = rows("SELECT * FROM attendance_punches WHERE session_id = :i", i=session_id)[0]
    assert raw["source"] == "SUPERVISOR" and raw["actor_user_id"] == arjun.user_id and raw["reason"]
    sample = rows("SELECT * FROM location_samples WHERE attendance_id = :i", i=session_id)[0]
    assert sample["status"] == "CAPTURED" and abs(sample["latitude"] - 12.9716) < 1e-6
    assert arjun.get(f"{TEAM}/today").json()["items"][0]["state"] == "WORKING"
    # The employee sees it and is told.
    notes = rows("SELECT title FROM notifications WHERE user_id = :u", u=team["priya"].user_id)
    assert any("Punched in by your manager" in n["title"] for n in notes)
    assert punch(arjun, priya_id).json()["code"] == "ALREADY_CHECKED_IN"

    team["clock"].at_ist("2026-11-02", "17:30")
    r = punch(arjun, priya_id, "OUT", location={"source": "MANUAL", "latitude": 12.98, "longitude": 77.6,
                                                "address": "Client office, MG Road"})
    assert r.status_code == 200, r.text
    out = rows("SELECT * FROM location_samples WHERE attendance_id = :i ORDER BY id", i=session_id)[-1]
    assert out["sample_type"] == "PUNCH_OUT" and "MG Road" in out["exception_reason"]
    assert rows("SELECT check_out_time FROM attendance WHERE id = :i", i=session_id)[0]["check_out_time"]
    audit = rows("SELECT action FROM audit_logs WHERE entity_type = 'attendance' AND entity_id = :i", i=str(session_id))
    assert [a["action"] for a in audit].count("PROXY_PUNCH") == 2

    # Manual location can be switched off.
    admin.put(f"{TEAM}/settings", json={"allow_manual_location": False})
    team["clock"].at_ist("2026-11-03", "09:00")
    r = punch(arjun, priya_id, location={"source": "MANUAL", "latitude": 12.98, "longitude": 77.6})
    assert r.status_code == 422 and r.json()["code"] == "LOCATION_MANUAL_NOT_ALLOWED"


def test_scope_self_and_permissions(team):
    admin, arjun, priya = team["admin"], team["arjun"], team["priya"]
    enable(admin)
    assert punch(arjun, team["other_id"]).status_code == 404            # not in Arjun's reporting line
    r = punch(arjun, team["arjun_id"])
    assert r.status_code == 403 and r.json()["code"] == "SELF_EDIT_FORBIDDEN"
    assert punch(priya, team["other_id"]).status_code in (403, 404)     # an employee holds no team punch
    assert priya.get(f"{TEAM}/today").status_code in (200, 403)
    if priya.get(f"{TEAM}/today").status_code == 200:
        assert priya.get(f"{TEAM}/today").json()["items"] == []
    # HR (company scope) reaches everyone.
    assert punch(admin, team["other_id"]).status_code == 200


def test_manager_deletes_a_report_session_and_never_a_live_one(team):
    admin, arjun, priya_id = team["admin"], team["arjun"], team["priya_id"]
    enable(admin)
    session_id = punch(arjun, priya_id).json()["attendance_id"]
    r = arjun.post(f"/api/v1/attendance/sessions/{session_id}/void", json={"reason": "Punched by mistake"})
    assert r.status_code == 409 and r.json()["code"] == "SESSION_LIVE"
    team["clock"].at_ist("2026-11-02", "12:00")
    punch(arjun, priya_id, "OUT")
    assert arjun.post(f"/api/v1/attendance/sessions/{session_id}/void", json={"reason": "x"}).status_code == 422
    r = arjun.post(f"/api/v1/attendance/sessions/{session_id}/void", json={"reason": "Punched by mistake"})
    assert r.status_code == 200, r.text
    session = rows("SELECT * FROM attendance WHERE id = :i", i=session_id)[0]
    assert session["voided_at"] is not None and session["void_reason"] == "ADMIN"
    assert {p["disposition"] for p in rows("SELECT disposition FROM attendance_punches WHERE session_id = :i",
                                           i=session_id)} == {"IGNORED_BY_ADMIN"}
    listed = admin.get("/api/v1/attendance", params={"start_date": "2026-11-02", "end_date": "2026-11-02"}).json()
    assert session_id not in [x["id"] for x in listed["items"]]  # readers exclude voided sessions
    r = arjun.post(f"/api/v1/attendance/sessions/{session_id}/void", json={"reason": "Punched by mistake"})
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_VOIDED"
    # Out of scope and own sessions.
    omar = punch(admin, team["other_id"]).json()["attendance_id"]
    team["clock"].at_ist("2026-11-02", "13:00")
    punch(admin, team["other_id"], "OUT")
    assert arjun.post(f"/api/v1/attendance/sessions/{omar}/void", json={"reason": "Not mine"}).status_code == 404
    # Managers can be stopped from deleting.
    admin.put(f"{TEAM}/settings", json={"edit_enabled": False})
    assert arjun.post(f"/api/v1/attendance/sessions/{omar}/void", json={"reason": "Not mine"}).status_code in (403, 404)


def test_v2_delete_is_a_direct_void_request(team):
    admin, arjun, priya_id = team["admin"], team["arjun"], team["priya_id"]
    enable_v2(team["cid"])
    enable(admin)
    team["clock"].at_ist("2026-11-02", "08:00")
    session_id = punch(arjun, priya_id).json()["attendance_id"]
    team["clock"].at_ist("2026-11-02", "17:00")
    punch(arjun, priya_id, "OUT")
    r = admin.post(f"/api/v1/attendance/sessions/{session_id}/void", json={"reason": "Duplicate entry"})
    assert r.status_code == 200, r.text
    assert r.json()["request_id"]
    request = rows("SELECT * FROM attendance_requests WHERE id = :i", i=r.json()["request_id"])[0]
    assert request["kind"] == "VOID_SESSION" and request["mode"] == "DIRECT" and request["status"] == "APPROVED"
    assert rows("SELECT void_reason FROM attendance WHERE id = :i", i=session_id)[0]["void_reason"] == "REQUEST"
    day = rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = :d", e=priya_id,
               d=date(2026, 11, 2))
    assert day and day[0]["session_count"] == 0


def test_tenant_isolation(team, app):
    enable(team["admin"])
    stranger = signup_company(app, "Stranger Co")
    assert punch(stranger, team["priya_id"]).status_code in (404, 422)
    sql("SELECT 1")
