"""TOTP secrets of company users, encrypted at rest (F-28, 3.7 §12, decision I-10).

``users.mfa_secret`` holds an envelope ``"v1:<key id>:<nonce>:<ciphertext>"`` sealed by
app.payroll.services.keys with the tenant's ``MFA_SECRET`` data key and bound to the user's row
(associated data ``users.mfa_secret:<user id>``), so a value copied to another user or tenant does not open.

Legacy rows still hold the plaintext base32 secret (base32 never contains ``:``, so the two forms cannot be
confused). Reading a legacy row returns it and re-stores it encrypted in the same transaction (lazy
migration); migration 0026 encrypts the rest when PAYROLL_MASTER_KEY is configured at migration time.

Platform administrators have no tenant, hence no tenant data key: their ``platform_admins.mfa_secret`` is
read as stored (open follow-up). Secrets and ciphertexts are never logged.

Every function needs a session bound to the user's company (keys refuses others).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.db import bind_tenant
from app.models.user import User
from app.payroll.banking import crypto
from app.payroll.banking.errors import CryptoError

logger = logging.getLogger(__name__)

PURPOSE = "MFA_SECRET"
ENVELOPE_PREFIX = crypto.VERSION + ":"


def is_encrypted(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(ENVELOPE_PREFIX)


def _aad(user_id: int) -> bytes:
    return f"users.mfa_secret:{int(user_id)}".encode("ascii")


def seal(db: Session, company_id: int, user_id: int, secret: str) -> str:
    from app.payroll.services import keys

    return keys.encrypt_field(db, company_id, secret, PURPOSE, aad=_aad(user_id))


def store(db: Session, user: User, secret: Optional[str]) -> None:
    """Set (encrypted) or clear the user's TOTP secret."""
    if secret is None:
        user.mfa_secret = None
        return
    if user.id is None:
        db.flush()
    user.mfa_secret = seal(db, user.company_id, user.id, secret)


def read(db: Session, account: Any) -> Optional[str]:
    """The plaintext TOTP secret of a user (or platform admin), or None when absent or unreadable.

    A legacy plaintext user secret is returned and re-stored encrypted (the caller commits); if that
    fails (no master key) the row stays as it is and the next use tries again.
    """
    value = getattr(account, "mfa_secret", None)
    if not value:
        return None
    if not isinstance(account, User):
        return value
    if is_encrypted(value):
        from app.payroll.services import keys

        try:
            return keys.decrypt_field(db, account.company_id, value, PURPOSE, aad=_aad(account.id))
        except (CryptoError, keys.MasterKeyError):
            logger.error("The TOTP secret of user %s cannot be decrypted (key missing or value altered)", account.id)
            return None
    try:
        with db.begin_nested():
            account.mfa_secret = seal(db, account.company_id, account.id, value)
            db.flush()
    except Exception as exc:  # noqa: BLE001 - never block a sign-in on the lazy upgrade
        account.mfa_secret = value
        logger.warning("The TOTP secret of user %s stays unencrypted until its next use (%s)", account.id,
                       type(exc).__name__)
    return value


def encrypt_legacy_rows(connection, *, master_key_configured: bool) -> int:
    """Encrypt every plaintext ``users.mfa_secret`` on `connection` (migration 0026); returns how many.

    Without a configured master key nothing changes: a warning is logged and read() encrypts each secret
    on its next use.
    """
    if not master_key_configured:
        logger.warning("PAYROLL_MASTER_KEY is not set: existing TOTP secrets stay unencrypted until each user's "
                       "next sign-in or step-up (lazy migration)")
        return 0
    companies = [cid for (cid,) in connection.execute(text(
        "SELECT DISTINCT company_id FROM users WHERE mfa_secret IS NOT NULL AND mfa_secret NOT LIKE :prefix "
        "ORDER BY company_id"), {"prefix": ENVELOPE_PREFIX + "%"})]
    count = 0
    for company_id in companies:
        db = Session(bind=connection)
        try:
            bind_tenant(db, company_id)
            rows = db.execute(text(
                "SELECT id, mfa_secret FROM users WHERE company_id = :c AND mfa_secret IS NOT NULL "
                "AND mfa_secret NOT LIKE :prefix ORDER BY id"), {"c": company_id, "prefix": ENVELOPE_PREFIX + "%"}).all()
            for user_id, secret in rows:
                db.execute(text("UPDATE users SET mfa_secret = :s WHERE company_id = :c AND id = :u"),
                           {"s": seal(db, company_id, user_id, secret), "c": company_id, "u": user_id})
                count += 1
            db.flush()
        finally:
            db.close()
    if count:
        logger.warning("Encrypted %d existing TOTP secret(s)", count)
    return count


def decrypt_all_rows(connection) -> int:
    """Downgrade helper: put every encrypted ``users.mfa_secret`` back in plaintext; returns how many."""
    companies = [cid for (cid,) in connection.execute(text(
        "SELECT DISTINCT company_id FROM users WHERE mfa_secret LIKE :prefix ORDER BY company_id"),
        {"prefix": ENVELOPE_PREFIX + "%"})]
    count = 0
    for company_id in companies:
        from app.payroll.services import keys

        db = Session(bind=connection)
        try:
            bind_tenant(db, company_id)
            rows = db.execute(text(
                "SELECT id, mfa_secret FROM users WHERE company_id = :c AND mfa_secret LIKE :prefix ORDER BY id"),
                {"c": company_id, "prefix": ENVELOPE_PREFIX + "%"}).all()
            for user_id, value in rows:
                plain = keys.decrypt_field(db, company_id, value, PURPOSE, aad=_aad(user_id))
                db.execute(text("UPDATE users SET mfa_secret = :s WHERE company_id = :c AND id = :u"),
                           {"s": plain, "c": company_id, "u": user_id})
                count += 1
            db.flush()
        finally:
            db.close()
    return count
