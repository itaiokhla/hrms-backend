from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from fastapi import UploadFile, File
import os
import shutil

from app.database.db import get_db
from app.services.settings_service import SettingsService

router = APIRouter(
    prefix="/api/v1/settings",
    tags=["Settings"]
)


@router.get("/")
def get_settings(db: Session = Depends(get_db)):
    return SettingsService.get_settings(db)


@router.put("/")
def update_settings(
    data: dict,
    db: Session = Depends(get_db),
):
    return SettingsService.update_settings(db, data)


@router.post("/logo")
async def upload_logo(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    # return {
    #     "success": True,
    #     "filename": file.filename,
    #     "content_type": file.content_type
    # }

    upload_dir = "uploads"

    os.makedirs(upload_dir, exist_ok=True)

    file_name = "company-logo.png"

    file_path = os.path.join(upload_dir, file_name)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    logo_url = "/uploads/" + file_name

    SettingsService.update_settings(
    db,
    {
        "companyLogo": logo_url
    }
)

    return {
    "success": True,
    "logo": logo_url
}