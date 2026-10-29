"""Per-tenant data keys for envelope encryption and step-up challenges (3.7 §3.2, §12; decision I-10)."""

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text

from app.models.base import BaseModel, TenantMixin

DATA_KEY_STATUSES = ("ACTIVE", "RETIRED")
DATA_KEY_PURPOSES = ("PAYROLL_PII", "MFA_SECRET", "ATTENDANCE_BIOMETRIC", "DEVICE_SECRET")  # DEVICE_SECRET: 0055  # face encodings: as_m2_sites_consents


class TenantDataKey(TenantMixin, BaseModel):
    """A tenant's AES-256 data key, stored only wrapped by the platform master key.

    key_id is the id every ciphertext names, so a RETIRED key still decrypts older values while the
    single ACTIVE key of a purpose encrypts new ones. kek_id identifies the master key that wrapped it
    (a digest, never the key), which lets master-key rotation re-wrap only what it must.
    """

    __tablename__ = "tenant_data_keys"

    purpose = Column(String(40), nullable=False)
    key_id = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)
    kek_id = Column(String(64), nullable=False)
    wrapped_key = Column(Text, nullable=False)
    algorithm = Column(String(20), nullable=False, default="AES-256-GCM")
    status = Column(String(10), nullable=False, default="ACTIVE")
    rotated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_tenant_data_keys_company_id_id"),
        UniqueConstraint("company_id", "key_id", name="uq_tenant_data_keys_key_id"),
        UniqueConstraint("company_id", "purpose", "version", name="uq_tenant_data_keys_version"),
        Index(
            "uq_tenant_data_keys_active", "company_id", "purpose", unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        CheckConstraint(f"status IN {DATA_KEY_STATUSES}", name="ck_tenant_data_keys_status"),
        CheckConstraint(f"purpose IN {DATA_KEY_PURPOSES}", name="ck_tenant_data_keys_purpose"),
        CheckConstraint("version >= 1", name="ck_tenant_data_keys_version"),
        CheckConstraint("algorithm = 'AES-256-GCM'", name="ck_tenant_data_keys_algorithm"),
    )


class StepUpChallenge(TenantMixin, BaseModel):
    """An emailed one-time step-up code for a user without MFA, bound to one session (3.7 §3.2)."""

    __tablename__ = "step_up_challenges"

    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String(36), nullable=False)
    code_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_step_up_challenges_company_id_id"),
        ForeignKeyConstraint(
            ["company_id", "user_id"], ["users.company_id", "users.id"],
            ondelete="CASCADE", name="fk_step_up_challenges_user",
        ),
        CheckConstraint("attempts >= 0", name="ck_step_up_challenges_attempts"),
        Index("ix_step_up_challenges_session", "company_id", "session_id",
              postgresql_where=text("consumed_at IS NULL")),
    )
