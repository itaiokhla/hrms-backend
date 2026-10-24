"""Cross-tenant isolation: API, ORM guards and PostgreSQL row-level security."""

import pytest
from sqlalchemy import text

from tests.helpers import ApiClient, create_employee, invite_and_activate, role_id, signup_company


def test_company_cannot_read_other_company_employee(three_companies):
    a, b = three_companies["A"], three_companies["B"]
    b_emp = create_employee(b, "Bella")
    assert a.get(f"/api/v1/employees/{b_emp['id']}").status_code == 404
    assert a.put(f"/api/v1/employees/{b_emp['id']}", json={"position": "x"}).status_code == 404
    assert a.post(f"/api/v1/employees/{b_emp['id']}/deactivate").status_code == 404
    # Same response as a genuinely missing id: existence is not revealed.
    assert a.get("/api/v1/employees/999999").json() == a.get(f"/api/v1/employees/{b_emp['id']}").json()


def test_lists_and_search_never_include_other_tenants(three_companies):
    a, b, c = three_companies["A"], three_companies["B"], three_companies["C"]
    create_employee(a, "Shared", "Name")
    create_employee(b, "Shared", "Name")
    create_employee(c, "Shared", "Name")
    for client in (a, b, c):
        body = client.get("/api/v1/employees?q=Shared").json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        # A full "first last" name matches too, case-insensitively, still within the tenant.
        assert client.get("/api/v1/employees", params={"q": "shared name"}).json()["total"] == 1


def test_forged_company_id_is_rejected(three_companies):
    a, b = three_companies["A"], three_companies["B"]
    r = a.post(
        "/api/v1/employees",
        json={"first_name": "X", "last_name": "Y", "email": "x@y.io", "employee_code": "X1",
              "hire_date": "2024-01-01", "company_id": b.company_id},
    )
    assert r.status_code == 422
    assert b.get("/api/v1/employees?q=X").json()["total"] == 0


def test_cannot_assign_manager_from_another_company(three_companies):
    a, b = three_companies["A"], three_companies["B"]
    a_emp = create_employee(a, "Alice")
    b_mgr = create_employee(b, "Bob")
    r = a.put(f"/api/v1/org/employees/{a_emp['id']}/manager", json={"manager_id": b_mgr["id"]})
    assert r.status_code == 404
    assert a.get(f"/api/v1/employees/{a_emp['id']}").json()["manager_id"] is None


def test_cannot_link_login_to_other_company_employee(three_companies):
    a, b = three_companies["A"], three_companies["B"]
    b_emp = create_employee(b, "Bella")
    r = a.post("/api/v1/users/invite", json={"email": "new@acme.io", "full_name": "New Person", "employee_id": b_emp["id"]})
    assert r.status_code == 404


def test_roles_with_same_names_are_independent(three_companies):
    a, b = three_companies["A"], three_companies["B"]
    grants = [{"permission": "employee:read", "scope": "COMPANY"}]
    ra = a.post("/api/v1/roles", json={"name": "Recruiter", "grants": grants})
    rb = b.post("/api/v1/roles", json={"name": "Recruiter", "grants": []})
    assert ra.status_code == rb.status_code == 200
    a.patch(f"/api/v1/roles/{ra.json()['id']}", json={"grants": []})
    b_roles = {r["name"]: r for r in b.get("/api/v1/roles").json()}
    assert b_roles["Recruiter"]["grants"] == []
    # Company B cannot touch Company A's role.
    assert b.patch(f"/api/v1/roles/{ra.json()['id']}", json={"name": "Hijacked"}).status_code == 404
    assert b.delete(f"/api/v1/roles/{ra.json()['id']}").status_code == 404


def test_cannot_assign_other_company_role(three_companies, app):
    a, b = three_companies["A"], three_companies["B"]
    emp = create_employee(a, "Erin")
    user = invite_and_activate(app, a, emp, ["employee"])
    b_admin_role = role_id(b, "company_admin")
    r = a.put(f"/api/v1/users/{user.user_id}/roles", json={"role_ids": [b_admin_role]})
    assert r.status_code == 404


def test_cross_tenant_payslip_and_audit_isolation(three_companies, app):
    a, b = three_companies["A"], three_companies["B"]
    emp = create_employee(b, "Payee")
    b.post("/api/v1/salary-structures", json={"employee_id": emp["id"], "basic_salary": "50000", "effective_from": "2024-01-01"})
    payroll = b.post(f"/api/v1/payroll/generate?employee_id={emp['id']}&year=2024&month=2")
    assert payroll.status_code == 200, payroll.text
    pid = payroll.json()["id"]
    assert a.get(f"/api/v1/payroll/{pid}/slip").status_code == 404
    assert a.get(f"/api/v1/payroll/{pid}/slip.pdf").status_code == 404
    assert a.get("/api/v1/payroll").json()["total"] == 0

    a_logs = a.get("/api/v1/audit-logs?limit=200").json()["logs"]
    assert a_logs and all("Payee" not in log["description"] for log in a_logs)
    assert all(log["actor_email"] != b.email for log in a_logs)


def test_bulk_operation_with_foreign_id_is_all_or_nothing(three_companies, app):
    a, b = three_companies["A"], three_companies["B"]
    mine = create_employee(a, "Mine")
    theirs = create_employee(b, "Theirs")
    reviewer_emp = create_employee(a, "Reviewer")
    reviewer = invite_and_activate(app, a, reviewer_emp, ["employee"])
    r = a.put(f"/api/v1/users/{reviewer.user_id}/assigned-employees", json={"employee_ids": [mine["id"], theirs["id"]]})
    assert r.status_code == 404
    detail = a.get(f"/api/v1/users/{reviewer.user_id}").json()
    assert detail["assigned_employee_ids"] == []


def test_company_user_cannot_use_platform_endpoints(three_companies):
    a = three_companies["A"]
    assert a.get("/api/v1/platform/companies").status_code == 401
    token = a.http.cookies.get("hrms_access")
    r = a.get("/api/v1/platform/companies", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_platform_admin_manages_tenants_without_hr_data(three_companies, app):
    """EC-ORG-20: tenant HR endpoints refuse the platform principal."""
    from app.cli import create_platform_admin

    create_platform_admin("ops@platform.io", "Ops", "Platf0rmPass!")
    ops = ApiClient(app)
    assert ops.post("/api/v1/platform/auth/login", json={"email": "ops@platform.io", "password": "Platf0rmPass!"}).status_code == 200
    companies = ops.get("/api/v1/platform/companies").json()
    assert companies["total"] == 3
    assert set(companies["items"][0]) >= {"name", "employee_count", "subscription_status"}
    # A platform session is not a company session: tenant HR endpoints refuse it.
    assert ops.get("/api/v1/employees").status_code == 401
    assert ops.get("/api/v1/payroll").status_code == 401

    a = three_companies["A"]
    r = ops.patch(f"/api/v1/platform/companies/{a.company_id}", json={"status": "suspended"})
    assert r.status_code == 200
    assert a.get("/api/v1/auth/me").status_code == 401  # sessions revoked immediately


def test_inactive_subscription_blocks_data_but_not_session(three_companies, app):
    """EC-WL-22 (API half): an inactive subscription gets no data; the blocked screen is checked in Chrome."""
    from app.cli import create_platform_admin

    create_platform_admin("ops@platform.io", "Ops", "Platf0rmPass!")
    ops = ApiClient(app)
    ops.post("/api/v1/platform/auth/login", json={"email": "ops@platform.io", "password": "Platf0rmPass!"})
    a = three_companies["A"]
    ops.patch(f"/api/v1/platform/companies/{a.company_id}", json={"subscription_status": "past_due"})
    r = a.get("/api/v1/employees")
    assert r.status_code == 402 and r.json()["code"] == "SUBSCRIPTION_INACTIVE"
    me = a.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["company"]["subscription"]["active"] is False


# ---------------------------------------------------------------------------
# Defence in depth below the API
# ---------------------------------------------------------------------------
def test_orm_guard_blocks_writes_without_tenant_context():
    from app.database.db import SessionLocal, TenantContextError
    from app.models.holiday import Holiday

    db = SessionLocal()
    try:
        db.add(Holiday(name="Rogue", date=__import__("datetime").date(2030, 1, 1)))
        with pytest.raises(TenantContextError):
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_orm_guard_blocks_cross_tenant_write(three_companies):
    from datetime import date

    from app.database.db import SessionLocal, TenantContextError, bind_tenant
    from app.models.holiday import Holiday

    a, b = three_companies["A"], three_companies["B"]
    db = SessionLocal()
    try:
        bind_tenant(db, a.company_id)
        db.add(Holiday(company_id=b.company_id, name="Injected", date=date(2030, 1, 1)))
        with pytest.raises(TenantContextError):
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_orm_queries_without_context_return_nothing(three_companies):
    from app.database.db import SessionLocal
    from app.models.employee import Employee

    db = SessionLocal()
    try:
        assert db.query(Employee).count() == 0
    finally:
        db.close()


def test_rls_blocks_raw_sql_across_tenants(three_companies):
    """Even hand-written SQL on the application role cannot cross tenants."""
    from app.database.db import engine, rls_status

    a, b = three_companies["A"], three_companies["B"]
    assert rls_status()["rls_enforced"] is True
    with engine.begin() as conn:
        # No tenant context: no rows at all.
        assert conn.execute(text("SELECT count(*) FROM employees")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 0
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(a.company_id)})
        companies = {r[0] for r in conn.execute(text("SELECT DISTINCT company_id FROM employees"))}
        assert companies == {a.company_id}
        # Inserting a row for another tenant violates the policy.
        with pytest.raises(Exception):
            conn.execute(
                text("INSERT INTO holidays (company_id, name, date, is_optional, created_at, updated_at) "
                     "VALUES (:b, 'x', '2030-01-01', false, now(), now())"),
                {"b": b.company_id},
            )


def test_tenant_context_does_not_leak_through_connection_pool(three_companies):
    from app.database.db import engine

    a = three_companies["A"]
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(a.company_id)})
            assert conn.execute(text("SELECT count(*) FROM employees")).scalar() > 0
        # Same physical connection, next transaction: the setting is gone.
        with conn.begin():
            assert conn.execute(text("SELECT count(*) FROM employees")).scalar() == 0


def test_app_role_cannot_tamper_with_audit_log_or_platform_tables(three_companies):
    from app.database.db import engine

    a = three_companies["A"]
    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(a.company_id)})
            conn.execute(text("DELETE FROM audit_logs"))
    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(text("SELECT count(*) FROM platform_admins"))


def test_background_job_requires_explicit_tenant():
    from app.database.db import SessionLocal, TenantContextError, bind_tenant

    db = SessionLocal()
    try:
        with pytest.raises(TenantContextError):
            bind_tenant(db, None)
        bind_tenant(db, 1)
        with pytest.raises(TenantContextError):
            bind_tenant(db, 2)  # a job can never switch tenants mid-session
    finally:
        db.close()
