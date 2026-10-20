"""Brand image processing and content-addressed storage (01-white-label §5.2).

Uploads are decoded with Pillow and refused when they do not decode, are animated,
exceed the size/pixel limits (decompression-bomb guard) or are below the minimum
dimensions for their kind. Accepted images are converted to sRGB, stripped of all
metadata by rebuilding them from raw pixels, re-encoded, and stored together with
their derivatives (favicons, PWA icons, email and document versions) as
``UPLOAD_DIR/brand/<sha256>.<ext>``. The uploaded bytes are never stored or served.
Files are never deleted when a brand changes, so issued documents and sent emails
keep working.
"""

import hashlib
import io
import logging
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image, ImageCms, ImageDraw, ImageFont, UnidentifiedImageError

from app.config import settings
from app.core.errors import bad_request

logger = logging.getLogger(__name__)

MAX_PIXELS = 40_000_000
MAX_SIDE = 4096
MB = 1024 * 1024
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
ASSET_NAME = re.compile(r"^([0-9a-f]{64})\.(png|jpg|webp|ico)$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "ico": "image/x-icon"}


@dataclass(frozen=True)
class KindRule:
    label: str
    formats: Tuple[str, ...]
    max_bytes: int
    min_width: int
    min_height: int


KIND_RULES: Dict[str, KindRule] = {
    "logo_light": KindRule("Logo (light backgrounds)", ("PNG", "JPEG", "WEBP"), 2 * MB, 48, 48),
    "logo_dark": KindRule("Logo (dark backgrounds)", ("PNG", "JPEG", "WEBP"), 2 * MB, 48, 48),
    "mark": KindRule("Square mark", ("PNG", "WEBP"), 2 * MB, 192, 192),
    "email_logo": KindRule("Email logo", ("PNG", "JPEG"), 1 * MB, 48, 24),
    "document_logo": KindRule("Document logo", ("PNG", "JPEG"), 2 * MB, 96, 48),
    "login_image": KindRule("Login image", ("JPEG", "WEBP", "PNG"), 3 * MB, 800, 600),
}
FORMAT_NAMES = {"PNG": "PNG", "JPEG": "JPG", "WEBP": "WebP"}


def _root() -> Path:
    root = Path(settings.upload_dir).resolve() / "brand"
    root.mkdir(parents=True, exist_ok=True)
    return root


def asset_path(sha256: str, ext: str) -> Path:
    return _root() / f"{sha256}.{ext}"


def _store(data: bytes, ext: str) -> Tuple[str, int]:
    """Write bytes under their SHA-256 (atomic, idempotent). Returns (sha256, size)."""
    digest = hashlib.sha256(data).hexdigest()
    target = asset_path(digest, ext)
    if not target.exists():
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return digest, len(data)


def _encode(img: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    if fmt == "JPEG":
        if img.mode != "RGB":
            img = _flatten(img)
        img.save(out, "JPEG", quality=86, optimize=True, progressive=True)
    elif fmt == "ICO":
        img.save(out, "ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    else:
        img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _flatten(img: Image.Image, background=(255, 255, 255)) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, background)
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return img.convert("RGB")


def _fit(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    copy = img.copy()
    copy.thumbnail((max_w, max_h), Image.LANCZOS)
    return copy


def _square(img: Image.Image, size: int, padding: float = 0.0, background=None) -> Image.Image:
    """Centre the image on a square canvas (transparent unless a background is given)."""
    mode = "RGB" if background else "RGBA"
    canvas = Image.new(mode, (size, size), background or (0, 0, 0, 0))
    inner = int(round(size * (1 - 2 * padding)))
    fitted = img.convert("RGBA")
    fitted.thumbnail((inner, inner), Image.LANCZOS)
    offset = ((size - fitted.width) // 2, (size - fitted.height) // 2)
    canvas.paste(fitted, offset, fitted)
    return canvas


def _derivative(img: Image.Image, fmt: str) -> dict:
    data = _encode(img, fmt)
    ext = {"PNG": "png", "JPEG": "jpg", "ICO": "ico"}[fmt]
    sha, size = _store(data, ext)
    return {"sha256": sha, "ext": ext, "width": img.width, "height": img.height, "bytes": size}


@dataclass
class ProcessedImage:
    kind: str
    sha256: str
    ext: str
    mime: str
    width: int
    height: int
    bytes: int
    derivatives: dict


def decode_and_clean(data: bytes, kind: str) -> Image.Image:
    """Validate an upload for `kind` and return a metadata-free sRGB image."""
    rule = KIND_RULES.get(kind)
    if rule is None:
        raise bad_request("Unknown image kind.", "BRAND_ASSET_KIND")
    if not data:
        raise bad_request("No file provided.", "BRAND_ASSET_EMPTY")
    if len(data) > rule.max_bytes:
        raise bad_request(f"{rule.label} must be at most {rule.max_bytes // MB} MB.", "BRAND_ASSET_TOO_LARGE")
    allowed = ", ".join(FORMAT_NAMES[f] for f in rule.formats)
    if data.lstrip()[:5].lower() in (b"<?xml", b"<svg ") or b"<svg" in data[:512].lower():
        raise bad_request(f"SVG images are not accepted yet. Upload {allowed}.", "BRAND_ASSET_FORMAT")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as probe:
                fmt = probe.format
                width, height = probe.size
                probe.verify()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise bad_request("This image has too many pixels.", "BRAND_ASSET_TOO_MANY_PIXELS")
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            raise bad_request(f"The file is not a valid image. Upload {allowed}.", "BRAND_ASSET_INVALID")
        if fmt not in rule.formats:
            raise bad_request(f"{rule.label} must be {allowed}.", "BRAND_ASSET_FORMAT")
        if width * height > MAX_PIXELS or width > MAX_SIDE or height > MAX_SIDE:
            raise bad_request(f"Images can be at most {MAX_SIDE} × {MAX_SIDE} pixels.", "BRAND_ASSET_TOO_MANY_PIXELS")
        if width < rule.min_width or height < rule.min_height:
            raise bad_request(
                f"{rule.label} must be at least {rule.min_width} × {rule.min_height} pixels.", "BRAND_ASSET_TOO_SMALL"
            )
        try:
            img = Image.open(io.BytesIO(data))
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                raise bad_request("Animated images are not accepted.", "BRAND_ASSET_ANIMATED")
            img.load()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise bad_request("This image has too many pixels.", "BRAND_ASSET_TOO_MANY_PIXELS")
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
            raise bad_request(f"The file is damaged or truncated. Upload {allowed}.", "BRAND_ASSET_INVALID")

    icc = img.info.get("icc_profile")
    if img.mode == "CMYK" or icc:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(icc)) if icc else ImageCms.createProfile("sRGB")
            target_mode = "RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB"
            if img.mode == "CMYK" and not icc:
                img = img.convert("RGB")
            else:
                img = ImageCms.profileToProfile(img, source, ImageCms.createProfile("sRGB"), outputMode=target_mode)
        except Exception:  # unusable embedded profile: fall back to a plain conversion
            img = img.convert("RGB")
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if has_alpha else "RGB")
    # Rebuilding from raw pixels drops EXIF, GPS, XMP, ICC and text chunks.
    return Image.frombytes(img.mode, img.size, img.tobytes())


def process_upload(data: bytes, kind: str) -> ProcessedImage:
    img = decode_and_clean(data, kind)
    derivatives: Dict[str, dict] = {}
    if kind == "login_image":
        master = _fit(_flatten(img), 1600, 1200)
        master_fmt = "JPEG"
    elif kind == "mark":
        master = _square(img, max(img.width, img.height) if max(img.width, img.height) <= 1024 else 1024)
        master_fmt = "PNG"
    else:
        master = _fit(img, 2400, 1200)
        master_fmt = "PNG"

    if kind == "mark":
        derivatives.update(mark_derivatives(master))
    if kind in ("logo_light", "logo_dark"):
        derivatives["display"] = _derivative(_fit(master, 640, 160), "PNG")
    if kind in ("logo_light", "email_logo"):
        derivatives["email"] = _derivative(_fit(master, 800, 200), "PNG")
    if kind in ("logo_light", "document_logo"):
        derivatives["document"] = _derivative(_fit(_flatten(master), 1200, 300), "PNG")

    encoded = _encode(master, master_fmt)
    ext = "jpg" if master_fmt == "JPEG" else "png"
    sha, size = _store(encoded, ext)
    return ProcessedImage(kind, sha, ext, MEDIA_TYPES[ext], master.width, master.height, size, derivatives)


def mark_derivatives(mark: Image.Image) -> Dict[str, dict]:
    square = _square(mark, 512)
    return {
        "favicon-16": _derivative(_square(square, 16), "PNG"),
        "favicon-32": _derivative(_square(square, 32), "PNG"),
        "favicon-48": _derivative(_square(square, 48), "PNG"),
        "favicon-ico": _derivative(_square(square, 48), "ICO"),
        "apple-touch-180": _derivative(_square(square, 180, background=(255, 255, 255)), "PNG"),
        "pwa-192": _derivative(_square(square, 192), "PNG"),
        "pwa-512": _derivative(square, "PNG"),
        # Maskable icons keep the mark inside the 80% safe zone.
        "maskable-512": _derivative(_square(square, 512, padding=0.1, background=(255, 255, 255)), "PNG"),
    }


def initials(name: str) -> str:
    words = [w for w in re.split(r"\s+", (name or "").strip()) if w and w[0].isalnum()]
    if not words:
        return "?"
    letters = words[0][0] + (words[1][0] if len(words) > 1 else "")
    return letters.upper()


def initials_mark(name: str, background_hex: str, foreground_hex: str) -> ProcessedImage:
    """Fallback square mark: the company initials on the brand colour (01 §5.2)."""
    size = 512
    bg = tuple(int(background_hex[i : i + 2], 16) for i in (1, 3, 5))
    fg = tuple(int(foreground_hex[i : i + 2], 16) for i in (1, 3, 5))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=112, fill=bg + (255,))
    text = initials(name)
    font = ImageFont.truetype(str(FONT_DIR / "NotoSans-Bold.ttf"), 230 if len(text) == 1 else 200)
    box = draw.textbbox((0, 0), text, font=font)
    position = ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1])
    draw.text(position, text, font=font, fill=fg + (255,))
    encoded = _encode(img, "PNG")
    sha, length = _store(encoded, "png")
    return ProcessedImage("mark", sha, "png", "image/png", size, size, length, mark_derivatives(img))


def parse_asset_name(name: str) -> Optional[Tuple[str, str]]:
    match = ASSET_NAME.match(name or "")
    return (match.group(1), match.group(2)) if match else None


def read_asset(sha256: str, ext: str) -> Optional[bytes]:
    path = asset_path(sha256, ext)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != sha256:
        logger.error("Brand asset %s.%s is corrupt (hash mismatch)", sha256, ext)
        return None
    return data


def file_name(sha256: str, ext: str) -> str:
    return f"{sha256}.{ext}"
