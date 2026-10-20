"""One attendance record per employee per day, enforced by the database.

Self check-in, the kiosk and manual corrections already keep a single record
per person per day; this unique constraint also covers concurrent requests.
If duplicate days already exist the upgrade stops and changes nothing, so the
records can be reviewed and merged instead of being deleted silently. On a
large live table, build the index CONCURRENTLY before running this.

Revision ID: 0003_attendance_one_per_day
Revises: 0002_totp_replay_guard
Create Date: 2026-10-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003_attendance_one_per_day'
down_revision: Union[str, None] = '0002_totp_replay_guard'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    duplicates = op.get_bind().execute(sa.text(
        "SELECT company_id, employee_id, date, count(*) AS records FROM attendance "
        "GROUP BY company_id, employee_id, date HAVING count(*) > 1 "
        "ORDER BY company_id, employee_id, date LIMIT 10"
    )).fetchall()
    if duplicates:
        listed = "; ".join(
            f"company {d.company_id}, employee {d.employee_id}, {d.date}: {d.records} records" for d in duplicates
        )
        raise RuntimeError(f"Merge duplicate attendance days before upgrading (first {len(duplicates)}): {listed}")
    op.create_unique_constraint('uq_attendance_employee_day', 'attendance', ['company_id', 'employee_id', 'date'])


def downgrade() -> None:
    op.drop_constraint('uq_attendance_employee_day', 'attendance', type_='unique')
