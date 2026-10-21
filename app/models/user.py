from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, TenantMixin

USER_STATUSES = ("invited", "active", "suspended", "deactivated")


class User(TenantMixin, BaseModel):
    """A company-scoped login account. Belongs to exactly one company."""

    __tablename__ = "users"

    # Login identifier. Globally unique so login never needs a company selector.
    email = Column(String(255), nullable=False, unique=True)
    full_name = Column(String(150), nullable=False)
    password_hash = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="active", index=True)
    mfa_secret = Column(String(64), nullable=True)
    mfa_enabled = Column(Boolean, nullable=False, default=False)
    # Last TOTP time step accepted; a code is never accepted twice (RFC 6238 section 5.2).
    mfa_last_step = Column(Integer, nullable=True)
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    password_changed_at = Column(DateTime(timezone=True), nullable=True)
    # Bumped whenever role assignments change so clients can refresh capabilities.
    permission_version = Column(Integer, nullable=False, default=1)

    company = relationship("Company")
    employee = relationship(
        "Employee",
        primaryjoin="User.id == Employee.user_id",
        foreign_keys="Employee.user_id",
        back_populates="user",
        uselist=False,
    )
    role_assignments = relationship(
        "UserRoleAssignment",
        primaryjoin="User.id == UserRoleAssignment.user_id",
        foreign_keys="UserRoleAssignment.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_users_company_id_id"),
        CheckConstraint(f"status IN {USER_STATUSES}", name="ck_users_status"),
    )

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class UserSession(TenantMixin, BaseModel):
    """Server-side session backing a refresh token. Revocation is immediate."""

    __tablename__ = "user_sessions"

    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
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
    # Host the session was created on (sessions never move between hosts; removing a
    # domain revokes its sessions). NULL for sessions created before hosts were tracked.
    host = Column(String(253), nullable=True, index=True)
    # Last successful step-up re-authentication of this session and how it was proven (3.7 §3.2).
    step_up_at = Column(DateTime(timezone=True), nullable=True)
    step_up_method = Column(String(20), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["users.company_id", "users.id"],
            ondelete="CASCADE",
            name="fk_user_sessions_user",
        ),
        CheckConstraint(
            "step_up_method IS NULL OR step_up_method IN ('TOTP', 'EMAIL_OTP')", name="ck_user_sessions_step_up_method"
        ),
    )


class UserInvitation(TenantMixin, BaseModel):
    __tablename__ = "user_invitations"

    user_id = Column(Integer, nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    invited_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["users.company_id", "users.id"],
            ondelete="CASCADE",
            name="fk_user_invitations_user",
        ),
    )


class PasswordResetOTP(TenantMixin, BaseModel):
    __tablename__ = "password_reset_otps"

    user_id = Column(Integer, nullable=False, index=True)
    otp_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["users.company_id", "users.id"],
            ondelete="CASCADE",
            name="fk_password_reset_otps_user",
        ),
    )
