"""Step-up re-authentication: TOTP or emailed one-time code, bound to the session, required within 5 minutes.

3.7 §3.2, 3.9 §9; EC-BNK-06, EC-SEC-11.
"""

import email
import re
import time
from datetime import datetime, timedelta, timezone
from email import policy
from pathlib import Path

import pyotp
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.core import clock
from app.core.errors import AppError
from app.core.principal import Principal
from app.database.db import system_session
from app.payroll.services import step_up
from tests.helpers import create_employee, invite_and_activate, login, new_client

URL = "/api/v1/payroll/step-up"


@pytest.fixture
def user(three_companies, app):
    founder = three_companies["A"]
    return invite_and_activate(app, founder, create_employee(founder, "Priya"), ["employee"])


@pytest.fixture
def frozen(monkeypatch):
    state = {"now": datetime.now(timezone.utc)}
    monkeypatch.setattr(clock, "utcnow", lambda: state["now"])
    return state


@pytest.fixture
def guarded(app):
    """A tiny app with one route behind require_step_up, sharing the real dependencies."""
    from app.main import app_error_handler

    mini = FastAPI()
    mini.add_exception_handler(AppError, app_error_handler)

    @mini.get("/guarded")
    def guarded_route(principal: Principal = Depends(step_up.require_step_up(300))):
        return {"user_id": principal.user_id}

    def call(client):
        return TestClient(mini, cookies=client.http.cookies).get("/guarded")

    return call


def _emailed_code(company_id, address):
    folder = Path(settings.email_outbox_dir).resolve() / str(company_id)
    safe = re.sub(r"[^a-zA-Z0-9@._-]", "_", address)
    files = sorted(folder.glob(f"auth.step_up_code-{safe}-*.eml"), key=lambda p: p.stat().st_mtime_ns)
    assert files, "no step-up email was written"
    message = email.message_from_bytes(files[-1].read_bytes(), policy=policy.default)
    body = message.get_body(preferencelist=("plain",)).get_content()
    return re.search(r"Code: (\d{6})", body).group(1), message


def _clear_outbox(company_id):
    folder = Path(settings.email_outbox_dir).resolve() / str(company_id)
    for path in folder.glob("auth.step_up_code-*"):
        path.unlink()


def _actions(company_id):
    with system_session() as db:
        return [a for (a,) in db.execute(text(
            "SELECT action FROM audit_logs WHERE company_id = :c AND module = 'Authentication' "
            "AND action LIKE 'STEP_UP%' ORDER BY id"), {"c": company_id})]


def test_ec_bnk_06_emailed_code_steps_up_the_current_session(user, three_companies, guarded):
    company = three_companies["A"].company_id
    _clear_outbox(company)
    status = user.get(URL).json()
    assert (status["active"], status["available_methods"]) == (False, ["EMAIL_OTP"])
    assert guarded(user).json()["code"] == "STEP_UP_REQUIRED"
    r = user.post(f"{URL}/email-code")
    assert r.status_code == 200 and r.json() == {"sent": True, "expires_in_seconds": 600}
    code, message = _emailed_code(company, user.email)
    assert "confirmation code" in message["Subject"] and "reset your password" not in message.get_body().get_content()
    verified = user.post(URL, json={"code": code})
    assert verified.status_code == 200 and verified.json()["method"] == "EMAIL_OTP"
    assert user.get(URL).json()["active"] is True
    assert guarded(user).json() == {"user_id": user.user_id}
    assert user.post(URL, json={"code": code}).json()["code"] == "STEP_UP_INVALID"  # single use
    assert _actions(company) == ["STEP_UP_CODE_SENT", "STEP_UP_VERIFIED", "STEP_UP_FAILED"]
    with system_session() as db:
        details = db.execute(text("SELECT details::text FROM audit_logs WHERE action LIKE 'STEP_UP%'")).scalars().all()
    assert all(code not in d for d in details)


def test_a_code_works_only_in_the_session_that_asked_for_it(app, user, three_companies, guarded):
    company = three_companies["A"].company_id
    _clear_outbox(company)
    user.post(f"{URL}/email-code")
    code, _ = _emailed_code(company, user.email)
    other_session = login(app, user.email)
    r = other_session.post(URL, json={"code": code})
    assert (r.status_code, r.json()["code"]) == (400, "STEP_UP_INVALID")
    assert user.post(URL, json={"code": code}).status_code == 200
    assert guarded(other_session).json()["code"] == "STEP_UP_REQUIRED"  # step-up never spreads to other sessions
    assert guarded(user).status_code == 200


def test_wrong_codes_are_recorded_and_burn_the_code_after_five_attempts(user, three_companies, monkeypatch):
    company = three_companies["A"].company_id
    _clear_outbox(company)
    monkeypatch.setattr(settings, "auth_rate_limit_scale", 10.0)
    user.post(f"{URL}/email-code")
    code, _ = _emailed_code(company, user.email)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(step_up.CODE_MAX_ATTEMPTS):
        r = user.post(URL, json={"code": wrong})
        assert (r.status_code, r.json()["code"]) == (400, "STEP_UP_INVALID")
    assert user.post(URL, json={"code": code}).status_code == 400
    assert _actions(company).count("STEP_UP_FAILED") == step_up.CODE_MAX_ATTEMPTS + 1


def test_emailed_codes_expire(user, three_companies, frozen):
    company = three_companies["A"].company_id
    _clear_outbox(company)
    user.post(f"{URL}/email-code")
    code, _ = _emailed_code(company, user.email)
    frozen["now"] += timedelta(minutes=10, seconds=1)
    assert user.post(URL, json={"code": code}).json()["code"] == "STEP_UP_INVALID"


def test_a_new_code_replaces_the_previous_one(user, three_companies):
    company = three_companies["A"].company_id
    _clear_outbox(company)
    user.post(f"{URL}/email-code")
    first, _ = _emailed_code(company, user.email)
    time.sleep(0.01)
    user.post(f"{URL}/email-code")
    second, _ = _emailed_code(company, user.email)
    if first != second:
        assert user.post(URL, json={"code": first}).status_code == 400
    assert user.post(URL, json={"code": second}).status_code == 200


def test_step_up_expires_after_five_minutes(user, three_companies, frozen, guarded):
    company = three_companies["A"].company_id
    _clear_outbox(company)
    user.post(f"{URL}/email-code")
    code, _ = _emailed_code(company, user.email)
    assert user.post(URL, json={"code": code}).status_code == 200
    frozen["now"] += timedelta(seconds=299)
    assert guarded(user).status_code == 200
    frozen["now"] += timedelta(seconds=2)
    r = guarded(user)
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    assert user.get(URL).json()["active"] is False


def test_mfa_users_step_up_with_a_single_use_totp_code(user, three_companies, guarded):
    company = three_companies["A"].company_id
    secret = user.post("/api/v1/auth/mfa/setup").json()["secret"]
    totp = pyotp.TOTP(secret)
    assert user.post("/api/v1/auth/mfa/enable", json={"code": totp.now()}).status_code == 200
    assert user.get(URL).json()["available_methods"] == ["TOTP"]
    r = user.post(f"{URL}/email-code")
    assert (r.status_code, r.json()["code"]) == (409, "MFA_ENROLLED")
    assert user.post(URL, json={"code": "000000"}).json()["code"] == "STEP_UP_INVALID"
    # The enabling code burned the current time step; the next step's code is accepted once.
    next_code = totp.at(time.time() + 30)
    ok = user.post(URL, json={"code": next_code})
    assert ok.status_code == 200 and ok.json()["method"] == "TOTP"
    assert guarded(user).status_code == 200
    assert user.post(URL, json={"code": next_code}).json()["code"] == "STEP_UP_INVALID"
    assert "STEP_UP_VERIFIED" in _actions(company)


def test_step_up_attempts_are_rate_limited(user):
    codes = [user.post(URL, json={"code": "000000"}).status_code for _ in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429


def test_step_up_requests_are_strict_and_need_a_session(app, user):
    assert user.post(URL, json={"code": "123456", "method": "TOTP"}).status_code == 422
    assert user.post(URL, json={"code": 123456}).status_code == 422
    assert user.post(URL, json={"code": "12"}).status_code == 422
    anonymous = new_client(app)
    assert anonymous.get(URL).status_code == 401
    assert anonymous.post(URL, json={"code": "123456"}).status_code in (401, 403)
    assert anonymous.post(f"{URL}/email-code").status_code in (401, 403)


def test_step_up_of_one_tenant_never_affects_another(three_companies, guarded):
    a, b = three_companies["A"], three_companies["B"]
    _clear_outbox(a.company_id)
    a.post(f"{URL}/email-code")
    code, _ = _emailed_code(a.company_id, a.email)
    assert b.post(URL, json={"code": code}).status_code == 400
    assert a.post(URL, json={"code": code}).status_code == 200
    assert guarded(b).json()["code"] == "STEP_UP_REQUIRED"


def test_ec_sec_01_step_up_challenges_are_tenant_isolated(three_companies):
    from app.database.db import SessionLocal, bind_tenant
    from app.models.security_keys import StepUpChallenge

    a, b = three_companies["A"], three_companies["B"]
    assert a.post(f"{URL}/email-code").status_code == 200
    for company_id, expected in ((b.company_id, 0), (a.company_id, 1)):
        with SessionLocal() as db:
            bind_tenant(db, company_id)
            assert db.query(StepUpChallenge).count() == expected
            assert db.execute(text("SELECT count(*) FROM step_up_challenges")).scalar() == expected
