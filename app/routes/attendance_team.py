"""Team attendance routes (IG-AS-98..100): the team board, punching for a team member, deleting a session and the
team settings (Settings › Attendance › Team).

``GET /attendance/team/today`` · ``POST /attendance/team/punch`` · ``POST /attendance/sessions/{attendance_id:int}/void``
· ``GET|PUT /attendance/team/settings``. Writes go through ``idempotency.execute`` (which commits).
"""

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import idempotency
from app.services import attendance_team

router = APIRouter(prefix="/api/v1", tags=["Team attendance"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocationIn(_Strict):
    source: Literal["DEVICE", "MANUAL"] = "DEVICE"
    latitude: float = Field(..., ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(..., ge=-180, le=180, allow_inf_nan=False)
    accuracy_m: Optional[float] = Field(None, gt=0, le=100_000, allow_inf_nan=False)
    address: Optional[str] = Field(None, max_length=200)


class PunchIn(_Strict):
    employee_id: int = Field(..., ge=1)
    direction: Literal["IN", "OUT"]
    location: Optional[LocationIn] = None
    reason: Optional[str] = Field(None, max_length=500)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    client_sent_at: Optional[datetime] = None


class BulkPunchIn(_Strict):
    employee_ids: list[int] = Field(..., min_length=1, max_length=attendance_team.BULK_MAX)
    direction: Literal["IN", "OUT"]
    location: Optional[LocationIn] = None
    reason: Optional[str] = Field(None, max_length=500)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    client_sent_at: Optional[datetime] = None


class VoidIn(_Strict):
    reason: str = Field(..., min_length=5, max_length=500)


class TeamSettingsIn(_Strict):
    proxy_punch_enabled: Optional[bool] = None
    proxy_punch_location: Optional[Literal["REQUIRED", "OPTIONAL", "OFF"]] = None
    allow_manual_location: Optional[bool] = None
    reason_required: Optional[bool] = None
    edit_enabled: Optional[bool] = None
    reason: str = Field("Team attendance settings", min_length=3, max_length=500)


@router.get("/attendance/team/today")
def team_today(q: Optional[str] = Query(None, max_length=80), principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    return attendance_team.team_today(db, principal, q=q)


@router.post("/attendance/team/punch")
def team_punch(data: PunchIn, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    def handler():
        return 200, attendance_team.proxy_punch(db, principal, data.model_dump(mode="python"))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/team/punch/bulk")
def team_bulk_punch(data: BulkPunchIn, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    def handler():
        return 200, attendance_team.bulk_punch(db, principal, data.model_dump(mode="python"))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.post("/attendance/sessions/{attendance_id:int}/void")
def void_session(attendance_id: int, data: VoidIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        return 200, attendance_team.delete_session(db, principal, attendance_id, reason=data.reason)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/attendance/team/settings")
def get_settings(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return attendance_team.get_settings(db, principal)


@router.put("/attendance/team/settings")
def put_settings(data: TeamSettingsIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        body = data.model_dump(exclude_unset=True)
        reason = body.pop("reason", "Team attendance settings")
        return 200, attendance_team.update_settings(db, principal, body, reason=reason)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))
