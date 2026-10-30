"""Roster publication approval (shift module; 03-shifts/02 §5.2; spine §4.1; owner decision IG-AS-150).

* Approval engine: module ``roster_publish`` on the module CHECKs of chains, requests, delegations and routes
  (approve code ``roster:publish``; approvers cover every employee of the publication, never the maker).
* ``roster_periods.approval_request_id`` gets its tenant-composite FK to ``approval_requests`` (column since 0045).

Downgrade refuses while ``roster_publish`` approval requests exist.

Revision ID: 0062_roster_publish_approval
Revises: 0061_roster_draft_author
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0062_roster_publish_approval"
down_revision: Union[str, None] = "0061_roster_draft_author"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_MODULES = ("leave", "payroll_run", "compensation", "wage_input", "overtime", "task", "reimbursement", "loan",
               "bank_change", "payment_batch", "off_cycle", "final_settlement", "lop", "attendance", "settings",
               "timesheet", "shift_request", "team_inputs")
NEW_MODULES = OLD_MODULES + ("roster_publish",)
MODULE_CHECKS = (("approval_chain_steps", "ck_approval_chain_module"),
                 ("approval_requests", "ck_approval_requests_module"),
                 ("approval_delegations", "ck_delegation_module"),
                 ("approval_routes", "ck_approval_routes_module"))


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _widen(table: str, name: str, values) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"module IN {_in(values)}")


def upgrade() -> None:
    for table, name in MODULE_CHECKS:
        _widen(table, name, NEW_MODULES)
    op.execute("ALTER TABLE roster_periods ADD CONSTRAINT fk_roster_periods_approval FOREIGN KEY "
               "(company_id, approval_request_id) REFERENCES approval_requests (company_id, id) ON DELETE RESTRICT")


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text("SELECT count(*) FROM approval_requests WHERE module = 'roster_publish'")).scalar()
    if used:
        raise RuntimeError("Roster publication approvals exist; refusing to remove the roster_publish module.")
    op.execute("ALTER TABLE roster_periods DROP CONSTRAINT IF EXISTS fk_roster_periods_approval")
    for table, name in MODULE_CHECKS:
        _widen(table, name, OLD_MODULES)
