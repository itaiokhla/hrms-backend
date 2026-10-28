"""Roster periods, drafts and publication (AS-M5 WI-AS-5.3, 5.4, 5.6; 03-shifts/02 §4-§7).

Publishing an illegal roster is blocked: 11 h minimum rest -> 422 INSUFFICIENT_REST_HOURS (issue rule key
MIN_REST_VIOLATION), 48 h a week -> 422 ROSTER_RULE_VIOLATION (WEEKLY_HOURS_EXCEEDED). A legal roster publishes
version-1 entries with change rows and a snapshot hash (GV-AS-ROS-08: same scope and entries = same hash), a repeated
publish is a no-op, a republished change bumps the version, ``shift_resolution`` step 1 and the LH day facts read the
entries (a rostered OFF is a weekly off), My schedule lists them, another tenant gets 404.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytz
from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from tests.helpers import create_employee, signup_company

IST = pytz.timezone("Asia/Kolkata")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


def _monday_after(days: int = 14) -> date:
    day = _today() + timedelta(days=days)
    return day - timedelta(days=day.weekday())


@pytest.fixture
def plant(app):
    admin = signup_company(app, "Roster Works")
    branch = admin.get("/api/v1/branches").json()[0]
    shifts = {}
    for code, start, end in (("EARLY", "06:00", "14:00"), ("LATE", "14:00", "22:00"), ("NIGHT", "22:00", "06:00"),
                             ("LONG", "08:00", "18:00")):
        r = admin.post("/api/v1/shifts", json={"code": code, "name": code.title(), "kind": "FIXED", "version": {
            "effective_from": "2024-01-01", "reason": "test", "start_time": start, "end_time": end}})
        assert r.status_code == 201, r.text
        shifts[code] = r.json()["id"]
    ravi = create_employee(admin, "Ravi", branch_id=branch["id"])
    sita = create_employee(admin, "Sita", branch_id=branch["id"])
    start = _monday_after()
    r = admin.post("/api/v1/rosters", json={"code": "RP-TEST-1", "name": "Plant two weeks", "branch_id": branch["id"],
                                            "start_date": start.isoformat(),
                                            "end_date": (start + timedelta(days=13)).isoformat()})
    assert r.status_code == 201, r.text
    return {"admin": admin, "branch": branch, "shifts": shifts, "ravi": ravi, "sita": sita, "start": start,
            "period": r.json()}


def _cell(employee, day: date, shift_id=None, kind="WORK", **extra):
    return {"employee_id": employee["id"], "shift_date": day.isoformat(), "kind": kind,
            **({"shift_id": shift_id} if shift_id else {}), **extra}


def _drafts(plant, cells, status=200):
    period = plant["admin"].get(f"/api/v1/rosters/{plant['period']['id']}").json()["period"]
    r = plant["admin"].put(f"/api/v1/rosters/{plant['period']['id']}/drafts",
                           json={"cells": cells, "row_version": period["row_version"]})
    assert r.status_code == status, r.text
    return r.json()


def _publish(plant, status=200, **body):
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/publish", json=body)
    assert r.status_code == status, r.text
    return r.json()


def test_period_rules_too_long_overlap_and_cross_tenant(app, plant):
    admin, start = plant["admin"], plant["start"]
    r = admin.post("/api/v1/rosters", json={"code": "RP-LONG", "name": "Too long", "branch_id": plant["branch"]["id"],
                                            "start_date": start.isoformat(),
                                            "end_date": (start + timedelta(days=70)).isoformat()})
    assert r.status_code == 422 and r.json()["code"] == "ROSTER_PERIOD_TOO_LONG"
    r = admin.post("/api/v1/rosters", json={"code": "RP-OVER", "name": "Overlap", "branch_id": plant["branch"]["id"],
                                            "start_date": (start + timedelta(days=3)).isoformat(),
                                            "end_date": (start + timedelta(days=9)).isoformat()})
    assert r.status_code == 409 and r.json()["code"] == "ROSTER_PERIOD_OVERLAP"
    other = signup_company(app, "Other Roster Co")
    assert other.get(f"/api/v1/rosters/{plant['period']['id']}").status_code == 404
    assert other.post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={}).status_code == 404


def test_publishing_too_little_rest_is_blocked_with_insufficient_rest_hours(plant):
    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    # LATE ends 22:00 Monday, EARLY starts 06:00 Tuesday: 8 h of rest < 11 h.
    _drafts(plant, [_cell(ravi, start, s["LATE"]), _cell(ravi, start + timedelta(days=1), s["EARLY"])])
    preview = _publish(plant, dry_run=True)
    assert any(r["rule_key"] == "MIN_REST_VIOLATION" and r["severity"] == "BLOCK" for r in preview["blocking"])
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={})
    assert r.status_code == 422 and r.json()["code"] == "INSUFFICIENT_REST_HOURS"
    assert r.json()["issues"][0]["rule_key"] == "MIN_REST_VIOLATION"
    with system_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM roster_entries")).scalar() == 0  # nothing was written


def test_more_than_48_hours_a_week_is_blocked(plant):
    s, start, sita = plant["shifts"], plant["start"], plant["sita"]
    # Six 10-hour LONG days Monday-Saturday = 60 h > 48 h.
    _drafts(plant, [_cell(sita, start + timedelta(days=i), s["LONG"]) for i in range(6)])
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={})
    assert r.status_code == 422 and r.json()["code"] == "ROSTER_RULE_VIOLATION"
    assert any(i["rule_key"] == "WEEKLY_HOURS_EXCEEDED" for i in r.json()["issues"])


def _legal_week(plant):
    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    cells = [_cell(ravi, start + timedelta(days=i), s["EARLY"]) for i in range(5)]
    cells += [_cell(ravi, start + timedelta(days=5), kind="OFF"), _cell(ravi, start + timedelta(days=6), kind="OFF")]
    return cells


def _ack_all(plant) -> list:
    preview = _publish(plant, dry_run=True)
    assert preview["blocking"] == [], preview["blocking"]
    return sorted({r["rule_key"] for r in preview["results"] if r["severity"] == "WARN"})


def test_a_legal_roster_publishes_versioned_entries_and_is_idempotent(plant):
    _drafts(plant, _legal_week(plant))
    preview = _publish(plant, dry_run=True)
    result = _publish(plant, acknowledged_warnings=_ack_all(plant))
    assert result["entries_created"] == 7 and result["snapshot_hash"] == preview["snapshot_hash"]  # GV-AS-ROS-08
    again = _publish(plant)
    assert again["nothing_to_publish"] is True and again["publication_id"] == result["publication_id"]
    with system_engine.connect() as conn:
        rows = conn.execute(text("SELECT change_kind, notice_hours FROM roster_entry_changes")).all()
        assert len(rows) == 7 and {k for k, _ in rows} == {"CREATED"}
        assert all(n is not None and n > 0 for _, n in rows)
        assert conn.execute(text("SELECT count(*) FROM source_change_log WHERE record_type = 'roster_entry'")).scalar() >= 1
        period = conn.execute(text("SELECT status FROM roster_periods WHERE id = :p"),
                              {"p": plant["period"]["id"]}).scalar()
    assert period == "PUBLISHED"


def test_a_republished_change_bumps_the_version_and_a_withdrawal_keeps_history(plant):
    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    _drafts(plant, _legal_week(plant))
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    _drafts(plant, [_cell(ravi, start, s["LONG"]), {"employee_id": ravi["id"], "shift_date":
                                                    (start + timedelta(days=4)).isoformat(), "action": "DELETE"}])
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    with system_engine.connect() as conn:
        monday = conn.execute(text("SELECT version, shift_id FROM roster_entries WHERE employee_id = :e AND shift_date = :d "
                                   "AND withdrawn_at IS NULL"), {"e": ravi["id"], "d": start}).one()
        friday = conn.execute(text("SELECT withdrawn_at FROM roster_entries WHERE employee_id = :e AND shift_date = :d"),
                              {"e": ravi["id"], "d": start + timedelta(days=4)}).scalar()
        kinds = sorted(k for (k,) in conn.execute(text(
            "SELECT change_kind FROM roster_entry_changes WHERE employee_id = :e"), {"e": ravi["id"]}))
        with pytest.raises(Exception):
            with system_engine.begin() as write:
                write.execute(text("UPDATE roster_entry_changes SET notice_hours = 0"))  # append-only
    assert tuple(monday) == (2, s["LONG"]) and friday is not None
    assert kinds.count("UPDATED") == 1 and kinds.count("WITHDRAWN") == 1 and kinds.count("CREATED") == 7


def test_the_resolver_day_facts_and_my_schedule_read_published_entries(app, plant):
    from app.payroll.services import time_inputs
    from app.services import shift_resolution

    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    _drafts(plant, _legal_week(plant))
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    cid = plant["admin"].company_id
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        monday = shift_resolution.shift_for(db, cid, ravi["id"], start)
        saturday = shift_resolution.shift_for(db, cid, ravi["id"], start + timedelta(days=5))
        facts = {f.date: f for f in time_inputs.day_facts(db, cid, ravi["id"], start, start + timedelta(days=6))}
    finally:
        db.close()
    assert (monday.source, monday.shift_id) == ("ROSTER", s["EARLY"]) and monday.roster_entry_id
    assert saturday is None  # a rostered OFF: no shift
    assert facts[start + timedelta(days=5)].weekly_off and not facts[start].weekly_off
    explain = plant["admin"].get("/api/v1/shifts/resolve", params={
        "employee_id": ravi["id"], "from": start.isoformat(), "to": start.isoformat()}).json()["days"][0]
    assert explain["source"] == "ROSTER"


def test_drafts_are_invisible_until_published(plant):
    from app.services import shift_resolution

    s, start, sita = plant["shifts"], plant["start"], plant["sita"]
    _drafts(plant, [_cell(sita, start, s["NIGHT"])])
    db = SessionLocal()
    try:
        bind_tenant(db, plant["admin"].company_id)
        assert shift_resolution.shift_for(db, plant["admin"].company_id, sita["id"], start) is None or \
            shift_resolution.shift_for(db, plant["admin"].company_id, sita["id"], start).source != "ROSTER"
    finally:
        db.close()


def test_archived_shift_is_refused_at_draft_save(plant):
    admin, s = plant["admin"], plant["shifts"]
    spare = admin.post("/api/v1/shifts", json={"code": "SPARE", "name": "Spare", "kind": "FIXED",
                                               "version": {"start_time": "09:00", "end_time": "17:00"}}).json()
    assert admin.post(f"/api/v1/shifts/{spare['id']}/archive", json={}).status_code == 200
    r = admin.put(f"/api/v1/rosters/{plant['period']['id']}/drafts", json={"cells": [
        _cell(plant["ravi"], plant["start"], spare["id"])]})
    assert r.status_code == 422 and r.json()["code"] == "SHIFT_ARCHIVED"
    assert s["EARLY"]


def test_gv_as_rot_01_a_template_rotation_generates_the_crew_drafts(plant):
    """PUNE-6ON2OFF from SIX_ON_TWO_OFF_SLOW (M→EARLY, A→LATE, N→NIGHT), crews B (offset 6) from the period start:
    index 8 on the third day is token A (cycle MMMMMMOOAAAAAAOO…) → LATE; index 6-7 are OFF."""
    admin, s, start = plant["admin"], plant["shifts"], plant["start"]
    r = admin.post("/api/v1/rotations", json={
        "code": "PUNE-6ON2OFF", "name": "Pune 6 on 2 off", "template_key": "SIX_ON_TWO_OFF_SLOW",
        "version": {"token_map": {"M": {"shift_id": s["EARLY"]}, "A": {"shift_id": s["LATE"]},
                                  "N": {"shift_id": s["NIGHT"]}}}})
    assert r.status_code == 201, r.text
    pattern = r.json()
    assert admin.post(f"/api/v1/rotations/{pattern['id']}/versions/1/activate").status_code == 200
    version_id = next(p for p in admin.get("/api/v1/rotations").json() if p["id"] == pattern["id"])["current_version_id"]
    r = admin.post("/api/v1/rotations/assignments", json={
        "pattern_version_id": version_id, "employee_ids": [plant["ravi"]["id"]], "crew_code": "B", "offset_days": 6,
        "anchor_date": start.isoformat(), "effective_from": start.isoformat()})
    assert r.status_code == 201, r.text
    again = admin.post("/api/v1/rotations/assignments", json={
        "pattern_version_id": version_id, "employee_ids": [plant["ravi"]["id"]], "crew_code": "B", "offset_days": 6,
        "anchor_date": start.isoformat(), "effective_from": start.isoformat()})
    assert again.status_code == 409 and again.json()["code"] == "ROTATION_OVERLAP"
    generated = admin.post(f"/api/v1/rosters/{plant['period']['id']}/generate", json={})
    assert generated.status_code == 200 and generated.json()["generated"] == 14
    drafts = {d["shift_date"]: d for d in admin.get(f"/api/v1/rosters/{plant['period']['id']}").json()["drafts"]
              if d["employee_id"] == plant["ravi"]["id"]}
    assert drafts[start.isoformat()]["kind"] == "OFF" and drafts[(start + timedelta(days=1)).isoformat()]["kind"] == "OFF"
    assert drafts[(start + timedelta(days=2)).isoformat()]["shift_id"] == s["LATE"]  # index 8 → token A → LATE
    assert drafts[(start + timedelta(days=2)).isoformat()]["source"] == "ROTATION"


def test_a_planner_validates_but_cannot_publish(app, plant):
    from tests.helpers import invite_and_activate

    admin = plant["admin"]
    role = admin.post("/api/v1/roles", json={"name": "Roster planner", "description": "plans", "grants": [
        {"permission": "roster:read", "scope": "COMPANY"}, {"permission": "roster:plan", "scope": "COMPANY"}]})
    assert role.status_code in (200, 201), role.text
    kiran = create_employee(admin, "Kiran", branch_id=plant["branch"]["id"])
    planner = invite_and_activate(app, admin, kiran, ["employee"])
    assert admin.put(f"/api/v1/users/{planner.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    from tests.helpers import login

    planner = login(app, kiran["email"])  # a fresh session carries the new grants
    _drafts(plant, _legal_week(plant))
    r = planner.post(f"/api/v1/rosters/{plant['period']['id']}/validate")
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is True and r.json()["blocking"] == []
    assert planner.post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={}).status_code == 403
