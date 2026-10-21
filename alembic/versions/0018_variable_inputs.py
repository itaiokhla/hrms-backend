"""Placeholder in the migration chain; the owning milestone fills it in.

Revision ID: 0018_variable_inputs
Revises: 0017_overtime_tasks
Create Date: 2026-10-21
"""
from typing import Sequence, Union

revision: str = '0018_variable_inputs'
down_revision: Union[str, None] = '0017_overtime_tasks'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
