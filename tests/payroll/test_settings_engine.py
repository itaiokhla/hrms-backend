"""Settings engine v2: resolution, values per scope, change control, API and the legacy facade.

3.1 §2, §3.3, §7, §9; 02 §14; audit 01 §1 Q7 and §6.4. EC-CFG-01, -02, -04, -06, -08, -09, -10, -11,
-12, -13, -15, -17, -18, -19, -20.
"""

import calendar
import importlib
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.payroll import settings_registry as reg
from tests.helpers import PASSWORD, ApiClient, create_employee, invite_and_activate, signup_company

VALUES = "/api/v1/payroll/settings/values"
RESOLVE = "/api/v1/payroll/settings/resolve"
DEFINITIONS = "/api/v1/payroll/settings/definitions"
REQUESTS = "/api/v1/payroll/settings/change-requests"
TEST_KEY = "payroll.test_only.every_level"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


def session(company_id):
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    bind_tenant(db, company_id)
    return db


def today_of(company_id):
    from app.core.clock import company_today

    db = session(company_id)
    try:
        return company_today(db, company_id)
    finally:
        db.close()


def change(key, value, **extra):
    return {"key": key, "value": value, **extra}


def put(client, *changes, headers=None):
    return client.put(VALUES, json={"changes": list(changes)}, headers=headers)


def values(client, **params):
    r = client.get(VALUES, params=params)
    assert r.status_code == 200, r.text
    return {item["key"]: item for item in r.json()["items"]}


def token(client, key, **params):
    return values(client, **params)[key]["row_version"]


def default_placement(company_id):
    return sql("SELECT legal_entity_id, branch_id FROM employee_assignments WHERE company_id = :c ORDER BY id LIMIT 1",
               c=company_id)[0]


def make_entity(client, code, country="AE", currency="AED", tz="Asia/Dubai"):
    r = client.post("/api/v1/legal-entities", json={"code": code, "legal_name": f"{code} Ltd", "country_code": country,
                                                    "base_currency": currency, "timezone": tz})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def make_branch(client, entity_id, code):
    r = client.post("/api/v1/branches", json={"legal_entity_id": entity_id, "code": code, "name": f"Branch {code}"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def make_pay_group(company_id, entity_id, code, currency="AED"):
    return sql("INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency) "
               "VALUES (:c, :le, :code, :code, :cur, 'MONTHLY') RETURNING id",
               c=company_id, le=entity_id, code=code, cur=currency)[0][0]


def place(company_id, employee_id, entity_id, branch_id, pay_group_id=None):
    sql("UPDATE employee_assignments SET legal_entity_id = :le, branch_id = :b, pay_group_id = :pg "
        "WHERE company_id = :c AND employee_id = :e AND is_primary", c=company_id, e=employee_id, le=entity_id,
        b=branch_id, pg=pay_group_id)


def invite_with_role_ids(app, admin, employee, role_ids):
    r = admin.post("/api/v1/users/invite", json={"email": employee["email"], "full_name": employee["first_name"],
                                                "employee_id": employee["id"], "role_ids": role_ids})
    assert r.status_code == 200, r.text
    client = ApiClient(app)
    r = client.post("/api/v1/auth/invitations/accept", json={"token": r.json()["invite_url"].split("token=")[1],
                                                             "password": PASSWORD})
    assert r.status_code == 200, r.text
    client.user_id = r.json()["me"]["user"]["id"]
    return client


def second_admin(app, founder, name="Second"):
    return invite_and_activate(app, founder, create_employee(founder, name), ["company_admin", "payroll_manager"])


def next_month(company_id, day):
    today = today_of(company_id)
    first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first.replace(day=day), first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


@pytest.fixture
def every_level_key(monkeypatch):
    """A test-only payroll key allowed at every scope level (no real key is allowed at both BRANCH and PAY_GROUP)."""
    defn = reg.SettingDef(
        key=TEST_KEY, type="int", default=1, allowed_levels=tuple(reg.SCOPE_LEVELS), module=reg.PAYROLL_MODULE,
        section="General & calendars", label="Test value", help="Test only.", resolution_grain="DAY",
        constraints={"min": 0, "max": 1000}, ui={"control": "number", "order": 9999},
    )
    monkeypatch.setitem(reg.REGISTRY, TEST_KEY, defn)
    return TEST_KEY


@pytest.fixture
def org(app):
    """Acme with a second legal entity (AE), branch and pay group, and an employee placed there."""
    acme = signup_company(app, "Acme Corp")
    cid = acme.company_id
    le1, br1 = default_placement(cid)
    le2 = make_entity(acme, "AE01")
    br2 = make_branch(acme, le2, "DXB")
    pg2 = make_pay_group(cid, le2, "AE-MONTHLY")
    worker = create_employee(acme, "Worker")
    place(cid, worker["id"], le2, br2, pg2)
    stay = create_employee(acme, "Stayer")
    return {"admin": acme, "cid": cid, "le1": le1, "br1": br1, "le2": le2, "br2": br2, "pg2": pg2,
            "worker": worker["id"], "stayer": stay["id"]}


@pytest.fixture
def ae_law(monkeypatch):
    """A test country-pack hook: AE requires wage inputs ON and a half-day value of at least 0.5."""
    packs = importlib.import_module("app.payroll.packs")

    def setting_constraints(country_code, on):
        if country_code != "AE":
            return ()
        return (
            reg.LegalConstraint("payroll.wage_inputs.enabled", "Test Labour Law art. 1", required_value=True),
            reg.LegalConstraint("payroll.attendance.half_day_value", "Test Labour Law art. 2", minimum=reg.Decimal("0.5")),
        )

    monkeypatch.setattr(packs, "setting_constraints", setting_constraints, raising=False)


# ---------------------------------------------------------------------------
# definitions, values at a scope, authorization
# ---------------------------------------------------------------------------
def test_definitions_are_served_with_an_etag_and_honour_if_none_match(app):
    acme = signup_company(app, "Defs Co")
    r = acme.get(DEFINITIONS, params={"module": "payroll"})
    assert r.status_code == 200, r.text
    etag = r.headers["ETag"]
    assert etag == f'"{reg.registry_fingerprint()}"'
    body = r.json()
    assert body["registry_version"] == reg.REGISTRY_VERSION
    assert {d["module"] for d in body["definitions"]} == {"Payroll"}
    assert any(d["key"] == "payroll.migration.legacy_review_acknowledged" for d in body["definitions"])
    assert {t["code"] for t in body["policy_types"]} >= {"OVERTIME_POLICY", "LEAVE_PAY_POLICY"}
    again = acme.get(DEFINITIONS, params={"module": "payroll"}, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.headers["ETag"] == etag


def test_values_at_tenant_scope_carry_badges_and_row_versions(app):
    acme = signup_company(app, "Badge Co")
    items = values(acme)
    basis = items["payroll.lop.proration_basis"]
    assert basis["value"] == "CALENDAR_DAYS" and basis["level"] == "TENANT"
    assert basis["badge"]["code"] == "SET_HERE" and basis["row_version"] == 1 and basis["editable"]
    assert basis["here"]["source"] == "STARTER_PROFILE" and basis["here"]["profile_version"] == "STARTER_V1"
    assert items["payroll.approval.separation_of_duties"]["here"]["locked"] is True
    assert items["payroll.standard_hours_per_day"]["editable"] is False  # PAY_GROUP and EMPLOYEE only
    assert items["payroll.standard_hours_per_day"]["badge"] == {"code": "INHERITED", "label": "Inherited from platform default",
                                                               "level": "PLATFORM"}
    only = values(acme, module="payroll", section="overtime")
    assert "payroll.overtime.enabled" in only and "payroll.enabled" not in only


def test_authorization_payroll_configuration_permissions_are_required(app):
    acme = signup_company(app, "Authz Co")
    employee = invite_and_activate(app, acme, create_employee(acme, "Plain"), ["employee"])
    processor = invite_and_activate(app, acme, create_employee(acme, "Proc"), ["payroll_processor"])
    for url in (DEFINITIONS, VALUES, f"{RESOLVE}?key=payroll.enabled", REQUESTS):
        assert employee.get(url).status_code == 403, url
    assert put(employee, change("payroll.enabled", False)).status_code == 403
    assert processor.get(VALUES).status_code == 200  # configuration:read
    assert put(processor, change("payroll.enabled", False, row_version=1)).status_code == 403
    assert processor.post(f"{REQUESTS}/1/approve").status_code == 403
    assert processor.get(REQUESTS).status_code == 200


def test_strict_requests_and_registry_validation(app):
    acme = signup_company(app, "Strict Co")
    assert acme.put(VALUES, json={"changes": [{"key": "payroll.enabled", "value": True, "company_id": 9}]}).status_code == 422
    assert acme.put(VALUES, json={"changes": []}).status_code == 422
    r = put(acme, change("payroll.attendance.half_day_value", 0.5, row_version=1))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_TYPE"  # decimals are strings, never JSON numbers
    assert put(acme, change("payroll.nope", True)).json()["code"] == "UNKNOWN_SETTING"
    r = put(acme, change("payroll.standard_hours_per_day", "7.5"))
    assert r.status_code == 400 and r.json()["code"] == "LEVEL_NOT_ALLOWED"
    r = put(acme, change("payroll.accounting.enabled", True, row_version=1))
    assert r.status_code == 400 and r.json()["code"] == "FEATURE_NOT_AVAILABLE"
    r = put(acme, change("payroll.lop.proration_basis", "WORKING_DAYS", row_version=1),
            change("payroll.lop.proration_basis", "FIXED_30", row_version=1))
    assert r.status_code == 400 and r.json()["code"] == "DUPLICATE_CHANGE"


# ---------------------------------------------------------------------------
# resolution: precedence, locks, explain (EC-CFG-01, EC-CFG-04)
# ---------------------------------------------------------------------------
def test_ec_cfg_01_precedence_employee_pay_group_branch_entity_tenant(app, org, every_level_key):
    from app.payroll.services import settings as engine

    admin, cid = org["admin"], org["cid"]
    r = put(admin, change(TEST_KEY, 10), change(TEST_KEY, 20, scope_type="LEGAL_ENTITY", scope_id=org["le2"]),
            change(TEST_KEY, 30, scope_type="BRANCH", scope_id=org["br2"]),
            change(TEST_KEY, 40, scope_type="PAY_GROUP", scope_id=org["pg2"]),
            change(TEST_KEY, 50, scope_type="EMPLOYEE", scope_id=org["worker"]))
    assert r.status_code == 200, r.text
    assert [c["status"] for c in r.json()["changes"]] == ["ACTIVE"] * 5
    db = session(cid)
    try:
        on = today_of(cid)
        assert engine.resolve(db, cid, TEST_KEY, employee_id=org["worker"], on=on).value == 50
        both = engine.resolve(db, cid, TEST_KEY, branch_id=org["br2"], pay_group_id=org["pg2"], on=on)
        assert (both.value, both.level) == (40, "PAY_GROUP")  # EC-CFG-01: the pay group wins over the branch
        assert {"level": "BRANCH", "reason": "OVERRIDDEN", "by": "PAY_GROUP"}.items() <= next(
            s for s in both.shadowed if s["level"] == "BRANCH").items()
        assert engine.resolve(db, cid, TEST_KEY, branch_id=org["br2"], on=on).value == 30
        assert engine.resolve(db, cid, TEST_KEY, legal_entity_id=org["le2"], on=on).value == 20
        assert engine.resolve(db, cid, TEST_KEY, employee_id=org["stayer"], on=on).value == 10
        assert engine.resolve(db, cid, TEST_KEY, on=on).value == 10
        many = engine.resolve_many(db, cid, [TEST_KEY, "graceTime"], employee_id=org["worker"], on=on)
        assert many[TEST_KEY].value == 50 and many["graceTime"].value == 15  # aliases resolve to their v2 key
    finally:
        db.close()

    explain = admin.get(RESOLVE, params={"key": TEST_KEY, "employee_id": org["worker"]})
    assert explain.status_code == 200, explain.text
    body = explain.json()
    assert body["value"] == 50 and body["level"] == "EMPLOYEE"
    levels = {lv["level"]: lv for lv in body["levels"]}
    assert [lv["level"] for lv in body["levels"]] == [lv.value for lv in reg.PRECEDENCE]
    assert levels["EMPLOYEE"]["status"] == "EFFECTIVE"
    assert {levels[k]["status"] for k in ("PAY_GROUP", "BRANCH", "LEGAL_ENTITY", "TENANT")} == {"OVERRIDDEN"}
    assert levels["PAY_GROUP"]["scope_id"] == org["pg2"] and levels["TENANT"]["value"] == 10
    assert levels["TENANT"]["set_by"]["email"] == admin.email
    assert levels["PLATFORM"]["value"] == 1 and body["context"]["country_code"] == "AE"


def test_ec_cfg_04_tenant_lock_beats_lower_overrides_and_blocks_new_ones(app, org, every_level_key):
    admin = org["admin"]
    assert put(admin, change(TEST_KEY, 20, scope_type="LEGAL_ENTITY", scope_id=org["le2"]),
               change(TEST_KEY, 50, scope_type="EMPLOYEE", scope_id=org["worker"])).status_code == 200
    r = put(admin, change(TEST_KEY, 99, locked=True))
    assert r.status_code == 200, r.text
    body = admin.get(RESOLVE, params={"key": TEST_KEY, "employee_id": org["worker"]}).json()
    assert (body["value"], body["level"], body["locked_by"]) == (99, "TENANT", "TENANT")
    levels = {lv["level"]: lv["status"] for lv in body["levels"]}
    assert levels["EMPLOYEE"] == levels["LEGAL_ENTITY"] == "LOCKED_ABOVE"
    item = values(admin, scope_type="LEGAL_ENTITY", scope_id=org["le2"])[TEST_KEY]
    assert item["badge"] == {"code": "LOCKED", "label": "Locked by tenant", "level": "TENANT"}
    assert item["here"]["value"] == 20 and item["shadowed"]
    r = put(admin, change(TEST_KEY, 21, scope_type="LEGAL_ENTITY", scope_id=org["le2"], row_version=item["row_version"]))
    assert r.status_code == 400 and r.json()["code"] == "SETTING_LOCKED"


# ---------------------------------------------------------------------------
# writes: dating, notices, ranges, concurrency, admin-only, maker-checker
# ---------------------------------------------------------------------------
def test_ec_cfg_02_notice_says_what_the_effective_date_means(app):
    acme = signup_company(app, "Notice Co")
    day, first, last = next_month(acme.company_id, 20)
    r = put(acme, change("payroll.lop.proration_basis", "WORKING_DAYS", effective_from=day.isoformat(), row_version=1),
            change("attendance.grace_minutes", 5, effective_from=day.isoformat(), row_version=1))
    assert r.status_code == 200, r.text
    period, daily = r.json()["changes"]
    assert period["message"] == f"Applies to the whole {day:%B %Y} period."
    assert daily["message"] == f"Applies from 20 {day:%B %Y}."
    assert period["resolution_grain"] == "PERIOD" and daily["resolution_grain"] == "DAY"
    # Scheduled rows: today's value stays in force until the effective date.
    assert values(acme)["payroll.lop.proration_basis"]["value"] == "CALENDAR_DAYS"
    assert values(acme, as_of=day.isoformat())["payroll.lop.proration_basis"]["value"] == "WORKING_DAYS"
    rows = sql("SELECT value, effective_from, effective_to, status, row_version FROM setting_values WHERE company_id = :c "
               "AND key = 'payroll.lop.proration_basis' ORDER BY id", c=acme.company_id)
    assert rows[0][2] == day - timedelta(days=1) and rows[0][4] == 2
    assert rows[1][:4] == ("WORKING_DAYS", day, None, "ACTIVE") and rows[1][4] == 2


def test_ec_cfg_08_ec_cfg_18_day_grain_ranges_follow_each_day_and_period_grain_the_period_end(app, org):
    from app.payroll.services import settings as engine

    admin, cid = org["admin"], org["cid"]
    day, first, last = next_month(cid, 20)
    move = first + timedelta(days=9)
    assert put(admin, change("attendance.grace_minutes", 5, effective_from=day.isoformat(), row_version=1),
               change("attendance.grace_minutes", 9, scope_type="LEGAL_ENTITY", scope_id=org["le2"]),
               change("payroll.lop.proration_basis", "WORKING_DAYS", effective_from=day.isoformat(),
                      row_version=1)).status_code == 200
    # The stayer moves to the AE entity on the 10th.
    sql("UPDATE employee_assignments SET effective_to = :to WHERE company_id = :c AND employee_id = :e",
        c=cid, e=org["stayer"], to=move - timedelta(days=1))
    sql("INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, is_primary, reason, "
        "effective_from, created_at, updated_at) VALUES (:c, :e, :le, :b, true, 'TRANSFER', :f, now(), now())",
        c=cid, e=org["stayer"], le=org["le2"], b=org["br2"], f=move)
    db = session(cid)
    try:
        tenant = engine.resolve_ranges(db, cid, "attendance.grace_minutes", start=first, end=last)
        assert tenant == [(first, day - timedelta(days=1), 15), (day, last, 5)]  # EC-CFG-08: days 1-19 keep 15
        mover = engine.resolve_ranges(db, cid, "graceTime", employee_id=org["stayer"], start=first, end=last)
        assert mover == [(first, move - timedelta(days=1), 15), (move, last, 9)]
        period = engine.resolve_ranges(db, cid, "payroll.lop.proration_basis", start=first, end=last)
        assert period == [(first, last, "WORKING_DAYS")]  # EC-CFG-02: the period-end value for the whole period
    finally:
        db.close()


def test_past_dated_change_needs_a_reason_and_supersedes_from_its_date(app):
    acme = signup_company(app, "Backdate Co")
    cid = acme.company_id
    yesterday = today_of(cid) - timedelta(days=1)
    r = put(acme, change("attendance.grace_minutes", 12, effective_from=yesterday.isoformat(), row_version=1))
    assert r.status_code == 400 and r.json()["code"] == "REASON_REQUIRED"
    r = put(acme, change("attendance.grace_minutes", 12, effective_from=yesterday.isoformat(), row_version=1,
                         reason="Agreed with the works council"))
    assert r.status_code == 200, r.text
    rows = sql("SELECT value, effective_from, effective_to, status, reason FROM setting_values WHERE company_id = :c "
               "AND key = 'attendance.grace_minutes' ORDER BY id", c=cid)
    assert rows[0][3] == "SUPERSEDED"  # the starter row started after the new date
    assert rows[1] == (12, yesterday, None, "ACTIVE", "Agreed with the works council")
    assert values(acme)["attendance.grace_minutes"]["value"] == 12
    audit = sql("SELECT module, details FROM audit_logs WHERE company_id = :c AND action = 'SETTING_CHANGED' "
                "ORDER BY id", c=cid)
    assert audit[-1][0] == "Settings"  # attendance keys are company settings
    details = audit[-1][1]
    assert (details["key"], details["old"], details["new"], details["effective_from"], details["reason"]) == (
        "attendance.grace_minutes", 15, 12, yesterday.isoformat(), "Agreed with the works council")
    assert details["scope"] == {"scope_type": "TENANT", "scope_id": None, "name": "Company"}


def test_ec_cfg_10_second_admin_with_a_stale_row_version_gets_409_with_the_current_value(app):
    acme = signup_company(app, "Race Co")
    other = second_admin(app, acme)
    seen = token(acme, "payroll.lop.proration_basis")
    assert token(other, "payroll.lop.proration_basis") == seen == 1
    first = put(acme, change("payroll.lop.proration_basis", "WORKING_DAYS", row_version=seen))
    assert first.status_code == 200 and first.json()["changes"][0]["row_version"] == 2
    second = put(other, change("payroll.lop.proration_basis", "FIXED_30", row_version=seen))
    assert second.status_code == 409 and second.json()["code"] == "STALE_ROW_VERSION"
    current = second.json()["detail"]["current"]
    assert current["value"] == "WORKING_DAYS" and current["row_version"] == 2
    assert put(other, change("payroll.lop.proration_basis", "FIXED_30", row_version=2)).status_code == 200
    # Omitting the token where a value exists is stale too.
    assert put(other, change("payroll.lop.sandwich_rule", "BOTH")).status_code == 409


def test_ec_cfg_11_admin_only_key_needs_an_administrator_but_unchanged_saves_pass(app):
    acme = signup_company(app, "Admin Only Co")
    manager = invite_and_activate(app, acme, create_employee(acme, "Paymgr"), ["payroll_manager"])
    key = "payroll.approval.separation_of_duties"
    r = put(manager, change(key, False, row_version=1))
    assert r.status_code == 403 and r.json()["code"] == "ADMIN_ONLY"
    r = put(manager, change(key, True, row_version=1))
    assert r.status_code == 200 and r.json()["changes"][0]["status"] == "UNCHANGED"
    # Keys outside the Payroll module are company settings: they also need settings:manage.
    r = put(manager, change("attendance.grace_minutes", 20, row_version=1))
    assert r.status_code == 403
    r = put(acme, change(key, False, row_version=1, locked=True))
    assert r.status_code == 200 and r.json()["changes"][0]["status"] == "ACTIVE"


def test_ec_cfg_12_maker_checker_keeps_the_old_value_until_another_admin_approves(app):
    from app.payroll.services import settings as engine

    maker = signup_company(app, "Maker Co")
    checker = second_admin(app, maker)
    cid = maker.company_id
    assert put(maker, change("settings.change_approval.enabled", True, row_version=1)).json()["changes"][0]["status"] \
        == "ACTIVE"  # switching approval ON applies at once (it was OFF)
    key = "payroll.lop.proration_basis"
    r = put(maker, change(key, "WORKING_DAYS", row_version=token(maker, key)))
    assert r.status_code == 200, r.text
    pending = r.json()["changes"][0]
    assert pending["status"] == "PENDING_APPROVAL" and "current value stays in force" in pending["message"]
    item = values(maker)[key]
    assert item["value"] == "CALENDAR_DAYS" and item["pending"]["value"] == "WORKING_DAYS"
    r = put(checker, change(key, "FIXED_30", row_version=item["row_version"]))
    assert r.status_code == 409 and r.json()["code"] == "CHANGE_PENDING"
    listed = checker.get(REQUESTS).json()
    assert [(x["id"], x["value"], x["current_value"]) for x in listed] == [
        (pending["change_request_id"], "WORKING_DAYS", "CALENDAR_DAYS")]
    request_id = pending["change_request_id"]
    r = maker.post(f"{REQUESTS}/{request_id}/approve")
    assert r.status_code == 403 and r.json()["code"] == "MAKER_CHECKER"
    db = session(cid)
    try:
        assert engine.resolve(db, cid, key, on=today_of(cid)).value == "CALENDAR_DAYS"
    finally:
        db.close()
    r = checker.post(f"{REQUESTS}/{request_id}/approve", json={"comment": "Checked against policy"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    assert values(maker)[key]["value"] == "WORKING_DAYS"
    assert checker.post(f"{REQUESTS}/{request_id}/approve").json()["code"] == "ALREADY_DECIDED"
    row = sql("SELECT created_by_user_id, approved_by_user_id, status FROM setting_values WHERE company_id = :c "
              "AND key = :k AND status = 'ACTIVE'", c=cid, k=key)[0]
    assert row[0] != row[1] and row[1] == checker.user_id

    r = put(maker, change(key, "FIXED_30", row_version=token(maker, key)))
    rejected = r.json()["changes"][0]["change_request_id"]
    assert checker.post(f"{REQUESTS}/{rejected}/reject", json={"comment": "No"}).json()["status"] == "REJECTED"
    assert values(maker)[key]["value"] == "WORKING_DAYS"
    assert {x["status"] for x in checker.get(REQUESTS, params={"status": "ALL"}).json()} == {"APPROVED", "REJECTED"}
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND module = 'Payroll Settings' "
                                 "AND entity_type = 'setting_value' ORDER BY id", c=cid)]
    assert actions == ["SETTING_CHANGE_REQUESTED", "SETTING_CHANGE_APPROVED", "SETTING_CHANGED",
                       "SETTING_CHANGE_REQUESTED", "SETTING_CHANGE_REJECTED"]


def test_ec_cfg_13_employee_override_needs_scope_over_that_employee(app, org):
    admin, cid = org["admin"], org["cid"]
    r = admin.post("/api/v1/roles", json={"name": "Entity payroll config", "grants": [
        {"permission": "payroll:configuration:read", "scope": "LEGAL_ENTITY"},
        {"permission": "payroll:configuration:write", "scope": "LEGAL_ENTITY"}]})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    scoped = invite_with_role_ids(app, admin, create_employee(admin, "Scoped"), [rid])
    r = admin.put(f"/api/v1/users/{scoped.user_id}/scope-targets",
                  json={"assignments": [{"role_id": rid, "legal_entity_ids": [org["le2"]]}]})
    assert r.status_code == 200, r.text
    key = "payroll.standard_hours_per_day"
    ok = put(scoped, change(key, "7.5", scope_type="EMPLOYEE", scope_id=org["worker"]))
    assert ok.status_code == 200, ok.text
    denied = put(scoped, change(key, "7.5", scope_type="EMPLOYEE", scope_id=org["stayer"]))
    assert denied.status_code == 404
    assert put(scoped, change("payroll.enabled", False, row_version=1)).status_code == 403  # TENANT needs COMPANY
    assert scoped.get(VALUES, params={"scope_type": "LEGAL_ENTITY", "scope_id": org["le1"]}).status_code == 404
    assert scoped.get(VALUES, params={"scope_type": "LEGAL_ENTITY", "scope_id": org["le2"]}).status_code == 200
    assert scoped.get(RESOLVE, params={"key": key, "employee_id": org["stayer"]}).status_code == 404
    assert scoped.get(RESOLVE, params={"key": key, "employee_id": org["worker"]}).json()["value"] == "7.5"


def test_ec_cfg_15_cross_setting_rules_block_saving(app):
    acme = signup_company(app, "Rules Co")
    r = put(acme, change("payroll.late_penalty.enabled", True, row_version=1),
            change("payroll.attendance.enabled", False, row_version=1))
    assert r.status_code == 400 and r.json()["code"] == "SETTINGS_INVALID"
    assert "V-01" in {i["rule"] for i in r.json()["detail"]["issues"]}
    r = put(acme, change("tasks.enabled", False, row_version=1))
    assert r.status_code == 400 and r.json()["detail"]["issues"][0]["rule"] == "V-08"
    # A warning does not block: V-15 is returned with the saved change.
    r = put(acme, change("payroll.bank.exclude_unverified_from_payout", False, row_version=1))
    assert r.status_code == 200 and [w["rule"] for w in r.json()["warnings"]] == ["V-15"]
    assert sql("SELECT count(*) FROM setting_values WHERE company_id = :c AND key = 'payroll.late_penalty.enabled'",
               c=acme.company_id)[0][0] == 1


def test_ec_cfg_09_invalid_stored_payroll_value_fails_closed_other_keys_fall_back(app):
    from app.payroll.services import settings as engine
    from app.services import settings_service

    acme = signup_company(app, "Invalid Co")
    cid = acme.company_id
    sql("UPDATE setting_values SET value = CAST(:v AS jsonb) WHERE company_id = :c AND key = :k",
        c=cid, k="payroll.lop.proration_basis", v='"BOGUS"')
    sql("UPDATE setting_values SET value = '999' WHERE company_id = :c AND key = 'attendance.grace_minutes'", c=cid)
    db = session(cid)
    try:
        on = today_of(cid)
        with pytest.raises(engine.SettingConfigError) as err:
            engine.resolve(db, cid, "payroll.lop.proration_basis", on=on)
        assert err.value.code == "CONFIG_INVALID" and err.value.issues[0].rule == "CONFIG_INVALID"
        grace = engine.resolve(db, cid, "attendance.grace_minutes", on=on)
        assert grace.value == 15 and grace.issues[0].severity == "WARNING"
        assert settings_service.get_value(db, cid, "graceTime") == 15
    finally:
        db.close()
    body = acme.get(RESOLVE, params={"key": "payroll.lop.proration_basis"}).json()
    assert body["valid"] is False and body["value"] is None and body["issues"][0]["rule"] == "CONFIG_INVALID"
    assert acme.get(RESOLVE, params={"key": "payroll.nope"}).status_code == 404


def test_ec_cfg_06_ec_cfg_19_ec_cfg_20_country_pack_limits_lock_and_reject(app, org, ae_law):
    from app.payroll.services import settings as engine

    admin, cid = org["admin"], org["cid"]
    item = values(admin, scope_type="LEGAL_ENTITY", scope_id=org["le2"])["payroll.wage_inputs.enabled"]
    assert item["badge"]["code"] == "REQUIRED_BY_LAW" and item["badge"]["source"] == "Test Labour Law art. 1"
    r = put(admin, change("payroll.wage_inputs.enabled", False, scope_type="LEGAL_ENTITY", scope_id=org["le2"]))
    assert r.status_code == 400 and r.json()["code"] == "STATUTORY_LOCK"  # EC-CFG-19
    r = put(admin, change("payroll.attendance.half_day_value", "0", scope_type="LEGAL_ENTITY", scope_id=org["le2"]))
    assert r.status_code == 400 and r.json()["code"] == "LEGAL_MINIMUM"  # EC-CFG-20
    r = put(admin, change("payroll.attendance.half_day_value", "0", row_version=1))
    assert r.status_code == 400 and r.json()["code"] == "LEGAL_MINIMUM"  # TENANT values reach the AE entity too
    assert put(admin, change("payroll.attendance.half_day_value", "1", scope_type="LEGAL_ENTITY",
                             scope_id=org["le2"])).status_code == 200
    # EC-CFG-06: a pack update that makes a saved value illegal fails closed at resolution.
    sql("UPDATE setting_values SET value = '\"0\"' WHERE company_id = :c AND key = 'payroll.attendance.half_day_value' "
        "AND scope_type = 'LEGAL_ENTITY'", c=cid)
    db = session(cid)
    try:
        on = today_of(cid)
        with pytest.raises(engine.SettingConfigError) as err:
            engine.resolve(db, cid, "payroll.attendance.half_day_value", employee_id=org["worker"], on=on)
        assert err.value.issues[0].rule == "V-16" and "Test Labour Law art. 2" in err.value.message
        law = engine.resolve(db, cid, "payroll.wage_inputs.enabled", employee_id=org["worker"], on=on)
        assert (law.value, law.level, law.source, law.locked_by) == (True, "COUNTRY_PACK", "COUNTRY_PACK", "COUNTRY_PACK")
        assert engine.resolve(db, cid, "payroll.attendance.half_day_value", employee_id=org["stayer"], on=on).value \
            == reg.Decimal("0.5")
    finally:
        db.close()


def test_idempotency_key_replays_the_batch(app):
    acme = signup_company(app, "Idem Co")
    headers = {"Idempotency-Key": "settings-batch-1"}
    first = put(acme, change("payroll.lop.proration_basis", "WORKING_DAYS", row_version=1), headers=headers)
    again = put(acme, change("payroll.lop.proration_basis", "WORKING_DAYS", row_version=1), headers=headers)
    assert first.status_code == again.status_code == 200
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json() == first.json()
    other = put(acme, change("payroll.lop.proration_basis", "FIXED_30", row_version=1), headers=headers)
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert sql("SELECT count(*) FROM setting_values WHERE company_id = :c AND key = 'payroll.lop.proration_basis' "
               "AND source = 'ADMIN'", c=acme.company_id)[0][0] == 1


# ---------------------------------------------------------------------------
# isolation (EC-CFG-17)
# ---------------------------------------------------------------------------
def test_ec_cfg_17_three_tenants_are_isolated(app, org):
    a, cid = org["admin"], org["cid"]
    b, c = signup_company(app, "Beta Industries"), signup_company(app, "Cobalt Labs")
    assert put(b, change("payroll.lop.proration_basis", "FIXED_30", row_version=1)).status_code == 200
    assert values(a)["payroll.lop.proration_basis"]["value"] == "CALENDAR_DAYS"
    assert values(c)["payroll.lop.proration_basis"]["value"] == "CALENDAR_DAYS"
    for scope_type, key in (("LEGAL_ENTITY", "le2"), ("BRANCH", "br2"), ("PAY_GROUP", "pg2"), ("EMPLOYEE", "worker")):
        assert b.get(VALUES, params={"scope_type": scope_type, "scope_id": org[key]}).status_code == 404, scope_type
        r = put(b, change("payroll.wage_inputs.enabled" if scope_type != "EMPLOYEE" else "payroll.standard_hours_per_day",
                          False if scope_type != "EMPLOYEE" else "7", scope_type=scope_type, scope_id=org[key]))
        assert r.status_code == 404, (scope_type, r.text)
    assert b.get(RESOLVE, params={"key": "payroll.enabled", "employee_id": org["worker"]}).status_code == 404
    assert b.get(RESOLVE, params={"key": "payroll.enabled", "legal_entity_id": org["le2"]}).status_code == 404
    assert put(a, change("settings.change_approval.enabled", True, row_version=1)).status_code == 200
    pending = put(a, change("payroll.lop.sandwich_rule", "BOTH", row_version=1)).json()["changes"][0]
    assert pending["status"] == "PENDING_APPROVAL"
    assert b.post(f"{REQUESTS}/{pending['change_request_id']}/approve").status_code == 404
    assert b.get(REQUESTS).json() == []


# ---------------------------------------------------------------------------
# compatibility facade (app.services.settings_service, 3.1 §2.4)
# ---------------------------------------------------------------------------
def test_legacy_settings_read_and_write_through_the_v2_engine(app):
    from app.payroll.services import settings as engine

    acme = signup_company(app, "Facade Co")
    cid = acme.company_id
    legacy = acme.get("/api/v1/settings").json()
    assert (legacy["graceTime"], legacy["lunchIsPaid"], legacy["payrollSeparationOfDuties"]) == (15, False, True)
    r = acme.put("/api/v1/settings", json={"graceTime": 7, "lunchIsPaid": True, "companyEmail": "hr@facade.io"})
    assert r.status_code == 200, r.text
    assert (r.json()["graceTime"], r.json()["lunchIsPaid"], r.json()["companyEmail"]) == (7, True, "hr@facade.io")
    db = session(cid)
    try:
        on = today_of(cid)
        assert engine.resolve(db, cid, "attendance.grace_minutes", on=on).value == 7
        assert engine.resolve(db, cid, "attendance.lunch_is_paid", on=on).source == "ADMIN"
    finally:
        db.close()
    mirror = dict(sql("SELECT key, value FROM company_settings WHERE company_id = :c AND key IN ('graceTime', 'lunchIsPaid')",
                      c=cid))
    assert mirror == {"graceTime": "7", "lunchIsPaid": "true"}  # kept for a code rollback
    assert acme.put("/api/v1/settings", json={"graceTime": 300}).status_code == 400  # v2 range 0-240
    assert acme.put("/api/v1/settings", json={"graceTime": 7}).status_code == 200  # unchanged: no new row
    assert sql("SELECT count(*) FROM setting_values WHERE company_id = :c AND key = 'attendance.grace_minutes'",
               c=cid)[0][0] == 2
    # The attendance policy editor (same facade) and the v2 API agree.
    r = acme.patch("/api/v1/attendance/policy", json={"lunchIsPaid": False})
    assert r.status_code == 200, r.text
    assert values(acme)["attendance.lunch_is_paid"]["value"] is False


def test_legacy_separation_of_duties_follows_v2_change_control(app):
    from app.services import settings_service

    acme = signup_company(app, "Facade Approval Co")
    cid = acme.company_id
    assert put(acme, change("settings.change_approval.enabled", True, row_version=1)).status_code == 200
    r = acme.put("/api/v1/settings", json={"payrollSeparationOfDuties": False})
    assert r.status_code == 200 and r.json()["payrollSeparationOfDuties"] is True  # pending approval
    db = session(cid)
    try:
        assert settings_service.get_value(db, cid, "payrollSeparationOfDuties") is True
    finally:
        db.close()
    assert [x["key"] for x in acme.get(REQUESTS).json()] == ["payroll.approval.separation_of_duties"]
    assert sql("SELECT count(*) FROM company_settings WHERE company_id = :c AND key = 'payrollSeparationOfDuties'",
               c=cid)[0][0] == 0
    hr = invite_and_activate(app, acme, create_employee(acme, "Hr"), ["hr_admin"])
    assert hr.put("/api/v1/settings", json={"payrollSeparationOfDuties": False}).status_code == 403  # no settings:manage
    r = acme.post("/api/v1/roles", json={"name": "Settings manager", "grants": [
        {"permission": "settings:manage", "scope": "COMPANY"}]})
    assert r.status_code == 200, r.text
    manager = invite_with_role_ids(app, acme, create_employee(acme, "Setmgr"), [r.json()["id"]])
    r = manager.put("/api/v1/settings", json={"payrollSeparationOfDuties": False})
    assert r.status_code == 400 and "Only company administrators" in r.text  # admin-only, as before
