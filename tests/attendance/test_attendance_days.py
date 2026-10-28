"""Evaluation write path and the payroll read of v2 tenants (AS-M3; 04-attendance/03 §2.5, §2.6, §8.3; AS 05 §4–§7).

* v1 tenants: every ``/attendance/days`` path answers 404, a STANDARD rules version is refused (V-AS-02, 409
  ``ATTENDANCE_V2_REQUIRED``) and ``attendance.v2.enabled`` cannot be written through the settings API.
* v2 tenants: an OPEN day is written in place (no version, no change log); the first write after the close point is
  version 1 + ``log_change('attendance_day')``; an unchanged ``inputs_hash`` writes nothing; a STANDARD rules version
  re-evaluates the day (version 2, UPDATED); SHADOW mode writes rows and versions but never the change log.
* Payroll (JC-AS-01, -13): ``ledger_inputs`` takes the STANDARD evaluation through ``DAY_RESULT_TO_LEDGER`` with the
  lineage ``attendance_day:{id}:v{version}``; another tenant's admin gets 404.
"""

from datetime import date, datetime, timezone

import pytest

from tests.attendance.capture_v2_helpers import rows, sql, v2_punch
from tests.attendance_helpers import employee_with_login, fake_clock  # noqa: F401
from tests.helpers import signup_company
from tests.time_goldens import tenant_db

DAY = date(2026, 11, 3)  # Tuesday


@pytest.fixture
def tenant(app):
    admin = signup_company(app, "Evaluation Co")
    emp = employee_with_login(app, admin, "Vikram")
    return {"admin": admin, "emp": emp, "emp_id": emp.me()["employee"]["id"], "cid": admin.company_id}


def enable_v2(company_id):
    """What the cut-over writes (attendance_access.set_v2_enabled); STARTER_V1 tenants already hold a false row."""
    updated = sql("UPDATE setting_values SET value = 'true'::jsonb WHERE company_id = :c "
                  "AND key = 'attendance.v2.enabled' AND scope_type = 'TENANT' AND status = 'ACTIVE'", c=company_id)
    if updated.rowcount == 0:
        sql("INSERT INTO setting_values (company_id, key, scope_type, value, locked, effective_from, status, "
            "row_version, source, created_at, updated_at) VALUES (:c, 'attendance.v2.enabled', 'TENANT', "
            "'true'::jsonb, false, '2026-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id)


def work_day(tenant, clock):
    clock.at_ist("2026-11-03", "09:20")
    assert v2_punch(tenant["emp"], clock).status_code == 200
    clock.at_ist("2026-11-03", "18:05")
    assert v2_punch(tenant["emp"], clock, "OUT").status_code == 200


def recompute(admin, emp_id, start=DAY, end=DAY):
    r = admin.post("/api/v1/attendance/days/recompute",
                   json={"employee_ids": [emp_id], "start": start.isoformat(), "end": end.isoformat(),
                         "reason": "AS-M3 test"})
    assert r.status_code == 200, r.text
    return r.json()


def day_rows(emp_id):
    return rows("SELECT * FROM attendance_days WHERE employee_id = :e ORDER BY shift_date", e=emp_id)


def versions(emp_id):
    return rows("SELECT v.* FROM attendance_day_versions v JOIN attendance_days d ON d.id = v.day_id "
                "WHERE d.employee_id = :e ORDER BY v.version", e=emp_id)


def change_log(emp_id):
    return rows("SELECT * FROM source_change_log WHERE record_type = 'attendance_day' AND employee_id = :e "
                "ORDER BY id", e=emp_id)


def activate_rules(admin, fields, status=200):
    r = admin.post("/api/v1/attendance/policies", json={"type": "ATTENDANCE_RULES_POLICY", "name": "Staff rules",
                                                        "fields": fields})
    assert r.status_code == 201, r.text
    policy = r.json()
    r = admin.put(f"/api/v1/attendance/policies/{policy['id']}/assignments",
                  json={"rules": [{"criteria": {"everyone": True}, "priority": 10}]})
    assert r.status_code == 200, r.text
    r = admin.post(f"/api/v1/attendance/policies/{policy['id']}/versions/1/activate",
                   json={"effective_from": "2026-01-01", "reason": "AS-M3 test"})
    assert r.status_code == status, r.text
    return r


def test_v1_tenant_paths_are_404_and_v2_cannot_be_typed(tenant, fake_clock):
    admin, emp_id = tenant["admin"], tenant["emp_id"]
    assert admin.get(f"/api/v1/attendance/days/{emp_id}/{DAY}").status_code == 404
    assert admin.get("/api/v1/attendance/days", params={"employee_id": emp_id, "start": str(DAY),
                                                        "end": str(DAY)}).status_code == 404
    r = admin.post("/api/v1/attendance/days/recompute",
                   json={"employee_ids": [emp_id], "start": str(DAY), "end": str(DAY), "reason": "nope"})
    assert r.status_code == 404
    refused = activate_rules(admin, {"threshold_mode": "ABSOLUTE"}, status=409)
    assert refused.json()["code"] == "ATTENDANCE_V2_REQUIRED"
    typed = admin.put("/api/v1/payroll/settings/values",
                      json={"changes": [{"key": "attendance.v2.enabled", "value": True, "row_version": 0}]})
    assert typed.status_code == 409 and typed.json()["code"] == "SETTING_SYSTEM_WRITTEN"
    work_day(tenant, fake_clock)
    assert day_rows(emp_id) == []  # v1: no rows outside reconciliation


def test_write_path_open_then_final_then_standard(tenant, fake_clock):
    admin, emp_id, cid = tenant["admin"], tenant["emp_id"], tenant["cid"]
    enable_v2(cid)
    work_day(tenant, fake_clock)

    fake_clock.at_ist("2026-11-03", "18:10")  # before the close point: OPEN, in place
    recompute(admin, emp_id)
    [row] = day_rows(emp_id)
    assert row["evaluation_state"] == "OPEN" and row["version"] == 1 and row["rules_mode"] == "LEGACY_V1"
    assert versions(emp_id) == [] and change_log(emp_id) == []

    fake_clock.at_ist("2026-11-05", "10:00")  # after the close point: version 1 + CREATED
    recompute(admin, emp_id)
    [row] = day_rows(emp_id)
    assert row["evaluation_state"] == "FINAL" and row["day_result"] == "PRESENT" and row["net_seconds"] == 525 * 60
    assert [v["version"] for v in versions(emp_id)] == [1]
    log = change_log(emp_id)
    assert [(c["change"], c["date_from"]) for c in log] == [("CREATED", DAY)]

    again = recompute(admin, emp_id)  # unchanged inputs: nothing written
    assert again["unchanged"] == 1 and again["versions"] == 0 and len(change_log(emp_id)) == 1

    activate_rules(admin, {"threshold_mode": "ABSOLUTE", "full_day_min_minutes": "450",
                           "half_day_min_minutes": "240"})
    recompute(admin, emp_id)
    [row] = day_rows(emp_id)
    assert row["rules_mode"] == "STANDARD" and row["version"] == 2 and row["day_result"] == "PRESENT"
    history = versions(emp_id)
    assert [v["version"] for v in history] == [1, 2] and "rules_mode" in history[1]["outputs"]
    assert [c["change"] for c in change_log(emp_id)] == ["CREATED", "UPDATED"]
    assert change_log(emp_id)[-1]["record_id"] == row["id"]

    body = admin.get(f"/api/v1/attendance/days/{emp_id}/{DAY}").json()["day"]
    assert body["version"] == 2 and body["day_result"] == "PRESENT" and len(body["history"]) == 2
    explained = admin.get(f"/api/v1/attendance/days/{emp_id}/{DAY}", params={"explain": "true"}).json()
    assert explained["explain"]["phase"] == "AFTER_CLOSE" and explained["explain"]["rules_mode"] == "STANDARD"
    assert len(versions(emp_id)) == 2  # explain never writes

    # Payroll reads the evaluation (JC-AS-01, -13).
    from app.payroll.services import time_inputs as ti

    with tenant_db(cid) as db:
        [ledger_day] = ti.ledger_inputs(db, cid, emp_id, DAY, DAY)
        att = ledger_day.attendance
        assert att.session_id == f"attendance_day:{row['id']}:v2" and att.rules_mode == "STANDARD"
        assert att.worked_seconds == 525 * 60 and att.content_hash == row["content_hash"]


def test_shadow_mode_never_logs_and_other_tenants_get_404(tenant, fake_clock, app):
    admin, emp_id, cid = tenant["admin"], tenant["emp_id"], tenant["cid"]
    work_day(tenant, fake_clock)
    from app.models.attendance import AttendanceDay, AttendanceDayVersion
    from app.models.time_inputs import SourceChangeLog
    from app.services import attendance_days

    with tenant_db(cid) as db:
        now = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)
        result = attendance_days.evaluate_range(db, cid, [emp_id], DAY, DAY, trigger="BACKFILL", mode="SHADOW",
                                                now=now)
        assert result.written == 1 and result.versions == 1 and result.changes_logged == 0
        assert db.query(AttendanceDay).count() == 1 and db.query(AttendanceDayVersion).count() == 1
        assert db.query(SourceChangeLog).filter(SourceChangeLog.record_type == "attendance_day").count() == 0
        repeat = attendance_days.evaluate_range(db, cid, [emp_id], DAY, DAY, trigger="BACKFILL", mode="SHADOW",
                                                now=now)
        assert repeat.unchanged == 1 and repeat.written == 0
    enable_v2(cid)
    other = signup_company(app, "Other Evaluation Co")
    enable_v2(other.company_id)
    assert other.get(f"/api/v1/attendance/days/{emp_id}/{DAY}").status_code == 404
    assert tenant["emp"].get(f"/api/v1/attendance/days/{emp_id}/{DAY}").status_code in (403, 404)


def test_change_log_accepts_bigint_record_ids(tenant):
    """JC-AS-11b: record_id is BIGINT and the new record types pass the CHECK."""
    for record_type in ("attendance_day", "attendance_request", "roster_entry"):
        sql("INSERT INTO source_change_log (company_id, record_type, record_id, change, version_hash, changed_at, "
            "created_at, updated_at) VALUES (:c, :t, 5000000000, 'CREATED', :h, now(), now(), now())",
            c=tenant["cid"], t=record_type, h="0" * 64)
    assert len(rows("SELECT id FROM source_change_log WHERE record_id = 5000000000")) == 3
