from tests.helpers import PASSWORD, ApiClient, login, signup_company


def test_signup_creates_isolated_workspace_and_admin(app):
    admin = signup_company(app, "Acme Corp")
    me = admin.me()
    assert me["company"]["name"] == "Acme Corp"
    assert me["is_company_admin"] is True
    assert "user:invite" in me["permissions"]
    assert me["employee"]["employee_code"] == "EMP-0001"


def test_signup_cannot_choose_role_or_company(app):
    client = ApiClient(app)
    r = client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Evil", "full_name": "E", "email": "e@evil.io", "password": PASSWORD,
            "role": "platform_admin", "company_id": 1,
        },
    )
    assert r.status_code == 422  # unknown fields are rejected outright


def test_login_without_company_selection_and_generic_errors(app):
    admin = signup_company(app, "Acme Corp", email="owner@acme.io")
    client = login(app, "owner@acme.io")
    assert client.me()["company"]["id"] == admin.company_id

    wrong = ApiClient(app).post("/api/v1/auth/login", json={"email": "owner@acme.io", "password": "Wrong1234"})
    unknown = ApiClient(app).post("/api/v1/auth/login", json={"email": "nobody@acme.io", "password": "Wrong1234"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_account_lockout_after_failed_attempts(app):
    signup_company(app, "Acme Corp", email="owner@acme.io")
    c = ApiClient(app)
    for _ in range(5):
        c.post("/api/v1/auth/login", json={"email": "owner@acme.io", "password": "Wrong1234"})
    r = c.post("/api/v1/auth/login", json={"email": "owner@acme.io", "password": PASSWORD})
    assert r.status_code == 423


def test_logout_revokes_session(app):
    admin = signup_company(app, "Acme Corp")
    access = admin.http.cookies.get("hrms_access")
    assert admin.post("/api/v1/auth/logout").status_code == 200
    replay = ApiClient(app)
    r = replay.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 401


def test_refresh_rotation_and_reuse_detection(app):
    admin = signup_company(app, "Acme Corp")
    old_refresh = admin.http.cookies.get("hrms_refresh")
    r = admin.post("/api/v1/auth/refresh")
    assert r.status_code == 200
    assert admin.http.cookies.get("hrms_refresh") != old_refresh

    import time

    from app.services import auth_service

    original = auth_service.REFRESH_GRACE_SECONDS
    auth_service.REFRESH_GRACE_SECONDS = 0
    try:
        time.sleep(1.1)
        attacker = ApiClient(app)
        r = attacker.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert r.status_code == 401
    finally:
        auth_service.REFRESH_GRACE_SECONDS = original
    # The legitimate session was revoked as a precaution.
    assert admin.post("/api/v1/auth/refresh").status_code == 401


def test_csrf_required_for_cookie_authenticated_writes(app):
    admin = signup_company(app, "Acme Corp")
    r = admin.http.put(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "An0therPass!"},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "CSRF_FAILED"


def test_password_reset_requires_verified_code(app, monkeypatch):
    signup_company(app, "Acme Corp", email="owner@acme.io")
    captured = {}
    from app.services import email_service

    monkeypatch.setattr(
        email_service.EmailService, "send_password_reset_code", staticmethod(lambda to, code, **_: captured.update(code=code))
    )
    c = ApiClient(app)
    # Response is identical for unknown emails (no enumeration).
    a = c.post("/api/v1/auth/forgot-password", json={"email": "owner@acme.io"})
    b = c.post("/api/v1/auth/forgot-password", json={"email": "ghost@acme.io"})
    assert a.json() == b.json()

    # The legacy takeover path (email + new password, no proof) is gone.
    r = c.post("/api/v1/auth/reset-password", json={"email": "owner@acme.io", "new_password": "Hijack1234"})
    assert r.status_code == 422

    bad = c.post("/api/v1/auth/verify-otp", json={"email": "owner@acme.io", "otp": "000000" if captured["code"] != "000000" else "111111"})
    assert bad.status_code == 400
    ok = c.post("/api/v1/auth/verify-otp", json={"email": "owner@acme.io", "otp": captured["code"]})
    assert ok.status_code == 200
    token = ok.json()["reset_token"]
    assert c.post("/api/v1/auth/reset-password", json={"reset_token": token, "new_password": "BrandNew123"}).status_code == 200
    # Single use.
    assert c.post("/api/v1/auth/reset-password", json={"reset_token": token, "new_password": "Again12345"}).status_code == 400
    login(app, "owner@acme.io", "BrandNew123")


def test_mfa_challenge_flow(app):
    import time

    import pyotp

    signup_company(app, "Acme Corp", email="owner@acme.io")
    c = login(app, "owner@acme.io")
    secret = c.post("/api/v1/auth/mfa/setup").json()["secret"]
    totp = pyotp.TOTP(secret)
    enable_code = totp.now()
    assert c.post("/api/v1/auth/mfa/enable", json={"code": enable_code}).status_code == 200

    def challenge(client):
        r = client.post("/api/v1/auth/login", json={"email": "owner@acme.io", "password": PASSWORD})
        assert r.json()["mfa_required"] is True
        return r.json()["mfa_token"]

    fresh = ApiClient(app)
    token = challenge(fresh)
    assert fresh.get("/api/v1/auth/me").status_code == 401
    # Codes are single-use: the one that enabled MFA is already spent...
    r = fresh.post("/api/v1/auth/login/mfa", json={"mfa_token": token, "code": enable_code})
    assert r.status_code == 401 and r.json()["code"] == "MFA_INVALID"
    # ...while the authenticator's next code works, once.
    next_code = totp.at(time.time() + totp.interval)
    assert fresh.post("/api/v1/auth/login/mfa", json={"mfa_token": token, "code": next_code}).status_code == 200
    assert fresh.get("/api/v1/auth/me").status_code == 200
    replay = ApiClient(app)
    r = replay.post("/api/v1/auth/login/mfa", json={"mfa_token": challenge(replay), "code": next_code})
    assert r.status_code == 401
    assert replay.get("/api/v1/auth/me").status_code == 401


def test_platform_admin_totp_codes_are_single_use(app):
    import pyotp

    from app.cli import create_platform_admin
    from app.database.db import system_session
    from app.models.company import PlatformAdmin

    create_platform_admin("ops@platform.io", "Ops", "Platf0rmPass!")
    secret = pyotp.random_base32()
    with system_session() as db:
        admin = db.query(PlatformAdmin).filter(PlatformAdmin.email == "ops@platform.io").one()
        admin.mfa_secret, admin.mfa_enabled = secret, True
        db.commit()
    creds = {"email": "ops@platform.io", "password": "Platf0rmPass!"}
    assert ApiClient(app).post("/api/v1/platform/auth/login", json=creds).json() == {"mfa_required": True}
    code = pyotp.TOTP(secret).now()
    assert ApiClient(app).post("/api/v1/platform/auth/login", json={**creds, "mfa_code": code}).status_code == 200
    r = ApiClient(app).post("/api/v1/platform/auth/login", json={**creds, "mfa_code": code})
    assert r.status_code == 401 and r.json()["code"] == "MFA_INVALID"
