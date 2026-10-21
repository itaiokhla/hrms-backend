"""Placeholder in the migration chain; the owning milestone fills it in.

Revision ID: 0022_payments
Revises: 0021_bank_accounts
Create Date: 2026-10-21
"""
from typing import Sequence, Union

revision: str = '0022_payments'
down_revision: Union[str, None] = '0021_bank_accounts'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
