"""Leave v2 gate and settings reads (LH 13 §2.3, SPINE §10).

``leave.v2.enabled`` (T, written only by the leave cut-over through ``set_v2_enabled``) decides per tenant whether
balances come from the ledger and plans (V2_ACTIVE) or from the legacy yearly quota (LEGACY_V1). GLOBAL features
(type editor, plan builder, preview, change requests, discussion) work for both; V2 features answer 404 while off.
"""

from datetime import date
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import not_found

V2_KEY = "leave.v2.enabled"
_MEMO = "leave_v2_enabled"


def v2_enabled(db: Session, company_id: int) -> bool:
    memo = db.info.setdefault(_MEMO, {})
    if company_id not in memo:
        memo[company_id] = bool(setting(db, company_id, V2_KEY, default=False))
    return memo[company_id]


def require_v2(db: Session, company_id: int) -> None:
    if not v2_enabled(db, company_id):
        raise not_found()


def forget(db: Session) -> None:
    """Drop the per-session memo (after the cut-over or its rollback)."""
    db.info.pop(_MEMO, None)


def setting(db: Session, company_id: int, key: str, *, on: Optional[date] = None, legal_entity_id: Optional[int] = None,
            branch_id: Optional[int] = None, employee_id: Optional[int] = None, default: Any = None) -> Any:
    """The resolved value of a leave setting; ``default`` when it is unset or invalid (fails closed elsewhere)."""
    from app.payroll.services import settings as settings_service

    try:
        resolved = settings_service.resolve(db, company_id, key, on=on or clock.company_today(db, company_id),
                                            legal_entity_id=legal_entity_id, branch_id=branch_id,
                                            employee_id=employee_id)
    except Exception:  # an invalid stored value: the caller's safe default
        return default
    value = getattr(resolved, "value", None)
    if value is None or type(value).__name__ == "_Unset":
        return default
    return value


def set_v2_enabled(db: Session, principal, enabled: bool, *, reason: str) -> dict:
    """The only writer of ``leave.v2.enabled`` (the settings API refuses the key): the cut-over and its rollback."""
    from app.payroll.services import settings as settings_service

    result = settings_service.write_tenant_value(db, principal, V2_KEY, bool(enabled), reason=reason)
    forget(db)
    return result
