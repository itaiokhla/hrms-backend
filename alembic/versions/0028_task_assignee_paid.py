"""Per-assignee task payment: task_assignees.paid_in_run_id (EC-TSK-*, EC-VAR-01).

A task shared by several assignees used to carry one paid marker (tasks.paid_in_run_id) while its line sources
were keyed '' at calculation and 'employee:<id>' at finalization. Assignees in different pay groups are paid by
different runs, so the second run either never saw the task or collided with the first run's consumption. Each
assignee's share is now paid exactly once by its own run:

* task_assignees.paid_in_run_id: the run that paid this assignee's share (not a foreign key, like
  tasks.paid_in_run_id); the task turns PAID once every share is paid;
* backfill (fail closed, never pays twice): an assignee whose consumed TASK line source names it gets that run;
  every other assignee of a PAID or REVERSED task gets the task's paid_in_run_id (the old state said "paid");
* unconsumed TASK line sources of runs that are not finalized are re-keyed 'employee:<id>' (consumed sources
  of finalized runs are immutable and keep '').

Downgrade drops the column (line source keys stay re-keyed; finalization accepts both).

Revision ID: 0028_task_assignee_paid
Revises: 0027_payslip_queries
Create Date: 2026-10-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0028_task_assignee_paid'
down_revision: Union[str, None] = '0027_payslip_queries'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy: later model changes must not alter what this revision does.
_FINALIZED_SQL = "('FINALIZED', 'PAYMENT_READY', 'SUBMITTED', 'PARTIALLY_PAID', 'PAID', 'RECONCILED', 'CLOSED')"


def upgrade() -> None:
    op.add_column('task_assignees', sa.Column('paid_in_run_id', sa.Integer(), nullable=True))
    op.create_index('ix_task_assignees_paid_in_run_id', 'task_assignees', ['company_id', 'paid_in_run_id'])
    op.execute("""
        UPDATE task_assignees ta SET paid_in_run_id = paid.run_id
        FROM (SELECT DISTINCT ON (s.company_id, s.source_id, e.employee_id)
                     s.company_id, s.source_id AS task_id, e.employee_id, s.run_id
              FROM payroll_line_sources s
              JOIN payroll_run_employees e ON e.company_id = s.company_id AND e.id = s.run_employee_id
              WHERE s.source_type = 'TASK' AND s.consumed
              ORDER BY s.company_id, s.source_id, e.employee_id, s.id) paid
        WHERE ta.company_id = paid.company_id AND ta.task_id = paid.task_id AND ta.employee_id = paid.employee_id
    """)
    op.execute("""
        UPDATE task_assignees ta SET paid_in_run_id = t.paid_in_run_id
        FROM tasks t
        WHERE t.company_id = ta.company_id AND t.id = ta.task_id AND t.paid_in_run_id IS NOT NULL
          AND ta.paid_in_run_id IS NULL
    """)
    op.execute(f"""
        UPDATE payroll_line_sources s SET source_key = 'employee:' || e.employee_id
        FROM payroll_run_employees e
        WHERE e.company_id = s.company_id AND e.id = s.run_employee_id
          AND s.source_type = 'TASK' AND s.source_key = '' AND NOT s.consumed
          AND NOT EXISTS (SELECT 1 FROM payroll_runs r WHERE r.id = s.run_id AND r.status IN {_FINALIZED_SQL})
    """)


def downgrade() -> None:
    op.drop_index('ix_task_assignees_paid_in_run_id', table_name='task_assignees')
    op.drop_column('task_assignees', 'paid_in_run_id')
