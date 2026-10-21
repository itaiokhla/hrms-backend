"""Security foundations: per-tenant data keys for envelope encryption, step-up authentication timestamps.

Additive and backwards compatible (3.7 §3.2, §12; 3.9 §9; decision I-10):
  * tenant_data_keys: a tenant's AES-256 data keys, stored only wrapped by the platform master key
    (PAYROLL_MASTER_KEY); one ACTIVE key per purpose, RETIRED keys keep decrypting older values;
  * step_up_challenges: emailed one-time step-up codes for users without MFA, bound to one session;
  * user_sessions.step_up_at / step_up_method: the last successful step-up of a session.

Both new tables are tenant tables with row-level security, composite same-company foreign keys and the
application-role grants of 0005. Nothing is backfilled: data keys are created on first use.

Revision ID: 0012_security_keys
Revises: 0011_settings_profiles
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0012_security_keys'
down_revision: Union[str, None] = '0011_settings_profiles'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
NEW_TABLES = ("tenant_data_keys", "step_up_challenges")


def _tenant(table: str):
    return [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{table}_company_id_id'),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        'tenant_data_keys',
        sa.Column('purpose', sa.String(40), nullable=False),
        sa.Column('key_id', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('kek_id', sa.String(64), nullable=False),
        sa.Column('wrapped_key', sa.Text(), nullable=False),
        sa.Column('algorithm', sa.String(20), nullable=False, server_default='AES-256-GCM'),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
        *_tenant('tenant_data_keys'),
        sa.UniqueConstraint('company_id', 'key_id', name='uq_tenant_data_keys_key_id'),
        sa.UniqueConstraint('company_id', 'purpose', 'version', name='uq_tenant_data_keys_version'),
        sa.CheckConstraint("status IN ('ACTIVE', 'RETIRED')", name='ck_tenant_data_keys_status'),
        sa.CheckConstraint("purpose IN ('PAYROLL_PII')", name='ck_tenant_data_keys_purpose'),
        sa.CheckConstraint('version >= 1', name='ck_tenant_data_keys_version'),
        sa.CheckConstraint("algorithm = 'AES-256-GCM'", name='ck_tenant_data_keys_algorithm'),
    )
    op.create_index('ix_tenant_data_keys_company_id', 'tenant_data_keys', ['company_id'])
    op.create_index('uq_tenant_data_keys_active', 'tenant_data_keys', ['company_id', 'purpose'], unique=True,
                    postgresql_where=sa.text("status = 'ACTIVE'"))

    op.create_table(
        'step_up_challenges',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.String(36), nullable=False),
        sa.Column('code_hash', sa.String(64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        *_tenant('step_up_challenges'),
        sa.ForeignKeyConstraint(['company_id', 'user_id'], ['users.company_id', 'users.id'], ondelete='CASCADE',
                                name='fk_step_up_challenges_user'),
        sa.CheckConstraint('attempts >= 0', name='ck_step_up_challenges_attempts'),
    )
    op.create_index('ix_step_up_challenges_company_id', 'step_up_challenges', ['company_id'])
    op.create_index('ix_step_up_challenges_user_id', 'step_up_challenges', ['user_id'])
    op.create_index('ix_step_up_challenges_session', 'step_up_challenges', ['company_id', 'session_id'],
                    postgresql_where=sa.text('consumed_at IS NULL'))

    op.add_column('user_sessions', sa.Column('step_up_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('user_sessions', sa.Column('step_up_method', sa.String(20), nullable=True))
    op.create_check_constraint('ck_user_sessions_step_up_method', 'user_sessions',
                               "step_up_method IS NULL OR step_up_method IN ('TOTP', 'EMAIL_OTP')")

    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    grant_app_roles(bind)


def grant_app_roles(bind) -> None:
    """Existing deployments: the RLS-restricted app role (it can write employees) gets the new tables.

    Fresh databases get the same grants from `python -m app.cli setup-db-roles` (grant_app_role).
    """
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        for table in NEW_TABLES:
            bind.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}"))
            bind.execute(sa.text(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}"))


def downgrade() -> None:
    bind = op.get_bind()
    wrapped = bind.execute(sa.text("SELECT count(*) FROM tenant_data_keys")).scalar()
    if wrapped:
        # Dropping data keys makes every value they encrypted unreadable (crypto-shredding).
        raise RuntimeError(
            f"Refusing to drop tenant_data_keys: {wrapped} data key(s) still protect encrypted payroll data. "
            "Decrypt or migrate the data first."
        )
    op.drop_constraint('ck_user_sessions_step_up_method', 'user_sessions', type_='check')
    op.drop_column('user_sessions', 'step_up_method')
    op.drop_column('user_sessions', 'step_up_at')
    op.drop_table('step_up_challenges')
    op.drop_table('tenant_data_keys')
