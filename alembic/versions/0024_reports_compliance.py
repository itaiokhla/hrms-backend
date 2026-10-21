"""Placeholder in the migration chain; the owning milestone fills it in.

Revision ID: 0024_reports_compliance
Revises: 0023_documents
Create Date: 2026-10-21
"""
from typing import Sequence, Union

revision: str = '0024_reports_compliance'
down_revision: Union[str, None] = '0023_documents'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
