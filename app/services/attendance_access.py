"""Per-tenant attendance v2 gate (spine §15.7, D-AS-83; AS 14 IG-AS-08).

``attendance.v2.enabled`` is set by the cut-over job (AS-M3, ``attendance.v2_cutover``). Until the settings registry
defines the key every tenant is a v1 tenant: punches are still logged (shadow input), one session per shift date
stays the rule, and v2-only endpoints answer 404.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.errors import not_found

V2_KEY = "attendance.v2.enabled"


def v2_enabled(db: Session, company_id: int) -> bool:
    from app.core import clock
    from app.payroll import settings_registry as reg

    if V2_KEY not in reg.REGISTRY:
        return False
    from app.payroll.services import settings as settings_service

    resolved = settings_service.resolve(db, company_id, V2_KEY, on=clock.company_today(db, company_id))
    return bool(getattr(resolved, "value", False))


def require_v2(db: Session, company_id: int) -> None:
    """First line of every v2-only handler: 404 for v1 tenants (spine §8.1)."""
    if not v2_enabled(db, company_id):
        raise not_found()


def set_v2_enabled(db: Session, principal, enabled: bool, *, reason: str) -> dict:
    """The only writer of ``attendance.v2.enabled`` (the settings API refuses the key, ``ui.system_written``): the
    cut-over and its rollback (spine §15.1) call this after their own checks. Does not commit."""
    from app.payroll.services import settings as settings_service

    return settings_service.write_tenant_value(db, principal, V2_KEY, bool(enabled), reason=reason)
