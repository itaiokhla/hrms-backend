"""FU-AS-54: what a plain employee sees of colleagues outside the roster grid (06-roles-permissions §7.2 V-7, V-10..V-12,
V-15; 07-security-privacy-compliance §2; 03-shifts/03 §4.2, §6.5, §10, §12; EC AS-SWP-06, GV-AS-SWP-17).

The viewers hold the default ``employee`` role only (``roster:request``, ``roster:read`` and ``shift:read`` at SELF) in
the AS-M6 ``plant`` of ``test_shift_requests``. A co-worker may see a colleague's **name and shift code** (V-10, the
swap picker) and, as the named party of a request, the other party's name, both shifts and the requester's note
(§10). Everything else stays with the colleague: free-text reasons of requests one is not a party to, who released or
claimed an open shift (V-7), availability and its note, skill tags (V-11), consent states and the protected group or
age a night-work / adolescent check reveals (V-12, 07 §2), leave (at most "Not available", V-15), the claimant
ranking inputs (§6.5, approvers only) and when a colleague acknowledged their shift. The roster grid itself is
IG-AS-144 (``test_three_company_isolation``).
"""

import json
from datetime import timedelta

from app.services import shift_requests as service
from tests.helpers import ApiClient
from tests.shifts.test_shift_requests import entry, plant, submit  # noqa: F401 - fixture

# Private free text of colleagues; none of it may reach a peer's screen.
AVAILABILITY_NOTE = "childcare-pickup-7731"
CHANGE_REASON = "hospital-visit-4410"
RELEASE_REASON = "doctor-letter-2208"
CLAIM_REASON = "second-job-5530"
SECRETS = (AVAILABILITY_NOTE, CHANGE_REASON, RELEASE_REASON, CLAIM_REASON)


def _warn_keys(client, body: dict) -> list:
    r = client.post("/api/v1/shift-requests/preview", json=body)
    assert r.status_code == 200, r.text
    return sorted({x["rule_key"] for x in r.json()["results"] if x.get("severity") == "WARN"})


def _of(checks: list, employee_id: int) -> list:
    return [c for c in checks if c.get("employee_id") == employee_id]


def _accept(client, request_id: int) -> dict:
    """The coworker accepts (acknowledging any warning of their own schedule the re-run raises)."""
    r = client.post(f"/api/v1/shift-requests/{request_id}/accept", json={})
    if r.status_code == 422 and r.json().get("code") == "WARNINGS_UNACKNOWLEDGED":
        keys = sorted({i["rule_key"] for i in r.json().get("issues") or []})
        r = client.post(f"/api/v1/shift-requests/{request_id}/accept", json={"acknowledged_warnings": keys})
    assert r.status_code == 200 and r.json()["status"] == "PENDING_APPROVAL", r.text
    return r.json()


def test_the_coworker_sees_the_requesters_name_shifts_and_note_but_not_the_requesters_checks(plant):
    """GV-AS-SWP-17 / §10 (EC AS-SWP-06, other direction): Mohan asks Sita to swap his Monday off for her LATE shift,
    which puts him on a night shift without a night-work consent. The check (NIGHT_WORK_CONSENT_MISSING, protected
    group in ``details``) is Mohan's: he and the approver see it, Sita — the coworker who must accept — sees his name,
    both shifts and his note, but neither his checks nor when he acknowledged his shift."""
    ids, mohan, sita, admin = plant["ids"], plant["people"]["Mohan"], plant["people"]["Sita"], plant["admin"]
    mon = entry(plant, "Mohan", 0)  # OFF; Sita works LATE 14:00-22:00 that Monday
    assert mohan.post(f"/api/v1/me/schedule/entries/{mon['id']}/acknowledge").status_code == 200
    body = {"kind": "SWAP", "entry_id": mon["id"], "target_employee_id": ids["Sita"], "reason": "Evening class moved"}
    body["acknowledged_warnings"] = _warn_keys(mohan, body)
    req = submit(plant, "Mohan", body)

    approver = admin.get(f"/api/v1/shift-requests/{req['id']}").json()
    mohans = _of(approver["checks"], ids["Mohan"])
    assert "NIGHT_WORK_CONSENT_MISSING" in {c["rule_key"] for c in mohans}, approver["checks"]  # approvers see all
    own = mohan.get(f"/api/v1/shift-requests/{req['id']}").json()
    assert _of(own["checks"], ids["Mohan"]) and own["entry"]["acknowledged_at"]  # his own data

    incoming = sita.get("/api/v1/shift-requests", params={"box": "incoming"}).json()["items"]
    detail = sita.get(f"/api/v1/shift-requests/{req['id']}")
    assert detail.status_code == 200, detail.text
    for item in (next(i for i in incoming if i["id"] == req["id"]), detail.json()):
        assert item["requester_name"] == "Mohan Test" and item["reason"] == "Evening class moved"
        assert item["entry"]["shift_date"] == mon["shift_date"].isoformat() and item["can"]["accept"] is True
        assert _of(item["checks"], ids["Mohan"]) == [], item["checks"]
        text = json.dumps(item["checks"])
        assert "NIGHT_WORK_CONSENT_MISSING" not in text and "WOMEN" not in text
        assert item["entry"]["acknowledged_at"] is None  # Mohan's entry: not "the shift" of 03 §10
    # Her answer comes back through the same serializer, now with the checks re-run for both of them.
    accepted = _accept(sita, req["id"])
    assert _of(accepted["checks"], ids["Mohan"]) == [] and "WOMEN" not in json.dumps(accepted["checks"])
    assert _of(admin.get(f"/api/v1/shift-requests/{req['id']}").json()["checks"], ids["Mohan"])
    assert _of(sita.get(f"/api/v1/shift-requests/{req['id']}").json()["checks"], ids["Mohan"]) == []


def test_the_requester_sees_one_neutral_item_for_the_coworkers_checks(plant):
    """EC AS-SWP-06 on the stored request: Sita asks Mohan to take her LATE Monday by a swap; the night-work check is
    Mohan's. Once Mohan has accepted (the checks run again for both and are stored whole for the approver), Sita's
    lists show one neutral PEER_NOT_AVAILABLE item in its place; Mohan sees his own check and nothing of Sita's."""
    ids, mohan, sita, admin = plant["ids"], plant["people"]["Mohan"], plant["people"]["Sita"], plant["admin"]
    mon = entry(plant, "Sita", 0)
    body = {"kind": "SWAP", "entry_id": mon["id"], "target_employee_id": ids["Mohan"], "reason": "Family dinner"}
    body["acknowledged_warnings"] = _warn_keys(sita, body)
    req = submit(plant, "Sita", body)
    _accept(mohan, req["id"])
    assert _of(admin.get(f"/api/v1/shift-requests/{req['id']}").json()["checks"], ids["Mohan"])

    mine = next(i for i in sita.get("/api/v1/shift-requests", params={"box": "mine"}).json()["items"]
                if i["id"] == req["id"])
    assert _of(mine["checks"], ids["Mohan"]) == [] and "WOMEN" not in json.dumps(mine["checks"])
    assert "PEER_NOT_AVAILABLE" in {c["rule_key"] for c in mine["checks"]}
    theirs = mohan.get(f"/api/v1/shift-requests/{req['id']}").json()
    assert _of(theirs["checks"], ids["Mohan"]) and _of(theirs["checks"], ids["Sita"]) == []
    assert "PEER_NOT_AVAILABLE" not in {c["rule_key"] for c in theirs["checks"]}


def test_the_swap_picker_shows_name_and_shift_code_only(plant, monkeypatch):
    """V-10 / EC AS-SWP-06: the picker row of a coworker is name and ``eligible`` with a neutral note; the coworker's
    shift (the cell) only when ``shift.roster.team_visibility`` is TEAM (default OFF) — never leave, reasons or when
    the coworker acknowledged their shift."""
    ids, sita = plant["ids"], plant["people"]["Sita"]
    real = service._on_leave
    monkeypatch.setattr(service, "_on_leave", lambda db, c, e, d: e == ids["Mohan"] or real(db, c, e, d))
    ravi_wed = entry(plant, "Ravi", 2)
    assert plant["people"]["Ravi"].post(f"/api/v1/me/schedule/entries/{ravi_wed['id']}/acknowledge").status_code == 200
    picker = {"kind": "SWAP", "entry_id": entry(plant, "Sita", 2)["id"]}

    def rows_of(r) -> dict:
        assert r.status_code == 200, r.text
        rows = {c["employee_id"]: c for c in r.json()["coworkers"]}
        assert {ids["Ravi"], ids["Mohan"]} <= set(rows)  # with the company's first employee (the admin's record)
        for row in rows.values():
            assert set(row) == {"employee_id", "name", "eligible", "cell", "note"}, row
        return rows

    # Default OFF: names, the eligible flag and the neutral note only.
    rows = rows_of(sita.post("/api/v1/shift-requests/preview", json=picker))
    assert all(row["cell"] is None for row in rows.values()), rows
    assert rows[ids["Ravi"]]["name"] == "Ravi Test" and rows[ids["Ravi"]]["eligible"] is True
    assert rows[ids["Mohan"]]["eligible"] is False and rows[ids["Mohan"]]["note"] == "Not available for this swap"
    # TEAM at tenant level: the co-workers' published shift codes appear.
    key = "shift.roster.team_visibility"
    current = plant["admin"].get("/api/v1/payroll/settings/values", params={"scope_type": "TENANT"})
    assert current.status_code == 200, current.text
    version = next(i for i in current.json()["items"] if i["key"] == key)["row_version"]
    s = plant["admin"].put("/api/v1/payroll/settings/values", json={"changes": [
        {"key": key, "value": "TEAM", "scope_type": "TENANT", "row_version": version, "reason": "swap picker"}]})
    assert s.status_code == 200 and s.json()["changes"][0]["status"] == "ACTIVE", s.text
    r = sita.post("/api/v1/shift-requests/preview", json=picker)
    rows = rows_of(r)
    ravi = rows[ids["Ravi"]]
    assert ravi["name"] == "Ravi Test" and ravi["eligible"] is True and ravi["cell"]["shift_code"] == "EARLY"
    assert ravi["cell"]["acknowledged_at"] is None, ravi["cell"]
    mohan = rows[ids["Mohan"]]
    assert mohan["eligible"] is False and mohan["note"] == "Not available for this swap"
    assert "LEAVE" not in r.text and "reasons" not in r.text


def test_the_open_shift_board_never_names_who_released_or_claimed(plant):
    """V-7 (request facts of colleagues: none) and §6.5 / §12.3: Ravi releases his Wednesday with a reason and Mohan
    claims it. Sita's board shows the shift, its places and a pending-claims count, but not who released it (the id
    resolves to a name through the picker) nor who claimed; the claim and its ranking inputs (week hours, tags,
    priority group) are approver views. Ravi still sees his own release (SP-AS-048 take back)."""
    ids, admin, ravi, mohan, sita = (plant["ids"], plant["admin"], plant["people"]["Ravi"], plant["people"]["Mohan"],
                                     plant["people"]["Sita"])
    released = submit(plant, "Ravi", {"kind": "RELEASE", "entry_id": entry(plant, "Ravi", 2)["id"],
                                      "reason": RELEASE_REASON})
    assert released["status"] == "APPROVED"
    params = {"from": plant["start"].isoformat(), "eligible_only": "false"}
    shift = next(s for s in admin.get("/api/v1/open-shifts", params=params).json()["items"] if s["source"] == "RELEASED")
    assert shift["released_by_employee_id"] == ids["Ravi"]  # planners see the source and the releaser
    claim = mohan.post(f"/api/v1/open-shifts/{shift['id']}/claim", json={"reason": CLAIM_REASON})
    assert claim.status_code == 201 and claim.json()["status"] == "PENDING_APPROVAL", claim.text

    board = sita.get("/api/v1/open-shifts", params=params)
    assert board.status_code == 200, board.text
    card = next(s for s in board.json()["items"] if s["id"] == shift["id"])
    assert card["released_by_employee_id"] is None, card
    assert card["pending_claims"] == 1 and not {"claims", "ranking", "claimant_ids"} & set(card)
    assert RELEASE_REASON not in board.text and CLAIM_REASON not in board.text
    # The detail (FU-AS-51 g) gives her her own fields only: the claims with week hours, tags and priority group stay
    # approver views, and the releaser stays unnamed.
    detail = sita.get(f"/api/v1/open-shifts/{shift['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert "claims" not in body and "ranking" not in body and body["released_by_employee_id"] is None, body
    assert body["my_claim"] is None and "can_claim" in body
    assert RELEASE_REASON not in detail.text and CLAIM_REASON not in detail.text
    assert sita.get(f"/api/v1/shift-requests/{claim.json()['id']}").status_code == 404
    assert sita.get(f"/api/v1/shift-requests/{released['id']}").status_code == 404

    own = next(s for s in ravi.get("/api/v1/open-shifts", params=params).json()["items"] if s["id"] == shift["id"])
    assert own["released_by_employee_id"] == ids["Ravi"] and own["can_claim"] is False
    full = admin.get(f"/api/v1/open-shifts/{shift['id']}").json()
    assert full["claims"][0]["ranking"]["week_hours_after"] >= 0 and full["claims"][0]["reason"] == CLAIM_REASON


def test_requests_availability_tags_schedules_feed_and_rotations_of_colleagues_stay_private(app, plant):
    """The rest of what a SELF employee can call: colleagues' requests (not a party: 404, never listed), availability
    and its note (V-11), skill tags (V-11: catalogue with holder counts, never who holds them), rotation assignments,
    another employee's resolved shifts, My schedule, the roster periods and the calendar feed (own schedule only)."""
    ids, admin, ravi, mohan, sita = (plant["ids"], plant["admin"], plant["people"]["Ravi"], plant["people"]["Mohan"],
                                     plant["people"]["Sita"])
    s, start = plant["shifts"], plant["start"]
    r = mohan.post("/api/v1/me/availability", json={"kind": "UNAVAILABLE", "weekday": 6, "note": AVAILABILITY_NOTE,
                                                    "start_date": (start + timedelta(days=21)).isoformat()})
    assert r.status_code == 201, r.text
    assert admin.post(f"/api/v1/availability/{r.json()['id']}/approve").status_code == 200
    assert admin.post("/api/v1/skill-tags", json={"code": "FORKLIFT", "name": "Forklift licence"}).status_code == 201
    assert admin.put(f"/api/v1/employees/{ids['Mohan']}/skill-tags",
                     json={"tags": [{"code": "FORKLIFT"}]}).status_code == 200
    change = submit(plant, "Ravi", {"kind": "CHANGE", "entry_id": entry(plant, "Ravi", 3)["id"], "reason": CHANGE_REASON,
                                    "requested_change": {"custom_start_time": "07:00", "custom_end_time": "15:00"}})
    pattern = admin.post("/api/v1/rotations", json={"code": "PRIV-ROT", "name": "Privacy rotation",
                                                    "template_key": "SIX_ON_TWO_OFF_SLOW", "version": {"token_map": {
                                                        t: {"shift_id": s["EARLY"]} for t in ("M", "A", "N")}}})
    assert pattern.status_code == 201, pattern.text
    assert admin.post(f"/api/v1/rotations/{pattern.json()['id']}/versions/1/activate").status_code == 200
    version = next(p for p in admin.get("/api/v1/rotations").json()
                   if p["id"] == pattern.json()["id"])["current_version_id"]
    later = (start + timedelta(days=60)).isoformat()
    r = admin.post("/api/v1/rotations/assignments", json={
        "pattern_version_id": version, "employee_ids": [ids["Mohan"]], "crew_code": "A", "offset_days": 0,
        "anchor_date": later, "effective_from": later})
    assert r.status_code == 201, r.text

    texts = []

    def call(method: str, path: str, status: int = 200, **kwargs):
        r = sita.request(method, f"/api/v1{path}", **kwargs)
        assert r.status_code == status, (path, r.status_code, r.text[:200])
        texts.append(r.text)
        return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else None

    week = {"from": start.isoformat(), "to": (start + timedelta(days=6)).isoformat()}
    schedule = call("GET", "/me/schedule", params=week)
    assert schedule["employee_id"] == ids["Sita"]
    assert {e["employee_id"] for d in schedule["days"] for e in d["entries"]} == {ids["Sita"]}
    periods = call("GET", "/rosters")["items"]
    assert [p["id"] for p in periods] == [plant["period"]["id"]]
    grid = call("GET", f"/rosters/{plant['period']['id']}")
    own_entries = {e["id"] for e in grid["entries"]}
    assert {e["employee_id"] for e in grid["entries"]} == {ids["Sita"]} and grid["drafts"] == []
    assert {c["roster_entry_id"] for c in grid["consent_conflicts"]} <= own_entries
    for box in ("mine", "incoming"):
        assert call("GET", "/shift-requests", params={"box": box})["items"] == []
    call("GET", "/shift-requests", 403, params={"box": "approvals"})
    call("GET", f"/shift-requests/{change['id']}", 404)
    assert call("GET", "/me/availability")["items"] == []
    call("GET", "/availability", 403)
    tags = call("GET", "/skill-tags")["items"]
    assert [(t["code"], t["holders"]) for t in tags] == [("FORKLIFT", 1)]
    assert all(set(t) == {"id", "code", "name", "status", "holders"} for t in tags)
    rotations = call("GET", "/rotations")
    assert pattern.json()["id"] in [p["id"] for p in rotations] and "employee_id" not in json.dumps(rotations)
    call("GET", "/shifts/resolve", 404, params={"employee_id": ids["Mohan"], **week})
    call("GET", "/shift-assignment-rules", 403)
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, secret

    feed = call("POST", "/me/calendar-feed")
    ics = ApiClient(app).get(feed["path"])
    assert ics.status_code == 200, ics.text
    assert "X-WR-CALNAME:Sita Test" in ics.text and "Ravi" not in ics.text and "Mohan" not in ics.text
    uids = [line for line in ics.text.splitlines() if line.startswith("UID:")]
    assert uids and all(u.startswith(f"UID:shift-{plant['cid']}-{ids['Sita']}-") for u in uids)
    assert ravi.get(f"/api/v1/shift-requests/{change['id']}").json()["reason"] == CHANGE_REASON  # the owner keeps it
