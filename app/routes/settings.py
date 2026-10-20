from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, File, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.core import storage
from app.core.principal import Principal, get_principal_any_subscription, require
from app.database.db import get_db
from app.routes.branding import legacy_logo_bytes
from app.services import brand_service, settings_service
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
    brand_service.invalidate_cache(principal.company_id)  # the company name is part of the brand
    return result


@router.post("/logo")
async def upload_logo(file: UploadFile = File(...), principal: Principal = Depends(require("branding:manage")),
                      db: Session = Depends(get_db)):
    """Compatibility endpoint: uploads the light logo and publishes it immediately."""
    data = await file.read(2 * 1024 * 1024 + 1)
    owner = brand_service.tenant_owner(principal.company_id)
    chain = brand_service.load_chain(principal.company_id, use_cache=False)
    asset = brand_service.add_asset(db, owner, "logo_light", data, file.filename, principal.email)
    draft = brand_service.save_draft(db, owner, {"logo_light": asset.id}, actor=principal.email,
                                     actor_user_id=principal.user_id, chain=chain)
    profile, _ = brand_service.publish(db, owner, actor=principal.email, actor_user_id=principal.user_id,
                                       expected_row_version=draft.row_version, chain=chain)
    AuditLogService.log(db, principal, "Settings", "LOGO", f"Updated company logo (brand v{profile.version})")
    db.commit()
    return {"hasLogo": True}


@router.get("/logo")
def get_logo(principal: Principal = Depends(get_principal_any_subscription), db: Session = Depends(get_db)):
    # Any signed-in member of the company may see its own branding.
    found = legacy_logo_bytes(principal.company_id)
    if found is not None:
        data, media_type = found
        return Response(content=data, media_type=media_type, headers={"Cache-Control": "private, max-age=600"})
    path = storage.resolve(principal.company_id, settings_service.get_logo_path(db, principal.company_id))
    return FileResponse(path, media_type=storage.media_type_for(path), headers={"Cache-Control": "private, max-age=600"})
