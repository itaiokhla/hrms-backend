"""Generic approvals engine support (indexes/constraints beyond 0009).

* approval_request_steps accepts the approver type `permission_holders`: the default chain of a
  module without configured steps is one step "holders of the module's approve permission"
  (app.payroll.services.approvals). Configured chains (approval_chain_steps) keep the 0009 types.
* Partial indexes for the approvals inbox (pending requests per module) and for active
  delegations per delegate and module.

Downgrade turns `permission_holders` steps into steps of the company's administrator role (the
engine's fallback approvers) so no request history is lost, then restores the 0009 constraint.

Revision ID: 0014_approvals_engine
Revises: 0013_org_wiring
Create Date: 2026-10-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0014_approvals_engine'
down_revision: Union[str, None] = '0013_org_wiring'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
APPROVER_TYPES = (
    "primary_manager", "skip_level_manager", "department_head", "role", "finance_role", "specific_user",
)
REQUEST_APPROVER_TYPES = APPROVER_TYPES + ("permission_holders",)


def upgrade() -> None:
    op.drop_constraint('ck_approval_request_steps_type', 'approval_request_steps', type_='check')
    op.create_check_constraint(
        'ck_approval_request_steps_type', 'approval_request_steps', f"approver_type IN {REQUEST_APPROVER_TYPES}"
    )
    op.create_index(
        'ix_approval_requests_pending', 'approval_requests', ['company_id', 'module'],
        postgresql_where="status = 'PENDING'",
    )
    op.create_index(
        'ix_approval_delegations_active', 'approval_delegations', ['company_id', 'delegate_employee_id', 'module'],
        postgresql_where="revoked_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index('ix_approval_delegations_active', table_name='approval_delegations')
    op.drop_index('ix_approval_requests_pending', table_name='approval_requests')
    op.execute(
        "UPDATE approval_request_steps s SET approver_type = 'role', role_id = r.id "
        "FROM roles r WHERE s.approver_type = 'permission_holders' "
        "AND r.company_id = s.company_id AND r.system_key = 'company_admin'"
    )
    op.drop_constraint('ck_approval_request_steps_type', 'approval_request_steps', type_='check')
    op.create_check_constraint(
        'ck_approval_request_steps_type', 'approval_request_steps', f"approver_type IN {APPROVER_TYPES}"
    )
