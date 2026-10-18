from sqlalchemy.orm import Session
from app.models.notification import Notification


class NotificationService:

    @staticmethod
    def get_all(db: Session, user_id: int):

        return (
            db.query(Notification)
            .filter(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .all()
        )

    @staticmethod
    def unread_count(db: Session, user_id: int):

        return (
            db.query(Notification)
            .filter(
                Notification.user_id == user_id,
                Notification.is_read == False
            )
            .count()
        )

    @staticmethod
    def mark_read(db: Session, notification_id: int):

        notification = (
            db.query(Notification)
            .filter(Notification.id == notification_id)
            .first()
        )

        if notification:
            notification.is_read = True
            db.commit()

    @staticmethod
    def mark_all_read(db: Session, user_id: int):

        notifications = (
            db.query(Notification)
            .filter(
                Notification.user_id == user_id,
                Notification.is_read == False
            )
            .all()
        )

        for item in notifications:
            item.is_read = True

        db.commit()

        return {
            "message": "All notifications marked as read."
        }

    @staticmethod
    def create(
        db: Session,
        user_id: int,
        title: str,
        message: str,
        notification_type: str = "info",
        action_url: str | None = None,
    ):

        notification = Notification(
            user_id=user_id,
            title=title,
            message=message,
            notification_type=notification_type,
            action_url=action_url,
        )

        db.add(notification)
        db.commit()
        db.refresh(notification)

        return notification