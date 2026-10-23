"""Change detection: a source changed after a run locked its inputs makes the run stale (3.4 §16, 3.5 §4.1).

EC-ATT-05 (attendance corrected after lock), EC-CMP-13 (revision approved after lock), EC-CFG-14 (policy version
activated while a run has locked inputs), EC-STA-03 (pack changed between calculation and finalization), EC-RUN-09
(submit and finalize blocked while stale); the periodic job payroll.match_source_changes; tenant isolation.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from tests.payroll.test_compensation import assign_and_approve
from tests.payroll.test_runs_lifecycle import (  # noqa: F401 - runs_world is a fixture
    force_status,
    new_run,
    principal_of,
    runs_world,
    session_for,
)
from tests.payroll.test_snapshot_builder import emma, ivan, lock, objects
from tests.payroll.test_wage_inputs import activate_policy, component, set_setting, sql

LOCKED = "REVIEW_REQUIRED"


@pytest.fixture
def locked_world(runs_world):
    """A regular run whose inputs for Emma and Ivan are locked; the run then waits for review."""
    w = runs_world
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    w["run"] = new_run(w["acme"], w["group"], w["period"]["id"])
    w["start"], w["end"] = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    force_status(w["run"]["id"], "READY")
    lock(w["cid"], w["run"]["id"], [emma(w), ivan(w)])
    force_status(w["run"]["id"], LOCKED)
    return w


def stale_of(run_id):
    stale, reasons = sql("SELECT stale, stale_reasons FROM payroll_runs WHERE id = :r", r=run_id)[0]
    return stale, reasons


def match(company_id, **kwargs):
    from app.payroll.services import change_detection

    db = session_for(company_id)
    try:
        outcome = change_detection.match_source_changes(db, company_id, **kwargs)
        db.commit()
        return outcome
    finally:
        db.close()


def detect(company_id, run_id, **kwargs):
    from app.payroll.services import change_detection

    db = session_for(company_id)
    try:
        run, _ = objects(db, run_id)
        return change_detection.detect_changes(db, run, **kwargs)
    finally:
        db.close()


def manual_attendance(client, employee_id, day, status="present"):
    """Record (or, for a date that has a session, correct) attendance as an administrator."""
    r = client.post("/api/v1/attendance/manual", json={
        "employee_id": employee_id, "date": day.isoformat(),
        "check_in_time": datetime(day.year, day.month, day.day, 3, 30, tzinfo=timezone.utc).isoformat(),
        "check_out_time": datetime(day.year, day.month, day.day, 12, 30, tzinfo=timezone.utc).isoformat(),
        "status": status})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Pure comparison
# ---------------------------------------------------------------------------
def test_diff_sources_reports_added_updated_and_removed_sources():
    from app.payroll.services.change_detection import ADDED, REMOVED, UPDATED, SourceChange, diff_sources

    old = {"attendance:1": "a" * 64, "leave:2": "b" * 64, "holiday:3": "c" * 64}
    new = {"attendance:1": "f" * 64, "holiday:3": "c" * 64, "lop_input:9": "d" * 64}
    assert diff_sources(old, new) == [SourceChange("attendance:1", UPDATED), SourceChange("leave:2", REMOVED),
                                      SourceChange("lop_input:9", ADDED)]
    assert diff_sources(old, dict(old)) == []
    assert SourceChange("attendance:1", UPDATED).to_json() == {"source": "attendance:1", "type": "attendance",
                                                               "change": "UPDATED"}


# ---------------------------------------------------------------------------
# Every source change type marks the run stale
# ---------------------------------------------------------------------------
def test_unchanged_inputs_and_other_employees_changes_never_mark_the_run_stale(locked_world):
    w = locked_world
    assert detect(w["cid"], w["run"]["id"], full=True) == {}
    gus_day = w["start"] + timedelta(days=2)
    manual_attendance(w["acme"], w["gus"]["id"], gus_day)            # another pay group's employee
    assert match(w["cid"]) == {"checked": 1, "marked": []}
    assert stale_of(w["run"]["id"]) == (False, [])


def test_ec_att_05_attendance_corrected_after_lock_marks_the_run_stale(locked_world):
    from app.core.errors import AppError
    from app.payroll.services import runs

    w = locked_world
    day = w["start"] + timedelta(days=3)
    session_id = manual_attendance(w["acme"], emma(w), day)
    found = detect(w["cid"], w["run"]["id"])
    assert list(found) == [emma(w)]
    assert {"source": f"attendance:{session_id}", "type": "attendance", "change": "ADDED"} in found[emma(w)]
    outcome = match(w["cid"])
    assert outcome["marked"] == [{"run_id": w["run"]["id"], "employees": [emma(w)]}]
    stale, reasons = stale_of(w["run"]["id"])
    assert stale and reasons[-1]["source"] == "change_detection" and reasons[-1]["run_id"] == w["run"]["id"]
    assert any(c["source"] == f"attendance:{session_id}" and c["employee_id"] == emma(w) for c in reasons[-1]["changes"])
    assert match(w["cid"]) == {"checked": 0, "marked": []}           # already stale: nothing appended twice
    assert len(stale_of(w["run"]["id"])[1]) == 1
    force_status(w["run"]["id"], "APPROVED", approved_at=datetime.now(timezone.utc))
    db, principal = principal_of(w["acme"])                          # EC-RUN-09: finalize is blocked while stale
    try:
        run, _ = objects(db, w["run"]["id"])
        assert [b["code"] for b in runs.finalization_blockers(db, run)][0] == "RUN_STALE"
        with pytest.raises(AppError) as info:
            runs.transition(db, principal, run, "FINALIZE", STEP_UP_MFA=True, IDEMPOTENCY_KEY=True,
                            STATUTORY_PACK_VERIFIED=True)
        assert info.value.code == "RUN_STALE"
        db.rollback()
    finally:
        db.close()


def test_an_updated_session_is_reported_as_updated_and_relocking_clears_the_difference(locked_world):
    w = locked_world
    day = w["start"] + timedelta(days=4)
    sql("UPDATE payroll_runs SET status = 'READY' WHERE id = :r", r=w["run"]["id"])
    session_id = manual_attendance(w["acme"], ivan(w), day)
    lock(w["cid"], w["run"]["id"], [ivan(w)])                         # the new session is now frozen
    force_status(w["run"]["id"], LOCKED)
    assert detect(w["cid"], w["run"]["id"], full=True) == {}
    assert manual_attendance(w["acme"], ivan(w), day, status="half-day") == session_id   # a correction (EC-ATT-05)
    found = detect(w["cid"], w["run"]["id"])
    assert {"source": f"attendance:{session_id}", "type": "attendance", "change": "UPDATED"} in found[ivan(w)]


def test_ec_att_05_a_holiday_declared_after_lock_reaches_every_locked_employee(locked_world):
    w = locked_world
    day = w["start"] + timedelta(days=6)
    r = w["acme"].post("/api/v1/holidays", json={"name": "Founders Day", "date": day.isoformat()})
    assert r.status_code in (200, 201), r.text
    holiday_id = r.json()["id"]
    found = detect(w["cid"], w["run"]["id"])                         # an employee-less log row reaches everyone
    assert set(found) == {emma(w), ivan(w)}
    for items in found.values():
        assert {"source": f"holiday:{holiday_id}", "type": "holiday", "change": "ADDED"} in items
    outcome = match(w["cid"])
    assert outcome["marked"] == [{"run_id": w["run"]["id"], "employees": sorted([emma(w), ivan(w)])}]
    stale, reasons = stale_of(w["run"]["id"])
    assert stale and "holiday" in reasons[-1]["reason"] and reasons[-1]["employees"] == 2
    # The holiday itself and the day ledgers it changed; nothing else (compensation, policies) is reported.
    assert {c["type"] for c in reasons[-1]["changes"]} == {"holiday", "day_ledger"}
    assert {(c["employee_id"], c["source"]) for c in reasons[-1]["changes"] if c["type"] == "holiday"} == {
        (emma(w), f"holiday:{holiday_id}"), (ivan(w), f"holiday:{holiday_id}")}


def test_ec_cmp_13_revision_approved_after_lock_marks_the_run_stale(locked_world):
    w = locked_world
    middle = w["start"] + timedelta(days=15)
    assign_and_approve(w["acme"], w["approver"], ivan(w), w["version"], "1500000", effective_from=middle)
    stale, reasons = stale_of(w["run"]["id"])
    assert stale and reasons[-1]["source"] == "compensation" and reasons[-1]["employee_id"] == ivan(w)
    changes = detect(w["cid"], w["run"]["id"], full=True)
    assert list(changes) == [ivan(w)]
    assert {c["type"] for c in changes[ivan(w)]} >= {"compensation_assignment"}


def test_ec_cfg_14_policy_activated_while_inputs_are_locked_marks_the_run_stale(locked_world):
    w = locked_world
    activate_policy(w["acme"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT", "missing_punch_out": "EXCEPTION"})
    stale, reasons = stale_of(w["run"]["id"])
    assert stale and reasons[-1]["source"] == "policies"
    changes = detect(w["cid"], w["run"]["id"], full=True)
    assert set(changes) == {emma(w), ivan(w)}
    assert all(any(c["type"] == "policy_version" for c in items) for items in changes.values())


def test_wage_input_approved_after_lock_is_found_by_a_full_check(locked_world):
    w = locked_world
    set_setting(w["cid"], "payroll.wage_inputs.require_approval_above", {"amount": "99999.00", "currency": "INR"})
    component(w["cid"], "SPOT_BONUS")                                    # an INPUT component in force since 2020
    r = w["acme"].post("/api/v1/payroll/wage-inputs", json={
        "employee_id": emma(w), "component_code": "SPOT_BONUS", "period_start": w["period"]["start"],
        "period_end": w["period"]["end"], "amount": {"amount": "700.00", "currency": "INR"}})
    assert r.status_code == 201, r.text
    input_id = r.json()["items"][0]["id"]
    changes = detect(w["cid"], w["run"]["id"], full=True)
    assert {"source": f"wage_input:{input_id}", "type": "wage_input", "change": "ADDED"} in changes[emma(w)]
    assert not [c for c in changes.get(ivan(w), []) if c["type"] == "wage_input"]  # the catalogue grew for both


def test_ec_sta_03_a_changed_pack_marks_every_employee(locked_world, monkeypatch):
    from app.payroll.services import snapshot

    w = locked_world
    original = snapshot.pack_reference

    def changed(run, period, entity):
        found = original(run, period, entity) or {"code": "IN", "version": "IN-0", "country": "IN"}
        return {**found, "fingerprint": "f" * 64}

    monkeypatch.setattr(snapshot, "pack_reference", changed)
    changes = detect(w["cid"], w["run"]["id"])
    assert set(changes) == {emma(w), ivan(w)}
    assert all(any(c["type"] == "pack" for c in items) for items in changes.values())
    assert match(w["cid"])["marked"][0]["run_id"] == w["run"]["id"]


# ---------------------------------------------------------------------------
# The periodic job, unlocked runs, isolation
# ---------------------------------------------------------------------------
def test_the_match_job_runs_marks_stale_and_reschedules_itself(locked_world):
    from app.core import clock
    from app.payroll.services import change_detection

    w = locked_world
    manual_attendance(w["acme"], emma(w), w["start"] + timedelta(days=5))
    db = session_for(w["cid"])
    try:
        job = change_detection.schedule_matching(db, w["cid"], after=clock.utcnow() - timedelta(hours=1))
        job_id = job.id
        db.commit()                                                  # JOBS_MODE=inline: it runs after commit
    finally:
        db.close()
    status, result = sql("SELECT status, result FROM jobs WHERE id = :i", i=job_id)[0]
    assert status == "SUCCEEDED", result
    assert result["marked_run_ids"] == [w["run"]["id"]] and stale_of(w["run"]["id"])[0]
    queued = sql("SELECT count(*) FROM jobs WHERE company_id = :c AND type = :t AND status = 'QUEUED'",
                 c=w["cid"], t=change_detection.MATCH_JOB)[0][0]
    assert queued >= 1                                               # the next pass is queued
    again = sql("SELECT count(*) FROM jobs WHERE company_id = :c AND type = :t", c=w["cid"],
                t=change_detection.MATCH_JOB)[0][0]
    db = session_for(w["cid"])
    try:
        change_detection.schedule_matching(db, w["cid"])             # idempotent per hour
        db.commit()
    finally:
        db.close()
    assert sql("SELECT count(*) FROM jobs WHERE company_id = :c AND type = :t", c=w["cid"],
               t=change_detection.MATCH_JOB)[0][0] == again


def test_runs_without_locked_inputs_are_not_checked(runs_world):
    w = runs_world
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    manual_attendance(w["acme"], emma(w), date.fromisoformat(w["period"]["start"]) + timedelta(days=1))
    assert match(w["cid"]) == {"checked": 0, "marked": []}
    assert stale_of(run["id"]) == (False, [])
    force_status(run["id"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    assert match(w["cid"], full=True) == {"checked": 0, "marked": []}


def test_ec_sec_change_detection_is_tenant_isolated(locked_world):
    from app.payroll.services import change_detection

    w = locked_world
    manual_attendance(w["acme"], emma(w), w["start"] + timedelta(days=6))
    for other in (w["beta"], w["cobalt"]):
        db = session_for(other["cid"])
        try:
            assert change_detection.match_source_changes(db, w["cid"], full=True) == {"checked": 0, "marked": []}
            assert change_detection.match_source_changes(db, other["cid"], full=True) == {"checked": 0, "marked": []}
            db.rollback()
        finally:
            db.close()
    assert stale_of(w["run"]["id"]) == (False, [])
    assert match(w["cid"])["marked"][0]["run_id"] == w["run"]["id"]
