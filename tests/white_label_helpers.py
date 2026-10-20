"""Helpers for white-label tests: host-bound clients, platform admin, images, DNS fixtures."""

import io
import json
import os
from typing import Optional

from PIL import Image

from tests.helpers import PASSWORD, ApiClient

PROXY_KEY = os.environ.get("PROXY_SHARED_SECRET", "test-proxy-secret-that-is-long-enough-123")
PLATFORM_HOST = "app.test.local"


class HostClient(ApiClient):
    """A browser on one host: every request carries the trusted proxy headers for that host."""

    def __init__(self, app, host: str, proxy_key: Optional[str] = PROXY_KEY, user_agent: str = "pytest-browser"):
        super().__init__(app)
        self.host = host
        self.proxy_key = proxy_key
        self.user_agent = user_agent

    def _headers(self, method, headers):
        merged = super()._headers(method, headers)
        merged.setdefault("X-HRMS-Host", self.host)
        if self.proxy_key:
            merged.setdefault("X-HRMS-Proxy-Key", self.proxy_key)
        merged.setdefault("User-Agent", self.user_agent)
        return merged

    def login(self, email: str, password: str = PASSWORD):
        return self.post("/api/v1/auth/login", json={"email": email, "password": password})


def tenant_host(slug: str) -> str:
    return f"{slug}.test.local"


def platform_admin(app) -> ApiClient:
    from app.cli import create_platform_admin

    create_platform_admin("ops@platform.io", "Ops", "Platf0rmPass!")
    ops = HostClient(app, PLATFORM_HOST)
    r = ops.post("/api/v1/platform/auth/login", json={"email": "ops@platform.io", "password": "Platf0rmPass!"})
    assert r.status_code == 200, r.text
    return ops


def set_plan(ops: ApiClient, company_id: int, plan: str) -> None:
    r = ops.patch(f"/api/v1/platform/companies/{company_id}", json={"subscription_plan": plan})
    assert r.status_code == 200, r.text


def png(width: int = 400, height: int = 120, color=(15, 118, 110, 255), mode: str = "RGBA") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), color if mode != "RGB" else color[:3]).save(out, "PNG")
    return out.getvalue()


def jpeg_with_gps(width: int = 900, height: int = 700) -> bytes:
    img = Image.new("RGB", (width, height), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"  # Make
    gps = {1: "N", 2: (12.0, 58.0, 0.0), 3: "E", 4: (77.0, 35.0, 0.0)}
    exif[0x8825] = gps
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes(), quality=90)
    return out.getvalue()


def animated_gif() -> bytes:
    frames = [Image.new("RGB", (300, 300), c) for c in ((255, 0, 0), (0, 255, 0))]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


def animated_png() -> bytes:
    frames = [Image.new("RGBA", (300, 300), c) for c in ((255, 0, 0, 255), (0, 0, 255, 255))]
    out = io.BytesIO()
    frames[0].save(out, "PNG", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


def huge_png_header() -> bytes:
    """A real PNG whose header claims 9000 x 9000 pixels (decompression-bomb guard)."""
    img = Image.new("1", (9000, 9000))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def cmyk_jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("CMYK", (400, 200), (0, 200, 200, 0)).save(out, "JPEG")
    return out.getvalue()


def write_dns(records: dict) -> None:
    path = os.environ["DOMAIN_DNS_FIXTURE_FILE"]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(records, handle)


def upload(client: ApiClient, kind: str, data: bytes, filename: str = "logo.png", content_type: str = "image/png"):
    return client.post("/api/v1/branding/assets", data={"kind": kind}, files={"file": (filename, data, content_type)})
