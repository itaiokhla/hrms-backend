"""S5 three-company isolation of the shift module (07-security-privacy-compliance; 06 §scopes; spine "cross-company
access fails closed as not found").

Company A is the AS-M6 ``plant`` (published roster, a swap request, an open shift, availability, a rule, a rotation);
companies B and C are fresh tenants on attendance v2 with the shift switches on, so their answers come from the
tenant boundary and not from a feature gate. Every shift route that takes an id is called by B's and C's company
administrators (the widest in-tenant access) with A's ids and a schema-valid body: each answers 404, nothing of A
changes, and A's rows never appear in B's or C's lists.
"""

from datetime import datetime, timedelta, timezone

from tests.attendance.capture_v2_helpers import rows, sql
from tests.attendance.test_attendance_days import enable_v2
from tests.helpers import signup_company
from tests.shifts.test_shift_requests import enable, entry, plant, submit  # noqa: F401


def _tenant(app, name):
    client = signup_company(app, name)
    enable_v2(client.company_id)
    enable(client.company_id, "shift.requests.enabled")
    enable(client.company_id, "shift.open_shifts.enabled")
    return client


def _company_a(plant) -> dict:
    admin, s = plant["admin"], plant["shifts"]
    fri = entry(plant, "Ravi", 4)
    request = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    sat = plant["start"] + timedelta(days=5)
    r = admin.post("/api/v1/open-shifts", json={"branch_id": plant["branch"]["id"], "shift_date": sat.isoformat(),
                                                "shift_id": s["EARLY"], "required_count": 1})
    assert r.status_code == 201, r.text
    open_shift = r.json()
    r = plant["people"]["Mohan"].post("/api/v1/me/availability", json={
        "kind": "UNAVAILABLE", "weekday": 6, "start_date": (plant["start"] + timedelta(days=30)).isoformat()})
    assert r.status_code == 201, r.text
    availability = r.json()["id"]
    r = admin.post("/api/v1/shift-assignment-rules", json={"name": "Everyone early", "shift_id": s["EARLY"],
                                                         "criteria": {"everyone": True},
                                                         "effective_from": (plant["start"] + timedelta(days=60)).isoformat()})
    assert r.status_code in (200, 201), r.text
    rule = r.json()["id"]
    r = admin.post("/api/v1/rotations", json={"code": "ISO-ROT", "name": "Isolation rotation",
                                              "template_key": "SIX_ON_TWO_OFF_SLOW", "version": {"token_map": {
                                                  t: {"shift_id": s["EARLY"]} for t in ("M", "A", "N")}}})
    assert r.status_code == 201, r.text
    return {"shift": s["EARLY"], "rule": rule, "period": plant["period"]["id"], "pattern": r.json()["id"],
            "request": request["id"], "open_shift": open_shift["id"], "open_row_version": open_shift["row_version"],
            "availability": availability, "employee": plant["ids"]["Ravi"], "entry": fri["id"]}


def _calls(a: dict) -> list:
    later = (datetime.now(timezone.utc) + timedelta(days=40)).date().isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()
    return [
        ("GET", f"/shifts/{a['shift']}", None),
        ("PATCH", f"/shifts/{a['shift']}", {"name": "Taken"}),
        ("POST", f"/shifts/{a['shift']}/archive", {}),
        ("POST", f"/shifts/{a['shift']}/restore", {}),
        ("GET", f"/shifts/{a['shift']}/versions", None),
        ("POST", f"/shifts/{a['shift']}/versions", {"effective_from": later, "start_time": "07:00", "end_time": "15:00"}),
        ("POST", f"/shifts/{a['shift']}/versions/1/void", {"reason": "Taken over"}),
        ("GET", f"/shift-assignment-rules/{a['rule']}", None),
        ("PATCH", f"/shift-assignment-rules/{a['rule']}", {"name": "Taken"}),
        ("GET", f"/rosters/{a['period']}", None),
        ("PUT", f"/rosters/{a['period']}/drafts", {"cells": [{"employee_id": a["employee"], "shift_date": later,
                                                              "kind": "OFF"}]}),
        ("DELETE", f"/rosters/{a['period']}/drafts", None),
        ("POST", f"/rosters/{a['period']}/validate", {}),
        ("POST", f"/rosters/{a['period']}/publish", {}),
        ("POST", f"/rosters/{a['period']}/cancel", {}),
        ("POST", f"/rosters/{a['period']}/close", {}),
        ("POST", f"/rosters/{a['period']}/generate", {}),
        ("POST", f"/rotations/{a['pattern']}/versions", {"token_map": {}}),
        ("POST", f"/rotations/{a['pattern']}/versions/1/activate", {}),
        ("GET", f"/rotations/{a['pattern']}/versions/1/preview?from={later}&to={later}", None),
        ("GET", f"/shift-requests/{a['request']}", None),
        ("POST", f"/shift-requests/{a['request']}/accept", {}),
        ("POST", f"/shift-requests/{a['request']}/decline", {}),
        ("POST", f"/shift-requests/{a['request']}/withdraw", None),
        ("POST", f"/shift-requests/{a['request']}/approve", {}),
        ("POST", f"/shift-requests/{a['request']}/reject", {"note": "Taken"}),
        ("POST", f"/shift-requests/{a['request']}/extend", {"expires_at": expires, "reason": "Taken over"}),
        ("GET", f"/open-shifts/{a['open_shift']}", None),
        ("PATCH", f"/open-shifts/{a['open_shift']}", {"row_version": a["open_row_version"], "required_count": 2}),
        ("POST", f"/open-shifts/{a['open_shift']}/claim", {}),
        ("POST", f"/open-shifts/{a['open_shift']}/cancel", {}),
        ("POST", f"/me/availability/{a['availability']}/cancel", None),
        ("POST", f"/availability/{a['availability']}/approve", {}),
        ("PUT", f"/employees/{a['employee']}/skill-tags", {"tags": []}),
        ("POST", f"/me/schedule/entries/{a['entry']}/acknowledge", {}),
        ("GET", f"/shifts/resolve?employee_id={a['employee']}&from={later}&to={later}", None),
    ]


def _state(a: dict) -> dict:
    """What a successful cross-tenant write would change."""
    return {
        "shift": rows("SELECT name, status, row_version FROM shifts WHERE id = :i", i=a["shift"]),
        "rule": rows("SELECT name, row_version FROM shift_assignment_rules WHERE id = :i", i=a["rule"]),
        "period": rows("SELECT status, row_version FROM roster_periods WHERE id = :i", i=a["period"]),
        "request": rows("SELECT status, row_version, expires_at FROM shift_requests WHERE id = :i", i=a["request"]),
        "open_shift": rows("SELECT status, required_count, row_version FROM open_shifts WHERE id = :i",
                           i=a["open_shift"]),
        "availability": rows("SELECT status FROM employee_availability WHERE id = :i", i=a["availability"]),
        "versions": rows("SELECT count(*) AS n FROM shift_versions WHERE shift_id = :i", i=a["shift"]),
        "patterns": rows("SELECT count(*) AS n FROM rotation_pattern_versions WHERE pattern_id = :i", i=a["pattern"]),
    }


def test_every_shift_route_with_an_id_is_not_found_for_two_other_companies(app, plant):
    a = _company_a(plant)
    before = _state(a)
    b, c = _tenant(app, "Isolation B"), _tenant(app, "Isolation C")
    wrong = []
    for client, label in ((b, "B"), (c, "C")):
        for method, path, body in _calls(a):
            kwargs = {"json": body} if body is not None else {}
            r = client.request(method, f"/api/v1{path}", **kwargs)
            if r.status_code != 404:
                wrong.append((label, method, path, r.status_code, r.text[:160]))
    assert wrong == [], "\n".join(map(str, wrong))
    assert _state(a) == before  # nothing of company A changed


def test_an_employee_of_the_same_company_cannot_use_planner_or_other_peoples_routes(plant):
    """Privilege escalation inside company A: Sita (default employee role, SELF) is not a party to Ravi's request, does
    not own Mohan's availability or Ravi's entry, and plans nothing — every call fails closed (403 or 404) and nothing
    changes. Claiming an open shift is her right and is not part of the sweep."""
    a = _company_a(plant)
    before = _state(a)
    sita, sita_id = plant["people"]["Sita"], plant["ids"]["Sita"]
    # Reads Sita may legitimately make: her own shift (BUG-AS-35) and the rotation pattern (no personal data); her
    # roster period opens with her own row only and no drafts (IG-AS-144, checked below).
    own_reads = {f"/shifts/{a['shift']}", f"/shifts/{a['shift']}/versions", f"/rosters/{a['period']}"}
    wrong = []
    for method, path, body in _calls(a):
        if path.endswith("/claim") or (method == "GET" and (path in own_reads or "/preview" in path)):
            continue
        kwargs = {"json": body} if body is not None else {}
        r = sita.request(method, f"/api/v1{path}", **kwargs)
        if r.status_code not in (403, 404):
            wrong.append((method, path, r.status_code, r.text[:160]))
    assert wrong == [], "\n".join(map(str, wrong))
    assert _state(a) == before
    grid = sita.get(f"/api/v1/rosters/{a['period']}").json()
    assert [e["id"] for e in grid["employees"]] == [sita_id]
    assert {e["employee_id"] for e in grid["entries"]} == {sita_id} and grid["drafts"] == []
    full = plant["admin"].get(f"/api/v1/rosters/{a['period']}").json()
    assert {plant["ids"]["Ravi"], plant["ids"]["Mohan"], sita_id} <= {e["id"] for e in full["employees"]}


def test_shift_jobs_run_for_one_company_never_touch_another(app, plant):
    """Background jobs are per tenant (ctx.company_id, bound session): running every shift job for company B — far in
    the future, where A's period has ended and A's request and open shift are past due — changes nothing of A."""
    from app.database.db import SessionLocal, bind_tenant
    from app.services import roster_consents, shift_requests, shift_rosters, shift_rotations

    a = _company_a(plant)
    sql("UPDATE roster_periods SET status = 'PUBLISHED' WHERE id = :p", p=a["period"])
    before = _state(a)
    notes = rows("SELECT count(*) AS n FROM notifications")[0]["n"]
    b = _tenant(app, "Jobs B")
    far = datetime.now(timezone.utc) + timedelta(days=400)
    with SessionLocal() as db:
        bind_tenant(db, b.company_id)
        shift_rosters.close_due_periods(db, b.company_id, far.date())
        shift_rosters.publish_reminders(db, b.company_id, far.date() - timedelta(days=300))
        shift_rotations.auto_publish(db, b.company_id, far.date())
        shift_requests.expire_due(db, b.company_id, now=far)
        # EC AS-CTY-06 rescan on a date where A has published entries: A's planners hear nothing from B's run.
        assert roster_consents.compliance_rescan(db, b.company_id, plant["start"])["checked"] == 0
        db.commit()
    assert _state(a) == before
    assert rows("SELECT count(*) AS n FROM notifications")[0]["n"] == notes


def test_privileged_shift_actions_write_the_documented_audit_actions(plant):
    """AS 11 §9: shift, rotation, roster, open-shift, request, availability and skill-tag changes are audited under
    the catalogue's action names, in the acting company."""
    admin, s = plant["admin"], plant["shifts"]
    a = _company_a(plant)
    r = admin.post(f"/api/v1/rotations/{a['pattern']}/versions/1/activate")
    assert r.status_code == 200, r.text
    version = next(p for p in admin.get("/api/v1/rotations").json() if p["id"] == a["pattern"])["current_version_id"]
    r = admin.post("/api/v1/rotations/assignments", json={
        "pattern_version_id": version, "employee_ids": [plant["ids"]["Mohan"]], "crew_code": "A", "offset_days": 0,
        "anchor_date": (plant["start"] + timedelta(days=60)).isoformat(),
        "effective_from": (plant["start"] + timedelta(days=60)).isoformat()})
    assert r.status_code == 201, r.text
    assert admin.post("/api/v1/skill-tags", json={"code": "FIRST_AID", "name": "First aid"}).status_code == 201
    assert admin.put(f"/api/v1/employees/{plant['ids']['Mohan']}/skill-tags",
                     json={"tags": [{"code": "FIRST_AID"}]}).status_code == 200
    assert plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{a['request']}/accept", json={}).status_code == 200
    assert admin.post(f"/api/v1/shift-requests/{a['request']}/reject", json={"note": "Not this week"}).status_code == 200
    spare = admin.post("/api/v1/shifts", json={"code": "SPARE", "name": "Spare", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "test", "start_time": "10:00", "end_time": "18:00"}}).json()["id"]
    assert admin.post(f"/api/v1/shifts/{spare}/archive", json={}).status_code == 200
    actions = {r["action"] for r in rows("SELECT action FROM audit_logs WHERE company_id = :c AND module IN "
                                         "('Shifts', 'Rosters')", c=plant["cid"])}
    expected = {"SHIFT_SAVED", "SHIFT_ARCHIVED", "ROSTER_PUBLISHED", "ROTATION_SAVED", "ROTATION_ACTIVATED",
                "ROTATION_ASSIGNED", "OPEN_SHIFT_POSTED", "SHIFT_REQUEST_DECIDED", "AVAILABILITY_SAVED",
                "SKILL_TAG_SAVED"}
    assert expected <= actions, sorted(expected - actions)


def test_lists_of_other_companies_never_contain_company_a_rows(app, plant):
    a = _company_a(plant)
    for client in (_tenant(app, "Lists B"), _tenant(app, "Lists C")):
        assert a["shift"] not in [s["id"] for s in client.get("/api/v1/shifts").json()]
        assert a["rule"] not in [x["id"] for x in client.get("/api/v1/shift-assignment-rules").json().get("items", [])]
        assert a["period"] not in [p["id"] for p in client.get("/api/v1/rosters").json()["items"]]
        assert a["pattern"] not in [p["id"] for p in client.get("/api/v1/rotations").json()]
        for box in ("mine", "incoming", "approvals"):
            r = client.get("/api/v1/shift-requests", params={"box": box})
            assert r.status_code in (200, 403, 404) and a["request"] not in [
                x["id"] for x in (r.json().get("items", []) if r.status_code == 200 else [])]
        listed = client.get("/api/v1/open-shifts", params={"eligible_only": "false"}).json()["items"]
        assert a["open_shift"] not in [x["id"] for x in listed]
        r = client.get("/api/v1/availability")
        assert r.status_code in (200, 403) and a["availability"] not in [
            x["id"] for x in (r.json().get("items", r.json()) if r.status_code == 200 else [])]
