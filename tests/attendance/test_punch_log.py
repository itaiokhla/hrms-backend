"""The raw punch log (AS-M2 WI-AS-2.9; 04-attendance/01 §2-§4; spine §3.2; D-AS-04).

Every capture source writes one attendance_punches row, also for v1 tenants (shadow input, S-3); the guard trigger
and the column grants keep a punch immutable; the interactive endpoint POST /attendance/me/punch (capture policy,
consent gate, rate limit) and the auto-close job (no invented punch, 01 §2.3).
"""

import json

import pytest
from sqlalchemy import text

from tests.attendance_helpers import act, employee_with_login, fake_clock, key, meta, owner_rows, set_policy  # noqa: F401
from tests.helpers import create_employee, signup_company

DAY = "2026-10-20"


@pytest.fixture
def team(app):
    admin = signup_company(app, "Punch Log Co")  # Asia/Kolkata
    set_policy(admin, requireFaceForSelfCheckIn=False)
    hr = employee_with_login(app, admin, "Kavya", roles=("hr_admin",))
    emp = employee_with_login(app, admin, "Priya")
    return {"admin": admin, "hr": hr, "emp": emp, "emp_id": emp.me()["employee"]["id"]}


def punch(client, clock, direction, **extra):
    body = {**meta(clock), "direction": direction, **extra}
    return client.post("/api/v1/attendance/me/punch", data={"meta": json.dumps(body)})


def punches(employee_id):
    return owner_rows("SELECT * FROM attendance_punches WHERE employee_id = :e ORDER BY id", e=employee_id)


def test_as_cap_every_legacy_transition_writes_one_used_punch(team, fake_clock):
    """S-3: the legacy WEB endpoints (v1 tenant) log IN, BREAK_START, BREAK_END, OUT, each linked to the session."""
    emp = team["emp"]
    fake_clock.at_ist(DAY, "09:00")
    session_id = act(emp, "PUNCH_IN", meta(fake_clock)).json()["id"]
    fake_clock.at_ist(DAY, "13:00")
    act(emp, "LUNCH_IN", meta(fake_clock))
    fake_clock.at_ist(DAY, "13:30")
    act(emp, "LUNCH_OUT", meta(fake_clock))
    fake_clock.at_ist(DAY, "18:00")
    assert act(emp, "PUNCH_OUT", meta(fake_clock)).status_code == 200
    rows = punches(team["emp_id"])
    assert [(r["source"], r["direction"]) for r in rows] == [("WEB", "IN"), ("WEB", "BREAK_START"),
                                                             ("WEB", "BREAK_END"), ("WEB", "OUT")]
    assert {(r["disposition"], r["mapping_status"], r["session_id"], str(r["shift_date"])) for r in rows} == {
        ("USED", "MAPPED", session_id, DAY)}
    assert all(r["verify_mode"] == "NONE" and r["geofence_result"] == "NOT_CHECKED" for r in rows)
    session = owner_rows("SELECT * FROM attendance WHERE id = :i", i=session_id)[0]
    assert session["capture_source"] == "WEB" and session["session_seq"] == 1
    assert (session["first_punch_id"], session["last_punch_id"]) == (rows[0]["id"], rows[-1]["id"])
    assert session["occupied_until"] == session["check_out_time"]
    events = owner_rows("SELECT event_type, punch_id FROM attendance_events WHERE attendance_id = :i ORDER BY id",
                        i=session_id)
    assert [e["punch_id"] for e in events] == [r["id"] for r in rows]
    brk = owner_rows("SELECT start_punch_id, end_punch_id FROM attendance_breaks WHERE attendance_id = :i", i=session_id)
    assert (brk[0]["start_punch_id"], brk[0]["end_punch_id"]) == (rows[1]["id"], rows[2]["id"])


def test_as_cap_01_retry_with_the_same_key_writes_nothing_new(team, fake_clock):
    """AS-CAP-01 (a): the same Idempotency-Key replays the first result (replayed: true) with one punch row;
    the same key with another direction -> 409 IDEMPOTENCY_CONFLICT; (b) OUT from a stale tab -> 400 NOT_CHECKED_IN."""
    emp = team["emp"]
    fake_clock.at_ist("2026-11-03", "10:12", 40)
    body = {**meta(fake_clock), "direction": "IN"}
    first = emp.post("/api/v1/attendance/me/punch", data={"meta": json.dumps(body)})
    assert first.status_code == 200, first.text
    fake_clock.at_ist("2026-11-03", "10:12", 41)
    again = emp.post("/api/v1/attendance/me/punch", data={"meta": json.dumps(body)})
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["punch_id"] == first.json()["punch_id"]
    assert len(punches(team["emp_id"])) == 1
    other = emp.post("/api/v1/attendance/me/punch", data={"meta": json.dumps({**body, "direction": "OUT"})})
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_CONFLICT"
    fake_clock.at_ist("2026-11-03", "19:20", 5)
    assert punch(emp, fake_clock, "OUT").status_code == 200
    stale = punch(emp, fake_clock, "OUT", attendance_id=first.json()["id"])
    assert stale.status_code == 400 and stale.json()["code"] == "NOT_CHECKED_IN"


def test_me_punch_header_key_and_response_shape(team, fake_clock):
    fake_clock.at_ist(DAY, "09:00")
    body = {"direction": "IN", "client_sent_at": fake_clock.now.isoformat(), "location_consent": True}
    r = team["emp"].post("/api/v1/attendance/me/punch", data={"meta": json.dumps(body)},
                         headers={"Idempotency-Key": "web-intent-0001"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["direction"] == "IN" and data["capture_source"] == "WEB" and data["punch_id"]
    assert data["attendance_state"] == "WORKING"
    assert punches(team["emp_id"])[0]["idempotency_key"] == "web-intent-0001"


def test_as_cap_source_not_allowed_is_refused_before_any_write(team, fake_clock):
    """S-1: LEGACY_V1 allowed_sources are {WEB, KIOSK}; a MOBILE punch -> 422 FEATURE_DISABLED, nothing written."""
    fake_clock.at_ist(DAY, "09:00")
    r = punch(team["emp"], fake_clock, "IN", capture_source="MOBILE")
    assert r.status_code == 422 and r.json()["code"] == "FEATURE_DISABLED"
    assert punches(team["emp_id"]) == []
    assert owner_rows("SELECT count(*) AS n FROM attendance WHERE employee_id = :e", e=team["emp_id"])[0]["n"] == 0


def test_as_cap_mobile_punch_with_a_policy_that_allows_it(team, fake_clock, monkeypatch):
    from dataclasses import replace

    from app.services import attendance_capture_policy as cp

    real = cp.effective
    monkeypatch.setattr(cp, "effective", lambda *a: replace(real(*a), allowed_sources=frozenset({"WEB", "MOBILE"})))
    fake_clock.at_ist(DAY, "09:00")
    r = punch(team["emp"], fake_clock, "IN", capture_source="MOBILE", work_location_choice="REMOTE")
    assert r.status_code == 200, r.text
    session = owner_rows("SELECT capture_source, source, work_mode FROM attendance WHERE id = :i", i=r.json()["id"])[0]
    assert session == {"capture_source": "MOBILE", "source": "self", "work_mode": "REMOTE"}
    assert punches(team["emp_id"])[0]["source"] == "MOBILE"


def test_as_cap_06_no_face_consent_skips_the_face_step(team, fake_clock, monkeypatch):
    """AS-CAP-06 / F-2: face_on_punch on, no face consent -> accepted without face, verify_mode NONE, no flag, no review
    item, never CONSENT_REQUIRED."""
    from app.services import attendance_consents

    set_policy(team["admin"], requireFaceForSelfCheckIn=True)
    monkeypatch.setattr(attendance_consents, "face_allowed", lambda *a, **k: False)
    fake_clock.at_ist(DAY, "09:00")
    r = punch(team["emp"], fake_clock, "IN")
    assert r.status_code == 200, r.text
    row = punches(team["emp_id"])[0]
    assert row["verify_mode"] == "NONE" and row["evidence_flags"] == [] and row["face_match_score"] is None
    assert owner_rows("SELECT count(*) AS n FROM attendance_review_items")[0]["n"] == 0
    # the legacy endpoint keeps today's rule: a face capture is required
    fake_clock.at_ist(DAY, "18:00")
    assert act(team["emp"], "PUNCH_OUT", meta(fake_clock)).json()["code"] == "FACE_REQUIRED"


def test_as_cap_consent_required_only_for_location_at_punch(team, fake_clock):
    """Step 4: location_mode != OFF without the location notice -> 422 CONSENT_REQUIRED (purpose LOCATION_AT_PUNCH) on
    the v2 endpoint; the legacy endpoint keeps 400 LOCATION_NOTICE_REQUIRED."""
    set_policy(team["admin"], locationPunchPolicy="OPTIONAL")
    fake_clock.at_ist(DAY, "09:00")
    r = punch(team["emp"], fake_clock, "IN", location_consent=False)
    assert r.status_code == 422 and r.json()["code"] == "CONSENT_REQUIRED"
    assert r.json()["details"]["purpose"] == "LOCATION_AT_PUNCH"
    legacy = act(team["emp"], "PUNCH_IN", {**meta(fake_clock), "location_consent": False})
    assert legacy.status_code == 400 and legacy.json()["code"] == "LOCATION_NOTICE_REQUIRED"
    assert punch(team["emp"], fake_clock, "IN").status_code == 200  # acknowledged inline


def test_bug_as_62_punch_endpoint_is_limited_to_20_per_minute_per_user(team, fake_clock):
    """Spine §8.1 / SR-AS-11: the 21st punch call of a user within a minute -> 429 RATE_LIMITED with Retry-After."""
    fake_clock.at_ist(DAY, "08:00")
    codes = [punch(team["emp"], fake_clock, "OUT").status_code for _ in range(20)]
    assert set(codes) == {400}  # NOT_CHECKED_IN, but counted
    r = punch(team["emp"], fake_clock, "OUT")
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED" and "retry-after" in r.headers


def test_as_cap_manual_entry_writes_manual_punches_once(team, fake_clock):
    """S-3: an HR entry (v1 MANUAL) logs its IN and OUT with the actor and the internal reason; the same times again
    collapse onto the same rows (P-5a)."""
    body = {"employee_id": team["emp_id"], "date": DAY, "status": "present", "check_in_local": "09:00",
            "check_out_local": "18:00", "notes": "Forgot to punch"}
    assert team["hr"].post("/api/v1/attendance/manual", json=body).status_code == 200
    assert team["hr"].post("/api/v1/attendance/manual", json=body).status_code == 200
    rows = punches(team["emp_id"])
    assert [(r["source"], r["direction"]) for r in rows] == [("MANUAL", "IN"), ("MANUAL", "OUT")]
    assert rows[0]["actor_user_id"] is not None and rows[0]["reason"] == "Forgot to punch"
    session = owner_rows("SELECT capture_source, occupied_until, check_out_time FROM attendance WHERE employee_id = :e",
                         e=team["emp_id"])[0]
    assert session["capture_source"] == "MANUAL" and session["occupied_until"] == session["check_out_time"]


def test_punch_guard_allows_only_processing_columns(team, fake_clock):
    """Guard attendance_punches_guard + column grants: the app role may change disposition, never the punch itself;
    nobody deletes (23514 constraint attendance_punches_guard -> 409 INVALID_TRANSITION)."""
    from sqlalchemy.exc import DBAPIError

    from app.core import db_errors
    from app.database.db import SessionLocal, bind_tenant, system_engine

    fake_clock.at_ist(DAY, "09:00")
    act(team["emp"], "PUNCH_IN", meta(fake_clock))
    row = punches(team["emp_id"])[0]
    company_id = team["admin"].company_id
    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        db.execute(text("UPDATE attendance_punches SET disposition = 'IGNORED_BY_ADMIN' WHERE id = :i"), {"i": row["id"]})
        db.rollback()
        bind_tenant(db, company_id)
        with pytest.raises(DBAPIError) as edit:
            db.execute(text("UPDATE attendance_punches SET occurred_at = occurred_at - interval '1 hour', "
                            "disposition = 'USED' WHERE id = :i"), {"i": row["id"]})
        assert "permission denied" in str(edit.value)  # occurred_at is not granted to the app role
        db.rollback()
        bind_tenant(db, company_id)
        with pytest.raises(DBAPIError) as delete:
            db.execute(text("DELETE FROM attendance_punches WHERE id = :i"), {"i": row["id"]})
        assert "permission denied" in str(delete.value)
        db.rollback()
    finally:
        db.close()
    with system_engine.connect() as conn:  # even the owner meets the trigger
        for statement in ("UPDATE attendance_punches SET direction = 'OUT' WHERE id = :i",
                          "DELETE FROM attendance_punches WHERE id = :i"):
            with pytest.raises(DBAPIError) as exc:
                conn.execute(text(statement), {"i": row["id"]})
            assert db_errors.constraint_name(exc.value) == "attendance_punches_guard"
            assert db_errors.map_integrity(exc.value).code == "INVALID_TRANSITION"
            conn.rollback()


def test_as_cap_auto_close_runs_in_the_job_and_invents_no_punch(team, fake_clock):
    """01 §2.3: attendance.day_close closes a live session past maxShiftHours: closed_reason MISSING_PUNCH_OUT,
    occupied_until = LEAST(closed_at, check_in + 16 h), SESSION_CLOSED event, no attendance_punches row."""
    from datetime import timedelta

    from app.database.db import SessionLocal, bind_tenant
    from app.services import attendance_jobs

    fake_clock.at_ist(DAY, "09:00")
    session_id = act(team["emp"], "PUNCH_IN", meta(fake_clock)).json()["id"]
    check_in = fake_clock.now
    fake_clock.advance(hours=17)
    db = SessionLocal()
    try:
        bind_tenant(db, team["admin"].company_id)
        assert attendance_jobs.close_overdue_sessions(db, team["admin"].company_id) == 1
        db.commit()
    finally:
        db.close()
    session = owner_rows("SELECT * FROM attendance WHERE id = :i", i=session_id)[0]
    assert session["closed_reason"] == "MISSING_PUNCH_OUT" and session["check_out_time"] is None
    assert session["occupied_until"] == check_in + timedelta(hours=16)
    assert [r["direction"] for r in punches(team["emp_id"])] == ["IN"]
    # the next day's IN is free (no stale live session, no overlap with the occupied interval)
    fake_clock.at_ist("2026-10-21", "09:00")
    assert act(team["emp"], "PUNCH_IN", meta(fake_clock)).status_code == 200


def test_as_cap_auto_close_handler_is_registered():
    from app.payroll.services import jobs

    assert jobs.find_handler("attendance.day_close") is not None
    assert "app.services.attendance_jobs" in jobs.HANDLER_MODULES
    from pathlib import Path

    revision = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0049_as_m2_punches_sessions.py"
    assert "VALUES ('attendance.day_close', 'HOURLY', 12, NULL, true, now())" in revision.read_text(encoding="utf-8")


def test_legacy_kiosk_scan_writes_a_kiosk_punch(team, fake_clock, monkeypatch):
    """The operator kiosk (e2e/06 "Scan once") keeps toggling and now logs KIOSK punches too (shadow input)."""
    from app.modules.face_recognition import engine as face_engine

    emp_id = team["emp_id"]
    monkeypatch.setattr(face_engine, "available", lambda: True)
    monkeypatch.setattr(face_engine, "encode", lambda image: "probe")
    monkeypatch.setattr(face_engine, "best_match", lambda probe, candidates, threshold: (emp_id, 0.2))
    from app.database.db import system_engine

    # A pre-AS-M2 enrolment: legacy_enrolled within its grace (07 §5.5; the operator kiosk matches only employees
    # whose face step is allowed, B-7).
    with system_engine.begin() as conn:
        conn.execute(text("INSERT INTO face_encodings (company_id, employee_id, encoding, is_primary, legacy_enrolled, "
                          "legacy_grace_until, created_at, updated_at) "
                          "VALUES (:c, :e, 'x', true, true, DATE '2026-12-31', now(), now())"),
                     {"c": team["admin"].company_id, "e": emp_id})
    fake_clock.at_ist(DAY, "09:00")
    r = team["admin"].post("/api/v1/attendance/kiosk/scan", files={"file": ("f.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200 and r.json()["action"] == "check_in", r.text
    row = punches(emp_id)[0]
    assert (row["source"], row["direction"], row["verify_mode"]) == ("KIOSK", "IN", "FACE")


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
    return buffer.getvalue()
