"""ledger_inputs(): the day ledger's source data for the snapshot builder (3.4 §2-§8; FOLLOW-UPS F-02, F-03, F-04).

EC-ATT-02, EC-ATT-13, EC-ATT-19, EC-ATT-21, EC-LV-01, EC-LV-05, EC-LV-09.
2099-06-01 is a Monday; tenants start with the MON_FRI work week.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytz
from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from app.payroll import ledger
from app.payroll.services import time_inputs
from tests.attendance_helpers import fake_clock  # noqa: F401  (fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company

BASE = "/api/v1/payroll"


def _sql(query, **params):
    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return [dict(r._mapping) for r in result] if result.returns_rows else None


def _inputs(company_id, employee_id, start, end, **kwargs):
    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        days = time_inputs.ledger_inputs(db, company_id, employee_id, start, end, **kwargs)
        db.commit()
        return days
    finally:
        db.close()


def _by_date(days):
    return {d.date: d for d in days}


def _tz(company_id):
    return pytz.timezone(_sql("SELECT timezone FROM companies WHERE id = :c", c=company_id)[0]["timezone"] or "UTC")


def _at(tz, day, hhmm):
    y, m, d = (int(x) for x in day.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    return tz.localize(datetime(y, m, d, h, mi))


@pytest.fixture
def acme(app):
    admin = signup_company(app, "Acme Corp")
    boss = create_employee(admin, "Boss")
    worker = create_employee(admin, "Worker", manager_id=boss["id"])
    hr = create_employee(admin, "Hana", "HR")
    return {
        "admin": admin, "cid": admin.company_id, "worker_id": worker["id"],
        "boss": invite_and_activate(app, admin, boss, ["manager"]),
        "worker": invite_and_activate(app, admin, worker, ["employee"]),
        "hr": invite_and_activate(app, admin, hr, ["hr_admin"]),
    }


def _leave(acme, start, end=None, leave_type="casual", **portions):
    r = acme["worker"].post("/api/v1/leaves/apply", json={"leave_type": leave_type, "start_date": start,
                                                          "end_date": end or start, **portions})
    assert r.status_code == 200, r.text
    leave_id = r.json()["id"]
    r = acme["boss"].post(f"/api/v1/leaves/{leave_id}/approve", json={})
    assert r.status_code == 200, r.text
    if r.json()["status"] == "pending":  # three days or more: the HR step of the default chain
        r = acme["hr"].post(f"/api/v1/leaves/{leave_id}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    return r.json()


def test_F_04_every_date_with_weekly_offs_even_outside_employment(acme):
    days = _inputs(acme["cid"], acme["worker_id"], date(2024, 1, 1), date(2024, 1, 31))  # hired 2024-01-15
    assert [d.date for d in days] == [date(2024, 1, 1) + timedelta(n) for n in range(31)]
    by = _by_date(days)
    assert not by[date(2024, 1, 14)].employed and by[date(2024, 1, 15)].employed  # EC-ATT-13
    assert by[date(2024, 1, 6)].weekly_off and by[date(2024, 1, 7)].weekly_off  # before hire
    assert not by[date(2024, 1, 8)].weekly_off
    assert by[date(2024, 1, 15)].segment == str(by[date(2024, 1, 15)].assignment_id) and by[date(2024, 1, 1)].segment == ""
    assert all(d.work_week_id is not None for d in days)
    result = ledger.build_ledger(days, ledger.LedgerConfig())
    assert Decimal(str(result.totals.to_dict()["employment_days"])) == 17
    assert Decimal(str(result.totals.to_dict()["lop_days"])) == 0


def test_EC_ATT_21_the_assignment_work_week_decides_weekly_offs(acme):
    cid = acme["cid"]
    pattern = _sql("INSERT INTO work_week_patterns (company_id, code, name, weekly_off_days, nth_weekday_offs, status, "
                   "created_at, updated_at) VALUES (:c, 'MON_SAT', 'Monday to Saturday', '[6]', "
                   "'[{\"weekday\": 5, \"nth\": [2, 4]}]', 'ACTIVE', now(), now()) RETURNING id", c=cid)[0]["id"]
    _sql("UPDATE employee_assignments SET work_week_pattern_id = :p WHERE employee_id = :e", p=pattern, e=acme["worker_id"])
    by = _by_date(_inputs(cid, acme["worker_id"], date(2099, 6, 1), date(2099, 6, 30)))
    saturdays = [d for d in by if d.weekday() == 5]
    assert [by[d].weekly_off for d in saturdays] == [False, True, False, True]
    assert all(by[d].weekly_off for d in by if d.weekday() == 6)
    assert by[date(2099, 6, 6)].work_week_id == pattern


def test_leave_portions_with_pay_treatment_and_version(acme):
    cid, eid = acme["cid"], acme["worker_id"]
    full = _leave(acme, "2099-06-01", "2099-06-02", end_portion="FIRST_HALF")
    unpaid = _leave(acme, "2099-06-02", leave_type="unpaid", start_portion="SECOND_HALF")
    by = _by_date(_inputs(cid, eid, date(2099, 6, 1), date(2099, 6, 30)))
    assert [(p.portion, p.pay_treatment, p.leave_type_code) for p in by[date(2099, 6, 2)].leaves] == [
        (ledger.Portion.FIRST_HALF, ledger.PayTreatment.FULL, "casual"),
        (ledger.Portion.SECOND_HALF, ledger.PayTreatment.UNPAID, "unpaid")]
    assert by[date(2099, 6, 1)].leaves[0].portion is ledger.Portion.FULL
    logged = _sql("SELECT version_hash FROM source_change_log WHERE record_type = 'leave' AND record_id = :l",
                  l=full["id"])[0]["version_hash"]
    assert by[date(2099, 6, 1)].leaves[0].content_hash == logged
    result = ledger.build_ledger(list(by.values()), ledger.LedgerConfig(leave_enabled=True))
    assert Decimal(str(result.totals.to_dict()["lop_days"])) == Decimal("0.5")
    assert unpaid["status"] == "approved"


def test_EC_LV_01_unpaid_leave_and_absence_count_once(acme):
    cid, eid = acme["cid"], acme["worker_id"]
    _leave(acme, "2099-06-03", leave_type="unpaid")
    tz = _tz(cid)
    r = acme["hr"].post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2099-06-03", "check_in_time": _at(tz, "2099-06-03", "09:00").isoformat(),
        "status": "absent"})
    assert r.status_code == 200, r.text
    days = _inputs(cid, eid, date(2099, 6, 1), date(2099, 6, 7))
    cfg = ledger.LedgerConfig(attendance_enabled=True, leave_enabled=True, unmarked_day="PRESENT")
    result = ledger.build_ledger(days, cfg)
    assert Decimal(str(result.totals.to_dict()["lop_days"])) == 1  # EC-ATT-19


def test_EC_LV_09_leave_spanning_two_periods_is_split_by_date(acme):
    cid, eid = acme["cid"], acme["worker_id"]
    _leave(acme, "2099-06-29", "2099-07-02")
    june = _inputs(cid, eid, date(2099, 6, 1), date(2099, 6, 30))
    july = _inputs(cid, eid, date(2099, 7, 1), date(2099, 7, 31))
    assert sum(len(d.leaves) for d in june) == 2 and sum(len(d.leaves) for d in july) == 2


def test_F_03_attendance_net_seconds_and_missing_punch_out(acme):
    cid, eid, hr = acme["cid"], acme["worker_id"], acme["hr"]
    tz = _tz(cid)
    r = hr.post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2026-10-12", "check_in_time": _at(tz, "2026-10-12", "09:00").isoformat(),
        "check_out_time": _at(tz, "2026-10-12", "18:00").isoformat(), "status": "present",
        "breaks": [{"started_at": _at(tz, "2026-10-12", "13:00").isoformat(),
                    "ended_at": _at(tz, "2026-10-12", "14:00").isoformat(), "is_paid": False}]})
    assert r.status_code == 200, r.text
    complete_id = r.json()["id"]
    r = hr.post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2026-10-13", "check_in_time": _at(tz, "2026-10-13", "09:00").isoformat(),
        "status": "present"})
    assert r.status_code == 200 and r.json()["closed_reason"] == "MANUAL_NO_PUNCH_OUT", r.text
    by = _by_date(_inputs(cid, eid, date(2026, 10, 12), date(2026, 10, 13)))
    done = by[date(2026, 10, 12)].attendance
    assert done.worked_seconds == 8 * 3600 and not done.closed_without_punch_out and done.session_id == complete_id
    logged = _sql("SELECT version_hash FROM source_change_log WHERE record_type = 'attendance' AND record_id = :r "
                  "ORDER BY id DESC LIMIT 1", r=complete_id)[0]["version_hash"]
    assert done.content_hash == logged
    missing = by[date(2026, 10, 13)].attendance
    assert missing.closed_without_punch_out and missing.worked_seconds is None  # EC-ATT-02: never invented
    result = ledger.build_ledger(list(by.values()), ledger.LedgerConfig(attendance_enabled=True))
    assert [e.code.value for e in result.exceptions if e.day == date(2026, 10, 13)] == ["MISSING_PUNCH_OUT"]


def test_stale_live_session_is_expired_at_the_lock_time(acme, fake_clock):  # noqa: F811
    cid, eid, hr = acme["cid"], acme["worker_id"], acme["hr"]
    tz = _tz(cid)
    start = _at(tz, "2026-10-19", "08:00")
    fake_clock.now = start.astimezone(timezone.utc) + timedelta(hours=1)
    r = hr.post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2026-10-19", "check_in_time": start.isoformat(), "status": "present"})
    assert r.status_code == 200 and r.json()["closed_reason"] is None, r.text
    session = r.json()["id"]
    early = _inputs(cid, eid, date(2026, 10, 19), date(2026, 10, 19), lock_at=start + timedelta(hours=4))
    running = early[0].attendance
    assert running.worked_seconds is None and not running.closed_without_punch_out
    assert _sql("SELECT closed_at FROM attendance WHERE id = :a", a=session)[0]["closed_at"] is None
    late = _inputs(cid, eid, date(2026, 10, 19), date(2026, 10, 19), lock_at=start + timedelta(hours=20))
    assert late[0].attendance.closed_without_punch_out and late[0].attendance.worked_seconds is None
    row = _sql("SELECT closed_reason, row_version FROM attendance WHERE id = :a", a=session)[0]
    assert row == {"closed_reason": "MISSING_PUNCH_OUT", "row_version": 2}


def test_manual_lop_on_duty_and_unpaid_suspension(acme):
    cid, eid, admin, hr = acme["cid"], acme["worker_id"], acme["admin"], acme["hr"]
    lop = admin.post(f"{BASE}/lop-inputs", json={"employee_id": eid, "date": "2099-06-02", "portion": "SECOND_HALF",
                                                 "reason": "Left without notice"}).json()
    pending = admin.post(f"{BASE}/lop-inputs", json={"employee_id": eid, "date": "2099-06-04", "reason": "x"}).json()
    assert hr.post(f"/api/v1/payroll/approvals/{lop['approval_request_id']}/approve", json={}).status_code == 200
    duty = acme["worker"].post(f"{BASE}/on-duty", json={"date": "2099-06-03", "reason": "Client"}).json()
    assert acme["boss"].post(f"/api/v1/payroll/approvals/{duty['approval_request_id']}/approve", json={}).status_code == 200
    suspension = admin.post(f"{BASE}/suspensions", json={"employee_id": eid, "start_date": "2099-06-08",
                                                         "end_date": "2099-06-09", "reason": "Inquiry"}).json()
    admin.post(f"{BASE}/suspensions", json={"employee_id": eid, "start_date": "2099-06-10", "end_date": "2099-06-10",
                                            "paid": True, "reason": "Paid leave of absence"})
    by = _by_date(_inputs(cid, eid, date(2099, 6, 1), date(2099, 6, 12)))
    assert [(m.source_id, m.portion) for m in by[date(2099, 6, 2)].manual_lop] == [(lop["id"], ledger.Portion.SECOND_HALF)]
    assert by[date(2099, 6, 4)].manual_lop == () and pending["status"] == "PENDING"
    assert [o.source_id for o in by[date(2099, 6, 3)].on_duty] == [duty["id"]]
    assert by[date(2099, 6, 8)].suspended_unpaid and by[date(2099, 6, 8)].suspension_id == suspension["id"]
    assert not by[date(2099, 6, 10)].suspended_unpaid  # a paid suspension is paid time
    cfg = ledger.LedgerConfig(attendance_enabled=True, unmarked_day="PRESENT")
    totals = ledger.build_ledger(list(by.values()), cfg).totals.to_dict()
    assert Decimal(str(totals["lop_days"])) == Decimal("2.5")


def test_F_02_tiered_leave_carries_its_rolling_window_history(acme):
    cid, eid = acme["cid"], acme["worker_id"]
    first = _leave(acme, "2099-01-05", "2099-01-09", leave_type="sick")
    current = _leave(acme, "2100-01-12", "2100-01-13", leave_type="sick")
    tiers = '[{"from_day": 1, "to_day": 5, "percent": "100"}, {"from_day": 6, "to_day": 30, "percent": "50"}]'
    _sql("UPDATE leave_days SET pay_treatment_snapshot = pay_treatment_snapshot || jsonb_build_object("
         "'pay_treatment', 'TIERED', 'tiers', CAST(:t AS jsonb), 'tiers_window_months', 12) WHERE leave_id IN (:a, :b)",
         t=tiers, a=first["id"], b=current["id"])
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        before, rolls = time_inputs.tiered_history(db, cid, eid, "sick", date(2099, 12, 1), date(2099, 12, 31))
        assert (before, rolls) == (Decimal(5), ())
    finally:
        db.close()
    by = _by_date(_inputs(cid, eid, date(2100, 1, 1), date(2100, 1, 31)))
    tiered = by[date(2100, 1, 12)].leaves[0].tiered
    assert tiered.days_before_period == 5
    assert tiered.rolls_off == tuple((date(2100, 1, d), Decimal(1)) for d in range(5, 10))
    assert [t.percent for t in tiered.tiers] == [Decimal(100), Decimal(50)]
    result = ledger.build_ledger(list(by.values()), ledger.LedgerConfig(leave_enabled=True))
    assert Decimal(str(result.totals.to_dict()["lop_days"])) == 0  # the January 2099 days have left the window


def test_standard_hours_from_a_decimal_shift_and_fte(acme):
    cid, eid, admin = acme["cid"], acme["worker_id"], acme["admin"]
    r = admin.post("/api/v1/shifts", json={"name": "Long", "start_time": "08:00", "end_time": "17:30",
                                           "duration_hours": 9.5})
    shift_id = r.json()["id"]
    _sql("UPDATE shifts SET unpaid_break_minutes = 60 WHERE id = :s", s=shift_id)
    _sql("UPDATE employee_assignments SET shift_id = :s, fte = 0.5 WHERE employee_id = :e", s=shift_id, e=eid)
    day = _inputs(cid, eid, date(2099, 6, 1), date(2099, 6, 1))[0]
    assert day.standard_hours == Decimal("4.25")


def test_day_ledger_preview_api(acme, app):
    admin, eid = acme["admin"], acme["worker_id"]
    _leave(acme, "2099-06-01", leave_type="unpaid")
    r = admin.get(f"{BASE}/employees/{eid}/day-ledger-preview", params={"start": "2099-06-01", "end": "2099-06-30"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["inputs"]) == 30 and len(body["ledger"]["days"]) == 30
    assert body["inputs"][0]["leaves"][0]["pay_treatment"] == "UNPAID"
    assert acme["worker"].get(f"{BASE}/employees/{eid}/day-ledger-preview",
                              params={"start": "2099-06-01", "end": "2099-06-30"}).status_code == 403
    assert admin.get(f"{BASE}/employees/999999/day-ledger-preview",
                     params={"start": "2099-06-01", "end": "2099-06-30"}).status_code == 404
    assert admin.get(f"{BASE}/employees/{eid}/day-ledger-preview",
                     params={"start": "2099-06-30", "end": "2099-06-01"}).status_code == 400
    other = signup_company(app, "Beta Industries")
    assert other.get(f"{BASE}/employees/{eid}/day-ledger-preview",
                     params={"start": "2099-06-01", "end": "2099-06-30"}).status_code == 404


def test_EC_LV_02_pending_requests_are_listed_for_the_input_cutoff(acme):
    cid, eid, admin = acme["cid"], acme["worker_id"], acme["admin"]
    pending_leave = acme["worker"].post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": "2099-06-01",
                                                                      "end_date": "2099-06-01"}).json()
    approved = _leave(acme, "2099-06-02")
    assert acme["worker"].post(f"/api/v1/leaves/{approved['id']}/cancel").status_code == 200
    lop = admin.post(f"{BASE}/lop-inputs", json={"employee_id": eid, "date": "2099-06-04", "reason": "x"}).json()
    duty = acme["worker"].post(f"{BASE}/on-duty", json={"date": "2099-06-05", "reason": "Client"}).json()
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        items = time_inputs.pending_inputs(db, cid, eid, date(2099, 6, 1), date(2099, 6, 30))
        assert time_inputs.pending_inputs(db, cid, eid, date(2099, 7, 1), date(2099, 7, 31)) == []
    finally:
        db.close()
    assert [(i["type"], i["id"]) for i in items] == [
        ("leave", pending_leave["id"]), ("leave_cancellation", approved["id"]), ("lop_input", lop["id"]),
        ("on_duty", duty["id"])]
    # An approved leave with a pending cancellation still counts until the cancellation is approved.
    by = _by_date(_inputs(cid, eid, date(2099, 6, 1), date(2099, 6, 2)))
    assert by[date(2099, 6, 1)].leaves == () and len(by[date(2099, 6, 2)].leaves) == 1
