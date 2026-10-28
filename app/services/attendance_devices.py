"""Device registry (KIOSK in AS-M2), claim codes, kiosk pairing and device tokens (04-attendance/01 §5.1, §7.2;
AS 11 §4.7, §6.13; AS 07 §10; D-AS-24, D-AS-25).

* ``POST /attendance/devices`` (KIOSK only in AS-M2) creates the device ``PENDING_ACTIVATION`` with a one-time claim
  code, shown once and valid 24 h (only its keyed hash is stored, SP-AS-009 / -010).
* ``POST /attendance/kiosk/pair`` (the one unauthenticated attendance route, K-1, SP-AS-207) checks ``device_code`` +
  ``claim_code``; an unknown device code and a wrong code get the same 422 ``CLAIM_CODE_INVALID``; failures count on
  the device row named by ``device_code`` and 5 invalidate the code (K-5, AS-SEC-04); the device becomes ``ACTIVE``
  and receives a device token (cookie set by the route; only the hash is stored).
* A token is ``v1.<company_id>.<device_id>.<secret>``: the company and device ids only route the lookup (RLS needs the
  tenant first); the secret is compared in constant time with ``token_hash``. Expiry is checked on every call
  (K-4); in the last 14 days the token is renewed silently, never beyond ``token_max_days`` after activation (K-12,
  AS-SEC-06). Suspend / retire clear the token at once (K-3).

Services never commit; routes commit.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytz
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, conflict, not_found
from app.core.principal import Principal
from app.core.security import hash_token, tokens_equal
from app.models.attendance import DEVICE_STATUSES, AttendanceDevice
from app.models.org import Branch
from app.services.audit_log_service import AuditLogService

CLAIM_CODE_TTL = timedelta(hours=24)
CLAIM_CODE_MAX_FAILURES = 5
# attendance.kiosk.token_ttl_days / token_max_days (SP-AS-212); registry keys arrive with the settings section in
# AS-M3, these are the documented defaults (FOLLOW-UPS).
TOKEN_TTL = timedelta(days=90)
TOKEN_MAX_AGE = timedelta(days=365)
TOKEN_RENEW_WINDOW = timedelta(days=14)
TOKEN_VERSION = "v1"
_CLAIM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
TRANSITIONS = {
    "suspend": (("ACTIVE",), "SUSPENDED", "DEVICE_SUSPENDED"),
    "resume": (("SUSPENDED",), "ACTIVE", "DEVICE_ACTIVATED"),
    "retire": (("PENDING_ACTIVATION", "ACTIVE", "SUSPENDED"), "RETIRED", "DEVICE_RETIRED"),
}


@dataclass(frozen=True)
class DevicePrincipal:
    """A paired kiosk acting with its device token (``principal.get_device_principal``). Not a user: no grants."""

    company_id: int
    device_id: int
    device_code: str
    kind: str
    branch_id: Optional[int]
    site_id: Optional[int]
    timezone: str
    token_expires_at: datetime
    renewed_token: Optional[str] = None  # set when this call renewed the token; the route re-issues the cookie


def _now() -> datetime:
    return clock.utcnow().replace(microsecond=0)


def new_claim_code() -> str:
    raw = "".join(secrets.choice(_CLAIM_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _canonical_claim(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _claim_hash(code: str) -> str:
    return hash_token("claim:" + _canonical_claim(code))


def make_token(device: AttendanceDevice) -> Tuple[str, str]:
    """(cookie value, hash to store) of a fresh device token."""
    secret = secrets.token_urlsafe(32)
    return f"{TOKEN_VERSION}.{device.company_id}.{device.id}.{secret}", hash_token("device:" + secret)


def parse_token(raw: Optional[str]) -> Optional[Tuple[int, int, str]]:
    if not raw or len(raw) > 200:
        return None
    parts = raw.split(".")
    if len(parts) != 4 or parts[0] != TOKEN_VERSION:
        return None
    try:
        return int(parts[1]), int(parts[2]), parts[3]
    except ValueError:
        return None


def token_matches(device: AttendanceDevice, secret: str) -> bool:
    return bool(device.token_hash) and tokens_equal(device.token_hash, hash_token("device:" + secret))


def _auth_failed() -> AppError:
    return AppError(401, "This kiosk is not active. Ask your administrator to pair it again.", "DEVICE_AUTH_FAILED")


def authenticate(db: Session, device_id: int, secret: str) -> DevicePrincipal:
    """Check a device token on a tenant-bound session; renews it inside the renewal window (K-12)."""
    device = db.get(AttendanceDevice, device_id)
    if device is None or device.kind != "KIOSK" or not token_matches(device, secret):
        raise _auth_failed()
    if device.status == "SUSPENDED":
        raise AppError(403, "This kiosk is suspended. Ask your administrator.", "DEVICE_SUSPENDED")
    now = _now()
    if device.status != "ACTIVE" or device.token_expires_at is None or device.token_expires_at <= now:
        raise _auth_failed()
    renewed = None
    if device.token_expires_at - now <= TOKEN_RENEW_WINDOW and device.activated_at is not None:
        limit = device.activated_at + TOKEN_MAX_AGE
        if now < limit:
            renewed, device.token_hash = make_token(device)
            device.token_expires_at = min(now + TOKEN_TTL, limit)
    device.last_seen_at = now
    return DevicePrincipal(
        company_id=device.company_id, device_id=device.id, device_code=device.code, kind=device.kind,
        branch_id=device.branch_id, site_id=device.site_id, timezone=device.timezone,
        token_expires_at=device.token_expires_at, renewed_token=renewed,
    )


# ------------------------------------------------------------------------------------------------- registry
def serialize(device: AttendanceDevice) -> dict:
    actions = []
    if device.status == "PENDING_ACTIVATION":
        actions = ["reissue-claim-code", "retire"]
    elif device.status == "ACTIVE":
        actions = ["suspend", "retire"]
    elif device.status == "SUSPENDED":
        actions = ["resume", "retire"]
    return {
        "id": device.id, "code": device.code, "name": device.name, "kind": device.kind, "vendor": device.vendor,
        "integration": device.integration, "branch_id": device.branch_id, "site_id": device.site_id,
        "timezone": device.timezone, "status": device.status, "health": device.health,
        "paired": device.token_hash is not None,
        "token_expires_at": device.token_expires_at.isoformat() if device.token_expires_at else None,
        "claim_code_expires_at": device.claim_code_expires_at.isoformat() if device.claim_code_expires_at else None,
        "activated_at": device.activated_at.isoformat() if device.activated_at else None,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "row_version": device.row_version, "allowed_actions": actions,
    }


def _get(db: Session, company_id: int, device_id: int) -> AttendanceDevice:
    device = db.query(AttendanceDevice).filter(AttendanceDevice.company_id == company_id,
                                               AttendanceDevice.id == device_id).first()
    if device is None or device.kind != "KIOSK":
        raise not_found("Device")
    return device


def _issue_claim(device: AttendanceDevice, now: datetime) -> dict:
    code = new_claim_code()
    device.claim_code_hash = _claim_hash(code)
    device.claim_code_expires_at = now + CLAIM_CODE_TTL
    device.claim_code_failed_attempts = 0
    return {"claim_code": code, "claim_code_expires_at": device.claim_code_expires_at.isoformat()}


def list_devices(db: Session, principal: Principal, *, status: Optional[str] = None, q: Optional[str] = None,
                 skip: int = 0, limit: int = 25) -> dict:
    query = db.query(AttendanceDevice).filter(AttendanceDevice.company_id == principal.company_id,
                                              AttendanceDevice.kind == "KIOSK")
    if status:
        if status not in DEVICE_STATUSES:
            raise AppError(400, "Unknown device status.", "BAD_REQUEST")
        query = query.filter(AttendanceDevice.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((AttendanceDevice.code.ilike(like)) | (AttendanceDevice.name.ilike(like)))
    total = query.count()
    rows = query.order_by(AttendanceDevice.code, AttendanceDevice.id).offset(skip).limit(limit).all()
    return {"items": [serialize(d) for d in rows], "total": total, "skip": skip, "limit": limit}


def get_device(db: Session, principal: Principal, device_id: int) -> dict:
    return serialize(_get(db, principal.company_id, device_id))


def create_device(db: Session, principal: Principal, data) -> dict:
    """KIOSK only in AS-M2 (TERMINAL / AGENT arrive with ingest in AS-M7)."""
    from app.core import db_errors

    if data.kind != "KIOSK":
        raise AppError(422, "Only kiosk devices can be registered yet.", "FEATURE_DISABLED")
    zone = data.timezone
    if data.branch_id is not None:
        branch = db.query(Branch).filter(Branch.company_id == principal.company_id, Branch.id == data.branch_id).first()
        if branch is None:
            raise not_found("Branch")
        zone = zone or branch.timezone
    if zone and zone not in pytz.all_timezones_set:
        raise AppError(400, "Unknown time zone.", "INVALID_TIMEZONE")
    if not zone:
        from app.models.company import Company

        zone = db.get(Company, principal.company_id).timezone or "UTC"
    site_id = getattr(data, "site_id", None)
    if site_id is not None:
        from app.services import attendance_sites

        site = attendance_sites.load_site(db, principal.company_id, site_id)
        if site.status != "ACTIVE":
            raise AppError(422, "Choose an active site.", "VALIDATION_ERROR")
    now = _now()
    device = AttendanceDevice(
        company_id=principal.company_id, code=data.code.strip().upper(), name=data.name.strip(), kind="KIOSK",
        vendor="BROWSER", integration="BROWSER", branch_id=data.branch_id, site_id=site_id, timezone=zone,
        settings=dict(data.settings or {}), created_by_user_id=principal.user_id or None,
    )
    claim = _issue_claim(device, now)
    with db_errors.mapped():
        with db.begin_nested():
            db.add(device)
            db.flush()
    AuditLogService.log(db, principal, "Attendance", "DEVICE_REGISTERED", f"Kiosk {device.code} registered",
                        "attendance_device", device.id)
    return {**serialize(device), **claim}


def reissue_claim_code(db: Session, principal: Principal, device_id: int) -> dict:
    device = _get(db, principal.company_id, device_id)
    if device.status != "PENDING_ACTIVATION":
        raise conflict("Only a device waiting for pairing gets a claim code.", "INVALID_TRANSITION")
    claim = _issue_claim(device, _now())
    device.row_version += 1
    AuditLogService.log(db, principal, "Attendance", "DEVICE_REGISTERED", f"Claim code reissued for {device.code}",
                        "attendance_device", device.id)
    return {**serialize(device), **claim}


def transition(db: Session, principal: Principal, device_id: int, action: str, reason: Optional[str],
               expected_version: Optional[int] = None) -> dict:
    device = _get(db, principal.company_id, device_id)
    if expected_version is not None and expected_version != device.row_version:
        raise conflict("The device changed since you opened it.", "ROW_VERSION_CONFLICT")
    sources, target, audit = TRANSITIONS[action]
    if device.status not in sources:
        raise conflict(f"A {device.status.lower()} device cannot be {target.lower()}.", "INVALID_TRANSITION")
    now = _now()
    if target in ("SUSPENDED", "RETIRED"):
        device.token_hash, device.token_expires_at = None, None  # K-3: the token stops working at once
        device.claim_code_hash, device.claim_code_expires_at = None, None
    if target == "RETIRED":
        device.retired_at = now
    if action == "resume":
        # A suspended token was revoked; the kiosk pairs again with a fresh claim code (K-3).
        device.status = "PENDING_ACTIVATION"
        device.activated_at = None
        claim = _issue_claim(device, now)
        device.row_version += 1
        AuditLogService.log(db, principal, "Attendance", audit, f"Kiosk {device.code} resumed (re-pair)",
                            "attendance_device", device.id, details={"reason": reason} if reason else None)
        return {**serialize(device), **claim}
    device.status = target
    device.row_version += 1
    AuditLogService.log(db, principal, "Attendance", audit, f"Kiosk {device.code} {target.lower()}",
                        "attendance_device", device.id, details={"reason": reason} if reason else None)
    return serialize(device)


# ------------------------------------------------------------------------------------------------- pairing
def _claim_invalid() -> AppError:
    return AppError(422, "The device code or claim code is not valid.", "CLAIM_CODE_INVALID")


def pair(db: Session, company_id: int, device_code: str, claim_code: str, ip: Optional[str]) -> Tuple[dict, str]:
    """Activate a KIOSK with its claim code on a tenant-bound session; returns (body, token)."""
    device = db.query(AttendanceDevice).filter(
        AttendanceDevice.company_id == company_id, AttendanceDevice.code == (device_code or "").strip().upper(),
        AttendanceDevice.kind == "KIOSK").with_for_update().first()
    if device is None or device.status != "PENDING_ACTIVATION" or device.claim_code_hash is None:
        raise _claim_invalid()
    now = _now()
    if not tokens_equal(device.claim_code_hash, _claim_hash(claim_code)):
        device.claim_code_failed_attempts += 1
        if device.claim_code_failed_attempts >= CLAIM_CODE_MAX_FAILURES:
            device.claim_code_hash, device.claim_code_expires_at = None, None  # the admin issues a new code
        raise _claim_invalid()
    if device.claim_code_expires_at is None or device.claim_code_expires_at <= now:
        raise AppError(410, "This claim code has expired. Ask your administrator for a new one.", "CLAIM_CODE_EXPIRED")
    token, token_hash = make_token(device)
    device.token_hash = token_hash
    device.token_expires_at = now + TOKEN_TTL
    device.status, device.activated_at = "ACTIVE", now
    device.claim_code_hash, device.claim_code_expires_at, device.claim_code_failed_attempts = None, None, 0
    device.row_version += 1
    AuditLogService.log(db, None, "Attendance", "DEVICE_ACTIVATED", f"Kiosk {device.code} paired",
                        "attendance_device", device.id, ip, company_id=company_id, actor_type="device",
                        actor_id=device.id)
    return {"device": {"id": device.id, "code": device.code, "name": device.name, "timezone": device.timezone},
            "token_expires_at": device.token_expires_at.isoformat()}, token
