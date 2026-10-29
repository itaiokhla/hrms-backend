"""G-16 / X-M5: split attendance cycles in the snapshot (JC-AS-10), the unified cutoff assistant (X 03 §5) and the
automatic retro of corrections that reach a finalized period (JC-AS-11; AS-PAY-05, -06, X-INT-43..49 subset).

* v1 tenants keep the blocking ATTENDANCE_CYCLE_UNSUPPORTED; attendance v2 tenants map the window onto the period
  (period dates, window facts) and the period state of a date follows the window that holds it;
* GET /cutoff lists pending attendance requests and leave with the configured severity, the lock state and the
  attendance window, 404 for another tenant's pay group, 403 without a payroll or close-period code;
* a session corrected on a finalized date enqueues payroll.retro.detect (key retro:attendance:{id}:{version}) that
  runs retro.detect for that period — the finalized run is never edited; the run employee's retro breakdown answers.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from tests.payroll.test_retro import retro_world, working_day  # noqa: F401  (fixture)
from tests.payroll.test_run_prechecks import pending_leave
from tests.payroll.test_runs_lifecycle import new_run, runs_world, session_for  # noqa: F401
from tests.payroll.test_snapshot_builder import built, emma
from tests.payroll.test_wage_inputs import set_setting, sql


def enable_v2(company_id):
    set_setting(company_id, "attendance.v2.enabled", True)


def split_periods(group_id, days=5):
    """Every period of the pay group gets a window that starts and ends ``days`` earlier (a 26th–25th-like cycle)."""
    sql("UPDATE pay_periods SET attendance_start = period_start - :n, attendance_end = period_end - :n "
        "WHERE pay_group_id = :g", n=days, g=group_id)


def test_jc_as_10_split_cycle_blocks_v1_and_maps_the_window_for_v2(runs_world):
    w = runs_world
    set_setting(w["cid"], "payroll.attendance.enabled", True)
    split_periods(w["group"])
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    v1 = built(w["cid"], run["id"], emma(w), probe=True)
    assert "ATTENDANCE_CYCLE_UNSUPPORTED" in [i.code for i in v1.issues if i.severity == "BLOCKING"]

    enable_v2(w["cid"])
    v2 = built(w["cid"], run["id"], emma(w), probe=True)
    assert "ATTENDANCE_CYCLE_UNSUPPORTED" not in [i.code for i in v2.issues]
    start, end = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    assert [d["date"] for d in v2.content["days"]] == [(start + timedelta(days=i)).isoformat()
                                                        for i in range((end - start).days + 1)]

    # The period state of a date follows the window that holds it: the last 5 days are the next period's input.
    from app.payroll.services import calendars

    sql("UPDATE pay_periods SET status = 'PROCESSING' WHERE id = :p", p=w["period"]["id"])
    db = session_for(w["cid"])
    try:
        inside = calendars.period_state_on(db, w["cid"], emma(w), end - timedelta(days=10))
        after_window = calendars.period_state_on(db, w["cid"], emma(w), end - timedelta(days=2))
    finally:
        db.close()
    assert inside == "INPUT_LOCKED" and after_window == "OPEN"


def test_cutoff_assistant_lists_pending_items_with_their_severity(runs_world, app):
    from tests.helpers import create_employee, signup_company
    from tests.payroll.test_compensation import login_as

    w = runs_world
    start = date.fromisoformat(w["period"]["start"])
    pending_leave(w["cid"], w["emp"]["Emma"]["id"], start + timedelta(days=2))
    sql("INSERT INTO attendance_requests (company_id, employee_id, shift_date, kind, mode, status, payload, "
        "reason_code, minutes, created_at, updated_at) VALUES (:c, :e, :d, 'MISSED_PUNCH', 'SELF', 'PENDING', "
        "CAST(:p AS jsonb), 'FORGOT_TO_PUNCH', 0, now(), now())", c=w["cid"], e=w["emp"]["Ivan"]["id"],
        d=start + timedelta(days=3), p='{"direction": "OUT", "at": "2026-01-01T18:00:00+00:00"}')
    r = w["acme"].get("/api/v1/cutoff", params={"pay_group_id": w["group"], "period_id": w["period"]["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    codes = {i["code"]: i for i in body["items"]}
    assert codes["ATTENDANCE_REQUESTS_PENDING"]["count"] == 1
    assert codes["ATTENDANCE_REQUESTS_PENDING"]["severity"] == "BLOCKING"  # pending_at_cutoff = EXCEPTION
    assert codes["LEAVE_PENDING"]["count"] == 1
    assert body["period"]["id"] == w["period"]["id"] and body["lock_state"] in ("OPEN", "INPUT_LOCKED")
    assert body["attendance_window"]["split"] is False and not body["ready"]

    set_setting(w["cid"], "payroll.attendance.pending_at_cutoff", "WARNING")
    body = w["acme"].get("/api/v1/cutoff", params={"pay_group_id": w["group"]}).json()
    assert {i["code"]: i["severity"] for i in body["items"]}["ATTENDANCE_REQUESTS_PENDING"] == "WARNING"

    other = signup_company(app, "Other Payroll Co")
    assert other.get("/api/v1/cutoff", params={"pay_group_id": w["group"]}).status_code == 404
    clerk = login_as(app, w["acme"], create_employee(w["acme"], "Ella", "Employee"), ["employee"])
    assert clerk.get("/api/v1/cutoff", params={"pay_group_id": w["group"]}).status_code == 403


def test_jc_as_11_a_correction_on_a_finalized_date_runs_retro_detect_automatically(retro_world):
    w = retro_world
    day = working_day(w["period"])
    r = w["hr"].post("/api/v1/attendance/manual", json={
        "employee_id": w["emma"], "date": day.isoformat(),
        "check_in_time": datetime(day.year, day.month, day.day, 3, 30, tzinfo=timezone.utc).isoformat(),
        "check_out_time": datetime(day.year, day.month, day.day, 12, 30, tzinfo=timezone.utc).isoformat(),
        "status": "present", "notes": "Gate register"})
    assert r.status_code == 200, r.text
    session_id = r.json()["id"]
    jobs = sql("SELECT idempotency_key, status, result, payload FROM jobs WHERE type = 'payroll.retro.detect' "
               "AND company_id = :c ORDER BY id", c=w["cid"])
    assert jobs, "no retro job was enqueued for the finalized date"
    key, status, result, payload = jobs[-1]
    assert key.startswith(f"retro:attendance:{session_id}:")
    assert status == "SUCCEEDED" and result["outcomes"] >= 1
    assert payload["periods"] == {str(w["period"]["id"]): [day.isoformat()]}
    # The finalized run is never edited; the run employee's retro breakdown answers.
    r = w["acme"].get(f"/api/v1/payroll/run-employees/{w['emma_row']}/retro-breakdown")
    assert r.status_code == 200, r.text
    assert r.json()["run_employee_id"] == w["emma_row"]
    for item in r.json()["corrections_of_this_period"]:
        assert item["cause"]["record_type"] == "attendance" and item["cause"]["record_id"] == session_id


def test_open_period_changes_enqueue_nothing(runs_world):
    w = runs_world
    day = working_day(w["period"])
    r = w["acme"].post("/api/v1/attendance/manual", json={
        "employee_id": w["emp"]["Emma"]["id"], "date": day.isoformat(),
        "check_in_time": datetime(day.year, day.month, day.day, 3, 30, tzinfo=timezone.utc).isoformat(),
        "check_out_time": datetime(day.year, day.month, day.day, 12, 30, tzinfo=timezone.utc).isoformat(),
        "status": "present"})
    assert r.status_code == 200, r.text
    assert sql("SELECT count(*) FROM jobs WHERE type = 'payroll.retro.detect' AND company_id = :c",
               c=w["cid"])[0][0] == 0


@pytest.mark.parametrize("record_type", ["lop_input", "holiday"])
def test_non_attendance_record_types_are_left_to_their_owners(runs_world, record_type):
    from app.services import payroll_sync

    class Change:
        employee_id = 1
        date_from = date(2026, 1, 1)
        date_to = None
        version_hash = "0" * 64
        record_id = 1
        details = None

    change = Change()
    change.record_type = record_type
    assert payroll_sync.on_change(None, runs_world["cid"], change) is None



def test_as_pay_01_a_correction_on_an_input_locked_date_schedules_stale_detection(runs_world):
    w = runs_world
    sql("UPDATE pay_periods SET status = 'PROCESSING' WHERE id = :p", p=w["period"]["id"])
    day = working_day(w["period"])
    r = w["acme"].post("/api/v1/attendance/manual", json={
        "employee_id": w["emp"]["Emma"]["id"], "date": day.isoformat(),
        "check_in_time": datetime(day.year, day.month, day.day, 3, 30, tzinfo=timezone.utc).isoformat(),
        "check_out_time": datetime(day.year, day.month, day.day, 12, 30, tzinfo=timezone.utc).isoformat(),
        "status": "present"})
    assert r.status_code == 200, r.text
    assert sql("SELECT count(*) FROM jobs WHERE type = 'payroll.match_source_changes' AND company_id = :c",
               c=w["cid"])[0][0] >= 1
    assert sql("SELECT count(*) FROM jobs WHERE type = 'payroll.retro.detect' AND company_id = :c",
               c=w["cid"])[0][0] == 0  # locked, not paid: stale path only


def test_as_pay_06_cutoff_before_the_window_end_is_warned(runs_world):
    w = runs_world
    body = w["acme"].get("/api/v1/cutoff", params={"pay_group_id": w["group"], "period_id": w["period"]["id"]}).json()
    window_end = body["attendance_window"]["end"]
    codes = {i["code"]: i for i in body["items"]}
    if body["period"]["input_cutoff_on"] <= window_end:
        assert codes["CUTOFF_BEFORE_WINDOW_END"]["severity"] == "WARNING"
    sql("UPDATE pay_periods SET attendance_end = input_cutoff_on - 1, attendance_start = period_start "
        "WHERE id = :p", p=w["period"]["id"])
    body = w["acme"].get("/api/v1/cutoff", params={"pay_group_id": w["group"], "period_id": w["period"]["id"]}).json()
    assert "CUTOFF_BEFORE_WINDOW_END" not in {i["code"] for i in body["items"]}


def test_cutoff_without_a_pay_calendar_says_what_to_do(app):
    from tests.helpers import signup_company

    admin = signup_company(app, "No Calendar Co")
    group = sql("SELECT id FROM pay_groups WHERE company_id = :c ORDER BY id LIMIT 1", c=admin.company_id)
    if not group:
        pytest.skip("signup creates no starter pay group")
    r = admin.get("/api/v1/cutoff", params={"pay_group_id": group[0][0]})
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["code"] == "NO_PAY_PERIOD" and r.json()["ready"] is False
