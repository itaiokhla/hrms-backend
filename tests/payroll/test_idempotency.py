"""Idempotency-Key handling (app/payroll/services/idempotency.py), exercised through POST /payroll/fx-rates.

3.5 §16, 3.9 §4; EC-RUN-02, EC-RUN-04, EC-SEC-01.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from starlette.requests import Request

from app.core.errors import AppError
from app.database.db import SessionLocal, bind_tenant, system_session
from app.models.catalogue import FxRate
from app.models.jobs import IdempotencyKey
from app.payroll.services import idempotency
from tests.helpers import create_employee, invite_and_activate, login

URL = "/api/v1/payroll/fx-rates"
BODY = {"base_currency": "USD", "quote_currency": "AED", "rate": "3.6725", "as_of": "2026-10-01"}


def _post(client, key=None, body=None, url=URL):
    headers = {idempotency.HEADER: key} if key is not None else {}
    return client.post(url, json=BODY if body is None else body, headers=headers)


def _rates():
    with system_session() as db:
        return db.query(FxRate).count()


def _keys():
    with system_session() as db:
        return db.query(IdempotencyKey).order_by(IdempotencyKey.id).all()


def test_ec_run_02_a_repeated_request_replays_the_first_response(three_companies):
    admin = three_companies["A"]
    first = _post(admin, "create-rate-1")
    again = _post(admin, "create-rate-1")
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and again.headers.get(idempotency.REPLAY_HEADER) == "true"
    assert idempotency.REPLAY_HEADER not in first.headers
    assert _rates() == 1
    [key] = _keys()
    assert (key.status_code, key.method, key.path, key.user_id) == (201, "POST", URL, admin.me()["user"]["id"])
    assert key.expires_at - key.created_at == timedelta(hours=24)
    with system_session() as db:
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'FX_RATE_CREATED'")).scalar() == 1


def test_ec_run_04_the_same_key_for_a_different_request_is_a_conflict(three_companies):
    admin = three_companies["A"]
    rate_id = _post(admin, "k-1").json()["id"]
    changed = _post(admin, "k-1", {**BODY, "rate": "3.70"})
    assert (changed.status_code, changed.json()["code"]) == (409, "IDEMPOTENCY_KEY_REUSED")
    other_path = admin.post(f"{URL}/{rate_id}/reject", headers={idempotency.HEADER: "k-1"})
    assert (other_path.status_code, other_path.json()["code"]) == (409, "IDEMPOTENCY_KEY_REUSED")
    assert _rates() == 1


@pytest.mark.parametrize("key", ["", "has space", "x" * 121, "semi;colon", "ключ"])
def test_malformed_keys_are_rejected(three_companies, key):
    r = three_companies["A"].post(URL, json=BODY, headers={idempotency.HEADER: key.encode("utf-8")})
    assert (r.status_code, r.json()["code"]) == (400, "IDEMPOTENCY_KEY_INVALID")
    assert _rates() == 0 and _keys() == []


def test_requests_without_a_key_run_every_time(three_companies):
    admin = three_companies["A"]
    assert _post(admin).status_code == 201 and _post(admin).status_code == 201
    assert _rates() == 2 and _keys() == []


def test_a_failed_request_releases_its_key_for_a_retry(three_companies):
    admin = three_companies["A"]
    failed = _post(admin, "retry-me", {**BODY, "rate": "0"})
    assert (failed.status_code, failed.json()["code"]) == (422, "RATE_NOT_POSITIVE")
    assert _keys() == []
    fixed = _post(admin, "retry-me")
    assert fixed.status_code == 201 and _rates() == 1


def test_ec_sec_01_keys_belong_to_one_user_of_one_tenant(three_companies, app):
    founder, other_company = three_companies["A"], three_companies["B"]
    colleague = invite_and_activate(app, founder, create_employee(founder, "Colleague"), ["company_admin"])
    ids = {_post(client, "shared-key").json()["id"] for client in (founder, colleague, other_company)}
    assert len(ids) == 3 and _rates() == 3
    assert {(k.company_id, k.user_id) for k in _keys()} == {
        (founder.company_id, founder.me()["user"]["id"]), (founder.company_id, colleague.user_id),
        (other_company.company_id, other_company.me()["user"]["id"])}
    with SessionLocal() as db:
        bind_tenant(db, other_company.company_id)
        assert db.query(IdempotencyKey).count() == 1
        assert db.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() == 1


def test_an_expired_key_may_be_reused_for_a_new_request(three_companies):
    admin = three_companies["A"]
    _post(admin, "old-key")
    with system_session() as db:
        db.execute(text("UPDATE idempotency_keys SET expires_at = :t"),
                   {"t": datetime.now(timezone.utc) - timedelta(seconds=1)})
        db.commit()
    fresh = _post(admin, "old-key", {**BODY, "as_of": "2026-10-02"})
    assert fresh.status_code == 201 and idempotency.REPLAY_HEADER not in fresh.headers
    assert _rates() == 2 and len(_keys()) == 1


def test_ec_run_02_concurrent_duplicates_run_once(three_companies, app):
    admin = three_companies["A"]
    second_tab = login(app, admin.email)

    def send(client):
        return _post(client, "double-click")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, (admin, second_tab)))
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert _rates() == 1


class _Principal:
    def __init__(self, company_id, user_id):
        self.company_id, self.user_id = company_id, user_id


def _request(method="POST", key="unit-key", path="/api/v1/payroll/unit"):
    headers = [(idempotency.HEADER.lower().encode(), key.encode())] if key else []
    return Request({"type": "http", "method": method, "path": path, "headers": headers, "query_string": b""})


def test_only_state_changing_methods_accept_a_key(three_companies):
    admin = three_companies["A"]
    with SessionLocal() as db:
        bind_tenant(db, admin.company_id)
        with pytest.raises(AppError) as error:
            idempotency.execute(db, _Principal(admin.company_id, admin.me()["user"]["id"]), _request("GET"),
                                lambda: (200, {}))
        assert error.value.code == "IDEMPOTENCY_KEY_INVALID"


def test_decimals_in_requests_and_responses_are_exact_strings(three_companies):
    """Money never passes through float: 0.1 + 0.2 style drift or 17-digit amounts would change the hash."""
    admin = three_companies["A"]
    principal = _Principal(admin.company_id, admin.me()["user"]["id"])
    big, bigger = Decimal("12345678901234567.01"), Decimal("12345678901234567.02")
    assert idempotency.request_hash("POST", "/x", {"amount": big}) != idempotency.request_hash("POST", "/x", {"amount": bigger})
    with SessionLocal() as db:
        bind_tenant(db, admin.company_id)
        response = idempotency.execute(db, principal, _request(), lambda: (201, {"amount": Decimal("12.30")}),
                                       body={"amount": big})
        assert response.body == b'{"amount":"12.30"}'
    with SessionLocal() as db:
        bind_tenant(db, admin.company_id)
        replay = idempotency.execute(db, principal, _request(), lambda: (201, {"amount": Decimal("99")}),
                                     body={"amount": big})
        assert replay.body == b'{"amount":"12.30"}' and replay.headers[idempotency.REPLAY_HEADER] == "true"
        with pytest.raises(AppError) as conflict:
            idempotency.execute(db, principal, _request(), lambda: (201, {}), body={"amount": bigger})
        assert conflict.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_purge_removes_only_expired_keys_of_the_tenant(three_companies):
    a, b = three_companies["A"], three_companies["B"]
    _post(a, "a-old")
    _post(a, "a-new")
    _post(b, "b-old")
    with system_session() as db:
        db.execute(text("UPDATE idempotency_keys SET expires_at = :t WHERE key IN ('a-old', 'b-old')"),
                   {"t": datetime.now(timezone.utc) - timedelta(hours=1)})
        db.commit()
    with SessionLocal() as db:
        bind_tenant(db, a.company_id)
        assert idempotency.purge_expired(db, a.company_id) == 1
        db.commit()
    assert sorted(k.key for k in _keys()) == ["a-new", "b-old"]
