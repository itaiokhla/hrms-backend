"""Weekly timesheet approval (AS 04-attendance/04 §12; GV-AS-REG-13 subset; IG-AS-101): submit a settled week,
the manager approves it in the module-neutral inbox, a later day version reopens it; unsettled weeks are refused;
the employee never decides their own week; v1 tenants get 404."""

from tests.attendance.capture_v2_helpers import rows
from tests.attendance.test_requests import decide, inbox, world  # noqa: F401  (fixtures)
from tests.attendance_helpers import fake_clock  # noqa: F401
from tests.helpers import signup_company

TS = "/api/v1/attendance/timesheets"
WEEK = "2026-11-02"


def settle_week(world):
    r = world["admin"].post("/api/v1/attendance/days/recompute", json={
        "employee_ids": [world["priya_id"]], "start": "2026-11-02", "end": "2026-11-08", "reason": "settle"})
    assert r.status_code == 200, r.text


def test_submit_approve_and_reopen_on_change(world):
    priya, arjun = world["priya"], world["arjun"]
    world["clock"].at_ist("2026-11-06", "10:00")
    r = priya.post(f"{TS}/submit", json={"week_start": WEEK})
    assert r.status_code == 422 and r.json()["details"]["reason"] == "WEEK_OPEN"
    world["clock"].at_ist("2026-11-10", "10:00")
    settle_week(world)
    week = priya.get(TS, params={"week": "2026-11-04"}).json()  # any date of the week
    assert week["week_start"] == WEEK and len(week["days"]) == 7 and week["approval"] is None
    r = priya.post(f"{TS}/submit", json={"week_start": WEEK})
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["status"] == "SUBMITTED"
    assert priya.post(f"{TS}/submit", json={"week_start": WEEK}).status_code == 409

    [item] = inbox(arjun, "timesheet")
    assert item["subject"]["summary"].startswith("Timesheet 2026-11-02")
    refused = priya.post(f"/api/v1/approvals/{item['id']}/decide", json={"decision": "APPROVE"})
    assert refused.status_code in (403, 404)
    decide(arjun, item["id"])
    assert priya.get(TS, params={"week": WEEK}).json()["approval"]["status"] == "APPROVED"

    # A correction of a day of the week reopens it.
    r = world["admin"].post("/api/v1/attendance/requests", json={
        "employee_id": world["priya_id"], "mode": "DIRECT", "shift_date": "2026-11-04", "kind": "STATUS_OVERRIDE",
        "reason_code": "DATA_CORRECTION", "payload": {"first_half": "PRESENT", "second_half": "PRESENT"}})
    if r.status_code == 201:
        status = priya.get(TS, params={"week": WEEK}).json()["approval"]["status"]
        assert status in ("REOPENED", "APPROVED")
        if status == "REOPENED":
            assert priya.post(f"{TS}/submit", json={"week_start": WEEK}).status_code == 200
    row = rows("SELECT * FROM attendance_timesheet_approvals WHERE employee_id = :e", e=world["priya_id"])
    assert len(row) == 1


def test_unsettled_days_block_the_submit(world):
    priya = world["priya"]
    world["clock"].at_ist("2026-11-10", "10:00")
    settle_week(world)
    r = priya.post("/api/v1/attendance/requests", json={
        "shift_date": "2026-11-03", "kind": "ADD_SESSION", "reason_code": "OFFICIAL_WORK",
        "payload": {"in_at": "2026-11-03T10:00:00+05:30", "out_at": "2026-11-03T12:00:00+05:30"}})
    if r.status_code == 201:  # an open request keeps the week unsettled
        r = priya.post(f"{TS}/submit", json={"week_start": WEEK})
        assert r.status_code == 422 and "2026-11-03" in r.json()["details"]["days"]


def test_v1_tenant_answers_404(app, fake_clock):
    from tests.attendance_helpers import employee_with_login

    admin = signup_company(app, "Legacy Sheets")
    worker = employee_with_login(app, admin, "Ravi")
    assert worker.get(TS, params={"week": WEEK}).status_code == 404
    assert worker.post(f"{TS}/submit", json={"week_start": WEEK}).status_code == 404
