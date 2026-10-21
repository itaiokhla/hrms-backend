"""Settings engine v2, policies and payroll tables (3.1 §2-§3, 3.9 §3.1).

Setting values are effective-dated rows per scope (a key that is not in the settings registry is
never stored); platform and partner defaults live in platform_setting_values, which the tenant
application role may only read. Overlap rules are triggers, because this PostgreSQL build has no
btree_gist for exclusion constraints (migration 0009): ACTIVE values of one key and scope, values
of one platform/partner key, ACTIVE versions of one policy and ACTIVE versions of one payroll table
never overlap.
"""

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

SETTING_SCOPE_TYPES = ("TENANT", "LEGAL_ENTITY", "BRANCH", "PAY_GROUP", "EMPLOYEE")
SETTING_STATUSES = ("PENDING_APPROVAL", "ACTIVE", "SUPERSEDED", "REJECTED")
# Where a value came from: profiles are materialized TENANT rows (decisions I-13, I-14, ARCHITECTURE §9).
SETTING_SOURCES = ("STARTER_PROFILE", "LEGACY_MIGRATION", "ADMIN", "PACK_LOCK", "PARTNER_PROVISIONING")
PROFILE_SOURCES = ("STARTER_PROFILE", "LEGACY_MIGRATION")
SETTING_DECISIONS = ("APPROVED", "REJECTED")
PLATFORM_SETTING_LEVELS = ("PLATFORM", "PARTNER")
POLICY_STATUSES = ("ACTIVE", "ARCHIVED")
POLICY_VERSION_STATUSES = ("DRAFT", "ACTIVE", "SUPERSEDED")
POLICY_ASSIGNMENT_STATUSES = ("ACTIVE", "INACTIVE")
PAYROLL_TABLE_KINDS = ("SLAB", "BAND", "LOOKUP")
PAYROLL_TABLE_OWNERS = ("COMPANY", "PACK")
PAYROLL_TABLE_STATUSES = ("DRAFT", "ACTIVE", "ARCHIVED")


class SettingValue(TenantMixin, BaseModel):
    """One value of a registry key at one scope, from effective_from to effective_to (inclusive).

    scope_id is the legal entity, branch, pay group or employee id (NULL for TENANT). Rows are never
    deleted: a new effective date closes the previous row. A maker-checker key is saved as
    PENDING_APPROVAL and the old value stays in force until another user approves it (3.1 §2.3, §2.5).
    created_by_user_id is NULL for values written by migrations or provisioning; rows of a
    configuration profile carry its profile_version (e.g. STARTER_V1, LEGACY_V1).
    """

    __tablename__ = "setting_values"

    key = Column(String(120), nullable=False)
    scope_type = Column(String(20), nullable=False)
    scope_id = Column(Integer, nullable=True)
    value = Column(JSONB, nullable=False)
    locked = Column(Boolean, nullable=False, default=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    status = Column(String(20), nullable=False)
    reason = Column(String(500), nullable=True)
    row_version = Column(Integer, nullable=False, default=1)
    created_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    source = Column(String(30), nullable=False, default="ADMIN")
    profile_version = Column(String(20), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_setting_values_company_id_id"),
        CheckConstraint(f"scope_type IN {SETTING_SCOPE_TYPES}", name="ck_setting_values_scope_type"),
        CheckConstraint("(scope_type = 'TENANT') = (scope_id IS NULL)", name="ck_setting_values_scope_id"),
        CheckConstraint(f"status IN {SETTING_STATUSES}", name="ck_setting_values_status"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_setting_values_window"),
        CheckConstraint("row_version >= 1", name="ck_setting_values_row_version"),
        CheckConstraint(f"source IN {SETTING_SOURCES}", name="ck_setting_values_source"),
        CheckConstraint(
            f"source NOT IN {PROFILE_SOURCES} OR profile_version IS NOT NULL", name="ck_setting_values_profile_version"
        ),
        CheckConstraint(
            "approved_by_user_id IS NULL OR created_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id",
            name="ck_setting_values_maker_checker",
        ),
        Index("ix_setting_values_lookup", "company_id", "key", "scope_type", "scope_id", "effective_from"),
    )


class SettingChangeRequest(TenantMixin, BaseModel):
    """Maker-checker decision on a PENDING_APPROVAL setting value; the maker never approves (EC-CFG-12)."""

    __tablename__ = "setting_change_requests"

    setting_value_id = Column(Integer, nullable=False, index=True)
    requested_by_user_id = Column(Integer, nullable=False)
    decided_by_user_id = Column(Integer, nullable=True)
    decision = Column(String(10), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    comment = Column(String(500), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_setting_change_requests_company_id_id"),
        ForeignKeyConstraint(
            ["company_id", "setting_value_id"], ["setting_values.company_id", "setting_values.id"],
            ondelete="CASCADE", name="fk_setting_change_requests_value",
        ),
        CheckConstraint(f"decision IN {SETTING_DECISIONS}", name="ck_setting_change_requests_decision"),
        CheckConstraint("(decision IS NULL) = (decided_at IS NULL)", name="ck_setting_change_requests_decided"),
        CheckConstraint(
            "decision IS DISTINCT FROM 'APPROVED' "
            "OR (decided_by_user_id IS NOT NULL AND decided_by_user_id <> requested_by_user_id)",
            name="ck_setting_change_requests_maker_checker",
        ),
        Index("uq_setting_change_requests_open", "setting_value_id", unique=True,
              postgresql_where=text("decision IS NULL")),
    )


class PlatformSettingValue(BaseModel):
    """A platform default or a partner's default for a registry key (02 §14.1, levels PLATFORM and PARTNER).

    Platform-owned: written by operators through the system session, read-only for the tenant
    application role. Values of one (level, partner, key) never overlap.
    """

    __tablename__ = "platform_setting_values"

    level = Column(String(10), nullable=False)
    partner_id = Column(Integer, ForeignKey("partners.id", ondelete="CASCADE"), nullable=True, index=True)
    key = Column(String(120), nullable=False)
    value = Column(JSONB, nullable=False)
    locked = Column(Boolean, nullable=False, default=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_by = Column(String(255), nullable=False)

    __table_args__ = (
        CheckConstraint(f"level IN {PLATFORM_SETTING_LEVELS}", name="ck_platform_setting_values_level"),
        CheckConstraint("(level = 'PARTNER') = (partner_id IS NOT NULL)", name="ck_platform_setting_values_partner"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="ck_platform_setting_values_window"
        ),
        Index("ix_platform_setting_values_lookup", "key", "level", "partner_id", "effective_from"),
    )


class Policy(TenantMixin, BaseModel):
    """A named policy of one type (3.1 §3.2). A policy used by a run is archived, never deleted (EC-CFG-16)."""

    __tablename__ = "policies"

    type = Column(String(40), nullable=False)
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_policies_company_id_id"),
        UniqueConstraint("company_id", "type", "name", name="uq_policies_company_type_name"),
        CheckConstraint(f"status IN {POLICY_STATUSES}", name="ck_policies_status"),
    )


class PolicyVersion(TenantMixin, BaseModel):
    """Field values of a policy from effective_from (3.1 §3.2).

    Drafts are edited in place. Once activated a version is immutable (trigger): only effective_to
    and ACTIVE -> SUPERSEDED may change. ACTIVE versions of one policy never overlap.
    """

    __tablename__ = "policy_versions"

    policy_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    # Validated by the policy type's schema (app.payroll.policy_types).
    fields = Column(JSONB, nullable=False, default=dict)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    status = Column(String(12), nullable=False, default="DRAFT")
    # SHA-256 (hex) of the canonical field JSON, set on activation and frozen into run snapshots.
    policy_hash = Column(CHAR(64), nullable=True)
    activated_by_user_id = Column(Integer, nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_policy_versions_company_id_id"),
        UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),
        ForeignKeyConstraint(
            ["company_id", "policy_id"], ["policies.company_id", "policies.id"], name="fk_policy_versions_policy"
        ),
        CheckConstraint(f"status IN {POLICY_VERSION_STATUSES}", name="ck_policy_versions_status"),
        CheckConstraint("version >= 1", name="ck_policy_versions_version"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_policy_versions_window"),
        CheckConstraint("jsonb_typeof(fields) = 'object'", name="ck_policy_versions_fields"),
        CheckConstraint("policy_hash IS NULL OR policy_hash ~ '^[0-9a-f]{64}$'", name="ck_policy_versions_hash"),
        CheckConstraint(
            "status = 'DRAFT' OR (policy_hash IS NOT NULL AND activated_at IS NOT NULL)",
            name="ck_policy_versions_activation",
        ),
    )


class PolicyAssignment(TenantMixin, BaseModel):
    """Eligibility rule of a policy: criteria (empty = everyone, the catch-all) and priority (3.1 §3.2).

    Two rules with the same priority matching one employee are rejected by the service (EC-CFG-05).
    """

    __tablename__ = "policy_assignments"

    policy_id = Column(Integer, nullable=False, index=True)
    criteria = Column(JSONB, nullable=False, default=dict)
    priority = Column(Integer, nullable=False, default=0)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    status = Column(String(10), nullable=False, default="ACTIVE")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_policy_assignments_company_id_id"),
        ForeignKeyConstraint(
            ["company_id", "policy_id"], ["policies.company_id", "policies.id"],
            ondelete="CASCADE", name="fk_policy_assignments_policy",
        ),
        CheckConstraint(f"status IN {POLICY_ASSIGNMENT_STATUSES}", name="ck_policy_assignments_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="ck_policy_assignments_window"
        ),
        CheckConstraint("jsonb_typeof(criteria) = 'object'", name="ck_policy_assignments_criteria"),
    )


class PayrollTable(TenantMixin, BaseModel):
    """One effective-dated version of a SLAB, BAND or LOOKUP table read by formulas (3.3 §4).

    Amounts are in `currency` (NULL for tables of rates or plain numbers). ACTIVE versions of one
    code never overlap, so a formula resolves exactly one table on any date.
    """

    __tablename__ = "payroll_tables"

    code = Column(String(60), nullable=False)
    name = Column(String(150), nullable=False)
    kind = Column(String(10), nullable=False)
    currency = Column(String(3), nullable=True)
    owner = Column(String(10), nullable=False, default="COMPANY")
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    status = Column(String(10), nullable=False, default="DRAFT")

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_payroll_tables_company_id_id"),
        UniqueConstraint("company_id", "code", "effective_from", name="uq_payroll_tables_code_from"),
        CheckConstraint(f"kind IN {PAYROLL_TABLE_KINDS}", name="ck_payroll_tables_kind"),
        CheckConstraint(f"owner IN {PAYROLL_TABLE_OWNERS}", name="ck_payroll_tables_owner"),
        CheckConstraint(f"status IN {PAYROLL_TABLE_STATUSES}", name="ck_payroll_tables_status"),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="ck_payroll_tables_currency"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_payroll_tables_window"),
    )


class PayrollTableRow(TenantMixin, BaseModel):
    """A band [lower, upper] or a LOOKUP key with its value and/or rate."""

    __tablename__ = "payroll_table_rows"

    table_id = Column(Integer, nullable=False, index=True)
    lower = Column(Numeric(19, 4), nullable=True)
    upper = Column(Numeric(19, 4), nullable=True)
    key = Column(String(100), nullable=True)
    value = Column(Numeric(19, 4), nullable=True)
    rate = Column(Numeric(12, 6), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_payroll_table_rows_company_id_id"),
        ForeignKeyConstraint(
            ["company_id", "table_id"], ["payroll_tables.company_id", "payroll_tables.id"],
            ondelete="CASCADE", name="fk_payroll_table_rows_table",
        ),
        CheckConstraint("lower IS NULL OR upper IS NULL OR upper >= lower", name="ck_payroll_table_rows_bounds"),
        CheckConstraint("value IS NOT NULL OR rate IS NOT NULL", name="ck_payroll_table_rows_result"),
        Index("uq_payroll_table_rows_key", "table_id", "key", unique=True, postgresql_where=text("key IS NOT NULL")),
    )
