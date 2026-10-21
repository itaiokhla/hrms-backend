"""Configuration profiles: materialize STARTER_V1 or LEGACY_V1 for one tenant (3.1 §9, decisions I-13, I-14).

A profile writes explicit TENANT rows for every TENANT-level key (``source`` STARTER_PROFILE or
LEGACY_MIGRATION, ``profile_version``, the profile's locks) and its catch-all starter policies
(ACTIVE where the profile turns a gate switch ON, DRAFT otherwise; LEAVE_PAY_POLICY rows from the
tenant's leave types), so later registry or partner default changes never alter the tenant (E5).
Statutory schemes are never switched ON and nothing is scheduled. Applying a profile again
changes nothing: equal rows are kept and policy types that already have a policy are skipped.
Existing tenants receive LEGACY_V1 from migration 0011 (embedded literals, same content).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.errors import bad_request
from app.core.principal import Principal
from app.core.security import utcnow
from app.models.company import Company
from app.models.leave import LeaveType
from app.models.payroll_config import Policy, SettingValue
from app.payroll import audit as payroll_audit
from app.payroll import policy_types as pt
from app.payroll import settings_registry as reg
from app.payroll.services import policies as policy_service
from app.payroll.services import settings as engine
from app.services.audit_log_service import AuditLogService

PROFILE_APPLIED = "PROFILE_APPLIED"


def leave_types_of(db: Session, company_id: int) -> list[dict[str, Any]]:
    rows = (db.query(LeaveType).filter(LeaveType.company_id == company_id, LeaveType.is_active.is_(True))
            .order_by(LeaveType.id))
    return [{"code": lt.code, "is_paid": bool(lt.is_paid), "name": lt.name} for lt in rows]


def apply_profile(
    db: Session,
    company: Company,
    name: str,
    *,
    effective_from: date | None = None,
    legacy_values: Mapping[str, Any] | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Materialize profile ``name`` for ``company`` from ``effective_from`` (default: the company's today)."""
    try:
        profile = reg.PROFILES[name]
        leave = leave_types_of(db, company.id)
        rows = reg.profile_rows(name, leave_types=leave, legacy_values=legacy_values)
        starters = pt.starter_policies(name, leave_types=leave, legacy_values=legacy_values)
    except KeyError:
        raise bad_request(f"{name!r} is not a configuration profile.", "UNKNOWN_PROFILE") from None
    except (reg.SettingValidationError, pt.PolicyValidationError) as error:
        raise bad_request(error.message, error.code) from None
    start = effective_from or engine.company_today(company)
    current = {
        r.key: r for r in db.query(SettingValue).filter(
            SettingValue.company_id == company.id, SettingValue.scope_type == "TENANT",
            SettingValue.status == "ACTIVE", SettingValue.effective_from <= start,
            or_(SettingValue.effective_to.is_(None), SettingValue.effective_to >= start),
        )
    }
    written = unchanged = 0
    for row in rows:
        existing = current.get(row.key)
        if existing is not None and existing.value == row.value and bool(existing.locked) == row.locked:
            unchanged += 1
            continue
        engine.materialize_row(db, company.id, key=row.key, value=row.value, locked=row.locked,
                               effective_from=start, source=row.source, profile_version=row.profile_version,
                               reason=row.reason)
        written += 1
    taken = {t for (t,) in db.query(Policy.type).filter(Policy.company_id == company.id, Policy.status == "ACTIVE")}
    created: list[dict[str, Any]] = []
    for starter in starters:
        if starter.type in taken:
            continue
        policy = policy_service.create_starter_policy(db, company.id, starter, effective_from=start)
        taken.add(starter.type)
        created.append({"id": policy.id, "type": starter.type, "name": starter.name, "status": starter.status,
                        "decision_required": starter.decision_required, "review_items": list(starter.review_items)})
    company.payroll_profile_version = profile.name
    company.payroll_profile_applied_at = utcnow()
    db.flush()
    summary = {
        "profile": profile.name,
        "source": profile.source,
        "effective_from": start,
        "rows_written": written,
        "rows_unchanged": unchanged,
        "locks": sorted(profile.locks),
        "policies": created,
        "decisions": sorted(profile.decisions),
        "acknowledgments": list(profile.acknowledgments),
        "legacy_values": dict(legacy_values or {}),
    }
    AuditLogService.log(
        db, principal, payroll_audit.PAYROLL_SETTINGS, PROFILE_APPLIED,
        f"Applied the payroll configuration profile {profile.name}: {written} settings and {len(created)} policies "
        f"from {start.isoformat()} ({profile.reason})",
        "company", company.id, company_id=company.id, actor_type="user" if principal is not None else "system",
        details=summary,
    )
    return summary | {"effective_from": start.isoformat()}
