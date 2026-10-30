"""Roster draft author (shift module S2; spine roster_drafts ``created_by_user_id``; EC AS-ROS-11).

Additive: ``roster_drafts.created_by_user_id INT NULL`` with the tenant-composite FK to ``users`` — the user who last
wrote the draft cell. The SoD setting ``shift.roster.planner_cannot_publish`` refuses a publication by that user
(403 ``SOD_PLANNER_PUBLISH``). Existing drafts keep NULL (no author known: they never block a publisher).

Revision ID: 0061_roster_draft_author
Revises: 0060_roster_rotation_generate
Create Date: 2026-10-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0061_roster_draft_author"
down_revision: Union[str, None] = "0060_roster_rotation_generate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE roster_drafts ADD COLUMN created_by_user_id INTEGER")
    op.execute("ALTER TABLE roster_drafts ADD CONSTRAINT fk_roster_drafts_created_by FOREIGN KEY "
               "(company_id, created_by_user_id) REFERENCES users (company_id, id) ON DELETE RESTRICT")


def downgrade() -> None:
    op.execute("ALTER TABLE roster_drafts DROP CONSTRAINT IF EXISTS fk_roster_drafts_created_by")
    op.execute("ALTER TABLE roster_drafts DROP COLUMN IF EXISTS created_by_user_id")
