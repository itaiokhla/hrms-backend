from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.models.base import BaseModel

COMPANY_STATUSES = ("active", "suspended", "closed")
SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "canceled")


class Company(BaseModel):
    """A tenant. Every company-owned row references exactly one company."""

    __tablename__ = "companies"

    name = Column(String(150), nullable=False)
    slug = Column(String(80), nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="active")
    subscription_status = Column(String(20), nullable=False, default="trialing")
    subscription_plan = Column(String(40), nullable=False, default="trial")
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    timezone = Column(String(64), nullable=False, default="Asia/Kolkata")
    currency = Column(String(3), nullable=False, default="INR")

    __table_args__ = (
        CheckConstraint(f"status IN {COMPANY_STATUSES}", name="ck_companies_status"),
        CheckConstraint(
            f"subscription_status IN {SUBSCRIPTION_STATUSES}",
            name="ck_companies_subscription_status",
        ),
    )


class PlatformAdmin(BaseModel):
    """Operator of the SaaS platform.

    Deliberately a separate principal from company users: platform admins
    manage tenants and subscriptions and have no route into tenant HR data.
    """

    __tablename__ = "platform_admins"

    email = Column(String(255), nullable=False, unique=True)
    full_name = Column(String(150), nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    mfa_secret = Column(String(64), nullable=True)
    mfa_enabled = Column(Boolean, nullable=False, default=False)
    # Last TOTP time step accepted; a code is never accepted twice (RFC 6238 section 5.2).
    mfa_last_step = Column(Integer, nullable=True)
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    sessions = relationship("PlatformSession", back_populates="admin")


class PlatformSession(BaseModel):
    __tablename__ = "platform_sessions"

    id = Column(String(36), primary_key=True)
    admin_id = Column(Integer, ForeignKey("platform_admins.id", ondelete="CASCADE"), nullable=False, index=True)
    refresh_token_hash = Column(String(64), nullable=False)
    # Previous token stays valid briefly so concurrent refreshes (two tabs)
    # are not mistaken for token theft; any later reuse revokes the session.
    previous_refresh_token_hash = Column(String(64), nullable=True)
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoke_reason = Column(String(50), nullable=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)

    admin = relationship("PlatformAdmin", back_populates="sessions")
