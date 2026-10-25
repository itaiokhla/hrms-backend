"""Partner operator delegated access: partner_delegations, partner_delegation_targets.

Additive: two tenant tables with row-level security, grants and composite same-company foreign keys
(app.models.partner_delegations documents them). Until a tenant administrator grants a delegation the
tenant's partner has no access to any HR data; existing tenants start with none. The registry gains
`settings:partner_access:manage`; the protected Company Administrator holds it implicitly (full access),
no role row changes.

Downgrade drops both tables (delegations are time-boxed grants; their audit rows stay in audit_logs).

Revision ID: 0030_partner_delegations
Revises: 0029_ot_rate_formula_default
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0030_partner_delegations'
down_revision: Union[str, None] = '0029_ot_rate_formula_default'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
DELEGATIONS, TARGETS = 'partner_delegations', 'partner_delegation_targets'
STATUSES = ("ACTIVE", "REVOKED")
TARGET_KINDS = ("LEGAL_ENTITY", "PAY_GROUP")


def _fk(table, columns, target, name, ondelete=None):
    local = ['company_id', *([columns] if isinstance(columns, str) else columns)]
    return sa.ForeignKeyConstraint(local, [f'{target}.company_id', f'{target}.id'], name=f'fk_{table}_{name}',
                                   ondelete=ondelete)


def _base():
    return [
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    ]


def _secure(bind, table):
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {quoted}")


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        DELEGATIONS,
        *_base(),
        sa.Column('partner_id', sa.Integer(), sa.ForeignKey('partners.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('permission_codes', postgresql.JSONB(), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='ACTIVE'),
        sa.Column('granted_by_user_id', sa.Integer(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_by_user_id', sa.Integer(), nullable=True),
        sa.Column('revoke_reason', sa.String(500), nullable=True),
        sa.UniqueConstraint('company_id', 'id', name=f'uq_{DELEGATIONS}_company_id_id'),
        _fk(DELEGATIONS, 'granted_by_user_id', 'users', 'granted_by'),
        _fk(DELEGATIONS, 'revoked_by_user_id', 'users', 'revoked_by'),
        sa.CheckConstraint(f"status IN {STATUSES}", name=f'ck_{DELEGATIONS}_status'),
        sa.CheckConstraint("jsonb_typeof(permission_codes) = 'array' AND jsonb_array_length(permission_codes) > 0",
                           name=f'ck_{DELEGATIONS}_codes'),
        sa.CheckConstraint("char_length(btrim(reason)) BETWEEN 10 AND 500", name=f'ck_{DELEGATIONS}_reason'),
        sa.CheckConstraint("expires_at > starts_at AND expires_at - starts_at <= interval '30 days'",
                           name=f'ck_{DELEGATIONS}_window'),
        sa.CheckConstraint("(status = 'REVOKED') = (revoked_at IS NOT NULL) AND (status = 'REVOKED') = "
                           "(revoked_by_user_id IS NOT NULL)", name=f'ck_{DELEGATIONS}_revoked'),
    )
    op.create_index(f'ix_{DELEGATIONS}_company_id', DELEGATIONS, ['company_id'])
    op.create_index(f'ix_{DELEGATIONS}_partner_id', DELEGATIONS, ['partner_id'])
    op.create_index(f'ix_{DELEGATIONS}_partner_status', DELEGATIONS, ['partner_id', 'company_id', 'status', 'expires_at'])

    op.create_table(
        TARGETS,
        *_base(),
        sa.Column('delegation_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(12), nullable=False),
        sa.Column('legal_entity_id', sa.Integer(), nullable=True),
        sa.Column('pay_group_id', sa.Integer(), nullable=True),
        _fk(TARGETS, 'delegation_id', DELEGATIONS, 'delegation', ondelete='CASCADE'),
        _fk(TARGETS, 'legal_entity_id', 'legal_entities', 'legal_entity'),
        _fk(TARGETS, 'pay_group_id', 'pay_groups', 'pay_group'),
        sa.CheckConstraint(f"kind IN {TARGET_KINDS}", name=f'ck_{TARGETS}_kind'),
        sa.CheckConstraint(
            "(kind = 'LEGAL_ENTITY' AND legal_entity_id IS NOT NULL AND pay_group_id IS NULL) OR "
            "(kind = 'PAY_GROUP' AND pay_group_id IS NOT NULL AND legal_entity_id IS NULL)",
            name=f'ck_{TARGETS}_one_target'),
    )
    op.create_index(f'ix_{TARGETS}_company_id', TARGETS, ['company_id'])
    op.create_index(f'ix_{TARGETS}_delegation_id', TARGETS, ['delegation_id'])
    op.create_index(f'uq_{TARGETS}_entity', TARGETS, ['delegation_id', 'legal_entity_id'], unique=True,
                    postgresql_where=sa.text("kind = 'LEGAL_ENTITY'"))
    op.create_index(f'uq_{TARGETS}_pay_group', TARGETS, ['delegation_id', 'pay_group_id'], unique=True,
                    postgresql_where=sa.text("kind = 'PAY_GROUP'"))

    for table in (DELEGATIONS, TARGETS):
        _secure(bind, table)


def downgrade() -> None:
    op.drop_table(TARGETS)
    op.drop_table(DELEGATIONS)
