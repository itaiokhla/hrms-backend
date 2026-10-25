"""In-app notification of the people who hold a payroll permission for a legal entity.

``notify_holders`` creates one in-app notification per active user who holds ``permission`` at a scope covering
the legal entity (or pay group), never for ``exclude_user_id`` (the person who caused the event). Texts carry no
amounts and no personal data: callers pass a neutral title and message and a relative link. Services take
``(db, ...)`` and never commit.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from app.core.principal import build_principal
from app.models.company import Company
from app.models.user import User
from app.payroll.services import access
from app.payroll.services.approvals import _permission_holder_user_ids
from app.services.notification_service import NotificationService


def holder_user_ids(db: Session, company_id: int, permission: str, legal_entity_id: int,
                    pay_group_id: Optional[int] = None) -> List[int]:
    """Active users of the tenant whose scope for ``permission`` covers the entity / pay group."""
    company = db.get(Company, company_id)
    found: List[int] = []
    for user_id in _permission_holder_user_ids(db, permission):
        user = db.query(User).filter(User.company_id == company_id, User.id == user_id).one_or_none()
        if user is None or user.status != "active":
            continue
        principal = build_principal(db, user, None, company)
        if principal.has(permission) and access.target_scope(principal, permission).covers(legal_entity_id,
                                                                                           pay_group_id):
            found.append(user_id)
    return sorted(set(found))


def notify_holders(db: Session, company_id: int, permission: str, legal_entity_id: int, title: str, message: str,
                   link: Optional[str] = None, *, pay_group_id: Optional[int] = None,
                   exclude_user_id: Optional[int] = None, notification_type: str = "info") -> int:
    count = 0
    for user_id in holder_user_ids(db, company_id, permission, legal_entity_id, pay_group_id):
        if user_id == exclude_user_id:
            continue
        NotificationService.create(db, user_id, title, message, notification_type, link)
        count += 1
    return count


def notify_users(db: Session, user_ids: Iterable[Optional[int]], title: str, message: str,
                 link: Optional[str] = None, notification_type: str = "info") -> int:
    count = 0
    for user_id in sorted({u for u in user_ids if u is not None}):
        NotificationService.create(db, user_id, title, message, notification_type, link)
        count += 1
    return count


__all__ = ["holder_user_ids", "notify_holders", "notify_users"]
