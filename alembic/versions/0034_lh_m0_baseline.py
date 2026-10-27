"""LH-M0 baseline (LH order 1; LH 11 §11.2, LH 14 WI-0.5): request correlation on audit rows.

Additive: ``audit_logs.request_id`` VARCHAR(64) and ``audit_logs.user_agent`` VARCHAR(300), both NULL (older rows and
rows written by jobs keep NULL). The application role already has INSERT/SELECT on audit_logs and never UPDATE.

Downgrade drops the two columns (their values are correlation data only).

Revision ID: 0034_lh_m0_baseline
Revises: 0033_tax_profiles_unclaimed
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034_lh_m0_baseline"
down_revision: Union[str, None] = "0033_tax_profiles_unclaimed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("request_id", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("user_agent", sa.String(300), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_logs", "user_agent")
    op.drop_column("audit_logs", "request_id")
