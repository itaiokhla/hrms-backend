"""Partner operator delegated access (Goal 2 section 15, 01-white-label partner model).

``partner_delegations``: a tenant administrator's explicit, time-boxed grant of read access to the
tenant's own partner (reseller or payroll bureau). Owning the commercial relationship never grants
access to HR data; only a current row here does. A delegation names the permission codes (read-only
codes only, never payroll money actions: see app.services.partner_delegation_service), an optional
narrowing to legal entities and pay groups, a start, an expiry of at most 30 days, the reason and the
tenant administrator who granted it (with step-up). It ends by expiry or by an explicit revocation that
records who revoked it and why; neither can be undone, a new grant is a new row.

``partner_delegation_targets``: the legal entities / pay groups a narrowed delegation is limited to, as
real composite same-company foreign keys (a delegation can never name another tenant's entity).

The database keeps what services alone cannot be trusted with: bounded reason, an expiry after the
start and at most 30 days later, a revocation always records who and when, a non-empty code list.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

DELEGATION_STATUSES = ("ACTIVE", "REVOKED")
TARGET_KINDS = ("LEGAL_ENTITY", "PAY_GROUP")
MAX_DELEGATION_DAYS = 30
REASON_MIN, REASON_MAX = 10, 500
TABLE, TARGETS = "partner_delegations", "partner_delegation_targets"


def _fk(table: str, columns, target: str, name: str, ondelete=None) -> ForeignKeyConstraint:
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    return ForeignKeyConstraint(local, [f"{target}.company_id", f"{target}.id"], name=f"fk_{table}_{name}",
                                ondelete=ondelete)


class PartnerDelegation(TenantMixin, BaseModel):
    """One tenant-granted delegation to the tenant's partner."""

    __tablename__ = TABLE

    partner_id = Column(Integer, ForeignKey("partners.id", ondelete="RESTRICT"), nullable=False, index=True)
    permission_codes = Column(JSONB, nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String(REASON_MAX), nullable=False)
    status = Column(String(10), nullable=False, default="ACTIVE")
    granted_by_user_id = Column(Integer, nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(Integer, nullable=True)
    revoke_reason = Column(String(REASON_MAX), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name=f"uq_{TABLE}_company_id_id"),
        _fk(TABLE, "granted_by_user_id", "users", "granted_by"),
        _fk(TABLE, "revoked_by_user_id", "users", "revoked_by"),
        CheckConstraint(f"status IN {DELEGATION_STATUSES}", name=f"ck_{TABLE}_status"),
        CheckConstraint("jsonb_typeof(permission_codes) = 'array' AND jsonb_array_length(permission_codes) > 0",
                        name=f"ck_{TABLE}_codes"),
        CheckConstraint(f"char_length(btrim(reason)) BETWEEN {REASON_MIN} AND {REASON_MAX}", name=f"ck_{TABLE}_reason"),
        CheckConstraint(f"expires_at > starts_at AND expires_at - starts_at <= interval '{MAX_DELEGATION_DAYS} days'",
                        name=f"ck_{TABLE}_window"),
        CheckConstraint("(status = 'REVOKED') = (revoked_at IS NOT NULL) AND (status = 'REVOKED') = "
                        "(revoked_by_user_id IS NOT NULL)", name=f"ck_{TABLE}_revoked"),
        Index(f"ix_{TABLE}_partner_status", "partner_id", "company_id", "status", "expires_at"),
    )


class PartnerDelegationTarget(TenantMixin, BaseModel):
    """A legal entity or pay group a narrowed delegation is limited to."""

    __tablename__ = TARGETS

    delegation_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(12), nullable=False)
    legal_entity_id = Column(Integer, nullable=True)
    pay_group_id = Column(Integer, nullable=True)

    __table_args__ = (
        _fk(TARGETS, "delegation_id", "partner_delegations", "delegation", ondelete="CASCADE"),
        _fk(TARGETS, "legal_entity_id", "legal_entities", "legal_entity"),
        _fk(TARGETS, "pay_group_id", "pay_groups", "pay_group"),
        CheckConstraint(f"kind IN {TARGET_KINDS}", name=f"ck_{TARGETS}_kind"),
        CheckConstraint(
            "(kind = 'LEGAL_ENTITY' AND legal_entity_id IS NOT NULL AND pay_group_id IS NULL) OR "
            "(kind = 'PAY_GROUP' AND pay_group_id IS NOT NULL AND legal_entity_id IS NULL)",
            name=f"ck_{TARGETS}_one_target",
        ),
        Index(f"uq_{TARGETS}_entity", "delegation_id", "legal_entity_id", unique=True,
              postgresql_where=text("kind = 'LEGAL_ENTITY'")),
        Index(f"uq_{TARGETS}_pay_group", "delegation_id", "pay_group_id", unique=True,
              postgresql_where=text("kind = 'PAY_GROUP'")),
    )
