"""Overtime rate formula default: stored DRAFT overtime policies still holding the old default text (F-61, F-64).

Registry 2026.10.3 shipped the overtime hourly rate as
``SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day``. That divides every OT_BASE rate by
the period's basis days and standard hours, so an hourly worker's per-hour rate (RATE_X_UNITS per hour) was divided
again. On 23 Oct the default became ``SUM_TAG_HOURLY("OT_BASE")`` (FOLLOW-UPS F-61), which converts each rate by its
unit (identical for period amounts, correct for per-day and per-hour rates).

Migration 0011 (LEGACY_V1, frozen literals) and the STARTER_V1 profile at signup stored the old text in the catch-all
overtime policy, a DRAFT (overtime is OFF in both profiles). Migration 0011 is history and is not edited; this
revision updates the stored rows instead:

* only DRAFT versions of OVERTIME_POLICY whose ``rate_formula`` is exactly the old default text: drafts are edited
  in place (3.1 §3.2) and were never used by a calculation;
* a customised formula (any other text, even one differing in white space) is never touched;
* ACTIVE and SUPERSEDED versions are immutable (trigger policy_versions_guard) and frozen into run snapshots by
  their hash, so they are left alone: the engine warns OT_BASE_UNITS_MIXED when such a formula mixes units, and the
  tenant activates a new version to change it.

Each affected tenant gets one "Payroll Settings" POLICY_DEFAULT_UPDATED audit row (details.migration =
0029_ot_rate_formula_default, with the version ids). Idempotent: a second run finds nothing to change. downgrade()
restores the old text on the versions this revision changed that are still DRAFT and still hold the new default,
and writes a POLICY_DEFAULT_REVERTED row.

Revision ID: 0029_ot_rate_formula_default
Revises: 0028_task_assignee_paid
Create Date: 2026-10-24
"""
import json
import logging
from typing import Dict, List, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0029_ot_rate_formula_default'
down_revision: Union[str, None] = '0028_task_assignee_paid'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.0029")

MARKER = "0029_ot_rate_formula_default"
MODULE = "Payroll Settings"
APPLIED = "POLICY_DEFAULT_UPDATED"
REVERTED = "POLICY_DEFAULT_REVERTED"
POLICY_TYPE = "OVERTIME_POLICY"
# Frozen literals: later registry changes must not alter what this revision does.
OLD_FORMULA = 'SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day'
NEW_FORMULA = 'SUM_TAG_HOURLY("OT_BASE")'


def _audit(bind, company_id: int, action: str, description: str, details: dict) -> None:
    bind.execute(sa.text(
        """
        INSERT INTO audit_logs (company_id, actor_type, actor_email, module, action, entity_type, entity_id,
                                description, details, created_at, updated_at)
        VALUES (:cid, 'system', 'migration 0029', :module, :action, 'company', :eid, :description,
                CAST(:details AS jsonb), now(), now())
        """
    ), {"cid": company_id, "module": MODULE, "action": action, "eid": str(company_id),
        "description": description[:500], "details": json.dumps(dict(details, migration=MARKER))})


def _swap(bind, company_id: int, version_ids: List[int], old: str, new: str) -> List[int]:
    """Set rate_formula old -> new on the given DRAFT overtime versions that still hold exactly ``old``."""
    if not version_ids:
        return []
    rows = bind.execute(sa.text(
        """
        UPDATE policy_versions v SET fields = jsonb_set(v.fields, '{rate_formula}', to_jsonb(CAST(:new AS text))),
                                     updated_at = now()
        FROM policies p
        WHERE p.company_id = v.company_id AND p.id = v.policy_id AND p.type = :ptype
          AND v.company_id = :cid AND v.id IN :ids AND v.status = 'DRAFT'
          AND v.fields->>'rate_formula' = :old
        RETURNING v.id
        """
    ).bindparams(sa.bindparam("ids", expanding=True)),
        {"cid": company_id, "ids": version_ids, "ptype": POLICY_TYPE, "old": old, "new": new}).all()
    return sorted(r[0] for r in rows)


def apply(bind) -> Dict[int, dict]:
    """Update every DRAFT overtime version holding exactly the old default; returns {company_id: stats}."""
    candidates = bind.execute(sa.text(
        """
        SELECT v.company_id, v.id FROM policy_versions v
        JOIN policies p ON p.company_id = v.company_id AND p.id = v.policy_id
        WHERE p.type = :ptype AND v.status = 'DRAFT' AND v.fields->>'rate_formula' = :old
        ORDER BY v.company_id, v.id
        """
    ), {"ptype": POLICY_TYPE, "old": OLD_FORMULA}).all()
    by_company: Dict[int, List[int]] = {}
    for company_id, version_id in candidates:
        by_company.setdefault(company_id, []).append(version_id)
    stats: Dict[int, dict] = {}
    for company_id, ids in by_company.items():
        changed = _swap(bind, company_id, ids, OLD_FORMULA, NEW_FORMULA)
        if not changed:
            continue
        stats[company_id] = {"policy_versions": changed, "from": OLD_FORMULA, "to": NEW_FORMULA}
        _audit(bind, company_id, APPLIED,
               f"Updated the overtime rate formula of {len(changed)} draft overtime policy version(s) from the old "
               f"default to {NEW_FORMULA} (migration 0029; customised formulas and activated versions unchanged)",
               stats[company_id])
    held = bind.execute(sa.text(
        """
        SELECT count(*) FROM policy_versions v
        JOIN policies p ON p.company_id = v.company_id AND p.id = v.policy_id
        WHERE p.type = :ptype AND v.status <> 'DRAFT' AND v.fields->>'rate_formula' = :old
        """
    ), {"ptype": POLICY_TYPE, "old": OLD_FORMULA}).scalar() or 0
    if held:
        logger.warning("%s activated overtime policy version(s) keep the old default formula (immutable); "
                       "the engine warns OT_BASE_UNITS_MIXED where units are mixed", held)
    return stats


def revert(bind) -> Dict[int, dict]:
    """Restore the old text on the versions changed since the last revert that still hold the new default."""
    companies = bind.execute(sa.text(
        "SELECT DISTINCT company_id FROM audit_logs WHERE action = :a AND details->>'migration' = :m ORDER BY 1"
    ), {"a": APPLIED, "m": MARKER}).all()
    stats: Dict[int, dict] = {}
    for (company_id,) in companies:
        last_revert = bind.execute(sa.text(
            "SELECT max(id) FROM audit_logs WHERE company_id = :cid AND action = :a AND details->>'migration' = :m"
        ), {"cid": company_id, "a": REVERTED, "m": MARKER}).scalar() or 0
        rows = bind.execute(sa.text(
            "SELECT details FROM audit_logs WHERE company_id = :cid AND id > :after AND action = :a "
            "AND details->>'migration' = :m ORDER BY id"
        ), {"cid": company_id, "after": last_revert, "a": APPLIED, "m": MARKER}).all()
        ids = sorted({i for (details,) in rows for i in details.get("policy_versions", [])})
        if not ids:
            continue
        restored = _swap(bind, company_id, ids, NEW_FORMULA, OLD_FORMULA)
        stats[company_id] = {"policy_versions": restored}
        _audit(bind, company_id, REVERTED,
               f"Restored the old default overtime rate formula on {len(restored)} draft version(s) "
               f"(migration 0029 downgrade)", stats[company_id])
    return stats


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    revert(op.get_bind())
