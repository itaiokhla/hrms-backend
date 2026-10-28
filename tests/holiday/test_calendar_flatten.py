"""Holiday calendar structure: includes (DAG), exclusions, effective-dated links, criteria rows and the resolver
(LH-M1 WI-1.13; owner 04-holiday/01 §3, §4, §8; 11 §6.10, §6.11, §6.13).

Catalogue cases: LH-HOL-05, 06, 07, 08, 09, 10, 11, 12, 29, 30; LH-ORG-20. Goal 2 readings are tested too:
"LH-HOL-05: circular inheritance rejected with ORG_CALENDAR_CYCLE", "LH-HOL-08: an exclusion removes an included
occurrence without altering the parent", "LH-HOL-10: an effective-dated branch re-link does not rewrite history".
The company clock is the attendance fake clock (app.core.clock.utcnow).
"""

from datetime import date

import pytest
from sqlalchemy import text

from app.services import holiday_calendar_service as svc
from app.services.holiday_calendar_service import Exclusion, Row, flatten_rows
from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.time_goldens import tenant_db

D = date
HC = "/api/v1/holiday-calendars"


# ============================================================================================== pure flattening
def _rows(*items):
    out = {}
    for hid, cal, day, name, kind, *rule in items:
        out.setdefault(cal, []).append(Row(hid, cal, day, name, kind, rule[0] if rule else None))
    return out


def test_lh_hol_06_diamond_include_counts_a_national_holiday_once():
    """BR-BLR (1) includes IN-KA-SHOPS (2, priority 10) and COMPANY (3, priority 20); both include IN-NATIONAL (4)."""
    includes = {1: [(10, 2), (20, 3)], 2: [(0, 4)], 3: [(5, 4)]}
    rows = _rows((1201, 4, D(2027, 1, 26), "Republic Day", "PUBLIC"), (77, 2, D(2027, 11, 1), "Rajyotsava", "PUBLIC"),
                 (90, 3, D(2027, 3, 22), "Foundation Day", "COMPANY"))
    flat = flatten_rows(1, includes, {}, rows)
    republic = [f for f in flat.rows if f.row.id == 1201]
    assert len(republic) == 1 and republic[0].path == (1, 2, 4)  # first path in priority order is the source
    assert sorted(f.row.id for f in flat.rows) == [77, 90, 1201] and flat.order == (1, 2, 4, 3)


def test_lh_hol_08_exclusion_removes_an_inherited_occurrence_but_not_from_the_parent():
    includes = {2: [(10, 1)]}  # child 2 includes parent 1
    rows = _rows((10, 1, D(2026, 11, 1), "Kannada Rajyotsava", "PUBLIC"),
                 (11, 1, D(2026, 12, 25), "Christmas", "PUBLIC"), (20, 2, D(2026, 12, 31), "Tech Park Day", "COMPANY"))
    exclusions = {2: [Exclusion(5, 2, holiday_id=10)]}
    child = flatten_rows(2, includes, exclusions, rows)
    parent = flatten_rows(1, includes, exclusions, rows)
    assert sorted(f.row.id for f in child.rows) == [11, 20]
    assert [(f.row.id, eid) for f, eid in child.excluded] == [(10, 5)]
    assert sorted(f.row.id for f in parent.rows) == [10, 11]  # the parent keeps its occurrence
    # An exclusion never removes the calendar's own rows
    own = flatten_rows(2, includes, {2: [Exclusion(6, 2, holiday_id=20)]}, rows)
    assert 20 in own.ids()


def test_lh_hol_08_exclusion_on_an_intermediate_calendar_and_a_second_path():
    """Compositional semantics (IG-LH-11): an exclusion on IN-KA (2) removes the row from IN-KA's set; BR (1) still
    gets it when another include (COMPANY, 3) brings it."""
    includes = {1: [(10, 2), (20, 3)], 2: [(0, 4)], 3: [(0, 4)]}
    rows = _rows((1, 4, D(2027, 8, 15), "Independence Day", "PUBLIC"))
    excluded_on_ka = {2: [Exclusion(9, 2, holiday_id=1)]}
    assert 1 not in flatten_rows(2, includes, excluded_on_ka, rows).ids()
    assert 1 in flatten_rows(1, includes, excluded_on_ka, rows).ids()
    only_ka = {1: [(10, 2)], 2: [(0, 4)]}
    assert 1 not in flatten_rows(1, only_ka, excluded_on_ka, rows).ids()


def test_lh_hol_10_rule_exclusion_from_a_year_keeps_earlier_occurrences():
    """BR-PLANT (1) includes IN-MH (2); Gudi Padwa (rule 7) is excluded from 2028 on: 2027 stays, 2028+ go."""
    includes = {1: [(10, 2)]}
    rows = _rows((100, 2, D(2027, 4, 7), "Gudi Padwa", "PUBLIC", 7), (101, 2, D(2028, 3, 27), "Gudi Padwa", "PUBLIC", 7),
                 (102, 2, D(2029, 3, 16), "Gudi Padwa", "PUBLIC", 7))
    flat = flatten_rows(1, includes, {1: [Exclusion(3, 1, rule_id=7, effective_from_year=2028)]}, rows)
    assert sorted(f.row.date for f in flat.rows) == [D(2027, 4, 7)]
    assert sorted(f.row.id for f, _ in flat.excluded) == [101, 102]
    every_year = flatten_rows(1, includes, {1: [Exclusion(3, 1, rule_id=7)]}, rows)
    assert every_year.rows == ()


def test_lh_hol_07_cycle_and_depth_checks_name_the_path():
    names = {1: "IN-KA", 2: "BR-BLR"}
    with pytest.raises(svc.CalendarGraphError) as cycle:
        svc.check_graph({2: [(10, 1)], 1: [(10, 2)]}, 1, names)
    assert cycle.value.code == "ORG_CALENDAR_CYCLE" and "IN-KA → BR-BLR → IN-KA" in cycle.value.message
    chain = {i: [(1, i + 1)] for i in range(1, 65)}  # 1 -> 2 -> ... -> 65: 64 edges, allowed
    svc.check_graph(chain, 1)
    deeper = {**chain, 65: [(1, 66)]}
    with pytest.raises(svc.CalendarGraphError) as deep:
        svc.check_graph(deeper, 65)
    assert "too deep" in deep.value.message


def test_flattening_is_deterministic_and_versioned():
    includes = {1: [(10, 2)]}
    rows = _rows((1, 2, D(2027, 1, 1), "New Year", "COMPANY"))
    a, b = flatten_rows(1, includes, {}, rows), flatten_rows(1, includes, {}, rows)
    assert a == b and len(a.calendar_version) == 64
    assert flatten_rows(1, includes, {1: [Exclusion(1, 1, holiday_id=1)]}, rows).calendar_version != a.calendar_version
    assert svc.diff_dates(a, flatten_rows(1, {}, {}, rows)) == {D(2027, 1, 1): "edit"}
    assert svc.diff_dates(flatten_rows(1, {}, {}, rows), a) == {D(2027, 1, 1): "addition"}


# ============================================================================================== API fixtures
def _items(body):
    return body.get("items") if isinstance(body, dict) else body


@pytest.fixture
def alpha(app, fake_clock):
    fake_clock.at_ist("2027-01-10", "10:00")
    admin = signup_company(app, "Alpha Calendars")
    branch = _items(admin.get("/api/v1/branches").json())[0]
    entity = _items(admin.get("/api/v1/legal-entities").json())[0]
    worker = create_employee(admin, "Asha", "Pillai", branch_id=branch["id"])
    return {"admin": admin, "branch": branch, "entity": entity, "worker": worker, "clock": fake_clock,
            "company_id": admin.company_id, "worker_client": invite_and_activate(app, admin, worker, ["employee"])}


def _calendar(admin, name, **extra):
    r = admin.post(HC, json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _publish(admin, calendar_id, year):
    """Publish a DRAFT year acknowledging its confirm-warnings (rows of DRAFT years never count, 01 §6.1)."""
    url = f"{HC}/{calendar_id}/years/{year}/publish"
    dry = admin.post(url, json={"dry_run": True})
    assert dry.status_code == 200, dry.text
    codes = [w["code"] for w in dry.json()["impact"]["warnings"] if w.get("confirm")]
    r = admin.post(url, json={"acknowledged_warnings": codes})
    assert r.status_code == 200, r.text
    return r.json()


def _occurrence(admin, calendar_id, name, day, kind="PUBLIC"):
    """Add a holiday that counts: a new year is published right away, a published year is amended."""
    r = admin.post(f"{HC}/{calendar_id}/occurrences",
                   json={"name": name, "date": day, "holiday_type": kind, "reason": "Test calendar"})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    if r.status_code == 201:
        _publish(admin, calendar_id, int(day[:4]))
    return {"id": body["id"], "name": name, "date": day}


def _includes(admin, calendar_id, *pairs, reason=None, status=200):
    body = {"includes": [{"included_calendar_id": c, "priority": p} for c, p in pairs]}
    if reason:
        body["reason"] = reason
    r = admin.put(f"{HC}/{calendar_id}/includes", json=body)
    assert r.status_code == status, r.text
    return r.json()


def _link(admin, branch_id, calendar_id, **extra):
    r = admin.put(f"/api/v1/branches/{branch_id}/calendar-link", json={"holiday_calendar_id": calendar_id, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _facts(company_id, employee_id, start, end):
    from app.payroll.services import time_inputs

    with tenant_db(company_id) as db:
        return {f.date: f for f in time_inputs.day_facts(db, company_id, employee_id, start, end)}


def _sql(statement, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(statement), params)
        return result.fetchall() if result.returns_rows else None


# ============================================================================================== structure through the API
def test_goal2_hierarchy_includes_exclusion_and_branch_link_end_to_end(alpha):
    """India National 2026 -> Bengaluru Tech Park 2026: include, exclude a regional festival, link the branch."""
    admin, cid = alpha["admin"], alpha["company_id"]
    national = _calendar(admin, "India National 2027", country_code="IN")
    child = _calendar(admin, "Bengaluru Tech Park 2027", country_code="IN", subdivision_code="IN-KA",
                      legal_entity_id=alpha["entity"]["id"])
    assert national["code"] == "INDIA-NATIONAL-2027" and national["owner"] == "Tenant-wide"
    holi = _occurrence(admin, national["id"], "Holi", "2027-03-22")
    regional = _occurrence(admin, national["id"], "Regional Festival", "2027-04-14")
    independence = _occurrence(admin, national["id"], "Independence Day", "2027-08-15")
    own = _occurrence(admin, child["id"], "Tech Park Foundation Day", "2027-06-01", "COMPANY")
    tree = _includes(admin, child["id"], (national["id"], 10))
    assert tree["tree"]["children"][0]["code"] == "INDIA-NATIONAL-2027"
    r = admin.post(f"{HC}/{child['id']}/exclusions", json={"holiday_id": regional["id"], "reason": "Works on it"})
    assert r.status_code == 201, r.text
    lineage = admin.get(f"{HC}/{child['id']}/lineage", params={"year": 2027}).json()
    assert lineage["flattened"] == {"total": 3, "own": 1, "inherited": 2, "excluded": 1}
    assert lineage["exclusions"][0]["holiday"]["name"] == "Regional Festival"
    holidays = admin.get(f"{HC}/{child['id']}/holidays", params={"year": 2027}).json()
    assert [h["name"] for h in holidays["items"]] == ["Holi", "Tech Park Foundation Day", "Independence Day"]
    assert holidays["items"][0]["path"] == ["BENGALURU-TECH-PARK-2027", "INDIA-NATIONAL-2027"]
    assert [h["name"] for h in holidays["excluded"]] == ["Regional Festival"]
    # LH-HOL-08 (Goal 2 reading): the parent keeps the excluded occurrence
    parent = admin.get(f"{HC}/{national['id']}/holidays", params={"year": 2027}).json()
    assert "Regional Festival" in [h["name"] for h in parent["items"]]
    _link(admin, alpha["branch"]["id"], child["id"])
    facts = _facts(cid, alpha["worker"]["id"], D(2027, 3, 1), D(2027, 6, 30))
    assert facts[D(2027, 3, 22)].holiday.name == "Holi" and facts[D(2027, 3, 22)].calendar_id == child["id"]
    assert facts[D(2027, 3, 22)].calendar_source == "BRANCH_LINK"
    assert facts[D(2027, 4, 14)].holiday is None  # excluded regional festival
    assert facts[D(2027, 6, 1)].holiday.name == "Tech Park Foundation Day"
    audit = [a["action"] for a in owner_rows("SELECT action FROM audit_logs WHERE module = 'Holidays' "
                                             "AND entity_id = :e ORDER BY id", e=str(child["id"]))]
    assert {"SAVE", "CALENDAR_INCLUDES_SAVED", "CALENDAR_EXCLUSION_ADDED"} <= set(audit)
    assert independence["id"] and own["id"] and holi["id"]


def test_lh_hol_05_cycle_rejected_by_service_and_trigger(alpha):
    """Goal 2 reading of LH-HOL-05 (catalogue LH-HOL-07): a cycle is refused with ORG_CALENDAR_CYCLE and the path."""
    admin, cid = alpha["admin"], alpha["company_id"]
    ka = _calendar(admin, "IN-KA")
    blr = _calendar(admin, "BR-BLR")
    _includes(admin, blr["id"], (ka["id"], 10))
    r = admin.put(f"{HC}/{ka['id']}/includes", json={"includes": [{"included_calendar_id": blr["id"], "priority": 10}]})
    assert (r.status_code, r.json()["code"]) == (400, "ORG_CALENDAR_CYCLE")
    assert "IN-KA → BR-BLR → IN-KA" in r.json()["detail"]
    self_include = admin.put(f"{HC}/{ka['id']}/includes",
                             json={"includes": [{"included_calendar_id": ka["id"], "priority": 10}]})
    assert self_include.json()["code"] == "ORG_CALENDAR_CYCLE"
    with pytest.raises(Exception) as caught:  # the trigger is the backstop for direct SQL
        _sql("INSERT INTO holiday_calendar_includes (company_id, calendar_id, included_calendar_id, priority) "
             "VALUES (:c, :a, :b, 10)", c=cid, a=ka["id"], b=blr["id"])
    assert "ORG_CALENDAR_CYCLE" in str(caught.value)


def test_lh_hol_07_depth_limit(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    ids = [_sql("INSERT INTO holiday_calendars (company_id, code, name, status, employee_observance, name_i18n, "
                "row_version, created_at, updated_at) VALUES (:c, :code, :code, 'ACTIVE', 'NONE', '{}'::jsonb, 1, "
                "now(), now()) RETURNING id", c=cid, code=f"CH-{i}")[0][0] for i in range(66)]
    for parent, child in zip(ids, ids[1:65]):  # a chain of 64 includes
        _sql("INSERT INTO holiday_calendar_includes (company_id, calendar_id, included_calendar_id, priority) "
             "VALUES (:c, :a, :b, 1)", c=cid, a=parent, b=child)
    r = admin.put(f"{HC}/{ids[64]}/includes", json={"includes": [{"included_calendar_id": ids[65], "priority": 1}]})
    assert (r.status_code, r.json()["code"]) == (400, "ORG_CALENDAR_CYCLE") and "too deep" in r.json()["detail"]
    assert owner_rows("SELECT count(*) AS n FROM holiday_calendar_includes WHERE calendar_id = :c",
                      c=ids[64])[0]["n"] == 0


def test_lh_hol_05_entity_owned_calendar_outside_its_entity(alpha):
    """Catalogue LH-HOL-05: link and override to another entity's calendar -> 422 CALENDAR_ENTITY_MISMATCH."""
    admin = alpha["admin"]
    beta = admin.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Beta FZ-LLC", "country_code": "AE",
                                                      "base_currency": "AED", "timezone": "Asia/Dubai"})
    assert beta.status_code in (200, 201), beta.text
    dxb = _calendar(admin, "AE-DXB", legal_entity_id=beta.json()["id"], country_code="AE")
    r = admin.put(f"/api/v1/branches/{alpha['branch']['id']}/calendar-link", json={"holiday_calendar_id": dxb["id"]})
    assert (r.status_code, r.json()["code"]) == (422, "CALENDAR_ENTITY_MISMATCH")
    move = admin.post(f"/api/v1/employees/{alpha['worker']['id']}/assignments", json={
        "branch_id": alpha["branch"]["id"], "effective_from": "2027-02-01", "reason": "CORRECTION",
        "holiday_calendar_id": dxb["id"]})
    assert (move.status_code, move.json()["code"]) == (422, "CALENDAR_ENTITY_MISMATCH"), move.text
    own_ka = _calendar(admin, "IN-KA-ONLY")  # tenant-wide calendars may include only tenant-wide calendars
    r = admin.put(f"{HC}/{own_ka['id']}/includes", json={"includes": [{"included_calendar_id": dxb["id"], "priority": 1}]})
    assert (r.status_code, r.json()["code"]) == (422, "CALENDAR_ENTITY_MISMATCH")


def test_lh_hol_09_excluding_an_own_row_is_refused(alpha):
    admin = alpha["admin"]
    blr = _calendar(admin, "BR-BLR")
    karaga = _occurrence(admin, blr["id"], "Karaga", "2027-04-02", "COMPANY")
    r = admin.post(f"{HC}/{blr['id']}/exclusions", json={"holiday_id": karaga["id"], "reason": "No"})
    assert (r.status_code, r.json()["code"]) == (422, "UNKNOWN_REFERENCE")
    assert r.json()["detail"] == "This holiday is not inherited by the calendar; cancel it on its own calendar instead."
    assert owner_rows("SELECT count(*) AS n FROM holiday_calendar_exclusions")[0]["n"] == 0


def test_lh_hol_08_structure_change_backdating_window(alpha):
    """calendar_local_today = 2027-03-10, W = 7: a gain on 2027-02-26 is BACKDATE_LIMIT; a gain on 2027-03-08
    needs holiday:publish and a reason; nothing is written when refused."""
    admin, cid = alpha["admin"], alpha["company_id"]
    alpha["clock"].at_ist("2027-03-10", "10:00")
    pune = _calendar(admin, "BR-PUNE")
    company = _calendar(admin, "COMPANY-2027")
    _link(admin, alpha["branch"]["id"], pune["id"])  # pune has users
    for name, day in (("Shutdown", "2027-02-26"), ("Foundation Day", "2027-03-08")):
        _sql("INSERT INTO holidays (company_id, calendar_id, name, date, holiday_type, is_optional, created_at, "
             "updated_at) VALUES (:c, :cal, :n, :d, 'COMPANY', false, now(), now())", c=cid, cal=company["id"], n=name,
             d=day)
    r = admin.put(f"{HC}/{pune['id']}/includes",
                  json={"includes": [{"included_calendar_id": company["id"], "priority": 10}], "reason": "Join"})
    assert (r.status_code, r.json()["code"]) == (422, "BACKDATE_LIMIT")
    assert "2027-02-26" in r.json()["details"]["dates"]
    assert owner_rows("SELECT count(*) AS n FROM holiday_calendar_includes WHERE calendar_id = :c",
                      c=pune["id"])[0]["n"] == 0
    _sql("DELETE FROM holidays WHERE calendar_id = :cal AND name = 'Shutdown'", cal=company["id"])
    no_reason = admin.put(f"{HC}/{pune['id']}/includes",
                          json={"includes": [{"included_calendar_id": company["id"], "priority": 10}]})
    assert (no_reason.status_code, no_reason.json()["code"]) == (400, "REASON_REQUIRED")
    ok = _includes(admin, pune["id"], (company["id"], 10), reason="Foundation Day joins the plant")
    assert ok["affected_dates"] == ["2027-03-08"]
    assert owner_rows("SELECT count(*) AS n FROM source_change_log WHERE record_type = 'calendar_link'")[0]["n"] >= 1
    # Removing it again loses a CONFIRMED holiday inside the window -> HOLIDAY_IN_PAST
    lose = admin.put(f"{HC}/{pune['id']}/includes", json={"includes": [], "reason": "Undo"})
    assert (lose.status_code, lose.json()["code"]) == (409, "HOLIDAY_IN_PAST")


def test_lh_hol_10_effective_dated_branch_relink_keeps_history(alpha):
    """Goal 2 reading of LH-HOL-10: re-linking the branch from 2027-04-01 changes no earlier date."""
    admin, cid = alpha["admin"], alpha["company_id"]
    old = _calendar(admin, "OLD-CAL")
    new = _calendar(admin, "NEW-CAL")
    _occurrence(admin, old["id"], "Old Holiday March", "2027-03-15")
    _occurrence(admin, old["id"], "Old Holiday May", "2027-05-17")
    _occurrence(admin, new["id"], "New Holiday May", "2027-05-18")
    _link(admin, alpha["branch"]["id"], old["id"])
    before = _facts(cid, alpha["worker"]["id"], D(2027, 1, 11), D(2027, 6, 30))
    _link(admin, alpha["branch"]["id"], new["id"], effective_from="2027-04-01")
    after = _facts(cid, alpha["worker"]["id"], D(2027, 1, 11), D(2027, 6, 30))
    for day in (d for d in before if d < D(2027, 4, 1)):
        assert (after[day].calendar_id, getattr(after[day].holiday, "id", None)) == (
            before[day].calendar_id, getattr(before[day].holiday, "id", None))
    assert after[D(2027, 3, 15)].holiday.name == "Old Holiday March"
    assert after[D(2027, 5, 17)].holiday is None and after[D(2027, 5, 18)].holiday.name == "New Holiday May"
    links = admin.get(f"/api/v1/branches/{alpha['branch']['id']}/calendar-links").json()["items"]
    assert [(l["holiday_calendar_id"], l["effective_to"]) for l in links][-2:] == [
        (old["id"], "2027-03-31"), (new["id"], None)]
    # The mirror keeps the calendar valid on the branch-local today until the job activates the new link.
    assert owner_rows("SELECT holiday_calendar_id FROM branches WHERE id = :b",
                      b=alpha["branch"]["id"])[0]["holiday_calendar_id"] == old["id"]


def test_lh_hol_11_override_beats_criteria_beats_branch_link(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    blr, night, kerala = (_calendar(admin, n) for n in ("BR-BLR", "BR-BLR-NIGHT", "IN-KL-SHOPS"))
    _link(admin, alpha["branch"]["id"], blr["id"])
    shift = admin.post("/api/v1/shifts", json={"name": "NIGHT-1", "start_time": "22:00", "end_time": "06:00"}).json()
    sid = shift["id"]
    _sql("UPDATE employee_assignments SET shift_id = :s WHERE employee_id = :e", s=sid, e=alpha["worker"]["id"])
    r = admin.post("/api/v1/holiday-calendar-assignments", json={
        "calendar_id": night["id"], "criteria": {"branch_ids": [alpha["branch"]["id"]], "shift_ids": [sid]},
        "priority": 10, "effective_from": "2027-02-01"})
    assert r.status_code == 201, r.text
    assert _facts(cid, alpha["worker"]["id"], D(2027, 4, 9), D(2027, 4, 9))[D(2027, 4, 9)].calendar_id == night["id"]
    _sql("UPDATE employee_assignments SET holiday_calendar_id = :k WHERE employee_id = :e", k=kerala["id"],
         e=alpha["worker"]["id"])
    fact = _facts(cid, alpha["worker"]["id"], D(2027, 4, 9), D(2027, 4, 9))[D(2027, 4, 9)]
    assert (fact.calendar_id, fact.calendar_source) == (kerala["id"], "OVERRIDE")
    from app.services import holiday_assignments

    with tenant_db(cid) as db:
        resolved = holiday_assignments.resolve(db, cid, alpha["worker"]["id"], D(2027, 4, 9))
    assert (resolved.calendar_id, resolved.calendar_source) == (kerala["id"], "OVERRIDE")


def test_lh_hol_12_equal_priority_overlapping_rows_are_refused(alpha):
    admin = alpha["admin"]
    qa, night = _calendar(admin, "CAL-QA"), _calendar(admin, "CAL-NIGHT")
    branch = alpha["branch"]["id"]
    a = admin.post("/api/v1/holiday-calendar-assignments", json={
        "calendar_id": qa["id"], "criteria": {"branch_ids": [branch]}, "priority": 50, "effective_from": "2027-02-01"})
    assert a.status_code == 201, a.text
    b = admin.post("/api/v1/holiday-calendar-assignments", json={
        "calendar_id": night["id"], "criteria": {"shift_ids": [1]}, "priority": 50, "effective_from": "2027-06-01",
        "effective_to": "2027-12-31"})
    assert (b.status_code, b.json()["code"]) == (409, "CALENDAR_ASSIGNMENT_CONFLICT")
    alpha["clock"].at_ist("2025-06-01", "10:00")
    c = admin.post("/api/v1/holiday-calendar-assignments", json={
        "calendar_id": night["id"], "criteria": {"shift_ids": [1]}, "priority": 50, "effective_from": "2026-01-01",
        "effective_to": "2026-12-31"})
    assert c.status_code == 201, c.text  # no date overlap with row A
    everyone = admin.post("/api/v1/holiday-calendar-assignments", json={
        "calendar_id": qa["id"], "criteria": {"everyone": True}, "priority": 60, "effective_from": "2027-02-01"})
    assert (everyone.status_code, everyone.json()["code"]) == (422, "INVALID_CRITERIA")


def test_lh_hol_29_moving_a_tenant_calendar_to_an_entity_used_elsewhere(alpha):
    admin = alpha["admin"]
    national = _calendar(admin, "IN-NATIONAL")
    beta = admin.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Beta FZ-LLC", "country_code": "AE",
                                                      "base_currency": "AED", "timezone": "Asia/Dubai"}).json()
    dubai = admin.post("/api/v1/branches", json={"legal_entity_id": beta["id"], "code": "DXB", "name": "Dubai",
                                                 "timezone": "Asia/Dubai"}).json()
    _link(admin, dubai["id"], national["id"])
    r = admin.patch(f"{HC}/{national['id']}", json={"legal_entity_id": alpha["entity"]["id"]})
    assert (r.status_code, r.json()["code"]) == (422, "CALENDAR_ENTITY_MISMATCH")
    other = _calendar(admin, "AE-NATIONAL")
    _link(admin, dubai["id"], other["id"])
    moved = admin.patch(f"{HC}/{national['id']}", json={"legal_entity_id": alpha["entity"]["id"]})
    assert moved.status_code == 200 and moved.json()["legal_entity_id"] == alpha["entity"]["id"]


def test_lh_hol_30_deactivation_blocked_by_future_link_and_banking_setting(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    old = _calendar(admin, "BR-OLD")
    keep = _calendar(admin, "BR-KEEP")
    _link(admin, alpha["branch"]["id"], keep["id"])
    _link(admin, alpha["branch"]["id"], old["id"], effective_from="2027-07-01")
    r = admin.patch(f"{HC}/{old['id']}", json={"status": "INACTIVE"})
    assert (r.status_code, r.json()["code"]) == (409, "ORG_DIMENSION_IN_USE")
    assert "branch link" in r.json()["detail"]
    _link(admin, alpha["branch"]["id"], keep["id"], effective_from="2027-07-01")
    _sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
         "row_version, source, created_at, updated_at) VALUES (:c, 'payroll.calendar.banking_calendar_id', 'TENANT', "
         "NULL, CAST(:v AS jsonb), false, '2027-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=cid, v=str(old["id"]))
    r = admin.patch(f"{HC}/{old['id']}", json={"status": "INACTIVE"})
    assert (r.status_code, r.json()["code"]) == (409, "ORG_DIMENSION_IN_USE") and "banking" in r.json()["detail"]
    _sql("DELETE FROM setting_values WHERE key = 'payroll.calendar.banking_calendar_id' AND company_id = :c", c=cid)
    assert admin.patch(f"{HC}/{old['id']}", json={"status": "INACTIVE"}).json()["status"] == "INACTIVE"
    relink = admin.put(f"/api/v1/branches/{alpha['branch']['id']}/calendar-link",
                       json={"holiday_calendar_id": old["id"], "effective_from": "2027-08-01"})
    assert (relink.status_code, relink.json()["code"]) == (400, "ORG_DIMENSION_INACTIVE")
    assert admin.get(f"{HC}/{old['id']}").status_code == 200  # still readable for history


def test_lh_org_20_saudi_calendar_weekend_is_derived_from_its_country(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    sa = _calendar(admin, "SA-NATIONAL", country_code="SA")
    assert sa["weekend_days"] is None and sa["effective_weekend"] == [4, 5]
    explicit = _calendar(admin, "SA-CUSTOM", country_code="SA", weekend_days=[5])
    assert explicit["effective_weekend"] == [5]
    in_cal = _calendar(admin, "IN-PLAIN", legal_entity_id=alpha["branch"]["legal_entity_id"])
    assert in_cal["effective_weekend"] == [6]  # no calendar country: the legal entity's (IN, Sunday)
    plain = _calendar(admin, "TENANT-PLAIN")
    assert plain["effective_weekend"] == [5, 6]  # tenant-wide, no country: Sat + Sun
    with tenant_db(cid) as db:
        from app.models.pay_org import HolidayCalendar

        row = db.get(HolidayCalendar, sa["id"])
        assert svc.effective_weekend(db, row) == [4, 5]


# ============================================================================================== zero diff, reads, scope
def test_legacy_parent_is_mirrored_and_flattens_like_the_old_lineage(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    parent = admin.post("/api/v1/payroll/org/holiday-calendars", json={"code": "IN-NAT", "name": "India national",
                                                                       "country_code": "IN"}).json()
    child = admin.post("/api/v1/payroll/org/holiday-calendars", json={"code": "IN-KA-LEG", "name": "Karnataka",
                                                                      "parent_calendar_id": parent["id"]}).json()
    assert owner_rows("SELECT included_calendar_id, priority FROM holiday_calendar_includes WHERE calendar_id = :c",
                      c=child["id"]) == [{"included_calendar_id": parent["id"], "priority": 0}]
    _occurrence(admin, parent["id"], "Republic Day", "2027-01-26")
    lineage = admin.get(f"{HC}/{child['id']}/holidays", params={"year": 2027}).json()
    assert [h["name"] for h in lineage["items"]] == ["Republic Day"]
    cycle = admin.patch(f"/api/v1/payroll/org/holiday-calendars/{parent['id']}",
                        json={"parent_calendar_id": child["id"]})
    assert (cycle.status_code, cycle.json()["code"]) == (400, "ORG_CALENDAR_CYCLE")
    cleared = admin.patch(f"/api/v1/payroll/org/holiday-calendars/{child['id']}", json={"parent_calendar_id": None})
    assert cleared.status_code == 200
    assert owner_rows("SELECT count(*) AS n FROM holiday_calendar_includes WHERE calendar_id = :c",
                      c=child["id"])[0]["n"] == 0


def test_reads_scope_and_day_facts_endpoint(app, alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "SCOPE-CAL")
    employee = alpha["worker_client"]
    assert employee.get(HC).status_code == 200  # holiday:read
    assert employee.post(HC, json={"name": "Nope"}).status_code == 403
    other = signup_company(app, "Other Calendars")
    assert other.get(f"{HC}/{cal['id']}").status_code == 404  # LH-SEC-01
    _link(admin, alpha["branch"]["id"], cal["id"])
    _occurrence(admin, cal["id"], "Scope Day", "2027-02-15")
    mine = employee.get("/api/v1/me/calendar", params={"from": "2027-02-01", "to": "2027-02-28"})
    assert mine.status_code == 200, mine.text
    day = next(d for d in mine.json()["days"] if d["date"] == "2027-02-15")
    assert day["holiday"]["name"] == "Scope Day" and day["calendar_id"] == cal["id"]
    other_days = employee.get("/api/v1/calendar/days", params={"employee_id": alpha["worker"]["id"],
                                                              "from": "2027-02-01", "to": "2027-02-28"})
    assert other_days.status_code == 200
    # 05 §3.1: fewer than 400 days per call (a whole year fits since Goal 4, IG-LH-24).
    too_long = admin.get("/api/v1/calendar/days", params={"employee_id": alpha["worker"]["id"],
                                                         "from": "2027-01-01", "to": "2028-02-05"})
    assert (too_long.status_code, too_long.json()["code"]) == (400, "RANGE_TOO_LONG")
    assignments = admin.get(f"{HC}/{cal['id']}/assignments").json()
    assert assignments["branch_links"][-1]["branch_id"] == alpha["branch"]["id"]
    stale = admin.patch(f"{HC}/{cal['id']}", json={"name": "Renamed"}, headers={"If-Match": '"99"'})
    assert (stale.status_code, stale.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
