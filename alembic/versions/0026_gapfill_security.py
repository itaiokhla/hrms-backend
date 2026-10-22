"""Gap-fill security: TOTP secrets encrypted at rest (F-28).

* ``tenant_data_keys.purpose`` accepts ``MFA_SECRET`` (one data key per tenant for TOTP secrets);
* ``users.mfa_secret`` widens from 64 to 512 characters to hold the envelope ciphertext
  ``"v1:<key id>:<nonce>:<ciphertext>"``;
* data: every plaintext secret is encrypted with the tenant's MFA_SECRET key when PAYROLL_MASTER_KEY is
  configured at migration time. Without it the rows stay plaintext, a warning is logged, and
  app.services.mfa_secrets encrypts each secret on its next use (lazy migration).

Downgrade decrypts the secrets back to plaintext (it needs the same master key and refuses otherwise,
rather than locking users out of two-factor sign-in), drops the MFA_SECRET data keys, narrows the
column and restores the purpose constraint.

Revision ID: 0026_gapfill_security
Revises: 0025_corrections
Create Date: 2026-10-21
"""
import logging
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0026_gapfill_security'
down_revision: Union[str, None] = '0025_corrections'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# Frozen copies: later model changes must not alter what this revision does.
PURPOSES_BEFORE = ('PAYROLL_PII',)
PURPOSES_AFTER = ('PAYROLL_PII', 'MFA_SECRET')
OLD_LENGTH, NEW_LENGTH = 64, 512


def _purpose_check(purposes) -> str:
    return "purpose IN (" + ", ".join(f"'{p}'" for p in purposes) + ")"


def _master_key_configured() -> bool:
    if (os.environ.get("PAYROLL_MASTER_KEY") or "").strip():
        return True
    from app.config import settings

    return bool((settings.payroll_master_key or "").strip())


def upgrade() -> None:
    op.drop_constraint('ck_tenant_data_keys_purpose', 'tenant_data_keys', type_='check')
    op.create_check_constraint('ck_tenant_data_keys_purpose', 'tenant_data_keys', _purpose_check(PURPOSES_AFTER))
    op.alter_column('users', 'mfa_secret', existing_type=sa.String(OLD_LENGTH), type_=sa.String(NEW_LENGTH),
                    existing_nullable=True)

    bind = op.get_bind()
    pending = bind.execute(sa.text("SELECT count(*) FROM users WHERE mfa_secret IS NOT NULL "
                                   "AND mfa_secret NOT LIKE 'v1:%'")).scalar()
    if not pending:
        return
    from app.services import mfa_secrets

    count = mfa_secrets.encrypt_legacy_rows(bind, master_key_configured=_master_key_configured())
    logger.info("0026: %d of %d TOTP secret(s) encrypted", count, pending)


def downgrade() -> None:
    bind = op.get_bind()
    encrypted = bind.execute(sa.text("SELECT count(*) FROM users WHERE mfa_secret LIKE 'v1:%'")).scalar()
    if encrypted:
        if not _master_key_configured():
            raise RuntimeError("0026 downgrade needs PAYROLL_MASTER_KEY to decrypt TOTP secrets back to plaintext")
        from app.services import mfa_secrets

        mfa_secrets.decrypt_all_rows(bind)
    too_long = bind.execute(sa.text(f"SELECT count(*) FROM users WHERE length(mfa_secret) > {OLD_LENGTH}")).scalar()
    if too_long:
        raise RuntimeError("0026 downgrade: some TOTP secrets do not fit the old column")
    bind.execute(sa.text("DELETE FROM tenant_data_keys WHERE purpose = 'MFA_SECRET'"))
    op.alter_column('users', 'mfa_secret', existing_type=sa.String(NEW_LENGTH), type_=sa.String(OLD_LENGTH),
                    existing_nullable=True)
    op.drop_constraint('ck_tenant_data_keys_purpose', 'tenant_data_keys', type_='check')
    op.create_check_constraint('ck_tenant_data_keys_purpose', 'tenant_data_keys', _purpose_check(PURPOSES_BEFORE))
