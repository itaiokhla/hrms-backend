"""White-label data: versioned brand profiles, content-addressed assets, tenant hosts,
login handoff codes, issued documents and email template overrides (01-white-label §11).

Tenant-owned rows (TENANT and LEGAL_ENTITY brands, tenant assets, domains, documents,
templates) use TenantMixin and row-level security like every other tenant table.
Platform and partner brands live in separate ``commercial_*`` tables that the tenant
application role cannot read; they are resolved through the system session.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.models.base import BaseModel, TenantMixin

BRAND_STATUSES = ("DRAFT", "PUBLISHED", "ARCHIVED")
TENANT_BRAND_OWNERS = ("TENANT", "LEGAL_ENTITY")
COMMERCIAL_BRAND_OWNERS = ("PLATFORM", "PARTNER")
SIDEBAR_STYLES = ("DARK", "LIGHT", "BRAND")
RADII = ("SHARP", "ROUNDED", "PILL")
FONTS = ("INTER", "POPPINS", "ROBOTO", "NOTO", "SYSTEM")
ASSET_KINDS = ("logo_light", "logo_dark", "mark", "email_logo", "document_logo", "login_image")
DOMAIN_KINDS = ("PLATFORM_SUBDOMAIN", "CUSTOM")
DOMAIN_STATUSES = ("PENDING_DNS", "VERIFYING", "ACTIVE", "DNS_BROKEN", "DISABLED", "FAILED", "REMOVED")
TLS_STATUSES = ("NONE", "PENDING", "ISSUED", "FAILED", "RENEWING")


class BrandFieldsMixin:
    """Every brandable field. NULL means "not set at this level" (inherit)."""

    version = Column(Integer, nullable=False)
    status = Column(String(10), nullable=False, default="DRAFT")

    display_name = Column(String(150), nullable=True)
    product_name = Column(String(40), nullable=True)
    short_name = Column(String(12), nullable=True)
    tagline = Column(String(80), nullable=True)
    legal_footer = Column(String(300), nullable=True)
    support_email = Column(String(255), nullable=True)
    support_phone = Column(String(20), nullable=True)
    support_url = Column(String(500), nullable=True)
    help_center_url = Column(String(500), nullable=True)
    terms_url = Column(String(500), nullable=True)
    privacy_url = Column(String(500), nullable=True)
    status_page_url = Column(String(500), nullable=True)
    default_locale = Column(String(35), nullable=True)
    primary_color = Column(String(7), nullable=True)
    accent_color = Column(String(7), nullable=True)
    sidebar_style = Column(String(10), nullable=True)
    radius = Column(String(10), nullable=True)
    font = Column(String(10), nullable=True)
    email_sender_name = Column(String(100), nullable=True)
    show_powered_by = Column(Boolean, nullable=True)
    # Asset references (ids in the matching assets table of the same owner).
    logo_light_asset_id = Column(Integer, nullable=True)
    logo_dark_asset_id = Column(Integer, nullable=True)
    mark_asset_id = Column(Integer, nullable=True)
    email_logo_asset_id = Column(Integer, nullable=True)
    document_logo_asset_id = Column(Integer, nullable=True)
    login_image_asset_id = Column(Integer, nullable=True)

    # Stored with each published version (01 §5.3 step 8).
    computed_tokens = Column(JSONB, nullable=True)
    contrast_report = Column(JSONB, nullable=True)
    change_summary = Column(JSONB, nullable=True)
    # Drafts: bumped on every save so concurrent editors and publishers can detect races.
    row_version = Column(Integer, nullable=False, default=1)
    idempotency_key = Column(String(80), nullable=True)
    restored_from_version = Column(Integer, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String(255), nullable=True)
    published_by = Column(String(255), nullable=True)


class BrandProfile(TenantMixin, BrandFieldsMixin, BaseModel):
    """A tenant's brand (owner TENANT) or a legal entity's document brand (owner LEGAL_ENTITY)."""

    __tablename__ = "brand_profiles"

    owner_type = Column(String(20), nullable=False)
    legal_entity_id = Column(Integer, nullable=True, index=True)
    created_by_user_id = Column(Integer, nullable=True)
    published_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_brand_profiles_company_id_id"),
        CheckConstraint(f"owner_type IN {TENANT_BRAND_OWNERS}", name="ck_brand_profiles_owner"),
        CheckConstraint(f"status IN {BRAND_STATUSES}", name="ck_brand_profiles_status"),
        CheckConstraint(
            "(owner_type = 'LEGAL_ENTITY') = (legal_entity_id IS NOT NULL)", name="ck_brand_profiles_entity"
        ),
        ForeignKeyConstraint(
            ["company_id", "legal_entity_id"],
            ["legal_entities.company_id", "legal_entities.id"],
            name="fk_brand_profiles_legal_entity",
        ),
        Index(
            "uq_brand_profiles_owner_version",
            "company_id", "owner_type", text("COALESCE(legal_entity_id, 0)"), "version",
            unique=True,
        ),
        Index(
            "uq_brand_profiles_one_published",
            "company_id", "owner_type", text("COALESCE(legal_entity_id, 0)"),
            unique=True, postgresql_where=text("status = 'PUBLISHED'"),
        ),
        Index(
            "uq_brand_profiles_one_draft",
            "company_id", "owner_type", text("COALESCE(legal_entity_id, 0)"),
            unique=True, postgresql_where=text("status = 'DRAFT'"),
        ),
    )


class CommercialBrandProfile(BrandFieldsMixin, BaseModel):
    """The platform brand (exactly one owner) or a partner's brand, with its field locks."""

    __tablename__ = "commercial_brand_profiles"

    owner_type = Column(String(20), nullable=False)
    partner_id = Column(Integer, ForeignKey("partners.id", ondelete="CASCADE"), nullable=True, index=True)
    # Partner level only: fields its tenants cannot override (01 §4).
    locked_fields = Column(ARRAY(String(40)), nullable=False, default=list)
    powered_by_label = Column(String(80), nullable=True)

    __table_args__ = (
        CheckConstraint(f"owner_type IN {COMMERCIAL_BRAND_OWNERS}", name="ck_commercial_brand_owner"),
        CheckConstraint(f"status IN {BRAND_STATUSES}", name="ck_commercial_brand_status"),
        CheckConstraint("(owner_type = 'PARTNER') = (partner_id IS NOT NULL)", name="ck_commercial_brand_partner"),
        Index(
            "uq_commercial_brand_owner_version",
            "owner_type", text("COALESCE(partner_id, 0)"), "version", unique=True,
        ),
        Index(
            "uq_commercial_brand_one_published",
            "owner_type", text("COALESCE(partner_id, 0)"),
            unique=True, postgresql_where=text("status = 'PUBLISHED'"),
        ),
        Index(
            "uq_commercial_brand_one_draft",
            "owner_type", text("COALESCE(partner_id, 0)"),
            unique=True, postgresql_where=text("status = 'DRAFT'"),
        ),
    )


class BrandAssetMixin:
    """One processed upload. Bytes live in content-addressed files (brand/<sha256>.<ext>)."""

    kind = Column(String(20), nullable=False)
    sha256 = Column(String(64), nullable=False, index=True)
    ext = Column(String(5), nullable=False)
    mime = Column(String(40), nullable=False)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    bytes = Column(Integer, nullable=False)
    original_filename = Column(String(255), nullable=True)
    # {"favicon-32": {"sha256": ..., "ext": "png", "width": 32, "height": 32}, ...}
    derivatives = Column(JSONB, nullable=False, default=dict)
    created_by = Column(String(255), nullable=True)


class BrandAsset(TenantMixin, BrandAssetMixin, BaseModel):
    __tablename__ = "brand_assets"

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_brand_assets_company_id_id"),
        CheckConstraint(f"kind IN {ASSET_KINDS}", name="ck_brand_assets_kind"),
    )


class CommercialBrandAsset(BrandAssetMixin, BaseModel):
    __tablename__ = "commercial_brand_assets"

    owner_type = Column(String(20), nullable=False)
    partner_id = Column(Integer, ForeignKey("partners.id", ondelete="CASCADE"), nullable=True, index=True)

    __table_args__ = (CheckConstraint(f"kind IN {ASSET_KINDS}", name="ck_commercial_brand_assets_kind"),)


class TenantDomain(TenantMixin, BaseModel):
    """A host that serves one tenant: its platform subdomain or a verified custom domain."""

    __tablename__ = "tenant_domains"

    hostname = Column(String(253), nullable=False)
    kind = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="PENDING_DNS")
    is_primary = Column(Boolean, nullable=False, default=False)
    redirect_to_primary = Column(Boolean, nullable=False, default=False)
    enforce_custom_domain = Column(Boolean, nullable=False, default=False)
    # Published in the tenant's DNS (TXT _hrms-challenge.<host>); proves control, not a secret.
    verification_token = Column(String(64), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(500), nullable=True)
    dns_broken_since = Column(DateTime(timezone=True), nullable=True)
    tls_status = Column(String(10), nullable=False, default="NONE")
    tls_expires_at = Column(DateTime(timezone=True), nullable=True)
    tls_failures = Column(Integer, nullable=False, default=0)
    status_history = Column(JSONB, nullable=False, default=list)
    removed_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_tenant_domains_company_id_id"),
        CheckConstraint(f"kind IN {DOMAIN_KINDS}", name="ck_tenant_domains_kind"),
        CheckConstraint(f"status IN {DOMAIN_STATUSES}", name="ck_tenant_domains_status"),
        CheckConstraint(f"tls_status IN {TLS_STATUSES}", name="ck_tenant_domains_tls_status"),
        CheckConstraint("hostname = lower(hostname)", name="ck_tenant_domains_hostname_lower"),
        # A hostname belongs to at most one tenant at a time (domain takeover guard).
        Index("uq_tenant_domains_hostname_live", "hostname", unique=True,
              postgresql_where=text("status <> 'REMOVED'")),
        Index("uq_tenant_domains_one_primary", "company_id", unique=True,
              postgresql_where=text("is_primary AND status <> 'REMOVED'")),
    )


class AuthHandoffCode(BaseModel):
    """Single-use, 60-second code that moves a fresh sign-in to the tenant's primary host.

    Read and written only through the system session.
    """

    __tablename__ = "auth_handoff_codes"

    code_hash = Column(String(64), nullable=False, unique=True)
    user_id = Column(Integer, nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    target_host = Column(String(253), nullable=False)
    ua_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)


class IssuedDocument(TenantMixin, BaseModel):
    """An immutable generated document (payslip). Reprints return the stored bytes."""

    __tablename__ = "issued_documents"

    document_type = Column(String(30), nullable=False)
    source_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=True, index=True)
    legal_entity_id = Column(Integer, nullable=True)
    brand_profile_id = Column(Integer, nullable=True)
    entity_brand_profile_id = Column(Integer, nullable=True)
    brand_version = Column(String(64), nullable=True)
    template_version = Column(String(20), nullable=False)
    # Everything the renderer needs (brand, employer, figures), frozen at issue time.
    snapshot = Column(JSONB, nullable=False)
    sha256 = Column(String(64), nullable=True)
    storage_path = Column(String(255), nullable=True)
    issued_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "document_type", "source_id", name="uq_issued_documents_source"),
    )


class EmailTemplateOverride(TenantMixin, BaseModel):
    """Tenant wording for one transactional template and locale (layout is never editable)."""

    __tablename__ = "email_template_overrides"

    template_key = Column(String(60), nullable=False)
    locale = Column(String(35), nullable=False, default="en")
    subject = Column(String(200), nullable=True)
    intro = Column(Text, nullable=True)
    signature = Column(String(500), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    updated_by_user_id = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "template_key", "locale", name="uq_email_template_overrides_key"),
    )
