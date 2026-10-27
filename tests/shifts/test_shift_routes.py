"""Shift routes: permissions, legacy contract, tenancy (AS-M1 WI-AS-1.5, 1.6, 1.8; AS 11 §6.2).

BUG-AS-35 (``GET /shifts`` needs ``shift:read`` and hides archived shifts), BUG-AS-38 (HR administrators manage
shifts), employees see only their own shift and no rules (§15), another tenant's ids answer 404 (LH-SEC-01 pattern),
``roster:exempt`` is never partner-delegable (CV-6), the list envelope searches, filters and sorts (X 04).
"""

from datetime import date

import pytest

from tests.helpers import create_employee, invite_and_activate, signup_company


@pytest.fixture
def tenant(app):
    admin = signup_company(app, "Route Works")
    gen = next(s for s in admin.get("/api/v1/shifts").json() if s["code"] == "GEN")
    flex = admin.post("/api/v1/shifts", json={"code": "FLEX-BLR", "name": "Flexi Bengaluru", "kind": "FLEXIBLE",
                                              "version": {"start_time": "08:00", "end_time": "20:00",
                                                          "core_start_time": "11:00", "core_end_time": "16:00",
                                                          "expected_net_minutes": 480}})
    assert flex.status_code == 201, flex.text
    hr_person = create_employee(admin, "Kavya", "Nair", shift_id=gen["id"])
    worker = create_employee(admin, "Priya", "Shah", shift_id=gen["id"])
    return {"admin": admin, "gen": gen, "flex": flex.json(),
            "hr": invite_and_activate(app, admin, hr_person, ["hr_admin"]),
            "priya": invite_and_activate(app, admin, worker, ["employee"]), "worker": worker}


def test_bug_as_38_an_hr_administrator_manages_shifts(tenant):
    hr = tenant["hr"]
    r = hr.post("/api/v1/shifts", json={"code": "HR-DAY", "name": "HR day", "kind": "FIXED",
                                        "version": {"start_time": "09:30", "end_time": "18:30"}})
    assert r.status_code == 201, r.text
    r = hr.post("/api/v1/shift-assignment-rules/simulate", json={
        "name": "Sim", "shift_id": r.json()["id"], "criteria": {"everyone": True}, "priority": 0,
        "effective_from": "2026-12-01"})
    assert r.status_code == 200, r.text


def test_bug_as_35_employees_see_only_their_shift_and_no_rules(tenant):
    priya = tenant["priya"]
    listed = priya.get("/api/v1/shifts").json()
    assert [s["code"] for s in listed] == ["GEN"]
    assert priya.get(f"/api/v1/shifts/{tenant['flex']['id']}").status_code == 404
    assert priya.get("/api/v1/shift-assignment-rules").status_code == 403
    assert priya.post("/api/v1/shifts", json={"name": "Mine", "start_time": "09:00",
                                              "end_time": "17:00"}).status_code == 403
    day = date.today().isoformat()
    own = priya.get("/api/v1/shifts/resolve", params={"employee_id": tenant["worker"]["id"], "from": day, "to": day})
    assert own.status_code == 200 and own.json()["days"][0]["source"] == "PLACEMENT"
    other = priya.get("/api/v1/shifts/resolve", params={"employee_id": tenant["hr"].employee["id"], "from": day,
                                                         "to": day})
    assert other.status_code == 404


def test_the_list_envelope_searches_filters_and_sorts(tenant):
    admin = tenant["admin"]
    r = admin.get("/api/v1/shifts", params={"q": "flex", "kind": "FLEXIBLE", "sort": "-code", "limit": 25})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1 and body["items"][0]["code"] == "FLEX-BLR"
    assert body["items"][0]["current_version"]["core_start_time"] == "11:00"
    assert admin.get("/api/v1/shifts", params={"sort": "colour"}).json()["code"] == "INVALID_SORT"


def test_another_tenant_sees_404_for_every_shift_route(app, tenant):
    other = signup_company(app, "Other Works")
    sid = tenant["flex"]["id"]
    assert other.get(f"/api/v1/shifts/{sid}").status_code == 404
    assert other.get(f"/api/v1/shifts/{sid}/versions").status_code == 404
    assert other.patch(f"/api/v1/shifts/{sid}", json={"name": "Taken"}).status_code == 404
    assert other.post(f"/api/v1/shifts/{sid}/archive", json={}).status_code == 404
    assert other.post(f"/api/v1/shifts/{sid}/versions", json={"start_time": "08:00",
                                                              "end_time": "16:00"}).status_code == 404
    r = other.post("/api/v1/shift-assignment-rules", json={"name": "X", "shift_id": sid, "criteria": {"everyone": True},
                                                           "effective_from": "2026-12-01"})
    assert r.status_code == 404


def test_cv_6_roster_exempt_is_never_partner_delegable():
    from app.services.partner_delegation_service import NEVER_DELEGABLE

    assert "roster:exempt" in NEVER_DELEGABLE

