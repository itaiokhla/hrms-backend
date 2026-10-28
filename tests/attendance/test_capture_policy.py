"""ATTENDANCE_CAPTURE_POLICY on the policy framework (AS-M2 WI-AS-2.4; spine §6.2.1, §6.3; 13 note C-2; SP-AS-221,
SP-AS-226; AS-CAP-09).

* No version -> the LEGACY_V1 mapping of the company settings (read live); ``prepare-capture`` (the capture and face
  steps of attendance.v2_prepare) creates one LEGACY_V1 catch-all, idempotently, and encrypts legacy encodings.
* V-AS-02 does not apply to this type: a v1 tenant activates a STANDARD version; while not V2_ACTIVE the punch path
  clamps it (one session per date, no offline, interactive sources only).
* Versions saved through /attendance/policies are STANDARD; the write gate is attendance:manage_policy (a payroll
  writer without it is refused); /payroll/policies never lists attendance types.
* Branch-level resolution of a kiosk (AS-CAP-09) never matches employee rules.
"""

import pytest

from tests.attendance.capture_v2_helpers import activate_capture, rows, sql
from tests.attendance_helpers import employee_with_login, fake_clock, set_policy  # noqa: F401
from tests.helpers import signup_company


@pytest.fixture
def tenant(app):
    admin = signup_company(app, "Capture Policy Co")
    set_policy(admin, requireFaceForSelfCheckIn=True, locationPunchPolicy="OPTIONAL")
    emp = employee_with_login(app, admin, "Kiran")
    return {"admin": admin, "emp": emp, "emp_id": emp.me()["employee"]["id"]}


def test_no_version_falls_back_to_legacy_v1_and_prepare_creates_it(tenant, monkeypatch):
    admin, emp = tenant["admin"], tenant["emp"]
    mine = emp.get("/api/v1/me/attendance/capture").json()
    assert mine["profile"] == "LEGACY_V1" and mine["face_on_punch"] is True and mine["location_mode"] == "OPTIONAL"
    assert mine["sources"] == ["KIOSK", "WEB"] and mine["policy_id"] is None
    resolved = admin.get("/api/v1/attendance/policies/resolve",
                         params={"type": "ATTENDANCE_CAPTURE_POLICY", "employee_id": tenant["emp_id"]}).json()
    assert resolved["status"] == "NO_POLICY" and resolved["fallback"] == "LEGACY_V1"
    sql("INSERT INTO face_encodings (company_id, employee_id, encoding, is_primary, created_at, updated_at) "
        "VALUES (:c, :e, 'plain', true, now(), now())", c=admin.company_id, e=tenant["emp_id"])
    first = admin.post("/api/v1/attendance/setup/prepare-capture")
    assert first.status_code == 200, first.text
    assert first.json()["capture_policy_id"] and first.json()["encrypted"] == 1 and first.json()["legacy_marked"] == 1
    face = rows("SELECT encoding, encoding_enc, legacy_enrolled FROM face_encodings WHERE employee_id = :e",
                e=tenant["emp_id"])[0]
    assert face["encoding"] is None and face["encoding_enc"] is not None and face["legacy_enrolled"] is True
    again = admin.post("/api/v1/attendance/setup/prepare-capture").json()
    assert again == {"capture_policy_id": None, "encrypted": 0, "legacy_marked": 0}
    resolved = admin.get("/api/v1/attendance/policies/resolve",
                         params={"type": "ATTENDANCE_CAPTURE_POLICY", "employee_id": tenant["emp_id"]}).json()
    assert resolved["status"] == "RESOLVED" and resolved["version"]["fields"]["profile"] == "LEGACY_V1"
    mine = emp.get("/api/v1/me/attendance/capture").json()
    assert mine["profile"] == "LEGACY_V1" and mine["policy_id"] == first.json()["capture_policy_id"]
    set_policy(admin, requireFaceForSelfCheckIn=False)  # the legacy settings page keeps working (read live)
    assert emp.get("/api/v1/me/attendance/capture").json()["face_on_punch"] is False
    listed = admin.get("/api/v1/attendance/policies").json()
    assert [p["type"] for p in listed] == ["ATTENDANCE_CAPTURE_POLICY"]
    payroll = admin.get("/api/v1/payroll/policies").json()
    assert all(p["type"] != "ATTENDANCE_CAPTURE_POLICY" for p in payroll)


def test_sp_as_221_v1_tenant_activates_standard_and_the_punch_path_clamps_it(tenant, fake_clock):
    """V-AS-02 skips the capture type; not V2_ACTIVE -> multiple_sessions_allowed false, offline OFF, sources ∩
    {WEB, MOBILE, KIOSK}; capture_required is carried (SP-AS-226)."""
    fake_clock.at_ist("2026-11-03", "08:00")  # after the rules' start (the payroll service dates them on the real day)
    policy = activate_capture(tenant["admin"], {"allowed_sources": ["WEB", "MOBILE", "DEVICE"],
                                                "offline_punches": "ALLOW", "capture_required": False})
    detail = tenant["admin"].get(f"/api/v1/attendance/policies/{policy['id']}").json()
    assert detail["active_version"]["fields"]["profile"] == "STANDARD"
    mine = tenant["emp"].get("/api/v1/me/attendance/capture").json()
    assert mine["profile"] == "STANDARD" and mine["sources"] == ["MOBILE", "WEB"]
    assert mine["multiple_sessions_allowed"] is False and mine["capture_required"] is False
    assert mine["face_on_punch"] is False  # STANDARD fields, not the legacy setting
    fake_clock.at_ist("2026-11-03", "09:00")
    from tests.attendance.capture_v2_helpers import v2_punch

    assert v2_punch(tenant["emp"], fake_clock, capture_source="MOBILE").status_code == 200
    fake_clock.at_ist("2026-11-03", "12:00")
    assert v2_punch(tenant["emp"], fake_clock, "OUT", capture_source="MOBILE").status_code == 200
    fake_clock.at_ist("2026-11-03", "14:00")
    second = v2_punch(tenant["emp"], fake_clock, capture_source="MOBILE")
    assert second.status_code == 409 and second.json()["code"] == "ALREADY_CHECKED_IN"  # one session per date (v1)


def test_versions_saved_here_are_standard_and_the_write_gate_is_manage_policy(tenant, app):
    admin = tenant["admin"]
    r = admin.post("/api/v1/attendance/policies", json={"type": "ATTENDANCE_CAPTURE_POLICY", "name": "Typed legacy",
                                                        "fields": {"profile": "LEGACY_V1"}})
    assert r.status_code == 201
    assert r.json()["versions"][0]["fields"]["profile"] == "STANDARD"
    wrong = admin.post("/api/v1/attendance/policies", json={"type": "OVERTIME_POLICY", "name": "No"})
    assert wrong.status_code == 400 and wrong.json()["code"] == "UNKNOWN_POLICY_TYPE"
    invalid = admin.post("/api/v1/attendance/policies", json={
        "type": "ATTENDANCE_CAPTURE_POLICY", "name": "Blocking without location", "fields": {"geofence_mode": "BLOCK"}})
    assert invalid.status_code == 400 and invalid.json()["code"] == "INVALID_FIELDS"  # AS-GEO-05
    payroll = employee_with_login(app, admin, "Pradeep", roles=("payroll_manager",))
    refused = payroll.post("/api/v1/attendance/policies", json={"type": "ATTENDANCE_CAPTURE_POLICY", "name": "P"})
    assert refused.status_code == 403
    via_payroll = payroll.post("/api/v1/payroll/policies", json={"type": "ATTENDANCE_CAPTURE_POLICY", "name": "P2"})
    assert via_payroll.status_code == 403  # the type's write gate also holds on the payroll route


def test_as_cap_09_kiosk_branch_resolution_ignores_employee_rules(tenant):
    from datetime import date

    from app.database.db import SessionLocal, bind_tenant
    from app.services import attendance_capture_policy as cp

    admin = tenant["admin"]
    branch = admin.get("/api/v1/branches").json()[0]
    activate_capture(admin, {"kiosk_methods": ["PIN", "QR"]}, criteria={"branch_ids": [branch["id"]]}, priority=400,
                     name="Branch kiosk")
    activate_capture(admin, {"kiosk_methods": ["CARD"]}, criteria={"employee_ids": [tenant["emp_id"]]},
                     priority=1000, name="Kiran only")
    db = SessionLocal()
    try:
        bind_tenant(db, admin.company_id)
        kiosk = cp.for_branch(db, admin.company_id, branch["id"], date(2026, 11, 3))
        assert kiosk.kiosk_methods == {"PIN", "QR"} and kiosk.profile == "STANDARD"
        assert cp.for_branch(db, admin.company_id, None, date(2026, 11, 3)).profile == "LEGACY_V1"
    finally:
        db.close()
