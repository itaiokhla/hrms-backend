"""Per-tenant data keys and field encryption (3.7 §12, 3.9 §9, decision I-10).

Each tenant has one ACTIVE AES-256 data key per purpose, created on first use and stored only wrapped
by the platform master key (settings.payroll_master_key, base64 of 32 bytes) through
app.payroll.banking.crypto. Ciphertexts name their data key, so after `rotate` the RETIRED key keeps
decrypting older values while new values use the new key (EC-SEC-15); `rewrap_master` moves wrapped keys
to a new master key without touching data. Every ciphertext is bound to its tenant and purpose plus the
caller's associated data, so a value copied to another tenant, column or row does not decrypt.
Fingerprints for duplicate detection use a per-tenant HMAC key derived from the master key.

Master key sources:
* PAYROLL_MASTER_KEY, required in staging and production: start-up refuses a missing or malformed key
  (`check_master_key`, called by app.main and app.worker).
* development without the variable: a random key is generated once into the git-ignored
  ``hrms-backend/.local-secrets/payroll-master-key.dev`` and reused afterwards (never commit it;
  deleting it makes local encrypted data unreadable);
* test without the variable: an ephemeral in-memory key (tests normally set one).

Plaintext, keys and ciphertexts are never logged, audited or put in errors.
"""

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.core import clock
from app.database.db import TenantContextError, current_company_id
from app.models.security_keys import DATA_KEY_PURPOSES, TenantDataKey
from app.payroll import audit
from app.payroll.banking import crypto
from app.payroll.banking.errors import CryptoError
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

DEFAULT_PURPOSE = "PAYROLL_PII"
ACTIVE, RETIRED = "ACTIVE", "RETIRED"
DATA_KEY_LOCK_NAMESPACE = 6111  # payroll advisory-lock range 6100-6199
DEV_KEY_FILE = Path(__file__).resolve().parents[3] / ".local-secrets" / "payroll-master-key.dev"
_CACHE_SECONDS = 300

_lock = threading.Lock()
_master: Optional[bytes] = None
_cache: Dict[Tuple[int, str], Tuple[crypto.DataKey, float]] = {}


class MasterKeyError(RuntimeError):
    """The payroll master key is missing or malformed."""


def _decode(encoded: str) -> bytes:
    try:
        return crypto.load_master_key(encoded)
    except CryptoError:
        raise MasterKeyError("PAYROLL_MASTER_KEY must be the base64 encoding of exactly 32 random bytes") from None


def _development_key() -> bytes:
    if DEV_KEY_FILE.exists():
        return _decode(DEV_KEY_FILE.read_text(encoding="ascii").strip())
    DEV_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(secrets.token_bytes(crypto.KEY_BYTES)).decode("ascii")
    fd = os.open(DEV_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(encoded + "\n")
    logger.warning("Generated a development payroll master key in %s (git-ignored; never commit it)", DEV_KEY_FILE)
    return _decode(encoded)


def _load_master() -> bytes:
    configured = (settings.payroll_master_key or "").strip()
    if configured:
        return _decode(configured)
    if settings.environment == "development":
        return _development_key()
    if settings.environment == "test":
        return secrets.token_bytes(crypto.KEY_BYTES)
    raise MasterKeyError("PAYROLL_MASTER_KEY is required outside development and test")


def master_key() -> bytes:
    global _master
    with _lock:
        if _master is None:
            _master = _load_master()
        return _master


def check_master_key() -> None:
    """Start-up check: raises MasterKeyError for a missing (outside development/test) or malformed key."""
    master_key()


def master_key_id(key: Optional[bytes] = None) -> str:
    """A public identifier of a master key (a digest prefix, never the key)."""
    material = key if key is not None else master_key()
    return "mk-" + hashlib.sha256(b"hrms.payroll.master-key-id|" + material).hexdigest()[:16]


def reset_cache() -> None:
    """Forget the loaded master key and unwrapped data keys (tests, key rotation)."""
    global _master
    with _lock:
        _master = None
        _cache.clear()


def _require_tenant(db: Session, company_id: int) -> None:
    bound = current_company_id(db)
    if bound != company_id:
        raise TenantContextError("data keys are used only in a session bound to their company")


def _check_purpose(purpose: str) -> None:
    if purpose not in DATA_KEY_PURPOSES:
        raise ValueError(f"unknown data key purpose {purpose!r}")


def _unwrap(row: TenantDataKey) -> crypto.DataKey:
    cached = _cache.get((row.company_id, row.key_id))
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]
    if row.kek_id != master_key_id():
        raise CryptoError("UNKNOWN_MASTER_KEY", "this data key was wrapped by another master key; re-wrap it first")
    key = crypto.unwrap_key(master_key(), row.wrapped_key, row.key_id)
    with _lock:
        _cache[(row.company_id, row.key_id)] = (key, now + _CACHE_SECONDS)
    return key


def _lock_tenant_keys(db: Session, company_id: int) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :cid)"), {"ns": DATA_KEY_LOCK_NAMESPACE, "cid": company_id})


def _new_key(db: Session, company_id: int, purpose: str, version: int) -> TenantDataKey:
    dek = crypto.generate_data_key(f"dk{company_id}-{purpose.lower()}-v{version}-{secrets.token_hex(4)}")
    row = TenantDataKey(
        company_id=company_id, purpose=purpose, key_id=dek.key_id, version=version,
        kek_id=master_key_id(), wrapped_key=crypto.wrap_key(master_key(), dek, master_key_id()),
        algorithm="AES-256-GCM", status=ACTIVE,
    )
    db.add(row)
    db.flush()
    with _lock:
        _cache[(company_id, dek.key_id)] = (dek, time.monotonic() + _CACHE_SECONDS)
    return row


def active_key(db: Session, company_id: int, purpose: str = DEFAULT_PURPOSE) -> TenantDataKey:
    """The ACTIVE data key row of the tenant for `purpose`, created on first use (race-safe)."""
    _require_tenant(db, company_id)
    _check_purpose(purpose)
    query = db.query(TenantDataKey).filter(
        TenantDataKey.company_id == company_id, TenantDataKey.purpose == purpose, TenantDataKey.status == ACTIVE
    )
    row = query.one_or_none()
    if row is not None:
        return row
    _lock_tenant_keys(db, company_id)
    row = query.one_or_none()
    if row is not None:
        return row
    latest = (
        db.query(TenantDataKey.version)
        .filter(TenantDataKey.company_id == company_id, TenantDataKey.purpose == purpose)
        .order_by(TenantDataKey.version.desc())
        .limit(1)
        .scalar()
    )
    row = _new_key(db, company_id, purpose, (latest or 0) + 1)
    AuditLogService.log(
        db, None, audit.PAYROLL_SETTINGS, "DATA_KEY_CREATED", f"Data key version {row.version} created for {purpose}",
        "tenant_data_key", row.id, company_id=company_id, actor_type="system",
        details={"purpose": purpose, "version": row.version, "key_id": row.key_id},
    )
    return row


def _aad(company_id: int, purpose: str, aad: bytes) -> bytes:
    if not isinstance(aad, bytes):
        raise TypeError("associated data must be bytes")
    return f"c={company_id}|p={purpose}|".encode("ascii") + aad


def encrypt_field(db: Session, company_id: int, plaintext: str, purpose: str = DEFAULT_PURPOSE, *,
                  aad: bytes = b"") -> str:
    """Encrypt `plaintext` with the tenant's ACTIVE key; `aad` binds it to its place (e.g. b"bank_accounts.iban:42")."""
    row = active_key(db, company_id, purpose)
    return crypto.encrypt(_unwrap(row), plaintext, _aad(company_id, purpose, aad))


def decrypt_field(db: Session, company_id: int, ciphertext: str, purpose: str = DEFAULT_PURPOSE, *,
                  aad: bytes = b"") -> str:
    """Decrypt a value sealed by encrypt_field with any (ACTIVE or RETIRED) key of the tenant."""
    _require_tenant(db, company_id)
    _check_purpose(purpose)
    key_id = crypto.envelope_key_id(ciphertext)
    row = (
        db.query(TenantDataKey)
        .filter(TenantDataKey.company_id == company_id, TenantDataKey.purpose == purpose, TenantDataKey.key_id == key_id)
        .one_or_none()
    )
    if row is None:
        raise CryptoError("UNKNOWN_DATA_KEY", "no data key of this company opens the value")
    return crypto.decrypt(_unwrap(row), ciphertext, _aad(company_id, purpose, aad))


def needs_reencryption(db: Session, company_id: int, ciphertext: str, purpose: str = DEFAULT_PURPOSE) -> bool:
    """Whether the value was sealed with a key other than the ACTIVE one (after `rotate`)."""
    return crypto.envelope_key_id(ciphertext) != active_key(db, company_id, purpose).key_id


def reencrypt_field(db: Session, company_id: int, ciphertext: str, purpose: str = DEFAULT_PURPOSE, *,
                    aad: bytes = b"") -> str:
    """Move a value to the ACTIVE key (rotation jobs); a value already on it is returned unchanged."""
    if not needs_reencryption(db, company_id, ciphertext, purpose):
        return ciphertext
    return encrypt_field(db, company_id, decrypt_field(db, company_id, ciphertext, purpose, aad=aad), purpose, aad=aad)


def fingerprint(db: Session, company_id: int, country: str, routing: str, account: str) -> str:
    """Per-tenant keyed fingerprint of a bank account identity, for duplicate detection without decrypting."""
    _require_tenant(db, company_id)
    return crypto.fingerprint(crypto.derive_fingerprint_key(master_key(), company_id), country, routing, account)


def rotate(db: Session, principal, company_id: int, purpose: str = DEFAULT_PURPOSE) -> TenantDataKey:
    """Data-key rotation: a new ACTIVE version; the previous key becomes RETIRED and keeps decrypting."""
    _require_tenant(db, company_id)
    _check_purpose(purpose)
    _lock_tenant_keys(db, company_id)
    current = (
        db.query(TenantDataKey)
        .filter(TenantDataKey.company_id == company_id, TenantDataKey.purpose == purpose, TenantDataKey.status == ACTIVE)
        .with_for_update()
        .one_or_none()
    )
    latest = (
        db.query(TenantDataKey.version)
        .filter(TenantDataKey.company_id == company_id, TenantDataKey.purpose == purpose)
        .order_by(TenantDataKey.version.desc())
        .limit(1)
        .scalar()
    )
    now = clock.utcnow()
    if current is not None:
        current.status = RETIRED
        current.rotated_at = now
        db.flush()
    row = _new_key(db, company_id, purpose, (latest or 0) + 1)
    AuditLogService.log(
        db, principal, audit.PAYROLL_SETTINGS, "DATA_KEY_ROTATED",
        f"Data key for {purpose} rotated to version {row.version}", "tenant_data_key", row.id, company_id=company_id,
        actor_type="user" if principal is not None else "system",
        details={"purpose": purpose, "version": row.version,
                 "retired_version": current.version if current is not None else None},
    )
    return row


def rewrap_master(db: Session, company_id: int, old_master_key: bytes) -> int:
    """Master-key rotation for one tenant: re-wrap its keys from `old_master_key` to the current one (no data change)."""
    _require_tenant(db, company_id)
    old_id, new_id = master_key_id(old_master_key), master_key_id()
    if old_id == new_id:
        return 0
    rows = (
        db.query(TenantDataKey)
        .filter(TenantDataKey.company_id == company_id, TenantDataKey.kek_id == old_id)
        .with_for_update()
        .all()
    )
    for row in rows:
        row.wrapped_key = crypto.rewrap(row.wrapped_key, dek_key_id=row.key_id, old_master_key=old_master_key,
                                        new_master_key=master_key(), new_key_id=new_id)
        row.kek_id = new_id
        row.rotated_at = clock.utcnow()
        with _lock:
            _cache.pop((company_id, row.key_id), None)
    if rows:
        AuditLogService.log(
            db, None, audit.PAYROLL_SETTINGS, "DATA_KEYS_REWRAPPED", f"{len(rows)} data key(s) re-wrapped",
            "company", company_id, company_id=company_id, actor_type="system",
            details={"count": len(rows), "from_master": old_id, "to_master": new_id},
        )
    db.flush()
    return len(rows)
