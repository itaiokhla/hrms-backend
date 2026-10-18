from sqlalchemy import Column, String, Boolean, Integer, ForeignKey, Text
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class Notification(BaseModel):
    __tablename__ = "notifications"

    user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    title = Column(String(255), nullable=False)

    message = Column(Text, nullable=False)

    notification_type = Column(
        String(50),
        default="info",
    )

    is_read = Column(
        Boolean,
        default=False,
        index=True,
    )

    action_url = Column(String(255), nullable=True)

    user = relationship(
        "User",
        back_populates="notifications",
    )