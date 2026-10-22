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


# ---------------------------------------------------------------------------
# F-28: TOTP secrets are encrypted at rest
# ---------------------------------------------------------------------------
def _stored_secret(user_id):
    from sqlalchemy import text

    from app.database.db import system_session

    with system_session() as db:
        return db.execute(text("SELECT mfa_secret FROM users WHERE id = :u"), {"u": user_id}).scalar()


def _set_stored_secret(user_id, value, enabled=True):
    from sqlalchemy import text

    from app.database.db import system_session

    with system_session() as db:
        db.execute(text("UPDATE users SET mfa_secret = :s, mfa_enabled = :e, mfa_last_step = NULL WHERE id = :u"),
                   {"s": value, "e": enabled, "u": user_id})
        db.commit()


def _mfa_login(app, email, code):
    client = ApiClient(app)
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["mfa_required"] is True, r.text
    r = client.post("/api/v1/auth/login/mfa", json={"mfa_token": r.json()["mfa_token"], "code": code})
    return client, r


def test_f28_totp_secret_is_stored_encrypted_and_sign_in_still_works(app):
    import time

    import pyotp
    from sqlalchemy import text

    from app.database.db import system_session

    admin = signup_company(app, "Acme Corp", email="owner@acme.io")
    c = login(app, "owner@acme.io")
    user_id = c.me()["user"]["id"]
    secret = c.post("/api/v1/auth/mfa/setup").json()["secret"]
    stored = _stored_secret(user_id)
    assert stored and stored != secret and secret not in stored and stored.startswith("v1:")
    with system_session() as db:
        purposes = [p for (p,) in db.execute(text(
            "SELECT purpose FROM tenant_data_keys WHERE company_id = :c AND status = 'ACTIVE'"),
            {"c": admin.company_id})]
    assert "MFA_SECRET" in purposes
    totp = pyotp.TOTP(secret)
    assert c.post("/api/v1/auth/mfa/enable", json={"code": "000000"}).json()["code"] == "MFA_INVALID"
    assert c.post("/api/v1/auth/mfa/enable", json={"code": totp.now()}).status_code == 200
    assert _stored_secret(user_id) == stored  # enabling never writes the plaintext back

    fresh, r = _mfa_login(app, "owner@acme.io", "000000")
    assert r.status_code == 401 and r.json()["code"] == "MFA_INVALID"
    fresh, r = _mfa_login(app, "owner@acme.io", totp.at(time.time() + totp.interval))
    assert r.status_code == 200, r.text
    assert fresh.get("/api/v1/auth/me").status_code == 200
    assert _stored_secret(user_id).startswith("v1:")


def test_f28_a_ciphertext_copied_to_another_user_does_not_open(app):
    import pyotp

    from tests.helpers import create_employee, invite_and_activate

    admin = signup_company(app, "Acme Corp", email="owner@acme.io")
    c = login(app, "owner@acme.io")
    secret = c.post("/api/v1/auth/mfa/setup").json()["secret"]
    stolen = _stored_secret(c.me()["user"]["id"])
    other = invite_and_activate(app, admin, create_employee(admin, "Olga"), ["employee"])
    _set_stored_secret(other.user_id, stolen)  # bound to the owner's row: useless elsewhere
    client, r = _mfa_login(app, other.email, pyotp.TOTP(secret).now())
    assert r.status_code == 401 and r.json()["code"] == "MFA_INVALID"
    assert client.get("/api/v1/auth/me").status_code == 401


def test_f28_legacy_plaintext_secret_still_signs_in_and_is_encrypted_on_use(app):
    import time

    import pyotp

    signup_company(app, "Acme Corp", email="owner@acme.io")
    c = login(app, "owner@acme.io")
    user_id = c.me()["user"]["id"]
    secret = pyotp.random_base32()
    _set_stored_secret(user_id, secret)  # a row written before F-28
    assert _stored_secret(user_id) == secret
    # A wrong code fails, but the secret is encrypted anyway (the failure path commits).
    _, r = _mfa_login(app, "owner@acme.io", "000000")
    assert r.status_code == 401
    stored = _stored_secret(user_id)
    assert stored.startswith("v1:") and secret not in stored
    client, r = _mfa_login(app, "owner@acme.io", pyotp.TOTP(secret).at(time.time() + 30))
    assert r.status_code == 200, r.text
    assert client.get("/api/v1/auth/me").status_code == 200
    assert _stored_secret(user_id) == stored


def test_f28_step_up_works_with_a_legacy_plaintext_secret_and_encrypts_it(app):
    import time

    import pyotp

    signup_company(app, "Acme Corp", email="owner@acme.io")
    c = login(app, "owner@acme.io")
    user_id = c.me()["user"]["id"]
    secret = pyotp.random_base32()
    _set_stored_secret(user_id, secret)
    assert c.post("/api/v1/payroll/step-up", json={"code": "000000"}).json()["code"] == "STEP_UP_INVALID"
    ok = c.post("/api/v1/payroll/step-up", json={"code": pyotp.TOTP(secret).at(time.time() + 30)})
    assert ok.status_code == 200 and ok.json()["method"] == "TOTP", ok.text
    stored = _stored_secret(user_id)
    assert stored.startswith("v1:") and secret not in stored


def test_f28_migration_encrypts_plaintext_secrets_only_with_a_master_key(app, caplog):
    import logging

    import pyotp
    from sqlalchemy import text

    from app.database.db import system_engine
    from app.services import mfa_secrets

    signup_company(app, "Acme Corp", email="owner@acme.io")
    signup_company(app, "Beta Industries", email="owner@beta.io")
    ids = {email: login(app, email).me()["user"]["id"] for email in ("owner@acme.io", "owner@beta.io")}
    secrets = {email: pyotp.random_base32() for email in ids}
    for email, user_id in ids.items():
        _set_stored_secret(user_id, secrets[email])

    with caplog.at_level(logging.WARNING, logger="app.services.mfa_secrets"):
        with system_engine.begin() as conn:
            assert mfa_secrets.encrypt_legacy_rows(conn, master_key_configured=False) == 0
    assert "PAYROLL_MASTER_KEY is not set" in caplog.text
    assert all(_stored_secret(ids[e]) == secrets[e] for e in ids)  # left as plaintext

    with system_engine.begin() as conn:
        assert mfa_secrets.encrypt_legacy_rows(conn, master_key_configured=True) >= 2
    for email, user_id in ids.items():
        stored = _stored_secret(user_id)
        assert stored.startswith("v1:") and secrets[email] not in stored
    with system_engine.begin() as conn:
        assert mfa_secrets.encrypt_legacy_rows(conn, master_key_configured=True) == 0  # idempotent
        left = conn.execute(text("SELECT count(*) FROM users WHERE mfa_secret IS NOT NULL "
                                 "AND mfa_secret NOT LIKE 'v1:%'")).scalar()
    assert left == 0
    assert all(s not in caplog.text for s in secrets.values())  # secrets are never logged
    # The migrated secrets still sign in.
    for email in ids:
        client, r = _mfa_login(app, email, pyotp.TOTP(secrets[email]).now())
        assert r.status_code == 200, r.text
