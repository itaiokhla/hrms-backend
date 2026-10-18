from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database.db import get_db
from app.services.audit_log_service import AuditLogService

router = APIRouter(
    prefix="/audit-logs",
    tags=["Audit Logs"]
)


@router.get("/")
def get_audit_logs(
    skip: int = Query(0),
    limit: int = Query(50),
    db: Session = Depends(get_db),
):
    logs = AuditLogService.get_logs(db, skip, limit)

    return {
        "count": len(logs),
        "logs": logs,
    }