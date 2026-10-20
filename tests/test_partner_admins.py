"""Partner administrators: own clients as aggregate counts, own brand, nothing else."""

import pytest

from tests.helpers import signup_company
from tests.white_label_helpers import PLATFORM_HOST, HostClient, platform_admin, tenant_host

PASSWORD = "PartnerPass!2026"
HR_FIELDS = {"email", "salary", "base_salary", "bank_account", "employees", "first_name", "phone"}


@pytest.fixture
def world(app):
    from app.cli import create_platform_admin

    ops = platform_admin(app)
    alpha = signup_company(app, "Acme Corp", "admin@acme.io")
    beta = signup_company(app, "Beta Industries", "admin@beta.io")
    gamma = signup_company(app, "Gamma Labs", "admin@gamma.io")
    north = ops.post("/api/v1/platform/partners", json={"name": "Northwind Partners"}).json()
    south = ops.post("/api/v1/platform/partners", json={"name": "Southern Cross"}).json()
    for company, partner in ((alpha, north), (beta, north), (gamma, south)):
        assert ops.patch(f"/api/v1/platform/companies/{company.company_id}", json={"partner_id": partner["id"]}).status_code == 200
    create_platform_admin("admin@northwind.example", "Northwind Admin", PASSWORD, partner_slug=north["slug"])
    create_platform_admin("admin@southern.example", "Southern Admin", PASSWORD, partner_slug=south["slug"])
    return {"app": app, "ops": ops, "north": north, "south": south, "alpha": alpha, "beta": beta, "gamma": gamma}


def _partner(app, email):
    client = HostClient(app, PLATFORM_HOST)
    r = client.post("/api/v1/partner/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


def test_partner_sees_only_its_own_clients_as_counts(world):
    north = _partner(world["app"], "admin@northwind.example")
    me = north.get("/api/v1/partner/me").json()
    assert me["partner"]["name"] == "Northwind Partners"
    items = north.get("/api/v1/partner/companies").json()["items"]
    assert sorted(i["name"] for i in items) == ["Acme Corp", "Beta Industries"]
    for item in items:
        assert not HR_FIELDS & set(item), item  # aggregate counts only
        assert item["active_employees"] == 1
    south = _partner(world["app"], "admin@southern.example")
    assert [i["name"] for i in south.get("/api/v1/partner/companies").json()["items"]] == ["Gamma Labs"]


def test_partner_session_opens_neither_the_platform_console_nor_tenant_data(world):
    app = world["app"]
    north = _partner(app, "admin@northwind.example")
    for path in ("/api/v1/platform/companies", "/api/v1/platform/partners", "/api/v1/platform/me",
                 "/api/v1/platform/audit-logs"):
        assert north.get(path).status_code == 401, path
    assert north.patch(f"/api/v1/platform/companies/{world['gamma'].company_id}", json={"status": "suspended"}).status_code == 401
    for path in ("/api/v1/employees", "/api/v1/payroll", "/api/v1/attendance/me/today", "/api/v1/users", "/api/v1/branding",
                 "/api/v1/audit-logs", "/api/v1/auth/me"):
        assert north.get(path).status_code == 401, path
    # Each kind of account signs in only through its own console, with the same generic answer.
    wrong = HostClient(app, PLATFORM_HOST).post("/api/v1/platform/auth/login",
                                                json={"email": "admin@northwind.example", "password": PASSWORD})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "Invalid email or password."
    wrong = HostClient(app, PLATFORM_HOST).post("/api/v1/partner/auth/login",
                                                json={"email": "ops@platform.io", "password": "Platf0rmPass!"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "Invalid email or password."
    # Platform operators cannot use the partner console either.
    assert world["ops"].get("/api/v1/partner/companies").status_code == 401
    # The partner console exists only on the platform host.
    tenant = HostClient(app, tenant_host("acme-corp"))
    assert tenant.post("/api/v1/partner/auth/login",
                       json={"email": "admin@northwind.example", "password": PASSWORD}).status_code == 404


def test_partner_brand_reaches_only_its_own_tenants(world):
    app = world["app"]
    north = _partner(app, "admin@northwind.example")
    r = north.put("/api/v1/partner/brand/draft", json={"changes": {"product_name": "Northwind HR"},
                                                      "locked_fields": ["product_name"]})
    assert r.status_code == 200, r.text
    assert north.post("/api/v1/partner/brand/publish").status_code == 200
    product = lambda slug: HostClient(app, tenant_host(slug)).get("/api/v1/public/brand").json()["product_name"]
    assert product("acme-corp") == "Northwind HR" and product("beta-industries") == "Northwind HR"
    assert product("gamma-labs") != "Northwind HR"
    south = _partner(app, "admin@southern.example")
    assert south.get("/api/v1/partner/brand").json()["brand"] is None  # Southern Cross has not published one
    assert north.get("/api/v1/partner/brand").json()["brand"]["fields"]["product_name"] == "Northwind HR"


def test_only_platform_operators_create_partner_admins(world):
    app = world["app"]
    body = {"email": "second@northwind.example", "full_name": "Second Admin", "password": PASSWORD}
    north = _partner(app, "admin@northwind.example")
    assert north.post(f"/api/v1/platform/partners/{world['north']['id']}/admins", json=body).status_code == 401
    r = world["ops"].post(f"/api/v1/platform/partners/{world['north']['id']}/admins", json=body)
    assert r.status_code == 200 and r.json()["partner_id"] == world["north"]["id"]
    assert world["ops"].post(f"/api/v1/platform/partners/{world['north']['id']}/admins", json=body).status_code == 400
    weak = world["ops"].post(f"/api/v1/platform/partners/{world['north']['id']}/admins",
                             json={**body, "email": "weak@northwind.example", "password": "password"})
    assert weak.status_code in (400, 422)
    second = _partner(app, "second@northwind.example")
    assert sorted(i["name"] for i in second.get("/api/v1/partner/companies").json()["items"]) == ["Acme Corp", "Beta Industries"]


def test_suspended_partner_and_rescoped_accounts_lose_access_at_once(world):
    from app.database.db import system_session
    from app.models.company import PlatformAdmin

    app = world["app"]
    north = _partner(app, "admin@northwind.example")
    south = _partner(app, "admin@southern.example")
    assert world["ops"].patch(f"/api/v1/platform/partners/{world['north']['id']}", json={"status": "suspended"}).status_code == 200
    assert north.get("/api/v1/partner/me").status_code == 401
    # Turning a partner account into a platform operator does not let its live token into the platform console.
    with system_session() as db:
        admin = db.query(PlatformAdmin).filter(PlatformAdmin.email == "admin@southern.example").one()
        admin.partner_id = None
        db.commit()
    assert south.get("/api/v1/partner/me").status_code == 401
    assert south.get("/api/v1/platform/companies").status_code == 401
