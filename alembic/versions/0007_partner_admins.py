"""Partner administrators: platform_admins.partner_id.

An operator account with partner_id set is a partner administrator (partner console
only, scoped to that partner); without it, a platform operator as before. Additive:
existing operators keep partner_id NULL.

Downgrade deletes partner administrator accounts (their sessions cascade).

Revision ID: 0007_partner_admins
Revises: 0006_white_label
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0007_partner_admins'
down_revision: Union[str, None] = '0006_white_label'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('platform_admins', sa.Column('partner_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_platform_admins_partner', 'platform_admins', 'partners', ['partner_id'], ['id'],
                          ondelete='RESTRICT')
    op.create_index('ix_platform_admins_partner_id', 'platform_admins', ['partner_id'])


def downgrade() -> None:
    op.execute("DELETE FROM platform_admins WHERE partner_id IS NOT NULL")
    op.drop_index('ix_platform_admins_partner_id', table_name='platform_admins')
    op.drop_constraint('fk_platform_admins_partner', 'platform_admins', type_='foreignkey')
    op.drop_column('platform_admins', 'partner_id')
