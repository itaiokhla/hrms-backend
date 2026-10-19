"""Single-use TOTP codes: remember the last accepted time step per account.

Additive and backwards compatible: two nullable columns, no data rewrite, no
locks beyond a brief catalog update. Existing sessions and enrolments keep
working; the first code accepted after deploy starts the guard.

Revision ID: 0002_totp_replay_guard
Revises: 0001_saas_baseline
Create Date: 2026-10-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002_totp_replay_guard'
down_revision: Union[str, None] = '0001_saas_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('mfa_last_step', sa.Integer(), nullable=True))
    op.add_column('platform_admins', sa.Column('mfa_last_step', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('platform_admins', 'mfa_last_step')
    op.drop_column('users', 'mfa_last_step')
