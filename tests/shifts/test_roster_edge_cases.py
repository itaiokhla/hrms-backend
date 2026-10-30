"""Edge cases AS-ROS-01, -03, -04, -05, -06, -07, -14, -15 and AS-ROT-06 (API level of
docs/attendance-shift/edge-cases.json), plus the 90-day roster generator of the G-14 definition of done.

AS-ROS-08 / -09: approved leave on a re-rostered date is flagged (leave id and units only, never the leave type)
and recounted by the publication.

AS-ROS-06 (consents from AS-M2 G-06): a valid NIGHT_WORK consent satisfies the night-work rule at publish, and a
withdrawal revalidates the published future entries (job roster.revalidate_employee) without withdrawing any.

AS-ROS-11 (planner-cannot-publish setting) is automated since S2 (IG-AS-143).
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database.db import system_engine
from tests.helpers import create_employee, invite_and_activate, login, signup_company
from tests.shifts.test_rosters import _ack_all, _cell, _drafts, _legal_week, _publish, _today, plant  # noqa: F401


def _sql(query, **params):
    with system_engine.begin() as conn:
        return conn.execute(text(query), params)


def test_as_ros_01_dates_in_locked_payroll_periods_warn_or_need_a_reason(plant):
    from tests.payroll.test_runs_lifecycle import calendar_for
    from tests.payroll.test_wage_inputs import pay_group

    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    cid = plant["admin"].company_id
    entity = _sql("SELECT legal_entity_id FROM employee_assignments WHERE employee_id = :e", e=ravi["id"]).scalar()
    group = pay_group(cid, entity, "PG-ROSTER")
    calendar_for(plant["admin"], group, rules={"monthly_start_day": 1, "pay_day_rule": "LAST_DAY"})
    _sql("UPDATE employee_assignments SET pay_group_id = :g WHERE employee_id = :e", g=group, e=ravi["id"])
    period = _sql("SELECT id FROM pay_periods WHERE pay_group_id = :g AND attendance_start <= :d AND attendance_end >= :d",
                  g=group, d=start).scalar()
    assert period, "the pay calendar covers the roster dates"
    _drafts(plant, [_cell(ravi, start, s["EARLY"])])
    _sql("UPDATE pay_periods SET status = 'PROCESSING' WHERE id = :p", p=period)
    locked = [r for r in _publish(plant, dry_run=True)["results"] if r["rule_key"] == "PERIOD_LOCKED"]
    assert [(r["severity"], r["measured"]) for r in locked] == [("WARN", "INPUT_LOCKED")]
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={})
    assert r.status_code == 422 and r.json()["code"] == "WARNINGS_UNACKNOWLEDGED"
    _sql("UPDATE pay_periods SET status = 'CLOSED' WHERE id = :p", p=period)
    finalized = [r for r in _publish(plant, dry_run=True)["results"] if r["rule_key"] == "PERIOD_LOCKED"]
    assert [(r["severity"], r["measured"]) for r in finalized] == [("REQUIRE_REASON", "FINALIZED")]
    _sql("UPDATE pay_periods SET status = 'OPEN' WHERE id = :p", p=period)


def test_as_ros_03_publications_above_the_cap_are_refused_and_a_narrower_scope_publishes(plant, monkeypatch):
    from app.services import shift_rosters

    _drafts(plant, _legal_week(plant))
    monkeypatch.setattr(shift_rosters, "MAX_PUBLISH_ENTRIES", 3)
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={})
    assert r.status_code == 422 and r.json()["code"] == "ROSTER_PUBLISH_TOO_LARGE"
    one = plant["start"].isoformat()
    preview = _publish(plant, dry_run=True, **{"from": one, "to": one})
    keys = sorted({x["rule_key"] for x in preview["results"] if x["severity"] == "WARN"})
    done = _publish(plant, acknowledged_warnings=keys, **{"from": one, "to": one})
    assert done["entries_created"] == 1 and done["period"]["status"] == "DRAFT"  # drafts remain: still DRAFT


def test_as_ros_04_entries_after_the_last_working_day_are_refused_not_employed(plant):
    s, start, sita = plant["shifts"], plant["start"], plant["sita"]
    _sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=start + timedelta(days=1), e=sita["id"])
    _drafts(plant, [_cell(sita, start + timedelta(days=3), s["EARLY"])])
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={})
    assert r.status_code == 422 and r.json()["code"] == "ROSTER_RULE_VIOLATION"
    assert any(i["rule_key"] == "NOT_EMPLOYED" for i in r.json()["issues"])


def test_as_ros_05_an_old_branch_shift_after_a_transfer_is_out_of_placement(plant):
    admin, start, ravi = plant["admin"], plant["start"], plant["ravi"]
    entity = admin.get("/api/v1/legal-entities").json()[0]
    ward = admin.post("/api/v1/branches", json={"legal_entity_id": entity["id"], "code": "WARD", "name": "Ward",
                                                "timezone": "Asia/Kolkata"}).json()
    plant_shift = admin.post("/api/v1/shifts", json={"code": "PLANT-B", "name": "Plant B", "kind": "FIXED",
                                                     "branch_id": plant["branch"]["id"], "version": {
                                                         "effective_from": "2024-01-01", "reason": "ec",
                                                         "start_time": "14:00", "end_time": "22:00"}}).json()
    moved = admin.post(f"/api/v1/employees/{ravi['id']}/assignments", json={
        "branch_id": ward["id"], "effective_from": (start + timedelta(days=3)).isoformat(), "reason": "TRANSFER"})
    assert moved.status_code == 200, moved.text
    _drafts(plant, [_cell(ravi, start + timedelta(days=5), plant_shift["id"])])
    r = admin.post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={})
    assert r.status_code == 422 and r.json()["code"] == "ROSTER_RULE_VIOLATION"
    assert any(i["rule_key"] == "ENTRY_OUT_OF_PLACEMENT" for i in r.json()["issues"])


def test_as_ros_07_a_cell_published_by_one_period_is_refused_in_another(plant):
    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    _drafts(plant, _legal_week(plant))
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    team = plant["admin"].post("/api/v1/teams", json={"code": "CREW-X", "name": "Crew X", "team_type": "CREW",
                                                      "effective_from": "2024-01-01"}).json()["id"]
    assert plant["admin"].post(f"/api/v1/teams/{team}/members", json={
        "employee_id": ravi["id"], "effective_from": "2024-02-01"}).status_code == 201
    crew = _sql("INSERT INTO roster_periods (company_id, code, name, team_id, start_date, end_date, status, "
                "requires_approval, row_version) VALUES (:c, 'RP-CREW', 'Crew period', :t, :s, :e, 'DRAFT', false, 1) "
                "RETURNING id", c=plant["admin"].company_id, t=team, s=start, e=start + timedelta(days=6)).scalar()
    r = plant["admin"].put(f"/api/v1/rosters/{crew}/drafts", json={"cells": [_cell(ravi, start, s["LATE"])]})
    assert r.status_code == 409 and r.json()["code"] == "ROSTER_CELL_IN_OTHER_PERIOD"
    assert r.json()["details"]["period_code"] == "RP-TEST-1"


def test_as_ros_14_a_draft_whose_published_base_moved_is_stale(plant):
    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    _drafts(plant, _legal_week(plant))
    first = _publish(plant, acknowledged_warnings=_ack_all(plant))
    again = _publish(plant)  # the second publisher finds nothing to publish
    assert again["nothing_to_publish"] and again["publication_id"] == first["publication_id"]
    _drafts(plant, [_cell(ravi, start + timedelta(days=2), s["LONG"])])  # base = version 1
    r = plant["admin"].post("/api/v1/rosters/direct-publish", json={"cells": [
        _cell(ravi, start + timedelta(days=2), s["EARLY"], note="moved by a swap")], "acknowledged_warnings": []})
    if r.status_code == 422:
        keys = sorted({i["rule_key"] for i in r.json()["issues"]})
        r = plant["admin"].post("/api/v1/rosters/direct-publish", json={"cells": [
            _cell(ravi, start + timedelta(days=2), s["EARLY"], note="moved by a swap")], "acknowledged_warnings": keys})
    assert r.status_code == 200, r.text
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={})
    assert r.status_code == 409 and r.json()["code"] == "ROSTER_STALE_DRAFT"


def test_as_ros_15_out_of_scope_employees_are_404_and_nothing_is_written(app, plant):
    admin = plant["admin"]
    packing = admin.post("/api/v1/departments", json={"name": "Packing"}).json()["id"]
    role = admin.post("/api/v1/roles", json={"name": "Packing planner", "description": "plans packing", "grants": [
        {"permission": "roster:read", "scope": "DEPARTMENT"}, {"permission": "roster:plan", "scope": "DEPARTMENT"}]})
    assert role.status_code in (200, 201), role.text
    kiran = create_employee(admin, "Kiran", branch_id=plant["branch"]["id"], department_id=packing)
    mate = create_employee(admin, "Mate", branch_id=plant["branch"]["id"], department_id=packing)
    user = invite_and_activate(app, admin, kiran, ["employee"])
    assert admin.put(f"/api/v1/users/{user.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    planner = login(app, kiran["email"])
    period = plant["period"]["id"]
    s, start = plant["shifts"], plant["start"]
    mixed = [_cell(mate, start, s["EARLY"]), _cell(plant["ravi"], start, s["EARLY"])]
    r = planner.put(f"/api/v1/rosters/{period}/drafts", json={"cells": mixed})
    assert r.status_code == 404  # Ravi is outside the planner's departments: the whole call fails
    assert _sql("SELECT count(*) FROM roster_drafts WHERE roster_period_id = :p", p=period).scalar() == 0
    r = planner.put(f"/api/v1/rosters/{period}/drafts", json={"cells": mixed[:1]})
    assert r.status_code == 200, r.text
    other = signup_company(app, "Other Planner Co")
    assert other.put(f"/api/v1/rosters/{period}/drafts", json={"cells": mixed[:1]}).status_code == 404


def _rotation(plant):
    admin, s = plant["admin"], plant["shifts"]
    r = admin.post("/api/v1/rotations", json={
        "code": "EC-6ON2OFF", "name": "EC 6 on 2 off", "template_key": "SIX_ON_TWO_OFF_SLOW",
        "version": {"token_map": {"M": {"shift_id": s["EARLY"]}, "A": {"shift_id": s["LATE"]},
                                  "N": {"shift_id": s["NIGHT"]}}}})
    assert r.status_code == 201, r.text
    assert admin.post(f"/api/v1/rotations/{r.json()['id']}/versions/1/activate").status_code == 200
    return next(p for p in admin.get("/api/v1/rotations").json() if p["id"] == r.json()["id"])["current_version_id"]


def test_as_rot_06_rotation_assignments_cannot_be_backdated_beyond_the_limit(plant):
    version = _rotation(plant)
    body = {"pattern_version_id": version, "employee_ids": [plant["ravi"]["id"]], "crew_code": "A", "offset_days": 0,
            "anchor_date": (_today() - timedelta(days=10)).isoformat()}
    r = plant["admin"].post("/api/v1/rotations/assignments",
                            json={**body, "effective_from": (_today() - timedelta(days=10)).isoformat()})
    assert r.status_code == 422 and r.json()["code"] == "BACKDATE_LIMIT"
    assert r.json()["details"]["earliest"] == (_today() - timedelta(days=7)).isoformat()
    r = plant["admin"].post("/api/v1/rotations/assignments",
                            json={**body, "effective_from": (_today() - timedelta(days=5)).isoformat()})
    assert r.status_code == 201, r.text


def test_the_roster_generator_builds_90_days_forward(plant):
    version = _rotation(plant)
    today = _today()
    r = plant["admin"].post("/api/v1/rotations/assignments", json={
        "pattern_version_id": version, "employee_ids": [plant["ravi"]["id"]], "crew_code": "B", "offset_days": 6,
        "anchor_date": today.isoformat(), "effective_from": today.isoformat()})
    assert r.status_code == 201, r.text
    r = plant["admin"].post("/api/v1/rotations/generate-ahead", json={"branch_id": plant["branch"]["id"], "days": 90})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["from"], body["to"]) == (today.isoformat(), (today + timedelta(days=89)).isoformat())
    covered = set()
    for period in body["periods"]:
        first, last = (_d(period["start_date"]), _d(period["end_date"]))
        assert (last - first).days <= 61  # every period stays within the 62-day cap
        covered |= {first + timedelta(days=i) for i in range((last - first).days + 1)}
    assert {today + timedelta(days=i) for i in range(90)} <= covered  # 90 days forward, no gap
    drafts = _sql("SELECT shift_date, kind, shift_id FROM roster_drafts WHERE employee_id = :e AND source = 'ROTATION' "
                  "ORDER BY shift_date", e=plant["ravi"]["id"]).all()
    days = [d for d, _, _ in drafts if today <= d <= today + timedelta(days=89)]
    assert len(days) == 90 and days[0] == today and days[-1] == today + timedelta(days=89)
    # Crew B offset 6 from today: idx 6, 7 OFF then 8 = token A -> LATE (GV-AS-ROT-01 shape).
    assert [k for _, k, _ in drafts[:3]] == ["OFF", "OFF", "WORK"] and drafts[2][2] == plant["shifts"]["LATE"]
    again = plant["admin"].post("/api/v1/rotations/generate-ahead", json={"branch_id": plant["branch"]["id"],
                                                                         "days": 90})
    assert again.status_code == 200 and len(again.json()["periods"]) == len(body["periods"])  # periods reused


def _d(value):
    from datetime import date

    return date.fromisoformat(value)


def test_as_ros_08_09_a_rostered_off_on_approved_leave_warns_masked_and_recounts(app, plant):
    """AS-ROS-08 (the planner sees the leave id and units, never its type) and AS-ROS-09 (publish recounts it)."""
    admin, s, start, ravi = plant["admin"], plant["shifts"], plant["start"], plant["ravi"]
    worker = invite_and_activate(app, admin, ravi, ["employee"])
    day = start + timedelta(days=1)
    leave = worker.post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": day.isoformat(),
                                                      "end_date": day.isoformat(), "reason": "family"})
    assert leave.status_code in (200, 201), leave.text
    leave = leave.json()
    assert admin.post(f"/api/v1/leaves/{leave['id']}/approve").status_code == 200
    charged = "SELECT counts_against_balance FROM leave_days WHERE leave_id = :l AND voided_at IS NULL"
    assert _sql(charged, l=leave["id"]).scalar() is True
    _drafts(plant, _legal_week(plant)[:1] + [_cell(ravi, day, kind="OFF")])
    preview = _publish(plant, dry_run=True)
    hits = [r for r in preview["results"] if r["rule_key"] == "ROSTER_AFFECTS_APPROVED_LEAVE"]
    assert [(h["severity"], h["employee_id"], h["dates"], h["measured"], h["limit"]) for h in hits] == [
        ("WARN", ravi["id"], [day.isoformat()], "0.00", "1.00")]
    assert hits[0]["details"] == {"leave_ref": leave["id"]}  # EC AS-ROS-08: the id travels, the type never does
    assert "casual" not in str(hits).lower() and "family" not in str(hits).lower()
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    assert _sql(charged, l=leave["id"]).scalar() is False  # EC AS-ROS-09: the rostered OFF released the day
    assert float(_sql("SELECT number_of_days FROM leaves WHERE id = :l", l=leave["id"]).scalar()) == 0


def test_a_team_owned_period_plans_the_team_members(plant):
    """X-M1: a roster period owned by a CREW team plans its members; an unknown team is 404."""
    admin, s, start, ravi, sita = plant["admin"], plant["shifts"], plant["start"], plant["ravi"], plant["sita"]
    crew = admin.post("/api/v1/teams", json={"code": "CREW-R", "name": "Crew R", "team_type": "CREW",
                                             "effective_from": "2024-01-01"}).json()["id"]
    assert admin.post(f"/api/v1/teams/{crew}/members", json={"employee_id": ravi["id"],
                                                             "effective_from": "2024-02-01"}).status_code == 201
    end = start + timedelta(days=20)
    r = admin.post("/api/v1/rosters", json={"code": "RP-CREW-R", "name": "Crew R", "team_id": crew,
                                            "start_date": (start + timedelta(days=14)).isoformat(),
                                            "end_date": end.isoformat()})
    assert r.status_code == 201, r.text
    grid = admin.get(f"/api/v1/rosters/{r.json()['id']}").json()
    assert [e["id"] for e in grid["employees"]] == [ravi["id"]]  # Sita is not a member
    assert admin.post("/api/v1/rosters", json={"code": "RP-NONE", "name": "None", "team_id": 999999,
                                               "start_date": start.isoformat(),
                                               "end_date": start.isoformat()}).status_code == 404


def test_as_ros_06_night_work_consent_satisfies_publish_and_its_withdrawal_flags_published_nights(plant):
    """EC AS-ROS-06 / GV-AS-ROS-14: Sita (no gender recorded -> WOMEN, IN pack night window 19:00-06:00) needs a
    NIGHT_WORK consent for night entries; a GRANTED consent in the attendance ledger satisfies the rule. When she
    withdraws it, job roster.revalidate_employee marks every published future night entry (pending item
    roster_consent_conflict + grid badge) and notifies the planners; no entry is withdrawn. A new grant resolves them."""
    s, start, sita, admin = plant["shifts"], plant["start"], plant["sita"], plant["admin"]
    period_id = plant["period"]["id"]
    nights = [start + timedelta(days=i) for i in range(3)]
    _drafts(plant, [_cell(sita, d, s["NIGHT"]) for d in nights])
    missing = [r for r in _publish(plant, dry_run=True)["results"] if r["rule_key"] == "NIGHT_WORK_CONSENT_MISSING"]
    assert {r["employee_id"] for r in missing} == {sita["id"]} and missing[0]["severity"] == "REQUIRE_REASON"
    r = admin.post("/api/v1/attendance/consents", json={"employee_id": sita["id"], "purpose": "NIGHT_WORK",
                                                        "status": "GRANTED", "notice_version": "NW-IN-1"})
    assert r.status_code == 201, r.text
    preview = _publish(plant, dry_run=True)
    assert not [x for x in preview["results"] if x["rule_key"] == "NIGHT_WORK_CONSENT_MISSING"]
    published = _publish(plant, acknowledged_warnings=_ack_all(plant))
    assert published["entries_created"] == 3
    assert _sql("SELECT count(*) FROM roster_consent_conflicts").scalar() == 0

    r = admin.post("/api/v1/attendance/consents", json={"employee_id": sita["id"], "purpose": "NIGHT_WORK",
                                                        "status": "WITHDRAWN"})
    assert r.status_code == 201, r.text
    conflicts = admin.get("/api/v1/attendance/roster-consent-conflicts", params={"period_id": period_id}).json()
    assert [(c["shift_date"], c["purpose"], c["rule_key"], c["kind"]) for c in conflicts["items"]] == [
        (d.isoformat(), "NIGHT_WORK", "NIGHT_WORK_CONSENT_MISSING", "roster_consent_conflict") for d in nights]
    grid = admin.get(f"/api/v1/rosters/{period_id}").json()
    assert len(grid["consent_conflicts"]) == 3 and all(c["purposes"] == ["NIGHT_WORK"] for c in grid["consent_conflicts"])
    assert _sql("SELECT count(*) FROM roster_entries WHERE employee_id = :e AND withdrawn_at IS NULL",
                e=sita["id"]).scalar() == 3  # nothing withdrawn automatically
    notes = _sql("SELECT title, message FROM notifications WHERE title = 'Schedule needs review'").all()
    assert notes and "3 published shift(s)" in notes[0][1]

    r = admin.post("/api/v1/attendance/consents", json={"employee_id": sita["id"], "purpose": "NIGHT_WORK",
                                                        "status": "GRANTED", "notice_version": "NW-IN-2"})
    assert r.status_code == 201, r.text
    assert _sql("SELECT count(*) FROM roster_consent_conflicts WHERE status = 'OPEN'").scalar() == 0
    assert _sql("SELECT count(*) FROM roster_consent_conflicts WHERE status = 'RESOLVED'").scalar() == 3


def test_x_int_51_rotation_drafts_stop_at_the_last_working_day(plant):
    """X-INT-51: a rotation assignment is implicitly bounded by employment — no drafts after the LWD, so nothing
    leaves a NOT_EMPLOYED BLOCK behind for later publications."""
    version = _rotation(plant)
    start, ravi = plant["start"], plant["ravi"]["id"]
    r = plant["admin"].post("/api/v1/rotations/assignments", json={
        "pattern_version_id": version, "employee_ids": [ravi], "crew_code": "A", "offset_days": 0,
        "anchor_date": start.isoformat(), "effective_from": start.isoformat()})
    assert r.status_code == 201, r.text
    last = start + timedelta(days=3)
    _sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=last, e=ravi)
    r = plant["admin"].post(f"/api/v1/rosters/{plant['period']['id']}/generate", json={})
    assert r.status_code == 200, r.text
    days = sorted(d["shift_date"] for d in plant["admin"].get(f"/api/v1/rosters/{plant['period']['id']}").json()["drafts"]
                  if d["employee_id"] == ravi)
    assert days and max(days) == last.isoformat() and min(days) == start.isoformat()


def _job(fn, plant, day):
    from app.database.db import SessionLocal, bind_tenant

    with SessionLocal() as db:
        bind_tenant(db, plant["admin"].company_id)
        result = fn(db, plant["admin"].company_id, day)
        db.commit()
    return result


def test_roster_close_periods_closes_published_periods_that_ended(plant):
    """Job roster.close_periods (03-shifts/02 §4.2): PUBLISHED, ended and no date still OPEN in payroll -> CLOSED.
    Dates in OPEN pay periods keep the period PUBLISHED; once every date is in a CLOSED (finalized) pay period it
    closes; with no pay group at all there is no payroll state to wait for (IG-AS-139)."""
    from app.services import shift_rosters
    from tests.payroll.test_runs_lifecycle import calendar_for
    from tests.payroll.test_wage_inputs import pay_group

    period, cid = plant["period"], plant["admin"].company_id
    start, end = date.fromisoformat(period["start_date"]), date.fromisoformat(period["end_date"])
    entity = _sql("SELECT legal_entity_id FROM employee_assignments WHERE employee_id = :e",
                  e=plant["ravi"]["id"]).scalar()
    group = pay_group(cid, entity, "PG-CLOSE")
    calendar_for(plant["admin"], group, rules={"monthly_start_day": 1, "pay_day_rule": "LAST_DAY"})
    _sql("UPDATE employee_assignments SET pay_group_id = :g WHERE company_id = :c", g=group, c=cid)
    _sql("UPDATE roster_periods SET status = 'PUBLISHED' WHERE id = :p", p=period["id"])
    assert _job(shift_rosters.close_due_periods, plant, end)["closed"] == []  # ends today: not yet
    assert _job(shift_rosters.close_due_periods, plant, end + timedelta(days=1))["closed"] == []  # payroll still OPEN
    _sql("UPDATE pay_periods SET status = 'CLOSED' WHERE pay_group_id = :g AND attendance_start <= :e "
         "AND attendance_end >= :s", g=group, s=start, e=end)
    assert _job(shift_rosters.close_due_periods, plant, end + timedelta(days=1))["closed"] == [period["id"]]
    assert _sql("SELECT status FROM roster_periods WHERE id = :p", p=period["id"]).scalar() == "CLOSED"
    assert _sql("SELECT count(*) FROM audit_logs WHERE action = 'ROSTER_PERIOD_CLOSED' AND entity_id = :p",
                p=str(period["id"])).scalar() == 1
    assert _job(shift_rosters.close_due_periods, plant, end + timedelta(days=2))["closed"] == []  # idempotent
    # IG-AS-139: nobody in a pay group -> nothing to wait for.
    _sql("UPDATE pay_periods SET status = 'OPEN' WHERE pay_group_id = :g", g=group)
    _sql("UPDATE employee_assignments SET pay_group_id = NULL WHERE company_id = :c", c=cid)
    _sql("UPDATE roster_periods SET status = 'PUBLISHED' WHERE id = :p", p=period["id"])
    assert _job(shift_rosters.close_due_periods, plant, end + timedelta(days=1))["closed"] == [period["id"]]


def test_rotation_auto_publish_fills_the_horizon_without_overriding_and_in_chunks(plant, monkeypatch):
    """Job roster.rotation_generate (03-shifts/02 §3.5, EC AS-REL-06): ROTATION_AUTO publications without a publisher up
    to the horizon; a rerun adds nothing; a day with a planner's draft is left alone; a BLOCK stops that employee only
    and notifies the planners; chunks of at most AUTO_PUBLISH_CHUNK entries."""
    from types import SimpleNamespace

    from app.services import roster_consents, shift_rosters, shift_rotations

    early = plant["shifts"]["EARLY"]  # day work only: a night token would need night-work consent (REQUIRE_REASON)
    r = plant["admin"].post("/api/v1/rotations", json={
        "code": "AUTO-DAYS", "name": "Auto days", "template_key": "SIX_ON_TWO_OFF_SLOW",
        "version": {"token_map": {t: {"shift_id": early} for t in ("M", "A", "N")}}})
    assert r.status_code == 201, r.text
    assert plant["admin"].post(f"/api/v1/rotations/{r.json()['id']}/versions/1/activate").status_code == 200
    version = next(p for p in plant["admin"].get("/api/v1/rotations").json()
                   if p["id"] == r.json()["id"])["current_version_id"]
    today = _today()
    ravi, sita = plant["ravi"]["id"], plant["sita"]["id"]
    kabir = create_employee(plant["admin"], "Kabir", branch_id=plant["branch"]["id"])["id"]
    for person, crew in ((ravi, "A"), (sita, "B"), (kabir, "C")):
        r = plant["admin"].post("/api/v1/rotations/assignments", json={
            "pattern_version_id": version, "employee_ids": [person], "crew_code": crew, "offset_days": 0,
            "anchor_date": today.isoformat(), "effective_from": today.isoformat(), "auto_publish": True})
        assert r.status_code == 201 and r.json()["items"][0]["auto_publish"] is True, r.text
    held = today + timedelta(days=3)  # a planner is working on Ravi's day: no auto-publish there
    _sql("INSERT INTO roster_drafts (company_id, roster_period_id, employee_id, shift_date, entry_seq, action, kind, "
         "source, created_at, updated_at) VALUES (:c, :p, :e, :d, 1, 'UPSERT', 'OFF', 'MANUAL', now(), now())",
         c=plant["admin"].company_id, p=plant["period"]["id"], e=ravi, d=held)
    real = shift_rosters.validate_cells

    def kabir_blocks(db, company_id, candidate, changed, **kw):
        outcome = real(db, company_id, candidate, changed, **kw)
        if any(c.employee_id == kabir for c in changed):
            outcome.blocking.append(SimpleNamespace(rule_key="MIN_REST_VIOLATION", severity="BLOCK"))
        return outcome

    monkeypatch.setattr(shift_rosters, "validate_cells", kabir_blocks)
    horizon = roster_consents.HORIZON_DAYS + 1
    monkeypatch.setattr(shift_rotations, "AUTO_PUBLISH_CHUNK", horizon + 1)  # one employee's days fit, two do not
    result = _job(shift_rotations.auto_publish, plant, today)
    assert result["blocked"] == {kabir: ["MIN_REST_VIOLATION"]} and result["failed_chunks"] == 0
    assert result["entries"] == 2 * horizon - 1  # every horizon day of Ravi and Sita but Ravi's drafted one
    assert len(result["publications"]) == 2  # chunks along employees, each within the bound
    pubs = _sql("SELECT kind, published_by_user_id FROM roster_publications WHERE id = ANY(:i)",
                i=result["publications"]).all()
    assert {(p.kind, p.published_by_user_id) for p in pubs} == {("ROTATION_AUTO", None)}
    rows = _sql("SELECT shift_date, source FROM roster_entries WHERE employee_id = :e AND withdrawn_at IS NULL",
                e=ravi).all()
    assert held not in {r.shift_date for r in rows} and {r.source for r in rows} == {"ROTATION"}
    assert _sql("SELECT count(*) FROM roster_entries WHERE employee_id = :e", e=kabir).scalar() == 0
    assert _sql("SELECT count(*) FROM notifications WHERE action_url LIKE '/rosters?autopub=%'").scalar() >= 1
    again = _job(shift_rotations.auto_publish, plant, today)
    assert again["entries"] == 0 and again["publications"] == []  # never overrides, nothing left to fill


def test_as_ros_11_the_planner_who_saved_the_drafts_cannot_publish_them(app, plant):
    """EC AS-ROS-11 / GV-AS-ROS-16: shift.roster.planner_cannot_publish. Switching it on needs two publishers (422
    MAKER_CHECKER_UNAVAILABLE, V-AS-05); the last draft saver gets 403 SOD_PLANNER_PUBLISH (not lifted by
    roster:exempt; the dry run still works); a second publisher publishes; when the second publisher's role ends the
    refusal also tells the shift:manage COMPANY holders (roster.sod_unavailable)."""
    admin = plant["admin"]
    r = admin.put("/api/v1/shift-requests/settings", json={"planner_cannot_publish": True})
    assert r.status_code == 422 and r.json()["code"] == "MAKER_CHECKER_UNAVAILABLE", r.text
    role = admin.post("/api/v1/roles", json={"name": "Roster publisher", "description": "publishes", "grants": [
        {"permission": "roster:read", "scope": "COMPANY"}, {"permission": "roster:publish", "scope": "COMPANY"}]})
    assert role.status_code in (200, 201), role.text
    invited = invite_and_activate(app, admin, create_employee(admin, "Asha", branch_id=plant["branch"]["id"]),
                                  ["employee"])
    assert admin.put(f"/api/v1/users/{invited.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    asha = login(app, invited.email)
    r = admin.put("/api/v1/shift-requests/settings", json={"planner_cannot_publish": True})
    assert r.status_code == 200 and r.json()["planner_cannot_publish"] is True, r.text

    _drafts(plant, _legal_week(plant))
    warnings = _ack_all(plant)  # the drafter's own dry run is allowed
    r = admin.post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={"acknowledged_warnings": warnings})
    assert r.status_code == 403 and r.json()["code"] == "SOD_PLANNER_PUBLISH", r.text
    assert r.json()["details"]["eligible_publishers"] == 1
    r = asha.post(f"/api/v1/rosters/{plant['period']['id']}/publish", json={"acknowledged_warnings": warnings})
    assert r.status_code == 200 and r.json().get("publication_id"), r.text

    # The second publisher leaves: the drafter is still refused and the shift managers are told.
    assert admin.put(f"/api/v1/users/{invited.user_id}/roles", json={"role_ids": []}).status_code == 200
    start = plant["start"]
    _drafts(plant, [_cell(plant["ravi"], start, kind="OFF")])
    r = admin.post(f"/api/v1/rosters/{plant['period']['id']}/publish",
                   json={"acknowledged_warnings": _ack_all(plant)})
    assert r.status_code == 403 and r.json()["details"]["eligible_publishers"] == 0, r.text
    assert _sql("SELECT count(*) FROM notifications WHERE action_url = :l",
                l=f"/rosters/{plant['period']['id']}?sod=unavailable").scalar() >= 1


def test_x_int_50_a_leavers_leftover_entries_do_not_block_an_unrelated_publish(plant):
    """X-INT-50: integrity rules (NOT_EMPLOYED, SHIFT_OVERLAP) judge only the entries a publication touches (overlap:
    pairs with a changed member), so Ravi's published entries after his last working day do not block a change to a
    day he still works."""
    start, ravi = plant["start"], plant["ravi"]
    _drafts(plant, _legal_week(plant))
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    _sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=start + timedelta(days=2), e=ravi["id"])
    _drafts(plant, [_cell(ravi, start + timedelta(days=1), kind="OFF")])
    preview = _publish(plant, dry_run=True)
    assert [r for r in preview["results"] if r["rule_key"] == "NOT_EMPLOYED"] == [], preview["results"]
    assert preview["blocking"] == []
    # A change on a day after the LWD is still refused.
    _drafts(plant, [_cell(ravi, start + timedelta(days=4), kind="OFF")])
    blocking = _publish(plant, dry_run=True)["blocking"]
    assert [r["rule_key"] for r in blocking] == ["NOT_EMPLOYED"]


def test_as_cty_08_an_outdoor_shift_in_the_ae_midday_season_warns_at_publish(plant):
    """EC AS-CTY-08 through the publish path: the AE WORKING_TIME pack (not verified) carries
    seasonal.outdoor_midday_ban 12:30–15:00, 06-15..09-15; an outdoor shift version at a Dubai branch on 1 July warns
    OUTDOOR_MIDDAY_BAN, the same day with an indoor shift does not."""
    admin = plant["admin"]
    entity = admin.post("/api/v1/legal-entities", json={"code": "LE-AE", "legal_name": "Alpha Gulf", "country_code": "AE",
                                                        "base_currency": "AED", "timezone": "Asia/Dubai"})
    assert entity.status_code in (200, 201), entity.text
    branch = admin.post("/api/v1/branches", json={"legal_entity_id": entity.json()["id"], "code": "DXB",
                                                  "name": "Dubai yard", "timezone": "Asia/Dubai"})
    assert branch.status_code in (200, 201), branch.text
    branch = branch.json()
    worker = create_employee(admin, "Omar", legal_entity_id=entity.json()["id"], branch_id=branch["id"])
    shifts = {}
    for code, outdoor in (("YARD", True), ("OFFICE", False)):
        r = admin.post("/api/v1/shifts", json={"code": code, "name": code.title(), "kind": "FIXED", "version": {
            "effective_from": "2024-01-01", "reason": "test", "start_time": "09:00", "end_time": "17:00",
            "outdoor": outdoor}})
        assert r.status_code == 201, r.text
        shifts[code] = r.json()["id"]
    r = admin.post("/api/v1/rosters", json={"code": "RP-DXB-JUL", "name": "Dubai July", "branch_id": branch["id"],
                                            "start_date": "2027-07-01", "end_date": "2027-07-02"})
    assert r.status_code == 201, r.text
    period = r.json()
    cells = [{"employee_id": worker["id"], "shift_date": "2027-07-01", "kind": "WORK", "shift_id": shifts["YARD"]},
             {"employee_id": worker["id"], "shift_date": "2027-07-02", "kind": "WORK", "shift_id": shifts["OFFICE"]}]
    r = admin.put(f"/api/v1/rosters/{period['id']}/drafts", json={"cells": cells, "row_version": period["row_version"]})
    assert r.status_code == 200, r.text
    preview = admin.post(f"/api/v1/rosters/{period['id']}/publish", json={"dry_run": True}).json()
    bans = [r for r in preview["results"] if r["rule_key"] == "OUTDOOR_MIDDAY_BAN"]
    assert [(r["dates"], r["severity"]) for r in bans] == [(["2027-07-01"], "WARN")], preview["results"]


def _pay_group_for(plant, code):
    from tests.payroll.test_runs_lifecycle import calendar_for
    from tests.payroll.test_wage_inputs import pay_group

    cid = plant["admin"].company_id
    entity = _sql("SELECT legal_entity_id FROM employee_assignments WHERE employee_id = :e",
                  e=plant["ravi"]["id"]).scalar()
    group = pay_group(cid, entity, code)
    calendar_for(plant["admin"], group, rules={"monthly_start_day": 1, "pay_day_rule": "LAST_DAY"})
    _sql("UPDATE employee_assignments SET pay_group_id = :g WHERE company_id = :c", g=group, c=cid)
    return group


def test_as_pay_15_a_new_shift_version_leaves_entries_on_input_locked_dates_on_the_old_version(plant):
    """EC AS-PAY-15 (owner decision IG-AS-150): the re-versioning publication only touches OPEN payroll dates; entries
    on INPUT_LOCKED dates keep their old shift_version_id and the effects say how many were left."""
    start, ravi, s = plant["start"], plant["ravi"], plant["shifts"]
    _drafts(plant, _legal_week(plant))
    _publish(plant, acknowledged_warnings=_ack_all(plant))
    group = _pay_group_for(plant, "PG-PAY15")
    _sql("UPDATE pay_periods SET status = 'PROCESSING' WHERE pay_group_id = :g AND attendance_start <= :e "
         "AND attendance_end >= :s", g=group, s=start, e=start + timedelta(days=6))
    before = {r.id: r.shift_version_id for r in _sql(
        "SELECT id, shift_version_id FROM roster_entries WHERE employee_id = :e AND kind = 'WORK' AND "
        "withdrawn_at IS NULL", e=ravi["id"]).all()}
    r = plant["admin"].post(f"/api/v1/shifts/{s['EARLY']}/versions", json={
        "effective_from": (_today() + timedelta(days=1)).isoformat(), "start_time": "06:30", "end_time": "14:30",
        "reason": "Later start"})
    assert r.status_code == 201, r.text
    effects = r.json()["effects"]["roster"]
    assert effects["entries_reversioned"] == 0 and effects["entries_left_on_previous_version"] == len(before) == 5
    after = {r.id: r.shift_version_id for r in _sql(
        "SELECT id, shift_version_id FROM roster_entries WHERE employee_id = :e AND kind = 'WORK' AND "
        "withdrawn_at IS NULL", e=ravi["id"]).all()}
    assert after == before


def test_as_pay_14_a_correction_in_a_finalized_period_is_an_exemption_with_step_up(plant):
    """EC AS-PAY-14 (owner decision IG-AS-150): a roster day 10 days back is beyond the 7-day backdate limit (BLOCK);
    with the date's pay period FINALIZED it also needs a PERIOD_LOCKED reason. It publishes only as an exemption by a
    roster:exempt holder with a fresh step-up."""
    from tests.attendance.capture_v2_helpers import step_up

    day = _today() - timedelta(days=10)
    group = _pay_group_for(plant, "PG-PAY14")
    _sql("UPDATE pay_periods SET status = 'CLOSED' WHERE pay_group_id = :g AND attendance_start <= :d "
         "AND attendance_end >= :d", g=group, d=day)
    cell = {"action": "UPSERT", "employee_id": plant["ravi"]["id"], "shift_date": day.isoformat(), "entry_seq": 1,
            "kind": "OFF"}
    admin = plant["admin"]
    dry = admin.post("/api/v1/rosters/direct-publish", json={"cells": [cell], "reason_code": "PLANNER",
                                                              "dry_run": True}).json()
    assert "BACKDATE_LIMIT" in {b["rule_key"] for b in dry["blocking"]}, dry
    body = {"cells": [cell], "reason_code": "PLANNER",
            "acknowledged_warnings": sorted({x["rule_key"] for x in dry["results"] if x["severity"] == "WARN"}),
            "reasons": {x["rule_key"]: "Payroll correction" for x in dry["results"] if x["severity"] == "REQUIRE_REASON"}}
    r = admin.post("/api/v1/rosters/direct-publish", json=body)
    assert r.status_code == 422, r.text  # BLOCK without an exemption
    body["exemptions"] = {"BACKDATE_LIMIT": {"exemption_ref": "PAYROLL-CORR-14", "reason": "Finalized period fix"}}
    r = admin.post("/api/v1/rosters/direct-publish", json=body)
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"  # roster:exempt is explicit-only
    from tests.attendance.capture_v2_helpers import grant

    grant(admin.company_id, "company_admin", "roster:exempt")
    r = admin.post("/api/v1/rosters/direct-publish", json=body)
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED", r.text
    step_up(admin)
    r = admin.post("/api/v1/rosters/direct-publish", json=body)
    assert r.status_code == 200 and r.json()["publication_id"], r.text
    assert _sql("SELECT count(*) FROM audit_logs WHERE action = 'ROSTER_EXEMPTION_USED'").scalar() >= 1


def test_publication_approval_freezes_the_drafts_until_a_covering_publisher_decides(app, plant):
    """03-shifts/02 §5.2 / spine §4.1 (owner decision IG-AS-150): with shift.roster.publish_requires_approval a planner
    (roster:plan, no roster:publish) sends the roster for approval: an engine request (module roster_publish) opens,
    drafts are frozen (409 ROSTER_PUBLISH_PENDING); a covering roster:publish holder approves in the inbox and the
    roster is published as previewed; a rejected request unfreezes the drafts."""
    admin = plant["admin"]
    r = admin.put("/api/v1/shift-requests/settings", json={"publish_requires_approval": True})
    assert r.status_code == 200 and r.json()["publish_requires_approval"] is True, r.text
    role = admin.post("/api/v1/roles", json={"name": "Planner only", "description": "plans", "grants": [
        {"permission": "roster:read", "scope": "COMPANY"}, {"permission": "roster:plan", "scope": "COMPANY"}]})
    assert role.status_code in (200, 201), role.text
    invited = invite_and_activate(app, admin, create_employee(admin, "Pia", branch_id=plant["branch"]["id"]),
                                  ["employee"])
    assert admin.put(f"/api/v1/users/{invited.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    planner = login(app, invited.email)
    period = plant["period"]["id"]

    def save(cells):
        version = planner.get(f"/api/v1/rosters/{period}").json()["period"]["row_version"]
        return planner.put(f"/api/v1/rosters/{period}/drafts", json={"cells": cells, "row_version": version})

    assert save(_legal_week(plant)).status_code == 200
    dry = planner.post(f"/api/v1/rosters/{period}/publish", json={"dry_run": True}).json()
    assert dry["requires_approval"] is True and dry["blocking"] == []
    warnings = sorted({r["rule_key"] for r in dry["results"] if r["severity"] == "WARN"})
    r = planner.post(f"/api/v1/rosters/{period}/publish", json={"acknowledged_warnings": warnings})
    assert r.status_code == 200 and r.json()["pending_approval"] is True, r.text
    engine_id = r.json()["approval_request_id"]
    assert r.json()["snapshot_hash"] == dry["snapshot_hash"]
    r = save([_cell(plant["ravi"], plant["start"], kind="OFF")])
    assert r.status_code == 409 and r.json()["code"] == "ROSTER_PUBLISH_PENDING", r.text
    assert _sql("SELECT count(*) FROM roster_entries WHERE roster_period_id = :p", p=period).scalar() == 0
    inbox = admin.get("/api/v1/approvals", params={"module": "roster_publish"}).json()["items"]
    assert engine_id in [i["id"] for i in inbox]
    r = admin.post(f"/api/v1/approvals/{engine_id}/decide", json={"decision": "APPROVE", "remarks": "Looks right"})
    assert r.status_code == 200, r.text
    state = _sql("SELECT status, last_publication_id FROM roster_periods WHERE id = :p", p=period).one()
    assert state.status == "PUBLISHED" and state.last_publication_id is not None
    publication = _sql("SELECT kind, snapshot_hash, published_by_user_id FROM roster_publications WHERE id = :i",
                       i=state.last_publication_id).one()
    assert publication.kind == "PERIOD" and publication.snapshot_hash == dry["snapshot_hash"]
    assert publication.published_by_user_id != invited.user_id  # the approver publishes, never the maker
    # A rejected request unfreezes the drafts.
    assert save([_cell(plant["ravi"], plant["start"], kind="OFF")]).status_code == 200
    dry = planner.post(f"/api/v1/rosters/{period}/publish", json={"dry_run": True}).json()
    r = planner.post(f"/api/v1/rosters/{period}/publish", json={
        "acknowledged_warnings": sorted({r["rule_key"] for r in dry["results"] if r["severity"] == "WARN"}),
        "reasons": {r["rule_key"]: "Planned" for r in dry["results"] if r["severity"] == "REQUIRE_REASON"}})
    assert r.status_code == 200 and r.json()["pending_approval"] is True, r.text
    r = admin.post(f"/api/v1/approvals/{r.json()['approval_request_id']}/decide",
                   json={"decision": "REJECT", "remarks": "Keep Monday"})
    assert r.status_code == 200, r.text
    assert save([_cell(plant["ravi"], plant["start"], plant["shifts"]["EARLY"])]).status_code == 200


def test_roster_publish_reminders_notify_planners_once_a_day(plant):
    """Job roster.publish_reminders (spine notification roster.publish_due, dedup pubdue:{period}:{local_date})."""
    from app.services import shift_rosters

    period = plant["period"]
    today = _today()
    _sql("UPDATE roster_periods SET status = 'DRAFT', publish_due_at = now() + interval '1 day' WHERE id = :p",
         p=period["id"])
    first = _job(shift_rosters.publish_reminders, plant, today)
    assert first["periods"] == 1 and first["notified"] >= 1
    assert _job(shift_rosters.publish_reminders, plant, today)["notified"] == 0  # same local date: deduplicated
    assert _job(shift_rosters.publish_reminders, plant, today + timedelta(days=1))["notified"] == first["notified"]
    links = _sql("SELECT DISTINCT action_url FROM notifications WHERE action_url LIKE :l",
                 l=f"/rosters/{period['id']}?due=%").all()
    assert len(links) == 2
    _sql("UPDATE roster_periods SET publish_due_at = now() + interval '10 days' WHERE id = :p", p=period["id"])
    assert _job(shift_rosters.publish_reminders, plant, today + timedelta(days=2))["periods"] == 0  # not near yet


def test_as_cty_17_a_held_exemption_reaches_publish_validation_only_in_its_state(plant):
    """EC AS-CTY-17 wiring (appendix-a §3.3, §5.10): the IN pack's women-at-night field applies to a night shift
    (gender unknown → WOMEN, AS-ROS-10). Holding TG_IT_ITES_2024 at the branch (setting
    shift.working_time.held_exemptions, BRANCH scope) waives it only once the branch is in Telangana; the packs
    overview lists what the branch holds and where it applies. Expiry by shift date is the pure test of the same
    name."""
    admin, s, start, ravi = plant["admin"], plant["shifts"], plant["start"], plant["ravi"]
    branch = plant["branch"]["id"]
    _drafts(plant, [_cell(ravi, start, s["NIGHT"])])

    def night_rule():
        dry = _publish(plant, dry_run=True)
        return "NIGHT_WORK_CONSENT_MISSING" in {r["rule_key"] for r in dry["results"]
                                                if r.get("employee_id") == ravi["id"]}

    assert night_rule()  # precondition: the IN pack's field applies
    r = admin.put("/api/v1/payroll/settings/values", json={"changes": [{
        "key": "shift.working_time.held_exemptions", "value": ["TG_IT_ITES_2024"], "scope_type": "BRANCH",
        "scope_id": branch, "reason": "Telangana IT/ITES order"}]})
    assert r.status_code == 200 and r.json()["changes"][0]["status"] == "ACTIVE", r.text
    assert night_rule()  # held, but the branch has no Telangana subdivision: an unknown input never exempts
    _sql("UPDATE branches SET subdivision_code = 'IN-TG' WHERE id = :b", b=branch)
    assert not night_rule()
    entity = next(e for e in admin.get("/api/v1/attendance/packs").json()["items"] if e["exemptions"])
    (held,) = entity["exemptions"]
    assert held["branch_id"] == branch and held["applies"] == ["TG_IT_ITES_2024"] and held["expiring"] == []
    bad = admin.put("/api/v1/payroll/settings/values", json={"changes": [{
        "key": "shift.working_time.held_exemptions", "value": ["MADE_UP"], "scope_type": "BRANCH",
        "scope_id": branch, "reason": "x"}]})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_CHOICE", bad.text


def test_as_cty_06_the_daily_rescan_tells_planners_when_published_findings_change(plant):
    """EC AS-CTY-06 (03-shifts/02 §6.5, appendix-a §14 step 6b): job roster.compliance_rescan re-validates published
    future entries with no publish involved. Ravi's published night shift carries the women-at-night finding
    (gender unknown); the planners hear about it once. When his date of birth shows he is under 18 the next rescan
    adds ADOLESCENT_LIMITS and tells them again; an unchanged rescan stays silent; no entry is ever changed."""
    from app.services import roster_consents

    s, start, ravi = plant["shifts"], plant["start"], plant["ravi"]
    _drafts(plant, [_cell(ravi, start, s["NIGHT"])])
    preview = _publish(plant, dry_run=True)
    reasons = {r["rule_key"]: "Agreed with the employee" for r in preview["results"]
               if r["severity"] == "REQUIRE_REASON"}
    _publish(plant, acknowledged_warnings=sorted({r["rule_key"] for r in preview["results"]
                                                  if r["severity"] == "WARN"}), reasons=reasons)
    version = _sql("SELECT version FROM roster_entries WHERE employee_id = :e", e=ravi["id"]).scalar()
    today = _today()

    first = _job(roster_consents.compliance_rescan, plant, today)
    (counts,) = first["branches"].values()
    assert any(k.startswith("NIGHT_WORK_CONSENT_MISSING:") for k in counts) and first["notified"] >= 1
    assert not any(k.startswith("ADOLESCENT_LIMITS:") for k in counts)
    assert _job(roster_consents.compliance_rescan, plant, today)["notified"] == 0  # same findings: silent

    _sql("UPDATE employees SET date_of_birth = :d WHERE id = :e", d=today.replace(year=today.year - 16), e=ravi["id"])
    second = _job(roster_consents.compliance_rescan, plant, today)
    (counts,) = second["branches"].values()
    assert any(k.startswith("ADOLESCENT_LIMITS:") for k in counts) and second["notified"] == first["notified"]
    texts = [m for (m,) in _sql("SELECT message FROM notifications WHERE title = 'Schedule compliance check' "
                                "ORDER BY id").all()]
    assert "Adolescent limits" in texts[-1] and "nothing was changed" in texts[-1]
    assert _sql("SELECT version FROM roster_entries WHERE employee_id = :e", e=ravi["id"]).scalar() == version


def test_as_tz_03_a_tz_release_that_moves_published_shifts_is_recorded_as_a_system_change(plant):
    """EC AS-TZ-03 (02-system-design §7.4 runbook TZDATA-1): the first scan only records the zones in use (company
    Asia/Kolkata, the US-E1 anchor America/New_York). A later release in which New York no longer falls back moves
    Ravi's published US-E1 shift in the window: one SYSTEM DIRECT publication re-versions it (change row reason
    TZ_RULE_CHANGE, initiated_by SYSTEM, notice hours), planners and the employee are told, validation re-runs. His
    Kolkata shift is untouched and a rerun finds nothing."""
    from functools import partial

    from app.services import tzdata_scan

    admin, s, ravi = plant["admin"], plant["shifts"], plant["ravi"]
    r = admin.post("/api/v1/shifts", json={"code": "US-E1", "name": "US East", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "test", "start_time": "09:00", "end_time": "18:00",
        "anchor_timezone": "America/New_York"}})
    assert r.status_code == 201, r.text
    us = r.json()["id"]
    now = datetime.now(timezone.utc)
    real = tzdata_scan.installed_rules("America/New_York", now, now + timedelta(days=tzdata_scan.WINDOW_DAYS))
    (t1, _), (t2, _) = real["transitions"][:2]
    t1, t2 = datetime.fromisoformat(t1), datetime.fromisoformat(t2)
    moved = (t1 + timedelta(days=3)).date()  # between the next two New York transitions
    assert moved < t2.date()
    kept = moved + timedelta(days=2)
    cells = [{"action": "UPSERT", "employee_id": ravi["id"], "shift_date": moved.isoformat(), "entry_seq": 1,
              "kind": "WORK", "shift_id": us},
             {"action": "UPSERT", "employee_id": ravi["id"], "shift_date": kept.isoformat(), "entry_seq": 1,
              "kind": "WORK", "shift_id": s["EARLY"]}]
    dry = admin.post("/api/v1/rosters/direct-publish", json={"cells": cells, "reason_code": "PLANNER",
                                                             "dry_run": True}).json()
    assert dry["blocking"] == [], dry
    r = admin.post("/api/v1/rosters/direct-publish", json={
        "cells": cells, "reason_code": "PLANNER",
        "acknowledged_warnings": sorted({x["rule_key"] for x in dry["results"] if x["severity"] == "WARN"}),
        "reasons": {x["rule_key"]: "Client hours" for x in dry["results"] if x["severity"] == "REQUIRE_REASON"}})
    assert r.status_code == 200, r.text

    first = _job(tzdata_scan.impact_scan, plant, _today())
    assert {"America/New_York", "Asia/Kolkata"} <= set(first["recorded"]) and first["changed"] == []

    def no_fall_back(zone, start, end):  # the "new release": New York keeps its current offset
        rules = tzdata_scan.installed_rules(zone, start, end)
        return {"initial": rules["initial"], "transitions": []} if zone == "America/New_York" else rules

    scan = partial(tzdata_scan.impact_scan, rules_of=no_fall_back)
    second = _job(scan, plant, _today())
    assert second["changed"] == ["America/New_York"] and second["entries"] == 1 and second["recompute_jobs"] == 0
    rows = _sql("SELECT c.shift_date, c.reason_code, c.initiated_by, c.notice_hours, c.change_kind, e.version "
                "FROM roster_entry_changes c JOIN roster_entries e ON e.id = c.entry_id "
                "WHERE c.publication_id = :p", p=second["publication_id"]).all()
    assert [(r.shift_date, r.reason_code, r.initiated_by, r.change_kind, r.version) for r in rows] == [
        (moved, "TZ_RULE_CHANGE", "SYSTEM", "UPDATED", 2)]
    assert rows[0].notice_hours is not None
    assert _sql("SELECT version FROM roster_entries WHERE employee_id = :e AND shift_date = :d",
                e=ravi["id"], d=kept).scalar() == 1
    assert _sql("SELECT count(*) FROM notifications WHERE title = 'Time-zone rules changed'").scalar() >= 1
    assert "rescan" in second
    third = _job(scan, plant, _today())
    assert third["changed"] == [] and third["publication_id"] is None


def test_publication_notices_shift_reminders_and_tag_expiry_reach_the_right_people(app, plant):
    """Spine §9.3 (FU-AS-53): roster.published / roster.changed go to each employee whose entries a publication
    created or changed, once per publication (dates and shift codes only); roster.shift_reminder reaches the employee
    once per entry version inside the lead time and never earlier; shift.tag_expiring reaches planners once per tag
    assignment ending within 30 days."""
    from app.services import shift_requests, shift_rosters

    admin, s, start, sita = plant["admin"], plant["shifts"], plant["start"], plant["sita"]
    user = invite_and_activate(app, admin, sita, ["employee"]).user_id

    def notes(title):
        return _sql("SELECT message, action_url FROM notifications WHERE user_id = :u AND title = :t ORDER BY id",
                    u=user, t=title).all()

    _drafts(plant, [_cell(sita, start, s["EARLY"])])
    first = _publish(plant, acknowledged_warnings=_ack_all(plant))
    (published,) = notes("Your schedule was published")
    assert "EARLY" in published.message and published.action_url == (
        f"/rosters/my-schedule?publication={first['publication_id']}")
    _drafts(plant, [_cell(sita, start, s["LATE"])])
    dry = _publish(plant, dry_run=True)  # LATE reaches into the women-at-night window: a reason is needed
    _publish(plant, acknowledged_warnings=_ack_all(plant),
             reasons={r["rule_key"]: "Agreed" for r in dry["results"] if r["severity"] == "REQUIRE_REASON"})
    (changed,) = notes("Your schedule changed")
    assert "LATE" in changed.message

    late_start = datetime.combine(start, datetime.min.time()).replace(hour=14) - timedelta(hours=5, minutes=30)
    late_start = late_start.replace(tzinfo=timezone.utc)  # LATE 14:00 Asia/Kolkata
    assert _job(shift_rosters.shift_reminders, plant, late_start - timedelta(hours=2))["reminded"] == 0  # too early
    assert _job(shift_rosters.shift_reminders, plant, late_start - timedelta(minutes=30))["reminded"] == 1
    assert _job(shift_rosters.shift_reminders, plant, late_start - timedelta(minutes=20))["reminded"] == 0  # once
    (reminder,) = notes("Your shift starts soon")
    assert "LATE starts at 14:00" in reminder.message

    assert admin.post("/api/v1/skill-tags", json={"code": "FORKLIFT", "name": "Forklift"}).status_code == 201
    today = _today()
    r = admin.put(f"/api/v1/employees/{sita['id']}/skill-tags", json={"tags": [
        {"code": "FORKLIFT", "valid_to": (today + timedelta(days=20)).isoformat()}]})
    assert r.status_code == 200, r.text
    assert _job(shift_requests.tag_expiry, plant, today)["notified"] >= 1
    assert _job(shift_requests.tag_expiry, plant, today)["notified"] == 0  # once per tag assignment
    assert _job(shift_requests.tag_expiry, plant, today - timedelta(days=20))["expiring"] == 0  # 40 days out
