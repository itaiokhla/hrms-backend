"""AS-M0 safe fixes (WI-AS-0.5; AS 13 §5): one regression test per BUG-AS id, failing on the old code.

BUG-AS-04 (form part), -05, -10, -14, -18, -20 (service validation), -22 here; -11, -13, -16, -17, -21 are UI fixes
covered by Jest and the e2e-as/00 spec; -63 is tests/payroll_unit/test_registry_versions.py.
"""

from datetime import datetime, timezone

import pytest
import pytz

from tests.attendance_helpers import IST, act, employee_with_login, fake_clock, meta, owner_rows, set_policy  # noqa: F401
from tests.helpers import create_employee, signup_company

DUBAI = pytz.timezone("Asia/Dubai")


@pytest.fixture
def alpha(app):
    admin = signup_company(app, "Alpha Night Ops")  # company zone Asia/Kolkata (signup default)
    hr = employee_with_login(app, admin, "Kavya", roles=("hr_admin",))
    sameer = create_employee(admin, "Sameer", "Khan")
    return {"admin": admin, "hr": hr, "sameer": sameer}


def _manual(client, **body):
    return client.post("/api/v1/attendance/manual", json=body)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


# --------------------------------------------------------------------------------------------- BUG-AS-04
def test_bug_as_04_absent_is_recorded_without_times_or_hours(alpha):
    r = _manual(alpha["hr"], employee_id=alpha["sameer"]["id"], date="2026-10-20", status="absent")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "absent"
    assert body["hours"]["net_seconds"] in (0, None) and body["hours"]["gross_seconds"] in (0, None)


def test_bug_as_04_present_still_needs_a_check_in(alpha):
    r = _manual(alpha["hr"], employee_id=alpha["sameer"]["id"], date="2026-10-20", status="present")
    assert r.status_code == 400 and r.json()["code"] == "CHECK_IN_REQUIRED"


# --------------------------------------------------------------------------------------------- BUG-AS-05
def test_bug_as_05_overnight_manual_entry_in_the_work_zone(alpha):
    """Kavya records Sameer's night 2026-10-26 18:30 -> 2026-10-27 03:30 IST (13 §4.1 exit 4 (b))."""
    r = _manual(alpha["hr"], employee_id=alpha["sameer"]["id"], date="2026-10-26", status="present",
                check_in_local="18:30", check_out_local="03:30", lunch_start_local="23:00", lunch_end_local="00:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert _utc(body["check_in_time"]) == IST.localize(datetime(2026, 10, 26, 18, 30)).astimezone(timezone.utc)
    assert _utc(body["check_out_time"]) == IST.localize(datetime(2026, 10, 27, 3, 30)).astimezone(timezone.utc)
    assert body["date"] == "2026-10-26"
    assert body["hours"]["gross_seconds"] == 9 * 3600 and body["hours"]["lunch_seconds"] == 3600
    lunch = owner_rows("SELECT started_at, ended_at FROM attendance_breaks WHERE attendance_id = :a", a=body["id"])
    assert lunch[0]["started_at"] == IST.localize(datetime(2026, 10, 26, 23, 0)).astimezone(timezone.utc)
    assert lunch[0]["ended_at"] == IST.localize(datetime(2026, 10, 27, 0, 0)).astimezone(timezone.utc)


def test_bug_as_05_local_times_follow_the_branch_zone_not_the_company(alpha, app):
    admin = alpha["admin"]
    entity = admin.post("/api/v1/legal-entities", json={"code": "LE12", "legal_name": "Alpha Gulf FZ-LLC",
                                                         "country_code": "AE", "base_currency": "AED",
                                                         "timezone": "Asia/Dubai"}).json()
    branch = admin.post("/api/v1/branches", json={"legal_entity_id": entity["id"], "code": "BR23",
                                                  "name": "Dubai Office", "timezone": "Asia/Dubai"}).json()
    farah = create_employee(admin, "Farah", "Ali", legal_entity_id=entity["id"], branch_id=branch["id"])
    r = _manual(alpha["hr"], employee_id=farah["id"], date="2026-10-26", status="present",
                check_in_local="09:00", check_out_local="18:00")
    assert r.status_code == 200, r.text
    assert _utc(r.json()["check_in_time"]) == DUBAI.localize(datetime(2026, 10, 26, 9, 0)).astimezone(timezone.utc)


def test_bug_as_05_instants_and_wall_times_are_not_mixed(alpha):
    r = _manual(alpha["hr"], employee_id=alpha["sameer"]["id"], date="2026-10-26", status="present",
                check_in_local="09:00", check_in_time="2026-10-26T09:00:00+05:30")
    assert r.status_code == 400 and r.json()["code"] == "MIXED_TIME_FORMATS"


# --------------------------------------------------------------------------------------------- BUG-AS-10
def test_bug_as_10_history_default_is_the_employees_local_today(app, fake_clock):
    admin = signup_company(app, "History Co")
    set_policy(admin, requireFaceForSelfCheckIn=False)
    emp = employee_with_login(app, admin, "Priya", hire_date="2026-01-05")
    fake_clock.at_ist("2026-06-15", "09:30")  # far from the server's real date
    assert act(emp, "PUNCH_IN", meta(fake_clock)).status_code == 200
    items = emp.get("/api/v1/attendance/me/history").json()["items"]
    assert [i["date"] for i in items] == ["2026-06-15"]


# --------------------------------------------------------------------------------------------- BUG-AS-14
@pytest.mark.parametrize("field", ["check_in_time", "check_out_time"])
def test_bug_as_14_naive_datetime_is_timezone_required(alpha, field):
    body = {"employee_id": alpha["sameer"]["id"], "date": "2026-10-20", "status": "present",
            "check_in_time": "2026-10-20T09:00:00+05:30", "check_out_time": "2026-10-20T18:00:00+05:30"}
    body[field] = body[field][:19]
    r = _manual(alpha["hr"], **body)
    assert r.status_code == 400 and r.json()["code"] == "TIMEZONE_REQUIRED", r.text


def test_bug_as_14_naive_break_is_timezone_required(alpha):
    r = _manual(alpha["hr"], employee_id=alpha["sameer"]["id"], date="2026-10-20", status="present",
                check_in_time="2026-10-20T09:00:00+05:30", check_out_time="2026-10-20T18:00:00+05:30",
                breaks=[{"started_at": "2026-10-20T13:00:00", "ended_at": "2026-10-20T14:00:00+05:30"}])
    assert r.status_code == 400 and r.json()["code"] == "TIMEZONE_REQUIRED", r.text


# --------------------------------------------------------------------------------------------- BUG-AS-18
def test_bug_as_18_inverted_range_is_invalid_range(alpha):
    r = alpha["hr"].get("/api/v1/attendance", params={"start_date": "2026-10-27", "end_date": "2026-10-20"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_RANGE"
    r = alpha["hr"].get("/api/v1/attendance/me/history", params={"start_date": "2026-10-27", "end_date": "2026-10-20"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_RANGE"
    ok = alpha["hr"].get("/api/v1/attendance", params={"start_date": "2026-10-20", "end_date": "2026-10-20"})
    assert ok.status_code == 200


# --------------------------------------------------------------------------------------------- BUG-AS-20
def test_bug_as_20_attendance_source_is_validated():
    from app.models.attendance import ATTENDANCE_SOURCES, Attendance

    assert set(ATTENDANCE_SOURCES) == {"self", "kiosk", "manual", "system", "device", "import", "request",
                                       "supervisor"}
    for source in ATTENDANCE_SOURCES:
        Attendance(source=source)
    with pytest.raises(ValueError):
        Attendance(source="selfie")


# --------------------------------------------------------------------------------------------- BUG-AS-22
def test_bug_as_22_unpaid_break_is_writable_and_readable(alpha):
    admin = alpha["admin"]
    body = {"name": "GEN", "start_time": "09:00", "end_time": "18:00", "duration_hours": "9",
            "unpaid_break_minutes": 60}
    r = admin.post("/api/v1/shifts", json=body)
    assert r.status_code == 200, r.text
    row = next(s for s in admin.get("/api/v1/shifts").json() if s["id"] == r.json()["id"])
    assert row["unpaid_break_minutes"] == 60
    assert admin.put(f"/api/v1/shifts/{row['id']}", json=dict(body, unpaid_break_minutes=45)).status_code == 200
    # From AS-M1 a legacy edit is a version from the owner's local tomorrow (A-24); today's mirror keeps 60.
    versions = admin.get(f"/api/v1/shifts/{row['id']}/versions").json()
    assert [v["unpaid_break_minutes"] for v in versions] == [60, 45]
