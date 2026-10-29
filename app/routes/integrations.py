"""Channels, browser push and the calendar feed (G-24).

``GET|POST /integrations/channels`` · ``PATCH|DELETE /integrations/channels/{id}`` ·
``POST /integrations/channels/{id}/test`` · ``GET /integrations/channels/{id}/deliveries`` ·
``POST /integrations/channels/{id}/secret`` · ``GET|POST|DELETE /me/push`` · ``POST /me/push/test`` ·
``GET|POST|DELETE /me/calendar-feed`` · public ``GET /calendar/{token}.ics`` (no session: the token is the secret).
"""

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import SessionLocal, bind_tenant, get_db
from app.payroll.services import idempotency
from app.services import integrations as service

router = APIRouter(prefix="/api/v1", tags=["Integrations"])
public = APIRouter(prefix="/api/v1", tags=["Integrations"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelIn(_Strict):
    kind: Literal["SLACK", "TEAMS", "WEBHOOK", "WHATSAPP"]
    name: str = Field(..., min_length=2, max_length=100)
    url: Optional[str] = Field(None, max_length=1000)
    events: List[str] = Field(default_factory=list, max_length=20)


class ChannelPatch(_Strict):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    url: Optional[str] = Field(None, max_length=1000)
    events: Optional[List[str]] = Field(None, max_length=20)
    active: Optional[bool] = None
    rotate_secret: bool = False
    row_version: Optional[int] = Field(None, ge=1)


class PushKeys(BaseModel):
    p256dh: Optional[str] = Field(None, max_length=200)
    auth: Optional[str] = Field(None, max_length=100)


class PushIn(BaseModel):
    endpoint: str = Field(..., max_length=1000)
    keys: PushKeys = Field(default_factory=PushKeys)
    expirationTime: Optional[Any] = None  # noqa: N815 - PushSubscription.toJSON() shape


class PushOut(_Strict):
    endpoint: Optional[str] = Field(None, max_length=1000)


def _write(db: Session, principal: Principal, request: Request, body: Any, run, status_code: int = 200):
    payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else (body or {})
    return idempotency.execute(db, principal, request, lambda: (status_code, run()), body=payload)


# ----------------------------------------------------------------------------------------------- channels
@router.get("/integrations/channels")
def list_channels(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.list_channels(db, principal)


@router.post("/integrations/channels", status_code=201)
def create_channel(data: ChannelIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return _write(db, principal, request, {**data.model_dump(), "url": "<sealed>" if data.url else None},
                  lambda: service.create_channel(db, principal, data.model_dump()), 201)


@router.patch("/integrations/channels/{channel_id:int}")
def update_channel(channel_id: int, data: ChannelPatch, request: Request,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _write(db, principal, request, {**data.model_dump(), "url": "<sealed>" if data.url else None},
                  lambda: service.update_channel(db, principal, channel_id, data.model_dump(exclude_unset=True)))


@router.delete("/integrations/channels/{channel_id:int}", status_code=204)
def delete_channel(channel_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    service.delete_channel(db, principal, channel_id)
    db.commit()
    return Response(status_code=204)


@router.post("/integrations/channels/{channel_id:int}/test")
def test_channel(channel_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = service.test_channel(db, principal, channel_id)
    db.commit()
    return result


@router.get("/integrations/channels/{channel_id:int}/deliveries")
def channel_deliveries(channel_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return {"items": service.deliveries(db, principal, channel_id)}


@router.post("/integrations/channels/{channel_id:int}/secret")
def channel_secret(channel_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = service.reveal_secret(db, principal, channel_id)
    db.commit()
    return result


# ----------------------------------------------------------------------------------------------- push
@router.get("/me/push")
def push_status(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.push_status(db, principal)


@router.post("/me/push")
def push_subscribe(data: PushIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    result = service.subscribe(db, principal, data.model_dump(), request.headers.get("user-agent"))
    db.commit()
    return result


@router.delete("/me/push")
def push_unsubscribe(data: Optional[PushOut] = None, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    result = service.unsubscribe(db, principal, data.endpoint if data else None)
    db.commit()
    return result


@router.post("/me/push/test")
def push_test(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = service.test_push(db, principal)
    db.commit()
    return result


# ----------------------------------------------------------------------------------------------- calendar feed
@router.get("/me/calendar-feed")
def feed_status(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.feed_status(db, principal)


@router.post("/me/calendar-feed")
def feed_rotate(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = service.rotate_feed(db, principal)
    db.commit()
    return result


@router.delete("/me/calendar-feed")
def feed_revoke(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    result = service.revoke_feed(db, principal)
    db.commit()
    return result


@public.get("/calendar/{token}.ics")
def calendar_feed(token: str):
    parsed = service.parse_feed_token(token)
    if parsed is None:
        return Response(status_code=404)
    company_id, secret = parsed
    db = SessionLocal()
    try:
        try:
            bind_tenant(db, company_id)
            body = service.feed_ics(db, company_id, secret)
        except Exception:  # noqa: BLE001 - an unknown company answers exactly like an unknown token
            db.rollback()
            body = None
        if body is None:
            return Response(status_code=404)
        db.commit()
        return Response(content=body, media_type="text/calendar; charset=utf-8",
                        headers={"Cache-Control": "private, max-age=900",
                                 "Content-Disposition": 'inline; filename="schedule.ics"'})
    finally:
        db.close()
