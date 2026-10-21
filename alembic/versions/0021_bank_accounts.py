"""Placeholder in the migration chain; the owning milestone fills it in.

Revision ID: 0021_bank_accounts
Revises: 0020_runs_extensions
Create Date: 2026-10-21
"""
from typing import Sequence, Union

revision: str = '0021_bank_accounts'
down_revision: Union[str, None] = '0020_runs_extensions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
