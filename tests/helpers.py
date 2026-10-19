"""Shared test helpers: CSRF-aware clients and tenant fixtures."""

import itertools
from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

PASSWORD = "Str0ngPassw0rd!"
_counter = itertools.count(1)


class ApiClient:
    """TestClient wrapper that behaves like the browser app (cookies + CSRF header)."""

    def __init__(self, app):
        self.http = TestClient(app)

    def _headers(self, method: str, headers: Optional[dict]) -> dict:
        merged = dict(headers or {})
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            csrf = self.http.cookies.get("hrms_csrf")
            if csrf:
                merged.setdefault("X-CSRF-Token", csrf)
        return merged

    def request(self, method: str, url: str, **kwargs):
        kwargs["headers"] = self._headers(method, kwargs.get("headers"))
        return self.http.request(method, url, **kwargs)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    def me(self) -> dict:
        r = self.get("/api/v1/auth/me")
        assert r.status_code == 200, r.text
        return r.json()


def new_client(app) -> ApiClient:
    return ApiClient(app)


def signup_company(app, name: str, email: Optional[str] = None) -> ApiClient:
    client = ApiClient(app)
    email = email or f"admin{next(_counter)}@{name.lower().replace(' ', '')}.io"
    r = client.post(
        "/api/v1/auth/signup",
        json={"company_name": name, "full_name": f"{name} Admin", "email": email, "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    client.email = email
    client.company_id = r.json()["me"]["company"]["id"]
    return client


def login(app, email: str, password: str = PASSWORD) -> ApiClient:
    client = ApiClient(app)
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    assert r.json()["mfa_required"] is False
    client.email = email
    return client


def role_id(admin: ApiClient, system_key: str) -> int:
    roles = admin.get("/api/v1/roles").json()
    return next(r["id"] for r in roles if r["system_key"] == system_key)


def create_employee(admin: ApiClient, first: str, last: str = "Test", manager_id: Optional[int] = None, **extra) -> dict:
    n = next(_counter)
    payload = {
        "first_name": first,
        "last_name": last,
        "email": f"{first.lower()}.{n}@corp.io",
        "employee_code": f"E{n:04d}",
        "hire_date": "2024-01-15",
        "position": extra.pop("position", "Engineer"),
    }
    if manager_id is not None:
        payload["manager_id"] = manager_id
    payload.update(extra)
    r = admin.post("/api/v1/employees", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def invite_and_activate(app, admin: ApiClient, employee: dict, role_keys: List[str]) -> ApiClient:
    """Create a login for an employee with the given roles and sign them in."""
    ids = [role_id(admin, k) for k in role_keys]
    r = admin.post(
        "/api/v1/users/invite",
        json={"email": employee["email"], "full_name": f"{employee['first_name']} {employee['last_name']}",
              "employee_id": employee["id"], "role_ids": ids},
    )
    assert r.status_code == 200, r.text
    token = r.json()["invite_url"].split("token=")[1]
    client = ApiClient(app)
    r = client.post("/api/v1/auth/invitations/accept", json={"token": token, "password": PASSWORD})
    assert r.status_code == 200, r.text
    client.email = employee["email"]
    client.employee = employee
    client.user_id = r.json()["me"]["user"]["id"]
    return client


@pytest.fixture
def three_companies(app) -> Dict[str, ApiClient]:
    return {
        "A": signup_company(app, "Acme Corp"),
        "B": signup_company(app, "Beta Industries"),
        "C": signup_company(app, "Cobalt Labs"),
    }


@pytest.fixture
def org(app):
    """The hierarchy fixture from the specification.

    Company A
    ├── Admin A
    ├── Manager A1
    │   ├── Manager A2
    │   │   ├── Employee A1
    │   │   └── Employee A2
    │   └── Employee A3
    └── Manager A3
        └── Employee A4
    Company B
    ├── Admin B
    └── Employee B1
    """
    admin_a = signup_company(app, "Acme Corp")
    admin_b = signup_company(app, "Beta Industries")

    m1 = create_employee(admin_a, "ManagerA1")
    m2 = create_employee(admin_a, "ManagerA2", manager_id=m1["id"])
    m3 = create_employee(admin_a, "ManagerA3")
    e1 = create_employee(admin_a, "EmployeeA1", manager_id=m2["id"])
    e2 = create_employee(admin_a, "EmployeeA2", manager_id=m2["id"])
    e3 = create_employee(admin_a, "EmployeeA3", manager_id=m1["id"])
    e4 = create_employee(admin_a, "EmployeeA4", manager_id=m3["id"])
    b1 = create_employee(admin_b, "EmployeeB1")

    users = {
        "admin_a": admin_a,
        "admin_b": admin_b,
        "m1": invite_and_activate(app, admin_a, m1, ["manager"]),
        "m2": invite_and_activate(app, admin_a, m2, ["manager"]),
        "m3": invite_and_activate(app, admin_a, m3, ["manager"]),
        "e1": invite_and_activate(app, admin_a, e1, ["employee"]),
        "e4": invite_and_activate(app, admin_a, e4, ["employee"]),
        "b1": invite_and_activate(app, admin_b, b1, ["employee"]),
    }
    employees = {"m1": m1, "m2": m2, "m3": m3, "e1": e1, "e2": e2, "e3": e3, "e4": e4, "b1": b1}
    return {"users": users, "emp": employees}
