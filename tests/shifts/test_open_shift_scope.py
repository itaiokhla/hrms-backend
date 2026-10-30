"""Open-shift scope (FU-AS-49; 03-shifts/03 §6.3, §11, §12.3; SP-AS-045).

``roster:plan`` must cover the branch / team an open shift plans for: a planner scoped to the Packing department
(whose people all work at the Packing branch) posts, sees, opens and cancels Packing open shifts, and gets 404 for
the plant branch where Ravi, Sita and Mohan work. Employees see only the open shifts they can claim unless they ask
for the others (``eligible_only=false``). Another company never sees any of it.
"""

from datetime import datetime, timedelta

from tests.attendance.capture_v2_helpers import rows
from tests.helpers import create_employee, invite_and_activate, login, signup_company
from tests.shifts.test_shift_requests import plant  # noqa: F401


def _packing(app, plant):
    admin = plant["admin"]
    packing = admin.post("/api/v1/departments", json={"name": "Packing"}).json()["id"]
    branch = admin.post("/api/v1/branches", json={"legal_entity_id": plant["branch"]["legal_entity_id"],
                                                  "code": "PACK", "name": "Packing hall",
                                                  "timezone": "Asia/Kolkata"})
    assert branch.status_code in (200, 201), branch.text
    branch = branch.json()
    role = admin.post("/api/v1/roles", json={"name": "Packing planner", "description": "plans packing", "grants": [
        {"permission": "roster:read", "scope": "DEPARTMENT"}, {"permission": "roster:plan", "scope": "DEPARTMENT"}]})
    assert role.status_code in (200, 201), role.text
    kiran = create_employee(admin, "Kiran", branch_id=branch["id"], department_id=packing)
    create_employee(admin, "Mate", branch_id=branch["id"], department_id=packing)
    user = invite_and_activate(app, admin, kiran, ["employee"])
    assert admin.put(f"/api/v1/users/{user.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    return login(app, kiran["email"]), branch


def _post(client, plant, branch_id, **extra):
    sat = plant["start"] + timedelta(days=5)
    return client.post("/api/v1/open-shifts", json={"branch_id": branch_id, "shift_date": sat.isoformat(),
                                                    "shift_id": plant["shifts"]["EARLY"], "required_count": 1,
                                                    **extra})


def test_a_department_planner_posts_sees_and_cancels_only_open_shifts_it_covers(app, plant):
    planner, pack = _packing(app, plant)
    before = rows("SELECT count(*) AS n FROM open_shifts WHERE company_id = :c", c=plant["cid"])[0]["n"]
    r = _post(planner, plant, plant["branch"]["id"])
    assert r.status_code == 404, r.text  # the plant branch holds people outside Packing: not covered, not revealed
    assert rows("SELECT count(*) AS n FROM open_shifts WHERE company_id = :c", c=plant["cid"])[0]["n"] == before
    r = _post(planner, plant, pack["id"])
    assert r.status_code == 201, r.text
    own = r.json()["id"]
    plant_shift = _post(plant["admin"], plant, plant["branch"]["id"]).json()["id"]

    listed = planner.get("/api/v1/open-shifts").json()
    assert [s["id"] for s in listed["items"]] == [own] and listed["can_post"] is True
    admin_ids = {s["id"] for s in plant["admin"].get("/api/v1/open-shifts").json()["items"]}
    assert {own, plant_shift} <= admin_ids  # a company-scope planner sees both

    assert planner.get(f"/api/v1/open-shifts/{plant_shift}").status_code == 404
    assert planner.post(f"/api/v1/open-shifts/{plant_shift}/cancel").status_code == 404
    assert rows("SELECT status FROM open_shifts WHERE id = :i", i=plant_shift)[0]["status"] == "OPEN"
    assert planner.get(f"/api/v1/open-shifts/{own}").status_code == 200
    r = planner.post(f"/api/v1/open-shifts/{own}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text


def test_employees_see_only_claimable_open_shifts_unless_they_ask(app, plant):
    _, pack = _packing(app, plant)
    here = _post(plant["admin"], plant, plant["branch"]["id"]).json()["id"]
    there = _post(plant["admin"], plant, pack["id"]).json()["id"]  # same_branch: Mohan cannot take it
    mohan = plant["people"]["Mohan"]
    default = {s["id"]: s for s in mohan.get("/api/v1/open-shifts").json()["items"]}
    assert here in default and there not in default
    assert default[here]["can_claim"] is True
    everything = {s["id"]: s for s in mohan.get("/api/v1/open-shifts", params={"eligible_only": "false"}).json()["items"]}
    assert {here, there} <= set(everything)
    assert everything[there]["can_claim"] is False and everything[there]["reasons"]
    # A claim on a shift outside the employee's branch is still refused at the claim itself.
    r = mohan.post(f"/api/v1/open-shifts/{there}/claim", json={})
    assert r.status_code == 422 and r.json()["code"] == "NOT_ELIGIBLE", r.text


def test_the_open_shift_detail_shows_a_claimant_only_their_own_side(app, plant):
    """FU-AS-51 (g), A14 detail page: an employee opens an OPEN shift (claimable or not) and sees their own
    eligibility, reasons and claim — never the claims of others, their checks or who released it; a cancelled shift
    they never claimed is not found; planners covering it still get every claim with its ranking."""
    _, pack = _packing(app, plant)
    here = _post(plant["admin"], plant, plant["branch"]["id"]).json()["id"]
    there = _post(plant["admin"], plant, pack["id"]).json()["id"]
    mohan, sita = plant["people"]["Mohan"], plant["people"]["Sita"]
    claim = sita.post(f"/api/v1/open-shifts/{here}/claim", json={"reason": "Saving up for a trip"})
    assert claim.status_code == 201, claim.text

    seen = mohan.get(f"/api/v1/open-shifts/{here}")
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body["can_claim"] is True and body["my_claim"] is None and "claims" not in body
    assert body["released_by_employee_id"] is None and "Saving up" not in seen.text
    assert body["branch_name"] == plant["branch"]["name"]  # claimants cannot read the branch list (FU-AS-56 b)
    listed = next(i for i in mohan.get("/api/v1/open-shifts").json()["items"] if i["id"] == here)
    assert listed["branch_name"] == plant["branch"]["name"]
    mine = sita.get(f"/api/v1/open-shifts/{here}").json()
    assert mine["my_claim"]["id"] == claim.json()["id"] and mine["can_claim"] is False
    other = mohan.get(f"/api/v1/open-shifts/{there}").json()
    assert other["can_claim"] is False and other["reasons"]
    assert plant["admin"].post(f"/api/v1/open-shifts/{there}/cancel").status_code == 200
    assert mohan.get(f"/api/v1/open-shifts/{there}").status_code == 404
    full = plant["admin"].get(f"/api/v1/open-shifts/{here}").json()
    assert [c["id"] for c in full["claims"]] == [claim.json()["id"]] and "ranking" in full["claims"][0]


def test_open_shifts_of_another_company_are_not_found_and_foreign_references_are_refused(app, plant):
    posted = _post(plant["admin"], plant, plant["branch"]["id"]).json()["id"]
    other = signup_company(app, "Other Open Shift Co")
    assert other.get(f"/api/v1/open-shifts/{posted}").status_code == 404
    assert other.post(f"/api/v1/open-shifts/{posted}/cancel").status_code == 404
    assert posted not in {s["id"] for s in other.get("/api/v1/open-shifts").json().get("items", [])}
    for field in ("team_id", "site_id"):
        r = _post(plant["admin"], plant, plant["branch"]["id"], **{field: 987654})
        assert r.status_code == 404, (field, r.text)  # unknown in this company: 404, never a database error


def test_planners_edit_open_shifts_they_cover_within_the_documented_limits(app, plant):
    """FU-AS-50 / §6.3 Edit: while OPEN; count up or down to filled; expiry earlier only; terms only before claims."""
    planner, _ = _packing(app, plant)
    admin = plant["admin"]
    shift = _post(admin, plant, plant["branch"]["id"], required_count=3, approval_required=False).json()
    url = f"/api/v1/open-shifts/{shift['id']}"
    assert planner.patch(url, json={"row_version": shift["row_version"], "required_count": 2}).status_code == 404
    r = admin.patch(url, json={"row_version": shift["row_version"] + 5, "required_count": 2})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT"
    r = admin.patch(url, json={"row_version": shift["row_version"], "required_tags": [], "note": "Saturday cover"})
    assert r.status_code == 200, r.text
    version = r.json()["row_version"]
    expires = datetime.fromisoformat(r.json()["expires_at"])
    for bad in (expires + timedelta(hours=1), datetime.fromisoformat(shift["posted_at"])):  # later / in the past
        r = admin.patch(url, json={"row_version": version, "expires_at": bad.isoformat()})
        assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR", r.text
    r = admin.patch(url, json={"row_version": version, "expires_at": (expires - timedelta(hours=1)).isoformat()})
    assert r.status_code == 200 and datetime.fromisoformat(r.json()["expires_at"]) == expires - timedelta(hours=1)
    # Mohan claims (no approval): one place filled; the terms are now frozen and the count cannot drop below 1.
    assert plant["people"]["Mohan"].post(f"{url}/claim", json={}).status_code == 201
    state = admin.get(url).json()
    assert state["filled_count"] == 1
    r = admin.patch(url, json={"row_version": state["row_version"], "shift_id": plant["shifts"]["LATE"]})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION", r.text
    r = admin.patch(url, json={"row_version": state["row_version"], "required_count": 0})
    assert r.status_code == 422  # schema floor
    # Lowering the count to the filled count fills the shift.
    r = admin.patch(url, json={"row_version": state["row_version"], "required_count": 1})
    assert r.status_code == 200 and r.json()["status"] == "FILLED", r.text
    r = admin.patch(url, json={"row_version": r.json()["row_version"], "required_count": 2})
    assert r.status_code == 409 and r.json()["details"]["current_status"] == "FILLED"
    assert rows("SELECT count(*) AS n FROM audit_logs WHERE action = 'OPEN_SHIFT_EDITED' AND entity_id = :i",
                i=str(shift["id"]))[0]["n"] == 3


def test_a_branch_owned_shift_is_offered_only_at_its_own_branch(app, plant):
    """§6.3 Validation (SP-AS-163): the shift's owner scope must contain the open shift's branch."""
    _, pack = _packing(app, plant)
    admin = plant["admin"]
    r = admin.post("/api/v1/shifts", json={"code": "PACK-E", "name": "Packing early", "kind": "FIXED",
                                           "branch_id": pack["id"], "version": {
                                               "effective_from": "2024-01-01", "reason": "test",
                                               "start_time": "06:00", "end_time": "14:00"}})
    assert r.status_code == 201, r.text
    owned = r.json()["id"]
    r = _post(admin, plant, plant["branch"]["id"], shift_id=owned)
    assert r.status_code == 422 and r.json()["details"]["reasons"] == ["SHIFT_SCOPE"], r.text
    r = _post(admin, plant, pack["id"], shift_id=owned)
    assert r.status_code == 201, r.text


def test_shift_lists_support_saved_views_with_their_own_allow_lists(app, plant):
    """FU-AS-51 (g) / X-M3: shift.shifts, shift.rules and roster.open_shifts are registered lists — the catalogue's
    views no longer answer 422 INVALID_LIST_KEY; a view stores only the list's allow-listed filters; an employee gets
    the "Shifts I can take" system view; another company never sees a view."""
    admin = plant["admin"]
    for key in ("shift.shifts", "shift.rules", "roster.open_shifts"):
        r = admin.get("/api/v1/saved-views", params={"list_key": key})
        assert r.status_code == 200, (key, r.text)
    made = admin.post("/api/v1/saved-views", json={"list_key": "roster.open_shifts", "name": "Open, everyone",
                                                   "query": {"status": "OPEN", "eligible_only": False},
                                                   "is_shared": True})
    assert made.status_code == 201, made.text
    bad = admin.post("/api/v1/saved-views", json={"list_key": "roster.open_shifts", "name": "Bad",
                                                  "query": {"employee_id": 3}})
    assert bad.status_code == 422 and bad.json()["code"] == "INVALID_QUERY", bad.text
    shifts = admin.post("/api/v1/saved-views", json={"list_key": "shift.shifts", "name": "Fixed by name",
                                                     "query": {"kind": "FIXED", "sort": "name", "columns": ["net"]}})
    assert shifts.status_code == 201, shifts.text
    mine = plant["people"]["Ravi"].get("/api/v1/saved-views", params={"list_key": "roster.open_shifts"}).json()
    assert "sys:available_to_me" in {v["id"] for v in mine["system_views"]}
    other = signup_company(app, "Other Views Co")
    theirs = other.get("/api/v1/saved-views", params={"list_key": "roster.open_shifts"}).json()
    assert made.json()["id"] not in {v["id"] for v in theirs["views"]}
    assert other.patch(f"/api/v1/saved-views/{made.json()['id']}", json={"name": "Mine now"}).status_code == 404
