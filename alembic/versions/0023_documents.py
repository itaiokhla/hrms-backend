"""Placeholder in the migration chain; the owning milestone fills it in.

Revision ID: 0023_documents
Revises: 0022_payments
Create Date: 2026-10-21
"""
from typing import Sequence, Union

revision: str = '0023_documents'
down_revision: Union[str, None] = '0022_payments'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
