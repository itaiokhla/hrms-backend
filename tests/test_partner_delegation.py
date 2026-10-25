"""Partner operator delegated access (Goal 2 section 15, 01-white-label partner model).

A partner has no implicit access to tenant data. Only a current, tenant-granted delegation (step-up, audited,
at most 30 days, read-only, never payroll money actions) lets a partner operator read the delegated
permission codes of one tenant, and ending it works on the very next request.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.permission_registry import PERMISSIONS
from app.core.security import create_jwt
from app.services import partner_delegation_service as service
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.white_label_helpers import PLATFORM_HOST, HostClient, platform_admin, tenant_host

PASSWORD = "PartnerPass!2026"
BASE = "/api/v1/partner-access"
PARTNER = "/api/v1/partner/delegations"
READ_CODES = ["employee:read", "org:read", "holiday:read"]
PAY_CODES = ["payroll:run:read", "payroll:payslip:read"]


def sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


def iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def step_up(client):
    sql("UPDATE user_sessions SET step_up_at = now(), step_up_method = 'EMAIL_OTP' WHERE user_id = :u",
        u=client.me()["user"]["id"])


def partner_login(app, email):
    client = HostClient(app, PLATFORM_HOST)
    r = client.post("/api/v1/partner/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


def body(codes=None, days=7, **extra):
    return {"permission_codes": codes or READ_CODES, "expires_at": iso(days=days),
            "reason": "Month-end bureau review of attendance", **extra}


def grant(admin, codes=None, days=7, expect=200, **extra):
    step_up(admin)
    r = admin.post(BASE, json=body(codes, days, **extra))
    assert r.status_code == expect, r.text
    return r.json()


def open_tenant(partner, delegation_id, expect=200):
    r = partner.post(f"{PARTNER}/{delegation_id}/token")
    assert r.status_code == expect, r.text
    return r.json()


class Delegate:
    """A partner operator's calls into one tenant, with the delegated bearer token."""

    def __init__(self, app, slug, token):
        self.http = HostClient(app, tenant_host(slug))
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, url, **kw):
        return self.http.get(url, headers=self.headers, **kw)

    def send(self, method, url, **kw):
        return self.http.request(method, url, headers=self.headers, **kw)


@pytest.fixture
def world(app):
    from app.cli import create_platform_admin

    ops = platform_admin(app)
    alpha = signup_company(app, "Acme Corp", "admin@acme.io")
    beta = signup_company(app, "Beta Industries", "admin@beta.io")
    gamma = signup_company(app, "Gamma Labs", "admin@gamma.io")
    delta = signup_company(app, "Delta Solo", "admin@delta.io")  # no partner
    north = ops.post("/api/v1/platform/partners", json={"name": "Northwind Partners"}).json()
    south = ops.post("/api/v1/platform/partners", json={"name": "Southern Cross"}).json()
    for company, partner in ((alpha, north), (beta, north), (gamma, south)):
        assert ops.patch(f"/api/v1/platform/companies/{company.company_id}", json={"partner_id": partner["id"]}).status_code == 200
    create_platform_admin("admin@northwind.example", "Northwind Admin", PASSWORD, partner_slug=north["slug"])
    create_platform_admin("admin@southern.example", "Southern Admin", PASSWORD, partner_slug=south["slug"])
    return {"app": app, "ops": ops, "north": north, "south": south, "alpha": alpha, "beta": beta, "gamma": gamma,
            "delta": delta, "pn": partner_login(app, "admin@northwind.example"),
            "ps": partner_login(app, "admin@southern.example")}


@pytest.fixture
def alpha_staff(world):
    """Alpha with two legal entities, pay groups and employees in each, and a payroll delegator (Dana)."""
    app, alpha = world["app"], world["alpha"]
    le01 = next(e for e in alpha.get("/api/v1/legal-entities").json() if e["code"] == "LE01")
    r = alpha.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Acme Gulf FZ-LLC",
                                                    "country_code": "AE", "base_currency": "AED", "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    ae = r.json()
    r = alpha.post("/api/v1/branches", json={"legal_entity_id": ae["id"], "code": "DXB", "name": "Dubai"})
    assert r.status_code == 200, r.text
    groups = {}
    for code, entity in (("PG-IN", le01), ("PG-AE", ae)):
        groups[code] = sql(
            "INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency, calendar_rules, "
            "status, created_at, updated_at) VALUES (:c, :le, :code, :code, 'INR', 'MONTHLY', '{}'::jsonb, 'ACTIVE', now(), now()) "
            "RETURNING id", c=alpha.company_id, le=entity["id"], code=code)[0][0]
    indian = create_employee(alpha, "Indira", hire_date="2024-01-15")
    gulf = create_employee(alpha, "Gulnar", hire_date="2024-01-15")
    r = alpha.post(f"/api/v1/employees/{gulf['id']}/assignments",
                   json={"branch_id": r.json()["id"], "effective_from": "2024-01-16", "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    # Dana holds the payroll reads and partner-access management company-wide through one custom role.
    codes = [*PAY_CODES, "payroll:bank_details:read_masked", service.MANAGE_CODE, "employee:read"]
    role = alpha.post("/api/v1/roles", json={"name": "Payroll delegator", "grants": [
        {"permission": c, "scope": "COMPANY"} for c in codes]})
    assert role.status_code == 200, role.text
    dana = invite_and_activate(app, alpha, create_employee(alpha, "Dana"), [])
    assert alpha.put(f"/api/v1/users/{dana.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    role = alpha.post("/api/v1/roles", json={"name": "Partner access only", "grants": [
        {"permission": c, "scope": "COMPANY"} for c in (service.MANAGE_CODE, "employee:read")]})
    assert role.status_code == 200, role.text
    hana = invite_and_activate(app, alpha, create_employee(alpha, "Hana"), [])
    assert alpha.put(f"/api/v1/users/{hana.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    return {"le01": le01, "ae": ae, "groups": groups, "indian": indian, "gulf": gulf, "dana": dana, "hana": hana}


def audit_rows(company_id, **where):
    clause = " AND ".join(f"{k} = :{k}" for k in where)
    return sql(f"SELECT action, actor_type, actor_id, actor_email, details FROM audit_logs WHERE company_id = :c "
               f"AND module = 'Partner access' {'AND ' + clause if clause else ''} ORDER BY id", c=company_id, **where)


# ---------------------------------------------------------------------------- what can be delegated
def test_only_read_only_non_money_codes_are_delegable():
    delegable = {p.code for p in PERMISSIONS if service.is_delegable(p.code)}
    assert {"employee:read", "org:read", "payroll:run:read", "payroll:bank_details:read_masked"} <= delegable
    forbidden = {"payroll:bank_details:read_full", "payroll:run:finalize", "payroll:run:approve", "payroll:payment:prepare",
                 "payroll:payment:approve", "payroll:payment:submit", "payroll:payment:reconcile",
                 "payroll:funding_accounts:manage", "payroll:bank_details:edit", "payroll:bank_details:approve",
                 "settings:partner_access:manage", "settings:manage", "settings:approve_changes", "user:read",
                 "user:assign_role", "role:update", "employee:update", "employee:create", "payroll:compensation:edit",
                 "payroll:reports:export", "domain:manage", "profile:update", "payroll:read", "compensation:read"}
    assert not forbidden & delegable
    assert all(code.rsplit(":", 1)[-1] in ("read", "read_masked") for code in delegable)
    assert not any(code.startswith("payroll:payment:") for code in delegable)
    assert {c["code"] for c in service.catalogue()} == delegable
    assert service.is_delegable("nonsense:read") is False


def test_runtime_grants_drop_non_delegable_codes_even_if_a_row_holds_them():
    grants, targets = service.build_grants(["employee:read", "payroll:payment:submit", "payroll:run:approve",
                                            "payroll:run:finalize", "payroll:bank_details:read_full", "user:read",
                                            "settings:partner_access:manage"], [], [])
    assert grants == {"employee:read": frozenset({"COMPANY"})} and targets == {}
    grants, targets = service.build_grants(["employee:read", "settings:read"], [5], [])
    assert grants == {"employee:read": frozenset({"LEGAL_ENTITY"})}  # settings:read cannot be narrowed: dropped
    assert targets == {("employee:read", "LEGAL_ENTITY"): frozenset({5})}


# ---------------------------------------------------------------------------- tenant administrator side
def test_granting_needs_the_permission_a_step_up_and_is_audited(world):
    alpha, app = world["alpha"], world["app"]
    # An employee without the permission is refused.
    staff = invite_and_activate(app, alpha, create_employee(alpha, "Eve"), ["employee"])
    assert staff.get(BASE).status_code == 403
    assert staff.post(BASE, json=body()).status_code == 403
    # The administrator needs a fresh step-up.
    r = alpha.post(BASE, json=body())
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED"
    row = grant(alpha)
    assert row["status"] == "ACTIVE" and row["read_only"] is True and row["partner_name"] == "Northwind Partners"
    assert row["permission_codes"] == sorted(READ_CODES) and row["granted_by"]["name"] == "Acme Corp Admin"
    overview = alpha.get(BASE).json()
    assert overview["partner"]["name"] == "Northwind Partners" and overview["max_days"] == 30
    assert [i["id"] for i in overview["items"]] == [row["id"]]
    assert [e["code"] for e in overview["targets"]["legal_entities"]] == ["LE01"] and [g["code"] for g in overview["targets"]["pay_groups"]] == ["MONTHLY"]
    assert "payroll:payment:submit" not in {c["code"] for c in overview["catalogue"]}
    audit = audit_rows(alpha.company_id, action="DELEGATION_GRANTED")
    assert len(audit) == 1 and audit[0].actor_type == "user"
    assert audit[0].details["delegation_id"] == row["id"] and audit[0].details["permission_codes"] == sorted(READ_CODES)


def test_non_delegable_unknown_and_unheld_codes_are_refused(world, alpha_staff):
    alpha = world["alpha"]
    step_up(alpha)
    for code in ("payroll:payment:submit", "payroll:run:approve", "payroll:run:finalize", "payroll:bank_details:read_full",
                 "employee:update", "user:read", "settings:partner_access:manage", "no:such_code", "payroll:read"):
        r = alpha.post(BASE, json=body([code]))
        assert r.status_code == 422 and r.json()["code"] == "NOT_DELEGABLE", (code, r.text)
    # One bad code spoils the grant: nothing was stored.
    assert alpha.post(BASE, json=body(["employee:read", "payroll:payment:submit"])).status_code == 422
    assert sql("SELECT count(*) FROM partner_delegations")[0][0] == 0
    # Delegating is no way to gain access: a manager of partner access who does not hold a permission
    # company-wide cannot delegate it (Hana holds employee:read only).
    hana = alpha_staff["hana"]
    step_up(hana)
    for codes in (["payroll:run:read"], ["employee:read", "attendance:read"]):
        r = hana.post(BASE, json=body(codes))
        assert r.status_code == 403 and r.json()["code"] == "DELEGATION_EXCEEDS_GRANTOR", r.text
    assert hana.post(BASE, json=body(["employee:read"])).status_code == 200
    # Dana holds it company-wide and can.
    dana = alpha_staff["dana"]
    step_up(dana)
    assert dana.post(BASE, json=body(PAY_CODES)).status_code == 200


def test_window_rules_thirty_days_future_start_reason_and_strict_body(world):
    alpha = world["alpha"]
    step_up(alpha)
    bad = [
        body(days=31), body(days=0), {**body(), "expires_at": iso(days=-1)},
        {**body(), "starts_at": iso(hours=-2)}, {**body(), "starts_at": iso(days=31), "expires_at": iso(days=32)},
        {**body(), "starts_at": iso(days=3), "expires_at": iso(days=2)},
        {**body(), "starts_at": iso(days=1), "expires_at": iso(days=32)},
        {**body(), "reason": "short"}, {**body(), "reason": "   " + "x" * 5 + "   "},
    ]
    for payload in bad:
        r = alpha.post(BASE, json=payload)
        assert r.status_code in (400, 422), (payload, r.text)
    for extra in ({"company_id": 99}, {"status": "ACTIVE"}, {"partner_id": 1}, {"granted_by_user_id": 1}):
        assert alpha.post(BASE, json={**body(), **extra}).status_code == 422
    naive = {**body(), "expires_at": (datetime.utcnow() + timedelta(days=2)).isoformat()}
    assert alpha.post(BASE, json=naive).status_code == 422
    assert alpha.post(BASE, json={**body(), "permission_codes": []}).status_code == 422
    # Exactly 30 days and a future start are fine.
    ok = alpha.post(BASE, json={**body(), "starts_at": iso(days=1), "expires_at": iso(days=31, seconds=-5)})
    assert ok.status_code == 200 and ok.json()["status"] == "PENDING"
    assert alpha.post(BASE, json={**body(), "expires_at": iso(days=30, seconds=-5)}).status_code == 200


def test_the_database_enforces_the_window_and_revocation_facts(world):
    from sqlalchemy.exc import IntegrityError

    alpha = world["alpha"]
    row = grant(alpha)
    uid = alpha.me()["user"]["id"]
    for update in ("UPDATE partner_delegations SET expires_at = starts_at + interval '31 days'",
                   "UPDATE partner_delegations SET expires_at = starts_at",
                   "UPDATE partner_delegations SET status = 'REVOKED'",
                   "UPDATE partner_delegations SET reason = 'too short'",
                   "UPDATE partner_delegations SET permission_codes = '[]'::jsonb",
                   "UPDATE partner_delegations SET status = 'PAUSED'"):
        with pytest.raises(IntegrityError):
            sql(update)
    assert sql("UPDATE partner_delegations SET status = 'REVOKED', revoked_at = now(), revoked_by_user_id = :u "
               "WHERE id = :i RETURNING 1", u=uid, i=row["id"]) == [(1,)]


def test_no_partner_or_inactive_partner_means_nothing_to_delegate_to(world):
    delta, alpha, ops = world["delta"], world["alpha"], world["ops"]
    step_up(delta)
    assert delta.get(BASE).json()["partner"] is None
    r = delta.post(BASE, json=body())
    assert r.status_code == 409 and r.json()["code"] == "NO_PARTNER"
    assert ops.patch(f"/api/v1/platform/partners/{world['north']['id']}", json={"status": "suspended"}).status_code == 200
    step_up(alpha)
    r = alpha.post(BASE, json=body())
    assert r.status_code == 409 and r.json()["code"] == "PARTNER_INACTIVE"


def test_revoking_records_who_and_why_and_cannot_repeat(world):
    alpha, beta = world["alpha"], world["beta"]
    row = grant(alpha)
    assert alpha.post(f"{BASE}/{row['id']}/revoke", json={"reason": "x"}).status_code == 400
    assert alpha.post(f"{BASE}/{row['id']}/revoke", json={"reason": "Work finished", "status": "ACTIVE"}).status_code == 422
    r = alpha.post(f"{BASE}/{row['id']}/revoke", json={"reason": "Work finished early"})
    assert r.status_code == 200 and r.json()["status"] == "REVOKED"
    assert r.json()["revoked_by"]["name"] == "Acme Corp Admin" and r.json()["revoke_reason"] == "Work finished early"
    again = alpha.post(f"{BASE}/{row['id']}/revoke", json={"reason": "Work finished early"})
    assert again.status_code == 409 and again.json()["code"] == "DELEGATION_ENDED"
    # Another tenant's administrator cannot see or end it.
    assert beta.post(f"{BASE}/{row['id']}/revoke", json={"reason": "Not yours to end"}).status_code == 404
    assert beta.get(f"{BASE}/{row['id']}/activity").status_code == 404
    assert [a.action for a in audit_rows(alpha.company_id)] == ["DELEGATION_GRANTED", "DELEGATION_REVOKED"]


def test_three_tenants_only_see_their_own_delegations(world):
    alpha, beta = world["alpha"], world["beta"]
    a, b = grant(alpha), grant(beta)
    assert [i["id"] for i in alpha.get(BASE).json()["items"]] == [a["id"]]
    assert [i["id"] for i in beta.get(BASE).json()["items"]] == [b["id"]]
    gamma = world["gamma"]
    assert gamma.get(BASE).json()["items"] == []
    assert sql("SELECT count(*) FROM partner_delegations")[0][0] == 2


# ---------------------------------------------------------------------------- partner operator side
def test_partner_sees_only_current_delegations_of_its_own_tenants(world):
    alpha, beta, gamma = world["alpha"], world["beta"], world["gamma"]
    a, b, g = grant(alpha), grant(beta), grant(gamma)
    assert g["partner_name"] == "Southern Cross"
    seen = world["pn"].get(PARTNER).json()["items"]
    assert sorted(i["company"]["name"] for i in seen) == ["Acme Corp", "Beta Industries"]
    for item in seen:  # names and counts, never HR data
        assert set(item) == {"id", "company", "permissions", "narrowed", "read_only", "legal_entity_count",
                             "pay_group_count", "starts_at", "expires_at", "reason"}
    assert [i["company"]["name"] for i in world["ps"].get(PARTNER).json()["items"]] == ["Gamma Labs"]
    # Ended or not-yet-started delegations are not listed.
    alpha.post(f"{BASE}/{a['id']}/revoke", json={"reason": "Done with the review"})
    step_up(beta)
    beta.post(BASE, json={**body(), "starts_at": iso(days=2), "expires_at": iso(days=3)})
    assert [i["id"] for i in world["pn"].get(PARTNER).json()["items"]] == [b["id"]]
    # Without a partner session there is nothing.
    assert HostClient(world["app"], PLATFORM_HOST).get(PARTNER).status_code == 401
    assert alpha.get(PARTNER).status_code in (401, 404)


def test_delegated_token_reads_only_the_delegated_codes_and_everything_is_audited(world):
    app, alpha = world["app"], world["alpha"]
    row = grant(alpha)
    # Another operator of the same partner... and the other partner cannot use this id.
    assert world["ps"].post(f"{PARTNER}/{row['id']}/token").status_code == 404
    session = open_tenant(world["pn"], row["id"])
    assert session["read_only"] is True and session["company"]["name"] == "Acme Corp"
    assert sorted(session["permissions"]) == sorted(READ_CODES)
    delegate = Delegate(app, "acme-corp", session["access_token"])
    # Delegated reads work.
    r = delegate.get("/api/v1/employees")
    assert r.status_code == 200 and r.json()["total"] >= 1
    assert delegate.get("/api/v1/holidays").status_code == 200
    # Everything outside the delegated codes is a 403.
    for path in ("/api/v1/attendance", "/api/v1/leaves", "/api/v1/audit-logs", "/api/v1/payroll/runs",
                 "/api/v1/reports/payroll", "/api/v1/attendance/statistics"):
        assert delegate.get(path).status_code == 403, path
    # Read only: any change is refused, whatever the permission.
    for method, path in (("POST", "/api/v1/employees"), ("PUT", "/api/v1/employees/1"),
                         ("POST", "/api/v1/employees/1/deactivate"), ("POST", f"{BASE}"), ("POST", f"{BASE}/{row['id']}/revoke")):
        r = delegate.send(method, path, json={})
        assert r.status_code == 403 and r.json()["code"] == "DELEGATION_READ_ONLY", (method, path, r.text)
    # Identity, step-up and delegation management are closed to it.
    for path in ("/api/v1/auth/me", "/api/v1/auth/sessions", "/api/v1/users", "/api/v1/roles", BASE,
                 "/api/v1/payroll/step-up", "/api/v1/me/payroll/preferences"):
        r = delegate.get(path)
        assert r.status_code == 403 and r.json()["code"] == "DELEGATION_ROUTE_BLOCKED", (path, r.text)
    # Every call is in the tenant's audit trail, attributed to the operator and the delegation.
    rows = audit_rows(alpha.company_id, actor_type="partner")
    actions = [r.action for r in rows]
    assert actions[0] == "DELEGATION_TOKEN_ISSUED"
    assert actions.count("DELEGATED_REQUEST") >= 8 and actions.count("DELEGATED_REQUEST_DENIED") >= 12
    for r in rows:
        assert r.actor_email == "admin@northwind.example" and r.details["delegation_id"] == row["id"]
    # The tenant administrator reads what the partner did.
    activity = alpha.get(f"{BASE}/{row['id']}/activity").json()["items"]
    assert {i["operator_email"] for i in activity} == {"admin@northwind.example"}
    assert any(i["path"] == "/api/v1/employees" and i["action"] == "DELEGATED_REQUEST" for i in activity)
    # Other tenants' audit trails hold nothing of it.
    assert audit_rows(world["beta"].company_id) == []


def test_token_only_travels_as_a_bearer_header_never_as_a_cookie(world):
    app, alpha = world["app"], world["alpha"]
    row = grant(alpha)
    token = open_tenant(world["pn"], row["id"])["access_token"]
    cookie_client = HostClient(app, tenant_host("acme-corp"))
    cookie_client.http.cookies.set("hrms_access", token)
    assert cookie_client.get("/api/v1/employees").status_code == 401
    assert HostClient(app, tenant_host("acme-corp")).get("/api/v1/employees", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_expired_delegation_stops_the_next_call_and_cannot_open_a_tenant(world):
    app, alpha = world["app"], world["alpha"]
    row = grant(alpha)
    delegate = Delegate(app, "acme-corp", open_tenant(world["pn"], row["id"])["access_token"])
    assert delegate.get("/api/v1/employees").status_code == 200
    sql("UPDATE partner_delegations SET starts_at = now() - interval '2 days', expires_at = now() - interval '1 second' "
        "WHERE id = :i", i=row["id"])
    r = delegate.get("/api/v1/employees")
    assert r.status_code == 403 and r.json()["code"] == "DELEGATION_NOT_CURRENT"
    assert world["pn"].post(f"{PARTNER}/{row['id']}/token").status_code == 403
    assert world["pn"].get(PARTNER).json()["items"] == []
    assert alpha.get(BASE).json()["items"][0]["status"] == "EXPIRED"
    refused = audit_rows(alpha.company_id, action="DELEGATED_ACCESS_REFUSED")
    assert refused and refused[0].details["reason"] == "expired"
    # Revoking something already expired is a conflict, not a silent no-op.
    assert alpha.post(f"{BASE}/{row['id']}/revoke", json={"reason": "Clean up the list"}).status_code == 409


def test_revocation_takes_effect_on_the_very_next_request(world):
    """EC-ORG-18: a grant revoked mid-session ends the next request with 403."""
    app, alpha = world["app"], world["alpha"]
    row = grant(alpha)
    delegate = Delegate(app, "acme-corp", open_tenant(world["pn"], row["id"])["access_token"])
    assert delegate.get("/api/v1/employees").status_code == 200
    assert alpha.post(f"{BASE}/{row['id']}/revoke", json={"reason": "Contract ended today"}).status_code == 200
    r = delegate.get("/api/v1/employees")
    assert r.status_code == 403 and r.json()["code"] == "DELEGATION_NOT_CURRENT"
    assert world["pn"].post(f"{PARTNER}/{row['id']}/token").status_code == 403
    assert audit_rows(alpha.company_id, action="DELEGATED_ACCESS_REFUSED")[0].details["reason"] == "revoked"


def test_future_delegation_is_not_usable_yet(world):
    alpha = world["alpha"]
    step_up(alpha)
    r = alpha.post(BASE, json={**body(), "starts_at": iso(days=1), "expires_at": iso(days=3)})
    assert r.status_code == 200
    assert world["pn"].post(f"{PARTNER}/{r.json()['id']}/token").status_code == 403


def test_wrong_tenant_forged_ids_and_privilege_escalation_fail(world):
    app, alpha, beta, gamma = world["app"], world["alpha"], world["beta"], world["gamma"]
    a, g = grant(alpha), grant(gamma)
    token = open_tenant(world["pn"], a["id"])["access_token"]
    # The token of one tenant on another tenant's host.
    assert Delegate(app, "beta-industries", token).get("/api/v1/employees").status_code == 401
    from app.core.security import decode_jwt
    real = decode_jwt(token, "access")

    def forged(**changes):
        data = {k: real[k] for k in ("sub", "pt", "did", "cid", "pid", "psid")}
        data.update({k: str(v) for k, v in changes.items()})
        return create_jwt(data, "access", timedelta(minutes=5))

    # Same operator and session, but ids that are not that tenant's / that partner's delegation.
    for changes, host in (({"cid": beta.company_id}, "beta-industries"),          # Alpha's delegation id, Beta's tenant
                          ({"did": g["id"]}, "acme-corp"),                           # Gamma's delegation under Alpha
                          ({"did": g["id"], "cid": gamma.company_id}, "gamma-labs"),  # South's delegation, North's partner id
                          ({"did": 99999}, "acme-corp"), ({"did": 0}, "acme-corp"), ({"pid": world["south"]["id"]}, "acme-corp")):
        r = Delegate(app, host, forged(**changes)).get("/api/v1/employees")
        assert r.status_code in (401, 403), (changes, r.text)
    assert Delegate(app, "acme-corp", forged(did="abc")).get("/api/v1/employees").status_code == 401
    # An invented operator session or an operator that is not of that partner.
    assert Delegate(app, "acme-corp", forged(psid="nope")).get("/api/v1/employees").status_code == 401
    south_admin = sql("SELECT id FROM platform_admins WHERE email = 'admin@southern.example'")[0][0]
    assert Delegate(app, "acme-corp", forged(sub=south_admin)).get("/api/v1/employees").status_code == 401
    # A token signed with the wrong key or the wrong type is just invalid.
    assert Delegate(app, "acme-corp", token[:-3] + "abc").get("/api/v1/employees").status_code == 401
    # Nothing was recorded in a tenant for a forged id.
    assert audit_rows(beta.company_id) == []
    # A tenant user's own token and the partner's cookie are no delegated principal, and vice versa.
    assert alpha.get(PARTNER).status_code in (401, 404)
    assert world["pn"].get(BASE).status_code in (401, 403, 404)
    assert world["pn"].get("/api/v1/employees").status_code == 401


def test_changing_the_tenants_partner_ends_the_delegation_at_once(world):
    app, alpha, ops = world["app"], world["alpha"], world["ops"]
    row = grant(alpha)
    delegate = Delegate(app, "acme-corp", open_tenant(world["pn"], row["id"])["access_token"])
    assert delegate.get("/api/v1/employees").status_code == 200
    assert ops.patch(f"/api/v1/platform/companies/{alpha.company_id}", json={"partner_id": world["south"]["id"]}).status_code == 200
    r = delegate.get("/api/v1/employees")
    assert r.status_code == 403 and r.json()["code"] == "DELEGATION_NOT_CURRENT"
    # The new partner never held a delegation, and the old one cannot open the tenant any more.
    assert world["ps"].get(PARTNER).json()["items"] == []
    assert world["pn"].post(f"{PARTNER}/{row['id']}/token").status_code == 403
    assert world["ps"].post(f"{PARTNER}/{row['id']}/token").status_code == 404


def test_operator_logout_suspension_and_tenant_suspension_end_the_session(world):
    app, alpha, ops = world["app"], world["alpha"], world["ops"]
    row = grant(alpha)
    # Signing out of the partner console revokes every token it opened.
    other = partner_login(app, "admin@northwind.example")
    delegate = Delegate(app, "acme-corp", open_tenant(other, row["id"])["access_token"])
    assert delegate.get("/api/v1/employees").status_code == 200
    assert other.post("/api/v1/partner/auth/logout").status_code == 200
    assert delegate.get("/api/v1/employees").status_code == 401
    # A deactivated operator account.
    again = partner_login(app, "admin@northwind.example")
    delegate = Delegate(app, "acme-corp", open_tenant(again, row["id"])["access_token"])
    sql("UPDATE platform_admins SET is_active = false WHERE email = 'admin@northwind.example'")
    assert delegate.get("/api/v1/employees").status_code == 401
    sql("UPDATE platform_admins SET is_active = true WHERE email = 'admin@northwind.example'")
    # A suspended tenant.
    third = partner_login(app, "admin@northwind.example")
    delegate = Delegate(app, "acme-corp", open_tenant(third, row["id"])["access_token"])
    assert ops.patch(f"/api/v1/platform/companies/{alpha.company_id}", json={"status": "suspended"}).status_code == 200
    assert delegate.get("/api/v1/employees").status_code in (401, 403)
    assert third.post(f"{PARTNER}/{row['id']}/token").status_code == 403
    # A suspended partner.
    assert ops.patch(f"/api/v1/platform/companies/{alpha.company_id}", json={"status": "active"}).status_code == 200
    assert ops.patch(f"/api/v1/platform/partners/{world['north']['id']}", json={"status": "suspended"}).status_code == 200
    assert delegate.get("/api/v1/employees").status_code == 401


def test_token_lifetime_never_outlives_the_delegation(world):
    alpha = world["alpha"]
    step_up(alpha)
    r = alpha.post(BASE, json={**body(), "expires_at": iso(seconds=90)})
    assert r.status_code == 200
    session = world["pn"].post(f"{PARTNER}/{r.json()['id']}/token").json()
    expires = datetime.fromisoformat(session["expires_at"])
    assert expires <= datetime.fromisoformat(session["delegation_expires_at"]) + timedelta(seconds=1)
    assert expires - datetime.now(timezone.utc) < timedelta(seconds=95)
    row = grant(alpha, days=7)
    long_lived = world["pn"].post(f"{PARTNER}/{row['id']}/token").json()
    assert datetime.fromisoformat(long_lived["expires_at"]) - datetime.now(timezone.utc) <= timedelta(minutes=10, seconds=2)


# ---------------------------------------------------------------------------- scope narrowing
def test_narrowing_to_a_legal_entity_limits_what_the_partner_reads(world, alpha_staff):
    app, alpha = world["app"], world["alpha"]
    everything = grant(alpha, ["employee:read"])
    wide = Delegate(app, "acme-corp", open_tenant(world["pn"], everything["id"])["access_token"])
    names = {e["first_name"] for e in wide.get("/api/v1/employees").json()["items"]}
    assert {"Indira", "Gulnar"} <= names
    narrow = grant(alpha, ["employee:read"], legal_entity_ids=[alpha_staff["le01"]["id"]])
    assert narrow["narrowed"] and [e["id"] for e in narrow["legal_entities"]] == [alpha_staff["le01"]["id"]]
    session = open_tenant(world["pn"], narrow["id"])
    assert session["targets"] == {"employee:read@LEGAL_ENTITY": [alpha_staff["le01"]["id"]]}
    scoped = Delegate(app, "acme-corp", session["access_token"])
    names = {e["first_name"] for e in scoped.get("/api/v1/employees").json()["items"]}
    assert "Indira" in names and "Gulnar" not in names
    assert scoped.get(f"/api/v1/employees/{alpha_staff['gulf']['id']}").status_code in (403, 404)
    assert scoped.get(f"/api/v1/employees/{alpha_staff['indian']['id']}").status_code == 200
    # The other legal entity gives the mirror image.
    other = Delegate(app, "acme-corp", open_tenant(world["pn"], grant(alpha, ["employee:read"], legal_entity_ids=[
        alpha_staff["ae"]["id"]])["id"])["access_token"])
    names = {e["first_name"] for e in other.get("/api/v1/employees").json()["items"]}
    assert "Gulnar" in names and "Indira" not in names


def test_narrowing_to_pay_groups_and_what_cannot_be_narrowed(world, alpha_staff):
    app, alpha, dana = world["app"], world["alpha"], alpha_staff["dana"]
    step_up(dana)
    r = dana.post(BASE, json=body(["payroll:run:read"], pay_group_ids=[alpha_staff["groups"]["PG-IN"]]))
    assert r.status_code == 200, r.text
    assert [g["id"] for g in r.json()["pay_groups"]] == [alpha_staff["groups"]["PG-IN"]]
    session = open_tenant(world["pn"], r.json()["id"])
    assert session["targets"] == {"payroll:run:read@PAY_GROUP": [alpha_staff["groups"]["PG-IN"]]}
    delegate = Delegate(app, "acme-corp", session["access_token"])
    assert delegate.get("/api/v1/payroll/runs").status_code == 200
    assert delegate.get("/api/v1/payroll/runs").json()["items"] == []
    step_up(alpha)
    # employee:read has no pay-group scope, settings:read has no targeted scope at all.
    for payload in (body(["employee:read"], pay_group_ids=[alpha_staff["groups"]["PG-IN"]]),
                    body(["employee:read", "holiday:read"], legal_entity_ids=[alpha_staff["le01"]["id"]])):
        r = alpha.post(BASE, json=payload)
        assert r.status_code == 422 and r.json()["code"] == "NOT_NARROWABLE", r.text
    # Unknown ids, and ids of another tenant's entity, are refused: never stored.
    beta_entity = world["beta"].get("/api/v1/legal-entities").json()[0]["id"]
    for payload in (body(["employee:read"], legal_entity_ids=[987654]), body(["employee:read"], legal_entity_ids=[beta_entity]),
                    body(["payroll:run:read"], pay_group_ids=[987654])):
        assert alpha.post(BASE, json=payload).status_code in (403, 422), payload
    assert sql("SELECT count(*) FROM partner_delegations WHERE company_id = :c", c=alpha.company_id)[0][0] == 1


def test_a_delegation_row_cannot_target_another_tenants_entity(world, alpha_staff):
    from sqlalchemy.exc import IntegrityError

    alpha, beta = world["alpha"], world["beta"]
    row = grant(alpha, ["employee:read"])
    beta_entity = beta.get("/api/v1/legal-entities").json()[0]["id"]
    with pytest.raises(IntegrityError):
        sql("INSERT INTO partner_delegation_targets (company_id, delegation_id, kind, legal_entity_id, created_at, updated_at) "
            "VALUES (:c, :d, 'LEGAL_ENTITY', :le, now(), now())", c=alpha.company_id, d=row["id"], le=beta_entity)
    with pytest.raises(IntegrityError):
        sql("INSERT INTO partner_delegation_targets (company_id, delegation_id, kind, legal_entity_id, pay_group_id, "
            "created_at, updated_at) VALUES (:c, :d, 'LEGAL_ENTITY', :le, 1, now(), now())",
            c=alpha.company_id, d=row["id"], le=alpha_staff["le01"]["id"])


def test_rows_with_non_delegable_codes_never_grant_them(world):
    """Even a row written around the API (old data, a later registry change) cannot hold money actions."""
    app, alpha = world["app"], world["alpha"]
    row = grant(alpha)
    sql("UPDATE partner_delegations SET permission_codes = CAST(:c AS jsonb) WHERE id = :i", i=row["id"],
        c=json.dumps(["payroll:payment:submit", "payroll:run:approve", "payroll:bank_details:read_full",
                      "employee:update", "user:read", "employee:read"]))
    session = open_tenant(world["pn"], row["id"])
    assert session["permissions"] == ["employee:read"]
    delegate = Delegate(app, "acme-corp", session["access_token"])
    assert delegate.get("/api/v1/employees").status_code == 200
    assert delegate.get("/api/v1/users").status_code == 403
    assert delegate.get("/api/v1/payroll/payment-batches").status_code in (403, 404)


def test_delegated_reads_of_payroll_never_reveal_full_bank_numbers(world, alpha_staff):
    """EC-ORG-16: a partner operator can neither prepare nor approve a run (nor pay, nor unmask a bank number):
    the delegation is read-only and the money permissions are not delegable at all."""
    app, dana = world["app"], alpha_staff["dana"]
    step_up(dana)
    r = dana.post(BASE, json=body(["payroll:run:read", "payroll:bank_details:read_masked"]))
    assert r.status_code == 200, r.text
    delegate = Delegate(app, "acme-corp", open_tenant(world["pn"], r.json()["id"])["access_token"])
    # The reveal endpoint needs read_full and a POST: both are out of reach.
    assert delegate.send("POST", "/api/v1/payroll/bank-accounts/1/reveal", json={"reason": "reviewing the account"}).status_code == 403
    assert delegate.send("POST", "/api/v1/payroll/runs/1/finalize", json={}).status_code == 403
    assert delegate.send("POST", "/api/v1/payroll/payment-batches", json={}).status_code == 403
    assert delegate.send("POST", "/api/v1/payroll/runs/1/approve", json={}).status_code == 403
