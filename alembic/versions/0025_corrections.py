"""Placeholder in the migration chain; the owning milestone fills it in.

Revision ID: 0025_corrections
Revises: 0024_reports_compliance
Create Date: 2026-10-21
"""
from typing import Sequence, Union

revision: str = '0025_corrections'
down_revision: Union[str, None] = '0024_reports_compliance'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
