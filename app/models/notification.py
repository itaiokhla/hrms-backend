from sqlalchemy import Boolean, Column, ForeignKeyConstraint, Integer, String, Text

from app.models.base import BaseModel, TenantMixin


class Notification(TenantMixin, BaseModel):
    __tablename__ = "notifications"

    user_id = Column(Integer, nullable=False, index=True)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    notification_type = Column(String(50), default="info", nullable=False)
    is_read = Column(Boolean, default=False, nullable=False, index=True)
    # Relative in-app path only (validated on creation).
    action_url = Column(String(255), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "user_id"], ["users.company_id", "users.id"], ondelete="CASCADE", name="fk_notifications_user"
        ),
    )
