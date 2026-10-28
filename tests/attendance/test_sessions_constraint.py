"""Sessions model and the two-step constraint change (AS-M2 WI-AS-2.9; spine §3.1, §15.2; AS 11 §11.4; A-08, A-18).

Shift date of interactive punches (BUG-AS-02), several sessions per date only with multiple_sessions_allowed
(AS-CAP-02), the overlap rule (409 SESSION_OVERLAP from the service and the trigger), the legacy overlap report
(AS-MIG-01), the resumable batched backfill (AS-MIG-02) and the downgrade refusal of 0049_as_m2_punches_sessions.
"""

import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.attendance_helpers import IST, act, employee_with_login, fake_clock, meta, owner_rows, set_policy  # noqa: F401
from tests.helpers import signup_company

DAY = "2026-10-20"
REVISION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0049_as_m2_punches_sessions.py"


def _revision():
    spec = importlib.util.spec_from_file_location("as_m2_punches_sessions", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def team(app):
    admin = signup_company(app, "Sessions Co")
    set_policy(admin, requireFaceForSelfCheckIn=False)
    hr = employee_with_login(app, admin, "Kavya", roles=("hr_admin",))
    emp = employee_with_login(app, admin, "Sameer")
    return {"admin": admin, "hr": hr, "emp": emp, "emp_id": emp.me()["employee"]["id"]}


def _night_shift(admin):
    r = admin.post("/api/v1/shifts", json={"code": "C", "name": "Night C", "kind": "FIXED", "version": {
        "effective_from": "2026-01-01", "reason": "Night crew", "start_time": "22:00", "end_time": "06:00"}})
    assert r.status_code == 201, r.text
    return r.json()


def _owner(sql, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        return conn.execute(text(sql), params)


def test_bug_as_02_night_punch_after_midnight_and_the_next_night(app, fake_clock):
    """BUG-AS-02: on shift C 22:00-06:00 an IN at Sat 00:10 belongs to Friday's instance (shift date Fri); the next
    night's IN at Sat 21:55 opens Saturday instead of 409 ALREADY_CHECKED_IN."""
    from tests.helpers import create_employee

    admin = signup_company(app, "Night Works")
    set_policy(admin, requireFaceForSelfCheckIn=False)
    shift = _night_shift(admin)
    imran = create_employee(admin, "Imran", "Shaikh", shift_id=shift["id"])
    from tests.helpers import invite_and_activate

    client = invite_and_activate(app, admin, imran, ["employee"])
    fake_clock.at_ist("2026-10-31", "00:10")
    first = act(client, "PUNCH_IN", meta(fake_clock))
    assert first.status_code == 200, first.text
    assert first.json()["date"] == "2026-10-30"
    fake_clock.at_ist("2026-10-31", "06:00")
    assert act(client, "PUNCH_OUT", meta(fake_clock)).status_code == 200
    fake_clock.at_ist("2026-10-31", "21:55")
    second = act(client, "PUNCH_IN", meta(fake_clock))
    assert second.status_code == 200, second.text
    assert second.json()["date"] == "2026-10-31"
    rows = owner_rows("SELECT date, work_timezone, shift_version_id FROM attendance WHERE employee_id = :e ORDER BY id",
                      e=imran["id"])
    assert [str(r["date"]) for r in rows] == ["2026-10-30", "2026-10-31"]
    assert rows[0]["work_timezone"] == "Asia/Kolkata" and rows[0]["shift_version_id"] is not None


def test_as_cap_02_second_session_only_with_multiple_sessions_allowed(team, fake_clock, monkeypatch):
    """AS-CAP-02 / spine §3.1 legacy guard: a v1 tenant refuses a second IN on the same shift date (409
    ALREADY_CHECKED_IN, nothing written); with multiple_sessions_allowed it opens session_seq 2."""
    emp = team["emp"]
    fake_clock.at_ist(DAY, "08:00")
    act(emp, "PUNCH_IN", meta(fake_clock))
    fake_clock.at_ist(DAY, "14:00", 2)
    act(emp, "PUNCH_OUT", meta(fake_clock))
    fake_clock.at_ist(DAY, "14:00", 20)
    refused = act(emp, "PUNCH_IN", meta(fake_clock))
    assert refused.status_code == 409 and refused.json()["code"] == "ALREADY_CHECKED_IN"

    from app.services import attendance_capture_policy as cp

    real = cp.effective
    monkeypatch.setattr(cp, "effective", lambda *a: replace(real(*a), multiple_sessions_allowed=True))
    import json

    r = emp.post("/api/v1/attendance/me/punch", data={"meta": json.dumps({**meta(fake_clock), "direction": "IN"})})
    assert r.status_code == 200, r.text
    rows = owner_rows("SELECT date, session_seq FROM attendance WHERE employee_id = :e ORDER BY id", e=team["emp_id"])
    assert [(str(r["date"]), r["session_seq"]) for r in rows] == [(DAY, 1), (DAY, 2)]


def test_session_overlap_from_the_service_and_from_the_trigger(team, fake_clock):
    """An HR entry overlapping another session -> 409 SESSION_OVERLAP (spine §3.1); a raw insert meets the trigger
    attendance_sessions_no_overlap (23P01) and the mapper answers SESSION_OVERLAP."""
    from sqlalchemy.exc import DBAPIError

    from app.core import db_errors

    hr = team["hr"]
    body = {"employee_id": team["emp_id"], "date": DAY, "status": "present", "check_in_local": "09:00",
            "check_out_local": "18:00"}
    assert hr.post("/api/v1/attendance/manual", json=body).status_code == 200
    overlapping = {**body, "date": "2026-10-19", "check_in_local": "22:00", "check_out_local": "10:00"}
    r = hr.post("/api/v1/attendance/manual", json=overlapping)
    assert r.status_code == 409 and r.json()["code"] == "SESSION_OVERLAP", r.text
    with pytest.raises(DBAPIError) as exc:
        _owner("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, is_manual, source, "
               "status, created_at, updated_at) VALUES (:c, :e, :i, :o, '2026-10-21', true, 'manual', 'present', now(), "
               "now())", c=team["admin"].company_id, e=team["emp_id"],
               i=IST.localize(datetime(2026, 10, 20, 17, 0)), o=IST.localize(datetime(2026, 10, 20, 19, 0)))
    assert db_errors.constraint_name(exc.value) == "attendance_sessions_no_overlap"
    assert db_errors.map_integrity(exc.value).code == "SESSION_OVERLAP"
    # a raw writer without the new columns still gets them (defaults trigger): capture_source, occupied_until
    _owner("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, is_manual, source, "
           "status, created_at, updated_at) VALUES (:c, :e, :i, :o, '2026-10-22', true, 'manual', 'present', now(), now())",
           c=team["admin"].company_id, e=team["emp_id"],
           i=IST.localize(datetime(2026, 10, 22, 9, 0)), o=IST.localize(datetime(2026, 10, 22, 17, 0)))
    row = owner_rows("SELECT capture_source, occupied_until, check_out_time, session_seq FROM attendance "
                     "WHERE employee_id = :e AND date = '2026-10-22'", e=team["emp_id"])[0]
    assert row["capture_source"] == "MANUAL" and row["occupied_until"] == row["check_out_time"]
    assert row["session_seq"] == 1


def test_same_date_duplicate_is_already_checked_in_not_overlap(team):
    """Two concurrent inserts of one date: the unique index answers (409 ALREADY_CHECKED_IN), not the overlap rule."""
    from sqlalchemy.exc import DBAPIError

    from app.core import db_errors

    params = dict(c=team["admin"].company_id, e=team["emp_id"], i=IST.localize(datetime(2026, 10, 20, 9, 0)))
    sql = ("INSERT INTO attendance (company_id, employee_id, check_in_time, date, is_manual, source, status, created_at, "
           "updated_at) VALUES (:c, :e, :i, '2026-10-20', false, 'self', 'present', now(), now())")
    _owner(sql, **params)
    with pytest.raises(DBAPIError) as exc:
        _owner(sql, **params)
    assert db_errors.map_integrity(exc.value).code == "ALREADY_CHECKED_IN"


def _plant_overlap(company_id: int, employee_id: int):
    """Two overlapping HR rows of 12 Oct written before the rule existed (06:00-14:00 and 13:00-15:00)."""
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        conn.execute(text("ALTER TABLE attendance DISABLE TRIGGER trg_attendance_sessions_no_overlap"))
        for start, end, day, seq in ((6, 14, "2026-10-12", 1), (13, 15, "2026-10-13", 1)):
            conn.execute(text(
                "INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, is_manual, source, "
                "status, session_seq, created_at, updated_at) VALUES (:c, :e, :i, :o, :d, true, 'manual', 'present', :s, "
                "now(), now())"), {"c": company_id, "e": employee_id, "d": day, "s": seq,
                                   "i": IST.localize(datetime(2026, 10, 12, start)),
                                   "o": IST.localize(datetime(2026, 10, 12, end))})
        conn.execute(text("ALTER TABLE attendance ENABLE TRIGGER trg_attendance_sessions_no_overlap"))


def test_as_mig_01_legacy_overlaps_are_reported_once_never_failed(team):
    """AS-MIG-01: one OPEN SESSION_OVERLAP_LEGACY item per overlapping pair (INFO), idempotent on re-run; HR sees it in
    the review queue and acknowledges it."""
    from app.database.db import system_engine

    _plant_overlap(team["admin"].company_id, team["emp_id"])
    revision = _revision()
    with system_engine.begin() as conn:
        assert revision.report_legacy_overlaps(conn) == 1
        assert revision.report_legacy_overlaps(conn) == 0
    items = owner_rows("SELECT kind, severity, status, dedup_key, session_id FROM attendance_review_items")
    assert len(items) == 1 and items[0]["kind"] == "SESSION_OVERLAP_LEGACY" and items[0]["severity"] == "INFO"
    queue = team["hr"].get("/api/v1/attendance/review", params={"kind": "SESSION_OVERLAP_LEGACY"})
    assert queue.status_code == 200 and queue.json()["total"] == 1, queue.text
    item = queue.json()["items"][0]
    assert item["allowed_actions"] == ["ACKNOWLEDGED"]
    r = team["hr"].post(f"/api/v1/attendance/review/{item['id']}/decide", json={"status": "APPROVED"})
    assert r.status_code == 422 and r.json()["code"] == "INVALID_DECISION"
    r = team["hr"].post(f"/api/v1/attendance/review/{item['id']}/decide", json={"status": "ACKNOWLEDGED"},
                        headers={"If-Match": str(item["row_version"])})
    assert r.status_code == 200 and r.json()["status"] == "ACKNOWLEDGED", r.text
    again = team["hr"].post(f"/api/v1/attendance/review/{item['id']}/decide", json={"status": "ACKNOWLEDGED"})
    assert again.status_code == 409 and again.json()["code"] == "INVALID_TRANSITION"
    own = team["emp"].get("/api/v1/attendance/review")
    assert own.status_code == 403  # an employee holds neither review code


def test_as_mig_02_backfill_resumes_after_a_killed_batch(team, fake_clock):
    """AS-MIG-02: batches are WHERE capture_source IS NULL AND id BETWEEN; a run killed after batch 1 leaves the rest
    NULL, a re-run finishes, and the result equals one uninterrupted run (self->WEB, manual->MANUAL; occupied_until
    from check_out_time, LEAST(closed_at, check_in + 16 h) for closed rows, NULL while live)."""
    from app.database.db import system_engine

    emp, hr = team["emp"], team["hr"]
    fake_clock.at_ist("2026-10-19", "09:00")
    act(emp, "PUNCH_IN", meta(fake_clock))
    fake_clock.at_ist("2026-10-19", "18:00")
    act(emp, "PUNCH_OUT", meta(fake_clock))
    hr.post("/api/v1/attendance/manual", json={"employee_id": team["emp_id"], "date": "2026-10-15", "status": "present",
                                               "check_in_local": "09:00"})  # past day without OUT -> closed
    fake_clock.at_ist(DAY, "09:00")
    act(emp, "PUNCH_IN", meta(fake_clock))  # live
    expected = owner_rows("SELECT id, capture_source, occupied_until FROM attendance ORDER BY id")
    assert [r["capture_source"] for r in expected] == ["WEB", "MANUAL", "WEB"]
    assert expected[2]["occupied_until"] is None and expected[1]["occupied_until"] is not None
    revision = _revision()
    with system_engine.connect() as conn:
        conn.execute(text("ALTER TABLE attendance ALTER COLUMN capture_source DROP NOT NULL"))
        conn.execute(text("ALTER TABLE attendance DISABLE TRIGGER trg_attendance_session_defaults"))
        conn.execute(text("ALTER TABLE attendance DISABLE TRIGGER trg_attendance_sessions_no_overlap"))
        conn.execute(text("UPDATE attendance SET capture_source = NULL, occupied_until = NULL"))
        conn.commit()
        try:
            low = conn.execute(text("SELECT min(id) FROM attendance")).scalar()
            assert revision.backfill(conn, batch=1, stop_after=1) == 1
            conn.commit()
            remaining = conn.execute(text("SELECT count(*) FROM attendance WHERE capture_source IS NULL")).scalar()
            assert remaining == 2
            assert conn.execute(text("SELECT capture_source FROM attendance WHERE id = :i"), {"i": low}).scalar() == "WEB"
            revision.backfill(conn, batch=1)
            conn.commit()
            assert conn.execute(text("SELECT count(*) FROM attendance WHERE capture_source IS NULL")).scalar() == 0
        finally:
            conn.execute(text("ALTER TABLE attendance ENABLE TRIGGER trg_attendance_session_defaults"))
            conn.execute(text("ALTER TABLE attendance ENABLE TRIGGER trg_attendance_sessions_no_overlap"))
            conn.execute(text("UPDATE attendance SET capture_source = 'WEB' WHERE capture_source IS NULL"))
            conn.execute(text("ALTER TABLE attendance ALTER COLUMN capture_source SET NOT NULL"))
            conn.commit()
    got = owner_rows("SELECT id, capture_source, occupied_until FROM attendance ORDER BY id")
    closed = got[1]
    check_in = owner_rows("SELECT check_in_time, closed_at FROM attendance WHERE id = :i", i=closed["id"])[0]
    assert closed["occupied_until"] == min(check_in["closed_at"], check_in["check_in_time"] + timedelta(hours=16))
    assert got == expected


def test_downgrade_is_refused_while_a_date_holds_two_sessions(team):
    """Spine §15.2: 0049's downgrade refuses with two non-voided sessions on one date, and with a voided row beside its
    replacement (the old unique key could not be restored)."""
    from app.database.db import system_engine

    revision = _revision()
    with system_engine.connect() as conn:
        assert revision.downgrade_blocker(conn) is None
    for seq, start, voided in ((1, 9, False), (2, 15, False)):
        _owner("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, is_manual, source, "
               "status, session_seq, created_at, updated_at) VALUES (:c, :e, :i, :o, '2026-10-20', false, 'self', "
               "'present', :s, now(), now())", c=team["admin"].company_id, e=team["emp_id"], s=seq,
               i=IST.localize(datetime(2026, 10, 20, start)), o=IST.localize(datetime(2026, 10, 20, start + 2)))
    with system_engine.connect() as conn:
        assert "more than one non-voided session" in revision.downgrade_blocker(conn)
    _owner("UPDATE attendance SET voided_at = now(), void_reason = 'DUPLICATE' WHERE session_seq = 2")
    with system_engine.connect() as conn:
        assert "voided session" in revision.downgrade_blocker(conn)


def test_readers_skip_voided_sessions(team, fake_clock):
    """WI-AS-2.5: a voided session is not a pay input (time_inputs._attendance_inputs), not worked time
    (overtime._worked) and not a live session; content() carries voided_at, session_seq, capture_source."""
    from datetime import date

    from app.database.db import SessionLocal, bind_tenant
    from app.models.attendance import Attendance
    from app.payroll.services import overtime, time_inputs
    from app.services import attendance_service

    fake_clock.at_ist(DAY, "09:00")
    session_id = act(team["emp"], "PUNCH_IN", meta(fake_clock)).json()["id"]
    fake_clock.at_ist(DAY, "18:00")
    act(team["emp"], "PUNCH_OUT", meta(fake_clock))
    company_id = team["admin"].company_id
    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        record = db.get(Attendance, session_id)
        content = attendance_service.content(record, [])
        assert (content["voided_at"], content["session_seq"], content["capture_source"]) == (None, 1, "WEB")
        day = date(2026, 10, 20)
        assert day in time_inputs._attendance_inputs(db, company_id, team["emp_id"], day, day, None, 0)
        assert day in overtime._worked(db, company_id, team["emp_id"], day, day)
        record.voided_at, record.void_reason = datetime.now(timezone.utc), "DUPLICATE"
        db.flush()
        assert time_inputs._attendance_inputs(db, company_id, team["emp_id"], day, day, None, 0) == {}
        assert overtime._worked(db, company_id, team["emp_id"], day, day) == {}
        db.rollback()
    finally:
        db.close()


@pytest.mark.skipif(__import__("os").environ.get("HRMS_MIGRATION_REHEARSAL") != "1",
                    reason="destructive rehearsal; set HRMS_MIGRATION_REHEARSAL=1 and run it alone")
def test_as_mig_01_rehearsal_with_a_planted_overlap(team):
    """AS-M2 exit 1: downgrade refused while a date holds two sessions; with a planted legacy overlap the revision goes
    down to 0048 and up again, reporting the pair as one SESSION_OVERLAP_LEGACY item instead of failing."""
    import os
    import subprocess
    import sys

    from app.cli import grant_app_role
    from app.database.db import system_engine
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    root = Path(__file__).resolve().parents[2]

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=os.environ.copy(),
                              capture_output=True, text=True)

    company_id, employee_id = team["admin"].company_id, team["emp_id"]
    for seq, start in ((1, 9), (2, 15)):
        _owner("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, is_manual, source, "
               "status, session_seq, created_at, updated_at) VALUES (:c, :e, :i, :o, '2026-10-20', false, 'self', "
               "'present', :s, now(), now())", c=company_id, e=employee_id, s=seq,
               i=IST.localize(datetime(2026, 10, 20, start)), o=IST.localize(datetime(2026, 10, 20, start + 2)))
    refused = alembic("downgrade", "0048_x_m1_permissions")
    assert refused.returncode != 0 and "more than one non-voided session" in refused.stdout + refused.stderr
    _owner("DELETE FROM attendance WHERE session_seq = 2")
    _plant_overlap(company_id, employee_id)
    system_engine.dispose()
    down = alembic("downgrade", "0048_x_m1_permissions")
    assert down.returncode == 0, down.stdout + down.stderr
    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stdout + up.stderr
    url = os.environ["MIGRATION_DATABASE_URL"]
    owner = create_engine(url, isolation_level="AUTOCOMMIT")
    with owner.connect() as conn:
        grant_app_role(conn, make_url(os.environ["DATABASE_URL"]).username)
    owner.dispose()
    items = owner_rows("SELECT kind FROM attendance_review_items")
    assert [i["kind"] for i in items] == ["SESSION_OVERLAP_LEGACY"]
    assert owner_rows("SELECT count(*) AS n FROM attendance WHERE capture_source IS NULL")[0]["n"] == 0
