import re
from typing import Optional

from sqlalchemy.orm import Session

from app.core.errors import not_found
from app.models.notification import Notification

_SAFE_PATH = re.compile(r"^/[A-Za-z0-9/_\-?=&.]*$")


class NotificationService:
    @staticmethod
    def create(
        db: Session,
        user_id: Optional[int],
        title: str,
        message: str,
        notification_type: str = "info",
        action_url: Optional[str] = None,
    ) -> Optional[Notification]:
        """Queue a notification in the current transaction (tenant comes from the session)."""
        if user_id is None:
            return None
        # Only relative in-app paths: a notification can never redirect off-site.
        if action_url and (not _SAFE_PATH.match(action_url) or action_url.startswith("//")):
            action_url = None
        notification = Notification(
            user_id=user_id,
            title=title[:255],
            message=message,
            notification_type=notification_type,
            action_url=action_url,
        )
        db.add(notification)
        # G-24: wake the user's browsers (payload-less Web Push; the service worker fetches this notification).
        from app.database.db import current_company_id
        from app.services import integrations

        company_id = current_company_id(db)
        if company_id is not None:
            integrations.tickle_user(db, company_id, user_id)
        return notification

    @staticmethod
    def list_for(db: Session, user_id: int, limit: int = 50):
        query = db.query(Notification).filter(Notification.user_id == user_id)
        unread = query.filter(Notification.is_read.is_(False)).count()
        items = query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit).all()
        return unread, items

    @staticmethod
    def mark_read(db: Session, user_id: int, notification_id: int) -> None:
        notification = (
            db.query(Notification)
            .filter(Notification.id == notification_id, Notification.user_id == user_id)
            .first()
        )
        if notification is None:
            raise not_found("Notification")
        notification.is_read = True

    @staticmethod
    def mark_all_read(db: Session, user_id: int) -> int:
        return (
            db.query(Notification)
            .filter(Notification.user_id == user_id, Notification.is_read.is_(False))
            .update({Notification.is_read: True}, synchronize_session=False)
        )

    @staticmethod
    def serialize(n: Notification) -> dict:
        return {
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "notification_type": n.notification_type,
            "is_read": n.is_read,
            "action_url": n.action_url,
            "created_at": n.created_at.isoformat(),
        }
