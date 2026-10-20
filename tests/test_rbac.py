"""Dynamic RBAC, delegation rules and session-time permission changes."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from tests.helpers import create_employee, invite_and_activate, role_id, signup_company


def test_registry_default_roles_are_valid():
    from app.core.permission_registry import DEFAULT_ROLES, is_valid_grant

    for role in DEFAULT_ROLES:
        for code, scope in role.grants:
            assert is_valid_grant(code, scope.value), (role.system_key, code, scope)


def test_user_without_roles_has_no_access(app):
    admin = signup_company(app, "Acme Corp")
    emp = create_employee(admin, "Nora")
    user = invite_and_activate(app, admin, emp, [])
    me = user.me()
    assert me["permissions"] == {}
    for path in ("/api/v1/employees", "/api/v1/leaves", "/api/v1/payroll", "/api/v1/users", "/api/v1/audit-logs"):
        assert user.get(path).status_code == 403, path
    # Navigating around the UI is irrelevant: the API refuses directly.
    assert user.post("/api/v1/employees", json={}).status_code in (403, 422)


def test_admin_creates_employee_and_invites_user(app):
    admin = signup_company(app, "Acme Corp")
    emp = create_employee(admin, "Ivy", position="Analyst")
    user = invite_and_activate(app, admin, emp, ["employee"])
    me = user.me()
    assert me["employee"]["id"] == emp["id"]
    assert me["company"]["id"] == admin.company_id
    assert me["permissions"]["employee:read"] == ["SELF"]


def _delegated_admin(app, admin):
    """A user-administrator who may assign roles but holds no payroll access."""
    role = admin.post("/api/v1/roles", json={"name": "People Ops", "grants": [
        {"permission": "user:read", "scope": "COMPANY"},
        {"permission": "user:invite", "scope": "COMPANY"},
        {"permission": "user:assign_role", "scope": "COMPANY"},
        {"permission": "role:read", "scope": "COMPANY"},
        {"permission": "role:create", "scope": "COMPANY"},
        {"permission": "role:update", "scope": "COMPANY"},
        {"permission": "employee:read", "scope": "COMPANY"},
    ]}).json()["id"]
    emp = create_employee(admin, "Delegate")
    user = invite_and_activate(app, admin, emp, [])
    assert admin.put(f"/api/v1/users/{user.user_id}/roles", json={"role_ids": [role]}).status_code == 200
    return user, role


def test_delegated_admin_cannot_create_more_privileged_role(app):
    admin = signup_company(app, "Acme Corp")
    delegate, _ = _delegated_admin(app, admin)
    r = delegate.post("/api/v1/roles", json={"name": "Shadow Payroll", "grants": [
        {"permission": "payroll:read", "scope": "COMPANY"}]})
    assert r.status_code == 403
    ok = delegate.post("/api/v1/roles", json={"name": "Viewer", "grants": [
        {"permission": "employee:read", "scope": "COMPANY"}]})
    assert ok.status_code == 200


def test_delegated_admin_cannot_assign_protected_or_stronger_roles(app):
    admin = signup_company(app, "Acme Corp")
    delegate, _ = _delegated_admin(app, admin)
    target = invite_and_activate(app, admin, create_employee(admin, "Target"), ["employee"])
    r = delegate.put(f"/api/v1/users/{target.user_id}/roles", json={"role_ids": [role_id(admin, "company_admin")]})
    assert r.status_code == 403
    r = delegate.put(f"/api/v1/users/{target.user_id}/roles", json={"role_ids": [role_id(admin, "payroll_manager")]})
    assert r.status_code == 403
    # Cannot elevate themselves either.
    r = delegate.put(f"/api/v1/users/{delegate.user_id}/roles", json={"role_ids": [role_id(admin, "payroll_manager")]})
    assert r.status_code == 403


def test_delegated_admin_cannot_edit_own_role_or_touch_admins(app):
    admin = signup_company(app, "Acme Corp")
    delegate, role = _delegated_admin(app, admin)
    r = delegate.patch(f"/api/v1/roles/{role}", json={"grants": [{"permission": "user:read", "scope": "COMPANY"}]})
    assert r.status_code == 403
    admin_user_id = admin.me()["user"]["id"]
    assert delegate.put(f"/api/v1/users/{admin_user_id}/roles", json={"role_ids": []}).status_code == 403


def test_last_admin_is_protected(app):
    admin = signup_company(app, "Acme Corp")
    me = admin.me()
    # Self changes are refused outright...
    assert admin.put(f"/api/v1/users/{me['user']['id']}/roles", json={"role_ids": []}).status_code == 403
    # ...and a second admin cannot remove the only other admin's role once alone.
    second = invite_and_activate(app, admin, create_employee(admin, "Second"), ["company_admin"])
    assert second.put(f"/api/v1/users/{me['user']['id']}/status", json={"status": "suspended"}).status_code == 200
    r = admin.get("/api/v1/auth/me")
    assert r.status_code == 401  # suspended admin's session ended
    first_admin_id = me["user"]["id"]
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        conn.execute(text("UPDATE users SET status='deactivated' WHERE id=:id"), {"id": first_admin_id})
    third = invite_and_activate(app, second, create_employee(second, "Third"), ["company_admin"])
    with system_engine.begin() as conn:
        conn.execute(text("UPDATE users SET status='deactivated' WHERE id=:id"), {"id": third.user_id})
    # `second` is now the only active administrator.
    alt = invite_and_activate(app, second, create_employee(second, "Alt"), ["hr_admin"])
    r = alt.put(f"/api/v1/users/{second.user_id}/status", json={"status": "suspended"})
    assert r.status_code == 403  # HR cannot manage an administrator's account
    from app.database.db import SessionLocal, bind_tenant
    from app.services import rbac_service
    import pytest
    from app.core.errors import AppError

    db = SessionLocal()
    try:
        bind_tenant(db, admin.company_id)
        with pytest.raises(AppError) as exc:
            rbac_service.assert_not_last_admin(db, admin.company_id, second.user_id)
        assert exc.value.code == "LAST_ADMIN"
    finally:
        db.close()


def test_role_deletion_blocked_while_assigned(app):
    admin = signup_company(app, "Acme Corp")
    role = admin.post("/api/v1/roles", json={"name": "Temp", "grants": []}).json()["id"]
    user = invite_and_activate(app, admin, create_employee(admin, "Tmp"), [])
    admin.put(f"/api/v1/users/{user.user_id}/roles", json={"role_ids": [role]})
    r = admin.delete(f"/api/v1/roles/{role}")
    assert r.status_code == 409 and r.json()["code"] == "ROLE_IN_USE"
    assert admin.delete(f"/api/v1/roles/{role_id(admin, 'company_admin')}").status_code == 403


def test_revoked_role_takes_effect_on_next_request(app):
    admin = signup_company(app, "Acme Corp")
    user = invite_and_activate(app, admin, create_employee(admin, "Hal"), ["hr_admin"])
    assert user.get("/api/v1/employees").status_code == 200
    admin.put(f"/api/v1/users/{user.user_id}/roles", json={"role_ids": [role_id(admin, "employee")]})
    # Same session, stale UI: the API now refuses.
    assert user.get("/api/v1/employees").json()["total"] == 1
    assert user.get("/api/v1/users").status_code == 403
    assert user.me()["permissions"]["employee:read"] == ["SELF"]


def test_suspended_account_is_cut_off_immediately(app):
    admin = signup_company(app, "Acme Corp")
    user = invite_and_activate(app, admin, create_employee(admin, "Sam"), ["employee"])
    assert user.get("/api/v1/auth/me").status_code == 200
    admin.put(f"/api/v1/users/{user.user_id}/status", json={"status": "suspended"})
    assert user.get("/api/v1/auth/me").status_code == 401
    assert user.post("/api/v1/auth/refresh").status_code == 401


def test_multiple_roles_combine_additively(app):
    admin = signup_company(app, "Acme Corp")
    user = invite_and_activate(app, admin, create_employee(admin, "Mia"), ["employee", "auditor"])
    perms = user.me()["permissions"]
    assert set(perms["employee:read"]) == {"SELF", "COMPANY"}
    assert "audit:read" in perms
    assert "payroll:prepare" not in perms
    assert user.get("/api/v1/payroll").status_code == 403


def test_expired_assignment_is_ignored(app):
    admin = signup_company(app, "Acme Corp")
    user = invite_and_activate(app, admin, create_employee(admin, "Exp"), ["employee", "auditor"])
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        conn.execute(
            text("UPDATE user_role_assignments SET effective_from = :f, effective_until = :u "
                 "WHERE user_id = :id AND role_id = :r"),
            {"f": datetime.now(timezone.utc) - timedelta(days=10), "u": datetime.now(timezone.utc) - timedelta(minutes=1),
             "id": user.user_id, "r": role_id(admin, "auditor")},
        )
    assert "audit:read" not in user.me()["permissions"]
    assert user.get("/api/v1/audit-logs").status_code == 403


def test_access_preview_matches_enforcement(app):
    admin = signup_company(app, "Acme Corp")
    user = invite_and_activate(app, admin, create_employee(admin, "Pre"), ["manager"])
    detail = admin.get(f"/api/v1/users/{user.user_id}").json()
    caps = {c["permission"]: c["scopes"] for m in detail["access"]["capabilities"].values() for c in m}
    assert caps["leave:approve"] == ["REPORTING_TREE"]
    assert "payroll:read" in caps and caps["payroll:read"] == ["SELF"]
    restricted = {r["permission"] for r in detail["access"]["restricted"]}
    assert "compensation:manage" in restricted
    assert caps == user.me()["permissions"]


def test_invited_user_cannot_be_pre_activated_by_admin(app):
    admin = signup_company(app, "Acme Corp")
    r = admin.post("/api/v1/users/invite", json={"email": "pending@acme.io", "full_name": "Pending Person"})
    uid = r.json()["user_id"]
    assert admin.put(f"/api/v1/users/{uid}/status", json={"status": "active"}).status_code == 400
    assert admin.get("/api/v1/users?status=invited").json()["total"] == 1


def test_duplicate_login_email_rejected_across_companies(app):
    a = signup_company(app, "Acme Corp", email="person@shared.io")
    b = signup_company(app, "Beta Industries")
    r = b.post("/api/v1/users/invite", json={"email": "person@shared.io", "full_name": "Clash"})
    assert r.status_code == 409
    assert a.me()["user"]["email"] == "person@shared.io"
