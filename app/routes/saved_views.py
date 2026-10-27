"""Saved list views (X-M3; X 04 §2.8; spine §8.2): any signed-in person, own rows only for writes."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field
from sqlalchemy.orm import Session

from app.core import concurrency
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.views import VIEW_NAME_MAX
from app.payroll.services import idempotency
from app.schemas.auth import StrictModel
from app.services import saved_views as service

router = APIRouter(prefix="/api/v1/saved-views", tags=["Saved views"])


class SavedViewIn(StrictModel):
    list_key: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=VIEW_NAME_MAX)
    query: Dict[str, Any] = Field(default_factory=dict)
    is_shared: bool = False
    is_default: bool = False


class SavedViewPatch(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=VIEW_NAME_MAX)
    query: Optional[Dict[str, Any]] = None
    is_shared: Optional[bool] = None
    is_default: Optional[bool] = None
    position: Optional[int] = Field(None, ge=0, le=1000)
    row_version: Optional[int] = Field(None, ge=1)


class SystemViewState(StrictModel):
    list_key: str = Field(..., min_length=1, max_length=64)
    system_key: str = Field(..., min_length=1, max_length=64)
    is_hidden: Optional[bool] = None
    position: Optional[int] = Field(None, ge=0, le=1000)


@router.get("")
def list_views(list_key: str = Query(..., min_length=1, max_length=64), principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    return service.list_views(db, principal, list_key)


@router.post("", status_code=201)
def create_view(data: SavedViewIn, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    return idempotency.execute(db, principal, request,
                               lambda: (201, service.serialize(service.create_view(db, principal, data), principal)),
                               body=data.model_dump(mode="json"))


# Literal path before the integer one (IG-AS-09 / MR-6).
@router.put("/system-state")
def set_system_state(data: SystemViewState, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    result = service.set_system_view_state(db, principal, data.list_key, data.system_key, data.is_hidden,
                                           data.position)
    db.commit()
    return result


@router.patch("/{view_id:int}")
def update_view(view_id: int, data: SavedViewPatch, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    row = service.update_view(db, principal, view_id, data, concurrency.if_match(request, data.row_version))
    db.commit()
    return service.serialize(row, principal)


@router.delete("/{view_id:int}", status_code=204)
def delete_view(view_id: int, request: Request, row_version: Optional[int] = Query(None, ge=1),
                principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    service.delete_view(db, principal, view_id, concurrency.if_match(request, row_version))
    db.commit()
    return Response(status_code=204)
