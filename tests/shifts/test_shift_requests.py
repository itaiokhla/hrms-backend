"""Shift changes, swaps and open shifts (AS-M6, G-15; 03-shifts/03).

Week (published, Monday two weeks ahead): Ravi EARLY Mon–Fri; Sita LATE Mon, OFF Tue, EARLY Wed–Thu, OFF Fri;
Mohan OFF all week. SCHEDULING_POLICY: swap (approval), drop (no approval, PAID_OFF), release (no approval,
PAID_OFF), pickups need approval, change on; ``open_shift_eligibility.same_team`` off.

* SWAP Fri: Sita accepts, HR approves -> one REQUEST publication; both cells exchanged (source SWAP, version 2,
  ``request_id``), change rows ``initiated_by = EMPLOYEE``.
* SWAP Mon: Ravi would work LATE Mon then EARLY Tue -> 422 INSUFFICIENT_REST_HOURS at submit.
* DROP Thu to Sita (she works EARLY Thu): the overlap is hers -> the requester sees only PEER_NOT_AVAILABLE.
* DROP Tue to Mohan: accepted -> applied at once (no approval): Ravi OFF, Mohan EARLY.
* RELEASE Wed -> open shift RELEASED at once; Mohan claims -> PENDING_APPROVAL; HR approves -> FILLED, Ravi OFF Wed.
* CHANGE Fri to LATE -> approval -> Ravi LATE Fri (source CHANGE).
* A planner's publication on a requested cell SUPERSEDES the open request; a second open request on one entry is
  409 SHIFT_REQUEST_OPEN; decline / withdraw; the requester never approves; another tenant gets 404; v1 -> 409.
* Open shift with a required tag: claiming without the tag needs the TAG_MISSING acknowledgement; APPROVED
  UNAVAILABLE availability warns UNAVAILABLE; expiry sweeps; leave applied afterwards cancels a pending release.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytz

from tests.attendance.capture_v2_helpers import rows, sql
from tests.attendance_helpers import employee_with_login
from tests.helpers import signup_company

IST = pytz.timezone("Asia/Kolkata")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


def _monday_after(days: int = 14) -> date:
    day = _today() + timedelta(days=days)
    return day - timedelta(days=day.weekday())


def enable(company_id: int, key: str) -> None:
    updated = sql("UPDATE setting_values SET value = 'true'::jsonb WHERE company_id = :c AND key = :k "
                  "AND scope_type = 'TENANT' AND status = 'ACTIVE'", c=company_id, k=key)
    if updated.rowcount == 0:
        sql("INSERT INTO setting_values (company_id, key, scope_type, value, locked, effective_from, status, "
            "row_version, source, created_at, updated_at) VALUES (:c, :k, 'TENANT', 'true'::jsonb, false, "
            "'2024-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id, k=key)


POLICY = {
    "schedule_source": "ROSTER",
    "requests": {"swap": {"enabled": True, "cutoff_hours": 24, "approval_required": True},
                 "drop": {"enabled": True, "cutoff_hours": 24, "approval_required": False, "pay_effect": "PAID_OFF"},
                 "release": {"enabled": True, "cutoff_hours": 48, "approval_required": False,
                             "pay_effect": "PAID_OFF"},
                 "pickup_approval_required": True, "change_enabled": True, "change_cutoff_hours": 48},
    "open_shift_eligibility": {"same_team": False, "same_branch": True, "priority": "NONE"},
    "availability": {"enabled": True, "change_notice_days": 0, "approval_required": True},
}


def _scheduling_policy(admin, fields=POLICY, name="Plant schedules"):
    r = admin.post("/api/v1/attendance/policies", json={"type": "SCHEDULING_POLICY", "name": name, "fields": fields})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = admin.put(f"/api/v1/attendance/policies/{pid}/assignments",
                  json={"rules": [{"criteria": {"everyone": True}, "priority": 10}]})
    assert r.status_code == 200, r.text
    r = admin.post(f"/api/v1/attendance/policies/{pid}/versions/1/activate",
                   json={"effective_from": _today().isoformat(), "reason": "AS-M6 test"})
    assert r.status_code == 200, r.text
    return pid


@pytest.fixture
def plant(app):
    from tests.attendance.test_attendance_days import enable_v2

    admin = signup_company(app, "Swap Works")
    cid = admin.company_id
    enable_v2(cid)
    enable(cid, "shift.requests.enabled")
    enable(cid, "shift.open_shifts.enabled")
    branch = admin.get("/api/v1/branches").json()[0]
    shifts = {}
    for code, start, end in (("EARLY", "06:00", "14:00"), ("LATE", "14:00", "22:00")):
        r = admin.post("/api/v1/shifts", json={"code": code, "name": code.title(), "kind": "FIXED", "version": {
            "effective_from": "2024-01-01", "reason": "test", "start_time": start, "end_time": end}})
        assert r.status_code == 201, r.text
        shifts[code] = r.json()["id"]
    people = {name: employee_with_login(app, admin, name, branch_id=branch["id"]) for name in ("Ravi", "Sita", "Mohan")}
    ids = {name: c.me()["employee"]["id"] for name, c in people.items()}
    _scheduling_policy(admin)
    start = _monday_after()
    r = admin.post("/api/v1/rosters", json={"code": "RP-SWAP-1", "name": "Plant week", "branch_id": branch["id"],
                                            "start_date": start.isoformat(),
                                            "end_date": (start + timedelta(days=6)).isoformat()})
    assert r.status_code == 201, r.text
    period = r.json()
    plan = {"Ravi": ["EARLY"] * 5 + ["OFF", "OFF"], "Sita": ["LATE", "OFF", "EARLY", "EARLY", "OFF", "OFF", "OFF"],
            "Mohan": ["OFF"] * 7}
    cells = []
    for name, week in plan.items():
        for i, code in enumerate(week):
            cell = {"employee_id": ids[name], "shift_date": (start + timedelta(days=i)).isoformat(),
                    "kind": "OFF" if code == "OFF" else "WORK"}
            if code != "OFF":
                cell["shift_id"] = shifts[code]
            cells.append(cell)
    r = admin.put(f"/api/v1/rosters/{period['id']}/drafts", json={"cells": cells, "row_version": period["row_version"]})
    assert r.status_code == 200, r.text
    dry = admin.post(f"/api/v1/rosters/{period['id']}/publish", json={"dry_run": True}).json()
    assert dry["blocking"] == [], dry["blocking"]
    warns = sorted({x["rule_key"] for x in dry["results"] if x["severity"] == "WARN"})
    reasons = {x["rule_key"]: "Planned with the crew" for x in dry["results"] if x["severity"] == "REQUIRE_REASON"}
    r = admin.post(f"/api/v1/rosters/{period['id']}/publish", json={"acknowledged_warnings": warns, "reasons": reasons})
    assert r.status_code == 200, r.text
    return {"admin": admin, "cid": cid, "branch": branch, "shifts": shifts, "start": start, "ids": ids,
            "people": people, "period": period}


def entry(plant, name: str, offset: int) -> dict:
    found = rows("SELECT * FROM roster_entries WHERE employee_id = :e AND shift_date = :d AND withdrawn_at IS NULL "
                 "ORDER BY entry_seq", e=plant["ids"][name], d=plant["start"] + timedelta(days=offset))
    assert found, (name, offset)
    return found[0]


def submit(plant, who: str, body: dict, status: int = 201) -> dict:
    r = plant["people"][who].post("/api/v1/shift-requests", json=body)
    assert r.status_code == status, r.text
    return r.json()


def test_swap_accepted_and_approved_is_one_request_publication(plant):
    fri_ravi, fri_sita = entry(plant, "Ravi", 4), entry(plant, "Sita", 4)
    preview = plant["people"]["Ravi"].post("/api/v1/shift-requests/preview",
                                           json={"kind": "SWAP", "entry_id": fri_ravi["id"]}).json()
    sita = next(c for c in preview["coworkers"] if c["employee_id"] == plant["ids"]["Sita"])
    assert sita["eligible"] is True
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri_ravi["id"], "target_employee_id": plant["ids"]["Sita"],
                                 "reason": "Family function"})
    assert req["status"] == "PENDING_ACCEPTANCE" and req["approval_required"] is True
    incoming = plant["people"]["Sita"].get("/api/v1/shift-requests", params={"box": "incoming"}).json()["items"]
    assert [i["id"] for i in incoming] == [req["id"]] and incoming[0]["can"]["accept"] is True
    # The requester cannot approve nor accept their own request.
    assert plant["people"]["Ravi"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={}).status_code == 404
    r = plant["people"]["Sita"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={})
    assert r.status_code == 200 and r.json()["status"] == "PENDING_APPROVAL", r.text
    # Until approved nothing changed (§4.5 responsibility).
    assert entry(plant, "Ravi", 4)["version"] == 1
    approvals = plant["admin"].get("/api/v1/shift-requests", params={"box": "approvals"}).json()["items"]
    assert req["id"] in [a["id"] for a in approvals]
    r = plant["admin"].post(f"/api/v1/shift-requests/{req['id']}/approve", json={"note": "OK"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    after_ravi, after_sita = entry(plant, "Ravi", 4), entry(plant, "Sita", 4)
    assert after_ravi["kind"] == "OFF" and after_ravi["source"] == "SWAP" and after_ravi["version"] == 2
    assert after_sita["kind"] == "WORK" and after_sita["shift_id"] == fri_ravi["shift_id"]
    assert after_ravi["request_id"] == req["id"] == after_sita["request_id"]
    pub = rows("SELECT kind FROM roster_publications WHERE id = :p", p=r.json()["applied_publication_id"])[0]
    assert pub["kind"] == "REQUEST"
    changes = rows("SELECT initiated_by, request_id FROM roster_entry_changes WHERE publication_id = :p",
                   p=r.json()["applied_publication_id"])
    assert len(changes) == 2 and all(c["initiated_by"] == "EMPLOYEE" and c["request_id"] == req["id"] for c in changes)
    # Applied requests do not move again.
    assert plant["admin"].post(f"/api/v1/shift-requests/{req['id']}/approve", json={}).status_code == 409


def test_swap_into_too_little_rest_is_blocked_and_peer_issues_are_masked(plant):
    mon = entry(plant, "Ravi", 0)
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={
        "kind": "SWAP", "entry_id": mon["id"], "target_employee_id": plant["ids"]["Sita"]})
    assert r.status_code == 422 and r.json()["code"] == "INSUFFICIENT_REST_HOURS", r.text
    thu = entry(plant, "Ravi", 3)  # Sita already works EARLY on Thursday: a drop to her overlaps HER shift
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={
        "kind": "DROP", "entry_id": thu["id"], "target_employee_id": plant["ids"]["Sita"]})
    assert r.status_code == 422, r.text
    issues = r.json().get("issues") or []
    assert [i["rule_key"] for i in issues] == ["PEER_NOT_AVAILABLE"], issues
    assert rows("SELECT count(*) AS n FROM shift_requests")[0]["n"] == 0


def test_drop_without_approval_applies_on_acceptance(plant):
    tue = entry(plant, "Ravi", 1)
    req = submit(plant, "Ravi", {"kind": "DROP", "entry_id": tue["id"], "target_employee_id": plant["ids"]["Mohan"],
                                 "reason": "Exam"})
    assert req["approval_required"] is False
    r = plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    assert entry(plant, "Ravi", 1)["kind"] == "OFF" and entry(plant, "Mohan", 1)["kind"] == "WORK"
    assert entry(plant, "Mohan", 1)["source"] == "DROP"


def test_release_then_pickup_fills_the_open_shift(plant):
    wed = entry(plant, "Ravi", 2)
    req = submit(plant, "Ravi", {"kind": "RELEASE", "entry_id": wed["id"], "reason": "Doctor"})
    assert req["status"] == "APPROVED"  # release without approval posts at once
    pool = plant["people"]["Mohan"].get("/api/v1/open-shifts", params={"from": plant["start"].isoformat()}).json()
    shift = next(s for s in pool["items"] if s["source"] == "RELEASED")
    assert shift["can_claim"] is True and shift["status"] == "OPEN"
    own = plant["people"]["Ravi"].get("/api/v1/open-shifts", params={"from": plant["start"].isoformat()}).json()
    assert next(s for s in own["items"] if s["id"] == shift["id"])["can_claim"] is False
    assert entry(plant, "Ravi", 2)["kind"] == "WORK"  # still responsible until a pickup is approved
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert r.status_code == 201 and r.json()["status"] == "PENDING_APPROVAL", r.text
    again = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert again.status_code == 409 and again.json()["code"] == "SHIFT_REQUEST_OPEN"
    r = plant["admin"].post(f"/api/v1/shift-requests/{r.json()['id']}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    assert entry(plant, "Ravi", 2)["kind"] == "OFF" and entry(plant, "Mohan", 2)["kind"] == "WORK"
    filled = rows("SELECT status, filled_count, filled_at FROM open_shifts WHERE id = :i", i=shift["id"])[0]
    assert filled["status"] == "FILLED" and filled["filled_count"] == 1 and filled["filled_at"] is not None
    late = plant["people"]["Sita"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert late.status_code == 409 and late.json()["code"] == "OPEN_SHIFT_FILLED"


def test_change_needs_approval_and_supersede_and_one_open_request_per_entry(plant):
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "CHANGE", "entry_id": fri["id"],
                                 "requested_change": {"shift_id": plant["shifts"]["LATE"]}, "reason": "Class"})
    assert req["status"] == "PENDING_APPROVAL" and req["requested_shift_code"] == "LATE"
    dup = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={
        "kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Sita"]})
    assert dup.status_code == 409 and dup.json()["code"] == "SHIFT_REQUEST_OPEN"
    r = plant["admin"].post(f"/api/v1/shift-requests/{req['id']}/approve", json={})
    assert r.status_code == 200, r.text
    after = entry(plant, "Ravi", 4)
    assert after["shift_id"] == plant["shifts"]["LATE"] and after["source"] == "CHANGE"
    # A pending request is superseded by a planner's publication on its cell.
    thu = entry(plant, "Ravi", 3)
    pending = submit(plant, "Ravi", {"kind": "CHANGE", "entry_id": thu["id"],
                                     "requested_change": {"custom_start_time": "07:00", "custom_end_time": "15:00"}})
    r = plant["admin"].post("/api/v1/rosters/direct-publish", json={
        "cells": [{"action": "UPSERT", "employee_id": plant["ids"]["Ravi"], "shift_date": thu["shift_date"].isoformat(),
                   "entry_seq": 1, "kind": "OFF"}], "reason_code": "PLANNER",
        "acknowledged_warnings": ["LINT_EARLY_START"], "reasons": {"NIGHT_WORK_CONSENT_MISSING": "Agreed with Ravi"}})
    assert r.status_code == 200, r.text
    assert rows("SELECT status, close_reason FROM shift_requests WHERE id = :i", i=pending["id"])[0] == {
        "status": "SUPERSEDED", "close_reason": "SCHEDULE_CHANGED"}


def test_decline_withdraw_reject_reason_and_scope(app, plant):
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Sita"]})
    r = plant["people"]["Sita"].post(f"/api/v1/shift-requests/{req['id']}/decline", json={"note": "Busy"})
    assert r.status_code == 200 and r.json()["status"] == "DECLINED"
    # Without a reason the late shift's night-work flag (REQUIRE_REASON) refuses the change.
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={
        "kind": "CHANGE", "entry_id": fri["id"], "requested_change": {"shift_id": plant["shifts"]["LATE"]}})
    assert r.status_code == 422 and r.json()["code"] == "ROSTER_REASON_REQUIRED", r.text
    again = submit(plant, "Ravi", {"kind": "CHANGE", "entry_id": fri["id"],
                                   "requested_change": {"shift_id": plant["shifts"]["LATE"]}, "reason": "Class"})
    r = plant["admin"].post(f"/api/v1/shift-requests/{again['id']}/reject", json={})
    assert r.status_code == 400 and r.json()["code"] == "REASON_REQUIRED"
    r = plant["people"]["Ravi"].post(f"/api/v1/shift-requests/{again['id']}/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    other = signup_company(app, "Other Swap Co")
    assert other.get(f"/api/v1/shift-requests/{again['id']}").status_code == 404
    assert other.post(f"/api/v1/shift-requests/{again['id']}/approve", json={}).status_code in (403, 404)


def test_open_shift_tags_availability_expiry_and_leave(plant):
    admin = plant["admin"]
    r = admin.post("/api/v1/skill-tags", json={"code": "FORKLIFT", "name": "Forklift licence"})
    assert r.status_code == 201, r.text
    sat = plant["start"] + timedelta(days=5)
    r = admin.post("/api/v1/open-shifts", json={"branch_id": plant["branch"]["id"], "shift_date": sat.isoformat(),
                                                "shift_id": plant["shifts"]["EARLY"], "required_count": 2,
                                                "required_tags": ["FORKLIFT"], "approval_required": False})
    assert r.status_code == 201, r.text
    shift = r.json()
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert r.status_code == 422 and r.json()["code"] == "WARNINGS_UNACKNOWLEDGED"
    r = admin.put(f"/api/v1/employees/{plant['ids']['Mohan']}/skill-tags", json={"tags": [{"code": "FORKLIFT"}]})
    assert r.status_code == 200, r.text
    # Mohan is unavailable on Saturdays (approved availability) -> a warning to acknowledge.
    r = plant["people"]["Mohan"].post("/api/v1/me/availability", json={"kind": "UNAVAILABLE", "weekday": 5,
                                                                       "start_date": _today().isoformat()})
    assert r.status_code == 201 and r.json()["status"] == "PENDING", r.text
    assert admin.post(f"/api/v1/availability/{r.json()['id']}/approve").status_code == 200
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert r.status_code == 422 and [i["rule_key"] for i in r.json()["issues"]] == ["UNAVAILABLE"]
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim",
                                      json={"acknowledged_warnings": ["UNAVAILABLE"]})
    assert r.status_code == 201 and r.json()["status"] == "APPROVED", r.text  # no approval: first claim applies
    state = rows("SELECT status, filled_count FROM open_shifts WHERE id = :i", i=shift["id"])[0]
    assert state == {"status": "OPEN", "filled_count": 1}
    # Expiry sweep: past the instance start the open shift expires.
    from app.database.db import SessionLocal, bind_tenant
    from app.services import shift_requests

    with SessionLocal() as db:
        bind_tenant(db, plant["cid"])
        result = shift_requests.expire_due(db, plant["cid"], now=datetime.now(timezone.utc) + timedelta(days=60))
        db.commit()
    assert result["open_shifts_expired"] == 1
    # Leave applied afterwards withdraws a pending swap of that date.
    fri = entry(plant, "Ravi", 4)
    pending = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Sita"]})
    with SessionLocal() as db:
        bind_tenant(db, plant["cid"])
        assert shift_requests.cancel_for_leave(db, plant["cid"], plant["ids"]["Ravi"], fri["shift_date"],
                                               fri["shift_date"]) == 1
        db.commit()
    assert rows("SELECT status, close_reason FROM shift_requests WHERE id = :i", i=pending["id"])[0] == {
        "status": "CANCELLED", "close_reason": "LEAVE_APPLIED"}


def test_acknowledge_and_v1_tenant(app, plant):
    mon = entry(plant, "Ravi", 0)
    r = plant["people"]["Ravi"].post(f"/api/v1/me/schedule/entries/{mon['id']}/acknowledge")
    assert r.status_code == 200 and r.json()["acknowledged_at"], r.text
    assert plant["people"]["Sita"].post(f"/api/v1/me/schedule/entries/{mon['id']}/acknowledge").status_code == 404
    v1 = signup_company(app, "Legacy Swap Co")
    r = v1.post("/api/v1/open-shifts", json={"branch_id": v1.get("/api/v1/branches").json()[0]["id"],
                                             "shift_date": (_today() + timedelta(days=5)).isoformat(),
                                             "custom_start_time": "09:00", "custom_end_time": "17:00"})
    assert r.status_code == 409 and r.json()["code"] == "ATTENDANCE_V2_REQUIRED"


def test_request_limit_per_month(plant):
    admin = plant["admin"]
    policy = rows("SELECT id FROM policies WHERE company_id = :c AND type = 'SCHEDULING_POLICY'", c=plant["cid"])[0]
    limited = {**POLICY, "requests": {**POLICY["requests"],
                                      "swap": {**POLICY["requests"]["swap"], "max_per_period": 1}}}
    r = admin.post(f"/api/v1/attendance/policies/{policy['id']}/versions",
                   json={"fields": limited, "effective_from": _today().isoformat()})
    assert r.status_code == 201, r.text
    r = admin.post(f"/api/v1/attendance/policies/{policy['id']}/versions/{r.json()['version']}/activate",
                   json={"effective_from": _today().isoformat(), "reason": "limit"})
    assert r.status_code == 200, r.text
    fri = entry(plant, "Ravi", 4)
    first = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Sita"]})
    plant["people"]["Ravi"].post(f"/api/v1/shift-requests/{first['id']}/withdraw")
    # A request withdrawn before acceptance does not count (§3.1); a second one is allowed, a third is not.
    second = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Sita"]})
    plant["people"]["Sita"].post(f"/api/v1/shift-requests/{second['id']}/decline", json={})
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={
        "kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Sita"]})
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_LIMIT_REACHED", r.text


def test_settings_switches_and_policy_round_trip(app):
    from tests.attendance.test_attendance_days import enable_v2

    admin = signup_company(app, "Settings Swap Co")
    enable_v2(admin.company_id)
    before = admin.get("/api/v1/shift-requests/settings")
    assert before.status_code == 200, before.text
    assert before.json()["requests_enabled"] is False and before.json()["can_manage"] is True
    body = before.json()
    body["requests"]["swap"]["enabled"] = True
    body["requests"]["swap"]["cutoff_hours"] = 12
    r = admin.put("/api/v1/shift-requests/settings", json={
        "requests_enabled": True, "open_shifts_enabled": True, "requests": body["requests"],
        "open_shift_eligibility": {**body["open_shift_eligibility"], "same_team": False},
        "availability": body["availability"], "acknowledge_published_shifts": True, "reason": "Pilot"})
    assert r.status_code == 200, r.text
    after = r.json()
    assert after["requests_enabled"] is True and after["open_shifts_enabled"] is True
    assert {k: after["requests"]["swap"][k] for k in ("enabled", "cutoff_hours", "approval_required")} == {
        "enabled": True, "cutoff_hours": 12, "approval_required": True}
    assert after["open_shift_eligibility"]["same_team"] is False and after["policy_id"]
    # A second save versions the same policy.
    r = admin.put("/api/v1/shift-requests/settings", json={"acknowledge_published_shifts": False})
    assert r.status_code == 200 and r.json()["policy_id"] == after["policy_id"], r.text
    assert r.json()["acknowledge_published_shifts"] is False and r.json()["requests"]["swap"]["enabled"] is True
    employee = employee_with_login(app, admin, "Asha")
    assert employee.put("/api/v1/shift-requests/settings", json={"requests_enabled": False}).status_code == 403
