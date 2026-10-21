"""Payroll access preview (contract K:214, audit 06 G-17)."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import access

router = APIRouter(prefix="/api/v1", tags=["Payroll access"])


@router.get("/payroll/access-preview")
def access_preview(
    user_id: Optional[int] = Query(None, ge=1),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    """A user's effective payroll permissions, scopes and targets; own access, or anyone's with user:read."""
    return access.access_preview(db, principal, user_id)
