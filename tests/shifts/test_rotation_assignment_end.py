"""Ending a rotation assignment (03-shifts/02 §3.3; spine §4.12; AS 11 §6.4; UA-AS-161).

``POST /rotation-assignments/{id}/end`` sets ``effective_to`` and ``ENDED``, deletes the not-yet-published ``ROTATION``
drafts of the person after the end (a planner's own draft and the drafts up to the end stay) and writes audit
``ROTATION_ASSIGNED``. A second end is 409 ``INVALID_TRANSITION``, a stale ``row_version`` 409 ``ROW_VERSION_CONFLICT``;
an employee without ``shift:assign`` is refused (403) and another company gets 404; neither changes anything. The
canonical ``/rotation-assignments`` paths serve the same handlers as the ``/rotations/assignments`` aliases.
"""

from datetime import timedelta

from sqlalchemy import text

from app.database.db import system_engine
from tests.helpers import create_employee, invite_and_activate, login, signup_company
from tests.shifts.test_rosters import _cell, _drafts, plant  # noqa: F401


def _sql(query, **params):
    with system_engine.begin() as conn:
        return conn.execute(text(query), params)


def _assignment(admin, assignment_id):
    return next(a for a in admin.get("/api/v1/rotation-assignments", params={"limit": 500}).json()["items"]
                if a["id"] == assignment_id)


def _drafts_of(plant, employee_id):
    grid = plant["admin"].get(f"/api/v1/rosters/{plant['period']['id']}").json()
    return {d["shift_date"]: d for d in grid["drafts"] if d["employee_id"] == employee_id}


def test_ending_a_rotation_assignment_removes_later_rotation_drafts_and_refuses_the_rest(app, plant):
    admin, s, start = plant["admin"], plant["shifts"], plant["start"]
    ravi, sita = plant["ravi"]["id"], plant["sita"]["id"]
    r = admin.post("/api/v1/rotations", json={
        "code": "END-6ON2OFF", "name": "End test", "template_key": "SIX_ON_TWO_OFF_SLOW",
        "version": {"token_map": {"M": {"shift_id": s["EARLY"]}, "A": {"shift_id": s["LATE"]},
                                  "N": {"shift_id": s["NIGHT"]}}}})
    assert r.status_code == 201, r.text
    pattern_id = r.json()["id"]
    assert admin.post(f"/api/v1/rotations/{pattern_id}/versions/1/activate").status_code == 200
    version = next(p for p in admin.get("/api/v1/rotations").json() if p["id"] == pattern_id)["current_version_id"]
    ids = {}
    for person, crew, offset in ((ravi, "B", 6), (sita, "C", 12)):
        r = admin.post("/api/v1/rotation-assignments", json={  # the canonical path
            "pattern_version_id": version, "employee_ids": [person], "crew_code": crew, "offset_days": offset,
            "anchor_date": start.isoformat(), "effective_from": start.isoformat()})
        assert r.status_code == 201, r.text
        [row] = r.json()["items"]
        assert (row["status"], row["row_version"]) == ("ACTIVE", 1)
        ids[person] = row["id"]
    assert admin.post(f"/api/v1/rosters/{plant['period']['id']}/generate", json={}).json()["generated"] == 28
    # A planner's own draft after the end date is not the rotation's: it stays.
    _drafts(plant, [_cell(plant["ravi"], start + timedelta(days=10), s["LONG"])])
    assert _drafts_of(plant, ravi)[(start + timedelta(days=10)).isoformat()]["source"] == "MANUAL"
    period_version = admin.get(f"/api/v1/rosters/{plant['period']['id']}").json()["period"]["row_version"]

    # Another company: 404, nothing changes.
    other = signup_company(app, "Other End Co")
    r = other.post(f"/api/v1/rotation-assignments/{ids[sita]}/end",
                   json={"effective_to": (start + timedelta(days=3)).isoformat(), "reason": "not mine"})
    assert r.status_code == 404, r.text
    # An employee without shift:assign: refused, nothing changes.
    worker = invite_and_activate(app, admin, create_employee(admin, "Meera", branch_id=plant["branch"]["id"]),
                                 ["employee"])
    r = login(app, worker.email).post(f"/api/v1/rotation-assignments/{ids[sita]}/end",
                                      json={"effective_to": (start + timedelta(days=3)).isoformat(), "reason": "x"})
    assert r.status_code == 403, r.text
    # A stale row_version (body or If-Match): 409 ROW_VERSION_CONFLICT, nothing changes.
    r = admin.post(f"/api/v1/rotation-assignments/{ids[sita]}/end",
                   json={"effective_to": (start + timedelta(days=3)).isoformat(), "reason": "stale", "row_version": 7})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT", r.text
    r = admin.post(f"/api/v1/rotation-assignments/{ids[sita]}/end", headers={"If-Match": 'W/"3"'},
                   json={"effective_to": (start + timedelta(days=3)).isoformat(), "reason": "stale"})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT", r.text
    sita_row = _assignment(admin, ids[sita])
    assert (sita_row["status"], sita_row["effective_to"], sita_row["row_version"]) == ("ACTIVE", None, 1)
    assert len(_drafts_of(plant, sita)) == 14

    # Success: Ravi's rotation ends on day 7 (start + 6).
    end = start + timedelta(days=6)
    r = admin.post(f"/api/v1/rotation-assignments/{ids[ravi]}/end",
                   json={"effective_to": end.isoformat(), "reason": "Moves to the day crew", "row_version": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["status"], body["effective_to"], body["row_version"]) == ("ENDED", end.isoformat(), 2)
    assert body["drafts_removed"] == 6  # days 8..14 minus the planner's own draft on day 11
    left = _drafts_of(plant, ravi)
    assert sorted(left) == [(start + timedelta(days=n)).isoformat() for n in range(7)] + [
        (start + timedelta(days=10)).isoformat()]
    assert all(left[(start + timedelta(days=n)).isoformat()]["source"] == "ROTATION" for n in range(7))
    assert len(_drafts_of(plant, sita)) == 14  # other people's drafts are untouched
    assert admin.get(f"/api/v1/rosters/{plant['period']['id']}").json()["period"]["row_version"] == period_version + 1
    assert _assignment(admin, ids[ravi])["status"] == "ENDED"
    audit = _sql("SELECT details FROM audit_logs WHERE action = 'ROTATION_ASSIGNED' AND entity_type = 'rotation_assignment' "
                 "AND entity_id = :i", i=str(ids[ravi])).all()
    assert len(audit) == 1
    assert audit[0].details["action"] == "END" and audit[0].details["reason"] == "Moves to the day crew"
    assert audit[0].details["effective_to"] == {"before": None, "after": end.isoformat()}

    # An ENDED row still covers its dates up to its last day: regeneration refills them and never goes past it, and a
    # new assignment overlapping those days is refused.
    _sql("DELETE FROM roster_drafts WHERE employee_id = :e AND source = 'ROTATION'", e=ravi)
    assert admin.post(f"/api/v1/rosters/{plant['period']['id']}/generate", json={}).status_code == 200
    refilled = _drafts_of(plant, ravi)
    assert sorted(d for d, v in refilled.items() if v["source"] != "MANUAL") == [
        (start + timedelta(days=n)).isoformat() for n in range(7)]
    clash = admin.post("/api/v1/rotation-assignments", json={
        "pattern_version_id": version, "employee_ids": [ravi], "crew_code": "A", "offset_days": 0,
        "anchor_date": start.isoformat(), "effective_from": (start + timedelta(days=3)).isoformat()})
    assert clash.status_code == 409 and clash.json()["code"] == "ROTATION_OVERLAP", clash.text

    # A second end: 409 INVALID_TRANSITION.
    r = admin.post(f"/api/v1/rotation-assignments/{ids[ravi]}/end",
                   json={"effective_to": (end - timedelta(days=1)).isoformat(), "reason": "again"})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION", r.text
    assert r.json()["details"]["current_status"] == "ENDED"
    # An end before the first day is refused (the row keeps effective_to >= effective_from).
    r = admin.post(f"/api/v1/rotation-assignments/{ids[sita]}/end",
                   json={"effective_to": (start - timedelta(days=1)).isoformat(), "reason": "too early"})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR", r.text
    # The old alias path still lists the same rows.
    alias = admin.get("/api/v1/rotations/assignments", params={"pattern_id": pattern_id}).json()
    assert {a["id"]: a["status"] for a in alias["items"]} == {ids[ravi]: "ENDED", ids[sita]: "ACTIVE"}
