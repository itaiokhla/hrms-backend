"""Per-tenant data keys: envelope encryption, rotation, master-key re-wrap, fingerprints, start-up key checks.

3.7 §12, 3.9 §9, decision I-10; EC-SEC-01, EC-SEC-08, EC-SEC-15.
"""

import base64
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from app.config import settings
from app.database.db import SessionLocal, TenantContextError, bind_tenant, system_session
from app.models.security_keys import TenantDataKey
from app.payroll.banking.errors import CryptoError
from app.payroll.services import keys

ACCOUNT = "50100123456789"


@pytest.fixture(autouse=True)
def _fresh_keys():
    keys.reset_cache()
    yield
    keys.reset_cache()


@contextmanager
def tenant(company_id):
    db = SessionLocal()
    bind_tenant(db, company_id)
    try:
        yield db
    finally:
        db.close()


def _key_rows(company_id=None):
    with system_session() as db:
        query = db.query(TenantDataKey).order_by(TenantDataKey.id)
        if company_id is not None:
            query = query.filter(TenantDataKey.company_id == company_id)
        return query.all()


def _audit_actions(company_id):
    with system_session() as db:
        return [a for (a,) in db.execute(text(
            "SELECT action FROM audit_logs WHERE company_id = :c AND module = 'Payroll Settings' "
            "AND action LIKE 'DATA_KEY%' ORDER BY id"
        ), {"c": company_id})]


def test_encrypt_field_round_trips_and_creates_one_wrapped_key_lazily(three_companies, caplog):
    a = three_companies["A"].company_id
    assert _key_rows(a) == []
    caplog.set_level(logging.DEBUG)
    with tenant(a) as db:
        first = keys.encrypt_field(db, a, ACCOUNT, aad=b"bank_accounts.account_number:1")
        second = keys.encrypt_field(db, a, ACCOUNT, aad=b"bank_accounts.account_number:1")
        db.commit()
        assert first != second and ACCOUNT not in first  # random nonces, no plaintext
        assert keys.decrypt_field(db, a, first, aad=b"bank_accounts.account_number:1") == ACCOUNT
    [row] = _key_rows(a)
    assert (row.status, row.version, row.purpose, row.algorithm) == ("ACTIVE", 1, "PAYROLL_PII", "AES-256-GCM")
    assert row.kek_id == keys.master_key_id() and row.wrapped_key.startswith(f"v1:{row.kek_id}:")
    assert first.startswith(f"v1:{row.key_id}:")
    assert _audit_actions(a) == ["DATA_KEY_CREATED"]
    assert ACCOUNT not in caplog.text


def test_ciphertexts_are_bound_to_their_place_and_tenant(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as db:
        sealed = keys.encrypt_field(db, a, ACCOUNT, aad=b"bank_accounts.account_number:1")
        db.commit()
        with pytest.raises(CryptoError):
            keys.decrypt_field(db, a, sealed, aad=b"bank_accounts.account_number:2")
    with tenant(b) as db:
        keys.encrypt_field(db, b, "other", aad=b"x")
        db.commit()
        with pytest.raises(CryptoError) as foreign:
            keys.decrypt_field(db, b, sealed, aad=b"bank_accounts.account_number:1")
        assert foreign.value.code == "UNKNOWN_DATA_KEY"
        with pytest.raises(TenantContextError):
            keys.decrypt_field(db, a, sealed, aad=b"bank_accounts.account_number:1")


def test_ec_sec_01_tenant_data_keys_are_isolated(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as db:
        keys.encrypt_field(db, a, ACCOUNT)
        db.commit()
    with tenant(b) as db:
        assert db.query(TenantDataKey).count() == 0
        assert db.execute(text("SELECT count(*) FROM tenant_data_keys")).scalar() == 0
    unbound = SessionLocal()
    try:
        with pytest.raises(TenantContextError):
            keys.encrypt_field(unbound, a, ACCOUNT)
    finally:
        unbound.close()


def test_concurrent_first_use_creates_a_single_key(three_companies):
    a = three_companies["A"].company_id

    def first_use(_):
        with tenant(a) as db:
            key_id = keys.active_key(db, a).key_id
            db.commit()
            return key_id

    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = set(pool.map(first_use, range(3)))
    assert len(ids) == 1 and len(_key_rows(a)) == 1


def test_ec_sec_15_rotation_retires_the_old_key_which_keeps_decrypting(three_companies):
    founder = three_companies["A"]
    a = founder.company_id
    with tenant(a) as db:
        old = keys.encrypt_field(db, a, ACCOUNT, aad=b"row:1")
        db.commit()
        rotated = keys.rotate(db, None, a)
        db.commit()
        assert keys.decrypt_field(db, a, old, aad=b"row:1") == ACCOUNT
        assert keys.needs_reencryption(db, a, old)
        new = keys.reencrypt_field(db, a, old, aad=b"row:1")
        assert new.startswith(f"v1:{rotated.key_id}:") and not keys.needs_reencryption(db, a, new)
        assert keys.reencrypt_field(db, a, new, aad=b"row:1") == new
        assert keys.decrypt_field(db, a, new, aad=b"row:1") == ACCOUNT
        db.commit()
    rows = _key_rows(a)
    assert [(r.version, r.status) for r in rows] == [(1, "RETIRED"), (2, "ACTIVE")]
    assert rows[0].rotated_at is not None
    assert _audit_actions(a) == ["DATA_KEY_CREATED", "DATA_KEY_ROTATED"]


def test_ec_sec_15_master_key_rotation_rewraps_without_touching_data(three_companies, monkeypatch):
    a = three_companies["A"].company_id
    with tenant(a) as db:
        sealed = keys.encrypt_field(db, a, ACCOUNT)
        db.commit()
    old_master = keys.master_key()
    old_kek = keys.master_key_id()
    new_master = secrets.token_bytes(32)
    monkeypatch.setattr(settings, "payroll_master_key", base64.b64encode(new_master).decode())
    keys.reset_cache()
    with tenant(a) as db:
        with pytest.raises(CryptoError) as unknown:
            keys.decrypt_field(db, a, sealed)
        assert unknown.value.code == "UNKNOWN_MASTER_KEY"
        assert keys.rewrap_master(db, a, old_master) == 1
        assert keys.rewrap_master(db, a, old_master) == 0
        db.commit()
        keys.reset_cache()
        assert keys.decrypt_field(db, a, sealed) == ACCOUNT
    [row] = _key_rows(a)
    assert row.kek_id == keys.master_key_id(new_master) != old_kek
    assert "DATA_KEYS_REWRAPPED" in _audit_actions(a)


def test_fingerprints_match_within_a_tenant_and_differ_across_tenants(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as db:
        one = keys.fingerprint(db, a, "IN", "HDFC", "5010 0123-456789")
        same = keys.fingerprint(db, a, "in", "hdfc", ACCOUNT)
    with tenant(b) as db:
        other = keys.fingerprint(db, b, "IN", "HDFC", ACCOUNT)
    assert one == same != other and ACCOUNT not in one


def test_master_key_is_required_and_well_formed_outside_development(monkeypatch, tmp_path):
    monkeypatch.setattr(keys, "DEV_KEY_FILE", tmp_path / ".local-secrets" / "payroll-master-key.dev")
    for environment in ("production", "staging"):
        monkeypatch.setattr(settings, "environment", environment)
        monkeypatch.setattr(settings, "payroll_master_key", "")
        keys.reset_cache()
        with pytest.raises(keys.MasterKeyError):
            keys.check_master_key()
    for malformed in ("not-base64!!", base64.b64encode(b"short").decode()):
        for environment in ("production", "development", "test"):
            monkeypatch.setattr(settings, "environment", environment)
            monkeypatch.setattr(settings, "payroll_master_key", malformed)
            keys.reset_cache()
            with pytest.raises(keys.MasterKeyError) as error:
                keys.check_master_key()
            assert malformed not in str(error.value)
    good = base64.b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "payroll_master_key", good)
    keys.reset_cache()
    keys.check_master_key()
    assert keys.master_key() == base64.b64decode(good)


def test_development_generates_a_local_key_once_and_test_uses_an_ephemeral_one(monkeypatch, tmp_path):
    dev_file = tmp_path / ".local-secrets" / "payroll-master-key.dev"
    monkeypatch.setattr(keys, "DEV_KEY_FILE", dev_file)
    monkeypatch.setattr(settings, "payroll_master_key", "")
    monkeypatch.setattr(settings, "environment", "development")
    keys.reset_cache()
    generated = keys.master_key()
    assert dev_file.exists() and base64.b64decode(dev_file.read_text().strip()) == generated
    keys.reset_cache()
    assert keys.master_key() == generated
    monkeypatch.setattr(settings, "environment", "test")
    keys.reset_cache()
    assert len(keys.master_key()) == 32 and keys.master_key() != generated


def test_the_dev_key_file_is_git_ignored():
    import subprocess
    from pathlib import Path

    backend = Path(keys.__file__).resolve().parents[3]
    result = subprocess.run(["git", "check-ignore", "-q", str(keys.DEV_KEY_FILE)], cwd=backend, capture_output=True)
    if result.returncode == 128:
        pytest.skip("not a git checkout")
    assert result.returncode == 0
