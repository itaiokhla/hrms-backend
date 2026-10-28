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


def day_rows(emp_id, day=None):
    if day is not None:  # policy activation recomputes the whole covered window (§8.1 POLICY_CHANGED)
        return rows("SELECT * FROM attendance_days WHERE employee_id = :e AND shift_date = :d", e=emp_id, d=day)
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
                           "half_day_min_minutes": "240"})  # schedules POLICY_CHANGED (inline job)
    recompute(admin, emp_id)
    [row] = day_rows(emp_id, DAY)
    assert row["rules_mode"] == "STANDARD" and row["version"] == 2 and row["day_result"] == "PRESENT"
    history = [v for v in versions(emp_id) if v["day_id"] == row["id"]]
    assert [v["version"] for v in history] == [1, 2] and "rules_mode" in history[1]["outputs"]
    tuesday = [c for c in change_log(emp_id) if c["record_id"] == row["id"]]
    assert [c["change"] for c in tuesday] == ["CREATED", "UPDATED"]
    assert history[1]["trigger"] == "POLICY_CHANGED"  # the activation's own recompute changed the day

    body = admin.get(f"/api/v1/attendance/days/{emp_id}/{DAY}").json()["day"]
    assert body["version"] == 2 and body["day_result"] == "PRESENT" and len(body["history"]) == 2
    explained = admin.get(f"/api/v1/attendance/days/{emp_id}/{DAY}", params={"explain": "true"}).json()
    assert explained["explain"]["phase"] == "AFTER_CLOSE" and explained["explain"]["rules_mode"] == "STANDARD"
    assert len([v for v in versions(emp_id) if v["day_id"] == row["id"]]) == 2  # explain never writes

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


# --------------------------------------------------------------------------- automatic triggers (IG-AS-71)


def test_punch_evaluates_its_day_on_v2_tenants_only(tenant, fake_clock):
    work_day(tenant, fake_clock)
    assert day_rows(tenant["emp_id"]) == []  # v1: the punch writes no evaluation
    enable_v2(tenant["cid"])
    fake_clock.at_ist("2026-11-04", "09:05")
    assert v2_punch(tenant["emp"], fake_clock).status_code == 200
    [row] = day_rows(tenant["emp_id"])
    assert row["shift_date"] == date(2026, 11, 4) and row["evaluation_state"] == "OPEN" and row["trigger"] == "PUNCH"
    fake_clock.at_ist("2026-11-04", "17:30")
    assert v2_punch(tenant["emp"], fake_clock, "OUT").status_code == 200
    [row] = day_rows(tenant["emp_id"])
    assert row["trigger"] == "SESSION_CLOSED" and row["net_seconds"] == (8 * 60 + 25) * 60


def test_change_log_schedules_recompute_for_v2_tenants(tenant, fake_clock):
    from app.models.jobs import Job
    from app.payroll.services import time_inputs as ti
    from app.services import attendance_days

    cid, emp_id = tenant["cid"], tenant["emp_id"]
    work_day(tenant, fake_clock)
    with tenant_db(cid) as db:
        ti.log_change(db, cid, record_type="on_duty", record_id=11, change="UPDATED", content={"v": 1},
                      employee_id=emp_id, date_from=DAY)
        assert db.query(Job).filter(Job.type == attendance_days.RECOMPUTE_JOB).count() == 0  # v1 tenant
    enable_v2(cid)
    fake_clock.at_ist("2026-11-06", "10:00")
    with tenant_db(cid) as db:
        ti.log_change(db, cid, record_type="on_duty", record_id=11, change="UPDATED", content={"v": 2},
                      employee_id=emp_id, date_from=DAY)
        ti.log_change(db, cid, record_type="lop_input", record_id=3, change="UPDATED", content={"v": 1},
                      employee_id=emp_id, date_from=DAY)  # pay-only: no evaluation change
        [job] = db.query(Job).filter(Job.type == attendance_days.RECOMPUTE_JOB).all()
        assert job.payload["trigger"] == "ON_DUTY_DECIDED" and job.payload["date_from"] == DAY.isoformat()
        result = attendance_days.recompute_scope(db, cid, job.payload)
        assert result["written"] == 1
        row = attendance_days.get_day(db, cid, emp_id, DAY)
        assert row.trigger == "ON_DUTY_DECIDED" and row.evaluation_state == "FINAL"


# --------------------------------------------------------------------------- cut-over (spine §15.1; exit criteria 1, 4)


def test_cutover_prepare_reconcile_refusals_and_activation(tenant, fake_clock, app):
    from tests.attendance.capture_v2_helpers import step_up

    admin, cid, emp_id = tenant["admin"], tenant["cid"], tenant["emp_id"]
    hr = employee_with_login(app, admin, "Ananya", roles=("hr_admin",))
    work_day(tenant, fake_clock)
    fake_clock.at_ist("2026-11-06", "10:00")
    assert admin.get("/api/v1/attendance/setup/status").json()["state"] == "LEGACY"
    early = admin.post("/api/v1/attendance/setup/reconcile", json={"start": "2026-11-02", "end": "2026-11-05"})
    assert early.status_code == 409 and early.json()["code"] == "ATTENDANCE_V2_NOT_PREPARED"
    prepared = admin.post("/api/v1/attendance/setup/prepare")
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["rules_policy_id"] and prepared.json()["scheduling_policy_id"]
    assert admin.post("/api/v1/attendance/setup/prepare").json()["rules_policy_id"] is None  # idempotent
    started = admin.post("/api/v1/attendance/setup/reconcile", json={"start": "2026-11-02", "end": "2026-11-05"})
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    # JOBS_MODE=inline (tests/conftest.py): the reconcile job ran when the request committed.
    report = admin.get("/api/v1/attendance/setup/status").json()
    assert report["state"] == "RECONCILING" and report["last_reconcile"]["differences"] == 0, report
    assert report["last_reconcile"]["status"] == "SUCCEEDED"
    shadow = day_rows(emp_id)
    assert len(shadow) == 4 and {r["rules_mode"] for r in shadow} == {"LEGACY_V1"} and change_log(emp_id) == []

    # An injected difference is reported by the comparison (exit criterion 4).
    from app.services import attendance_cutover

    sql("UPDATE attendance_days SET net_seconds = net_seconds + 60 WHERE employee_id = :e AND shift_date = :d",
        e=emp_id, d=DAY)
    with tenant_db(cid) as db:
        found = attendance_cutover.compare(db, cid, [emp_id], date(2026, 11, 2), date(2026, 11, 5))
    assert [(f["date"], f["field"]) for f in found] == [(DAY.isoformat(), "worked_seconds")]
    sql("UPDATE jobs SET result = jsonb_set(result, '{differences}', '1') WHERE id = :j", j=job_id)
    refused = hr.post("/api/v1/attendance/setup/cutover", json={"reconcile_job_id": job_id})
    assert refused.status_code == 409 and refused.json()["code"] == "ATTENDANCE_V2_NOT_RECONCILED"
    sql("UPDATE jobs SET result = jsonb_set(result, '{differences}', '0') WHERE id = :j", j=job_id)

    maker = admin.post("/api/v1/attendance/setup/cutover", json={"reconcile_job_id": job_id})
    assert maker.status_code == 409 and maker.json()["code"] == "SELF_DECISION_FORBIDDEN"
    unverified = hr.post("/api/v1/attendance/setup/cutover", json={"reconcile_job_id": job_id})
    assert unverified.status_code == 403 and unverified.json()["code"] == "STEP_UP_REQUIRED"
    step_up(hr)
    sql("UPDATE user_sessions SET step_up_at = :t WHERE step_up_at IS NOT NULL", t=fake_clock.now)  # fake clock
    activated = hr.post("/api/v1/attendance/setup/cutover", json={"reconcile_job_id": job_id})
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "V2_ACTIVE"
    audit = rows("SELECT action FROM audit_logs WHERE company_id = :c AND module = 'Attendance' "
                 "AND action LIKE 'ATTENDANCE_V2_%' ORDER BY id", c=cid)
    assert [a["action"] for a in audit] == ["ATTENDANCE_V2_RECONCILED", "ATTENDANCE_V2_ACTIVATED"]
    again = hr.post("/api/v1/attendance/setup/cutover", json={"reconcile_job_id": job_id})
    assert again.status_code == 409 and again.json()["code"] == "ATTENDANCE_V2_ACTIVE"
    assert tenant["emp"].get("/api/v1/attendance/setup/status").status_code == 403
    other = signup_company(app, "Foreign Cutover Co")
    assert other.post("/api/v1/attendance/setup/cutover", json={"reconcile_job_id": job_id}).status_code == 404


def test_exceptions_list_decided_days_in_scope(tenant, fake_clock):
    admin, emp_id, cid = tenant["admin"], tenant["emp_id"], tenant["cid"]
    enable_v2(cid)
    work_day(tenant, fake_clock)  # Tue 3 Nov worked; Wed 4 and Thu 5 Nov not worked
    fake_clock.at_ist("2026-11-06", "10:00")
    recompute(admin, emp_id, DAY, date(2026, 11, 5))
    body = admin.get("/api/v1/attendance/exceptions", params={"start": "2026-11-01", "end": "2026-11-05"}).json()
    by_date = {item["shift_date"]: item for item in body["items"] if item["employee_id"] == emp_id}
    assert "2026-11-04" in by_date and "UNMARKED_DAY" in by_date["2026-11-04"]["exception_codes"], body
    only = admin.get("/api/v1/attendance/exceptions", params={"start": "2026-11-01", "end": "2026-11-05",
                                                              "code": "UNMARKED_DAY"}).json()
    assert only["total"] >= 1 and all("UNMARKED_DAY" in item["exception_codes"] for item in only["items"])
    bad = admin.get("/api/v1/attendance/exceptions", params={"start": "2026-11-01", "end": "2026-11-05",
                                                             "code": "NOPE"})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_EXCEPTION_CODE"
    mine = tenant["emp"].get("/api/v1/attendance/exceptions", params={"start": "2026-11-01", "end": "2026-11-05"})
    assert mine.status_code in (200, 403)
    if mine.status_code == 200:
        assert {item["employee_id"] for item in mine.json()["items"]} <= {emp_id}


def test_loader_query_count_does_not_grow_with_head_count(tenant, app):
    """Spine §5.11: one chunk reads with a fixed number of queries (IG-AS-72 records the measured bound)."""
    from sqlalchemy import event

    from app.services import attendance_days

    admin, cid = tenant["admin"], tenant["cid"]
    extra = [employee_with_login(app, admin, f"Load{n}").me()["employee"]["id"] for n in range(4)]
    now = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)

    def count(ids):
        with tenant_db(cid) as db:
            seen = []

            def listener(*args, **kwargs):
                seen.append(1)

            engine = db.get_bind()
            event.listen(engine, "before_cursor_execute", listener)
            try:
                inputs = attendance_days.load_inputs_many(db, cid, ids, date(2026, 11, 1), date(2026, 11, 7),
                                                          now=now, v2=True)
            finally:
                event.remove(engine, "before_cursor_execute", listener)
            assert len(inputs) == len(ids) * 7
            return len(seen)

    small, large = count([tenant["emp_id"]]), count([tenant["emp_id"], *extra])
    print(f"loader statements: 1 employee {small}, 5 employees {large}")
    assert large <= small + 2, (small, large)
    # IG-AS-77: one LH day-facts pass, shared shifts, batched policies and settings (was 112 before batching).
    assert large <= 60, (small, large)


# --------------------------------------------------------------------------- locked periods, preview, attestation


def test_configuration_triggers_leave_locked_periods_alone(tenant, fake_clock, monkeypatch):
    """§8.5 / D-AS-44: POLICY_CHANGED / SETTING_CHANGED / SHIFT_VERSION skip INPUT_LOCKED and FINALIZED dates unless
    apply_to_locked_periods; facts always recompute."""
    from app.services import attendance_days

    cid, emp_id = tenant["cid"], tenant["emp_id"]
    enable_v2(cid)
    work_day(tenant, fake_clock)
    now = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)
    with tenant_db(cid) as db:
        assert set(attendance_days.period_states(db, cid, [emp_id], DAY, DAY, now=now).values()) == {"OPEN"}
    monkeypatch.setattr(attendance_days, "period_states",
                        lambda db, cid, ids, start, end, now=None: {(e, DAY): "INPUT_LOCKED" for e in ids})
    with tenant_db(cid) as db:
        skipped = attendance_days.evaluate_range(db, cid, [emp_id], DAY, DAY, trigger="POLICY_CHANGED", now=now)
        assert skipped.skipped_locked == 1 and skipped.written == 0
        forced = attendance_days.evaluate_range(db, cid, [emp_id], DAY, DAY, trigger="POLICY_CHANGED", now=now,
                                                apply_to_locked_periods=True)
        assert forced.written == 1 and forced.skipped_locked == 0
    with tenant_db(cid) as db:
        fact = attendance_days.evaluate_range(db, cid, [emp_id], DAY, DAY, trigger="LEAVE_CHANGED", now=now)
        assert fact.written == 1 and fact.skipped_locked == 0


def test_impact_preview_then_activation_recomputes(tenant, fake_clock):
    admin, cid, emp_id = tenant["admin"], tenant["cid"], tenant["emp_id"]
    enable_v2(cid)
    work_day(tenant, fake_clock)  # 525 net minutes on Tue 3 Nov
    fake_clock.at_ist("2026-11-06", "10:00")
    recompute(admin, emp_id)
    assert day_rows(emp_id, DAY)[0]["day_result"] == "PRESENT"
    r = admin.post("/api/v1/attendance/policies", json={
        "type": "ATTENDANCE_RULES_POLICY", "name": "Strict staff rules",
        "fields": {"threshold_mode": "ABSOLUTE", "full_day_min_minutes": "600", "half_day_min_minutes": "240"}})
    assert r.status_code == 201, r.text
    policy_id = r.json()["id"]
    preview = admin.post(f"/api/v1/attendance/policies/{policy_id}/versions/1/simulate",
                         json={"effective_from": "2026-11-02",
                               "assignments": [{"criteria": {"everyone": True}, "priority": 10}]})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["population"]["covered_now"] == 0 and body["population"]["covered_after"] >= 1
    assert body["changes"]["day_result"].get("PRESENT -> HALF_DAY", 0) >= 1, body["changes"]
    assert body["dates"]["OPEN"] >= 1 and emp_id in {item["employee_id"] for item in body["sample"]}
    assert day_rows(emp_id, DAY)[0]["day_result"] == "PRESENT"  # the preview never writes
    assert admin.put(f"/api/v1/attendance/policies/{policy_id}/assignments",
                     json={"rules": [{"criteria": {"everyone": True}, "priority": 10}]}).status_code == 200
    locked = admin.post(f"/api/v1/attendance/policies/{policy_id}/versions/1/activate",
                        json={"effective_from": "2026-11-02", "apply_to_locked_periods": True})
    assert locked.status_code == 400 and locked.json()["code"] == "REASON_REQUIRED"
    activated = admin.post(f"/api/v1/attendance/policies/{policy_id}/versions/1/activate",
                           json={"effective_from": "2026-11-02", "reason": "stricter thresholds"})
    assert activated.status_code == 200, activated.text
    assert activated.json()["recompute_job_id"]
    [row] = day_rows(emp_id, DAY)  # JOBS_MODE=inline: the recompute ran when the activation committed
    assert row["rules_mode"] == "STANDARD" and row["day_result"] == "HALF_DAY" and row["trigger"] == "POLICY_CHANGED"


def test_punch_out_attestation_reaches_the_evaluation(tenant, fake_clock):
    from app.services import attendance_days

    cid, emp_id = tenant["cid"], tenant["emp_id"]
    fake_clock.at_ist("2026-11-03", "09:20")
    assert v2_punch(tenant["emp"], fake_clock).status_code == 200
    fake_clock.at_ist("2026-11-03", "18:05")
    assert v2_punch(tenant["emp"], fake_clock, "OUT", no_break_taken=True).status_code == 200
    with tenant_db(cid) as db:
        inputs = attendance_days.load_inputs_many(db, cid, [emp_id], DAY, DAY,
                                                  now=datetime(2026, 11, 6, 6, tzinfo=timezone.utc), v2=True)
        [session] = inputs[(emp_id, DAY)].sessions
        assert session.no_break_attested is True


def test_rosters_read_the_scheduling_policy_limits(tenant, fake_clock):
    """IG-AS-79: SCHEDULING_POLICY min_rest_hours / max_hours_per_week set the roster limits; none = IG-AS-31."""
    from app.services import roster_validation

    admin, cid, emp_id = tenant["admin"], tenant["cid"], tenant["emp_id"]
    fake_clock.at_ist("2026-11-06", "10:00")  # after the tenant's settings rows start (the fixture starts 19 Oct)
    with tenant_db(cid) as db:
        before = roster_validation.limits_for(db, cid, [emp_id], DAY).per_employee[emp_id]
    assert (before.min_rest_hours.value, before.weekly_max_hours.value) == (11, 48)
    enable_v2(cid)
    r = admin.post("/api/v1/attendance/policies", json={"type": "SCHEDULING_POLICY", "name": "Plant schedules",
                                                        "fields": {"min_rest_hours": 12, "max_hours_per_week": 40}})
    assert r.status_code == 201, r.text
    policy_id = r.json()["id"]
    assert admin.put(f"/api/v1/attendance/policies/{policy_id}/assignments",
                     json={"rules": [{"criteria": {"everyone": True}, "priority": 10}]}).status_code == 200
    activated = admin.post(f"/api/v1/attendance/policies/{policy_id}/versions/1/activate",
                           json={"effective_from": "2026-01-01", "reason": "plant limits"})
    assert activated.status_code == 200, activated.text
    with tenant_db(cid) as db:
        after = roster_validation.limits_for(db, cid, [emp_id], DAY).per_employee[emp_id]
    assert (after.min_rest_hours.value, after.weekly_max_hours.value) == (12, 40)
    assert after.min_rest_hours.source_ref == "SCHEDULING_POLICY"
