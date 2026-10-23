"""Settings inheritance harness (Goal 2 §6): every level of the chain really drives the calculation.

EMPLOYEE -> PAY_GROUP -> BRANCH -> LEGAL_ENTITY -> TENANT -> PARTNER -> COUNTRY_PACK -> PLATFORM (3.1 §2.2, 02 §14),
locks, statutory locks and legal limits, fail-closed resolution, PERIOD vs DAY grain, future-dated and backdated
changes (reason required, finalized periods untouched), version history, maker-checker, stale row versions (409),
cross-setting rules V-01..V-16, and stale runs after a change of locked inputs.

Every expected amount is written out by hand below (Decimal arithmetic with the reasoning in comments); the
engine and the services are never called to produce an expectation. The world (``settings_world`` in
conftest.py) pays BASIC = 1,000.00 GBP x N for a month of N calendar days, so one calendar day of LOP is
exactly 1,000.00 under CALENDAR_DAYS. Platform and partner defaults are platform-owned rows without a tenant
API ("written by operators through the system session", models/payroll_config.py): they are inserted with the
system engine, which is the operator path.

EC-CFG-01, EC-CFG-02, EC-CFG-03, EC-CFG-04, EC-CFG-05, EC-CFG-07, EC-CFG-08, EC-CFG-09, EC-CFG-10, EC-CFG-12,
EC-CFG-14, EC-CFG-15, EC-CFG-17, EC-CFG-18, EC-CFG-19, EC-CFG-20.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text

from tests.payroll.verification.conftest import (
    DAY_RATE,
    PAY,
    REQUESTS,
    RUNS,
    activate_policy,
    approve_request,
    calculate,
    create_run,
    days_between,
    employee_lines,
    idem,
    make_branch,
    make_entity,
    monthly_basic,
    ok,
    period_days,
    precheck,
    put_setting,
    recalc,
    resolve,
    row_version,
    run_detail,
    second_admin,
    today_of,
)

D = Decimal
CENT = D("0.01")
BASIS = "payroll.lop.proration_basis"
REASON = "Verification: inheritance"


def r2(value):
    """Half-up to the penny (payroll.rounding.line_mode = HALF_UP, GBP has 2 minor units)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


def code_of(response):
    return response.json().get("code")


def weekdays(w):
    """Scheduled working days of the period (the tenants start with the MON_FRI work week)."""
    return [d for d in days_between(w["start"], w["end"]) if d.weekday() < 5]


def manual_lop(w, person, day):
    """An approved manual LOP input (POST /payroll/lop-inputs, decided through the approvals engine)."""
    created = ok(w["admin"].post(f"{PAY}/lop-inputs", json={"employee_id": w["emp"][person], "date": day.isoformat(),
                                                            "reason": "Unauthorised absence"}, headers=idem()), 201)
    approve_request(w, created["approval_request_id"], deciders=("manager", "approver", "second_admin"))
    return created


def basic_of(w, run_id, person):
    lines, _ = employee_lines(w["admin"], run_id, w["emp"][person])
    return lines.get("BASIC")


def run_for(w, group_code):
    run = create_run(w["admin"], w["groups"][group_code], w["periods"][group_code]["id"])
    precheck(w["admin"], run["id"])
    calculate(w["admin"], run["id"])
    return run["id"]


# ---------------------------------------------------------------------------
# Precedence: TENANT -> LEGAL_ENTITY -> PAY_GROUP drive real run amounts (EC-CFG-01, EC-CFG-17)
# ---------------------------------------------------------------------------
def test_tenant_entity_and_pay_group_values_each_drive_their_own_employees(settings_world):
    w, beta = settings_world["alpha"], settings_world["beta"]
    n = period_days(w["period"])
    month = monthly_basic(w["period"])                       # 1,000.00 x N
    first = weekdays(w)[0]
    for person in ("Ann", "Cal", "Dee"):                     # one approved day of manual LOP each; Ben none
        manual_lop(w, person, first)
    start = w["start"]
    put_setting(w["admin"], BASIS, "FIXED_30", effective_from=start, reason=REASON)
    put_setting(w["admin"], BASIS, "FIXED_26", "LEGAL_ENTITY", w["entities"]["VA1"], effective_from=start,
                reason=REASON)
    put_setting(w["admin"], BASIS, "CALENDAR_DAYS", "PAY_GROUP", w["groups"]["PG-B"], effective_from=start,
                reason=REASON)

    # The resolve API names the level that wins for each employee.
    expected_levels = {"Ann": ("FIXED_26", "LEGAL_ENTITY"), "Ben": ("FIXED_26", "LEGAL_ENTITY"),
                       "Cal": ("CALENDAR_DAYS", "PAY_GROUP"), "Dee": ("FIXED_30", "TENANT")}
    for person, (value, level) in expected_levels.items():
        found = resolve(w["admin"], BASIS, employee_id=w["emp"][person], on=w["end"])
        assert (found["value"], found["level"]) == (value, level), (person, found)

    runs = {code: run_for(w, code) for code in ("PG-A", "PG-B", "PG-C")}
    # Ann, FIXED_26 (entity VA1): paid units 26 - 1 = 25 of 26 -> month x 25 / 26.
    assert basic_of(w, runs["PG-A"], "Ann") == r2(month * 25 / 26)
    # Ben, FIXED_26 without LOP: 26 / 26 -> the whole month.
    assert basic_of(w, runs["PG-A"], "Ben") == month
    # Cal, CALENDAR_DAYS (pay group PG-B wins over the entity): (N - 1) / N -> 1,000.00 x (N - 1).
    assert basic_of(w, runs["PG-B"], "Cal") == DAY_RATE * (n - 1)
    # Dee, FIXED_30 (tenant; entity VA2 has no override): 29 / 30.
    assert basic_of(w, runs["PG-C"], "Dee") == r2(month * 29 / 30)

    # Another tenant is never affected (EC-CFG-17): its value, level and recalculated run are unchanged.
    found = resolve(beta["admin"], BASIS, employee_id=beta["emp"]["Zed"], on=beta["end"])
    assert (found["value"], found["level"]) == ("CALENDAR_DAYS", "TENANT")
    recalc(beta["admin"], beta["run"])
    assert basic_of(beta, beta["run"], "Zed") == monthly_basic(beta["period"])


def test_employee_level_only_where_the_registry_allows_it(settings_world):
    """Proration basis is T/LE/PG only: an EMPLOYEE value is refused. standard_hours_per_day allows EMPLOYEE and
    wins for that employee only."""
    w = settings_world["alpha"]
    r = w["admin"].put(f"{PAY}/settings/values", json={"changes": [{
        "key": BASIS, "value": "FIXED_30", "scope_type": "EMPLOYEE", "scope_id": w["emp"]["Ann"], "row_version": 0}]})
    assert r.status_code == 400 and code_of(r) == "LEVEL_NOT_ALLOWED", r.text
    key = "payroll.standard_hours_per_day"
    put_setting(w["admin"], key, "10", "EMPLOYEE", w["emp"]["Ben"], effective_from=w["start"], reason=REASON)
    assert resolve(w["admin"], key, employee_id=w["emp"]["Ben"], on=w["end"])["level"] == "EMPLOYEE"
    assert D(resolve(w["admin"], key, employee_id=w["emp"]["Ben"], on=w["end"])["value"]) == D(10)
    ann = resolve(w["admin"], key, employee_id=w["emp"]["Ann"], on=w["end"])
    assert ann["level"] != "EMPLOYEE" and D(ann["value"]) == D(8)
    # The EMPLOYEE value drives Ben's run and only Ben's: 2 approved overtime hours each, multiplier 1.5, hourly
    # rate = 1,000.00 x N / N / standard hours (explicit rate formula, see test_settings_onoff.OT_RATE_FORMULA).
    from tests.payroll.verification.test_settings_onoff import ot_fields, overtime, past_weekdays

    day = past_weekdays(w)[0]
    overtime(w, "Ben", day, 120)
    overtime(w, "Ann", day, 120)
    activate_policy(w["admin"], "OVERTIME_POLICY", ot_fields(workday="1.5"))
    put_setting(w["admin"], "payroll.overtime.enabled", True, effective_from=w["start"], reason=REASON)
    run_id = run_for(w, "PG-A")
    lines = {p: employee_lines(w["admin"], run_id, w["emp"][p])[0] for p in ("Ann", "Ben")}
    assert lines["Ben"].get("OT_WORKDAY") == D("300.00"), lines     # 2 h x (1,000.00 / 10) x 1.5
    assert lines["Ann"].get("OT_WORKDAY") == D("375.00"), lines     # 2 h x (1,000.00 / 8) x 1.5


def test_settings_writes_need_permission_scope_and_the_own_tenant(settings_world):
    """Authorization of the settings API: no permission -> 403; another tenant's scope ids -> 404 without
    disclosure; nothing is written by a refused request (EC-CFG-13, EC-CFG-17)."""
    from tests.payroll.verification.conftest import employee_login

    w, beta = settings_world["alpha"], settings_world["beta"]
    ann = employee_login(w, "Ann")
    assert ann.get(f"{PAY}/settings/values").status_code == 403
    r = ann.put(f"{PAY}/settings/values", json={"changes": [{"key": BASIS, "value": "FIXED_30", "row_version": 0}]})
    assert r.status_code == 403, r.text
    r = w["approver"].put(f"{PAY}/settings/values", json={"changes": [{"key": BASIS, "value": "FIXED_30",
                                                                      "row_version": 0}]})
    assert r.status_code == 403, r.text                           # payroll approver: no configuration write
    for scope_type, scope_id in (("LEGAL_ENTITY", w["entities"]["VA1"]), ("PAY_GROUP", w["groups"]["PG-A"]),
                                 ("EMPLOYEE", w["emp"]["Ann"])):
        key = "payroll.standard_hours_per_day" if scope_type == "EMPLOYEE" else BASIS
        value = "9" if scope_type == "EMPLOYEE" else "FIXED_30"
        r = beta["admin"].put(f"{PAY}/settings/values", json={"changes": [{
            "key": key, "value": value, "scope_type": scope_type, "scope_id": scope_id, "row_version": 0}]})
        assert r.status_code == 404, (scope_type, r.text)
        assert beta["admin"].get(f"{PAY}/settings/resolve", params={
            "key": key, {"LEGAL_ENTITY": "legal_entity_id", "PAY_GROUP": "pay_group_id",
                         "EMPLOYEE": "employee_id"}[scope_type]: scope_id}).status_code == 404
    assert sql("SELECT count(*) FROM setting_values WHERE company_id = :c AND scope_type <> 'TENANT'",
               c=w["cid"])[0][0] == 0
    assert resolve(w["admin"], BASIS, employee_id=w["emp"]["Ann"], on=w["end"])["value"] == "CALENDAR_DAYS"


# ---------------------------------------------------------------------------
# Locks (EC-CFG-04)
# ---------------------------------------------------------------------------
def test_tenant_lock_shadows_lower_overrides_blocks_new_ones_and_unlock_restores_them(settings_world):
    w = settings_world["alpha"]
    month = monthly_basic(w["period"])
    manual_lop(w, "Ann", weekdays(w)[0])
    start = w["start"]
    put_setting(w["admin"], BASIS, "FIXED_26", "LEGAL_ENTITY", w["entities"]["VA1"], effective_from=start,
                reason=REASON)
    put_setting(w["admin"], BASIS, "FIXED_30", effective_from=start, reason=REASON, locked=True)
    found = resolve(w["admin"], BASIS, employee_id=w["emp"]["Ann"], on=w["end"])
    assert (found["value"], found["level"], found["locked_by"]) == ("FIXED_30", "TENANT", "TENANT")
    entity_level = next(l for l in found["levels"] if l["level"] == "LEGAL_ENTITY")
    assert entity_level["value"] == "FIXED_26" and entity_level["status"] not in ("EFFECTIVE", "EMPTY")
    r = w["admin"].put(f"{PAY}/settings/values", json={"changes": [{
        "key": BASIS, "value": "CALENDAR_DAYS", "scope_type": "PAY_GROUP", "scope_id": w["groups"]["PG-A"],
        "row_version": 0, "effective_from": start.isoformat(), "reason": REASON}]})
    assert r.status_code == 400 and code_of(r) == "SETTING_LOCKED", r.text
    run_id = run_for(w, "PG-A")
    assert basic_of(w, run_id, "Ann") == r2(month * 29 / 30)          # the locked tenant FIXED_30 applies
    put_setting(w["admin"], BASIS, "FIXED_30", effective_from=start, reason=REASON, locked=False)
    assert resolve(w["admin"], BASIS, employee_id=w["emp"]["Ann"], on=w["end"])["level"] == "LEGAL_ENTITY"
    detail = run_detail(w["admin"], run_id)
    assert detail["stale"] is True                                  # the unlock changed the locked inputs
    recalc(w["admin"], run_id)
    assert basic_of(w, run_id, "Ann") == r2(month * 25 / 26)          # FIXED_26 of the entity again


# ---------------------------------------------------------------------------
# Platform and partner defaults (02 §14.1; I-13: tenants without a materialized row follow them)
# ---------------------------------------------------------------------------
def _platform_row(level, key, value, partner_id=None):
    import json

    sql("INSERT INTO platform_setting_values (level, partner_id, key, value, locked, effective_from, created_by, "
        "created_at, updated_at) VALUES (:l, :p, :k, CAST(:v AS jsonb), false, DATE '2000-01-01', "
        "'verification operator', now(), now())", l=level, p=partner_id, k=key, v=json.dumps(value))


def _partner_for(company_id):
    partner_id = sql("INSERT INTO partners (name, slug, status, billing_mode, tenant_entitlements, created_at, "
                     "updated_at) VALUES ('Verify Bureau', 'verify-bureau', 'active', 'PLATFORM_BILLS_TENANT', "
                     "'{}'::jsonb, now(), now()) RETURNING id")[0][0]
    sql("UPDATE companies SET partner_id = :p WHERE id = :c", p=partner_id, c=company_id)
    return partner_id


def test_platform_and_partner_defaults_apply_until_a_tenant_scope_overrides_them(settings_world):
    """payroll.run.allow_multiple_regular_runs has no starter row: PLATFORM says yes, Beta's partner says no,
    and Beta's own pay-group value wins over its partner."""
    w, beta = settings_world["alpha"], settings_world["beta"]
    key = "payroll.run.allow_multiple_regular_runs"
    _platform_row("PLATFORM", key, True)
    _partner_for(beta["cid"])
    _platform_row("PARTNER", key, False, partner_id=sql("SELECT partner_id FROM companies WHERE id = :c",
                                                        c=beta["cid"])[0][0])
    alpha_view = resolve(w["admin"], key, pay_group_id=w["groups"]["PG-A"], on=w["end"])
    assert (alpha_view["value"], alpha_view["level"]) == (True, "PLATFORM")
    beta_view = resolve(beta["admin"], key, pay_group_id=beta["groups"]["PG-Z"], on=beta["end"])
    assert (beta_view["value"], beta_view["level"]) == (False, "PARTNER")

    from tests.helpers import create_employee
    from tests.payroll.verification.conftest import place

    first = create_run(w["admin"], w["groups"]["PG-A"], w["periods"]["PG-A"]["id"])
    # A further regular run of the period names employees that no other regular run of the period pays
    # (EMPLOYEES_REQUIRED / EMPLOYEE_ALREADY_IN_RUN otherwise): Eve joins PG-A after the first run.
    eve = create_employee(w["admin"], "Eve", "Verify", hire_date="2024-01-15")
    place(w["admin"], eve["id"], w["entities"]["VA1"], w["branches"]["LON"], w["groups"]["PG-A"])
    second = w["admin"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": w["groups"]["PG-A"],
                                         "pay_period_id": w["periods"]["PG-A"]["id"],
                                         "employee_ids": [eve["id"]]}, headers=idem())
    assert second.status_code == 201, second.text                 # the platform default allows a second run
    assert second.json()["id"] != first["id"]
    # Beta already has one regular run for its period (fixture): its partner forbids a second one.
    zoe = create_employee(beta["admin"], "Zoe", "Verify", hire_date="2024-01-15")
    place(beta["admin"], zoe["id"], beta["entities"]["VB1"], beta["branches"]["BRS"], beta["groups"]["PG-Z"])
    refused = beta["admin"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": beta["groups"]["PG-Z"],
                                             "pay_period_id": beta["periods"]["PG-Z"]["id"],
                                             "employee_ids": [zoe["id"]]}, headers=idem())
    assert refused.status_code == 409 and code_of(refused) == "DUPLICATE_RUN", refused.text
    put_setting(beta["admin"], key, True, "PAY_GROUP", beta["groups"]["PG-Z"], effective_from=beta["start"],
                reason=REASON)
    beta_view = resolve(beta["admin"], key, pay_group_id=beta["groups"]["PG-Z"], on=beta["end"])
    assert (beta_view["value"], beta_view["level"]) == (True, "PAY_GROUP")
    allowed = beta["admin"].post(RUNS, json={"run_type": "REGULAR", "pay_group_id": beta["groups"]["PG-Z"],
                                             "pay_period_id": beta["periods"]["PG-Z"]["id"],
                                             "employee_ids": [zoe["id"]]}, headers=idem())
    assert allowed.status_code == 201, allowed.text


# ---------------------------------------------------------------------------
# Country-pack locks and legal limits (EC-CFG-19, EC-CFG-20, V-10, V-16): the fictional verified pack ZZ
# ---------------------------------------------------------------------------
def test_statutory_locks_and_legal_limits_of_a_verified_pack_cannot_be_overridden(settings_world):
    from app.payroll import packs
    from app.payroll.packs.zz_test import PENSION
    from app.payroll.settings_registry import unregister_pack_settings

    # The fictional ZZ pack exists only in tests: register its employer choices (idempotent), as the product's
    # pack tests do; the application registers the real packs at start-up. The settings registry is
    # process-global, so the keys this test added are removed again (they leaked into later unit tests, F-64).
    added = packs.register_settings(["ZZ"], environment="test")
    try:
        w = settings_world["alpha"]
        admin = w["admin"]
        # ZZ is a test-only country code: the entity is created through the API and its country set by the operator
        # path, exactly as the product's own pack tests do (tests/payroll/test_reports.py make_entity).
        entity = make_entity(admin, "VZ", country="DE", currency="EUR", tz="Europe/Berlin")
        make_branch(admin, entity, "ZED")
        sql("UPDATE legal_entities SET country_code = 'ZZ' WHERE id = :e", e=entity)
        pension = PENSION.switch_key
        found = resolve(admin, pension, legal_entity_id=entity)
        assert found["value"] is True and found["legal_limit"], found      # locked ON by the verified pack
        for key, value, code in ((pension, False, "STATUTORY_LOCK"),              # V-10: mandatory scheme OFF
                                 ("payroll.late_penalty.enabled", True, "STATUTORY_LOCK"),   # pack forbids penalties
                                 ("payroll.deductions.min_net_pay_percent", "40", "LEGAL_MINIMUM")):  # V-16 floor 50
            r = admin.put(f"{PAY}/settings/values", json={"changes": [{
                "key": key, "value": value, "scope_type": "LEGAL_ENTITY", "scope_id": entity,
                "row_version": row_version(admin, key, "LEGAL_ENTITY", entity)}]})
            assert r.status_code == 400 and code_of(r) == code, (key, r.text)
        put_setting(admin, "payroll.deductions.min_net_pay_percent", "60", "LEGAL_ENTITY", entity)
        late = resolve(admin, "payroll.late_penalty.enabled", legal_entity_id=entity)
        assert late["value"] is False and late["legal_limit"]
        # The GB entity has no pack: the same key is freely settable there and carries no legal limit.
        gb = resolve(admin, "payroll.late_penalty.enabled", legal_entity_id=w["entities"]["VA1"])
        assert gb["legal_limit"] == []
    finally:
        if added:
            unregister_pack_settings("ZZ")


# ---------------------------------------------------------------------------
# Cross-setting rules V-01..V-16 (EC-CFG-05, EC-CFG-07, EC-CFG-15): every save-blocking rule is refused
# ---------------------------------------------------------------------------
def _rules_of(response):
    body = response.json()
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    return [i["rule"] for i in (detail.get("issues") or [])]


def _batch(client, *changes):
    payload = []
    for change in changes:
        key, value = change[0], change[1]
        scope_type = change[2] if len(change) > 2 else "TENANT"
        scope_id = change[3] if len(change) > 3 else None
        item = {"key": key, "value": value, "scope_type": scope_type,
                "row_version": row_version(client, key, scope_type, scope_id)}
        if scope_id is not None:
            item["scope_id"] = scope_id
        payload.append(item)
    return client.put(f"{PAY}/settings/values", json={"changes": payload})


def test_every_save_blocking_cross_setting_rule_is_refused_with_its_rule_id(settings_world):
    w = settings_world["alpha"]
    admin = w["admin"]
    group = w["groups"]["PG-C"]
    blocked = {
        "V-01": [("payroll.late_penalty.enabled", True)],                         # attendance is OFF (baseline)
        "V-04": [(BASIS, "HOURS")],
        "V-05": [(BASIS, "CUSTOM_DAYS")],
        "V-06": [("payroll.attendance_cycle.mode", "CUSTOM_START_DAY", "PAY_GROUP", group),
                 ("payroll.attendance_cycle.start_day", None, "PAY_GROUP", group)],
        "V-07": [("payroll.cutoff.input_days_before_pay_day", 1, "PAY_GROUP", group)],  # 1 > 2 > 1 fails
        "V-08": [("tasks.enabled", False)],                                        # task pay is ON (starter)
        "V-09": [("payslip.email_attach_pdf", True)],                              # PDF protection is NONE
        "V-13": [("payroll.rounding.net_pay_increment", "0.001", "PAY_GROUP", group)],  # below the GBP penny
    }
    for rule, changes in blocked.items():
        r = _batch(admin, *changes)
        assert r.status_code == 400 and code_of(r) == "SETTINGS_INVALID", (rule, r.text)
        assert rule in _rules_of(r), (rule, r.text)
    # The valid combinations of the same keys are accepted.
    assert _batch(admin, (BASIS, "CUSTOM_DAYS"), ("payroll.lop.custom_days", 25)).status_code == 200
    # V-02 and V-03 come from policies in force: an attendance-sourced overtime policy and an adjacent-day
    # holiday policy may exist while their switches are OFF, but switching ON without attendance is refused.
    activate_policy(admin, "OVERTIME_POLICY", {"source": "ATTENDANCE_AUTO", "buckets": [
        {"day_type": t, "after_hours": "0", "multiplier": "1.5"} for t in ("WORKDAY", "WEEKLY_OFF", "HOLIDAY")]},
        name="Attendance OT")
    r = _batch(admin, ("payroll.overtime.enabled", True))
    assert r.status_code == 400 and "V-02" in _rules_of(r), r.text
    activate_policy(admin, "HOLIDAY_PAY_POLICY", {"eligibility_adjacent_days": "BOTH"}, name="Adjacent days")
    r = _batch(admin, ("payroll.holiday_pay.non_salaried.enabled", True))
    assert r.status_code == 400 and "V-03" in _rules_of(r), r.text
    # V-15 and V-14 are warnings: the change is saved and the warning returned.
    r = _batch(admin, ("payroll.bank.exclude_unverified_from_payout", False))
    assert r.status_code == 200 and "V-15" in [x["rule"] for x in r.json()["warnings"]], r.text
    # V-12: two rules with the same priority matching one employee are refused (EC-CFG-05).
    policies = ok(admin.get(f"{PAY}/policies", params={"type": "LATE_PENALTY_POLICY"}))
    policy_id = policies[0]["id"]
    other = ok(admin.post(f"{PAY}/policies", json={"type": "LATE_PENALTY_POLICY", "name": "Second late policy",
                                                   "effective_from": "2020-01-01"}, headers=idem()), 201)
    r = admin.put(f"{PAY}/policies/{other['id']}/assignments", json={"rules": [{
        "criteria": {"employee_ids": [w["emp"]["Ann"]]}, "priority": 0, "effective_from": "2020-01-01"}]})
    rules_of_default = ok(admin.get(f"{PAY}/policies/{policy_id}/assignments"))
    assert r.status_code == 400, (r.text, rules_of_default)


def test_v11_switch_on_with_employees_outside_every_policy_is_a_blocking_precheck(settings_world):
    """EC-CFG-07: the attendance policy is narrowed to pay group PG-A; with attendance ON, Cal (PG-B) has none."""
    w = settings_world["alpha"]
    admin = w["admin"]
    [policy] = ok(admin.get(f"{PAY}/policies", params={"type": "ATTENDANCE_PAY_POLICY"}))
    ok(admin.put(f"{PAY}/policies/{policy['id']}/assignments", json={"rules": [{
        "criteria": {"pay_group_ids": [w["groups"]["PG-A"]]}, "priority": 0, "effective_from": "2020-01-01"}]},
        headers=idem()))
    put_setting(admin, "payroll.attendance.enabled", True, effective_from=w["start"], reason=REASON)
    run = create_run(admin, w["groups"]["PG-B"], w["periods"]["PG-B"]["id"])
    findings = precheck(admin, run["id"])
    v11 = [f for f in findings if (f.get("details") or {}).get("rule") == "V-11"]
    assert v11 and all(f["severity"] == "BLOCKING" for f in v11), findings
    assert any((f.get("details") or {}).get("policy_type") == "ATTENDANCE_PAY_POLICY" for f in v11)


# ---------------------------------------------------------------------------
# Concurrency, maker-checker, version history (EC-CFG-10, EC-CFG-12)
# ---------------------------------------------------------------------------
def test_stale_row_version_is_409_with_the_current_value(settings_world):
    w = settings_world["alpha"]
    admin, other = w["admin"], second_admin(w)
    token = row_version(admin, BASIS)
    put_setting(other, BASIS, "FIXED_30", version=token)
    r = admin.put(f"{PAY}/settings/values", json={"changes": [{"key": BASIS, "value": "FIXED_26",
                                                               "row_version": token}]})
    assert r.status_code == 409 and code_of(r) == "STALE_ROW_VERSION", r.text
    detail = r.json()["detail"]
    assert detail["current"]["value"] == "FIXED_30" and detail["current"]["row_version"] == token + 1
    assert resolve(admin, BASIS, employee_id=w["emp"]["Ann"], on=w["end"])["value"] == "FIXED_30"


def test_maker_checker_keeps_the_run_on_the_old_value_until_another_admin_approves(settings_world):
    w = settings_world["alpha"]
    admin, checker = w["admin"], second_admin(w)
    month = monthly_basic(w["period"])
    manual_lop(w, "Ann", weekdays(w)[0])
    put_setting(admin, "settings.change_approval.enabled", True)
    pending = put_setting(admin, BASIS, "FIXED_30", effective_from=w["start"], reason=REASON)["changes"][0]
    assert pending["status"] == "PENDING_APPROVAL"
    run_id = run_for(w, "PG-A")
    # Still CALENDAR_DAYS (starter): Ann loses exactly one day.
    assert basic_of(w, run_id, "Ann") == month - DAY_RATE
    r = admin.post(f"{REQUESTS}/{pending['change_request_id']}/approve", json={})
    assert r.status_code == 403 and code_of(r) == "MAKER_CHECKER"
    ok(checker.post(f"{REQUESTS}/{pending['change_request_id']}/approve", json={"comment": "Checked"}))
    assert run_detail(admin, run_id)["stale"] is True
    recalc(admin, run_id)
    assert basic_of(w, run_id, "Ann") == r2(month * 29 / 30)
    history = sql("SELECT value #>> '{}', status, effective_from, effective_to, created_by_user_id, "
                  "approved_by_user_id FROM setting_values WHERE company_id = :c AND key = :k AND scope_type = "
                  "'TENANT' ORDER BY id", c=w["cid"], k=BASIS)
    assert history[-1][:2] == ("FIXED_30", "ACTIVE") and history[-1][4] != history[-1][5]
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = "
                                 "'setting_value' AND details->>'key' = :k ORDER BY id", c=w["cid"], k=BASIS)]
    assert actions[-3:] == ["SETTING_CHANGE_REQUESTED", "SETTING_CHANGE_APPROVED", "SETTING_CHANGED"]


def test_version_history_closes_the_previous_row_and_audits_old_and_new_values(settings_world):
    w = settings_world["alpha"]
    admin = w["admin"]
    nxt = w["end"] + timedelta(days=1)
    put_setting(admin, BASIS, "FIXED_30", effective_from=w["start"], reason=REASON)
    put_setting(admin, BASIS, "FIXED_26", effective_from=nxt)                    # future-dated: no reason needed
    rows = sql("SELECT value #>> '{}', status, effective_from, effective_to FROM setting_values WHERE company_id = :c "
               "AND key = :k AND scope_type = 'TENANT' AND status = 'ACTIVE' ORDER BY effective_from",
               c=w["cid"], k=BASIS)
    starter = [r for r in rows if r[2] < w["start"]]
    assert [(r[0], r[2], r[3]) for r in rows if r[2] >= w["start"]] == [
        ("FIXED_30", w["start"], w["end"]), ("FIXED_26", nxt, None)]
    assert all(r[3] == w["start"] - timedelta(days=1) for r in starter)
    assert resolve(admin, BASIS, employee_id=w["emp"]["Ann"], on=w["end"])["value"] == "FIXED_30"
    assert resolve(admin, BASIS, employee_id=w["emp"]["Ann"], on=nxt)["value"] == "FIXED_26"
    audits = sql("SELECT details->>'old', details->>'new', details->>'reason' FROM audit_logs WHERE company_id = :c "
                 "AND action = 'SETTING_CHANGED' AND details->>'key' = :k ORDER BY id", c=w["cid"], k=BASIS)
    assert [a[:2] for a in audits[-2:]] == [('"CALENDAR_DAYS"', '"FIXED_30"'), ('"FIXED_30"', '"FIXED_26"')] or \
        [a[:2] for a in audits[-2:]] == [("CALENDAR_DAYS", "FIXED_30"), ("FIXED_30", "FIXED_26")], audits
    assert audits[-2][2] == REASON


# ---------------------------------------------------------------------------
# Effective dating: PERIOD vs DAY grain (EC-CFG-02, EC-CFG-08, EC-CFG-18), backdating and finalized periods
# ---------------------------------------------------------------------------
def test_period_grain_value_on_the_period_end_applies_to_the_whole_period(settings_world):
    w = settings_world["alpha"]
    month = monthly_basic(w["period"])
    manual_lop(w, "Ann", weekdays(w)[0])
    late_in_period = w["end"] - timedelta(days=2)
    put_setting(w["admin"], BASIS, "FIXED_30", effective_from=late_in_period,
                reason=REASON if late_in_period < today_of(w["admin"]) else None)
    run_id = run_for(w, "PG-A")
    # FIXED_30 is in force on the period end, so it prices the LOP day that happened before it: 29 / 30.
    assert basic_of(w, run_id, "Ann") == r2(month * 29 / 30)


def test_past_dated_change_needs_a_reason_and_a_finalized_period_stays_untouched(settings_world):
    """EC-CFG-03: finalize PG-C (Dee) for the period, then backdate a basis change into it."""
    from tests.payroll.test_run_finalize import ATTESTATIONS, approve, finalize, step_up, submit

    w = settings_world["alpha"]
    admin = w["admin"]
    month = monthly_basic(w["period"])
    manual_lop(w, "Dee", weekdays(w)[0])
    attestation = ok(admin.post(ATTESTATIONS, json={"legal_entity_id": w["entities"]["VA2"],
                                                    "effective_from": "2020-01-01",
                                                    "reason": "Statutory filings handled by our bureau"}), 201)
    ok(second_admin(w).post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
    run_id = run_for(w, "PG-C")
    assert basic_of(w, run_id, "Dee") == month - DAY_RATE              # CALENDAR_DAYS: N - 1 days
    submit(admin, run_id)
    approve(w["approver"], run_id)
    step_up(admin)
    finalize(admin, run_id, key=f"fin-{run_id}")
    assert run_detail(admin, run_id)["status"] in ("FINALIZED", "PAYMENT_READY")
    lines_before = sql("SELECT component_code, amount FROM payroll_lines WHERE run_id = :r ORDER BY id", r=run_id) \
        if sql("SELECT 1 FROM information_schema.columns WHERE table_name = 'payroll_lines' AND "
               "column_name = 'component_code'") else None

    group = w["groups"]["PG-C"]
    r = admin.put(f"{PAY}/settings/values", json={"changes": [{
        "key": BASIS, "value": "FIXED_30", "scope_type": "PAY_GROUP", "scope_id": group, "row_version": 0,
        "effective_from": w["start"].isoformat()}]})
    assert r.status_code == 400 and code_of(r) == "REASON_REQUIRED", r.text
    put_setting(admin, BASIS, "FIXED_30", "PAY_GROUP", group, effective_from=w["start"],
                reason="Contract says 30-day months (retro)")
    detail = run_detail(admin, run_id)
    assert detail["status"] in ("FINALIZED", "PAYMENT_READY") and detail["stale"] is False
    assert basic_of(w, run_id, "Dee") == month - DAY_RATE              # the finalized run is untouched
    replay = ok(admin.post(f"{RUNS}/{run_id}/replay"))
    assert replay["matched"] == replay["checked"] == 1
    if lines_before is not None:
        assert sql("SELECT component_code, amount FROM payroll_lines WHERE run_id = :r ORDER BY id",
                   r=run_id) == lines_before


def test_invalid_stored_payroll_value_fails_closed_in_the_precheck(settings_world):
    """EC-CFG-09: a stored value that no longer validates is never replaced by a default. Only a registry change
    can produce such a row, so it is written with the operator path."""
    w = settings_world["alpha"]
    sql("UPDATE setting_values SET value = '\"FIXED_99\"'::jsonb WHERE company_id = :c AND key = :k AND "
        "scope_type = 'TENANT' AND status = 'ACTIVE'", c=w["cid"], k=BASIS)
    found = resolve(w["admin"], BASIS, employee_id=w["emp"]["Ann"], on=w["end"])
    assert found["valid"] is False
    run = create_run(w["admin"], w["groups"]["PG-A"], w["periods"]["PG-A"]["id"])
    findings = precheck(w["admin"], run["id"])
    assert any(f["code"] == "CONFIG_INVALID" and f["severity"] == "BLOCKING" for f in findings), findings


# ---------------------------------------------------------------------------
# Stale runs after a change of locked inputs (EC-CFG-14)
# ---------------------------------------------------------------------------
def test_setting_policy_and_assignment_changes_after_lock_mark_the_run_stale(settings_world):
    w = settings_world["alpha"]
    admin = w["admin"]
    run_id = run_for(w, "PG-A")
    assert run_detail(admin, run_id)["stale"] is False
    # 1. A setting of another legal entity does not touch this run; one of its pay group does.
    put_setting(admin, BASIS, "FIXED_30", "LEGAL_ENTITY", w["entities"]["VA2"], effective_from=w["start"],
                reason=REASON)
    assert run_detail(admin, run_id)["stale"] is False
    put_setting(admin, BASIS, "FIXED_30", "PAY_GROUP", w["groups"]["PG-A"], effective_from=w["start"], reason=REASON)
    detail = run_detail(admin, run_id)
    assert detail["stale"] is True and detail["stale_reasons"]
    recalc(admin, run_id)
    assert run_detail(admin, run_id)["stale"] is False
    # 2. A new policy version in force for the period.
    activate_policy(admin, "LATE_PENALTY_POLICY", {"free_late_marks": 1},
                    policy_id=ok(admin.get(f"{PAY}/policies", params={"type": "LATE_PENALTY_POLICY"}))[0]["id"])
    assert run_detail(admin, run_id)["stale"] is True
    recalc(admin, run_id)
    assert run_detail(admin, run_id)["stale"] is False
    # 3. Changed assignment rules move employees to another policy: the locked inputs are out of date too.
    #    "Strict" applies to PG-A (priority 10) and is activated (stale, recalculated); then its rule moves to
    #    PG-B, so Ann and Ben fall back to the default policy without any new version (regression, EC-CFG-14).
    strict, _ = activate_policy(admin, "ATTENDANCE_PAY_POLICY", {"unmarked_day": "ABSENT"}, name="Strict",
                                criteria={"pay_group_ids": [w["groups"]["PG-A"]]}, priority=10)
    assert run_detail(admin, run_id)["stale"] is True
    recalc(admin, run_id)
    assert run_detail(admin, run_id)["stale"] is False
    ok(admin.put(f"{PAY}/policies/{strict}/assignments", json={"rules": [{
        "criteria": {"pay_group_ids": [w["groups"]["PG-B"]]}, "priority": 10, "effective_from": "2020-01-01"}]},
        headers=idem()))
    detail = run_detail(admin, run_id)
    assert detail["stale"] is True and any((r.get("policy_id") == strict) for r in detail["stale_reasons"]), detail


def test_setting_change_after_calculation_makes_submit_refuse_with_run_stale(settings_world):
    """EC-CFG-14 / EC-RUN-09: a setting changed after the inputs were locked marks the draft stale, the submit gate
    refuses it with 409 RUN_STALE (nothing is submitted), and only a recalculation with the new value lets it
    through. The bystander tenant's run is never marked."""
    from tests.payroll.test_run_finalize import ATTESTATIONS, submit

    w, beta = settings_world["alpha"], settings_world["beta"]
    admin = w["admin"]
    month = monthly_basic(w["period"])
    manual_lop(w, "Ann", weekdays(w)[0])
    attestation = ok(admin.post(ATTESTATIONS, json={"legal_entity_id": w["entities"]["VA1"],
                                                    "effective_from": "2020-01-01",
                                                    "reason": "Statutory filings handled by our bureau"}), 201)
    ok(second_admin(w).post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
    run_id = run_for(w, "PG-A")
    # CALENDAR_DAYS (starter): one LOP day is exactly 1,000.00.
    assert basic_of(w, run_id, "Ann") == month - DAY_RATE
    before = run_detail(admin, run_id)
    assert before["stale"] is False
    put_setting(admin, BASIS, "FIXED_30", "PAY_GROUP", w["groups"]["PG-A"], effective_from=w["start"], reason=REASON)
    assert run_detail(admin, run_id)["stale"] is True
    r = admin.post(f"{RUNS}/{run_id}/submit", json={}, headers=idem())
    assert r.status_code == 409 and code_of(r) == "RUN_STALE", r.text
    after = run_detail(admin, run_id)
    assert after["status"] == before["status"] and after["stale"] is True      # nothing was submitted
    # The bystander's draft is untouched by Alpha's change.
    assert run_detail(beta["admin"], beta["run"])["stale"] is False
    recalc(admin, run_id)
    # FIXED_30 for PG-A: 29 of 30 units paid -> month x 29 / 30, rounded half-up to the penny.
    assert basic_of(w, run_id, "Ann") == r2(month * 29 / 30)
    submitted = submit(admin, run_id)
    assert submitted["status"] == "APPROVAL_PENDING", submitted


# ---------------------------------------------------------------------------
# BRANCH level (02 §14): between PAY_GROUP and LEGAL_ENTITY, for the keys that allow it
# ---------------------------------------------------------------------------
def test_branch_value_wins_over_the_legal_entity_only_for_that_branch(settings_world):
    """holiday.optional_quota_per_year (TENANT / LEGAL_ENTITY / BRANCH) drives the optional-holiday quota the
    service hands each employee (GET /payroll/optional-holidays)."""
    w, beta = settings_world["alpha"], settings_world["beta"]
    admin = w["admin"]
    key = "holiday.optional_quota_per_year"
    year = w["end"].year

    def quota(client, employee_id):
        return ok(client.get(f"{PAY}/optional-holidays", params={"year": year, "employee_id": employee_id}))["quota"]

    # Starter: 2 for everyone.
    assert {p: quota(admin, w["emp"][p]) for p in ("Ann", "Cal", "Dee")} == {"Ann": 2, "Cal": 2, "Dee": 2}
    put_setting(admin, key, 5, "BRANCH", w["branches"]["LON"], effective_from=date(year, 1, 1),
                reason=REASON)                                       # Ann and Ben work at LON
    put_setting(admin, key, 3, "LEGAL_ENTITY", w["entities"]["VA1"], effective_from=date(year, 1, 1),
                reason=REASON)                                       # LON and MAN belong to VA1
    found = resolve(admin, key, employee_id=w["emp"]["Ann"], on=date(year, 12, 31))
    assert (found["value"], found["level"]) == (5, "BRANCH"), found
    found = resolve(admin, key, employee_id=w["emp"]["Cal"], on=date(year, 12, 31))
    assert (found["value"], found["level"]) == (3, "LEGAL_ENTITY"), found
    # The branch value wins at LON only; MAN gets the entity value; VA2 (Dee) keeps the tenant value.
    assert {p: quota(admin, w["emp"][p]) for p in ("Ann", "Ben", "Cal", "Dee")} == {
        "Ann": 5, "Ben": 5, "Cal": 3, "Dee": 2}
    # Another tenant is unchanged.
    assert quota(beta["admin"], beta["emp"]["Zed"]) == 2
    assert resolve(beta["admin"], key, employee_id=beta["emp"]["Zed"])["level"] == "TENANT"
