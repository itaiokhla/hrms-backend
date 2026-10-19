"""Regressions for issues found in an adversarial review of the attendance/location code."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from tests.attendance_helpers import (  # noqa: F401  (fake_clock is a fixture)
    BLR, IST, act, employee_with_login, fake_clock, fix, meta, owner_rows, sample, set_policy, tracking,
)
from tests.helpers import create_employee, invite_and_activate, role_id, signup_company

DAY = "2026-10-19"


def ist(hhmm: str, day: str = DAY) -> str:
    return IST.localize(datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")).isoformat()


@pytest.fixture
def tracked(app):
    admin = signup_company(app, "Review Co")
    set_policy(admin, requireFaceForSelfCheckIn=False, locationPunchPolicy="OPTIONAL", locationLunchPolicy="OPTIONAL",
               trackingEnabled=True, trackingIntervalMinutes=120, trackingGraceMinutes=15)
    return admin


def _sweep(company_id: int) -> dict:
    from app.database.db import SessionLocal, bind_tenant
    from app.services import location_service

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        result = location_service.sweep(db, company_id)
        db.commit()
        return result
    finally:
        db.close()


def test_skip_level_viewers_get_the_schedule_but_no_location_details(app, tracked, fake_clock):
    from app.core.rate_limit import limiter

    manager = create_employee(tracked, "Mona")
    lead = create_employee(tracked, "Leo", manager_id=manager["id"])
    worker = create_employee(tracked, "Wes", manager_id=lead["id"])
    limiter.reset()
    mona = invite_and_activate(app, tracked, manager, ["manager"])  # location:read DIRECT_REPORTS only
    wes = invite_and_activate(app, tracked, worker, ["employee"])
    fake_clock.at_ist(DAY, "09:00")
    att = act(wes, "PUNCH_IN", meta(fake_clock, fix(fake_clock))).json()["record"]["id"]
    fake_clock.at_ist(DAY, "11:05")
    far = sample(wes, fake_clock, att, fix(fake_clock, BLR[0] + 0.08, BLR[1]))
    assert far.status_code == 200 and far.json()["last_capture"]["movement_status"] == "SIGNIFICANT_MOVEMENT_REVIEW"
    detail = mona.get(f"/api/v1/attendance/sessions/{att}", params={"include_locations": True}).json()
    assert detail["location_access"] is False and detail["samples"] is None
    last = detail["tracking"]["last_capture"]
    assert set(last) == {"id", "sample_type", "status", "received_at"}  # no distance, accuracy or movement


def test_tracking_follows_the_current_policy_even_if_a_change_raced_a_punch_in(tracked, app, fake_clock):
    emp = employee_with_login(app, tracked, "Rae")
    fake_clock.at_ist(DAY, "09:00")
    att = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock))).json()["record"]["id"]
    # Simulate the race: the policy is off, but this session never received the change.
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        conn.execute(text("UPDATE company_settings SET value = 'false' WHERE key = 'trackingEnabled' AND company_id = :c"),
                     {"c": tracked.company_id})
    status = tracking(emp)
    assert status["state"] == "STOPPED" and status["stop_reason"] == "POLICY_DISABLED"
    fake_clock.at_ist(DAY, "11:05")
    assert sample(emp, fake_clock, att, fix(fake_clock)).json()["code"] == "TRACKING_NOT_ACTIVE"


def test_an_event_reading_never_erases_a_missed_slot(tracked, app, fake_clock):
    emp = employee_with_login(app, tracked, "Ivy")
    fake_clock.at_ist(DAY, "09:00")
    act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock)))
    fake_clock.at_ist(DAY, "11:20")  # the 11:00 check had no reading by 11:15
    act(emp, "LUNCH_IN", meta(fake_clock, fix(fake_clock)))
    rows = owner_rows("SELECT status, slot_due_at FROM location_samples WHERE sample_type = 'PERIODIC'")
    assert [r["status"] for r in rows] == ["MISSED"]
    assert rows[0]["slot_due_at"].astimezone(IST).strftime("%H:%M") == "11:00"


def test_closing_a_shift_by_correction_settles_only_up_to_the_corrected_end(tracked, app, fake_clock):
    emp = employee_with_login(app, tracked, "Cal")
    fake_clock.at_ist(DAY, "09:00")
    act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock)))
    fake_clock.at_ist(DAY, "19:20")
    r = tracked.post("/api/v1/attendance/manual", json={
        "employee_id": emp.employee["id"], "date": DAY, "check_in_time": ist("09:00"), "check_out_time": ist("17:00")})
    assert r.status_code == 200, r.text
    slots = [x["slot_due_at"].astimezone(IST).strftime("%H:%M")
             for x in owner_rows("SELECT slot_due_at FROM location_samples WHERE status = 'MISSED' ORDER BY slot_due_at")]
    assert slots == ["11:00", "13:00", "15:00"]  # nothing after the corrected 17:00 punch-out


def test_corrections_keep_tracking_and_breaks_consistent(tracked, app, fake_clock):
    emp = employee_with_login(app, tracked, "Lin")
    fake_clock.at_ist(DAY, "09:00")
    act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock)))
    fake_clock.at_ist(DAY, "12:00")
    act(emp, "LUNCH_IN", meta(fake_clock))
    assert tracking(emp)["state"] == "PAUSED"
    # HR records the lunch as already over: the pause it caused ends too.
    fake_clock.at_ist(DAY, "12:40")
    r = tracked.post("/api/v1/attendance/manual", json={
        "employee_id": emp.employee["id"], "date": DAY, "check_in_time": ist("09:00"),
        "breaks": [{"started_at": ist("12:00"), "ended_at": ist("12:30")}]})
    assert r.status_code == 200, r.text
    status = tracking(emp)
    assert status["state"] == "ACTIVE" and status["next_due_at"] is not None

    # A shift that expired during lunch cannot be closed before that lunch began: 409, not a server error.
    other = employee_with_login(app, tracked, "Max")
    fake_clock.at_ist("2026-10-20", "08:00")
    act(other, "PUNCH_IN", meta(fake_clock))
    fake_clock.at_ist("2026-10-20", "14:00")
    act(other, "LUNCH_IN", meta(fake_clock))
    fake_clock.at_ist("2026-10-21", "01:00")  # past the 16-hour limit: closed, lunch left open
    tracking(other)
    r = tracked.post("/api/v1/attendance/manual", json={
        "employee_id": other.employee["id"], "date": "2026-10-20", "check_in_time": ist("08:00", "2026-10-20"),
        "check_out_time": ist("13:30", "2026-10-20")})
    assert r.status_code == 409 and r.json()["code"] == "BREAKS_OUTSIDE_SESSION"


def test_review_answers_out_of_scope_items_like_unknown_ones(app, tracked, fake_clock):
    from app.core.rate_limit import limiter

    boss_a = create_employee(tracked, "Ann")
    boss_b = create_employee(tracked, "Bob")
    worker = create_employee(tracked, "Kit", manager_id=boss_b["id"])
    limiter.reset()
    ann = invite_and_activate(app, tracked, boss_a, ["manager"])
    kit = invite_and_activate(app, tracked, worker, ["employee"])
    fake_clock.at_ist(DAY, "09:00")
    att = act(kit, "PUNCH_IN", meta(fake_clock, fix(fake_clock))).json()["record"]["id"]
    fake_clock.at_ist(DAY, "11:05")
    sample(kit, fake_clock, att, fix(fake_clock, BLR[0] + 0.08, BLR[1]))
    flagged = owner_rows("SELECT id FROM location_samples WHERE review_status = 'PENDING'")[0]["id"]
    foreign = ann.post(f"/api/v1/attendance/location-review/{flagged}", json={"decision": "ACKNOWLEDGED"})
    unknown = ann.post(f"/api/v1/attendance/location-review/{flagged + 999}", json={"decision": "ACKNOWLEDGED"})
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


def test_payroll_snapshot_counts_finished_shifts_and_all_lunch_time(app, tracked, fake_clock):
    set_policy(tracked, lunchIsPaid=True)
    emp = employee_with_login(app, tracked, "Pia")
    for hhmm, action in (("09:00", "PUNCH_IN"), ("13:00", "LUNCH_IN"), ("13:45", "LUNCH_OUT"), ("18:00", "PUNCH_OUT")):
        fake_clock.at_ist("2026-09-07", hhmm)
        act(emp, action, meta(fake_clock))
    fake_clock.at_ist("2026-09-30", "10:00")
    act(emp, "PUNCH_IN", meta(fake_clock))  # still running when payroll is calculated
    tracked.post("/api/v1/salary-structures", json={"employee_id": emp.employee["id"], "basic_salary": "30000",
                                                    "effective_from": "2026-01-01"})
    payroll = employee_with_login(app, tracked, "Pay", roles=("payroll_manager",))
    hours = payroll.post(f"/api/v1/payroll/calculate?employee_id={emp.employee['id']}&year=2026&month=9").json()["attendance_hours"]
    assert hours["provisional_sessions"] == 1 and hours["complete_sessions"] == 1
    assert hours["net_seconds"] == 9 * 3600 and hours["lunch_seconds"] == 45 * 60  # paid lunch: not deducted
    run = payroll.post(f"/api/v1/payroll/generate?employee_id={emp.employee['id']}&year=2026&month=9").json()
    stored = owner_rows("SELECT break_seconds, net_work_seconds FROM payroll WHERE id = :i", i=run["id"])[0]
    assert stored == {"break_seconds": 45 * 60, "net_work_seconds": 9 * 3600}


def test_sweep_stops_collection_when_attendance_access_is_removed_through_a_role(app, tracked, fake_clock):
    field = tracked.post("/api/v1/roles", json={"name": "Field staff", "grants": [
        {"permission": "attendance:mark", "scope": "SELF"}, {"permission": "attendance:read", "scope": "SELF"},
        {"permission": "location:read", "scope": "SELF"}]}).json()
    from app.core.rate_limit import limiter

    limiter.reset()
    worker = create_employee(tracked, "Fay")
    fay = invite_and_activate(app, tracked, worker, ["employee"])
    tracked.put(f"/api/v1/users/{fay.user_id}/roles", json={"role_ids": [field["id"]]})
    fake_clock.at_ist(DAY, "09:00")
    act(fay, "PUNCH_IN", meta(fake_clock, fix(fake_clock)))
    r = tracked.patch(f"/api/v1/roles/{field['id']}", json={"grants": [{"permission": "attendance:read", "scope": "SELF"}]})
    assert r.status_code == 200, r.text
    fake_clock.at_ist(DAY, "15:00")
    result = _sweep(tracked.company_id)
    assert result["stopped"] == 1 and result["missed_slots"] == 0
    assert owner_rows("SELECT tracking_state, tracking_stop_reason FROM attendance") == [
        {"tracking_state": "STOPPED", "tracking_stop_reason": "PERMISSION_REVOKED"}]
    assert owner_rows("SELECT count(*) AS n FROM location_samples WHERE status = 'MISSED'")[0]["n"] == 0
    assert role_id(tracked, "employee")  # default roles untouched
