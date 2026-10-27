"""AS-M0 baseline (AS 11 §11.2, AS 14 §0.5): marker of the generic explicit-only permission mechanism (spine A-09).

No attendance DDL and no new permission code. The registry now names the mechanism (``PermissionDef.explicit_only``;
``payroll_confidential`` = explicit-only codes of the Payroll modules, the same set as before), so the revision only
records it in the catalogue: a comment on ``role_permissions`` stating that explicit-only grants are never implied
by full access and must be held as rows. It fixes the chain position of AS-M0 between LH-M0 and X-M3 (spine §15.5).

Downgrade restores the previous (absent) comment.

Revision ID: 0035_as_m0_baseline
Revises: 0034_lh_m0_baseline
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0035_as_m0_baseline"
down_revision: Union[str, None] = "0034_lh_m0_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MARKER = ("Grants of roles. Explicit-only codes (permission_registry.PermissionDef.explicit_only: payroll-confidential "
          "and later attendance/leave codes) are never implied by full access and exist only as rows here "
          "(AS-M0, spine A-09).")


def upgrade() -> None:
    op.execute(f"COMMENT ON TABLE role_permissions IS '{MARKER}'")


def downgrade() -> None:
    op.execute("COMMENT ON TABLE role_permissions IS NULL")
