"""Placeholder in the migration chain; the owning milestone fills it in.

Revision ID: 0016_time_inputs
Revises: 0015_compensation_v2
Create Date: 2026-10-21
"""
from typing import Sequence, Union

revision: str = '0016_time_inputs'
down_revision: Union[str, None] = '0015_compensation_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
