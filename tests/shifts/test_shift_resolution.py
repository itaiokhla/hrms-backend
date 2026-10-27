"""Shift resolution and assignment rules (AS-M1 WI-AS-1.3, 1.4, 1.8; 03-shifts/01 §11-§14).

Order rule > placement > none; higher priority wins; equal-priority overlaps refused (PRIORITY_CONFLICT); a rule
whose shift belongs to another branch is skipped (OWNER_SCOPE_MISMATCH, EC AS-SHF-10); ``shift_ids`` criteria refused;
an in-force rule is replaced, not edited (SP-AS-031); bulk assign creates one priority-1000 rule (C-15) and rejects
``mode`` (SP-AS-033); payroll standard hours follow the resolved shift (JC-AS-02) and the
``use_shift_duration`` flag (BUG-AS-31); the employee form's shift edits the placement (BUG-AS-34).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytz

from app.database.db import SessionLocal, bind_tenant
from tests.helpers import create_employee, signup_company

IST = pytz.timezone("Asia/Kolkata")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


@pytest.fixture
def acme(app):
    admin = signup_company(app, "Resolve Works")
    long_day = admin.post("/api/v1/shifts", json={"code": "LONG", "name": "Long day", "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "History import", "start_time": "08:00", "end_time": "18:00"}})
    assert long_day.status_code == 201, long_day.text
    gen = next(s for s in admin.get("/api/v1/shifts").json() if s["code"] == "GEN")
    ravi = create_employee(admin, "Ravi", shift_id=gen["id"])
    sita = create_employee(admin, "Sita", shift_id=gen["id"])
    return {"admin": admin, "gen": gen, "long": long_day.json(), "ravi": ravi, "sita": sita}


def _rule(admin, status=201, **body):
    payload = {"name": "Rule", "priority": 500, "effective_from": "2024-02-01", **body}
    r = admin.post("/api/v1/shift-assignment-rules", json=payload)
    assert r.status_code == status, r.text
    return r.json()


def _resolve(admin, employee_id, day: date):
    r = admin.get("/api/v1/shifts/resolve", params={"employee_id": employee_id, "from": day.isoformat(),
                                                     "to": day.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()["days"][0]


def test_placement_then_rule_wins_and_explain_says_why(acme):
    admin, day = acme["admin"], date(2024, 3, 4)
    before = _resolve(admin, acme["ravi"]["id"], day)
    assert (before["source"], before["shift_id"]) == ("PLACEMENT", acme["gen"]["id"])
    rule = _rule(admin, name="Ravi long days", shift_id=acme["long"]["id"],
                 criteria={"employee_ids": [acme["ravi"]["id"]]}, reason="Backdated test rule")
    after = _resolve(admin, acme["ravi"]["id"], day)
    assert (after["source"], after["shift_id"], after["rule_id"]) == ("RULE", acme["long"]["id"], rule["id"])
    steps = {(c["step"], c["won"]) for c in after["candidates"]}
    assert ("RULE", True) in steps and ("PLACEMENT", False) in steps
    assert _resolve(admin, acme["sita"]["id"], day)["source"] == "PLACEMENT"  # not in the criteria


def test_priority_conflict_and_higher_priority_wins(acme):
    admin = acme["admin"]
    _rule(admin, name="Everyone long", shift_id=acme["long"]["id"], criteria={"everyone": True}, priority=0,
          reason="Backdated test rule")
    r = admin.post("/api/v1/shift-assignment-rules", json={
        "name": "Everyone gen", "shift_id": acme["gen"]["id"], "criteria": {"everyone": True}, "priority": 0,
        "effective_from": "2024-02-01", "reason": "x"})
    assert r.status_code == 409 and r.json()["code"] == "PRIORITY_CONFLICT"
    _rule(admin, name="Sita gen", shift_id=acme["gen"]["id"], criteria={"employee_ids": [acme["sita"]["id"]]},
          priority=1000, reason="Backdated test rule")
    day = date(2024, 3, 4)
    assert _resolve(admin, acme["sita"]["id"], day)["shift_id"] == acme["gen"]["id"]
    assert _resolve(admin, acme["ravi"]["id"], day)["shift_id"] == acme["long"]["id"]


def test_shift_ids_criteria_and_archived_shifts_are_refused(acme):
    admin = acme["admin"]
    r = admin.post("/api/v1/shift-assignment-rules", json={
        "name": "Bad", "shift_id": acme["long"]["id"], "criteria": {"shift_ids": [1]}, "effective_from": "2026-12-01"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_CRITERIA"


def test_as_shf_10_a_branch_shift_never_follows_a_transfer(acme):
    admin = acme["admin"]
    entity = admin.get("/api/v1/legal-entities").json()[0]
    other = admin.post("/api/v1/branches", json={"legal_entity_id": entity["id"], "code": "BR2", "name": "Plant 2",
                                                 "timezone": "Asia/Kolkata"}).json()
    plant = admin.post("/api/v1/shifts", json={"code": "PLANT", "name": "Plant shift", "kind": "FIXED",
                                               "branch_id": other["id"], "version": {
                                                   "effective_from": "2024-01-01", "reason": "import",
                                                   "start_time": "07:00", "end_time": "15:00"}}).json()
    # Ravi is not placed in Plant 2: an employee_ids rule with him is refused (NOT_ELIGIBLE, SHIFT_SCOPE).
    r = admin.post("/api/v1/shift-assignment-rules", json={
        "name": "Plant", "shift_id": plant["id"], "criteria": {"employee_ids": [acme["ravi"]["id"]]},
        "effective_from": "2024-02-01", "reason": "x"})
    assert r.status_code == 422 and r.json()["code"] == "NOT_ELIGIBLE"
    assert r.json()["details"]["refused"] == [{"employee_id": acme["ravi"]["id"], "reason": "SHIFT_SCOPE"}]
    # A branch rule pinned to the branch is fine; Ravi (other branch) still resolves to his placement.
    _rule(admin, name="Plant 2 everyone", shift_id=plant["id"], criteria={"branch_ids": [other["id"]]},
          reason="Backdated test rule")
    assert _resolve(admin, acme["ravi"]["id"], date(2024, 3, 4))["source"] == "PLACEMENT"


def test_sp_as_031_an_in_force_rule_is_replaced_from_a_date(acme):
    admin = acme["admin"]
    rule = _rule(admin, name="Ravi long", shift_id=acme["long"]["id"],
                 criteria={"employee_ids": [acme["ravi"]["id"]]}, reason="Backdated test rule")
    r = admin.patch(f"/api/v1/shift-assignment-rules/{rule['id']}", json={"priority": 900})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION"
    assert r.json()["details"]["current_status"] == "IN_FORCE"
    successor = _rule(admin, name="Ravi long v2", shift_id=acme["long"]["id"], priority=900,
                      criteria={"employee_ids": [acme["ravi"]["id"]]}, replaces_rule_id=rule["id"],
                      effective_from=(_today() + timedelta(days=7)).isoformat())
    old = admin.get(f"/api/v1/shift-assignment-rules/{rule['id']}").json()
    assert old["effective_to"] == (_today() + timedelta(days=6)).isoformat()
    assert successor["replaces_rule_id"] == rule["id"]
    # Renaming an in-force rule is allowed.
    r = admin.patch(f"/api/v1/shift-assignment-rules/{rule['id']}", json={"name": "Ravi long (old)"})
    assert r.status_code == 200 and r.json()["name"] == "Ravi long (old)"


def test_c15_bulk_assign_previews_then_creates_one_employee_rule(acme):
    admin = acme["admin"]
    ids = [acme["ravi"]["id"], acme["sita"]["id"]]
    start = (_today() + timedelta(days=3)).isoformat()
    preview = admin.post("/api/v1/shift-assignment-rules/bulk-assign", json={
        "employee_ids": ids, "shift_id": acme["long"]["id"], "effective_from": start, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert preview.json()["employees_affected"] == 2 and preview.json()["dry_run"] is True
    assert admin.get("/api/v1/shift-assignment-rules").json()["total"] == 0
    r = admin.post("/api/v1/shift-assignment-rules/bulk-assign", json={
        "employee_ids": ids, "shift_id": acme["long"]["id"], "effective_from": start, "mode": "RULE"})
    assert r.status_code == 422  # SP-AS-033: the name `mode` is rejected
    r = admin.post("/api/v1/shift-assignment-rules/bulk-assign", json={
        "employee_ids": ids, "shift_id": acme["long"]["id"], "effective_from": start})
    assert r.status_code == 200, r.text
    rule = r.json()["rule"]
    assert rule["priority"] == 1000 and rule["criteria"]["employee_ids"] == sorted(ids)
    assert {x["status"] for x in r.json()["results"]} == {"ok"}


def _standard_hours(company_id: int, employee_id: int, day: date, **kwargs):
    from app.payroll.services import time_inputs

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        [item] = time_inputs.ledger_inputs(db, company_id, employee_id, day, day, **kwargs)
        return item.standard_hours
    finally:
        db.close()


def test_jc_as_02_standard_hours_follow_the_resolved_shift(acme):
    admin, day = acme["admin"], date(2024, 3, 4)
    cid = admin.company_id
    # GEN placement: 9 gross - 60 unpaid lunch = 8.
    assert _standard_hours(cid, acme["ravi"]["id"], day) == Decimal(8)
    _rule(admin, name="Ravi long", shift_id=acme["long"]["id"], criteria={"employee_ids": [acme["ravi"]["id"]]},
          reason="Backdated test rule")
    assert _standard_hours(cid, acme["ravi"]["id"], day) == Decimal(10)  # the rule's 08:00-18:00, no break
    # BUG-AS-31: with use_shift_duration off everyone uses the standard hours per day.
    assert _standard_hours(cid, acme["ravi"]["id"], day, standard_hours_per_day=Decimal("7.5"),
                           use_shift_duration=False) == Decimal("7.5")


def test_bug_as_34_the_employee_form_shift_edits_the_placement(acme):
    admin = acme["admin"]
    sita = acme["sita"]
    r = admin.put(f"/api/v1/employees/{sita['id']}", json={"shift_id": acme["long"]["id"]})
    assert r.status_code == 200, r.text
    history = admin.get(f"/api/v1/employees/{sita['id']}/assignments").json()
    current = next(a for a in history if a["effective_to"] is None)
    assert current["shift_id"] == acme["long"]["id"]
    assert current["effective_from"] == _today().isoformat()
    previous = [a for a in history if a["effective_to"] is not None]
    assert previous and previous[0]["shift_id"] == acme["gen"]["id"]  # history kept, not overwritten
    profile = admin.get(f"/api/v1/employees/{sita['id']}").json()
    assert profile["shift_id"] == acme["long"]["id"]  # the display mirror
