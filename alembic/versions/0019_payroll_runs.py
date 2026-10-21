"""Placeholder in the migration chain; the owning milestone fills it in.

Revision ID: 0019_payroll_runs
Revises: 0018_variable_inputs
Create Date: 2026-10-21
"""
from typing import Sequence, Union

revision: str = '0019_payroll_runs'
down_revision: Union[str, None] = '0018_variable_inputs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
