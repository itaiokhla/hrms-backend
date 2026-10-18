from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from fastapi import Header
from app.database.db import get_db
from app.services.notification_service import NotificationService
from app.core.security import get_user_id_from_token

router = APIRouter(
    prefix="/api/v1/notifications",
    tags=["Notifications"],
)


@router.get("/")
def get_notifications(
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    token = authorization.replace("Bearer ", "").strip()

    user_id = get_user_id_from_token(token)

    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token"
        )

    return {
        "count": NotificationService.unread_count(db, user_id),
        "notifications": NotificationService.get_all(db, user_id),
    }

@router.put("/read/{notification_id}")
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
):

    NotificationService.mark_read(
        db,
        notification_id,
    )

    return {
        "message": "Notification marked as read."
    }




@router.put("/read-all")
def mark_all_read(
    authorization: str = Header(...),
    db: Session = Depends(get_db),
):
    token = authorization.replace("Bearer ", "")
    user_id = get_user_id_from_token(token)

    return NotificationService.mark_all_read(
        db,
        int(user_id),
    )