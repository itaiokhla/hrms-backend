"""Request bodies of capture v2 (AS 11 §6.8, §6.12, §6.13; SP-AS-053)."""

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from app.schemas.attendance import EventMeta
from app.schemas.auth import StrictModel

Direction = Literal["IN", "OUT", "BREAK_START", "BREAK_END"]


class PunchMeta(EventMeta):
    """The JSON ``meta`` part of ``POST /attendance/me/punch`` (today's EventMeta + the v2 fields)."""

    direction: Direction
    break_kind: Optional[Literal["LUNCH", "REST", "PERSONAL", "OTHER"]] = None
    # WEB = desktop browser, MOBILE = the installed /punch PWA; both authenticate with the user session (01 §2.1).
    capture_source: Literal["WEB", "MOBILE"] = "WEB"
    work_location_choice: Optional[Literal["ON_SITE", "REMOTE", "FIELD"]] = None
    qr_payload: Optional[str] = Field(None, max_length=256)


class KioskPairIn(StrictModel):
    device_code: str = Field(..., min_length=1, max_length=40)
    claim_code: str = Field(..., min_length=4, max_length=20)


class KioskIdentifyIn(StrictModel):
    method: Literal["FACE", "PIN", "QR", "CARD"]
    value: Optional[str] = Field(None, min_length=1, max_length=64)  # PIN / QR / CARD


class KioskPunchIn(StrictModel):
    kiosk_ticket: str = Field(..., min_length=10, max_length=1000)
    direction: Direction


class DeviceIn(StrictModel):
    code: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(..., min_length=1, max_length=100)
    kind: Literal["KIOSK", "TERMINAL", "AGENT"] = "KIOSK"
    branch_id: Optional[int] = None
    site_id: Optional[int] = None  # the kiosk's fixed site (kiosk punches are never geofenced, 01 §9.2)
    timezone: Optional[str] = Field(None, max_length=64)
    settings: Dict[str, Any] = Field(default_factory=dict)


class DeviceAction(StrictModel):
    reason: Optional[str] = Field(None, max_length=500)


class ReviewDecideIn(StrictModel):
    status: Literal["ACKNOWLEDGED", "APPROVED", "REJECTED", "RESOLVED"]
    note: Optional[str] = Field(None, max_length=500)


class ReviewFilter(StrictModel):
    kind: List[str] = Field(default_factory=list)


# ------------------------------------------------------------------------------------------------ sites (AS 11 §6.14)
SiteKind = Literal["OFFICE", "PLANT", "STORE", "CLINIC", "WAREHOUSE", "CLIENT_SITE", "PROJECT_SITE", "OTHER"]


class SitePatch(StrictModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    kind: Optional[SiteKind] = None
    legal_entity_id: Optional[int] = None
    branch_id: Optional[int] = None
    timezone: Optional[str] = Field(None, max_length=64)
    geometry_type: Optional[Literal["CIRCLE", "POLYGON", "NONE"]] = None
    center_lat: Optional[float] = Field(None, ge=-90, le=90, allow_inf_nan=False)
    center_lng: Optional[float] = Field(None, ge=-180, le=180, allow_inf_nan=False)
    radius_m: Optional[int] = Field(None, ge=1, le=100_000)
    polygon: Optional[List[List[float]]] = Field(None, max_length=65)
    ip_ranges: Optional[List[str]] = Field(None, max_length=50)
    outdoor: Optional[bool] = None
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None


class SiteIn(SitePatch):
    code: str = Field(..., min_length=1, max_length=30, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(..., min_length=1, max_length=100)
    kind: SiteKind = "OFFICE"
    geometry_type: Literal["CIRCLE", "POLYGON", "NONE"] = "CIRCLE"


class SiteAction(StrictModel):
    reason: Optional[str] = Field(None, max_length=500)


# ------------------------------------------------------------------------------------------------ privacy (§6.15)
Purpose = Literal["BIOMETRIC_FACE", "BIOMETRIC_DEVICE", "PUNCH_PHOTO", "LOCATION_AT_PUNCH", "LOCATION_TRACKING",
                  "NIGHT_WORK", "OVERTIME", "COMPRESSED_WEEK", "REST_WAIVER", "SCHEDULE_CHANGE_ACCEPTED",
                  "WEEKLY_HOURS_OPT_OUT"]


class MyConsentIn(StrictModel):
    purpose: Purpose
    status: Literal["GRANTED", "DECLINED", "WITHDRAWN"]
    notice_version: Optional[str] = Field(None, min_length=1, max_length=32)
    subject_ref: Optional[Dict[str, Any]] = None


class HrConsentIn(MyConsentIn):
    employee_id: int
    legal_basis: Optional[Literal["CONSENT", "EMPLOYMENT_LEGITIMATE_USE", "LEGAL_OBLIGATION", "CONTRACT"]] = None
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    notice_period_days: Optional[int] = Field(None, ge=0, le=365)


class PolicyDocumentPatch(StrictModel):
    title: Optional[str] = Field(None, min_length=1, max_length=150)
    legal_entity_id: Optional[int] = None
    effective_from: Optional[date] = None
    requires_acknowledgement: Optional[bool] = None
    purposes: Optional[List[Purpose]] = None
    summary: Optional[str] = Field(None, max_length=2000)
    privacy_contact: Optional[str] = Field(None, max_length=200)
    document_attachment_id: Optional[int] = None
    status: Optional[Literal["DRAFT", "PUBLISHED", "RETIRED"]] = None


class PolicyDocumentIn(StrictModel):
    code: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._-]+$")
    version: str = Field(..., min_length=1, max_length=16)
    title: str = Field(..., min_length=1, max_length=150)
    legal_entity_id: Optional[int] = None
    effective_from: date
    requires_acknowledgement: bool = False
    purposes: List[Purpose] = Field(default_factory=list)
    summary: Optional[str] = Field(None, max_length=2000)
    privacy_contact: Optional[str] = Field(None, max_length=200)
    document_attachment_id: Optional[int] = None
