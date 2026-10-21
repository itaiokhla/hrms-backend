"""Holiday calendars per branch in the holiday service (3.4 §8; 02 §10, §19; audit 03 §5, §8.10).

EC-HOL-04, EC-HOL-06, EC-HOL-10. The location-based screens keep working (test_workflows, test_org_documents).
"""

from datetime import date

import pytest
from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from app.payroll.services import time_inputs
from tests.helpers import create_employee, invite_and_activate, signup_company

HOLIDAYS = "/api/v1/holidays"


def _sql(query, **params):
    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return [dict(r._mapping) for r in result] if result.returns_rows else None


def _holiday(client, name, day, **extra):
    r = client.post(HOLIDAYS, json={"name": name, "date": day, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _facts(company_id, employee_id, start, end):
    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        return time_inputs.day_facts(db, company_id, employee_id, start, end)
    finally:
        db.close()


@pytest.fixture
def acme(app):
    admin = signup_company(app, "Acme Corp")
    home = admin.get("/api/v1/branches").json()[0]
    delhi = admin.post("/api/v1/branches", json={"legal_entity_id": home["legal_entity_id"], "code": "DEL",
                                                 "name": "Delhi"}).json()
    chennai = admin.post("/api/v1/branches", json={"legal_entity_id": home["legal_entity_id"], "code": "CHN",
                                                   "name": "Chennai"}).json()
    return {"admin": admin, "cid": admin.company_id, "home": home, "delhi": delhi, "chennai": chennai}


def test_holidays_carry_their_calendar_and_type(acme):
    admin = acme["admin"]
    company = _holiday(admin, "Company Day", "2099-02-01")
    optional = _holiday(admin, "Choice Day", "2099-02-02", is_optional=True)
    regional = _holiday(admin, "Delhi Day", "2099-02-03", location="Delhi")
    calendars = {r["id"]: r for r in _sql("SELECT id, code, parent_calendar_id FROM holiday_calendars WHERE company_id = :c",
                                          c=acme["cid"])}
    assert calendars[company["calendar_id"]]["code"] == "COMPANY"
    assert company["holiday_type"] == "COMPANY" and optional["holiday_type"] == "OPTIONAL"
    delhi_calendar = calendars[regional["calendar_id"]]
    assert delhi_calendar["parent_calendar_id"] == company["calendar_id"]
    branch = _sql("SELECT holiday_calendar_id FROM branches WHERE id = :b", b=acme["delhi"]["id"])[0]
    assert branch["holiday_calendar_id"] == regional["calendar_id"]


def test_mine_follows_the_branch_calendar_lineage(acme, app):
    admin = acme["admin"]
    _holiday(admin, "Company Day", "2099-02-01")
    _holiday(admin, "Delhi Day", "2099-02-03", location="Delhi")
    _holiday(admin, "Chennai Day", "2099-02-04", location="Chennai")
    delhi_user = invite_and_activate(app, admin, create_employee(admin, "Dee", branch_id=acme["delhi"]["id"]), ["employee"])
    home_user = invite_and_activate(app, admin, create_employee(admin, "Hal"), ["employee"])
    mine = lambda c: [h["name"] for h in c.get(HOLIDAYS, params={"year": 2099, "mine": True}).json()]  # noqa: E731
    assert mine(delhi_user) == ["Company Day", "Delhi Day"]
    assert mine(home_user) == ["Company Day"]
    assert len(admin.get(HOLIDAYS, params={"year": 2099}).json()) == 3


def test_two_branches_may_observe_a_holiday_of_the_same_name(acme):
    admin = acme["admin"]
    _holiday(admin, "Pongal", "2099-01-14", location="Delhi")
    _holiday(admin, "Pongal", "2099-01-14", location="Chennai")
    r = admin.post(HOLIDAYS, json={"name": "pongal", "date": "2099-01-14", "location": "Chennai"})
    assert r.status_code == 409
    _holiday(admin, "Republic Day", "2099-01-26")
    assert admin.post(HOLIDAYS, json={"name": "Republic Day", "date": "2099-01-26"}).status_code == 409


def test_F_30_editing_the_location_moves_a_future_holiday_to_that_calendar(acme):
    admin = acme["admin"]
    h = _holiday(admin, "Regional Day", "2099-02-10", location="Delhi")
    chennai = _holiday(admin, "Chennai Day", "2099-02-11", location="Chennai")
    r = admin.put(f"{HOLIDAYS}/{h['id']}", json={"name": "Regional Day", "date": "2099-02-10", "location": "Chennai"})
    assert r.status_code == 200, r.text
    assert r.json()["calendar_id"] == chennai["calendar_id"] != h["calendar_id"]
    r = admin.put(f"{HOLIDAYS}/{h['id']}", json={"name": "Regional Day", "date": "2099-02-10"})
    company = _sql("SELECT id FROM holiday_calendars WHERE company_id = :c AND code = 'COMPANY'", c=acme["cid"])[0]
    assert r.json()["calendar_id"] == company["id"] and r.json()["location"] is None


def test_EC_HOL_06_past_holidays_keep_date_place_and_type(acme):
    admin = acme["admin"]
    past = _holiday(admin, "Independence Day", "2025-08-15")
    body = {"name": "Independence Day", "date": "2025-08-15"}
    r = admin.put(f"{HOLIDAYS}/{past['id']}", json={**body, "date": "2025-08-16"})
    assert r.status_code == 409 and r.json()["code"] == "HOLIDAY_IN_PAST"
    assert admin.put(f"{HOLIDAYS}/{past['id']}", json={**body, "is_optional": True}).status_code == 409
    assert admin.put(f"{HOLIDAYS}/{past['id']}", json={**body, "location": "Delhi"}).status_code == 409
    ok = admin.put(f"{HOLIDAYS}/{past['id']}", json={**body, "description": "National holiday"})
    assert ok.status_code == 200, ok.text
    future = _holiday(admin, "Moved Day", "2099-03-01")
    assert admin.put(f"{HOLIDAYS}/{future['id']}", json={"name": "Moved Day", "date": "2025-03-01"}).status_code == 409
    assert admin.delete(f"{HOLIDAYS}/{past['id']}").status_code == 409


def test_EC_HOL_06_every_holiday_change_reaches_the_change_log(acme):
    admin = acme["admin"]
    h = _holiday(admin, "Foundation Day", "2099-04-01")
    r = admin.put(f"{HOLIDAYS}/{h['id']}", json={"name": "Foundation Day", "date": "2099-04-02"})
    assert r.status_code == 200, r.text
    admin.put(f"{HOLIDAYS}/{h['id']}", json={"name": "Foundation Day", "date": "2099-04-02", "description": "same"})
    assert admin.delete(f"{HOLIDAYS}/{h['id']}").status_code == 200
    rows = _sql("SELECT change, date_from, date_to, employee_id, version_hash FROM source_change_log "
                "WHERE record_type = 'holiday' AND record_id = :h ORDER BY id", h=h["id"])
    assert [(r["change"], r["date_from"], r["date_to"]) for r in rows] == [
        ("CREATED", date(2099, 4, 1), date(2099, 4, 1)),
        ("UPDATED", date(2099, 4, 1), date(2099, 4, 2)),
        ("DELETED", date(2099, 4, 2), date(2099, 4, 2)),
    ]
    assert all(r["employee_id"] is None for r in rows)
    assert len({r["version_hash"] for r in rows}) == 3
    audit = _sql("SELECT details FROM audit_logs WHERE entity_type = 'holiday' AND entity_id = :h AND action = 'SAVE' "
                 "ORDER BY id", h=str(h["id"]))
    assert audit[1]["details"]["before"]["date"] == "2099-04-01"


def test_EC_HOL_04_transfer_mid_month_follows_the_calendar_of_each_date(acme):
    admin, cid = acme["admin"], acme["cid"]
    worker = create_employee(admin, "Tara")
    _holiday(admin, "Home Day", "2099-03-05", location=acme["home"]["name"])
    _holiday(admin, "Delhi Day", "2099-03-20", location="Delhi")
    _holiday(admin, "Late Home Day", "2099-03-25", location=acme["home"]["name"])
    _holiday(admin, "Company Day", "2099-03-26")
    r = admin.post(f"/api/v1/employees/{worker['id']}/assignments",
                   json={"branch_id": acme["delhi"]["id"], "effective_from": "2099-03-15"})
    assert r.status_code in (200, 201), r.text
    facts = {f.date: f for f in _facts(cid, worker["id"], date(2099, 3, 1), date(2099, 3, 31))}
    named = {d: f.holiday.name for d, f in facts.items() if f.holiday is not None}
    assert named == {date(2099, 3, 5): "Home Day", date(2099, 3, 20): "Delhi Day", date(2099, 3, 26): "Company Day"}
    assert facts[date(2099, 3, 14)].branch_id == acme["home"]["id"]
    assert facts[date(2099, 3, 15)].branch_id == acme["delhi"]["id"]


def test_EC_HOL_10_holiday_after_the_exit_date_is_not_an_employed_day(acme):
    admin, cid = acme["admin"], acme["cid"]
    worker = create_employee(admin, "Lee")
    _holiday(admin, "Diwali", "2026-10-20")
    r = admin.post(f"/api/v1/employees/{worker['id']}/termination", json={"last_working_day": "2026-10-09"})
    assert r.status_code == 200, r.text
    facts = {f.date: f for f in _facts(cid, worker["id"], date(2026, 10, 1), date(2026, 10, 31))}
    assert facts[date(2026, 10, 9)].employed and not facts[date(2026, 10, 10)].employed
    assert facts[date(2026, 10, 20)].holiday.name == "Diwali" and not facts[date(2026, 10, 20)].employed


def test_branch_without_a_calendar_falls_back_to_locations(acme):
    admin, cid = acme["admin"], acme["cid"]
    worker = create_employee(admin, "Nia", branch_id=acme["chennai"]["id"])
    _holiday(admin, "Everywhere Day", "2099-05-01")
    _sql("UPDATE holidays SET calendar_id = NULL WHERE company_id = :c", c=cid)
    _sql("UPDATE branches SET holiday_calendar_id = NULL WHERE id = :b", b=acme["chennai"]["id"])
    _sql("INSERT INTO holidays (company_id, name, date, location, is_optional, holiday_type, created_at, updated_at) "
         "VALUES (:c, 'Chennai Only', '2099-05-02', 'Chennai', false, 'COMPANY', now(), now())", c=cid)
    facts = {f.date: f for f in _facts(cid, worker["id"], date(2099, 5, 1), date(2099, 5, 3))}
    assert facts[date(2099, 5, 1)].holiday.name == "Everywhere Day"
    assert facts[date(2099, 5, 2)].holiday.name == "Chennai Only"


def test_holidays_are_tenant_isolated(three_companies):
    a, b, c = three_companies["A"], three_companies["B"], three_companies["C"]
    ha = _holiday(a, "Alpha Day", "2099-02-01")
    _holiday(b, "Alpha Day", "2099-02-01")
    _holiday(c, "Gamma Day", "2099-02-02")
    assert [h["name"] for h in c.get(HOLIDAYS, params={"year": 2099}).json()] == ["Gamma Day"]
    assert b.put(f"{HOLIDAYS}/{ha['id']}", json={"name": "Hijack", "date": "2099-02-01"}).status_code == 404
    assert c.delete(f"{HOLIDAYS}/{ha['id']}").status_code == 404
    rows = _sql("SELECT company_id, count(*) AS n FROM source_change_log WHERE record_type = 'holiday' "
                "GROUP BY company_id ORDER BY company_id")
    assert [r["n"] for r in rows] == [1, 1, 1]
