"""Wage inputs: components, target periods, approvals, recurrence, duplicates, scope, imports and the
exactly-once consumption API (3.4 §13, 3.1 §5.9, 3.9 §3.5/§4), plus row-level isolation of the wage
input tables across three tenants.

EC-VAR-01, EC-VAR-02, EC-VAR-03, EC-VAR-12, EC-VAR-16, EC-SEC-01, EC-SEC-05.

The fixtures and helpers here are shared with test_claims.py and test_loans.py.
"""

import calendar
import json
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.helpers import create_employee, invite_and_activate, signup_company

BASE = "/api/v1/payroll/wage-inputs"
IMPORTS = "/api/v1/payroll/wage-input-imports"
POLICIES = "/api/v1/payroll/policies"
CHECK, UNIQUE, FOREIGN_KEY, DENIED, RAISED = "23514", "23505", "23503", "42501", "P0001"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


@contextmanager
def session(company_id, user_id=None):
    from app.core.principal import build_principal
    from app.database.db import SessionLocal, bind_tenant
    from app.models.company import Company
    from app.models.user import User

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        principal = None
        if user_id is not None:
            principal = build_principal(db, db.get(User, user_id), None, db.get(Company, company_id))
        yield db, principal
    finally:
        db.close()


@contextmanager
def tenant(company_id):
    from app.database.db import engine

    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.company_id', :c, true)"),
                     {"c": "" if company_id is None else str(company_id)})
        yield conn


def expect_error(conn, code, sql_text, **params):
    with pytest.raises(DBAPIError) as info:
        with conn.begin_nested():
            conn.execute(text(sql_text), params)
    assert info.value.orig.pgcode == code, info.value.orig


def today_of(company_id):
    from app.core.clock import company_today

    with session(company_id) as (db, _):
        return company_today(db, company_id)


def month_of(day):
    return day.replace(day=1), day.replace(day=calendar.monthrange(day.year, day.month)[1])


def next_month(day):
    return month_of(month_of(day)[1] + timedelta(days=1))


def set_setting(company_id, key, value):
    """The tenant value of a setting from 2000-01-01 (replacing the starter row)."""
    payload = json.dumps(value)
    sql("DELETE FROM setting_values WHERE company_id = :c AND key = :k AND scope_type = 'TENANT'", c=company_id, k=key)
    sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
        "row_version, source, created_at, updated_at) VALUES (:c, :k, 'TENANT', NULL, CAST(:v AS jsonb), false, "
        "'2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id, k=key, v=payload)


def pay_group(company_id, entity_id, code, currency="INR", frequency="MONTHLY"):
    return sql(
        "INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency, calendar_rules, "
        "status, created_at, updated_at) VALUES (:c, :le, :code, :name, :cur, :f, "
        "'{\"monthly_start_day\": 1}'::jsonb, 'ACTIVE', now(), now()) RETURNING id",
        c=company_id, le=entity_id, code=code, name=f"Group {code}", cur=currency, f=frequency,
    )[0][0]


def place(employee_id, entity_id, pay_group_id):
    sql("UPDATE employee_assignments SET pay_group_id = :pg WHERE employee_id = :e AND legal_entity_id = :le",
        pg=pay_group_id, e=employee_id, le=entity_id)


def component(company_id, code, method="INPUT", kind="EARNING", category="BONUS", params=None,
              approval_above=None, approval_currency=None):
    """The id of the tenant's component `code`, in force since 2020-01-01.

    A new tenant is provisioned with the country-neutral catalogue (component_seed: BASIC, BONUS,
    COMMISSION, ...), so a seeded code is reused rather than inserted twice (the code is unique per
    tenant). The seeded definition must be the one the caller asks for; a test that needs particular
    params or an approval threshold uses a code outside the seeded set. The seeded version starts on the
    signup day, so it is made effective from 2020-01-01 like the components created here, for inputs of
    the whole current period to resolve it.
    """
    from app.models.compensation import PayComponent, PayComponentVersion

    existing = sql("SELECT p.id, p.kind, p.category, p.status, v.calc_method, v.params, v.input_approval_above "
                   "FROM pay_components p JOIN pay_component_versions v ON v.company_id = p.company_id AND "
                   "v.component_id = p.id AND v.version = 1 WHERE p.company_id = :c AND p.code = :code",
                   c=company_id, code=code)
    if existing:
        [(component_id, have_kind, have_category, status, have_method, have_params, have_threshold)] = existing
        assert (have_kind, have_category, status, have_method) == (kind, category, "ACTIVE", method), (
            f"{code} already exists as {have_kind}/{have_category}/{have_method} ({status}); use another code")
        assert not params and approval_above is None and not have_params and have_threshold is None, (
            f"{code} is a seeded component; use a code outside the seeded set for specific params")
        sql("UPDATE pay_component_versions SET effective_from = DATE '2020-01-01' WHERE company_id = :c "
            "AND component_id = :p AND version = 1", c=company_id, p=component_id)
        return component_id

    with session(company_id) as (db, _):
        row = PayComponent(company_id=company_id, code=code, kind=kind, category=category, owner="COMPANY",
                           status="ACTIVE")
        db.add(row)
        db.flush()
        db.add(PayComponentVersion(
            company_id=company_id, component_id=row.id, version=1, name=code.replace("_", " ").title(),
            calc_method=method, params=params or {}, effective_from=date(2020, 1, 1), status="ACTIVE",
            input_approval_above=approval_above, input_approval_currency=approval_currency,
        ))
        db.commit()
        return row.id


def role(admin, name, scope, codes):
    r = admin.post("/api/v1/roles", json={"name": name, "grants": [{"permission": c, "scope": scope} for c in codes]})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def user_with_role(app, admin, employee, role_id, entity_ids=None):
    client = invite_and_activate(app, admin, employee, [])
    r = admin.put(f"/api/v1/users/{client.user_id}/roles", json={"role_ids": [role_id]})
    assert r.status_code == 200, r.text
    if entity_ids:
        r = admin.put(f"/api/v1/users/{client.user_id}/scope-targets",
                      json={"assignments": [{"role_id": role_id, "legal_entity_ids": entity_ids}]})
        assert r.status_code == 200, r.text
    return client


def activate_policy(admin, type_code, fields):
    """A new active version of the tenant's policy of the type, effective 2020-01-01, applying to everyone."""
    policies = admin.get(POLICIES, params={"type": type_code}).json()
    policies = policies if isinstance(policies, list) else policies.get("items", [])
    if policies:
        policy_id = policies[0]["id"]
        r = admin.post(f"{POLICIES}/{policy_id}/versions", json={"fields": fields, "effective_from": "2020-01-01"})
        assert r.status_code == 201, r.text
        version = r.json()["version"]
    else:
        r = admin.post(POLICIES, json={"type": type_code, "name": f"Test {type_code}", "fields": fields,
                                       "effective_from": "2020-01-01"})
        assert r.status_code == 201, r.text
        policy_id, version = r.json()["id"], 1
    r = admin.put(f"{POLICIES}/{policy_id}/assignments",
                  json={"rules": [{"criteria": {"everyone": True}, "effective_from": "2020-01-01"}]})
    assert r.status_code == 200, r.text
    r = admin.post(f"{POLICIES}/{policy_id}/versions/{version}/activate", json={"reason": "Test policy"})
    assert r.status_code == 200, r.text
    return policy_id


def audit_actions(company_id, entity_type, entity_id):
    return [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = :t "
                              "AND entity_id = :e ORDER BY id", c=company_id, t=entity_type, e=str(entity_id))]


@pytest.fixture
def world(app):
    """Acme: LE01 (INR, pay group PG-IN: Emma, Ivan) and AE01 (AED, PG-AE: Gus); the founder (payroll manager),
    processor Pia (COMPANY), entity clerk Cleo (LE01 only), employees Emma and Gus with logins. Beta, Cobalt."""
    acme = signup_company(app, "Acme Corp")
    beta = signup_company(app, "Beta Industries")
    cobalt = signup_company(app, "Cobalt Labs")
    cid = acme.company_id
    le01 = next(e for e in acme.get("/api/v1/legal-entities").json() if e["code"] == "LE01")
    r = acme.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Acme Gulf FZ-LLC",
                                                   "country_code": "AE", "base_currency": "AED",
                                                   "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    ae = r.json()
    r = acme.post("/api/v1/branches", json={"legal_entity_id": ae["id"], "code": "DXB", "name": "Dubai"})
    assert r.status_code == 200, r.text
    dubai = r.json()
    groups = {"IN": pay_group(cid, le01["id"], "PG-IN"), "AE": pay_group(cid, ae["id"], "PG-AE", currency="AED")}
    emp = {name: create_employee(acme, name, hire_date="2024-01-15")
           for name in ("Emma", "Ivan", "Gus", "Pia", "Cleo")}
    r = acme.post(f"/api/v1/employees/{emp['Gus']['id']}/assignments",
                  json={"branch_id": dubai["id"], "effective_from": "2024-01-16", "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    for name in ("Emma", "Ivan", "Pia", "Cleo"):
        place(emp[name]["id"], le01["id"], groups["IN"])
    place(emp["Gus"]["id"], ae["id"], groups["AE"])
    clerk_role = role(acme, "Entity payroll clerk", "LEGAL_ENTITY", (
        "payroll:inputs:read", "payroll:inputs:edit", "payroll:inputs:import", "payroll:inputs:approve",
        "payroll:reimbursements:read", "payroll:reimbursements:manage", "payroll:reimbursements:approve",
        "payroll:loans:read", "payroll:loans:manage", "payroll:loans:approve"))
    users = {
        "founder": acme,
        "pia": invite_and_activate(app, acme, emp["Pia"], ["employee", "payroll_processor"]),
        "cleo": user_with_role(app, acme, emp["Cleo"], clerk_role, [le01["id"]]),
        "emma": invite_and_activate(app, acme, emp["Emma"], ["employee"]),
        "gus": invite_and_activate(app, acme, emp["Gus"], ["employee"]),
        "beta": beta,
        "cobalt": cobalt,
    }
    acme.user_id = acme.me()["user"]["id"]
    today = today_of(cid)
    return {"cid": cid, "today": today, "period": month_of(today), "le01": le01, "ae": ae, "groups": groups,
            "emp": {k: v["id"] for k, v in emp.items()}, "codes": {k: v["employee_code"] for k, v in emp.items()},
            "u": users}


# ---------------------------------------------------------------------------
# Wage-input helpers
# ---------------------------------------------------------------------------
def body(world, employee="Emma", code="BONUS", amount="5000.00", currency="INR", period=None, **extra):
    start, end = period or world["period"]
    data = {"employee_id": world["emp"][employee], "component_code": code, "period_start": start.isoformat(),
            "period_end": end.isoformat(), **extra}
    if amount is not None:
        data["amount"] = {"amount": amount, "currency": currency}
    return data


def create(client, data):
    return client.post(BASE, json=data)


def created(client, data):
    r = create(client, data)
    assert r.status_code == 201, r.text
    return r.json()["items"]


def get(client, input_id):
    r = client.get(f"{BASE}/{input_id}")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def inputs_world(world):
    """BONUS, COMMISSION and BASIC are the provisioned catalogue's; CLAWBACK and OT_HOURS are added."""
    cid = world["cid"]
    seeded = dict(sql("SELECT code, id FROM pay_components WHERE company_id = :c AND code IN "
                      "('BONUS', 'COMMISSION', 'BASIC')", c=cid))
    assert set(seeded) == {"BONUS", "COMMISSION", "BASIC"}  # provisioned at signup
    world["components"] = {
        "BONUS": component(cid, "BONUS"),
        "COMMISSION": component(cid, "COMMISSION", category="COMMISSION"),
        "CLAWBACK": component(cid, "CLAWBACK", params={"allow_negative_input": True}),
        "OT_HOURS": component(cid, "OT_HOURS", method="RATE_X_UNITS", category="OVERTIME"),
        "BASIC": component(cid, "BASIC", method="FIXED", category="BASE"),
    }
    assert all(world["components"][code] == seeded[code] for code in seeded)  # reused, never a second row
    return world


# ---------------------------------------------------------------------------
# Lifecycle and approval
# ---------------------------------------------------------------------------
def test_input_is_submitted_for_approval_and_approved_by_another_user(inputs_world):
    w = inputs_world
    pia, founder = w["u"]["pia"], w["u"]["founder"]
    [item] = created(pia, body(w))
    assert item["status"] == "PENDING_APPROVAL" and item["approval_request_id"]
    row = get(pia, item["id"])
    assert row["amount"] == {"amount": "5000.00", "currency": "INR"} and row["component_code"] == "BONUS"
    assert row["pay_group_id"] == w["groups"]["IN"] and row["source"] == "MANUAL"

    r = pia.post(f"{BASE}/{item['id']}/approve", json={})
    assert r.status_code == 403, r.text  # the maker never approves their own input
    r = founder.post(f"{BASE}/{item['id']}/approve", json={"remarks": "ok"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"
    assert founder.post(f"{BASE}/{item['id']}/approve", json={}).status_code == 409
    assert audit_actions(w["cid"], "wage_input", item["id"]) == [
        "WAGE_INPUT_CREATED", "WAGE_INPUT_SUBMITTED", "WAGE_INPUT_APPROVED"]
    module = sql("SELECT DISTINCT module FROM audit_logs WHERE company_id = :c AND entity_type = 'wage_input'",
                 c=w["cid"])
    assert module == [("Payroll Inputs",)]


def test_reject_needs_a_reason_and_ends_the_input(inputs_world):
    w = inputs_world
    [item] = created(w["u"]["pia"], body(w))
    founder = w["u"]["founder"]
    assert founder.post(f"{BASE}/{item['id']}/reject", json={"remarks": " "}).status_code == 422
    r = founder.post(f"{BASE}/{item['id']}/reject", json={"remarks": "Not agreed"})
    assert r.status_code == 200, r.text
    row = get(founder, item["id"])
    assert (row["status"], row["status_reason"]) == ("REJECTED", "Not agreed")
    assert w["u"]["pia"].post(f"{BASE}/{item['id']}/cancel", json={"reason": "x"}).status_code == 409


def test_input_at_or_below_the_approval_threshold_is_approved_on_submission(inputs_world):
    w = inputs_world
    set_setting(w["cid"], "payroll.wage_inputs.require_approval_above", {"amount": "10000.00", "currency": "INR"})
    pia = w["u"]["pia"]
    [small] = created(pia, body(w, amount="10000.00"))
    assert small["status"] == "APPROVED" and small["approval_request_id"] is None
    [large] = created(pia, body(w, amount="10000.01", external_ref="L-1"))
    assert large["status"] == "PENDING_APPROVAL"
    [negative] = created(pia, body(w, code="CLAWBACK", amount="-500.00"))
    assert negative["status"] == "PENDING_APPROVAL"  # EC-VAR-12: a clawback always needs approval
    [units] = created(pia, body(w, code="OT_HOURS", amount=None, units="2.5", rate="300.00"))
    assert units["status"] == "PENDING_APPROVAL"  # no amount to compare with the threshold
    assert "WAGE_INPUT_APPROVED" in audit_actions(w["cid"], "wage_input", small["id"])


def test_draft_inputs_edit_with_row_versions_and_submit_later(inputs_world):
    w = inputs_world
    pia = w["u"]["pia"]
    [item] = created(pia, body(w, submit=False))
    assert item["status"] == "DRAFT"
    r = pia.patch(f"{BASE}/{item['id']}", json={"row_version": 1, "amount": {"amount": "6000.00", "currency": "INR"},
                                                "reason": "Corrected"})
    assert r.status_code == 200, r.text
    assert pia.patch(f"{BASE}/{item['id']}", json={"row_version": 1, "reason": "stale"}).status_code == 409
    assert get(pia, item["id"])["amount"]["amount"] == "6000.00"
    r = pia.post(f"{BASE}/{item['id']}/submit")
    assert r.status_code == 200 and r.json()["status"] == "PENDING_APPROVAL", r.text
    request_id = r.json()["approval_request_id"]
    assert pia.patch(f"{BASE}/{item['id']}", json={"row_version": 3, "reason": "late"}).status_code == 409
    r = pia.post(f"{BASE}/{item['id']}/cancel", json={"reason": "Entered twice"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text
    assert sql("SELECT status FROM approval_requests WHERE id = :i", i=request_id) == [("CANCELLED",)]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_component_must_be_input_or_units_based_and_amounts_are_strings(inputs_world):
    w = inputs_world
    pia = w["u"]["pia"]
    r = create(pia, body(w, code="BASIC"))
    assert (r.status_code, r.json()["code"]) == (400, "COMPONENT_NOT_INPUT")
    r = create(pia, body(w, code="NOPE"))
    assert (r.status_code, r.json()["code"]) == (400, "COMPONENT_NOT_FOUND")
    r = create(pia, body(w, code="BONUS", amount=None, units="3"))
    assert (r.status_code, r.json()["code"]) == (400, "UNITS_NOT_ALLOWED")
    r = create(pia, body(w, code="OT_HOURS"))
    assert (r.status_code, r.json()["code"]) == (400, "UNITS_REQUIRED")
    r = create(pia, body(w, code="OT_HOURS", amount=None, units="-1"))
    assert (r.status_code, r.json()["code"]) == (400, "UNITS_NOT_POSITIVE")
    r = create(pia, body(w, amount="-100.00"))
    assert (r.status_code, r.json()["code"]) == (400, "AMOUNT_NEGATIVE")
    r = create(pia, body(w, amount="100.001"))
    assert r.status_code == 422, r.text
    data = body(w)
    data["amount"] = {"amount": 5000, "currency": "INR"}
    assert create(pia, data).status_code == 422  # a JSON number is never money
    for extra in ({"status": "APPROVED"}, {"company_id": w["cid"]}, {"source": "SYSTEM"}):
        assert create(pia, body(w, **extra)).status_code == 422  # server-owned fields
    r = create(pia, body(w, code="CLAWBACK", amount="-250.00"))
    assert r.status_code == 201, r.text


def test_currency_is_the_pay_group_currency_unless_an_fx_rule_is_requested(inputs_world):
    w = inputs_world
    pia = w["u"]["pia"]
    r = create(pia, body(w, currency="USD", amount="100.00"))
    assert (r.status_code, r.json()["code"]) == (400, "CURRENCY_MISMATCH")
    [item] = created(pia, body(w, currency="USD", amount="100.00", convert_currency=True))
    row = get(pia, item["id"])
    assert row["currency"] == "USD" and row["fx_rule"]["to"] == "INR"
    [gulf] = created(pia, body(w, employee="Gus", currency="AED", amount="750.00"))
    assert get(pia, gulf["id"])["pay_group_id"] == w["groups"]["AE"]


def test_ec_var_03_closed_period_is_rejected_and_the_next_open_period_suggested(inputs_world, monkeypatch):
    from app.payroll.services import wage_inputs

    w = inputs_world
    pia = w["u"]["pia"]
    start, end = w["period"]
    r = create(pia, body(w, period=(start, end - timedelta(days=1))))
    assert (r.status_code, r.json()["code"]) == (400, "TARGET_PERIOD_INVALID")
    assert f"{start.isoformat()} to {end.isoformat()}" in r.json()["detail"]
    monkeypatch.setattr(wage_inputs, "period_is_open", lambda db, cid, pg, s, e: s != start)
    r = create(pia, body(w))
    assert (r.status_code, r.json()["code"]) == (409, "PERIOD_CLOSED")
    following = next_month(start)
    assert f"Next open period: {following[0].isoformat()} to {following[1].isoformat()}" in r.json()["detail"]
    [item] = created(pia, body(w, period=following))
    assert get(pia, item["id"])["target_period"] == {"id": None, "start": following[0].isoformat(),
                                                    "end": following[1].isoformat()}


def test_ec_var_02_external_ref_is_unique_and_identical_amounts_are_flagged(inputs_world):
    w = inputs_world
    pia = w["u"]["pia"]
    [first] = created(pia, body(w, external_ref="BONUS-2026-E1"))
    r = create(pia, body(w, external_ref="BONUS-2026-E1"))
    assert (r.status_code, r.json()["code"]) == (409, "DUPLICATE_EXTERNAL_REF")
    [other_period] = created(pia, body(w, external_ref="BONUS-2026-E1", period=next_month(w["period"][0])))
    assert other_period["flags"] == []
    [commission] = created(pia, body(w, code="COMMISSION"))  # bonus and commission for the same thing
    assert commission["flags"] == ["POSSIBLE_DUPLICATE"]
    flag = get(pia, commission["id"])["flags"][0]
    assert flag["input_ids"] == [first["id"]]
    # A cancelled input frees its reference.
    assert pia.post(f"{BASE}/{first['id']}/cancel", json={"reason": "dup"}).status_code == 200
    created(pia, body(w, external_ref="BONUS-2026-E1"))
    with tenant(w["cid"]) as conn:  # the database enforces it too
        expect_error(conn, UNIQUE,
                     "INSERT INTO wage_inputs (company_id, employee_id, legal_entity_id, pay_group_id, component_id, "
                     "component_code, amount, currency, target_period_start, target_period_end, status, "
                     "external_ref) VALUES (:c, :e, :le, :pg, :comp, 'BONUS', 1, 'INR', :s, :t, 'DRAFT', "
                     "'BONUS-2026-E1')", c=w["cid"], e=w["emp"]["Emma"], le=w["le01"]["id"], pg=w["groups"]["IN"],
                     comp=w["components"]["BONUS"], s=w["period"][0], t=w["period"][1])


def test_recurring_input_creates_one_input_per_period_approved_together(inputs_world):
    w = inputs_world
    pia, founder = w["u"]["pia"], w["u"]["founder"]
    start = w["period"][0]
    third = next_month(next_month(start)[0])
    items = created(pia, body(w, amount="1500.00", recurrence={"until": third[1].isoformat()}, external_ref="ALLOW"))
    assert len(items) == 3
    parent, *children = items
    assert all(c["recurrence_parent_id"] == parent["id"] for c in children)
    r = pia.post(f"{BASE}/{children[0]['id']}/submit")
    assert (r.status_code, r.json()["code"]) == (409, "SERIES_MEMBER")
    r = founder.post(f"{BASE}/{children[1]['id']}/approve", json={})
    assert r.status_code == 200, r.text
    statuses = {i["id"]: get(pia, i["id"])["status"] for i in items}
    assert set(statuses.values()) == {"APPROVED"}
    periods = [get(pia, i["id"])["target_period"]["start"] for i in items]
    assert periods == sorted(periods) and len(set(periods)) == 3
    r = create(pia, body(w, recurrence={"until": (start + timedelta(days=900)).isoformat()}))
    assert (r.status_code, r.json()["code"]) == (400, "RECURRENCE_TOO_LONG")


# ---------------------------------------------------------------------------
# Scope and tenants
# ---------------------------------------------------------------------------
def test_ec_sec_05_scope_decides_who_sees_creates_and_approves_inputs(inputs_world):
    w = inputs_world
    pia, cleo, emma, beta = w["u"]["pia"], w["u"]["cleo"], w["u"]["emma"], w["u"]["beta"]
    [indian] = created(pia, body(w))
    [gulf] = created(pia, body(w, employee="Gus", currency="AED", amount="900.00"))

    r = create(cleo, body(w, employee="Gus", currency="AED", amount="10.00"))
    assert r.status_code == 404, r.text  # outside the clerk's legal entity
    [own_entity] = created(cleo, body(w, employee="Ivan"))
    listed = cleo.get(BASE).json()
    assert {i["id"] for i in listed["items"]} == {indian["id"], own_entity["id"]} and listed["total"] == 2
    assert cleo.get(f"{BASE}/{gulf['id']}").status_code == 404
    assert cleo.post(f"{BASE}/{gulf['id']}/approve", json={}).status_code == 404
    assert cleo.post(f"{BASE}/{indian['id']}/approve", json={}).status_code == 200

    assert emma.get(BASE).status_code == 403  # no payroll:inputs:read
    assert create(emma, body(w)).status_code == 403
    assert beta.get(f"{BASE}/{indian['id']}").status_code == 404  # another tenant
    assert beta.get(BASE).json()["total"] == 0
    r = create(beta, body(w))
    assert r.status_code == 404, r.text  # Acme's employee does not exist for Beta


# ---------------------------------------------------------------------------
# Payroll consumption (EC-VAR-01)
# ---------------------------------------------------------------------------
def test_ec_var_01_inputs_are_reserved_consumed_exactly_once_and_released(inputs_world):
    from app.core.errors import AppError
    from app.payroll.services import wage_inputs

    w = inputs_world
    cid = w["cid"]
    set_setting(cid, "payroll.wage_inputs.require_approval_above", {"amount": "99999.00", "currency": "INR"})
    pia = w["u"]["pia"]
    [a] = created(pia, body(w, amount="1000.00"))
    [b] = created(pia, body(w, code="COMMISSION", amount="2000.00"))
    [draft] = created(pia, body(w, amount="7.00", submit=False))
    start, end = w["period"]
    with session(cid) as (db, _):
        rows = wage_inputs.approved_inputs(db, cid, w["emp"]["Emma"], start, end)
        assert [(r["id"], r["amount"], r["currency"]) for r in rows] == [(a["id"], "1000.00", "INR"),
                                                                        (b["id"], "2000.00", "INR")]
        assert all(len(r["version_hash"]) == 64 for r in rows)
        wage_inputs.reserve_inputs(db, cid, 501, [a["id"], b["id"]])
        db.commit()
    with session(cid) as (db, _):
        assert wage_inputs.approved_inputs(db, cid, w["emp"]["Emma"], start, end) == []
        assert len(wage_inputs.approved_inputs(db, cid, w["emp"]["Emma"], start, end, run_employee_id=501)) == 2
        with pytest.raises(AppError) as info:
            wage_inputs.reserve_inputs(db, cid, 502, [a["id"]])
        assert info.value.code == "INPUT_RESERVED"
        with pytest.raises(AppError) as info:
            wage_inputs.reserve_inputs(db, cid, 501, [draft["id"]])
        assert info.value.code == "INPUT_NOT_APPROVED"
        db.rollback()
        assert wage_inputs.release_inputs(db, cid, [501]) == 2  # the run was reopened
        db.commit()
    with session(cid) as (db, _):
        assert wage_inputs.consume_inputs(db, cid, 777, [a["id"]]) == 1
        assert wage_inputs.consume_inputs(db, cid, 777, [a["id"]]) == 0  # a retried finalization
        with pytest.raises(AppError) as info:
            wage_inputs.consume_inputs(db, cid, 778, [a["id"]])
        assert info.value.code == "INPUT_ALREADY_CONSUMED"
        db.rollback()
        wage_inputs.consume_inputs(db, cid, 777, [a["id"]])
        assert wage_inputs.release_inputs(db, cid, [777]) == 0  # consumed inputs stay consumed
        db.commit()
    assert get(pia, a["id"])["status"] == "CONSUMED" and get(pia, a["id"])["consumed_by_run_employee_id"] == 777
    assert pia.post(f"{BASE}/{a['id']}/cancel", json={"reason": "late"}).status_code == 409
    with tenant(cid) as conn:
        expect_error(conn, CHECK, "UPDATE wage_inputs SET status = 'CONSUMED' WHERE id = :i", i=b["id"])


# ---------------------------------------------------------------------------
# CSV import (EC-VAR-16)
# ---------------------------------------------------------------------------
def _csv(*rows):
    header = "employee_code,component_code,amount,units,rate,currency,period_start,period_end,external_ref,reason"
    return ("\n".join((header, *rows)) + "\n").encode()


def test_ec_var_16_import_dry_run_reports_every_bad_row_and_commits_nothing_until_fixed(inputs_world, monkeypatch):
    from app.payroll.services import wage_inputs

    w = inputs_world
    pia = w["u"]["pia"]
    start, end = w["period"]
    emma, gus = w["codes"]["Emma"], w["codes"]["Gus"]
    s, e = start.isoformat(), end.isoformat()
    bad = _csv(
        f"{emma},BONUS,1000.00,,,INR,{s},{e},R-1,Festival",
        f"NOBODY,BONUS,1000.00,,,INR,{s},{e},R-2,",
        f"{emma},BONUS,1000.00,,,INR,{s},{e},R-1,duplicate ref",
        f"{emma},OT_HOURS,,-4,100,INR,{s},{e},R-3,negative hours",
        f"{emma},BONUS,=1+1,,,INR,{s},{e},R-4,",
        f"{emma},BONUS,10.00,,,INR,{s},{e},R-5,=2+5",
        f"{gus},BONUS,10.00,,,INR,{s},{e},R-6,wrong currency",
        f"{emma},BONUS,-10.00,,,INR,{s},{e},R-7,negative bonus",
        f"{emma},BONUS,10.00,,,INR,{s},2099-01-01,R-8,wrong period",
    )
    r = pia.post(f"{BASE}/import", files={"file": ("inputs.csv", bad, "text/csv")})
    assert r.status_code == 201, r.text
    result = r.json()
    assert (result["status"], result["row_count"], result["error_count"]) == ("INVALID", 9, 8)
    errors = {row["row"]: " ".join(row["errors"]) for row in result["rows"]}
    assert errors[2] == ""
    assert "unknown employee" in errors[3] and "duplicate of row 2" in errors[4]
    assert "negative" in errors[5] and "decimal" in errors[6] and "formula" in errors[7]
    assert "pays in AED" in errors[8] and "negative" in errors[9] and "not a pay period" in errors[10]
    r = pia.post(f"{IMPORTS}/{result['id']}/commit")
    assert (r.status_code, r.json()["code"]) == (409, "IMPORT_HAS_ERRORS")
    assert pia.get(BASE).json()["total"] == 0
    result_csv = pia.get(f"{IMPORTS}/{result['id']}/result").text
    assert result_csv.startswith("row,employee_code,outcome") and "unknown employee NOBODY" in result_csv

    good = _csv(f"{emma},BONUS,1000.00,,,INR,{s},{e},R-1,Festival",
                f"{emma},OT_HOURS,,4,100,INR,{s},{e},R-3,",
                f"{gus},BONUS,75.00,,,AED,{s},{e},G-1,")
    r = pia.post(f"{BASE}/import", files={"file": ("inputs.csv", good, "text/csv")})
    assert r.status_code == 201 and r.json()["status"] == "VALIDATED", r.text
    import_id = r.json()["id"]
    r = pia.post(f"{IMPORTS}/{import_id}/commit")
    assert r.status_code == 202, r.text
    job = pia.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "SUCCEEDED", job
    done = pia.get(f"{IMPORTS}/{import_id}").json()
    assert done["status"] == "COMMITTED" and done["result"]["created"] == 3
    items = pia.get(BASE).json()["items"]
    assert {(i["source"], i["status"]) for i in items} == {("IMPORT", "PENDING_APPROVAL")}
    assert pia.post(f"{IMPORTS}/{import_id}/commit").status_code == 409  # committed once
    assert "IMPORT_COMMITTED" in audit_actions(w["cid"], "wage_input_import", import_id)

    for malformed, code in ((b"", "IMPORT_EMPTY"), (b"employee_code,amount\nE1,1\n", "IMPORT_COLUMNS"),
                            (_csv(f'{emma},BONUS,"1,000') , "IMPORT_MALFORMED"), (b"\xff\xfe\x00", "IMPORT_MALFORMED")):
        r = pia.post(f"{BASE}/import", files={"file": ("x.csv", malformed, "text/csv")})
        assert (r.status_code, r.json()["code"]) == (400, code), (malformed, r.text)
    emma_client = w["u"]["emma"]
    assert emma_client.post(f"{BASE}/import", files={"file": ("x.csv", good, "text/csv")}).status_code == 403
    assert w["u"]["beta"].get(f"{IMPORTS}/{import_id}").status_code == 404
    template = pia.get(f"{IMPORTS}/template")
    assert template.status_code == 200 and template.text.startswith("employee_code,component_code")


def test_import_formula_like_cells_in_results_are_escaped():
    from app.payroll.services import wage_inputs

    assert wage_inputs._escape_cell("=SUM(A1)") == "'=SUM(A1)"
    assert wage_inputs._escape_cell("@cmd") == "'@cmd"
    assert wage_inputs._escape_cell("Bonus") == "Bonus"


# ---------------------------------------------------------------------------
# Row-level isolation (three tenants)
# ---------------------------------------------------------------------------
TABLES = ("wage_input_imports", "wage_inputs")


def test_ec_sec_01_wage_input_tables_are_isolated_across_three_tenants(world):
    from app.models import TENANT_TABLES

    assert set(TABLES) <= set(TENANT_TABLES)
    companies = [world["cid"], world["u"]["beta"].company_id, world["u"]["cobalt"].company_id]
    seeded = {}
    for index, cid in enumerate(companies):
        employee = world["emp"]["Emma"] if index == 0 else create_employee(
            world["u"]["beta" if index == 1 else "cobalt"], "Iso")["id"]
        entity = sql("SELECT id FROM legal_entities WHERE company_id = :c ORDER BY id LIMIT 1", c=cid)[0][0]
        group = world["groups"]["IN"] if index == 0 else pay_group(cid, entity, f"ISO{index}")
        if index:
            place(employee, entity, group)
        comp = component(cid, f"ISO_BONUS_{index}")
        with tenant(cid) as conn:
            imp = conn.execute(text(
                "INSERT INTO wage_input_imports (company_id, file_name, storage_path, sha256, bytes, status) "
                "VALUES (:c, 'f.csv', 'x', :h, 1, 'VALIDATED') RETURNING id"), {"c": cid, "h": "a" * 64}).scalar()
            inp = conn.execute(text(
                "INSERT INTO wage_inputs (company_id, employee_id, legal_entity_id, pay_group_id, component_id, "
                "component_code, amount, currency, target_period_start, target_period_end) VALUES (:c, :e, :le, :pg, "
                ":comp, 'X', 10, 'INR', '2026-10-01', '2026-10-31') RETURNING id"),
                {"c": cid, "e": employee, "le": entity, "pg": group, "comp": comp}).scalar()
        seeded[cid] = {"wage_input_imports": imp, "wage_inputs": inp, "employee": employee, "entity": entity,
                       "group": group, "component": comp}

    with tenant(None) as conn:
        assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES} == {t: 0 for t in TABLES}
    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            for table in TABLES:
                owners = {o for (o,) in conn.execute(text(f"SELECT company_id FROM {table}"))}
                assert owners == {cid}, table
                for other in others:
                    foreign = seeded[other][table]
                    assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :i"), {"i": foreign}).scalar() == 0
                    assert conn.execute(text(f"UPDATE {table} SET updated_at = now() WHERE id = :i"),
                                        {"i": foreign}).rowcount == 0
                    assert conn.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": foreign}).rowcount == 0
                expect_error(conn, DENIED, f"UPDATE {table} SET company_id = :o WHERE id = :i", o=others[0],
                             i=seeded[cid][table])
    a, b = companies[0], companies[1]
    with tenant(a) as conn:  # composite keys refuse another tenant's employee, pay group or component
        for column, value in (("employee_id", seeded[b]["employee"]), ("pay_group_id", seeded[b]["group"]),
                              ("component_id", seeded[b]["component"])):
            values = {"employee_id": seeded[a]["employee"], "pay_group_id": seeded[a]["group"],
                      "component_id": seeded[a]["component"], column: value}
            expect_error(conn, FOREIGN_KEY,
                         "INSERT INTO wage_inputs (company_id, employee_id, legal_entity_id, pay_group_id, component_id, "
                         "component_code, amount, currency, target_period_start, target_period_end) VALUES (:c, :e, "
                         ":le, :pg, :comp, 'X', 10, 'INR', '2026-11-01', '2026-11-30')",
                         c=a, e=values["employee_id"], le=seeded[a]["entity"], pg=values["pay_group_id"],
                         comp=values["component_id"])
