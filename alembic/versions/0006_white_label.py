"""White label: brand profiles, assets, tenant hosts, handoff codes, issued documents.

Additive and backwards compatible (01-white-label §11):
  * commercial_brand_profiles / commercial_brand_assets hold the platform and partner
    brands; they are not granted to the tenant application role;
  * brand_profiles, brand_assets, tenant_domains, issued_documents and
    email_template_overrides are tenant tables with row-level security;
  * auth_handoff_codes is read and written only by the system role;
  * user_sessions.host records the host a session was created on (NULL for older
    sessions, which keep working on the platform host).

Backfill:
  * one PUBLISHED platform brand (text only; colours stay the stylesheet defaults);
  * one PUBLISHED brand (version 1) per company, with the company's existing logo
    re-processed into a logo asset when the file can be read. Companies keep their
    current look, and GET /api/v1/settings/logo keeps working;
  * HR Administrators may edit email wording and see/manage branches (frozen copy of
    registry 2026.10.3).

Tenant platform subdomains depend on PLATFORM_BASE_DOMAIN, so they are created by
`python -m app.cli sync-tenant-hosts` (and at startup), not here.

Revision ID: 0006_white_label
Revises: 0005_org_structure
Create Date: 2026-10-20
"""
import logging
import os
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0006_white_label'
down_revision: Union[str, None] = '0005_org_structure'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.0006")
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TENANT_TABLES = ("brand_profiles", "brand_assets", "tenant_domains", "issued_documents", "email_template_overrides")
HR_ADMIN_GRANTS = [("email_template:manage", "COMPANY"), ("org:read", "COMPANY"), ("org:manage_branches", "COMPANY")]
ASSET_KINDS = "('logo_light', 'logo_dark', 'mark', 'email_logo', 'document_logo', 'login_image')"


def _base():
    return [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _brand_fields():
    return [
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False),
        sa.Column('display_name', sa.String(150)), sa.Column('product_name', sa.String(40)),
        sa.Column('short_name', sa.String(12)), sa.Column('tagline', sa.String(80)),
        sa.Column('legal_footer', sa.String(300)), sa.Column('support_email', sa.String(255)),
        sa.Column('support_phone', sa.String(20)), sa.Column('support_url', sa.String(500)),
        sa.Column('help_center_url', sa.String(500)), sa.Column('terms_url', sa.String(500)),
        sa.Column('privacy_url', sa.String(500)), sa.Column('status_page_url', sa.String(500)),
        sa.Column('default_locale', sa.String(35)), sa.Column('primary_color', sa.String(7)),
        sa.Column('accent_color', sa.String(7)), sa.Column('sidebar_style', sa.String(10)),
        sa.Column('radius', sa.String(10)), sa.Column('font', sa.String(10)),
        sa.Column('email_sender_name', sa.String(100)), sa.Column('show_powered_by', sa.Boolean()),
        sa.Column('logo_light_asset_id', sa.Integer()), sa.Column('logo_dark_asset_id', sa.Integer()),
        sa.Column('mark_asset_id', sa.Integer()), sa.Column('email_logo_asset_id', sa.Integer()),
        sa.Column('document_logo_asset_id', sa.Integer()), sa.Column('login_image_asset_id', sa.Integer()),
        sa.Column('computed_tokens', postgresql.JSONB()), sa.Column('contrast_report', postgresql.JSONB()),
        sa.Column('change_summary', postgresql.JSONB()),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('idempotency_key', sa.String(80)), sa.Column('restored_from_version', sa.Integer()),
        sa.Column('published_at', sa.DateTime(timezone=True)),
        sa.Column('created_by', sa.String(255)), sa.Column('published_by', sa.String(255)),
    ]


def _asset_fields():
    return [
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('ext', sa.String(5), nullable=False),
        sa.Column('mime', sa.String(40), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False),
        sa.Column('original_filename', sa.String(255)),
        sa.Column('derivatives', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_by', sa.String(255)),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    owner_key = sa.text("COALESCE(partner_id, 0)")

    op.create_table(
        'commercial_brand_profiles',
        *_brand_fields(),
        sa.Column('owner_type', sa.String(20), nullable=False),
        sa.Column('partner_id', sa.Integer(), sa.ForeignKey('partners.id', ondelete='CASCADE')),
        sa.Column('locked_fields', postgresql.ARRAY(sa.String(40)), nullable=False, server_default='{}'),
        sa.Column('powered_by_label', sa.String(80)),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("owner_type IN ('PLATFORM', 'PARTNER')", name='ck_commercial_brand_owner'),
        sa.CheckConstraint("status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')", name='ck_commercial_brand_status'),
        sa.CheckConstraint("(owner_type = 'PARTNER') = (partner_id IS NOT NULL)", name='ck_commercial_brand_partner'),
    )
    op.create_index('ix_commercial_brand_profiles_partner_id', 'commercial_brand_profiles', ['partner_id'])
    op.create_index('uq_commercial_brand_owner_version', 'commercial_brand_profiles',
                    ['owner_type', owner_key, 'version'], unique=True)
    op.create_index('uq_commercial_brand_one_published', 'commercial_brand_profiles', ['owner_type', owner_key],
                    unique=True, postgresql_where=sa.text("status = 'PUBLISHED'"))
    op.create_index('uq_commercial_brand_one_draft', 'commercial_brand_profiles', ['owner_type', owner_key],
                    unique=True, postgresql_where=sa.text("status = 'DRAFT'"))

    op.create_table(
        'commercial_brand_assets',
        *_asset_fields(),
        sa.Column('owner_type', sa.String(20), nullable=False),
        sa.Column('partner_id', sa.Integer(), sa.ForeignKey('partners.id', ondelete='CASCADE')),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(f"kind IN {ASSET_KINDS}", name='ck_commercial_brand_assets_kind'),
    )
    op.create_index('ix_commercial_brand_assets_sha256', 'commercial_brand_assets', ['sha256'])
    op.create_index('ix_commercial_brand_assets_partner_id', 'commercial_brand_assets', ['partner_id'])

    entity_key = sa.text("COALESCE(legal_entity_id, 0)")
    op.create_table(
        'brand_profiles',
        *_brand_fields(),
        sa.Column('owner_type', sa.String(20), nullable=False),
        sa.Column('legal_entity_id', sa.Integer()),
        sa.Column('created_by_user_id', sa.Integer()),
        sa.Column('published_by_user_id', sa.Integer()),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['company_id', 'legal_entity_id'], ['legal_entities.company_id', 'legal_entities.id'],
                                name='fk_brand_profiles_legal_entity'),
        sa.UniqueConstraint('company_id', 'id', name='uq_brand_profiles_company_id_id'),
        sa.CheckConstraint("owner_type IN ('TENANT', 'LEGAL_ENTITY')", name='ck_brand_profiles_owner'),
        sa.CheckConstraint("status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')", name='ck_brand_profiles_status'),
        sa.CheckConstraint("(owner_type = 'LEGAL_ENTITY') = (legal_entity_id IS NOT NULL)", name='ck_brand_profiles_entity'),
    )
    op.create_index('ix_brand_profiles_company_id', 'brand_profiles', ['company_id'])
    op.create_index('ix_brand_profiles_legal_entity_id', 'brand_profiles', ['legal_entity_id'])
    op.create_index('uq_brand_profiles_owner_version', 'brand_profiles',
                    ['company_id', 'owner_type', entity_key, 'version'], unique=True)
    op.create_index('uq_brand_profiles_one_published', 'brand_profiles', ['company_id', 'owner_type', entity_key],
                    unique=True, postgresql_where=sa.text("status = 'PUBLISHED'"))
    op.create_index('uq_brand_profiles_one_draft', 'brand_profiles', ['company_id', 'owner_type', entity_key],
                    unique=True, postgresql_where=sa.text("status = 'DRAFT'"))

    op.create_table(
        'brand_assets',
        *_asset_fields(),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name='uq_brand_assets_company_id_id'),
        sa.CheckConstraint(f"kind IN {ASSET_KINDS}", name='ck_brand_assets_kind'),
    )
    op.create_index('ix_brand_assets_company_id', 'brand_assets', ['company_id'])
    op.create_index('ix_brand_assets_sha256', 'brand_assets', ['sha256'])

    op.create_table(
        'tenant_domains',
        sa.Column('hostname', sa.String(253), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('redirect_to_primary', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('enforce_custom_domain', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('verification_token', sa.String(64)),
        sa.Column('verified_at', sa.DateTime(timezone=True)),
        sa.Column('last_checked_at', sa.DateTime(timezone=True)),
        sa.Column('last_error', sa.String(500)),
        sa.Column('dns_broken_since', sa.DateTime(timezone=True)),
        sa.Column('tls_status', sa.String(10), nullable=False, server_default='NONE'),
        sa.Column('tls_expires_at', sa.DateTime(timezone=True)),
        sa.Column('tls_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status_history', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('removed_at', sa.DateTime(timezone=True)),
        sa.Column('created_by_user_id', sa.Integer()),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name='uq_tenant_domains_company_id_id'),
        sa.CheckConstraint("kind IN ('PLATFORM_SUBDOMAIN', 'CUSTOM')", name='ck_tenant_domains_kind'),
        sa.CheckConstraint(
            "status IN ('PENDING_DNS', 'VERIFYING', 'ACTIVE', 'DNS_BROKEN', 'DISABLED', 'FAILED', 'REMOVED')",
            name='ck_tenant_domains_status'),
        sa.CheckConstraint("tls_status IN ('NONE', 'PENDING', 'ISSUED', 'FAILED', 'RENEWING')",
                           name='ck_tenant_domains_tls_status'),
        sa.CheckConstraint("hostname = lower(hostname)", name='ck_tenant_domains_hostname_lower'),
    )
    op.create_index('ix_tenant_domains_company_id', 'tenant_domains', ['company_id'])
    op.create_index('uq_tenant_domains_hostname_live', 'tenant_domains', ['hostname'], unique=True,
                    postgresql_where=sa.text("status <> 'REMOVED'"))
    op.create_index('uq_tenant_domains_one_primary', 'tenant_domains', ['company_id'], unique=True,
                    postgresql_where=sa.text("is_primary AND status <> 'REMOVED'"))

    op.create_table(
        'auth_handoff_codes',
        sa.Column('code_hash', sa.String(64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='CASCADE'), nullable=False),
        sa.Column('target_host', sa.String(253), nullable=False),
        sa.Column('ua_hash', sa.String(64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True)),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code_hash'),
    )
    op.create_index('ix_auth_handoff_codes_company_id', 'auth_handoff_codes', ['company_id'])

    op.create_table(
        'issued_documents',
        sa.Column('document_type', sa.String(30), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer()),
        sa.Column('legal_entity_id', sa.Integer()),
        sa.Column('brand_profile_id', sa.Integer()),
        sa.Column('entity_brand_profile_id', sa.Integer()),
        sa.Column('brand_version', sa.String(64)),
        sa.Column('template_version', sa.String(20), nullable=False),
        sa.Column('snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('sha256', sa.String(64)),
        sa.Column('storage_path', sa.String(255)),
        sa.Column('issued_by_user_id', sa.Integer()),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'document_type', 'source_id', name='uq_issued_documents_source'),
    )
    op.create_index('ix_issued_documents_company_id', 'issued_documents', ['company_id'])
    op.create_index('ix_issued_documents_employee_id', 'issued_documents', ['employee_id'])

    op.create_table(
        'email_template_overrides',
        sa.Column('template_key', sa.String(60), nullable=False),
        sa.Column('locale', sa.String(35), nullable=False),
        sa.Column('subject', sa.String(200)),
        sa.Column('intro', sa.Text()),
        sa.Column('signature', sa.String(500)),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('updated_by_user_id', sa.Integer()),
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'template_key', 'locale', name='uq_email_template_overrides_key'),
    )
    op.create_index('ix_email_template_overrides_company_id', 'email_template_overrides', ['company_id'])

    op.add_column('user_sessions', sa.Column('host', sa.String(253), nullable=True))
    op.create_index('ix_user_sessions_host', 'user_sessions', ['host'])

    # Issued documents are immutable for everyone, including the owner role.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION issued_documents_immutable() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'issued_documents are immutable' USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.snapshot IS DISTINCT FROM OLD.snapshot OR NEW.template_version IS DISTINCT FROM OLD.template_version
               OR NEW.brand_version IS DISTINCT FROM OLD.brand_version OR NEW.source_id IS DISTINCT FROM OLD.source_id
               OR (OLD.sha256 IS NOT NULL AND NEW.sha256 IS DISTINCT FROM OLD.sha256) THEN
                RAISE EXCEPTION 'issued_documents are immutable' USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_issued_documents_immutable BEFORE UPDATE OR DELETE ON issued_documents "
        "FOR EACH ROW EXECUTE FUNCTION issued_documents_immutable()"
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TENANT_TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")
        op.execute(f"REVOKE UPDATE, DELETE ON issued_documents FROM {quoted}")
        op.execute(f"GRANT UPDATE (sha256, storage_path, updated_at) ON issued_documents TO {quoted}")

    _backfill(bind)


def _backfill(bind) -> None:
    bind.execute(sa.text(
        """
        INSERT INTO commercial_brand_profiles (owner_type, version, status, display_name, product_name, short_name,
                                               tagline, show_powered_by, powered_by_label, locked_fields,
                                               published_at, published_by, created_by, change_summary)
        VALUES ('PLATFORM', 1, 'PUBLISHED', 'Intertech HRMS', 'Intertech HRMS', 'HRMS',
                'People, attendance, leave and payroll in one place.', true, 'Powered by Intertech HRMS', '{}',
                now(), 'migration 0006', 'migration 0006', '[]'::jsonb)
        """
    ))
    companies = bind.execute(sa.text("SELECT id FROM companies ORDER BY id")).fetchall()
    for (company_id,) in companies:
        logo_asset = _import_logo(bind, company_id)
        bind.execute(sa.text(
            """
            INSERT INTO brand_profiles (company_id, owner_type, version, status, logo_light_asset_id, published_at,
                                        published_by, created_by, change_summary, row_version)
            VALUES (:cid, 'TENANT', 1, 'PUBLISHED', :logo, now(), 'migration 0006', 'migration 0006', '[]'::jsonb, 1)
            """
        ), {"cid": company_id, "logo": logo_asset})
    for code, scope in HR_ADMIN_GRANTS:
        bind.execute(sa.text(
            """
            INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at)
            SELECT r.company_id, r.id, :code, :scope, now(), now() FROM roles r
            WHERE r.system_key = 'hr_admin' AND NOT EXISTS (
                SELECT 1 FROM role_permissions rp WHERE rp.role_id = r.id AND rp.permission_code = :code AND rp.scope = :scope
            )
            """
        ), {"code": code, "scope": scope})


def _import_logo(bind, company_id: int):
    """Re-process the legacy logo (company_settings._logoPath) into a brand asset; None on any problem."""
    path = bind.execute(sa.text(
        "SELECT value FROM company_settings WHERE company_id = :cid AND key = '_logoPath'"
    ), {"cid": company_id}).scalar()
    if not path:
        return None
    try:
        from app.services import brand_asset_service

        root = Path(os.environ.get("UPLOAD_DIR", "./uploads")).resolve()
        file = (root / path).resolve()
        if (root / str(company_id)).resolve() not in file.parents:
            return None
        processed = brand_asset_service.process_upload(file.read_bytes(), "logo_light")
    except Exception as exc:  # unreadable or invalid legacy file: the legacy endpoint still serves it
        logger.warning("Company %s: legacy logo not imported (%s)", company_id, exc)
        return None
    return bind.execute(sa.text(
        """
        INSERT INTO brand_assets (company_id, kind, sha256, ext, mime, width, height, bytes, original_filename,
                                  derivatives, created_by)
        VALUES (:cid, 'logo_light', :sha, :ext, :mime, :w, :h, :bytes, 'legacy-logo', CAST(:derivatives AS jsonb),
                'migration 0006')
        RETURNING id
        """
    ), {"cid": company_id, "sha": processed.sha256, "ext": processed.ext, "mime": processed.mime,
        "w": processed.width, "h": processed.height, "bytes": processed.bytes,
        "derivatives": __import__("json").dumps(processed.derivatives)}).scalar()


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission_code IN "
               "('branding:manage', 'domain:manage', 'email_template:manage', 'org:read', 'org:manage_entities', "
               "'org:manage_branches')")
    op.drop_index('ix_user_sessions_host', table_name='user_sessions')
    op.drop_column('user_sessions', 'host')
    op.execute("DROP TRIGGER IF EXISTS trg_issued_documents_immutable ON issued_documents")
    op.execute("DROP FUNCTION IF EXISTS issued_documents_immutable()")
    for table in ('email_template_overrides', 'issued_documents', 'auth_handoff_codes', 'tenant_domains',
                  'brand_assets', 'brand_profiles', 'commercial_brand_assets', 'commercial_brand_profiles'):
        op.drop_table(table)
