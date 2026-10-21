"""Organisation dimension APIs (02-multi-level-company §12, §20): pay groups, grades, cost centers,
employment types, designations, work weeks, holiday calendars, branch links, the starter organisation
and PAY_GROUP scope targets (audit 01 D-13). EC-ORG-01, EC-ORG-02, EC-MON-03, EC-CAL-01.
"""

import pytest
from sqlalchemy import text

from tests.helpers import create_employee, invite_and_activate, signup_company

BASE = "/api/v1/payroll/org"


def _sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        return conn.execute(text(query), params).fetchall()


def _entity(client, code="LE01"):
    return next(e for e in client.get("/api/v1/legal-entities").json() if e["code"] == code)


def _make_entity(client, code, name, country, currency, tz="UTC"):
    r = client.post("/api/v1/legal-entities", json={"code": code, "legal_name": name, "country_code": country,
                                                    "base_currency": currency, "timezone": tz})
    assert r.status_code == 200, r.text
    return r.json()


def _post(client, path, body, status=201, **kw):
    r = client.post(f"{BASE}/{path}", json=body, **kw)
    assert r.status_code == status, r.text
    return r.json()


def _by_code(client, path, code):
    return next(x for x in client.get(f"{BASE}/{path}").json() if x["code"] == code)


@pytest.fixture
def acme(app):
    return signup_company(app, "Acme Corp")


# ---------------------------------------------------------------------------
# Starter organisation of a new tenant
# ---------------------------------------------------------------------------
def test_new_tenant_gets_starter_pay_group_work_week_calendar_and_employment_type(acme):
    entity = _entity(acme)
    groups = acme.get(f"{BASE}/pay-groups").json()
    assert [(g["code"], g["legal_entity_id"], g["currency"], g["pay_frequency"], g["status"]) for g in groups] == [
        ("MONTHLY", entity["id"], entity["base_currency"], "MONTHLY", "ACTIVE")]
    assert groups[0]["calendar_rules"] == {"monthly_start_day": 1, "pay_day_rule": "LAST_WORKING_DAY"}
    week = _by_code(acme, "work-weeks", "MON_FRI")
    assert (week["weekly_off_days"], week["nth_weekday_offs"]) == ([5, 6], [])
    calendar = _by_code(acme, "holiday-calendars", "COMPANY")
    assert [t["code"] for t in acme.get(f"{BASE}/employment-types").json()] == ["PERMANENT"]
    branch = acme.get("/api/v1/branches").json()[0]
    assert (branch["holiday_calendar_id"], branch["work_week_pattern_id"]) == (calendar["id"], week["id"])
    rows = _sql("SELECT a.pay_group_id, a.employment_type_id, a.work_week_pattern_id, a.shift_id, e.shift_id "
                "FROM employee_assignments a JOIN employees e ON e.id = a.employee_id WHERE a.company_id = :c",
                c=acme.company_id)
    permanent = _by_code(acme, "employment-types", "PERMANENT")["id"]
    assert len(rows) == 1 and tuple(rows[0][:3]) == (groups[0]["id"], permanent, week["id"])
    assert rows[0][3] is not None and rows[0][3] == rows[0][4]
    audit = _sql("SELECT module, details FROM audit_logs WHERE company_id = :c AND action = 'ORG_STARTER_PROVISIONED'",
                 c=acme.company_id)
    assert audit[0][0] == "Payroll Settings" and audit[0][1]["pay_group_id"] == groups[0]["id"]


# ---------------------------------------------------------------------------
# Pay groups
# ---------------------------------------------------------------------------
def test_pay_group_crud_validates_entity_currency_frequency_and_calendar_rules(app, acme):
    entity = _entity(acme)
    weekly = _post(acme, "pay-groups", {"legal_entity_id": entity["id"], "code": "wk-1", "name": "Weekly staff",
                                        "currency": "INR", "pay_frequency": "WEEKLY",
                                        "calendar_rules": {"week_start_day": "MON", "pay_day_rule": "DAYS_AFTER_PERIOD_END",
                                                           "pay_day_value": 3}})
    assert weekly["code"] == "WK-1" and weekly["calendar_rules"]["pay_day_value"] == 3
    # EC-CAL-01: start day 31 is valid (clamped per month by the calendar).
    _post(acme, "pay-groups", {"legal_entity_id": entity["id"], "code": "M31", "name": "From the 31st",
                               "pay_frequency": "MONTHLY", "calendar_rules": {"monthly_start_day": 31}})
    biweekly = {"legal_entity_id": entity["id"], "code": "BW", "name": "Bi-weekly", "pay_frequency": "BI_WEEKLY"}
    r = acme.post(f"{BASE}/pay-groups", json=biweekly)
    assert r.status_code == 400 and r.json()["code"] == "BIWEEKLY_ANCHOR_REQUIRED"
    r = acme.post(f"{BASE}/pay-groups", json={**biweekly, "calendar_rules": {"biweekly_anchor_date": "2026-01-06"}})
    assert r.status_code == 400 and r.json()["code"] == "BIWEEKLY_ANCHOR_WEEKDAY"  # a Tuesday
    created = _post(acme, "pay-groups", {**biweekly, "calendar_rules": {"biweekly_anchor_date": "2026-01-05"}})
    assert created["calendar_rules"] == {"biweekly_anchor_date": "2026-01-05"} and created["currency"] == "INR"
    for rules, code in (({"no_such_rule": 1}, "INVALID_RULES"), ({"monthly_start_day": 32}, "INVALID_MONTHLY_START_DAY"),
                        ({"pay_day_rule": "SOMETIMES"}, "INVALID_PAY_DAY_RULE")):
        r = acme.post(f"{BASE}/pay-groups", json={**biweekly, "code": "BAD", "pay_frequency": "MONTHLY",
                                                  "calendar_rules": rules})
        assert r.status_code == 400 and r.json()["code"] == code, (rules, r.text)
    r = acme.post(f"{BASE}/pay-groups", json={**biweekly, "code": "BAD", "pay_frequency": "FORTNIGHTLY"})
    assert r.status_code == 422
    r = acme.post(f"{BASE}/pay-groups", json={**biweekly, "code": "MONTHLY", "pay_frequency": "MONTHLY"})
    assert r.status_code == 409 and r.json()["code"] == "ORG_CODE_TAKEN"
    # Server-owned fields cannot be posted.
    r = acme.post(f"{BASE}/pay-groups", json={**biweekly, "code": "X", "company_id": 1})
    assert r.status_code == 422
    r = acme.patch(f"{BASE}/pay-groups/{weekly['id']}", json={"name": "Weekly crew", "description": "Site crew"})
    assert r.status_code == 200 and (r.json()["name"], r.json()["description"]) == ("Weekly crew", "Site crew")
    assert acme.get(f"{BASE}/pay-groups/{weekly['id']}").json()["name"] == "Weekly crew"
    audit = _sql("SELECT action, details FROM audit_logs WHERE company_id = :c AND entity_type = 'pay_group' "
                 "AND entity_id = :id ORDER BY id", c=acme.company_id, id=str(weekly["id"]))
    assert [a for a, _ in audit] == ["PAY_GROUP_CREATED", "PAY_GROUP_UPDATED"]
    assert audit[1][1]["before"]["name"] == "Weekly staff" and audit[1][1]["after"]["name"] == "Weekly crew"


@pytest.mark.parametrize("currency, code", [
    ("XAU", "NON_MONETARY_CURRENCY"),    # gold: an ISO code wages are never paid in
    ("XDR", "NON_MONETARY_CURRENCY"),
    ("CLF", "FUND_CURRENCY"),            # fund code
    ("ZWL", "HISTORIC_CURRENCY"),        # withdrawn (successor ZWG)
    ("ABC", "UNKNOWN_CURRENCY"),
    ("inr", "INVALID_CURRENCY_CODE"),
])
def test_ec_mon_03_pay_group_currency_must_be_a_payroll_currency(acme, currency, code):
    r = acme.post(f"{BASE}/pay-groups", json={"legal_entity_id": _entity(acme)["id"], "code": "G", "name": "G",
                                              "currency": currency, "pay_frequency": "MONTHLY"})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == code, r.json()


def test_pay_group_in_use_cannot_change_entity_currency_or_frequency_or_be_deactivated(acme):
    group = _by_code(acme, "pay-groups", "MONTHLY")
    for change in ({"currency": "USD"}, {"pay_frequency": "WEEKLY"}):
        r = acme.patch(f"{BASE}/pay-groups/{group['id']}", json=change)
        assert r.status_code == 409 and r.json()["code"] == "ORG_PAY_GROUP_LOCKED", change
    r = acme.patch(f"{BASE}/pay-groups/{group['id']}", json={"status": "INACTIVE"})
    assert r.status_code == 409 and r.json()["code"] == "ORG_DIMENSION_IN_USE"
    unused = _post(acme, "pay-groups", {"legal_entity_id": group["legal_entity_id"], "code": "SPARE", "name": "Spare",
                                        "pay_frequency": "MONTHLY"})
    r = acme.patch(f"{BASE}/pay-groups/{unused['id']}", json={"status": "INACTIVE", "currency": "USD"})
    assert r.status_code == 200 and (r.json()["status"], r.json()["currency"]) == ("INACTIVE", "USD")
    assert [g["code"] for g in acme.get(f"{BASE}/pay-groups?status=ACTIVE").json()] == ["MONTHLY"]
    gulf = _make_entity(acme, "AE01", "Acme Gulf", "AE", "AED", "Asia/Dubai")
    r = acme.patch(f"{BASE}/pay-groups/{unused['id']}", json={"legal_entity_id": gulf["id"]})
    assert r.status_code == 200 and r.json()["legal_entity_id"] == gulf["id"]


def test_pay_group_create_is_idempotent_with_an_idempotency_key(acme):
    body = {"legal_entity_id": _entity(acme)["id"], "code": "IDEM", "name": "Once", "pay_frequency": "MONTHLY"}
    first = acme.post(f"{BASE}/pay-groups", json=body, headers={"Idempotency-Key": "pg-1"})
    again = acme.post(f"{BASE}/pay-groups", json=body, headers={"Idempotency-Key": "pg-1"})
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and again.headers.get("Idempotent-Replayed") == "true"
    other = acme.post(f"{BASE}/pay-groups", json={**body, "name": "Twice"}, headers={"Idempotency-Key": "pg-1"})
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len([g for g in acme.get(f"{BASE}/pay-groups").json() if g["code"] == "IDEM"]) == 1


# ---------------------------------------------------------------------------
# Grades, cost centers, employment types, designations
# ---------------------------------------------------------------------------
def test_grade_salary_ranges_are_decimal_strings_per_payroll_currency(acme):
    grade = _post(acme, "grades", {"code": "G1", "name": "Grade 1", "rank": 1, "salary_ranges": [
        {"currency": "INR", "min": "300000", "mid": "450000.5", "max": "600000.00"},
        {"currency": "KWD", "min": "1000.125", "max": "2000"}]})
    assert grade["salary_ranges"] == [
        {"currency": "INR", "min": "300000.00", "mid": "450000.50", "max": "600000.00"},
        {"currency": "KWD", "min": "1000.125", "max": "2000.000"}]
    for ranges, status, code in (
        ([{"currency": "INR", "min": 1000, "max": "2000"}], 422, None),           # JSON number
        ([{"currency": "INR", "min": "3000", "max": "2000"}], 400, "GRADE_RANGE_INVALID"),
        ([{"currency": "INR", "min": "1", "mid": "5", "max": "2"}], 400, "GRADE_RANGE_INVALID"),
        ([{"currency": "INR", "min": "-1", "max": "2"}], 400, "GRADE_RANGE_INVALID"),
        ([{"currency": "INR", "min": "1.234", "max": "2"}], 400, "AMOUNT_NOT_ROUNDED"),
        ([{"currency": "XAU", "min": "1", "max": "2"}], 400, "NON_MONETARY_CURRENCY"),
        ([{"currency": "CLF", "min": "1", "max": "2"}], 400, "FUND_CURRENCY"),
        ([{"currency": "INR", "min": "1", "max": "2"}, {"currency": "INR", "min": "1", "max": "3"}], 400,
         "GRADE_RANGE_DUPLICATE"),
        ([{"currency": "INR", "min": "1", "max": "2", "avg": "1"}], 422, None),
    ):
        r = acme.post(f"{BASE}/grades", json={"code": "G2", "name": "Grade 2", "salary_ranges": ranges})
        assert r.status_code == status, (ranges, r.text)
        if code:
            assert r.json()["code"] == code, (ranges, r.json())
    r = acme.patch(f"{BASE}/grades/{grade['id']}", json={"salary_ranges": []})
    assert r.status_code == 200 and r.json()["salary_ranges"] == []


def test_cost_centers_employment_types_and_designations(app, acme):
    other = signup_company(app, "Other Co")
    entity = _entity(acme)
    center = _post(acme, "cost-centers", {"code": "CC-ENG", "name": "Engineering", "legal_entity_id": entity["id"]})
    assert center["legal_entity_id"] == entity["id"]
    r = acme.post(f"{BASE}/cost-centers", json={"code": "CC-X", "name": "X", "legal_entity_id": _entity(other)["id"]})
    assert r.status_code == 404  # EC-ORG-02: another tenant's entity
    contractor = _post(acme, "employment-types", {"code": "CONTRACT", "name": "Contractor", "worker_class": "CONTRACTOR"})
    assert contractor["worker_class"] == "CONTRACTOR"
    r = acme.post(f"{BASE}/employment-types", json={"code": "FREE", "name": "Freelance", "worker_class": "VENDOR"})
    assert r.status_code == 422
    r = acme.post(f"{BASE}/employment-types", json={"code": "FREE", "name": "Freelance"})
    assert r.status_code == 400 and r.json()["code"] == "ORG_WORKER_CLASS_INVALID"
    grade = _post(acme, "grades", {"code": "G1", "name": "Grade 1"})
    other_grade = _post(other, "grades", {"code": "G1", "name": "Grade 1"})  # EC-ORG-01: codes are per tenant
    designation = _post(acme, "designations", {"code": "SWE", "name": "Software Engineer", "grade_id": grade["id"]})
    assert designation["grade_id"] == grade["id"]
    r = acme.post(f"{BASE}/designations", json={"code": "SWE2", "name": "SWE 2", "grade_id": other_grade["id"]})
    assert r.status_code == 404
    acme.patch(f"{BASE}/grades/{grade['id']}", json={"status": "INACTIVE"})
    r = acme.post(f"{BASE}/designations", json={"code": "SWE3", "name": "SWE 3", "grade_id": grade["id"]})
    assert r.status_code == 400 and r.json()["code"] == "ORG_DIMENSION_INACTIVE"
    for bad in ({"code": "", "name": "x"}, {"code": "has space!", "name": "x"}, {"code": "OK", "name": "   "}):
        assert acme.post(f"{BASE}/designations", json=bad).status_code == 400, bad


# ---------------------------------------------------------------------------
# Work weeks, holiday calendars, branch links
# ---------------------------------------------------------------------------
def test_work_weeks_are_validated_with_the_workweek_rules(acme):
    week = _post(acme, "work-weeks", {"code": "MON_SAT_ALT", "name": "2nd and 4th Saturday off",
                                      "weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [4, 2]}]})
    assert (week["weekly_off_days"], week["nth_weekday_offs"]) == ([6], [{"weekday": 5, "nth": [2, 4]}])
    for body, code in (({"weekly_off_days": list(range(7))}, "NO_WORKING_DAYS"),
                       ({"weekly_off_days": [7]}, "INVALID_WEEKDAY"),
                       ({"weekly_off_days": [6, 6]}, "INVALID_PATTERN"),
                       ({"weekly_off_days": [5], "nth_weekday_offs": [{"weekday": 5, "nth": [2]}]}, "REDUNDANT_RULE"),
                       ({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [9]}]}, "INVALID_OCCURRENCE")):
        r = acme.post(f"{BASE}/work-weeks", json={"code": "BAD", "name": "Bad", **body})
        assert r.status_code == 400 and r.json()["code"] == code, (body, r.text)
    r = acme.patch(f"{BASE}/work-weeks/{week['id']}", json={"weekly_off_days": [4]})
    assert r.status_code == 200 and r.json()["weekly_off_days"] == [4]
    mon_fri = _by_code(acme, "work-weeks", "MON_FRI")
    r = acme.patch(f"{BASE}/work-weeks/{mon_fri['id']}", json={"status": "INACTIVE"})
    assert r.status_code == 409 and r.json()["code"] == "ORG_DIMENSION_IN_USE"
    r = acme.patch(f"{BASE}/work-weeks/{mon_fri['id']}", json={"weekly_off_days": [6], "name": "Mon to Sat"})
    assert r.status_code == 200 and r.json()["weekly_off_days"] == [6]  # the starter week is editable


def test_holiday_calendar_parents_cycles_and_regions(app, acme):
    other = signup_company(app, "Other Co")
    company = _by_code(acme, "holiday-calendars", "COMPANY")
    india = _post(acme, "holiday-calendars", {"code": "IN", "name": "India", "parent_calendar_id": company["id"],
                                              "country_code": "in"})
    assert (india["parent_calendar_id"], india["country_code"]) == (company["id"], "IN")
    mh = _post(acme, "holiday-calendars", {"code": "IN-MH", "name": "Maharashtra", "parent_calendar_id": india["id"],
                                           "country_code": "IN", "subdivision_code": "IN-MH"})
    r = acme.patch(f"{BASE}/holiday-calendars/{company['id']}", json={"parent_calendar_id": mh["id"]})
    assert r.status_code == 400 and r.json()["code"] == "ORG_CALENDAR_CYCLE"
    r = acme.patch(f"{BASE}/holiday-calendars/{india['id']}", json={"parent_calendar_id": india["id"]})
    assert r.status_code == 400 and r.json()["code"] == "ORG_CALENDAR_CYCLE"
    r = acme.patch(f"{BASE}/holiday-calendars/{india['id']}",
                   json={"parent_calendar_id": _by_code(other, "holiday-calendars", "COMPANY")["id"]})
    assert r.status_code == 404
    for body, code in (({"country_code": "QQ"}, "ORG_COUNTRY_INVALID"),
                       ({"country_code": "IN", "subdivision_code": "AE-DU"}, "ORG_SUBDIVISION_INVALID"),
                       ({"subdivision_code": "IN-XX"}, "ORG_SUBDIVISION_INVALID")):
        r = acme.post(f"{BASE}/holiday-calendars", json={"code": "BAD", "name": "Bad", **body})
        assert r.status_code == 400 and r.json()["code"] == code, (body, r.text)
    r = acme.patch(f"{BASE}/holiday-calendars/{india['id']}", json={"status": "INACTIVE"})
    assert r.status_code == 409  # Maharashtra inherits from it
    r = acme.patch(f"{BASE}/holiday-calendars/{mh['id']}", json={"parent_calendar_id": None})
    assert r.status_code == 200 and r.json()["parent_calendar_id"] is None


def test_branch_calendar_and_work_week_links(app, acme):
    other = signup_company(app, "Other Co")
    branch = acme.get("/api/v1/branches").json()[0]
    calendar = _post(acme, "holiday-calendars", {"code": "MAIN", "name": "Main office"})
    week = _post(acme, "work-weeks", {"code": "SIX", "name": "Six days", "weekly_off_days": [6]})
    r = acme.put(f"{BASE}/branches/{branch['id']}/calendar",
                 json={"holiday_calendar_id": calendar["id"], "work_week_pattern_id": week["id"]})
    assert r.status_code == 200 and (r.json()["holiday_calendar_id"], r.json()["work_week_pattern_id"]) == (
        calendar["id"], week["id"])
    listed = acme.get("/api/v1/branches").json()[0]
    assert (listed["holiday_calendar_id"], listed["work_week_pattern_id"]) == (calendar["id"], week["id"])
    r = acme.put(f"{BASE}/branches/{branch['id']}/calendar", json={"holiday_calendar_id": None})
    assert r.status_code == 400 and r.json()["code"] == "ORG_BRANCH_LINK_REQUIRED"
    foreign = _by_code(other, "holiday-calendars", "COMPANY")
    assert acme.put(f"{BASE}/branches/{branch['id']}/calendar", json={"holiday_calendar_id": foreign["id"]}).status_code == 404
    other_branch = other.get("/api/v1/branches").json()[0]
    assert acme.put(f"{BASE}/branches/{other_branch['id']}/calendar",
                    json={"holiday_calendar_id": calendar["id"]}).status_code == 404
    spare = _post(acme, "holiday-calendars", {"code": "SPARE", "name": "Spare"})
    acme.patch(f"{BASE}/holiday-calendars/{spare['id']}", json={"status": "INACTIVE"})
    r = acme.put(f"{BASE}/branches/{branch['id']}/calendar", json={"holiday_calendar_id": spare["id"]})
    assert r.status_code == 400 and r.json()["code"] == "ORG_DIMENSION_INACTIVE"


def test_branches_and_holidays_created_by_existing_code_get_calendars_and_work_weeks(acme):
    company = _by_code(acme, "holiday-calendars", "COMPANY")
    week = _by_code(acme, "work-weeks", "MON_FRI")
    entity = _entity(acme)
    r = acme.post("/api/v1/branches", json={"legal_entity_id": entity["id"], "code": "PNQ", "name": "Pune"})
    assert r.status_code == 200 and (r.json()["holiday_calendar_id"], r.json()["work_week_pattern_id"]) == (
        company["id"], week["id"])
    create_employee(acme, "Remote", location="Nagpur")  # a free-text location becomes a branch
    nagpur = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Nagpur")
    assert (nagpur["holiday_calendar_id"], nagpur["work_week_pattern_id"]) == (company["id"], week["id"])

    for body in ({"name": "New Year", "date": "2027-01-01"},
                 {"name": "Ganesh Chaturthi", "date": "2027-09-04", "location": "Pune", "is_optional": True},
                 {"name": "Pune Day", "date": "2027-09-05", "location": "Pune"},
                 {"name": "Nowhere Day", "date": "2027-09-06", "location": "Atlantis"}):
        assert acme.post("/api/v1/holidays", json=body).status_code == 200, body
    rows = {name: (calendar_id, holiday_type) for name, calendar_id, holiday_type in _sql(
        "SELECT name, calendar_id, holiday_type FROM holidays WHERE company_id = :c", c=acme.company_id)}
    calendars = {c["code"]: c for c in acme.get(f"{BASE}/holiday-calendars").json()}
    pune_cal = calendars["BR-PNQ"]
    assert pune_cal["parent_calendar_id"] == company["id"]  # Pune's own calendar inherits the company holidays
    assert rows == {"New Year": (company["id"], "COMPANY"), "Ganesh Chaturthi": (pune_cal["id"], "OPTIONAL"),
                    "Pune Day": (pune_cal["id"], "COMPANY"), "Nowhere Day": (calendars["UNMATCHED"]["id"], "COMPANY")}
    pune = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Pune")
    assert pune["holiday_calendar_id"] == pune_cal["id"]
    main = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Main")
    assert main["holiday_calendar_id"] == company["id"]  # other branches never see Pune's holidays


def test_branch_rename_keeps_legacy_holiday_locations_in_sync_d7(acme):
    branch = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Main")
    assert acme.post("/api/v1/holidays", json={"name": "Founders Day", "date": "2027-03-01",
                                               "location": "Main"}).status_code == 200
    r = acme.patch(f"/api/v1/branches/{branch['id']}", json={"name": "Head Office"})
    assert r.status_code == 200, r.text
    holidays = acme.get("/api/v1/holidays?year=2027&location=Head Office").json()
    assert [(h["name"], h["location"]) for h in holidays] == [("Founders Day", "Head Office")]
    assert acme.get("/api/v1/holidays?year=2027&location=Main").json() == []


# ---------------------------------------------------------------------------
# Authorization and tenant isolation
# ---------------------------------------------------------------------------
KINDS = (
    ("pay-groups", lambda c: {"legal_entity_id": _entity(c)["id"], "code": "PG", "name": "PG", "pay_frequency": "MONTHLY"}),
    ("grades", lambda c: {"code": "GR", "name": "GR"}),
    ("cost-centers", lambda c: {"code": "CC", "name": "CC"}),
    ("employment-types", lambda c: {"code": "ET", "name": "ET", "worker_class": "INTERN"}),
    ("designations", lambda c: {"code": "DS", "name": "DS"}),
    ("work-weeks", lambda c: {"code": "WW", "name": "WW", "weekly_off_days": [6]}),
    ("holiday-calendars", lambda c: {"code": "HC", "name": "HC"}),
)


def test_ec_org_02_every_dimension_is_invisible_and_immutable_across_three_tenants(app):
    tenants = [signup_company(app, n) for n in ("Alpha Co", "Beta Co", "Gamma Co")]
    ids = {}
    for t in tenants:
        for path, body in KINDS:
            ids[(t.company_id, path)] = _post(t, path, body(t))["id"]  # EC-ORG-01: same codes in every tenant
    a, b, c = tenants
    for path, body in KINDS:
        theirs = ids[(a.company_id, path)]
        for intruder in (b, c):
            assert intruder.get(f"{BASE}/{path}/{theirs}").status_code == 404, path
            assert intruder.patch(f"{BASE}/{path}/{theirs}", json={"name": "Hijacked"}).status_code == 404, path
            assert theirs not in {x["id"] for x in intruder.get(f"{BASE}/{path}").json()}, path
        assert a.get(f"{BASE}/{path}/{theirs}").json()["name"] != "Hijacked"
    assert b.put(f"{BASE}/branches/{a.get('/api/v1/branches').json()[0]['id']}/calendar",
                 json={"work_week_pattern_id": ids[(b.company_id, "work-weeks")]}).status_code == 404


def test_reads_need_org_read_and_writes_need_manage_dimensions(app, acme):
    staff = invite_and_activate(app, acme, create_employee(acme, "Staff"), ["employee"])
    hr = invite_and_activate(app, acme, create_employee(acme, "Hr"), ["hr_admin"])
    group = _by_code(acme, "pay-groups", "MONTHLY")
    for path, body in KINDS:
        assert staff.get(f"{BASE}/{path}").status_code == 403, path
        assert staff.post(f"{BASE}/{path}", json=body(acme)).status_code == 403, path
        assert hr.get(f"{BASE}/{path}").status_code == 200, path
    assert staff.get(f"{BASE}/pay-groups/{group['id']}").status_code == 403
    assert staff.patch(f"{BASE}/pay-groups/{group['id']}", json={"name": "x"}).status_code == 403
    assert staff.put(f"{BASE}/branches/1/calendar", json={}).status_code == 403
    assert _post(hr, "grades", {"code": "HR1", "name": "By HR"})["code"] == "HR1"
    # A role with org:read only reads.
    role = acme.post("/api/v1/roles", json={"name": "Org viewer", "grants": [{"permission": "org:read", "scope": "COMPANY"}]})
    viewer = invite_and_activate(app, acme, create_employee(acme, "Viewer"), [])
    acme.put(f"/api/v1/users/{viewer.user_id}/roles", json={"role_ids": [role.json()["id"]]})
    assert viewer.get(f"{BASE}/pay-groups").status_code == 200
    assert viewer.post(f"{BASE}/grades", json={"code": "V", "name": "V"}).status_code == 403


def test_entity_scoped_org_read_sees_only_its_entities_pay_groups_and_cost_centers(app, acme):
    india = _entity(acme)
    gulf = _make_entity(acme, "AE01", "Acme Gulf", "AE", "AED", "Asia/Dubai")
    gulf_group = _post(acme, "pay-groups", {"legal_entity_id": gulf["id"], "code": "AE-M", "name": "Gulf monthly",
                                            "pay_frequency": "MONTHLY"})
    india_group = _by_code(acme, "pay-groups", "MONTHLY")
    shared = _post(acme, "cost-centers", {"code": "CC-ALL", "name": "Shared"})
    gulf_cc = _post(acme, "cost-centers", {"code": "CC-AE", "name": "Gulf", "legal_entity_id": gulf["id"]})
    india_cc = _post(acme, "cost-centers", {"code": "CC-IN", "name": "India", "legal_entity_id": india["id"]})
    role = acme.post("/api/v1/roles", json={"name": "Gulf org", "grants": [{"permission": "org:read", "scope": "LEGAL_ENTITY"}]})
    viewer = invite_and_activate(app, acme, create_employee(acme, "GulfViewer"), [])
    acme.put(f"/api/v1/users/{viewer.user_id}/roles", json={"role_ids": [role.json()["id"]]})
    assert viewer.get(f"{BASE}/pay-groups").json() == []  # EC-ORG-12: no targets, no reach
    r = acme.put(f"/api/v1/users/{viewer.user_id}/scope-targets",
                 json={"assignments": [{"role_id": role.json()["id"], "legal_entity_ids": [gulf["id"]]}]})
    assert r.status_code == 200, r.text
    assert [g["id"] for g in viewer.get(f"{BASE}/pay-groups").json()] == [gulf_group["id"]]
    assert viewer.get(f"{BASE}/pay-groups/{india_group['id']}").status_code == 404
    assert {c["id"] for c in viewer.get(f"{BASE}/cost-centers").json()} == {shared["id"], gulf_cc["id"]}
    assert viewer.get(f"{BASE}/cost-centers/{india_cc['id']}").status_code == 404
    assert viewer.get(f"{BASE}/cost-centers/{gulf_cc['id']}").status_code == 200


# ---------------------------------------------------------------------------
# PAY_GROUP scope targets and delegation (audit 01 D-13)
# ---------------------------------------------------------------------------
def _pay_group_clerk(app, acme):
    role = acme.post("/api/v1/roles", json={"name": "Run reader", "grants": [
        {"permission": "payroll:run:read", "scope": "PAY_GROUP"}, {"permission": "employee:read", "scope": "LEGAL_ENTITY"}]})
    assert role.status_code == 200, role.text
    clerk = invite_and_activate(app, acme, create_employee(acme, "Clerk"), [])
    assert acme.put(f"/api/v1/users/{clerk.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    return role.json()["id"], clerk


def test_scope_targets_accept_and_return_pay_groups_of_the_company(app, acme):
    other = signup_company(app, "Other Co")
    role, clerk = _pay_group_clerk(app, acme)
    group = _by_code(acme, "pay-groups", "MONTHLY")
    url = f"/api/v1/users/{clerk.user_id}/scope-targets"
    r = acme.put(url, json={"assignments": [{"role_id": role, "pay_group_ids": [group["id"]]}]})
    assert r.status_code == 200 and r.json()[0]["pay_group_ids"] == [group["id"]], r.text
    assert acme.get(url).json()[0]["pay_group_ids"] == [group["id"]]
    assert ("payroll:run:read", "PAY_GROUP", group["id"]) in {tuple(x) for x in _sql(
        "SELECT p.permission_code, t.scope, t.target_id FROM user_role_assignment_targets t "
        "JOIN user_role_assignments a ON a.id = t.user_role_assignment_id "
        "JOIN role_permissions p ON p.role_id = a.role_id AND p.scope = t.scope WHERE a.user_id = :u", u=clerk.user_id)}
    foreign = _by_code(other, "pay-groups", "MONTHLY")
    r = acme.put(url, json={"assignments": [{"role_id": role, "pay_group_ids": [foreign["id"]]}]})
    assert r.status_code == 404
    assert acme.get(url).json()[0]["pay_group_ids"] == [group["id"]]
    entity_only = acme.post("/api/v1/roles", json={"name": "Entity HR", "grants": [
        {"permission": "employee:read", "scope": "LEGAL_ENTITY"}]}).json()["id"]
    acme.put(f"/api/v1/users/{clerk.user_id}/roles", json={"role_ids": [role, entity_only]})
    r = acme.put(url, json={"assignments": [{"role_id": entity_only, "pay_group_ids": [group["id"]]}]})
    assert r.status_code == 400  # that role has no PAY_GROUP-scoped permissions
    audit = _sql("SELECT details FROM audit_logs WHERE company_id = :c AND action = 'SCOPE_TARGETS' ORDER BY id",
                 c=acme.company_id)
    assert audit[0][0]["after"]["PAY_GROUP"] == [group["id"]]


def test_d13_removing_targets_needs_the_same_delegation_as_adding_them(app, acme):
    role, clerk = _pay_group_clerk(app, acme)
    group = _by_code(acme, "pay-groups", "MONTHLY")
    entity = _entity(acme)
    hr = invite_and_activate(app, acme, create_employee(acme, "Hr"), ["hr_admin"])  # no payroll rights
    url = f"/api/v1/users/{clerk.user_id}/scope-targets"
    r = hr.put(url, json={"assignments": [{"role_id": role, "pay_group_ids": [group["id"]]}]})
    assert r.status_code == 403  # adding a payroll target
    assert acme.put(url, json={"assignments": [{"role_id": role, "pay_group_ids": [group["id"]],
                                                "legal_entity_ids": [entity["id"]]}]}).status_code == 200
    r = hr.put(url, json={"assignments": [{"role_id": role, "legal_entity_ids": [entity["id"]]}]})
    assert r.status_code == 403  # removing it (D-13)
    assert acme.get(url).json()[0]["pay_group_ids"] == [group["id"]]
    # hr holds employee:read company-wide: changing entity targets while keeping pay groups is allowed.
    r = hr.put(url, json={"assignments": [{"role_id": role, "pay_group_ids": [group["id"]], "legal_entity_ids": []}]})
    assert r.status_code == 200 and r.json()[0]["legal_entity_ids"] == [] and r.json()[0]["pay_group_ids"] == [group["id"]]
    assert acme.put(url, json={"assignments": [{"role_id": role}]}).status_code == 200
    assert acme.get(url).json()[0]["pay_group_ids"] == []
