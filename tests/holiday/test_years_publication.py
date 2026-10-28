"""Holiday rules, year generation, publication and amendments through the API (LH-M1 WI-1.13; owner 04-holiday/01 §6,
04-holiday/02 §3-§6; 11 §6.10).

Catalogue cases: LH-REC-03 (API), LH-HOL-15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26. Goal 3 readings are tested
too: "LH-REC-03: NEAREST_WORKDAY moves a Sunday holiday to Monday and a Saturday one to Friday", "LH-HOL-18: an
amendment needs a reason and increments the year version", "LH-HOL-22: a second person when
holiday.publish.requires_second_person is on". The company clock is the attendance fake clock (app.core.clock.utcnow).
"""

from datetime import date

import pytest
from sqlalchemy import text

from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.time_goldens import tenant_db

D = date
HC = "/api/v1/holiday-calendars"
SETTINGS = "/api/v1/payroll/settings/values"
REPUBLIC_DAY = {"name": "Republic Day", "kind": "FIXED_DATE", "params": {"month": 1, "day": 26},
                "holiday_type": "PUBLIC", "category": "NATIONAL"}


# ============================================================================================== fixtures and helpers
def _items(body):
    return body.get("items") if isinstance(body, dict) else body


@pytest.fixture
def alpha(app, fake_clock):
    fake_clock.at_ist("2026-11-02", "10:00")
    admin = signup_company(app, "Alpha Years")
    branch = _items(admin.get("/api/v1/branches").json())[0]
    entity = _items(admin.get("/api/v1/legal-entities").json())[0]
    boss = create_employee(admin, "Arjun", "Mehta", branch_id=branch["id"])
    worker = create_employee(admin, "Asha", "Pillai", manager_id=boss["id"], branch_id=branch["id"])
    kavya = create_employee(admin, "Kavya", "Nair", branch_id=branch["id"])
    return {"admin": admin, "branch": branch, "entity": entity, "worker": worker, "clock": fake_clock,
            "company_id": admin.company_id,
            "boss_client": invite_and_activate(app, admin, boss, ["manager"]),
            "worker_client": invite_and_activate(app, admin, worker, ["employee"]),
            "kavya": invite_and_activate(app, admin, kavya, ["hr_admin"])}


def _calendar(admin, name, **extra):
    r = admin.post(HC, json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _link(admin, branch_id, calendar_id):
    r = admin.put(f"/api/v1/branches/{branch_id}/calendar-link", json={"holiday_calendar_id": calendar_id})
    assert r.status_code == 200, r.text


def _rule(admin, calendar_id, body, status=201):
    r = admin.post(f"{HC}/{calendar_id}/rules", json=body)
    assert r.status_code == status, r.text
    return r.json()


def _generate(admin, calendar_id, year, status=200):
    r = admin.post(f"{HC}/{calendar_id}/years/{year}/generate")
    assert r.status_code == status, r.text
    return r.json()


def _year(admin, calendar_id, year):
    r = admin.get(f"{HC}/{calendar_id}/years/{year}")
    assert r.status_code == 200, r.text
    return r.json()


def _dry(client, calendar_id, year):
    r = client.post(f"{HC}/{calendar_id}/years/{year}/publish", json={"dry_run": True})
    assert r.status_code == 200, r.text
    return r.json()


def _publish(client, calendar_id, year, status=200):
    dry = _dry(client, calendar_id, year)
    codes = [w["code"] for w in dry["impact"]["warnings"] if w.get("confirm")]
    r = client.post(f"{HC}/{calendar_id}/years/{year}/publish",
                    json={"acknowledged_warnings": codes, "content_hash": dry["impact"]["content_hash"]})
    assert r.status_code == status, r.text
    return r.json()


def _amend(client, calendar_id, year, operations, reason="Government notice", status=200, **extra):
    body = {"operations": operations, **extra}
    if reason:
        body["reason"] = reason
    r = client.post(f"{HC}/{calendar_id}/years/{year}/amend", json=body)
    assert r.status_code == status, r.text
    return r.json()


def _set(admin, key, value, scope=None):
    current = admin.get(SETTINGS).json()["items"]
    item = next(i for i in current if i["key"] == key)
    change = {"key": key, "value": value, "row_version": item.get("row_version") or 1, "reason": "Test policy"}
    if scope:
        change.update(scope)
    r = admin.put(SETTINGS, json={"changes": [change]})
    assert r.status_code == 200, r.text


def _facts(company_id, employee_id, start, end):
    from app.payroll.services import time_inputs

    with tenant_db(company_id) as db:
        return {f.date: f for f in time_inputs.day_facts(db, company_id, employee_id, start, end)}


def _holiday_on(alpha, day):
    fact = _facts(alpha["company_id"], alpha["worker"]["id"], day, day)[day]
    return fact.holiday.name if fact.holiday else None


def _own(year_view, **match):
    return [i for i in year_view["items"] if i["own"] and all(i.get(k) == v for k, v in match.items())]


def _apply_leave(alpha, start, end):
    r = alpha["worker_client"].post("/api/v1/leaves/apply", json={
        "leave_type": "casual", "start_date": start, "end_date": end, "reason": "Family"})
    assert r.status_code == 200, r.text
    leave_id = r.json()["id"]
    for approver in (alpha["boss_client"], alpha["kavya"], alpha["admin"]):  # every step of the default chain
        if owner_rows("SELECT status FROM leaves WHERE id = :i", i=leave_id)[0]["status"] != "pending":
            break
        decided = approver.post(f"/api/v1/leaves/{leave_id}/approve", json={"remarks": "ok"})
        assert decided.status_code == 200, decided.text
    assert owner_rows("SELECT status FROM leaves WHERE id = :i", i=leave_id)[0]["status"] == "approved"
    return leave_id


def _charged(leave_id):
    return float(owner_rows("SELECT number_of_days FROM leaves WHERE id = :i", i=leave_id)[0]["number_of_days"])


# ============================================================================================== Goal 3 end to end
def test_goal3_republic_day_rule_preview_generate_publish(alpha):
    """Rule -> 5-year preview -> DRAFT 2027 (not counted) -> dry run -> PUBLISHED v1 (counted, notified).
    Also LH-REC-14 (regenerating with unchanged inputs changes nothing) and LH-HOL-19 (content changed between the
    dry run and the commit -> 409, nothing published)."""
    admin, cid = alpha["admin"], alpha["company_id"]
    national = _calendar(admin, "India National 2026", country_code="IN")
    _link(admin, alpha["branch"]["id"], national["id"])

    preview = admin.post(f"{HC}/rules/preview", json={"calendar_id": national["id"], "rule": REPUBLIC_DAY, "years": 5})
    assert preview.status_code == 200, preview.text
    assert [(y["year"], [r["date"] for r in y["rows"]]) for y in preview.json()["years"]] == [
        (2026, ["2026-01-26"]), (2027, ["2027-01-26"]), (2028, ["2028-01-26"]), (2029, ["2029-01-26"]),
        (2030, ["2030-01-26"])]
    assert [r["weekday"] for y in preview.json()["years"] for r in y["rows"]] == [0, 1, 2, 4, 5]  # Mon .. Sat

    created = _rule(admin, national["id"], REPUBLIC_DAY)
    rule = created["rule"]
    assert (rule["valid_from_year"], rule["version"], rule["summary"]) == (2026, 1, "Every 26 Jan")
    assert rule["next_dates"] == ["2027-01-26", "2028-01-26", "2029-01-26"]
    assert created["published_years_affected"] == [] and created["draft_changes"] == []
    assert _year(admin, national["id"], 2027)["status"] == "NONE"

    gen = _generate(admin, national["id"], 2027)
    assert gen["counts"]["inserted"] == 1 and gen["year_record"]["status"] == "DRAFT"
    again = _generate(admin, national["id"], 2027)  # LH-REC-14: idempotent
    assert all(v == 0 for v in again["counts"].values()), again["counts"]
    view = _year(admin, national["id"], 2027)
    assert (view["status"], view["version"]) == ("DRAFT", 0)
    republic = _own(view, name="Republic Day")[0]
    assert (republic["date"], republic["source"], republic["occurrence_key"], republic["status"]) == (
        "2027-01-26", "RULE", "2027:1:O", "CONFIRMED")
    assert _holiday_on(alpha, D(2027, 1, 26)) is None  # a DRAFT year never counts

    dry = _dry(admin, national["id"], 2027)
    impact = dry["impact"]
    assert [(d["date"], d["change"], d["name"]) for d in impact["diff"]] == [("2027-01-26", "added", "Republic Day")]
    assert impact["people"]["employees"] >= 3 and impact["holidays"] == {"own": 1, "expected": 0}
    assert not impact["errors"] and dry["year"]["status"] == "DRAFT"  # the dry run wrote nothing

    # LH-HOL-19: the draft changes between the dry run and the commit -> 409, nothing published
    added = admin.post(f"{HC}/{national['id']}/occurrences",
                       json={"name": "Founders' Day", "date": "2027-03-01", "holiday_type": "COMPANY"})
    assert added.status_code == 201 and added.json()["status"] == "DRAFT_UPDATED", added.text
    stale = admin.post(f"{HC}/{national['id']}/years/2027/publish", json={"content_hash": impact["content_hash"]})
    assert (stale.status_code, stale.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
    assert _year(admin, national["id"], 2027)["status"] == "DRAFT"

    published = _publish(admin, national["id"], 2027)
    assert published["status"] == "PUBLISHED" and published["year"]["version"] == 1
    assert published["year"]["published_by"] and published["year"]["approved_by"] is None
    assert _holiday_on(alpha, D(2027, 1, 26)) == "Republic Day"
    assert _holiday_on(alpha, D(2027, 3, 1)) == "Founders' Day"
    assert _generate(admin, national["id"], 2027, status=409)["code"] == "YEAR_PUBLISHED"
    again = admin.post(f"{HC}/{national['id']}/years/2027/publish", json={})
    assert (again.status_code, again.json()["code"]) == (409, "YEAR_PUBLISHED")
    log = owner_rows("SELECT change, details FROM source_change_log WHERE company_id = :c AND record_type = 'holiday_year'",
                     c=cid)
    assert log and log[-1]["details"]["event"] == "PUBLISHED"
    notes = owner_rows("SELECT message FROM notifications WHERE company_id = :c AND title = '2027 holidays published'",
                       c=cid)
    assert notes and "2 holiday(s)" in notes[0]["message"]
    history = admin.get(f"{HC}/{national['id']}/history").json()
    assert {"RULE_SAVED", "YEAR_GENERATED", "YEAR_PUBLISHED"} <= {e["action"] for e in history["events"]}
    assert history["years"][0]["year"] == 2027 and history["years"][0]["version"] == 1


def test_goal_label_rec_03_nearest_workday_sunday_to_monday_saturday_to_friday(alpha):
    """Goal 3 reading of LH-REC-03 (catalogue LH-REC-06/07 family): Sat -> Fri, Sun -> Mon on a Sat+Sun weekend;
    the ORIGINAL row stops counting once its OBSERVED partner is published (V3-063)."""
    admin = alpha["admin"]
    us = _calendar(admin, "Company Holidays US", country_code="US")
    assert us["effective_weekend"] == [5, 6]
    for name, month, day in (("New Year's Day", 1, 1), ("Juneteenth", 6, 19), ("Independence Day", 7, 4),
                             ("Christmas Day", 12, 25)):
        _rule(admin, us["id"], {"name": name, "kind": "FIXED_DATE", "params": {"month": month, "day": day},
                                "holiday_type": "PUBLIC", "observed_rule": "NEAREST_WORKDAY"})
    juneteenth = next(r for r in admin.get(f"{HC}/{us['id']}/rules").json()["items"] if r["name"] == "Juneteenth")
    preview = admin.post(f"{HC}/rules/preview", json={"calendar_id": us["id"], "years": 2, "from_year": 2027,
                                                      "rule_id": juneteenth["id"], "rule": {
        "name": "Juneteenth", "kind": "FIXED_DATE", "params": {"month": 6, "day": 19},
        "observed_rule": "NEAREST_WORKDAY", "holiday_type": "PUBLIC"}}).json()
    assert [(r["date"], r["observance"]) for r in preview["years"][0]["rows"]] == [
        ("2027-06-18", "OBSERVED"), ("2027-06-19", "ORIGINAL")]
    _generate(admin, us["id"], 2027)
    rows = [(i["date"], i["name"], i["observance"], i["observed_from_date"])
            for i in _year(admin, us["id"], 2027)["items"] if i["own"]]
    assert rows == [
        ("2027-01-01", "New Year's Day", "ORIGINAL", None),
        ("2027-06-18", "Juneteenth", "OBSERVED", "2027-06-19"),          # Saturday -> Friday
        ("2027-06-19", "Juneteenth", "ORIGINAL", None),
        ("2027-07-04", "Independence Day", "ORIGINAL", None),
        ("2027-07-05", "Independence Day", "OBSERVED", "2027-07-04"),    # Sunday -> Monday
        ("2027-12-24", "Christmas Day", "OBSERVED", "2027-12-25"),
        ("2027-12-25", "Christmas Day", "ORIGINAL", None),
        ("2027-12-31", "New Year's Day", "OBSERVED", "2028-01-01"),      # rule-year 2028 lands in 2027 (V3-059)
    ]
    _publish(admin, us["id"], 2027)
    _link(admin, alpha["branch"]["id"], us["id"])
    facts = _facts(alpha["company_id"], alpha["worker"]["id"], D(2027, 6, 18), D(2027, 7, 5))
    assert facts[D(2027, 6, 18)].holiday.name == "Juneteenth"
    assert facts[D(2027, 6, 19)].holiday is None  # the ORIGINAL row is replaced by its published partner
    assert facts[D(2027, 7, 4)].holiday is None and facts[D(2027, 7, 5)].holiday.name == "Independence Day"


def test_lh_rec_03_offset_into_a_published_previous_year_is_reported_not_written(alpha):
    admin = alpha["admin"]
    us = _calendar(admin, "US Company", country_code="US")
    admin.post(f"{HC}/{us['id']}/occurrences", json={"name": "Thanksgiving", "date": "2026-11-26",
                                                      "holiday_type": "PUBLIC"})
    _publish(admin, us["id"], 2026)
    created = _rule(admin, us["id"], {"name": "New Year's Eve (company)", "kind": "FIXED_DATE",
                                      "params": {"month": 1, "day": 1, "offset_days": -1}})
    assert created["rule"]["valid_from_year"] == 2026
    assert created["published_years_affected"] == [2026]  # Thu 2026-12-31 (rule-year 2027) needs an amendment
    _generate(admin, us["id"], 2027)
    rows = _own(_year(admin, us["id"], 2027), name="New Year's Eve (company)")
    assert [(r["date"], r["rule_year"]) for r in rows] == [("2027-12-31", 2028)]
    assert not _own(_year(admin, us["id"], 2026), name="New Year's Eve (company)")
    # "Review and apply to 2026…": one amendment, reason required, rule identity kept
    url = f"{HC}/{us['id']}/years/2026/apply-rules"
    no_reason = admin.post(url, json={})
    assert (no_reason.status_code, no_reason.json()["code"]) == (400, "REASON_REQUIRED")
    dry = admin.post(url, json={"dry_run": True, "reason": "Rule added"}).json()
    assert [(o["op"], o["date"]) for o in dry["operations"]] == [("add", "2026-12-31")]
    applied = admin.post(url, json={"reason": "Rule added"})
    assert applied.status_code == 200 and applied.json()["year"]["version"] == 2, applied.text
    row = _own(_year(admin, us["id"], 2026), name="New Year's Eve (company)")[0]
    assert (row["date"], row["source"], row["occurrence_key"], row["rule_year"]) == ("2026-12-31", "RULE", "2027:1:O", 2027)
    assert admin.post(url, json={"reason": "again"}).json()["status"] == "NO_CHANGE"


# ============================================================================================== amendments
def _published_national(alpha):
    admin = alpha["admin"]
    national = _calendar(admin, "India National 2026", country_code="IN")
    _link(admin, alpha["branch"]["id"], national["id"])
    _rule(admin, national["id"], REPUBLIC_DAY)
    _generate(admin, national["id"], 2027)
    _publish(admin, national["id"], 2027)
    return national


def test_goal_label_hol_18_amendment_needs_a_reason_and_increments_the_version(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    national = _published_national(alpha)
    before = _year(admin, national["id"], 2027)
    op = {"op": "add", "name": "Plant foundation day", "date": "2027-08-13", "holiday_type": "COMPANY"}
    missing = admin.post(f"{HC}/{national['id']}/years/2027/amend", json={"operations": [op]})
    assert (missing.status_code, missing.json()["code"]) == (400, "REASON_REQUIRED")
    dry = _amend(admin, national["id"], 2027, [op], dry_run=True)
    assert dry["dry_run"] and [d["date"] for d in dry["impact"]["diff"]] == ["2027-08-13"]
    assert _year(admin, national["id"], 2027)["version"] == 1
    done = _amend(admin, national["id"], 2027, [op], reason="Board resolution 14/2027")
    assert done["year"]["version"] == 2 and done["id"]
    after = _year(admin, national["id"], 2027)
    assert after["content_hash"] != before["content_hash"] and after["current_content_hash"] == after["content_hash"]
    assert _holiday_on(alpha, D(2027, 8, 13)) == "Plant foundation day"
    cancelled = _amend(admin, national["id"], 2027, [{"op": "cancel", "holiday_id": done["id"]}], reason="Revoked")
    assert cancelled["year"]["version"] == 3 and _holiday_on(alpha, D(2027, 8, 13)) is None
    audit = owner_rows("SELECT details FROM audit_logs WHERE company_id = :c AND action = 'YEAR_AMENDED' ORDER BY id",
                       c=cid)
    assert [a["details"]["reason"] for a in audit] == ["Board resolution 14/2027", "Revoked"]
    logs = owner_rows("SELECT record_id FROM source_change_log WHERE company_id = :c AND record_type = 'holiday' "
                      "AND record_id = :h", c=cid, h=done["id"])
    assert len(logs) == 2  # created, then cancelled
    # A PUBLISHED row is never edited directly: the draft PATCH refuses it, the DB guard refuses an app-role write.
    r = admin.patch(f"{HC}/occurrences/{done['id']}", json={"name": "Renamed"})
    assert (r.status_code, r.json()["code"]) == (409, "YEAR_PUBLISHED")
    with pytest.raises(Exception, match="YEAR_PUBLISHED"):
        with tenant_db(cid) as db:
            db.execute(text("UPDATE holidays SET name = 'x' WHERE id = :i"), {"i": done["id"]})
            db.flush()


def test_lh_hol_18_publication_that_covers_every_working_day_of_a_weekly_pay_period(alpha, fake_clock):
    from tests.payroll.test_runs_lifecycle import calendar_for, periods_of
    from tests.payroll.test_wage_inputs import pay_group, place

    admin = alpha["admin"]
    group = pay_group(alpha["company_id"], alpha["branch"]["legal_entity_id"], "WEEKLYH", frequency="WEEKLY")
    place(alpha["worker"]["id"], alpha["branch"]["legal_entity_id"], group)
    calendar_for(admin, group, frequency="WEEKLY", rules={"week_start_day": "MON"})
    assert any(p["start"] == "2027-03-01" for p in periods_of(admin, group))
    cal = _calendar(admin, "Plant IN", country_code="IN")  # weekend Sunday: Mon-Sat are working days
    _link(admin, alpha["branch"]["id"], cal["id"])
    r = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Annual shutdown", "date": "2027-03-01",
                                                          "duration_days": 6, "holiday_type": "COMPANY",
                                                          "category": "SHUTDOWN"})
    assert r.status_code == 201, r.text
    dry = _dry(admin, cal["id"], 2027)
    covered = [w for w in dry["impact"]["warnings"] if w["code"] == "HOLIDAYS_COVER_ALL_WORKING_DAYS"]
    assert covered and covered[0]["confirm"] and covered[0]["details"]["period_start"] == "2027-03-01"
    blocked = admin.post(f"{HC}/{cal['id']}/years/2027/publish", json={})
    assert (blocked.status_code, blocked.json()["code"]) == (422, "HOLIDAYS_COVER_ALL_WORKING_DAYS")
    ok = admin.post(f"{HC}/{cal['id']}/years/2027/publish",
                    json={"acknowledged_warnings": ["HOLIDAYS_COVER_ALL_WORKING_DAYS"]})
    assert ok.status_code == 200 and ok.json()["status"] == "PUBLISHED", ok.text


def test_goal_label_hol_22_second_person_for_publication_and_amendments(alpha):
    """LH-HOL-20 (catalogue) / Goal 3 LH-HOL-22: holiday.publish.requires_second_person = true."""
    admin, kavya = alpha["admin"], alpha["kavya"]
    _set(admin, "holiday.publish.requires_second_person", True)
    national = _calendar(admin, "India National 2026", country_code="IN")
    _rule(admin, national["id"], REPUBLIC_DAY)
    _rule(admin, national["id"], {"name": "Eid al-Fitr", "kind": "ANNUAL_CONFIRM", "params": {"expected_month": 3},
                                  "holiday_type": "PUBLIC", "category": "FESTIVAL"})
    rule_id = admin.get(f"{HC}/{national['id']}/rules").json()["items"]
    eid = next(r for r in rule_id if r["name"] == "Eid al-Fitr")
    assert admin.put(f"{HC}/rules/{eid['id']}/expected-date", json={"year": 2027, "date": "2027-03-10"}).status_code == 200
    _generate(admin, national["id"], 2027)

    pending = _publish(admin, national["id"], 2027, status=202)
    assert pending["status"] == "PENDING_SECOND_PERSON"
    assert pending["year"]["status"] == "DRAFT" and pending["year"]["pending_request"]["requested_by"]
    self_ok = admin.post(f"{HC}/{national['id']}/years/2027/publish", json={})
    assert (self_ok.status_code, self_ok.json()["code"]) == (403, "SELF_DECISION_FORBIDDEN")
    done = kavya.post(f"{HC}/{national['id']}/years/2027/publish", json={})
    assert done.status_code == 200, done.text
    year = done.json()["year"]
    assert (year["status"], year["version"], year["approved_by"]) == ("PUBLISHED", 1, "Kavya Nair")
    assert year["published_by"] != year["approved_by"] and year["pending_request"] is None

    op = {"op": "add", "name": "Plant foundation day", "date": "2027-08-13", "holiday_type": "COMPANY"}
    requested = _amend(admin, national["id"], 2027, [op], reason="Board resolution", status=202)
    assert requested["year"]["pending_request"]["kind"] == "AMEND"
    second = admin.post(f"{HC}/{national['id']}/years/2027/amend", json={"operations": [op], "reason": "Again"})
    assert (second.status_code, second.json()["code"]) == (409, "ROW_VERSION_CONFLICT")  # one pending request
    mine = admin.post(f"{HC}/{national['id']}/years/2027/amend", json={"complete_request": True})
    assert (mine.status_code, mine.json()["code"]) == (403, "SELF_DECISION_FORBIDDEN")
    completed = kavya.post(f"{HC}/{national['id']}/years/2027/amend", json={"complete_request": True})
    assert completed.status_code == 200, completed.text
    assert completed.json()["year"]["version"] == 2 and _holiday_on(alpha, D(2027, 8, 13)) is None  # branch unlinked
    assert any(i["name"] == "Plant foundation day" for i in _own(_year(admin, national["id"], 2027)))

    # P-4: confirming an expected date as expected never needs a second person (day facts unchanged)
    eid_row = _own(_year(admin, national["id"], 2027), name="Eid al-Fitr")[0]
    assert eid_row["status"] == "PROVISIONAL"
    confirm = admin.post(f"{HC}/occurrences/{eid_row['id']}/confirm", json={})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["year"]["version"] == 3

    # A pending request is voided when the year changes underneath it (hash mismatch -> 409)
    _amend(admin, national["id"], 2027, [{"op": "add", "name": "Bridge day", "date": "2027-08-16",
                                          "holiday_type": "COMPANY"}], reason="Bridge", status=202)
    with tenant_db(alpha["company_id"]) as db:
        db.execute(text("SET LOCAL app.holiday_amendment = 'on'"))
        db.execute(text("UPDATE holidays SET description = 'changed' , name = 'Republic Day (IN)' "
                        "WHERE calendar_id = :c AND name = 'Republic Day'"), {"c": national["id"]})
        db.commit()
    stale = kavya.post(f"{HC}/{national['id']}/years/2027/amend", json={"complete_request": True})
    assert (stale.status_code, stale.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
    assert _year(admin, national["id"], 2027)["pending_request"] is None


def test_lh_hol_15_provisional_counts_false_then_confirmed_is_an_addition(alpha):
    admin = alpha["admin"]
    national = _calendar(admin, "AE National", country_code="IN")
    _link(admin, alpha["branch"]["id"], national["id"])
    eid = _rule(admin, national["id"], {"name": "Eid al-Fitr", "kind": "ANNUAL_CONFIRM", "params": {},
                                        "holiday_type": "PUBLIC", "duration_days": 3,
                                        "duration_range": [3, 4]})["rule"]
    gen = _generate(admin, national["id"], 2027)
    assert gen["counts"]["date_needed"] == 1
    dry = _dry(admin, national["id"], 2027)
    assert "HOLIDAY_DATE_NEEDED" in [w["code"] for w in dry["impact"]["warnings"] if w["confirm"]]
    admin.put(f"{HC}/rules/{eid['id']}/expected-date", json={"year": 2027, "date": "2027-03-09"})
    rows = _own(_year(admin, national["id"], 2027), name="Eid al-Fitr")
    assert [(r["date"], r["status"], r["sequence_index"]) for r in rows] == [
        ("2027-03-09", "PROVISIONAL", 1), ("2027-03-10", "PROVISIONAL", 2), ("2027-03-11", "PROVISIONAL", 3)]
    _publish(admin, national["id"], 2027)
    assert _holiday_on(alpha, D(2027, 3, 9)) == "Eid al-Fitr"  # provisional_counts default true
    _set(admin, "holiday.provisional_counts", False)
    assert _holiday_on(alpha, D(2027, 3, 9)) is None
    leave = _apply_leave(alpha, "2027-03-08", "2027-03-12")
    assert _charged(leave) == 5.0
    no_reason = admin.post(f"{HC}/occurrences/{rows[0]['id']}/confirm", json={})
    assert (no_reason.status_code, no_reason.json()["code"]) == (400, "REASON_REQUIRED")  # an addition now
    ops = [{"op": "confirm", "holiday_id": r["id"]} for r in rows]
    done = _amend(admin, national["id"], 2027, ops, reason="Official announcement")
    assert done["year"]["version"] == 2 and done["effects"]["recount"]["dates"] == 3, done["effects"]
    assert _holiday_on(alpha, D(2027, 3, 9)) == "Eid al-Fitr"
    assert _charged(leave) == 2.0, done["effects"]  # Mon 03-08 and Fri 03-12


def test_lh_hol_16_half_days_and_optional_half_days_are_refused(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Half day", country_code="IN")
    half = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Diwali eve", "date": "2027-10-28",
                                                             "portion": "SECOND_HALF", "holiday_type": "COMPANY"})
    assert (half.status_code, half.json()["code"]) == (422, "INVALID_PORTION")
    optional = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Onam (half)", "date": "2027-09-14",
                                                                 "portion": "FIRST_HALF", "holiday_type": "OPTIONAL"})
    assert (optional.status_code, optional.json()["code"]) == (422, "INVALID_PORTION")
    rule = admin.post(f"{HC}/{cal['id']}/rules", json={**REPUBLIC_DAY, "portion": "FIRST_HALF"})
    assert (rule.status_code, rule.json()["code"]) == (422, "INVALID_PORTION")


def test_lh_hol_17_multi_day_eid_with_mixed_statuses_and_a_one_day_cancel(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "AE National", country_code="AE")
    eid = _rule(admin, cal["id"], {"name": "Eid al-Adha", "kind": "ANNUAL_CONFIRM", "params": {},
                                   "holiday_type": "PUBLIC", "duration_days": 3, "duration_range": [3, 3]})["rule"]
    admin.put(f"{HC}/rules/{eid['id']}/expected-date", json={"year": 2027, "date": "2027-05-17"})
    _generate(admin, cal["id"], 2027)
    _publish(admin, cal["id"], 2027)
    days = _own(_year(admin, cal["id"], 2027), name="Eid al-Adha")
    assert [d["date"] for d in days] == ["2027-05-17", "2027-05-18", "2027-05-19"]
    for d in days[:2]:
        assert admin.post(f"{HC}/occurrences/{d['id']}/confirm", json={}).status_code == 200
    no_reason = admin.post(f"{HC}/occurrences/{days[2]['id']}/cancel", json={})
    assert (no_reason.status_code, no_reason.json()["code"]) == (400, "REASON_REQUIRED")
    cancel = admin.post(f"{HC}/occurrences/{days[2]['id']}/cancel", json={"reason": "Two days announced"})
    assert cancel.status_code == 200 and cancel.json()["year"]["version"] == 4
    statuses = [d["status"] for d in _own(_year(admin, cal["id"], 2027), name="Eid al-Adha")]
    assert statuses == ["CONFIRMED", "CONFIRMED", "CANCELLED"]


def test_lh_hol_21_dependent_published_while_the_included_year_is_a_draft(alpha):
    admin = alpha["admin"]
    national = _calendar(admin, "IN National", country_code="IN")
    _rule(admin, national["id"], REPUBLIC_DAY)
    _generate(admin, national["id"], 2027)
    state = _calendar(admin, "IN-KA", country_code="IN", subdivision_code="IN-KA")
    assert admin.put(f"{HC}/{state['id']}/includes",
                     json={"includes": [{"included_calendar_id": national["id"], "priority": 1}]}).status_code == 200
    _link(admin, alpha["branch"]["id"], state["id"])
    admin.post(f"{HC}/{state['id']}/occurrences", json={"name": "Kannada Rajyotsava", "date": "2027-11-01",
                                                        "holiday_type": "PUBLIC"})
    dry = _dry(admin, state["id"], 2027)
    info = [w for w in dry["impact"]["warnings"] if w["code"] == "INCLUDED_YEAR_NOT_PUBLISHED"]
    assert info and not info[0]["confirm"] and info[0]["details"]["calendars"] == [national["code"]]
    _publish(admin, state["id"], 2027)
    leave = _apply_leave(alpha, "2027-01-25", "2027-01-27")
    assert _charged(leave) == 3.0  # 26 Jan is not a holiday for her yet
    published = _publish(admin, national["id"], 2027)
    assert published["effects"]["recount"]["dates"] >= 1, published["effects"]
    assert _charged(leave) == 2.0
    assert _holiday_on(alpha, D(2027, 1, 26)) == "Republic Day"


def test_lh_hol_22_future_company_holiday_cancelled_after_leave_is_recounted(alpha):
    national = _published_national(alpha)
    admin = alpha["admin"]
    added = _amend(admin, national["id"], 2027, [{"op": "add", "name": "Plant day", "date": "2027-10-29",
                                                  "holiday_type": "COMPANY"}], reason="Production plan")
    leave = _apply_leave(alpha, "2027-10-28", "2027-10-29")
    assert _charged(leave) == 1.0
    alpha["clock"].at_ist("2027-10-20", "10:00")
    _amend(admin, national["id"], 2027, [{"op": "cancel", "holiday_id": added["id"]}], reason="Production schedule")
    assert _charged(leave) == 2.0  # recounted, never auto-cancelled
    alpha["clock"].at_ist("2027-10-30", "10:00")
    late = admin.post(f"{HC}/occurrences/{added['id']}/cancel", json={"reason": "late"})
    assert late.status_code in (409, 422)  # already cancelled / past


def test_lh_hol_23_renaming_or_retyping_a_past_holiday_is_refused(alpha):
    national = _published_national(alpha)
    admin = alpha["admin"]
    alpha["clock"].at_ist("2027-02-10", "10:00")
    republic = _own(_year(admin, national["id"], 2027), name="Republic Day")[0]
    for change in ({"name": "Republic Day (National)"}, {"holiday_type": "COMPANY"}):
        r = admin.post(f"{HC}/{national['id']}/years/2027/amend",
                       json={"operations": [{"op": "edit", "holiday_id": republic["id"], **change}], "reason": "x"})
        assert (r.status_code, r.json()["code"]) == (409, "HOLIDAY_IN_PAST"), change
    before = owner_rows("SELECT count(*) AS n FROM source_change_log WHERE record_type = 'holiday' AND record_id = :h",
                        h=republic["id"])[0]["n"]
    ok = _amend(admin, national["id"], 2027, [{"op": "edit", "holiday_id": republic["id"],
                                              "description": "Constitution came into force"}], reason="Wording")
    assert ok["status"] == "AMENDED"
    after = owner_rows("SELECT count(*) AS n FROM source_change_log WHERE record_type = 'holiday' AND record_id = :h",
                       h=republic["id"])[0]["n"]
    assert after == before  # description is outside the content hash


def test_lh_hol_24_25_backdated_closures_and_date_bands(alpha):
    """LH-HOL-24 / LH-HOL-25: a backdated closure inside the window (reason, publish permission) credits leave back;
    older dates answer BACKDATE_LIMIT; a CONFIRMED day in the window cannot be cancelled (HOLIDAY_IN_PAST).
    LH-HOL-31: the PROVISIONAL day dated today is moved as a backdated amendment. LH-HOL-26: a closure is a typed,
    non-working holiday (an unpaid lay-off day is recorded as unpaid leave instead, 01 §5.8)."""
    national = _published_national(alpha)
    admin = alpha["admin"]
    _amend(admin, national["id"], 2027, [{"op": "add", "name": "Expected festival", "date": "2027-07-06",
                                          "holiday_type": "PUBLIC", "status": "PROVISIONAL"},
                                         {"op": "add", "name": "Confirmed day", "date": "2027-07-02",
                                          "holiday_type": "COMPANY"}], reason="List")
    leave = _apply_leave(alpha, "2027-06-30", "2027-06-30")
    alpha["clock"].at_ist("2027-07-06", "10:00")
    closure = {"name": "Power failure", "date": "2027-06-30", "holiday_type": "COMPANY", "category": "EMERGENCY"}
    no_reason = admin.post(f"{HC}/{national['id']}/occurrences", json=closure)
    assert (no_reason.status_code, no_reason.json()["code"]) == (400, "REASON_REQUIRED")
    ok = admin.post(f"{HC}/{national['id']}/occurrences", json={**closure, "reason": "Power failure at the plant"})
    assert ok.status_code == 200 and ok.json()["status"] == "AMENDED", ok.text
    assert _charged(leave) == 0.0  # credited back
    audit = owner_rows("SELECT action FROM audit_logs WHERE company_id = :c AND action = 'BACKDATED_CLOSURE'",
                       c=alpha["company_id"])
    assert audit
    old = admin.post(f"{HC}/{national['id']}/occurrences",
                     json={**closure, "name": "Old closure", "date": "2027-06-28", "reason": "too late"})
    assert (old.status_code, old.json()["code"]) == (422, "BACKDATE_LIMIT")
    view = _year(admin, national["id"], 2027)
    confirmed = _own(view, name="Confirmed day")[0]
    refused = admin.post(f"{HC}/occurrences/{confirmed['id']}/cancel", json={"reason": "no"})
    assert (refused.status_code, refused.json()["code"]) == (409, "HOLIDAY_IN_PAST")
    expected = _own(view, name="Expected festival")[0]
    assert "move" in expected["actions"]
    moved = admin.post(f"{HC}/occurrences/{expected['id']}/move",
                       json={"new_date": "2027-07-07", "reason": "Announced a day later"})
    assert moved.status_code == 200, moved.text
    rows = _own(_year(admin, national["id"], 2027), name="Expected festival")
    assert [(r["date"], r["status"]) for r in rows] == [("2027-07-06", "CANCELLED"), ("2027-07-07", "PROVISIONAL")]
    assert rows[1]["moved_from"] == "2027-07-06" and rows[1]["occurrence_key"] is None
    # LH-HOL-26: an unpaid closure is not a holiday; a holiday added as EMERGENCY is a non-working, typed holiday
    fact = _facts(alpha["company_id"], alpha["worker"]["id"], D(2027, 6, 30), D(2027, 6, 30))[D(2027, 6, 30)]
    assert fact.holiday.name == "Power failure" and not fact.working


# ============================================================================================== drafts, rules, working days
def test_draft_edits_skip_reset_override_and_discard(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Draft play", country_code="IN")
    _rule(admin, cal["id"], REPUBLIC_DAY)
    _rule(admin, cal["id"], {"name": "Labour Day", "kind": "FIXED_DATE", "params": {"month": 5, "day": 1}})
    _generate(admin, cal["id"], 2027)
    view = _year(admin, cal["id"], 2027)
    republic = _own(view, name="Republic Day")[0]
    labour = _own(view, name="Labour Day")[0]
    skip = admin.patch(f"{HC}/occurrences/{republic['id']}", json={"action": "SKIP"})
    assert skip.status_code == 200 and skip.json()["occurrence"]["status"] == "CANCELLED"
    moved = admin.patch(f"{HC}/occurrences/{labour['id']}", json={"date": "2027-05-03"})
    assert moved.json()["occurrence"]["overridden"] is True
    regen = _generate(admin, cal["id"], 2027)
    assert regen["counts"]["kept_skipped"] == 1 and regen["counts"]["kept_overridden"] == 1
    assert any(n["code"] == "OVERRIDDEN" for n in regen["notes"])
    reset = admin.patch(f"{HC}/occurrences/{labour['id']}", json={"action": "RESET"})
    assert reset.status_code == 200 and reset.json()["occurrence"]["date"] == "2027-05-01"
    restore = admin.patch(f"{HC}/occurrences/{republic['id']}", json={"action": "RESTORE"})
    assert restore.json()["occurrence"]["status"] == "CONFIRMED"
    manual = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Picnic", "date": "2027-02-12",
                                                               "holiday_type": "COMPANY"}).json()
    gone = admin.patch(f"{HC}/occurrences/{manual['id']}", json={"action": "SKIP"})
    assert gone.json()["status"] == "DELETED"
    discarded = admin.delete(f"{HC}/{cal['id']}/years/2027")
    assert discarded.status_code == 200 and discarded.json()["removed"] == 2
    assert _year(admin, cal["id"], 2027)["status"] == "NONE"
    assert _generate(admin, cal["id"], 2025, status=409)["code"] == "HOLIDAY_IN_PAST"
    assert _generate(admin, cal["id"], 2029, status=422)["code"] == "FUTURE_LIMIT"


def test_rule_validation_kind_immutability_and_deactivation(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Rules", country_code="US")
    bad = admin.post(f"{HC}/{cal['id']}/rules", json={"name": "Bad", "kind": "FIXED_DATE",
                                                      "params": {"month": 2, "day": 30}})
    assert (bad.status_code, bad.json()["code"]) == (422, "RULE_INVALID")  # LH-REC-01 RV-01
    rrule = admin.post(f"{HC}/{cal['id']}/rules", json={"name": "Rr", "kind": "RRULE",
                                                        "params": {"rrule": "FREQ=YEARLY", "dtstart": "2027-01-01"}})
    assert (rrule.status_code, rrule.json()["code"]) == (422, "RULE_INVALID")
    fifth = _rule(admin, cal["id"], {"name": "Spring team day", "kind": "NTH_WEEKDAY",
                                     "params": {"month": 3, "weekday": 0, "nth": 5}})
    assert any("5th" in i["message"] for i in fifth["info"])  # LH-REC-02 RV-02 info
    dup = admin.post(f"{HC}/{cal['id']}/rules", json={"name": "spring team day", "kind": "NTH_WEEKDAY",
                                                      "params": {"month": 4, "weekday": 0, "nth": 1}})
    assert (dup.status_code, dup.json()["code"]) == (422, "RULE_INVALID")
    trigger = admin.post(f"{HC}/{cal['id']}/rules", json={"name": "Odd", "kind": "FIXED_DATE",
                                                          "params": {"month": 7, "day": 4},
                                                          "observed_rule": "NEAREST_WORKDAY", "applies_on": [2]})
    assert (trigger.status_code, trigger.json()["code"]) == (422, "RULE_INVALID")  # RV-04
    _generate(admin, cal["id"], 2027)
    rule = fifth["rule"]
    assert [r["date"] for r in _own(_year(admin, cal["id"], 2027), name="Spring team day")] == ["2027-03-29"]
    changed = admin.patch(f"{HC}/rules/{rule['id']}", json={"kind": "FIXED_DATE", "params": {"month": 3, "day": 1}})
    assert (changed.status_code, changed.json()["code"]) == (422, "RULE_INVALID")
    edited = admin.patch(f"{HC}/rules/{rule['id']}", json={"params": {"month": 3, "weekday": 0, "nth": -1}},
                         headers={"If-Match": str(rule["row_version"])})
    assert edited.status_code == 200 and edited.json()["rule"]["version"] == 2
    assert edited.json()["draft_changes"][0]["year"] == 2027
    stale = admin.patch(f"{HC}/rules/{rule['id']}", json={"name": "x"},
                        headers={"If-Match": str(rule["row_version"])})
    assert (stale.status_code, stale.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
    off = admin.post(f"{HC}/rules/{rule['id']}/deactivate", json={"reason": "Not any more"})
    assert off.status_code == 200 and off.json()["rule"]["status"] == "INACTIVE"
    assert not _own(_year(admin, cal["id"], 2027), name="Spring team day")
    assert admin.post(f"{HC}/rules/{rule['id']}/deactivate", json={}).json()["code"] == "INVALID_TRANSITION"


def test_declared_working_days_flow_through_includes_and_conflict_with_holidays(alpha):
    """Declared working days (01 §5.5): released with their year, reach including calendars, and conflict with any
    non-cancelled holiday of the flattened calendar or of a calendar that includes it (LH-WWK-28: a PROVISIONAL one
    too, WORKING_DAY_CONFLICT)."""
    admin = alpha["admin"]
    national = _calendar(admin, "IN National", country_code="IN")
    child = _calendar(admin, "Branch", country_code="IN")
    admin.put(f"{HC}/{child['id']}/includes", json={"includes": [{"included_calendar_id": national["id"],
                                                                  "priority": 1}]})
    _link(admin, alpha["branch"]["id"], child["id"])
    child_holiday = admin.post(f"{HC}/{child['id']}/occurrences", json={"name": "Local fair", "date": "2027-02-14",
                                                                        "holiday_type": "COMPANY"})
    assert child_holiday.status_code == 201
    _publish(admin, child["id"], 2027)
    sunday = D(2027, 2, 7)
    made_up = admin.post(f"{HC}/{national['id']}/working-days", json={
        "date": sunday.isoformat(), "name": "Make-up day", "reason": "Bridge for Republic Day"})
    assert made_up.status_code == 201, made_up.text
    assert _facts(alpha["company_id"], alpha["worker"]["id"], sunday, sunday)[sunday].weekly_off  # draft year
    _publish(admin, national["id"], 2027)
    fact = _facts(alpha["company_id"], alpha["worker"]["id"], sunday, sunday)[sunday]
    assert (fact.weekly_off, fact.declared_working_day, fact.declared_working_portion, fact.working) == (
        False, True, "FULL", True)
    conflict = admin.post(f"{HC}/{national['id']}/working-days", json={
        "date": "2027-02-14", "name": "Clash", "reason": "x"})
    assert (conflict.status_code, conflict.json()["code"]) == (409, "WORKING_DAY_CONFLICT")
    assert child["code"] in conflict.json()["detail"]
    _amend(admin, national["id"], 2027, [{"op": "add", "name": "Eid al-Adha", "date": "2027-05-15",
                                          "holiday_type": "PUBLIC", "status": "PROVISIONAL"}], reason="Expected date")
    provisional = admin.post(f"{HC}/{national['id']}/working-days", json={
        "date": "2027-05-15", "name": "Make-up", "reason": "x"})
    assert (provisional.status_code, provisional.json()["code"]) == (409, "WORKING_DAY_CONFLICT")


def test_legacy_holiday_writes_keep_saved_equals_effective(alpha):
    admin, cid = alpha["admin"], alpha["company_id"]
    r = admin.post("/api/v1/holidays", json={"name": "Legacy Day", "date": "2027-09-01"})
    assert r.status_code == 200, r.text
    calendar_id = r.json()["calendar_id"]
    year = owner_rows("SELECT status, version FROM holiday_calendar_years WHERE calendar_id = :c AND year = 2027",
                      c=calendar_id)
    assert [(y["status"], y["version"]) for y in year] == [("PUBLISHED", 1)]
    second = admin.post("/api/v1/holidays", json={"name": "Legacy Day 2", "date": "2027-09-02"})
    assert second.status_code == 200
    assert owner_rows("SELECT version FROM holiday_calendar_years WHERE calendar_id = :c AND year = 2027",
                      c=calendar_id)[0]["version"] == 2
    deleted = admin.delete(f"/api/v1/holidays/{second.json()['id']}")
    assert deleted.status_code == 200
    status = owner_rows("SELECT status FROM holidays WHERE id = :i", i=second.json()["id"])[0]["status"]
    assert status == "CANCELLED"  # a published future row is cancelled, never deleted (BUG-15)
    listed = [h["name"] for h in admin.get("/api/v1/holidays", params={"year": 2027}).json()]
    assert "Legacy Day" in listed and "Legacy Day 2" not in listed
    again = admin.post("/api/v1/holidays", json={"name": "Legacy Day 2", "date": "2027-09-02"})
    assert again.status_code == 200  # the cancelled row does not block a re-creation
    assert cid


def test_lh_hol_32_confirming_an_expected_date_at_any_age(alpha):
    """P-4 (02 §6.4, LH-HOL-32): confirming a PROVISIONAL row on its expected date is allowed long after the backdate
    window when holiday.provisional_counts is on — no reason, no second person, a new version."""
    national = _published_national(alpha)
    admin = alpha["admin"]
    added = _amend(admin, national["id"], 2027, [{"op": "add", "name": "Eid al-Fitr", "date": "2027-03-09",
                                                  "holiday_type": "PUBLIC", "status": "PROVISIONAL"}],
                   reason="Expected")
    alpha["clock"].at_ist("2027-04-02", "10:00")
    moved = admin.post(f"{HC}/occurrences/{added['id']}/move", json={"new_date": "2027-04-10", "reason": "late"})
    assert (moved.status_code, moved.json()["code"]) == (409, "HOLIDAY_IN_PAST")  # P-5: too old to move
    confirmed = admin.post(f"{HC}/occurrences/{added['id']}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["year"]["version"] == 3
    assert _own(_year(admin, national["id"], 2027), name="Eid al-Fitr")[0]["status"] == "CONFIRMED"


def test_lh_hol_35_first_publication_with_past_dates_needs_acknowledgement(alpha):
    admin = alpha["admin"]
    alpha["clock"].at_ist("2027-03-15", "10:00")
    cal = _calendar(admin, "IN-MH", country_code="IN", subdivision_code="IN-MH")
    _link(admin, alpha["branch"]["id"], cal["id"])
    for name, day in (("Republic Day", "2027-01-26"), ("Holi", "2027-03-22")):
        r = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": name, "date": day, "holiday_type": "PUBLIC"})
        assert r.status_code == 201, r.text
    dry = _dry(admin, cal["id"], 2027)
    past = [w for w in dry["impact"]["warnings"] if w["code"] == "PAST_DATES_IN_FIRST_PUBLICATION"]
    assert past and past[0]["confirm"] and past[0]["details"]["dates"] == ["2027-01-26"]
    blocked = admin.post(f"{HC}/{cal['id']}/years/2027/publish", json={})
    assert (blocked.status_code, blocked.json()["code"]) == (422, "PAST_DATES_IN_FIRST_PUBLICATION")
    published = _publish(admin, cal["id"], 2027)
    assert published["year"]["version"] == 1 and "2027-01-26" in published["impact"]["changed_dates"]
    # After the first publication the same past date is an edit under the date bands (V3-042)
    late = admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Old day", "date": "2027-02-01",
                                                             "holiday_type": "COMPANY", "reason": "late"})
    assert (late.status_code, late.json()["code"]) == (422, "BACKDATE_LIMIT")
