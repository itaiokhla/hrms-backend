"""Payroll platform foundations: settings v2 and policies, payroll tables, country capability, FX, jobs, idempotency, outbox, generic approvals.

Additive and backwards compatible (3.1 §2-§3, 3.9 §3.1/§3.3/§3.9, 3.2 §4/§6.2, 3.5 §16):
  * tenant tables with row-level security, composite same-company foreign keys and grants:
    setting_values, setting_change_requests, policies, policy_versions, policy_assignments,
    payroll_tables, payroll_table_rows, fx_snapshots, fx_snapshot_rates, fx_conversions,
    idempotency_keys, outbox_events, approval_requests, approval_request_steps;
  * platform tables: platform_setting_values and country_capabilities (the app role may only read
    them); fx_rates (company_id NULL = platform rate: a tenant sees platform rows and its own, and
    inserts or updates only its own); jobs (company_id NULL = platform job; no row-level security,
    because the worker claims across tenants; tenant reads filter company_id explicitly);
  * setting_values carry their source (STARTER_PROFILE, LEGACY_MIGRATION, ADMIN, PACK_LOCK,
    PARTNER_PROVISIONING) and, for profile rows, the profile_version (decisions I-13, I-14);
  * approval_chain_steps accepts the payroll modules (new CHECK), the finance_role and
    specific_user approver types, specific_user_id, min_amount with its currency and condition;
    approval_delegations accepts the same modules. Leave chains keep working unchanged.

Overlap rules are triggers, because this PostgreSQL build has no btree_gist for exclusion
constraints: ACTIVE setting values of one key and scope, platform/partner values of one key,
ACTIVE versions of one policy and ACTIVE versions of one payroll table never overlap. Each guard
first takes a transaction-scoped advisory lock (namespaces 6101-6104 of the payroll range
6100-6199) so concurrent writers are serialised. FX snapshot rates and conversions may only cite
ACTIVE platform rates or rates of their own company; a job's parent is a platform job or a job
of the same company; an activated policy version is immutable.

Nothing is backfilled and nothing is seeded (a missing country_capabilities row means the code
default). The upgrade stops, changing nothing, if approval_chain_steps holds an unknown module.

Downgrade drops the new tables (their data is lost) and deletes approval chain steps and
delegations of the new modules or approver types before restoring the previous checks.

Revision ID: 0009_payroll_platform
Revises: 0008_payroll_org
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0009_payroll_platform'
down_revision: Union[str, None] = '0008_payroll_org'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
CURRENT_TENANT = "NULLIF(current_setting('app.company_id', true), '')::integer"
TENANT_TABLES = (
    "setting_values", "setting_change_requests", "policies", "policy_versions", "policy_assignments",
    "payroll_tables", "payroll_table_rows", "fx_snapshots", "fx_snapshot_rates", "fx_conversions",
    "idempotency_keys", "outbox_events", "approval_requests", "approval_request_steps",
)
# Platform tables: what the application role may do (fx_rates rows are further limited by RLS).
PLATFORM_GRANTS = (
    ("platform_setting_values", "SELECT"),
    ("country_capabilities", "SELECT"),
    ("fx_rates", "SELECT, INSERT, UPDATE"),
    ("jobs", "SELECT, INSERT, UPDATE"),
)
PLATFORM_SEQUENCES = ("fx_rates", "jobs")  # country_capabilities has a natural key

# Frozen copies of the vocabularies in app/models (migrations never import application code).
APPROVAL_MODULES = (
    'leave', 'payroll_run', 'compensation', 'wage_input', 'overtime', 'task', 'reimbursement', 'loan',
    'bank_change', 'payment_batch', 'off_cycle', 'final_settlement', 'lop', 'attendance', 'settings',
)
OLD_APPROVER_TYPES = ('primary_manager', 'skip_level_manager', 'department_head', 'role')
APPROVER_TYPES = OLD_APPROVER_TYPES + ('finance_role', 'specific_user')
STEP_STATUSES = ('waiting', 'pending', 'approved', 'rejected', 'skipped', 'cancelled')
SETTING_SCOPE_TYPES = ('TENANT', 'LEGAL_ENTITY', 'BRANCH', 'PAY_GROUP', 'EMPLOYEE')
SETTING_STATUSES = ('PENDING_APPROVAL', 'ACTIVE', 'SUPERSEDED', 'REJECTED')
SETTING_SOURCES = ('STARTER_PROFILE', 'LEGACY_MIGRATION', 'ADMIN', 'PACK_LOCK', 'PARTNER_PROVISIONING')
PROFILE_SOURCES = ('STARTER_PROFILE', 'LEGACY_MIGRATION')
CAPABILITY_LEVELS = ('UNSUPPORTED', 'DISPLAY_ONLY', 'CONFIG_ONLY', 'CALC_SUPPORTED', 'VERIFIED', 'FILING_INTEGRATED')
FX_RATE_TYPES = ('MID', 'BUY', 'SELL')
FX_PURPOSES = ('CONTRACT_TO_PAY', 'PAY_TO_PAYMENT', 'REPORTING', 'ACCOUNTING', 'REIMBURSEMENT')
ROUNDING_MODES = ('HALF_UP', 'HALF_EVEN', 'UP', 'DOWN', 'CEILING', 'FLOOR')
JOB_STATUSES = ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'DEAD', 'CANCELLED')
WINDOW = "effective_to IS NULL OR effective_to >= effective_from"
JSONB_OBJECT = sa.text("'{}'::jsonb")


def _base():
    return [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _tenant(table: str):
    """company_id, the base columns, primary key and the (company_id, id) key for composite references."""
    return [
        sa.Column('company_id', sa.Integer(), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{table}_company_id_id'),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    _settings_and_policies()
    _catalogue_and_fx()
    _jobs_idempotency_outbox()
    _approvals(bind)
    _install_guards()
    _install_security(bind)


def _settings_and_policies() -> None:
    op.create_table(
        'setting_values',
        sa.Column('key', sa.String(120), nullable=False),
        sa.Column('scope_type', sa.String(20), nullable=False),
        sa.Column('scope_id', sa.Integer(), nullable=True),
        sa.Column('value', postgresql.JSONB(), nullable=False),
        sa.Column('locked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('reason', sa.String(500), nullable=True),
        sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source', sa.String(30), nullable=False, server_default='ADMIN'),
        sa.Column('profile_version', sa.String(20), nullable=True),
        *_tenant('setting_values'),
        sa.CheckConstraint(f"scope_type IN {SETTING_SCOPE_TYPES}", name='ck_setting_values_scope_type'),
        sa.CheckConstraint("(scope_type = 'TENANT') = (scope_id IS NULL)", name='ck_setting_values_scope_id'),
        sa.CheckConstraint(f"status IN {SETTING_STATUSES}", name='ck_setting_values_status'),
        sa.CheckConstraint(WINDOW, name='ck_setting_values_window'),
        sa.CheckConstraint('row_version >= 1', name='ck_setting_values_row_version'),
        sa.CheckConstraint(f"source IN {SETTING_SOURCES}", name='ck_setting_values_source'),
        sa.CheckConstraint(f"source NOT IN {PROFILE_SOURCES} OR profile_version IS NOT NULL",
                           name='ck_setting_values_profile_version'),
        sa.CheckConstraint(
            'approved_by_user_id IS NULL OR created_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id',
            name='ck_setting_values_maker_checker'),
    )
    op.create_index('ix_setting_values_company_id', 'setting_values', ['company_id'])
    op.create_index('ix_setting_values_lookup', 'setting_values',
                    ['company_id', 'key', 'scope_type', 'scope_id', 'effective_from'])

    op.create_table(
        'setting_change_requests',
        sa.Column('setting_value_id', sa.Integer(), nullable=False),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=False),
        sa.Column('decided_by_user_id', sa.Integer(), nullable=True),
        sa.Column('decision', sa.String(10), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('comment', sa.String(500), nullable=True),
        *_tenant('setting_change_requests'),
        sa.ForeignKeyConstraint(['company_id', 'setting_value_id'], ['setting_values.company_id', 'setting_values.id'],
                                ondelete='CASCADE', name='fk_setting_change_requests_value'),
        sa.CheckConstraint("decision IN ('APPROVED', 'REJECTED')", name='ck_setting_change_requests_decision'),
        sa.CheckConstraint('(decision IS NULL) = (decided_at IS NULL)', name='ck_setting_change_requests_decided'),
        sa.CheckConstraint(
            "decision IS DISTINCT FROM 'APPROVED' "
            "OR (decided_by_user_id IS NOT NULL AND decided_by_user_id <> requested_by_user_id)",
            name='ck_setting_change_requests_maker_checker'),
    )
    op.create_index('ix_setting_change_requests_company_id', 'setting_change_requests', ['company_id'])
    op.create_index('ix_setting_change_requests_setting_value_id', 'setting_change_requests', ['setting_value_id'])
    op.create_index('uq_setting_change_requests_open', 'setting_change_requests', ['setting_value_id'], unique=True,
                    postgresql_where=sa.text('decision IS NULL'))

    op.create_table(
        'platform_setting_values',
        sa.Column('level', sa.String(10), nullable=False),
        sa.Column('partner_id', sa.Integer(), nullable=True),
        sa.Column('key', sa.String(120), nullable=False),
        sa.Column('value', postgresql.JSONB(), nullable=False),
        sa.Column('locked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('created_by', sa.String(255), nullable=False),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['partner_id'], ['partners.id'], ondelete='CASCADE'),
        sa.CheckConstraint("level IN ('PLATFORM', 'PARTNER')", name='ck_platform_setting_values_level'),
        sa.CheckConstraint("(level = 'PARTNER') = (partner_id IS NOT NULL)", name='ck_platform_setting_values_partner'),
        sa.CheckConstraint(WINDOW, name='ck_platform_setting_values_window'),
    )
    op.create_index('ix_platform_setting_values_partner_id', 'platform_setting_values', ['partner_id'])
    op.create_index('ix_platform_setting_values_lookup', 'platform_setting_values',
                    ['key', 'level', 'partner_id', 'effective_from'])

    op.create_table(
        'policies',
        sa.Column('type', sa.String(40), nullable=False),
        sa.Column('name', sa.String(150), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        *_tenant('policies'),
        sa.UniqueConstraint('company_id', 'type', 'name', name='uq_policies_company_type_name'),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name='ck_policies_status'),
    )
    op.create_index('ix_policies_company_id', 'policies', ['company_id'])

    op.create_table(
        'policy_versions',
        sa.Column('policy_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('fields', postgresql.JSONB(), nullable=False, server_default=JSONB_OBJECT),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='DRAFT'),
        sa.Column('policy_hash', sa.CHAR(64), nullable=True),
        sa.Column('activated_by_user_id', sa.Integer(), nullable=True),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        *_tenant('policy_versions'),
        sa.ForeignKeyConstraint(['company_id', 'policy_id'], ['policies.company_id', 'policies.id'],
                                name='fk_policy_versions_policy'),
        sa.UniqueConstraint('policy_id', 'version', name='uq_policy_versions_policy_version'),
        sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'SUPERSEDED')", name='ck_policy_versions_status'),
        sa.CheckConstraint('version >= 1', name='ck_policy_versions_version'),
        sa.CheckConstraint(WINDOW, name='ck_policy_versions_window'),
        sa.CheckConstraint("jsonb_typeof(fields) = 'object'", name='ck_policy_versions_fields'),
        sa.CheckConstraint("policy_hash IS NULL OR policy_hash ~ '^[0-9a-f]{64}$'", name='ck_policy_versions_hash'),
        sa.CheckConstraint("status = 'DRAFT' OR (policy_hash IS NOT NULL AND activated_at IS NOT NULL)",
                           name='ck_policy_versions_activation'),
    )
    op.create_index('ix_policy_versions_company_id', 'policy_versions', ['company_id'])
    op.create_index('ix_policy_versions_policy_id', 'policy_versions', ['policy_id'])

    op.create_table(
        'policy_assignments',
        sa.Column('policy_id', sa.Integer(), nullable=False),
        sa.Column('criteria', postgresql.JSONB(), nullable=False, server_default=JSONB_OBJECT),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        *_tenant('policy_assignments'),
        sa.ForeignKeyConstraint(['company_id', 'policy_id'], ['policies.company_id', 'policies.id'],
                                ondelete='CASCADE', name='fk_policy_assignments_policy'),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name='ck_policy_assignments_status'),
        sa.CheckConstraint(WINDOW, name='ck_policy_assignments_window'),
        sa.CheckConstraint("jsonb_typeof(criteria) = 'object'", name='ck_policy_assignments_criteria'),
    )
    op.create_index('ix_policy_assignments_company_id', 'policy_assignments', ['company_id'])
    op.create_index('ix_policy_assignments_policy_id', 'policy_assignments', ['policy_id'])

    op.create_table(
        'payroll_tables',
        sa.Column('code', sa.String(60), nullable=False),
        sa.Column('name', sa.String(150), nullable=False),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('owner', sa.String(10), nullable=False, server_default='COMPANY'),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', sa.String(10), nullable=False, server_default='DRAFT'),
        *_tenant('payroll_tables'),
        sa.UniqueConstraint('company_id', 'code', 'effective_from', name='uq_payroll_tables_code_from'),
        sa.CheckConstraint("kind IN ('SLAB', 'BAND', 'LOOKUP')", name='ck_payroll_tables_kind'),
        sa.CheckConstraint("owner IN ('COMPANY', 'PACK')", name='ck_payroll_tables_owner'),
        sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')", name='ck_payroll_tables_status'),
        sa.CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name='ck_payroll_tables_currency'),
        sa.CheckConstraint(WINDOW, name='ck_payroll_tables_window'),
    )
    op.create_index('ix_payroll_tables_company_id', 'payroll_tables', ['company_id'])

    op.create_table(
        'payroll_table_rows',
        sa.Column('table_id', sa.Integer(), nullable=False),
        sa.Column('lower', sa.Numeric(19, 4), nullable=True),
        sa.Column('upper', sa.Numeric(19, 4), nullable=True),
        sa.Column('key', sa.String(100), nullable=True),
        sa.Column('value', sa.Numeric(19, 4), nullable=True),
        sa.Column('rate', sa.Numeric(12, 6), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        *_tenant('payroll_table_rows'),
        sa.ForeignKeyConstraint(['company_id', 'table_id'], ['payroll_tables.company_id', 'payroll_tables.id'],
                                ondelete='CASCADE', name='fk_payroll_table_rows_table'),
        sa.CheckConstraint('lower IS NULL OR upper IS NULL OR upper >= lower', name='ck_payroll_table_rows_bounds'),
        sa.CheckConstraint('value IS NOT NULL OR rate IS NOT NULL', name='ck_payroll_table_rows_result'),
    )
    op.create_index('ix_payroll_table_rows_company_id', 'payroll_table_rows', ['company_id'])
    op.create_index('ix_payroll_table_rows_table_id', 'payroll_table_rows', ['table_id'])
    op.create_index('uq_payroll_table_rows_key', 'payroll_table_rows', ['table_id', 'key'], unique=True,
                    postgresql_where=sa.text('key IS NOT NULL'))


def _catalogue_and_fx() -> None:
    op.create_table(
        'country_capabilities',
        sa.Column('alpha2', sa.CHAR(2), nullable=False),
        sa.Column('capability_level', sa.String(20), nullable=False),
        sa.Column('note', sa.String(500), nullable=True),
        sa.Column('updated_by', sa.String(255), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('alpha2'),
        sa.CheckConstraint("alpha2 ~ '^[A-Z]{2}$'", name='ck_country_capabilities_alpha2'),
        sa.CheckConstraint(f"capability_level IN {CAPABILITY_LEVELS}", name='ck_country_capabilities_level'),
    )

    op.create_table(
        'fx_rates',
        sa.Column('company_id', sa.Integer(), nullable=True),
        sa.Column('base_currency', sa.String(3), nullable=False),
        sa.Column('quote_currency', sa.String(3), nullable=False),
        sa.Column('rate', sa.Numeric(24, 12), nullable=False),
        sa.Column('rate_type', sa.String(10), nullable=False, server_default='MID'),
        sa.Column('source', sa.String(40), nullable=False),
        sa.Column('as_of', sa.Date(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('derived_from', postgresql.JSONB(), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('approved_by_user_id', sa.Integer(), nullable=True),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.CheckConstraint("base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$'",
                           name='ck_fx_rates_currencies'),
        sa.CheckConstraint('base_currency <> quote_currency', name='ck_fx_rates_pair'),
        sa.CheckConstraint('rate > 0', name='ck_fx_rates_rate'),
        sa.CheckConstraint(f"rate_type IN {FX_RATE_TYPES}", name='ck_fx_rates_rate_type'),
        sa.CheckConstraint("status IN ('ACTIVE', 'PENDING_APPROVAL', 'REJECTED')", name='ck_fx_rates_status'),
        sa.CheckConstraint(
            'approved_by_user_id IS NULL OR created_by_user_id IS NULL OR approved_by_user_id <> created_by_user_id',
            name='ck_fx_rates_maker_checker'),
    )
    op.create_index('ix_fx_rates_company_id', 'fx_rates', ['company_id'])
    op.create_index('ix_fx_rates_lookup', 'fx_rates', ['base_currency', 'quote_currency', 'rate_type', 'as_of'])

    op.create_table(
        'fx_snapshots',
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('purpose', sa.String(40), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
        *_tenant('fx_snapshots'),
    )
    op.create_index('ix_fx_snapshots_company_id', 'fx_snapshots', ['company_id'])
    op.create_index('ix_fx_snapshots_run_id', 'fx_snapshots', ['run_id'])

    op.create_table(
        'fx_snapshot_rates',
        sa.Column('snapshot_id', sa.Integer(), nullable=False),
        sa.Column('fx_rate_id', sa.Integer(), nullable=False),
        sa.Column('base_currency', sa.String(3), nullable=False),
        sa.Column('quote_currency', sa.String(3), nullable=False),
        sa.Column('rate', sa.Numeric(24, 12), nullable=False),
        sa.Column('rate_type', sa.String(10), nullable=False),
        sa.Column('source', sa.String(40), nullable=False),
        sa.Column('as_of', sa.Date(), nullable=False),
        *_tenant('fx_snapshot_rates'),
        sa.ForeignKeyConstraint(['company_id', 'snapshot_id'], ['fx_snapshots.company_id', 'fx_snapshots.id'],
                                ondelete='CASCADE', name='fk_fx_snapshot_rates_snapshot'),
        sa.UniqueConstraint('snapshot_id', 'base_currency', 'quote_currency', 'rate_type',
                            name='uq_fx_snapshot_rates_pair'),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$' AND base_currency <> quote_currency",
            name='ck_fx_snapshot_rates_pair'),
        sa.CheckConstraint('rate > 0', name='ck_fx_snapshot_rates_rate'),
        sa.CheckConstraint(f"rate_type IN {FX_RATE_TYPES}", name='ck_fx_snapshot_rates_rate_type'),
    )
    op.create_index('ix_fx_snapshot_rates_company_id', 'fx_snapshot_rates', ['company_id'])
    op.create_index('ix_fx_snapshot_rates_snapshot_id', 'fx_snapshot_rates', ['snapshot_id'])

    op.create_table(
        'fx_conversions',
        sa.Column('source_amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('source_currency', sa.String(3), nullable=False),
        sa.Column('target_currency', sa.String(3), nullable=False),
        sa.Column('purpose', sa.String(20), nullable=False),
        sa.Column('fx_rate_id', sa.Integer(), nullable=True),
        sa.Column('rate', sa.Numeric(24, 12), nullable=False),
        sa.Column('quotation', sa.String(40), nullable=False),
        sa.Column('rate_date', sa.Date(), nullable=False),
        sa.Column('rounding_mode', sa.String(10), nullable=False),
        sa.Column('result_amount', sa.Numeric(19, 4), nullable=False),
        sa.Column('ref_type', sa.String(40), nullable=True),
        sa.Column('ref_id', sa.Integer(), nullable=True),
        *_tenant('fx_conversions'),
        sa.CheckConstraint(
            "source_currency ~ '^[A-Z]{3}$' AND target_currency ~ '^[A-Z]{3}$' AND source_currency <> target_currency",
            name='ck_fx_conversions_currencies'),
        sa.CheckConstraint(f"purpose IN {FX_PURPOSES}", name='ck_fx_conversions_purpose'),
        sa.CheckConstraint('rate > 0', name='ck_fx_conversions_rate'),
        sa.CheckConstraint(f"rounding_mode IN {ROUNDING_MODES}", name='ck_fx_conversions_rounding'),
        sa.CheckConstraint('(ref_type IS NULL) = (ref_id IS NULL)', name='ck_fx_conversions_ref'),
    )
    op.create_index('ix_fx_conversions_company_id', 'fx_conversions', ['company_id'])
    op.create_index('ix_fx_conversions_ref', 'fx_conversions', ['company_id', 'ref_type', 'ref_id'])


def _jobs_idempotency_outbox() -> None:
    op.create_table(
        'jobs',
        sa.Column('company_id', sa.Integer(), nullable=True),
        sa.Column('type', sa.String(60), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False, server_default=JSONB_OBJECT),
        sa.Column('status', sa.String(12), nullable=False, server_default='QUEUED'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('locked_by', sa.String(120), nullable=True),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('progress_done', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('progress_total', sa.Integer(), nullable=True),
        sa.Column('result', postgresql.JSONB(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('parent_job_id', sa.Integer(), nullable=True),
        sa.Column('idempotency_key', sa.String(120), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        *_base(),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['parent_job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.CheckConstraint(f"status IN {JOB_STATUSES}", name='ck_jobs_status'),
        sa.CheckConstraint('attempts >= 0 AND max_attempts >= 1', name='ck_jobs_attempts'),
        sa.CheckConstraint('progress_done >= 0 AND (progress_total IS NULL OR progress_total >= 0)',
                           name='ck_jobs_progress'),
        sa.CheckConstraint("status <> 'RUNNING' OR (locked_by IS NOT NULL AND locked_at IS NOT NULL)",
                           name='ck_jobs_running_lock'),
        sa.CheckConstraint('parent_job_id IS NULL OR parent_job_id <> id', name='ck_jobs_not_own_parent'),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name='ck_jobs_payload'),
    )
    op.create_index('ix_jobs_company_id', 'jobs', ['company_id'])
    op.create_index('ix_jobs_parent_job_id', 'jobs', ['parent_job_id'])
    op.create_index('uq_jobs_idempotency', 'jobs', [sa.text('COALESCE(company_id, 0)'), 'type', 'idempotency_key'],
                    unique=True, postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.create_index('ix_jobs_queue', 'jobs', ['status', 'run_at', 'priority'],
                    postgresql_where=sa.text("status = 'QUEUED'"))
    op.create_index('ix_jobs_running_heartbeat', 'jobs', ['heartbeat_at'],
                    postgresql_where=sa.text("status = 'RUNNING'"))

    op.create_table(
        'idempotency_keys',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(120), nullable=False),
        sa.Column('method', sa.String(10), nullable=False),
        sa.Column('path', sa.String(500), nullable=False),
        sa.Column('request_hash', sa.CHAR(64), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('response', postgresql.JSONB(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        *_tenant('idempotency_keys'),
        sa.ForeignKeyConstraint(['company_id', 'user_id'], ['users.company_id', 'users.id'],
                                ondelete='CASCADE', name='fk_idempotency_keys_user'),
        sa.UniqueConstraint('company_id', 'user_id', 'key', name='uq_idempotency_keys_user_key'),
        sa.CheckConstraint("method IN ('POST', 'PUT', 'PATCH', 'DELETE')", name='ck_idempotency_keys_method'),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name='ck_idempotency_keys_request_hash'),
        sa.CheckConstraint('status_code IS NULL OR status_code BETWEEN 100 AND 599',
                           name='ck_idempotency_keys_status_code'),
    )
    op.create_index('ix_idempotency_keys_company_id', 'idempotency_keys', ['company_id'])
    op.create_index('ix_idempotency_keys_expires_at', 'idempotency_keys', ['expires_at'])

    op.create_table(
        'outbox_events',
        sa.Column('topic', sa.String(80), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False, server_default=JSONB_OBJECT),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        *_tenant('outbox_events'),
        sa.CheckConstraint('attempts >= 0', name='ck_outbox_events_attempts'),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name='ck_outbox_events_payload'),
    )
    op.create_index('ix_outbox_events_company_id', 'outbox_events', ['company_id'])
    op.create_index('ix_outbox_events_pending', 'outbox_events', ['created_at'],
                    postgresql_where=sa.text('dispatched_at IS NULL'))


def _approvals(bind) -> None:
    unknown = [module for (module,) in bind.execute(sa.text(
        f"SELECT DISTINCT module FROM approval_chain_steps WHERE module NOT IN {APPROVAL_MODULES} ORDER BY 1"
    ))]
    if unknown:
        raise RuntimeError(f"approval_chain_steps uses unknown modules {unknown}: review those rows before upgrading")
    op.create_check_constraint('ck_approval_chain_module', 'approval_chain_steps', f"module IN {APPROVAL_MODULES}")
    op.drop_constraint('ck_approval_chain_type', 'approval_chain_steps', type_='check')
    op.create_check_constraint('ck_approval_chain_type', 'approval_chain_steps', f"approver_type IN {APPROVER_TYPES}")
    op.add_column('approval_chain_steps', sa.Column('specific_user_id', sa.Integer(), nullable=True))
    op.add_column('approval_chain_steps', sa.Column('min_amount', sa.Numeric(19, 4), nullable=True))
    op.add_column('approval_chain_steps', sa.Column('min_amount_currency', sa.String(3), nullable=True))
    op.add_column('approval_chain_steps', sa.Column('condition', postgresql.JSONB(), nullable=True))
    op.create_foreign_key('fk_approval_chain_specific_user', 'approval_chain_steps', 'users',
                          ['company_id', 'specific_user_id'], ['company_id', 'id'])
    op.create_check_constraint('ck_approval_chain_user_required', 'approval_chain_steps',
                               "(approver_type = 'specific_user') = (specific_user_id IS NOT NULL)")
    op.create_check_constraint(
        'ck_approval_chain_min_amount', 'approval_chain_steps',
        '(min_amount IS NULL) = (min_amount_currency IS NULL) AND (min_amount IS NULL OR min_amount >= 0)')
    op.create_check_constraint('ck_approval_chain_currency', 'approval_chain_steps',
                               "min_amount_currency IS NULL OR min_amount_currency ~ '^[A-Z]{3}$'")

    op.drop_constraint('ck_delegation_module', 'approval_delegations', type_='check')
    op.create_check_constraint('ck_delegation_module', 'approval_delegations', f"module IN {APPROVAL_MODULES}")

    op.create_table(
        'approval_requests',
        sa.Column('module', sa.String(20), nullable=False),
        sa.Column('subject_type', sa.String(40), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=False),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='PENDING'),
        sa.Column('current_step', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Numeric(19, 4), nullable=True),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('context', postgresql.JSONB(), nullable=False, server_default=JSONB_OBJECT),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        *_tenant('approval_requests'),
        sa.CheckConstraint(f"module IN {APPROVAL_MODULES}", name='ck_approval_requests_module'),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
                           name='ck_approval_requests_status'),
        sa.CheckConstraint("(status = 'PENDING') = (decided_at IS NULL)", name='ck_approval_requests_decided'),
        sa.CheckConstraint('(amount IS NULL) = (currency IS NULL)', name='ck_approval_requests_amount'),
        sa.CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name='ck_approval_requests_currency'),
        sa.CheckConstraint("jsonb_typeof(context) = 'object'", name='ck_approval_requests_context'),
    )
    op.create_index('ix_approval_requests_company_id', 'approval_requests', ['company_id'])
    op.create_index('ix_approval_requests_subject', 'approval_requests', ['company_id', 'subject_type', 'subject_id'])
    op.create_index('uq_approval_requests_one_pending', 'approval_requests',
                    ['company_id', 'module', 'subject_type', 'subject_id'], unique=True,
                    postgresql_where=sa.text("status = 'PENDING'"))

    op.create_table(
        'approval_request_steps',
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.Column('step_order', sa.Integer(), nullable=False),
        sa.Column('approver_type', sa.String(30), nullable=False),
        sa.Column('role_id', sa.Integer(), nullable=True),
        sa.Column('specific_user_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='waiting'),
        sa.Column('acted_by_user_id', sa.Integer(), nullable=True),
        sa.Column('acted_as', sa.String(20), nullable=True),
        sa.Column('acted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('remarks', sa.Text(), nullable=True),
        *_tenant('approval_request_steps'),
        sa.ForeignKeyConstraint(['company_id', 'request_id'], ['approval_requests.company_id', 'approval_requests.id'],
                                ondelete='CASCADE', name='fk_approval_request_steps_request'),
        sa.ForeignKeyConstraint(['company_id', 'role_id'], ['roles.company_id', 'roles.id'],
                                name='fk_approval_request_steps_role'),
        sa.ForeignKeyConstraint(['company_id', 'specific_user_id'], ['users.company_id', 'users.id'],
                                name='fk_approval_request_steps_user'),
        sa.UniqueConstraint('request_id', 'step_order', name='uq_approval_request_steps_order'),
        sa.CheckConstraint('step_order >= 1', name='ck_approval_request_steps_order'),
        sa.CheckConstraint(f"approver_type IN {APPROVER_TYPES}", name='ck_approval_request_steps_type'),
        sa.CheckConstraint("(approver_type = 'role') = (role_id IS NOT NULL)",
                           name='ck_approval_request_steps_role_required'),
        sa.CheckConstraint("(approver_type = 'specific_user') = (specific_user_id IS NOT NULL)",
                           name='ck_approval_request_steps_user_required'),
        sa.CheckConstraint(f"status IN {STEP_STATUSES}", name='ck_approval_request_steps_status'),
        sa.CheckConstraint("acted_as IN ('approver', 'delegate', 'override')", name='ck_approval_request_steps_acted_as'),
    )
    op.create_index('ix_approval_request_steps_company_id', 'approval_request_steps', ['company_id'])
    op.create_index('ix_approval_request_steps_request_id', 'approval_request_steps', ['request_id'])
    op.create_index('ix_approval_request_steps_pending', 'approval_request_steps', ['company_id'],
                    postgresql_where=sa.text("status = 'pending'"))


def _overlap_guard(function: str, table: str, condition: str, lock: str, scope: str, message: str) -> None:
    """BEFORE INSERT/UPDATE trigger: lock `lock`, then reject a row of `scope` overlapping NEW's window."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$
        BEGIN
            -- An inverted window is left to the CHECK constraint, which reports it.
            IF ({condition}) AND (NEW.effective_to IS NULL OR NEW.effective_to >= NEW.effective_from) THEN
                -- Serialise concurrent writers of the same rows, then compare inclusive date windows.
                PERFORM pg_advisory_xact_lock({lock});
                IF EXISTS (
                    SELECT 1 FROM {table} o
                    WHERE {scope} AND o.id <> NEW.id
                      AND daterange(o.effective_from, o.effective_to, '[]')
                          && daterange(NEW.effective_from, NEW.effective_to, '[]')
                ) THEN
                    RAISE EXCEPTION '{message}' USING ERRCODE = 'exclusion_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(f"CREATE TRIGGER trg_{function} BEFORE INSERT OR UPDATE ON {table} "
               f"FOR EACH ROW EXECUTE FUNCTION {function}()")


def _install_guards() -> None:
    _overlap_guard(
        'setting_values_guard', 'setting_values', "NEW.status = 'ACTIVE'",
        "6101, hashtext(NEW.company_id || ':' || NEW.key || ':' || NEW.scope_type || ':' || COALESCE(NEW.scope_id, 0))",
        "o.company_id = NEW.company_id AND o.key = NEW.key AND o.scope_type = NEW.scope_type "
        "AND COALESCE(o.scope_id, 0) = COALESCE(NEW.scope_id, 0) AND o.status = 'ACTIVE'",
        'Active values of one setting and scope cannot overlap')
    _overlap_guard(
        'platform_setting_values_guard', 'platform_setting_values', 'true',
        "6102, hashtext(NEW.level || ':' || COALESCE(NEW.partner_id, 0) || ':' || NEW.key)",
        "o.level = NEW.level AND COALESCE(o.partner_id, 0) = COALESCE(NEW.partner_id, 0) AND o.key = NEW.key",
        'Platform values of one setting cannot overlap')
    _overlap_guard(
        'payroll_tables_guard', 'payroll_tables', "NEW.status = 'ACTIVE'",
        "6104, hashtext(NEW.company_id || ':' || NEW.code)",
        "o.company_id = NEW.company_id AND o.code = NEW.code AND o.status = 'ACTIVE'",
        'Active versions of one payroll table cannot overlap')

    # Policy versions: immutable once activated (only closing effective_to and ACTIVE -> SUPERSEDED),
    # and ACTIVE versions of one policy never overlap.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION policy_versions_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.status <> 'DRAFT' AND (
                    NEW.company_id <> OLD.company_id OR NEW.policy_id <> OLD.policy_id OR NEW.version <> OLD.version
                    OR NEW.fields IS DISTINCT FROM OLD.fields OR NEW.policy_hash IS DISTINCT FROM OLD.policy_hash
                    OR NEW.effective_from <> OLD.effective_from
                    OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
                    OR NEW.activated_by_user_id IS DISTINCT FROM OLD.activated_by_user_id
                    OR NEW.status = 'DRAFT'
                    OR (OLD.status = 'SUPERSEDED'
                        AND (NEW.status <> 'SUPERSEDED' OR NEW.effective_to IS DISTINCT FROM OLD.effective_to))
                ) THEN
                    RAISE EXCEPTION 'An activated policy version cannot be changed'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
            END IF;
            IF NEW.status = 'ACTIVE' AND (NEW.effective_to IS NULL OR NEW.effective_to >= NEW.effective_from) THEN
                PERFORM pg_advisory_xact_lock(6103, NEW.policy_id);
                IF EXISTS (
                    SELECT 1 FROM policy_versions o
                    WHERE o.company_id = NEW.company_id AND o.policy_id = NEW.policy_id AND o.status = 'ACTIVE'
                      AND o.id <> NEW.id
                      AND daterange(o.effective_from, o.effective_to, '[]')
                          && daterange(NEW.effective_from, NEW.effective_to, '[]')
                ) THEN
                    RAISE EXCEPTION 'Active versions of one policy cannot overlap' USING ERRCODE = 'exclusion_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("CREATE TRIGGER trg_policy_versions_guard BEFORE INSERT OR UPDATE ON policy_versions "
               "FOR EACH ROW EXECUTE FUNCTION policy_versions_guard()")

    # A rate is cited only while approved, and only a platform rate or one of the citing company.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fx_rate_reference_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.fx_rate_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM fx_rates r
                WHERE r.id = NEW.fx_rate_id AND r.status = 'ACTIVE'
                  AND (r.company_id IS NULL OR r.company_id = NEW.company_id)
            ) THEN
                RAISE EXCEPTION 'The FX rate must be an approved platform rate or a rate of the same company'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ('fx_snapshot_rates', 'fx_conversions'):
        op.execute(f"CREATE TRIGGER trg_{table}_rate BEFORE INSERT OR UPDATE OF fx_rate_id, company_id ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION fx_rate_reference_guard()")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION jobs_parent_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.parent_job_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM jobs p
                WHERE p.id = NEW.parent_job_id AND (p.company_id IS NULL OR p.company_id = NEW.company_id)
            ) THEN
                RAISE EXCEPTION 'A parent job must be a platform job or a job of the same company'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("CREATE TRIGGER trg_jobs_parent_guard BEFORE INSERT OR UPDATE OF parent_job_id, company_id ON jobs "
               "FOR EACH ROW EXECUTE FUNCTION jobs_parent_guard()")


def _install_security(bind) -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    # FX rates: everyone reads platform rates; a tenant reads, inserts and updates only its own; nobody deletes.
    op.execute("ALTER TABLE fx_rates ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY fx_rates_read ON fx_rates FOR SELECT USING (company_id IS NULL OR company_id = {CURRENT_TENANT})")
    op.execute(f"CREATE POLICY fx_rates_insert ON fx_rates FOR INSERT WITH CHECK (company_id = {CURRENT_TENANT})")
    op.execute(f"CREATE POLICY fx_rates_update ON fx_rates FOR UPDATE USING (company_id = {CURRENT_TENANT}) "
               f"WITH CHECK (company_id = {CURRENT_TENANT})")

    grant_app_roles(bind)


def grant_app_roles(bind) -> None:
    """Existing deployments: give the RLS-restricted app role (it can write employees) its reach on the new tables.

    Fresh databases get the same grants from `python -m app.cli setup-db-roles` (grant_app_role).
    """
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in TENANT_TABLES:
            bind.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}"))
            bind.execute(sa.text(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}"))
        for table, privileges in PLATFORM_GRANTS:
            bind.execute(sa.text(f"GRANT {privileges} ON {table} TO {quoted}"))
        for table in PLATFORM_SEQUENCES:
            bind.execute(sa.text(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}"))


def downgrade() -> None:
    for table in ('approval_request_steps', 'approval_requests', 'outbox_events', 'idempotency_keys', 'jobs',
                  'fx_conversions', 'fx_snapshot_rates', 'fx_snapshots', 'fx_rates', 'country_capabilities',
                  'payroll_table_rows', 'payroll_tables', 'policy_assignments', 'policy_versions', 'policies',
                  'setting_change_requests', 'platform_setting_values', 'setting_values'):
        op.drop_table(table)
    for function in ('jobs_parent_guard', 'fx_rate_reference_guard', 'policy_versions_guard', 'payroll_tables_guard',
                     'platform_setting_values_guard', 'setting_values_guard'):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

    op.execute("DELETE FROM approval_delegations WHERE module <> 'leave'")
    op.drop_constraint('ck_delegation_module', 'approval_delegations', type_='check')
    op.create_check_constraint('ck_delegation_module', 'approval_delegations', "module IN ('leave')")

    op.execute(f"DELETE FROM approval_chain_steps WHERE module <> 'leave' OR approver_type NOT IN {OLD_APPROVER_TYPES}")
    for name in ('ck_approval_chain_currency', 'ck_approval_chain_min_amount', 'ck_approval_chain_user_required',
                 'ck_approval_chain_module'):
        op.drop_constraint(name, 'approval_chain_steps', type_='check')
    op.drop_constraint('fk_approval_chain_specific_user', 'approval_chain_steps', type_='foreignkey')
    for column in ('condition', 'min_amount_currency', 'min_amount', 'specific_user_id'):
        op.drop_column('approval_chain_steps', column)
    op.drop_constraint('ck_approval_chain_type', 'approval_chain_steps', type_='check')
    op.create_check_constraint('ck_approval_chain_type', 'approval_chain_steps', f"approver_type IN {OLD_APPROVER_TYPES}")
