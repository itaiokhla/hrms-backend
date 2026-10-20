"""Private, tenant-partitioned file storage.

Files live under UPLOAD_DIR/<company_id>/<category>/ and are never mounted
as static files; they are streamed through endpoints that authorize the
caller first. Stored names are random, so paths cannot be guessed.
"""

import io
import uuid
from pathlib import Path
from typing import Optional, Tuple

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError

from app.config import settings
from app.core.errors import bad_request, not_found

IMAGE_FORMATS = {"JPEG": "jpg", "PNG": "png"}
MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png"}


def _root() -> Path:
    root = Path(settings.upload_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


async def read_image_upload(upload: UploadFile, max_bytes: Optional[int] = None) -> Tuple[bytes, str]:
    """Read and validate an uploaded image. Returns (bytes, extension)."""
    limit = max_bytes or settings.max_upload_size
    data = await upload.read(limit + 1)
    if not data:
        raise bad_request("No file provided")
    if len(data) > limit:
        raise bad_request(f"File is too large (max {limit // (1024 * 1024)} MB)")
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            img.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise bad_request("Upload a valid JPG or PNG image")
    if fmt not in IMAGE_FORMATS:
        raise bad_request("Only JPG and PNG images are allowed")
    return data, IMAGE_FORMATS[fmt]


def save_bytes(company_id: int, category: str, data: bytes, ext: str) -> str:
    folder = _root() / str(int(company_id)) / category
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    (folder / name).write_bytes(data)
    return f"{int(company_id)}/{category}/{name}"


def resolve(company_id: int, relative_path: Optional[str]) -> Path:
    """Map a stored path to disk, refusing anything outside the company folder."""
    if not relative_path:
        raise not_found("File")
    base = (_root() / str(int(company_id))).resolve()
    path = (_root() / relative_path).resolve()
    if base not in path.parents or not path.is_file():
        raise not_found("File")
    return path


def delete(company_id: int, relative_path: Optional[str]) -> None:
    if not relative_path:
        return
    try:
        resolve(company_id, relative_path).unlink(missing_ok=True)
    except Exception:
        pass


def media_type_for(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
