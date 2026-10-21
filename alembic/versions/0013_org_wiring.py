"""Organisation wiring for payroll: data fixes needed by pay-group-aware placements.

Data only (no schema change), idempotent:
  * D-4: placements of terminated employees end on the termination date. Primary rows that would
    start after it (a future transfer recorded before the termination) are deleted when an earlier
    row exists, and rows still open after it are closed on it. Before this fix such a row made a
    terminated person "currently placed" from its start date, and a rehire reused it.
  * Rows written by the previous release since 0008 (entities without a pay group, placements
    without a pay group or employment type, branches without a work week or holiday calendar,
    holidays without a calendar) get what 0008 gives them, by re-running its idempotent backfill.

Downgrade changes nothing: the removed rows were placements that never started and cannot be
told apart from intended data afterwards.

Revision ID: 0013_org_wiring
Revises: 0012_security_keys
Create Date: 2026-10-20
"""
import importlib.util
import logging
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0013_org_wiring'
down_revision: Union[str, None] = '0012_security_keys'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.0013")

TERMINATED = """
    FROM employees e
    WHERE e.company_id = a.company_id AND e.id = a.employee_id
      AND NOT e.is_active AND e.termination_date IS NOT NULL
"""


def close_terminated_placements(bind) -> dict:
    """D-4 data fix; returns {"removed": n, "closed": n}."""
    removed = bind.execute(sa.text("""
        DELETE FROM employee_assignments a
        USING employees e
        WHERE e.company_id = a.company_id AND e.id = a.employee_id
          AND NOT e.is_active AND e.termination_date IS NOT NULL
          AND a.effective_from > e.termination_date
          AND EXISTS (SELECT 1 FROM employee_assignments p
                      WHERE p.company_id = a.company_id AND p.employee_id = a.employee_id
                        AND p.effective_from <= e.termination_date)
    """)).rowcount
    closed = bind.execute(sa.text(f"""
        UPDATE employee_assignments a SET effective_to = e.termination_date
        {TERMINATED}
          AND a.effective_from <= e.termination_date
          AND (a.effective_to IS NULL OR a.effective_to > e.termination_date)
    """)).rowcount
    return {"removed": removed, "closed": closed}


def _migration_0008():
    path = Path(__file__).with_name("0008_payroll_org.py")
    spec = importlib.util.spec_from_file_location("migration_0008_payroll_org", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    bind = op.get_bind()
    summary = close_terminated_placements(bind)
    summary.update(_migration_0008().backfill(bind))
    logger.info("Organisation wiring data fixes: %s", summary)


def downgrade() -> None:
    pass
