"""Holidays, assets, notifications and the company audit log."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal, require
from app.database.db import get_db
from app.schemas.admin import AssetAssign, AssetReturn, AssetStatus, AssetWrite, HolidayWrite
from app.services import asset_service, holiday_service
from app.services.audit_log_service import AuditLogService
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/api/v1", tags=["Operations"])


# Holidays -------------------------------------------------------------------
@router.get("/holidays")
def list_holidays(year: Optional[int] = Query(None, ge=2000, le=2100), location: Optional[str] = Query(None, max_length=100),
                  mine: bool = False, principal: Principal = Depends(require("holiday:read")),
                  db: Session = Depends(get_db)):
    return holiday_service.list_holidays(db, principal, year, location, mine)


@router.get("/holidays/locations")
def holiday_locations(principal: Principal = Depends(require("holiday:read")), db: Session = Depends(get_db)):
    return holiday_service.locations(db, principal)


@router.post("/holidays")
def create_holiday(data: HolidayWrite, principal: Principal = Depends(require("holiday:manage")),
                   db: Session = Depends(get_db)):
    holiday = holiday_service.save(db, principal, data)
    db.commit()
    return holiday_service.serialize(holiday)


@router.put("/holidays/{holiday_id}")
def update_holiday(holiday_id: int, data: HolidayWrite, principal: Principal = Depends(require("holiday:manage")),
                   db: Session = Depends(get_db)):
    holiday = holiday_service.save(db, principal, data, holiday_id)
    db.commit()
    return holiday_service.serialize(holiday)


@router.delete("/holidays/{holiday_id}")
def delete_holiday(holiday_id: int, principal: Principal = Depends(require("holiday:manage")),
                   db: Session = Depends(get_db)):
    holiday_service.delete(db, principal, holiday_id)
    db.commit()
    return {"ok": True}


# Assets ---------------------------------------------------------------------
@router.get("/assets")
def list_assets(q: Optional[str] = Query(None, max_length=100),
                status: Optional[str] = Query(None, pattern=r"^(in_stock|assigned|maintenance|retired)$"),
                employee_id: Optional[int] = None, skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                principal: Principal = Depends(require("asset:read")), db: Session = Depends(get_db)):
    total, items = asset_service.list_assets(db, principal, q, status, employee_id, skip, limit)
    return {"total": total, "items": items, "skip": skip, "limit": limit}


@router.get("/assets/mine")
def my_assets(principal: Principal = Depends(require("asset:read")), db: Session = Depends(get_db)):
    return asset_service.my_assets(db, principal)


@router.get("/assets/{asset_id}")
def get_asset(asset_id: int, principal: Principal = Depends(require("asset:read")), db: Session = Depends(get_db)):
    return asset_service.get_asset(db, principal, asset_id)


@router.post("/assets")
def create_asset(data: AssetWrite, principal: Principal = Depends(require("asset:manage")), db: Session = Depends(get_db)):
    asset = asset_service.save_asset(db, principal, data)
    db.commit()
    return asset_service.get_asset(db, principal, asset.id)


@router.put("/assets/{asset_id}")
def update_asset(asset_id: int, data: AssetWrite, principal: Principal = Depends(require("asset:manage")),
                 db: Session = Depends(get_db)):
    asset_service.save_asset(db, principal, data, asset_id)
    db.commit()
    return asset_service.get_asset(db, principal, asset_id)


@router.post("/assets/{asset_id}/assign")
def assign_asset(asset_id: int, data: AssetAssign, principal: Principal = Depends(require("asset:assign")),
                 db: Session = Depends(get_db)):
    asset_service.assign(db, principal, asset_id, data.employee_id, data.notes)
    db.commit()
    return asset_service.get_asset(db, principal, asset_id)


@router.post("/assets/{asset_id}/return")
def return_asset(asset_id: int, data: AssetReturn, principal: Principal = Depends(require("asset:assign")),
                 db: Session = Depends(get_db)):
    asset_service.recover(db, principal, asset_id, data.condition, data.status)
    db.commit()
    return asset_service.get_asset(db, principal, asset_id)


@router.put("/assets/{asset_id}/status")
def asset_status(asset_id: int, data: AssetStatus, principal: Principal = Depends(require("asset:manage")),
                 db: Session = Depends(get_db)):
    asset_service.set_status(db, principal, asset_id, data.status)
    db.commit()
    return asset_service.get_asset(db, principal, asset_id)


@router.post("/assets/{asset_id}/acknowledge")
def acknowledge_asset(asset_id: int, principal: Principal = Depends(require("asset:acknowledge")),
                      db: Session = Depends(get_db)):
    asset_service.acknowledge(db, principal, asset_id)
    db.commit()
    return {"ok": True}


# Notifications --------------------------------------------------------------
@router.get("/notifications")
def notifications(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    unread, items = NotificationService.list_for(db, principal.user_id)
    return {"count": unread, "notifications": [NotificationService.serialize(n) for n in items]}


@router.put("/notifications/read-all")
def read_all(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    NotificationService.mark_all_read(db, principal.user_id)
    db.commit()
    return {"ok": True}


@router.put("/notifications/{notification_id}/read")
def read_one(notification_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    NotificationService.mark_read(db, principal.user_id, notification_id)
    db.commit()
    return {"ok": True}


# Audit log ------------------------------------------------------------------
@router.get("/audit-logs")
def audit_logs(skip: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
               module: Optional[str] = Query(None, max_length=50),
               principal: Principal = Depends(require("audit:read")), db: Session = Depends(get_db)):
    total, rows = AuditLogService.list(db, principal, skip, limit, module)
    return {
        "total": total,
        "logs": [
            {
                "id": r.id, "created_at": r.created_at.isoformat(), "actor_email": r.actor_email,
                "actor_type": r.actor_type, "module": r.module, "action": r.action, "entity_type": r.entity_type,
                "entity_id": r.entity_id, "description": r.description, "ip_address": r.ip_address,
            }
            for r in rows
        ],
    }
