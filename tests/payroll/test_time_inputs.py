"""Manual LOP inputs, suspensions, on-duty, optional holidays, attendance minutes and the source change log.

3.4 §4.2 (rules 2, 6, 7), §8, §11, §16; 3.1 §5.2, §5.4; audit 03 §1.6-§1.8, §6-§8.
EC-ATT-05, EC-ATT-07, EC-ATT-16, EC-ATT-22, EC-HOL-05; three-tenant isolation of every new table.
2099-06-01 is a Monday; tenants start with the MON_FRI work week.
"""

from datetime import date, datetime

import pytest
import pytz
from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from tests.attendance_helpers import fake_clock  # noqa: F401  (fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company

BASE = "/api/v1/payroll"
APPROVALS = "/api/v1/payroll/approvals"
NEW_TABLES = ("leave_days", "leave_balance_transactions", "lop_inputs", "employee_suspensions", "on_duty_requests",
              "employee_optional_holidays", "source_change_log")


def _sql(query, **params):
    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return [dict(r._mapping) for r in result] if result.returns_rows else None


def _approve(client, request_id):
    return client.post(f"{APPROVALS}/{request_id}/approve", json={})


def _lop(client, employee_id, day, portion="FULL", reason="Unauthorised absence"):
    return client.post(f"{BASE}/lop-inputs", json={"employee_id": employee_id, "date": day, "portion": portion,
                                                    "reason": reason})


@pytest.fixture
def acme(app):
    admin = signup_company(app, "Acme Corp")
    boss = create_employee(admin, "Boss")
    worker = create_employee(admin, "Worker", manager_id=boss["id"])
    peer = create_employee(admin, "Peer")
    hr = create_employee(admin, "Hana", "HR")
    return {
        "admin": admin, "cid": admin.company_id, "worker_id": worker["id"], "peer_id": peer["id"],
        "boss": invite_and_activate(app, admin, boss, ["manager"]),
        "worker": invite_and_activate(app, admin, worker, ["employee"]),
        "peer": invite_and_activate(app, admin, peer, ["employee"]),
        "hr": invite_and_activate(app, admin, hr, ["hr_admin"]),
    }


# ---------------------------------------------------------------------------
# Manual LOP inputs
# ---------------------------------------------------------------------------
def test_lop_input_is_approved_through_the_engine(acme):
    admin, hr = acme["admin"], acme["hr"]
    r = _lop(admin, acme["worker_id"], "2099-06-02")
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["status"] == "PENDING" and row["approval_request_id"]
    assert _approve(admin, row["approval_request_id"]).status_code in (403, 404, 409)  # the maker never approves
    assert _approve(acme["worker"], row["approval_request_id"]).status_code in (403, 404)
    r = _approve(hr, row["approval_request_id"])
    assert r.status_code == 200, r.text
    listed = admin.get(f"{BASE}/lop-inputs", params={"employee_id": acme["worker_id"]}).json()
    assert [(i["id"], i["status"], i["row_version"]) for i in listed] == [(row["id"], "APPROVED", 2)]
    log = _sql("SELECT change, employee_id, date_from FROM source_change_log WHERE record_type = 'lop_input' "
               "AND record_id = :r", r=row["id"])
    assert log == [{"change": "UPDATED", "employee_id": acme["worker_id"], "date_from": date(2099, 6, 2)}]
    audit = _sql("SELECT module, action FROM audit_logs WHERE entity_type = 'lop_input' AND entity_id = :e",
                 e=str(row["id"]))
    assert audit == [{"module": "Payroll Inputs", "action": "LOP_INPUT_CREATED"}]


def test_rejected_lop_input(acme):
    row = _lop(acme["admin"], acme["worker_id"], "2099-06-02").json()
    r = acme["hr"].post(f"{APPROVALS}/{row['approval_request_id']}/reject", json={"remarks": "was on duty"})
    assert r.status_code == 200, r.text
    assert acme["admin"].get(f"{BASE}/lop-inputs").json()[0]["status"] == "REJECTED"
    assert _lop(acme["admin"], acme["worker_id"], "2099-06-02").status_code == 201  # the slot is free again


def test_EC_ATT_16_lop_input_on_leave_holiday_or_weekly_off_is_rejected(acme):
    admin, worker, boss = acme["admin"], acme["worker"], acme["boss"]
    leave = worker.post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": "2099-06-03",
                                                      "end_date": "2099-06-03", "end_portion": "FIRST_HALF"}).json()
    assert boss.post(f"/api/v1/leaves/{leave['id']}/approve", json={}).status_code == 200
    admin.post("/api/v1/holidays", json={"name": "Founders Day", "date": "2099-06-04"})
    for day, portion in (("2099-06-03", "FULL"), ("2099-06-03", "FIRST_HALF"), ("2099-06-04", "FULL"),
                         ("2099-06-06", "FULL")):
        r = _lop(admin, acme["worker_id"], day, portion)
        assert r.status_code == 409 and r.json()["code"] == "LOP_SLOT_TAKEN", (day, portion, r.text)
    assert _lop(admin, acme["worker_id"], "2099-06-03", "SECOND_HALF").status_code == 201


def test_lop_slots_do_not_overlap(acme):
    admin = acme["admin"]
    assert _lop(admin, acme["worker_id"], "2099-06-02", "FIRST_HALF").status_code == 201
    assert _lop(admin, acme["worker_id"], "2099-06-02", "FIRST_HALF").status_code == 409
    assert _lop(admin, acme["worker_id"], "2099-06-02", "FULL").status_code == 409
    assert _lop(admin, acme["worker_id"], "2099-06-02", "SECOND_HALF").status_code == 201


def test_lop_input_outside_employment_is_rejected(acme):
    _sql("UPDATE employees SET termination_date = '2099-05-31' WHERE id = :e", e=acme["worker_id"])
    r = _lop(acme["admin"], acme["worker_id"], "2099-06-02")
    assert r.status_code == 409 and r.json()["code"] == "LOP_SLOT_TAKEN"


def test_lop_input_permissions_and_scope(acme, app):
    other = signup_company(app, "Beta Industries")
    assert _lop(acme["worker"], acme["peer_id"], "2099-06-02").status_code == 403
    assert _lop(acme["boss"], acme["worker_id"], "2099-06-02").status_code == 403
    assert acme["worker"].get(f"{BASE}/lop-inputs").status_code == 403
    assert _lop(other, acme["worker_id"], "2099-06-02").status_code == 404
    assert _lop(acme["admin"], 999999, "2099-06-02").status_code == 404
    r = acme["admin"].post(f"{BASE}/lop-inputs", json={"employee_id": acme["worker_id"], "date": "2099-06-02",
                                                       "reason": "x", "status": "APPROVED"})
    assert r.status_code == 422
    assert _lop(acme["admin"], acme["worker_id"], "2099-06-02", reason="   ").status_code == 400


def test_cancelling_lop_inputs(acme, app):
    admin = acme["admin"]
    pending = _lop(admin, acme["worker_id"], "2099-06-02").json()
    r = admin.post(f"{BASE}/lop-inputs/{pending['id']}/cancel", json={"row_version": 7})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT"
    r = admin.post(f"{BASE}/lop-inputs/{pending['id']}/cancel", json={"row_version": 1})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    request = _sql("SELECT status FROM approval_requests WHERE id = :r", r=pending["approval_request_id"])[0]
    assert request["status"] == "CANCELLED"
    assert admin.post(f"{BASE}/lop-inputs/{pending['id']}/cancel").status_code == 409
    approved = _lop(admin, acme["worker_id"], "2099-06-02").json()
    _approve(acme["hr"], approved["approval_request_id"])
    other = signup_company(app, "Beta Industries")
    assert other.post(f"{BASE}/lop-inputs/{approved['id']}/cancel").status_code == 404
    r = admin.post(f"{BASE}/lop-inputs/{approved['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    changes = [c["change"] for c in _sql("SELECT change FROM source_change_log WHERE record_type = 'lop_input' "
                                         "AND record_id = :r ORDER BY id", r=approved["id"])]
    assert changes == ["UPDATED", "VOIDED"]


def test_lop_input_create_is_idempotent(acme):
    body = {"employee_id": acme["worker_id"], "date": "2099-06-02", "portion": "FULL", "reason": "No show"}
    headers = {"Idempotency-Key": "lop-1"}
    first = acme["admin"].post(f"{BASE}/lop-inputs", json=body, headers=headers)
    second = acme["admin"].post(f"{BASE}/lop-inputs", json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(_sql("SELECT id FROM lop_inputs")) == 1


# ---------------------------------------------------------------------------
# Suspensions
# ---------------------------------------------------------------------------
def test_suspensions_lifecycle(acme, app):
    admin = acme["admin"]
    body = {"employee_id": acme["worker_id"], "start_date": "2099-06-08", "end_date": "2099-06-12",
            "reason": "Pending inquiry"}
    r = admin.post(f"{BASE}/suspensions", json=body)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["paid"] is False and row["status"] == "ACTIVE"
    assert admin.post(f"{BASE}/suspensions", json={**body, "start_date": "2099-06-12", "end_date": "2099-06-20"}
                      ).status_code == 409
    bad = admin.post(f"{BASE}/suspensions", json={**body, "start_date": "2099-06-12", "end_date": "2099-06-01"})
    assert bad.status_code == 400
    assert acme["worker"].post(f"{BASE}/suspensions", json=body).status_code == 403
    other = signup_company(app, "Beta Industries")
    assert other.post(f"{BASE}/suspensions", json=body).status_code == 404
    assert other.post(f"{BASE}/suspensions/{row['id']}/cancel").status_code == 404
    assert [s["id"] for s in admin.get(f"{BASE}/suspensions").json()] == [row["id"]]
    assert other.get(f"{BASE}/suspensions").json() == []
    r = admin.post(f"{BASE}/suspensions/{row['id']}/cancel", json={"row_version": 1})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    changes = [c["change"] for c in _sql("SELECT change FROM source_change_log WHERE record_type = 'suspension'")]
    assert changes == ["CREATED", "VOIDED"]


def test_suspension_outside_employment_is_rejected(acme):
    _sql("UPDATE employees SET termination_date = '2099-06-10' WHERE id = :e", e=acme["worker_id"])
    r = acme["admin"].post(f"{BASE}/suspensions", json={"employee_id": acme["worker_id"], "start_date": "2099-06-08",
                                                         "end_date": "2099-06-12", "reason": "x"})
    assert r.status_code == 400 and r.json()["code"] == "OUTSIDE_EMPLOYMENT"


# ---------------------------------------------------------------------------
# On-duty
# ---------------------------------------------------------------------------
def test_on_duty_request_approved_by_the_manager(acme):
    worker, boss = acme["worker"], acme["boss"]
    r = worker.post(f"{BASE}/on-duty", json={"date": "2099-06-02", "portion": "FULL", "reason": "Client site"})
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["employee_id"] == acme["worker_id"] and row["status"] == "PENDING"
    assert worker.post(f"{BASE}/on-duty", json={"date": "2099-06-02", "portion": "SECOND_HALF", "reason": "x"}
                       ).status_code == 409
    assert _approve(worker, row["approval_request_id"]).status_code in (403, 404)
    assert _approve(acme["peer"], row["approval_request_id"]).status_code in (403, 404)
    r = _approve(boss, row["approval_request_id"])
    assert r.status_code == 200, r.text
    mine = worker.get(f"{BASE}/on-duty").json()
    assert [(i["id"], i["status"]) for i in mine] == [(row["id"], "APPROVED")]
    assert acme["peer"].get(f"{BASE}/on-duty").json() == []
    assert acme["peer"].post(f"{BASE}/on-duty/{row['id']}/cancel").status_code == 404
    # Approved on-duty work is withdrawn by HR, not by the employee.
    assert worker.post(f"{BASE}/on-duty/{row['id']}/cancel").status_code == 403
    assert acme["hr"].post(f"{BASE}/on-duty/{row['id']}/cancel").status_code == 200


def test_on_duty_recorded_for_a_report_and_scope(acme, app):
    boss = acme["boss"]
    r = boss.post(f"{BASE}/on-duty", json={"employee_id": acme["worker_id"], "date": "2099-06-02", "reason": "Visit"})
    assert r.status_code == 201, r.text
    assert boss.post(f"{BASE}/on-duty", json={"employee_id": acme["peer_id"], "date": "2099-06-02", "reason": "x"}
                     ).status_code == 404
    other = signup_company(app, "Beta Industries")
    assert other.post(f"{BASE}/on-duty", json={"employee_id": acme["worker_id"], "date": "2099-06-02",
                                                "reason": "x"}).status_code == 404
    # The worker withdraws their own pending request.
    assert acme["worker"].post(f"{BASE}/on-duty/{r.json()['id']}/cancel").status_code == 200


# ---------------------------------------------------------------------------
# Optional holidays
# ---------------------------------------------------------------------------
def test_EC_HOL_05_optional_holidays_within_the_quota(acme, app):
    admin, worker = acme["admin"], acme["worker"]
    ids = [admin.post("/api/v1/holidays", json={"name": f"Choice {n}", "date": f"2099-0{n}-0{n + 1}",
                                                "is_optional": True}).json()["id"] for n in (2, 3, 4)]
    fixed = admin.post("/api/v1/holidays", json={"name": "Fixed", "date": "2099-07-01"}).json()["id"]
    view = worker.get(f"{BASE}/optional-holidays", params={"year": 2099}).json()
    assert view["quota"] == 2 and view["remaining"] == 2
    assert [h["id"] for h in view["holidays"]] == ids
    for holiday_id in ids[:2]:
        r = worker.post(f"{BASE}/optional-holidays", json={"holiday_id": holiday_id})
        assert r.status_code == 201, r.text
    assert worker.post(f"{BASE}/optional-holidays", json={"holiday_id": ids[0]}).status_code == 409
    r = worker.post(f"{BASE}/optional-holidays", json={"holiday_id": ids[2]})
    assert r.status_code == 409 and r.json()["code"] == "OPTIONAL_QUOTA_EXCEEDED"
    assert worker.post(f"{BASE}/optional-holidays", json={"holiday_id": fixed}).status_code == 404
    view = worker.get(f"{BASE}/optional-holidays", params={"year": 2099}).json()
    assert view["used"] == 2 and [h["chosen"] for h in view["holidays"]] == [True, True, False]
    assert worker.post(f"{BASE}/optional-holidays/{ids[0]}/cancel").status_code == 200
    assert worker.post(f"{BASE}/optional-holidays", json={"holiday_id": ids[2]}).status_code == 201
    other = signup_company(app, "Beta Industries")
    assert other.post(f"{BASE}/optional-holidays", json={"holiday_id": ids[1]}).status_code in (403, 404)
    assert worker.get(f"{BASE}/optional-holidays", params={"year": 2099, "employee_id": acme["peer_id"]}
                      ).status_code == 403
    assert admin.get(f"{BASE}/optional-holidays", params={"year": 2099, "employee_id": acme["worker_id"]}
                     ).json()["used"] == 2
    log = [r["change"] for r in _sql("SELECT change FROM source_change_log WHERE record_type = 'optional_holiday' "
                                     "ORDER BY id")]
    assert log == ["CREATED", "CREATED", "VOIDED", "CREATED"]


def test_EC_HOL_05_an_availed_optional_holiday_is_not_charged_as_leave(acme):
    admin, worker, boss = acme["admin"], acme["worker"], acme["boss"]
    holiday = admin.post("/api/v1/holidays", json={"name": "Choice", "date": "2099-06-03", "is_optional": True}).json()
    leave = worker.post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": "2099-06-02",
                                                      "end_date": "2099-06-04"}).json()
    assert leave["number_of_days"] == 3  # an optional holiday is a working day until chosen
    boss.post(f"/api/v1/leaves/{leave['id']}/approve", json={})
    assert acme["hr"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}).json()["status"] == "approved"
    assert worker.post(f"{BASE}/optional-holidays", json={"holiday_id": holiday["id"]}).status_code == 201
    balance = worker.get("/api/v1/leaves/my/balance", params={"year": 2099}).json()["casual"]
    assert balance["used"] == 2


# ---------------------------------------------------------------------------
# Attendance: late / early-exit minutes, versions and the change log
# ---------------------------------------------------------------------------
def _tz(company_id):
    return pytz.timezone(_sql("SELECT timezone FROM companies WHERE id = :c", c=company_id)[0]["timezone"] or "UTC")


def _at(tz, day, hhmm):
    y, m, d = (int(x) for x in day.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    return tz.localize(datetime(y, m, d, h, mi)).isoformat()


def _shift(admin, name, start, end, duration=None):
    r = admin.post("/api/v1/shifts", json={"name": name, "start_time": start, "end_time": end,
                                           "duration_hours": duration})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _assign_shift(admin, employee_id, shift_id):
    r = admin.put(f"/api/v1/employees/{employee_id}", json={"shift_id": shift_id})
    assert r.status_code == 200, r.text


def test_late_and_early_exit_minutes_on_correction(acme):
    admin, cid, eid = acme["hr"], acme["cid"], acme["worker_id"]
    tz = _tz(cid)
    _assign_shift(acme["admin"], eid, _shift(acme["admin"], "Day", "09:00", "18:00", 8.5))
    r = admin.post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2026-10-12", "check_in_time": _at(tz, "2026-10-12", "09:20"),
        "check_out_time": _at(tz, "2026-10-12", "17:30"), "status": "late"})
    assert r.status_code == 200, r.text
    first = r.json()
    assert (first["late_minutes"], first["early_exit"], first["early_exit_minutes"]) == (20, True, 30)
    assert first["row_version"] == 1
    r = admin.post("/api/v1/attendance/manual", json={
        "attendance_id": first["id"], "employee_id": eid, "date": "2026-10-12",
        "check_in_time": _at(tz, "2026-10-12", "08:55"), "check_out_time": _at(tz, "2026-10-12", "18:05"),
        "status": "present"})
    assert r.status_code == 200, r.text
    second = r.json()
    assert (second["late_minutes"], second["early_exit"], second["early_exit_minutes"]) == (0, False, 0)
    assert second["row_version"] == 2
    log = _sql("SELECT change, version_hash, details FROM source_change_log WHERE record_type = 'attendance' "
               "AND record_id = :r ORDER BY id", r=first["id"])
    assert [l["change"] for l in log] == ["CREATED", "UPDATED"]  # EC-ATT-05: each version is detectable
    assert log[0]["version_hash"] != log[1]["version_hash"] and log[1]["details"]["row_version"] == 2


def test_EC_ATT_07_overnight_shift_minutes(acme):
    admin, cid, eid = acme["hr"], acme["cid"], acme["worker_id"]
    tz = _tz(cid)
    _assign_shift(acme["admin"], eid, _shift(acme["admin"], "Night", "22:00", "06:00"))
    r = admin.post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2026-10-12", "check_in_time": _at(tz, "2026-10-12", "22:10"),
        "check_out_time": _at(tz, "2026-10-13", "05:00"), "status": "present"})
    assert r.status_code == 200, r.text
    assert (r.json()["late_minutes"], r.json()["early_exit_minutes"]) == (10, 60)
    # Punched in after midnight: still measured against the start of the evening before.
    r = admin.post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2026-10-14", "check_in_time": _at(tz, "2026-10-14", "00:30"),
        "check_out_time": _at(tz, "2026-10-14", "06:30"), "status": "late"})
    assert r.status_code == 200, r.text
    assert (r.json()["late_minutes"], r.json()["early_exit"], r.json()["early_exit_minutes"]) == (150, False, 0)


def test_punch_out_records_late_and_early_exit_minutes(app, fake_clock):  # noqa: F811
    from tests.attendance_helpers import act, employee_with_login, meta, set_policy

    admin = signup_company(app, "Punch Co")
    set_policy(admin, requireFaceForSelfCheckIn=False)
    emp = employee_with_login(app, admin, "Asha")
    _assign_shift(admin, emp.employee["id"], _shift(admin, "Day", "09:00", "18:00", 9))
    fake_clock.at_ist("2026-10-19", "09:25")
    assert act(emp, "PUNCH_IN", meta(fake_clock)).status_code == 200
    fake_clock.at_ist("2026-10-19", "17:40")
    r = act(emp, "PUNCH_OUT", meta(fake_clock))
    assert r.status_code == 200, r.text
    row = _sql("SELECT id, late_minutes, early_exit, early_exit_minutes, row_version FROM attendance")[0]
    assert (row["late_minutes"], row["early_exit"], row["early_exit_minutes"], row["row_version"]) == (25, True, 20, 2)
    log = _sql("SELECT change, actor_user_id FROM source_change_log WHERE record_type = 'attendance' AND record_id = :r",
               r=row["id"])
    assert log == [{"change": "UPDATED", "actor_user_id": emp.user_id}]


def test_flexible_or_missing_shift_leaves_minutes_unknown(acme):
    admin, cid, eid = acme["hr"], acme["cid"], acme["peer_id"]
    tz = _tz(cid)
    r = admin.post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2026-10-12", "check_in_time": _at(tz, "2026-10-12", "11:00"),
        "check_out_time": _at(tz, "2026-10-12", "12:00"), "status": "present"})
    assert r.status_code == 200, r.text
    assert (r.json()["late_minutes"], r.json()["early_exit"], r.json()["early_exit_minutes"]) == (None, False, None)


def test_shift_duration_accepts_decimal_hours(acme):
    admin = acme["admin"]
    _shift(admin, "Long", "08:00", "17:00", 8.5)
    shifts = {s["name"]: s for s in admin.get("/api/v1/shifts").json()}
    assert float(shifts["Long"]["duration_hours"]) == 8.5
    bad = admin.post("/api/v1/shifts", json={"name": "Bad", "start_time": "08:00", "end_time": "17:00",
                                             "duration_hours": 25})
    assert bad.status_code == 422


def test_EC_ATT_22_attendance_payload_stays_backward_compatible(acme):
    admin, cid, eid = acme["hr"], acme["cid"], acme["worker_id"]
    tz = _tz(cid)
    r = admin.post("/api/v1/attendance/manual", json={
        "employee_id": eid, "date": "2026-10-12", "check_in_time": _at(tz, "2026-10-12", "09:00"),
        "check_out_time": _at(tz, "2026-10-12", "18:00"), "status": "present"})
    body = r.json()
    for key in ("id", "employee_id", "date", "check_in_time", "check_out_time", "working_seconds", "status", "source",
                "is_manual", "hours", "breaks", "attendance_state", "closed_reason"):
        assert key in body
    assert body["working_seconds"] == 9 * 3600


# ---------------------------------------------------------------------------
# Three-tenant isolation of every new table
# ---------------------------------------------------------------------------
def test_new_time_input_tables_are_isolated_across_three_tenants(app):
    tenants = []
    for name in ("Acme Corp", "Beta Industries", "Cobalt Labs"):
        admin = signup_company(app, name)
        boss = create_employee(admin, "Boss")
        worker = create_employee(admin, "Worker", manager_id=boss["id"])
        boss_client = invite_and_activate(app, admin, boss, ["manager"])
        worker_client = invite_and_activate(app, admin, worker, ["employee"])
        leave = worker_client.post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": "2099-06-01",
                                                                 "end_date": "2099-06-01"}).json()
        boss_client.post(f"/api/v1/leaves/{leave['id']}/approve", json={})
        assert _lop(admin, worker["id"], "2099-06-02").status_code == 201
        assert admin.post(f"{BASE}/suspensions", json={"employee_id": worker["id"], "start_date": "2099-06-08",
                                                       "end_date": "2099-06-09", "reason": "x"}).status_code == 201
        assert worker_client.post(f"{BASE}/on-duty", json={"date": "2099-06-03", "reason": "x"}).status_code == 201
        holiday = admin.post("/api/v1/holidays", json={"name": "Choice", "date": "2099-06-10",
                                                       "is_optional": True}).json()
        assert worker_client.post(f"{BASE}/optional-holidays", json={"holiday_id": holiday["id"]}).status_code == 201
        tenants.append({"admin": admin, "cid": admin.company_id, "worker": worker})

    for tenant in tenants:
        db = SessionLocal()
        try:
            bind_tenant(db, tenant["cid"])
            for table in NEW_TABLES:
                companies = {r[0] for r in db.execute(text(f"SELECT DISTINCT company_id FROM {table}"))}
                assert companies == {tenant["cid"]}, table
        finally:
            db.close()
    a, b = tenants[0], tenants[1]
    lop_b = b["admin"].get(f"{BASE}/lop-inputs").json()[0]
    assert a["admin"].post(f"{BASE}/lop-inputs/{lop_b['id']}/cancel").status_code == 404
    assert a["admin"].get(f"{BASE}/lop-inputs", params={"employee_id": b["worker"]["id"]}).json() == []
    assert a["admin"].get(f"{BASE}/employees/{b['worker']['id']}/day-ledger-preview",
                          params={"start": "2099-06-01", "end": "2099-06-30"}).status_code == 404
    # RLS also stops a forged insert into another tenant.
    db = SessionLocal()
    try:
        bind_tenant(db, a["cid"])
        with pytest.raises(Exception):
            db.execute(text("INSERT INTO lop_inputs (company_id, employee_id, date, portion, reason, status, row_version, "
                            "created_at, updated_at) VALUES (:c, :e, '2099-06-05', 'FULL', 'x', 'PENDING', 1, now(), now())"),
                       {"c": b["cid"], "e": b["worker"]["id"]})
            db.flush()
        db.rollback()
    finally:
        db.close()


def test_source_change_log_is_append_only(acme):
    assert _lop(acme["admin"], acme["worker_id"], "2099-06-02").status_code == 201
    acme["admin"].post("/api/v1/holidays", json={"name": "Any", "date": "2099-06-10"})
    db = SessionLocal()
    try:
        bind_tenant(db, acme["cid"])
        for statement in ("DELETE FROM source_change_log", "UPDATE source_change_log SET change = 'DELETED'"):
            with pytest.raises(Exception):
                db.execute(text(statement))
                db.flush()
            db.rollback()
            bind_tenant(db, acme["cid"])
    finally:
        db.close()
    assert _sql("SELECT count(*) AS n FROM source_change_log")[0]["n"] == 1
