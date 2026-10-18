from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.db import get_db
from app.services.dashboard_service import DashboardService

router = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["Dashboard"],
)


@router.get("")
def get_dashboard(db: Session = Depends(get_db)):
    return DashboardService.get_dashboard_summary(db)


@router.get("/")
def get_dashboard_slash(db: Session = Depends(get_db)):
    return DashboardService.get_dashboard_summary(db)