from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class NotificationResponse(BaseModel):
    id: int
    title: str
    message: str
    notification_type: str
    is_read: bool
    action_url: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True