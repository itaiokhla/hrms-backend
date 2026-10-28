"""Edge cases AS-ROS-01, -03, -04, -05, -06, -07, -14, -15 and AS-ROT-06 (API level of
docs/attendance-shift/edge-cases.json), plus the 90-day roster generator of the G-14 definition of done.

AS-ROS-08 / -09: approved leave on a re-rostered date is flagged (leave id and units only, never the leave type)
and recounted by the publication.

AS-ROS-06 (consents from AS-M2 G-06): a valid NIGHT_WORK consent satisfies the night-work rule at publish, and a
withdrawal revalidates the published future entries (job roster.revalidate_employee) without withdrawing any.

Not automated here (its module is a later goal, IG-AS-32 / FU-AS-12): AS-ROS-11 (planner-cannot-publish setting,
AS-M3).
"""

from datetime import timedelta

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
