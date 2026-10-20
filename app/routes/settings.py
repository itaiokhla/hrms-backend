from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core import storage
from app.core.principal import Principal, get_principal_any_subscription, require
from app.database.db import get_db
from app.services import settings_service
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])


@router.get("")
def get_settings(principal: Principal = Depends(require("settings:manage")), db: Session = Depends(get_db)):
    return settings_service.get_all(db, principal.company_id)


@router.put("")
def update_settings(data: Dict[str, Any] = Body(...), principal: Principal = Depends(require("settings:manage")),
                    db: Session = Depends(get_db)):
    result = settings_service.update(db, principal, data)
    db.commit()
    return result


@router.post("/logo")
async def upload_logo(file: UploadFile = File(...), principal: Principal = Depends(require("settings:manage")),
                      db: Session = Depends(get_db)):
    data, ext = await storage.read_image_upload(file, max_bytes=2 * 1024 * 1024)
    old = settings_service.get_logo_path(db, principal.company_id)
    settings_service.set_logo_path(db, principal.company_id, storage.save_bytes(principal.company_id, "branding", data, ext))
    AuditLogService.log(db, principal, "Settings", "LOGO", "Updated company logo")
    db.commit()
    storage.delete(principal.company_id, old)
    return {"hasLogo": True}


@router.get("/logo")
def get_logo(principal: Principal = Depends(get_principal_any_subscription), db: Session = Depends(get_db)):
    # Any signed-in member of the company may see its own branding.
    path = storage.resolve(principal.company_id, settings_service.get_logo_path(db, principal.company_id))
    return FileResponse(path, media_type=storage.media_type_for(path), headers={"Cache-Control": "private, max-age=600"})
