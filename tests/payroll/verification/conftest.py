"""Payroll v2 verification suites (Goal 2): shared by the settings-harness, golden/lifecycle and security agents.

Fixtures live in the test modules (or helper modules here); this file is intentionally light so each agent can
import what it needs without side effects.

Settings harness (Goal 2 §6, owner: settings-verification agent)
-----------------------------------------------------------------
``settings_world`` builds two tenants **through the real APIs** (signup, legal entities, branches, pay groups,
placements, pay calendars, salary structure, compensation assignment and approval by a second user). The only
direct SQL is read-only inspection of results. Nothing here calls the calculation engine or the services to
produce an expected value: tests write their expectations out by hand.

Tenant "Verity Alpha" (country GB, which has no country pack, so no statutory line can appear and every amount
is a pure function of the settings under test):

* legal entity ``VA1`` (GBP) with branches ``LON`` and ``MAN``; pay groups ``PG-A`` (Ann, Ben) and ``PG-B``
  (Cal), both monthly from day 1;
* legal entity ``VA2`` (GBP) with branch ``EDI`` and pay group ``PG-C`` (Dee);
* every paid employee is on the structure ``FLAT`` (CTC_BASED, one line ``BASIC = ctc.monthly * 100%``; BASIC is
  seeded with the tags LOP_APPLICABLE / OT_BASE and proration BY_PAID_DAYS), with an annual CTC of
  ``12 * DAY_RATE * N`` where N is the number of calendar days of the current pay period. A month's BASIC is
  therefore ``DAY_RATE * N`` and one calendar-day of LOP is exactly ``DAY_RATE`` (1,000.00 GBP);
* ``payroll.attendance.enabled`` is switched OFF at TENANT level from the period start (through the settings
  API, with a reason) so the baseline is the full monthly BASIC; each test switches what it verifies.

Tenant "Verity Beta" is the bystander for step 13 (other tenants unchanged): one entity, one pay group, one
paid employee (Zed) with the same structure and a calculated draft run.
"""

import calendar as _calendar
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.helpers import create_employee, signup_company
from tests.payroll.test_compensation import assign_and_approve, login_as

PAY = "/api/v1/payroll"
RUNS = f"{PAY}/runs"
VALUES = f"{PAY}/settings/values"
RESOLVE = f"{PAY}/settings/resolve"
REQUESTS = f"{PAY}/settings/change-requests"
POLICIES = f"{PAY}/policies"
PAY_GROUPS = f"{PAY}/org/pay-groups"
DAY_RATE = Decimal("1000.00")
BASELINE_REASON = "Verification baseline: attendance does not drive pay"


# ---------------------------------------------------------------------------
# Small API helpers (used by the settings harness tests)
# ---------------------------------------------------------------------------
def ok(response, status=200):
    assert response.status_code == status, f"{response.status_code}: {response.text}"
    return response.json()


def idem():
    return {"Idempotency-Key": f"v-{uuid.uuid4().hex}"}


def today_of(client):
    from app.core.clock import company_today
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    try:
        bind_tenant(db, client.company_id)
        return company_today(db, client.company_id)
    finally:
        db.close()


def scope_values(client, scope_type="TENANT", scope_id=None, **params):
    query = {"scope_type": scope_type, **params}
    if scope_id is not None:
        query["scope_id"] = scope_id
    return {item["key"]: item for item in ok(client.get(VALUES, params=query))["items"]}


def row_version(client, key, scope_type="TENANT", scope_id=None):
    return scope_values(client, scope_type, scope_id)[key]["row_version"]


def put_setting(client, key, value, scope_type="TENANT", scope_id=None, *, effective_from=None, reason=None,
                locked=None, version=None, expect=200):
    """PUT one change through the settings API with the scope's current row_version (or ``version``)."""
    change = {"key": key, "value": value, "scope_type": scope_type,
              "row_version": row_version(client, key, scope_type, scope_id) if version is None else version}
    if scope_id is not None:
        change["scope_id"] = scope_id
    if effective_from is not None:
        change["effective_from"] = effective_from.isoformat()
    if reason is not None:
        change["reason"] = reason
    if locked is not None:
        change["locked"] = locked
    r = client.put(VALUES, json={"changes": [change]})
    assert r.status_code == expect, f"{key}={value!r} at {scope_type}:{scope_id} -> {r.status_code}: {r.text}"
    return r.json()


def resolve(client, key, **scope):
    params = {"key": key, **{k: v for k, v in scope.items() if v is not None}}
    if "on" in params:
        params["date"] = params.pop("on").isoformat()
    return ok(client.get(RESOLVE, params=params))


def policy_for(client, type_code):
    items = ok(client.get(POLICIES, params={"type": type_code}))
    items = items if isinstance(items, list) else items.get("items", [])
    return items


def activate_policy(client, type_code, fields, *, name=None, criteria=None, priority=10, effective_from=None,
                    policy_id=None):
    """Activate policy fields through the policies API; returns ``(policy_id, version)``.

    Without ``name`` the tenant's existing policy of the type (the starter policy with its catch-all rule) gets a
    new version. With ``name`` a new policy is created and assigned by ``criteria`` at ``priority`` (an override
    above the catch-all). ``effective_from`` defaults to 2020-01-01 so the version covers the whole period.
    """
    start = (effective_from or date(2020, 1, 1)).isoformat()
    draft = None
    created_here = False
    if policy_id is None and name is None:
        existing = [p for p in policy_for(client, type_code) if p["status"] == "ACTIVE"]
        policy_id = existing[0]["id"] if existing else None
        if policy_id is None:
            name, criteria, priority = f"Verify {type_code}", criteria or {"everyone": True}, 0
        elif existing[0].get("active_version") is None and existing[0].get("draft_versions"):
            # A starter policy that is still a draft (e.g. overtime, I-13): complete and activate that draft.
            first = existing[0]["draft_versions"][0]
            draft = first["version"] if isinstance(first, dict) else first
    if draft is not None:
        ok(client.put(f"{POLICIES}/{policy_id}/versions/{draft}", json={"fields": fields, "effective_from": start},
                      headers=idem()))
        version = draft
    elif policy_id is None:
        created = ok(client.post(POLICIES, json={"type": type_code, "name": name, "fields": fields,
                                                 "effective_from": start}, headers=idem()), 201)
        policy_id, created_here = created["id"], True
        version = created.get("latest_version") or (created.get("active_version") or {}).get("version") or 1
        if isinstance(version, dict):
            version = version.get("version", 1)
        ok(client.put(f"{POLICIES}/{policy_id}/assignments", json={"rules": [{
            "criteria": criteria or {"everyone": True}, "priority": priority, "effective_from": "2020-01-01"}]},
            headers=idem()))
    else:
        created = ok(client.post(f"{POLICIES}/{policy_id}/versions", json={"fields": fields, "effective_from": start},
                                 headers=idem()), 201)
        version = created["version"]
    if not created_here:
        # The starter catch-all rule starts on the signup day; the harness backdates the rule set so the policy
        # covers the whole period (a tenant provisioned mid-period, F-41).
        rule = {"criteria": criteria, "priority": priority} if criteria is not None else {
            "criteria": {"everyone": True}, "priority": 0}
        ok(client.put(f"{POLICIES}/{policy_id}/assignments", json={"rules": [{**rule, "effective_from": "2020-01-01"}]},
                      headers=idem()))
    ok(client.post(f"{POLICIES}/{policy_id}/versions/{version}/activate", json={"reason": "Verification policy"},
                   headers=idem()))
    return policy_id, version


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
def periods_of(client, group_id):
    return ok(client.get(f"{PAY}/pay-groups/{group_id}/periods", params={"limit": 500}))["items"]


def current_period(client, group_id):
    day = today_of(client).isoformat()
    return next(p for p in periods_of(client, group_id) if p["start"] <= day <= p["end"])


def create_run(client, group_id, period_id, expect=201):
    return ok(client.post(RUNS, json={"run_type": "REGULAR", "pay_group_id": group_id, "pay_period_id": period_id},
                          headers=idem()), expect)


def precheck(client, run_id):
    ok(client.post(f"{RUNS}/{run_id}/precheck", headers=idem()))
    return ok(client.get(f"{RUNS}/{run_id}/exceptions", params={"status": "OPEN"}))["items"]


def calculate(client, run_id, expect=202):
    return ok(client.post(f"{RUNS}/{run_id}/calculate", headers=idem()), expect)


def run_detail(client, run_id):
    return ok(client.get(f"{RUNS}/{run_id}"))


def employee_rows(client, run_id):
    """{employee_id: list item} of the run's employees (all statuses)."""
    items = ok(client.get(f"{RUNS}/{run_id}/employees", params={"limit": 200}))["items"]
    return {item["employee_id"]: item for item in items}


def employee_lines(client, run_id, employee_id):
    """{component: Decimal amount} of one employee's lines, and the detail payload."""
    row = employee_rows(client, run_id)[employee_id]
    detail = ok(client.get(f"{RUNS}/{run_id}/employees/{row['id']}"))
    amounts = {}
    for line in detail["lines"]:
        amounts[line["component"]] = amounts.get(line["component"], Decimal(0)) + Decimal(line["amount"]["amount"])
    return amounts, detail


def net_of(client, run_id, employee_id):
    row = employee_rows(client, run_id)[employee_id]
    return Decimal(row["net"]["amount"]) if row.get("net") else None


def open_findings(client, run_id, code=None):
    items = ok(client.get(f"{RUNS}/{run_id}/exceptions", params={"status": "OPEN"}))["items"]
    return [i for i in items if code is None or i.get("code") == code]


def recalc(client, run_id):
    """Recalculate the whole draft run (re-snapshots every included employee) and return the run detail."""
    calculate(client, run_id)
    return run_detail(client, run_id)


# ---------------------------------------------------------------------------
# World construction (real APIs)
# ---------------------------------------------------------------------------
def make_entity(client, code, country="GB", currency="GBP", tz="Europe/London"):
    return ok(client.post("/api/v1/legal-entities", json={
        "code": code, "legal_name": f"{code} Verification Ltd", "country_code": country, "base_currency": currency,
        "timezone": tz}))["id"]


def make_branch(client, entity_id, code):
    return ok(client.post("/api/v1/branches", json={"legal_entity_id": entity_id, "code": code,
                                                    "name": f"Branch {code}"}))["id"]


def make_pay_group(client, entity_id, code, currency="GBP"):
    created = ok(client.post(PAY_GROUPS, json={"code": code, "name": f"Group {code}", "legal_entity_id": entity_id,
                                               "currency": currency, "pay_frequency": "MONTHLY"}, headers=idem()), 201)
    ok(client.post(f"{PAY}/pay-groups/{created['id']}/calendar",
                   json={"frequency": "MONTHLY", "rules": {"monthly_start_day": 1}}, headers=idem()))
    return created["id"]


def place(client, employee_id, entity_id, branch_id, group_id, effective_from="2024-01-16"):
    ok(client.post(f"/api/v1/employees/{employee_id}/assignments", json={
        "legal_entity_id": entity_id, "branch_id": branch_id, "pay_group_id": group_id,
        "effective_from": effective_from, "reason": "TRANSFER"}))


def flat_structure(client):
    """CTC_BASED structure with BASIC = 100% of the monthly CTC (BASIC carries LOP_APPLICABLE and OT_BASE)."""
    ok(client.post(f"{PAY}/components/seed"))
    created = ok(client.post(f"{PAY}/salary-structures", json={
        "code": "FLAT", "name": "Flat basic", "type": "CTC_BASED",
        "lines": [{"component": "BASIC", "method": "FORMULA", "formula": "ctc.monthly * 100%"}],
        "test_range": {"min": "12000", "max": "5000000"}}), 201)
    published = ok(client.post(f"{PAY}/salary-structures/{created['id']}/publish", json={"version": 1}))
    return next(v["id"] for v in published["versions"] if v["status"] == "ACTIVE")


def period_days(period):
    start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
    return (end - start).days + 1


def monthly_basic(period):
    """Hand rule of the world: BASIC for a full month = DAY_RATE x calendar days of the period."""
    return DAY_RATE * period_days(period)


def build_tenant(app, name, layout, *, approver_name="Ava"):
    """``layout``: {entity_code: {"branches": [codes], "groups": {group_code: (branch_code, [employee names])}}}."""
    admin = signup_company(app, name)
    cid = admin.company_id
    version_id = flat_structure(admin)
    approver = login_as(app, admin, create_employee(admin, approver_name, "Approver"), ["payroll_approver"])
    boss = create_employee(admin, "Max", "Manager")
    manager = login_as(app, admin, boss, ["manager"])
    world = {"app": app, "admin": admin, "cid": cid, "approver": approver, "manager": manager,
             "manager_id": boss["id"], "structure_version": version_id, "entities": {}, "branches": {}, "groups": {},
             "emp": {}, "employees": {}, "group_of": {}, "logins": {}}
    for entity_code, spec in layout.items():
        entity_id = make_entity(admin, entity_code)
        world["entities"][entity_code] = entity_id
        for branch_code in spec["branches"]:
            world["branches"][branch_code] = make_branch(admin, entity_id, branch_code)
        for group_code, (branch_code, names) in spec["groups"].items():
            group_id = make_pay_group(admin, entity_id, group_code)
            world["groups"][group_code] = group_id
            for person in names:
                employee = create_employee(admin, person, "Verify", manager_id=boss["id"], hire_date="2024-01-15")
                place(admin, employee["id"], entity_id, world["branches"][branch_code], group_id)
                world["emp"][person] = employee["id"]
                world["employees"][person] = employee
                world["group_of"][person] = group_code
    any_group = next(iter(world["groups"].values()))
    period = current_period(admin, any_group)
    world["period"] = period
    world["periods"] = {code: current_period(admin, gid) for code, gid in world["groups"].items()}
    world["start"] = date.fromisoformat(period["start"])
    world["end"] = date.fromisoformat(period["end"])
    annual = str(monthly_basic(period) * 12)
    for person, employee_id in world["emp"].items():
        assign_and_approve(admin, approver, employee_id, version_id, annual, effective_from=world["start"],
                           currency="GBP")
    # Baseline: attendance does not drive pay (starter profile has it ON with unmarked days -> exceptions).
    put_setting(admin, "payroll.attendance.enabled", False, effective_from=world["start"], reason=BASELINE_REASON)
    return world


ALPHA_LAYOUT = {
    "VA1": {"branches": ["LON", "MAN"], "groups": {"PG-A": ("LON", ["Ann", "Ben"]), "PG-B": ("MAN", ["Cal"])}},
    "VA2": {"branches": ["EDI"], "groups": {"PG-C": ("EDI", ["Dee"])}},
}
BETA_LAYOUT = {"VB1": {"branches": ["BRS"], "groups": {"PG-Z": ("BRS", ["Zed"])}}}


@pytest.fixture
def settings_world(app):
    """Verity Alpha (the tenant under test) and Verity Beta (bystander with a calculated draft run)."""
    alpha = build_tenant(app, "Verity Alpha", ALPHA_LAYOUT)
    beta = build_tenant(app, "Verity Beta", BETA_LAYOUT, approver_name="Bea")
    beta_run = create_run(beta["admin"], beta["groups"]["PG-Z"], beta["periods"]["PG-Z"]["id"])
    precheck(beta["admin"], beta_run["id"])
    calculate(beta["admin"], beta_run["id"])
    beta["run"] = beta_run["id"]
    return {"alpha": alpha, "beta": beta}


def employee_login(world, person):
    """A signed-in self-service client (role ``employee``) for one of the world's employees (cached)."""
    if person not in world["logins"]:
        world["logins"][person] = login_as(world["app"], world["admin"], world["employees"][person], ["employee"])
    return world["logins"][person]


def second_admin(world, name="Sam"):
    """Another company administrator with payroll_manager (the checker of maker-checker flows), cached."""
    if "second_admin" not in world:
        world["second_admin"] = login_as(world["app"], world["admin"], create_employee(world["admin"], name, "Checker"),
                                         ["company_admin", "payroll_manager"])
    return world["second_admin"]


def approve_request(world, request_id, *, deciders=None):
    """Approve a payroll approvals-engine request with the first eligible decider (manager, approver, second
    admin, founder). Returns the decider's label; fails when nobody may approve."""
    tried = []
    for label in deciders or ("manager", "approver", "second_admin", "admin"):
        client = second_admin(world) if label == "second_admin" else world[label]
        r = client.post(f"{PAY}/approvals/{request_id}/approve", json={}, headers=idem())
        if r.status_code == 200:
            return label
        tried.append((label, r.status_code, r.text[:200]))
    raise AssertionError(f"nobody could approve request {request_id}: {tried}")


def month_bounds(day):
    return day.replace(day=1), day.replace(day=_calendar.monthrange(day.year, day.month)[1])


def days_between(start, end):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
