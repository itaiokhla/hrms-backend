"""DB-level golden vectors of 03-shifts/01 §21 (GV-AS-SHF-09, -10, -11, -14; the pure ones are in
tests/attendance_unit/test_shift_engine.py). Dates are relative to the owner's real today because the version guard
uses the database clock (IG-AS-30); cast names follow the vectors where they matter.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytz
from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from tests.helpers import create_employee, signup_company

IST = pytz.timezone("Asia/Kolkata")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


def _shift(admin, code, start, end, **extra):
    r = admin.post("/api/v1/shifts", json={"code": code, "name": code.title(), "kind": "FIXED", "version": {
        "effective_from": "2024-01-01", "reason": "vector", "start_time": start, "end_time": end, **extra}})
    assert r.status_code == 201, r.text
    return r.json()


def _sql(query, **params):
    with system_engine.begin() as conn:
        return conn.execute(text(query), params)


@pytest.fixture
def plant(app):
    admin = signup_company(app, "Vector Works")
    branch = admin.get("/api/v1/branches").json()[0]
    gen = next(s for s in admin.get("/api/v1/shifts").json() if s["code"] == "GEN")
    shifts = {"A": _shift(admin, "A", "06:00", "14:00"), "B": _shift(admin, "B", "14:00", "22:00"),
              "FLEX": _shift(admin, "FLEXI", "10:00", "19:00"), "GEN": gen}
    packing = admin.post("/api/v1/departments", json={"name": "Packing"})
    assert packing.status_code == 200, packing.text
    return {"admin": admin, "branch": branch, "shifts": shifts, "packing": packing.json()["id"]}


def _resolve(admin, employee_id, day):
    r = admin.get("/api/v1/shifts/resolve", params={"employee_id": employee_id, "from": day.isoformat(),
                                                     "to": day.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()["days"][0]


def _direct(admin, cells):
    """Direct publication with every WARN acknowledged and every REQUIRE_REASON answered (dry run first)."""
    preview = admin.post("/api/v1/rosters/direct-publish", json={"cells": cells, "dry_run": True})
    assert preview.status_code == 200, preview.text
    results = preview.json()["results"]
    body = {"cells": cells,
            "acknowledged_warnings": sorted({r["rule_key"] for r in results if r["severity"] == "WARN"}),
            "reasons": {r["rule_key"]: "Planned" for r in results if r["severity"] == "REQUIRE_REASON"}}
    r = admin.post("/api/v1/rosters/direct-publish", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_gv_as_shf_09_resolution_sources_roster_off_rule_placement_none(plant):
    """ROSTER (published WORK), ROSTER with roster_off (no shift), RULE over placement, PLACEMENT, NONE (480)."""
    admin, s = plant["admin"], plant["shifts"]
    day = _today() + timedelta(days=9)
    ravi = create_employee(admin, "Ravi", shift_id=s["GEN"]["id"])
    sunita = create_employee(admin, "Sunita", shift_id=s["GEN"]["id"])
    priya = create_employee(admin, "Priya", shift_id=s["GEN"]["id"])
    rahul = create_employee(admin, "Rahul", shift_id=s["GEN"]["id"])
    kavya = create_employee(admin, "Kavya")
    _direct(admin, [{"employee_id": ravi["id"], "shift_date": day.isoformat(), "kind": "WORK", "shift_id": s["B"]["id"]},
                    {"employee_id": sunita["id"], "shift_date": day.isoformat(), "kind": "OFF"}])
    r = admin.post("/api/v1/shift-assignment-rules", json={
        "name": "Priya flexi", "shift_id": s["FLEX"]["id"], "criteria": {"employee_ids": [priya["id"]]},
        "priority": 600, "effective_from": day.isoformat()})
    assert r.status_code == 201, r.text
    sources = {name: _resolve(admin, emp["id"], day) for name, emp in
               (("ravi", ravi), ("sunita", sunita), ("priya", priya), ("rahul", rahul), ("kavya", kavya))}
    assert (sources["ravi"]["source"], sources["ravi"]["shift_id"]) == ("ROSTER", s["B"]["id"])
    assert sources["sunita"]["source"] == "NONE"  # roster_off: no shift on a rostered OFF
    assert (sources["priya"]["source"], sources["priya"]["shift_id"]) == ("RULE", s["FLEX"]["id"])
    assert (sources["rahul"]["source"], sources["rahul"]["shift_id"]) == ("PLACEMENT", s["GEN"]["id"])
    assert sources["kavya"]["source"] == "NONE"
    from app.services import shift_resolution

    db = SessionLocal()
    try:
        bind_tenant(db, admin.company_id)
        none = shift_resolution.shift_for(db, admin.company_id, kavya["id"], day)
        standard = shift_resolution.standard_hours(none, Decimal(1), Decimal(8), use_shift_duration=False)
    finally:
        db.close()
    assert none is None and standard * 60 == 480  # NONE: payroll.standard_hours_per_day x 60


def test_gv_as_shf_10_equal_priority_conflict_then_the_contract_rule_wins(plant):
    admin, s, branch = plant["admin"], plant["shifts"], plant["branch"]
    contract = admin.post("/api/v1/payroll/org/employment-types",
                          json={"code": "CONTRACT", "name": "Contractor", "worker_class": "CONTRACTOR"})
    assert contract.status_code in (200, 201), contract.text
    imran = create_employee(admin, "Imran", shift_id=s["GEN"]["id"], department_id=plant["packing"])
    _sql("UPDATE employee_assignments SET employment_type_id = :t WHERE employee_id = :e",
         t=contract.json()["id"], e=imran["id"])
    start = _today() + timedelta(days=5)
    packing = {"name": "Pune Packing", "shift_id": s["A"]["id"], "priority": 600, "effective_from": start.isoformat(),
               "criteria": {"branch_ids": [branch["id"]], "department_ids": [plant["packing"]]}}
    assert admin.post("/api/v1/shift-assignment-rules", json=packing).status_code == 201
    contract_rule = {"name": "Pune contract operators", "shift_id": s["B"]["id"], "priority": 600,
                     "effective_from": start.isoformat(),
                     "criteria": {"branch_ids": [branch["id"]], "employment_types": ["CONTRACT"]}}
    r = admin.post("/api/v1/shift-assignment-rules", json=contract_rule)
    assert r.status_code == 409 and r.json()["code"] == "PRIORITY_CONFLICT"
    r = admin.post("/api/v1/shift-assignment-rules", json={**contract_rule, "priority": 650})
    assert r.status_code == 201, r.text
    resolved = _resolve(admin, imran["id"], start + timedelta(days=1))
    assert (resolved["source"], resolved["shift_id"], resolved["rule_id"]) == ("RULE", s["B"]["id"], r.json()["id"])


def test_gv_as_shf_11_today_accepted_yesterday_refused_legacy_put_tomorrow(plant):
    admin, s = plant["admin"], plant["shifts"]
    today = _today()
    b = s["B"]["id"]
    r = admin.post(f"/api/v1/shifts/{b}/versions", json={"effective_from": today.isoformat(),
                                                          "start_time": "14:30", "end_time": "22:30"})
    assert r.status_code == 201, r.text
    versions = admin.get(f"/api/v1/shifts/{b}/versions").json()
    assert versions[0]["effective_to"] == (today - timedelta(days=1)).isoformat()  # previous ends yesterday
    r = admin.post(f"/api/v1/shifts/{b}/versions", json={"effective_from": (today - timedelta(days=1)).isoformat(),
                                                          "start_time": "15:00", "end_time": "23:00"})
    assert r.status_code == 409 and r.json()["code"] == "SHIFT_VERSION_IN_FORCE"
    r = admin.put(f"/api/v1/shifts/{b}", json={"name": "B", "start_time": "14:00", "end_time": "22:00"})
    assert r.status_code == 200, r.text
    latest = admin.get(f"/api/v1/shifts/{b}/versions").json()[-1]
    assert latest["effective_from"] == (today + timedelta(days=1)).isoformat()


def test_gv_as_shf_14_a_new_version_reversions_published_future_entries(plant):
    admin, s = plant["admin"], plant["shifts"]
    imran = create_employee(admin, "Imran", shift_id=s["GEN"]["id"])
    entry_day = _today() + timedelta(days=4)
    _direct(admin, [{"employee_id": imran["id"], "shift_date": entry_day.isoformat(), "kind": "WORK",
                     "shift_id": s["B"]["id"]}])
    before = _sql("SELECT id, version, shift_version_id FROM roster_entries WHERE employee_id = :e",
                  e=imran["id"]).one()
    effective = _today() + timedelta(days=2)
    r = admin.post(f"/api/v1/shifts/{s['B']['id']}/versions", json={
        "effective_from": effective.isoformat(), "start_time": "14:30", "end_time": "22:30"})
    assert r.status_code == 201, r.text
    assert r.json()["effects"]["roster"]["entries_reversioned"] == 1
    after = _sql("SELECT id, version, shift_version_id FROM roster_entries WHERE employee_id = :e AND "
                 "withdrawn_at IS NULL", e=imran["id"]).one()
    assert after.id == before.id and after.version == before.version + 1
    assert after.shift_version_id == r.json()["version"]["id"]
    change = _sql("SELECT c.change_kind, c.reason_code, c.initiated_by, c.notice_hours, p.kind FROM roster_entry_changes c "
                  "JOIN roster_publications p ON p.id = c.publication_id WHERE c.entry_id = :i AND c.change_kind = 'UPDATED'",
                  i=before.id).one()
    assert (change.reason_code, change.initiated_by, change.kind) == ("SHIFT_VERSION_CHANGED", "EMPLOYER", "DIRECT")
    assert change.notice_hours > 0
    logged = _sql("SELECT count(*) FROM source_change_log WHERE record_type = 'shift_version' AND employee_id = :e",
                  e=imran["id"]).scalar()
    assert logged >= 1
