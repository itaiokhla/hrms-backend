"""Versioned brand profiles and effective-brand resolution (01-white-label §4, §9).

Levels: PLATFORM -> PARTNER (optional, may lock fields) -> TENANT -> LEGAL_ENTITY
(documents only). Each owner has at most one DRAFT and one PUBLISHED version; older
published versions become ARCHIVED and stay retrievable. Only published versions take
part in resolution, and each field is resolved independently:

    partner.value   if the field is locked by the partner
    tenant.value    else if set and the tenant is entitled to that field
    partner.value   else if set
    platform.value  otherwise

Reads of platform and partner profiles use the system session (the tenant
application role cannot see them); tenant drafts are read and written through the
caller's RLS-bound session.
"""

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.config import settings
from app.core.entitlements import ENTITLEMENTS, MINIMUM_PLAN, entitlements_for, normalize_plan
from app.core.errors import AppError, bad_request, conflict, forbidden, not_found
from app.core.security import utcnow
from app.database.db import system_session
from app.models.branding import (
    ASSET_KINDS,
    FONTS,
    RADII,
    SIDEBAR_STYLES,
    BrandAsset,
    BrandProfile,
    CommercialBrandAsset,
    CommercialBrandProfile,
)
from app.models.company import Company, Partner
from app.services import brand_asset_service, brand_theme

logger = logging.getLogger(__name__)

PLATFORM, PARTNER, TENANT, LEGAL_ENTITY = "PLATFORM", "PARTNER", "TENANT", "LEGAL_ENTITY"

SUPPORTED_LOCALES = (
    "en", "en-IN", "en-US", "en-GB", "en-AE", "ar", "ar-AE", "hi", "hi-IN", "fr", "fr-FR", "de", "de-DE",
    "es", "es-ES", "he", "ur",
)
RTL_LANGUAGES = {"ar", "he", "ur", "fa"}

TEXT_FIELDS = {
    # field: (label, min length, max length)
    "display_name": ("Company name", 2, 150),
    "product_name": ("Product name", 2, 40),
    "short_name": ("Short name", 1, 12),
    "tagline": ("Tagline", 1, 80),
    "legal_footer": ("Legal footer", 1, 300),
    "email_sender_name": ("Email sender name", 2, 100),
}
URL_FIELDS = {
    "support_url": "Support page", "help_center_url": "Help center", "terms_url": "Terms of service",
    "privacy_url": "Privacy policy", "status_page_url": "Status page",
}
ENUM_FIELDS = {"sidebar_style": SIDEBAR_STYLES, "radius": RADII, "font": FONTS}
COLOR_FIELDS = {"primary_color": "Primary colour", "accent_color": "Accent colour"}
ASSET_FIELDS = {kind: f"{kind}_asset_id" for kind in ASSET_KINDS}
OTHER_FIELDS = {
    "support_email": "Support email", "support_phone": "Support phone", "default_locale": "Default language",
    "show_powered_by": "Show 'Powered by'",
}
BRAND_FIELDS: Tuple[str, ...] = (
    tuple(TEXT_FIELDS) + tuple(URL_FIELDS) + tuple(ENUM_FIELDS) + tuple(COLOR_FIELDS) + tuple(OTHER_FIELDS)
    + tuple(ASSET_FIELDS)
)
FIELD_LABELS = {
    **{k: v[0] for k, v in TEXT_FIELDS.items()}, **URL_FIELDS, **COLOR_FIELDS, **OTHER_FIELDS,
    "sidebar_style": "Sidebar style", "radius": "Corner style", "font": "Font",
    **{k: brand_asset_service.KIND_RULES[k].label for k in ASSET_FIELDS},
}
# Which entitlement a tenant needs to set each field (default: branding.logo_colors).
FIELD_ENTITLEMENT = {
    "product_name": "branding.product_name",
    "short_name": "branding.product_name",
    "email_sender_name": "branding.product_name",
}
# A legal entity's document brand may only set these.
ENTITY_FIELDS = ("document_logo", "accent_color", "legal_footer")

PLATFORM_DEFAULTS = {
    "display_name": "Intertech HRMS",
    "product_name": "Intertech HRMS",
    "short_name": "HRMS",
    "tagline": "People, attendance, leave and payroll in one place.",
    "primary_color": "#4F46E5",
    "accent_color": "#1FA6B8",
    "sidebar_style": "DARK",
    "radius": "ROUNDED",
    "font": "INTER",
    "default_locale": "en",
    "show_powered_by": True,
}
PLATFORM_POWERED_BY = "Powered by Intertech HRMS"

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Owner:
    kind: str
    company_id: Optional[int] = None
    partner_id: Optional[int] = None
    legal_entity_id: Optional[int] = None

    @property
    def is_commercial(self) -> bool:
        return self.kind in (PLATFORM, PARTNER)

    @property
    def model(self):
        return CommercialBrandProfile if self.is_commercial else BrandProfile

    @property
    def asset_model(self):
        return CommercialBrandAsset if self.is_commercial else BrandAsset

    def profiles(self, db: Session):
        model = self.model
        query = db.query(model).filter(model.owner_type == self.kind)
        if self.is_commercial:
            return query.filter(model.partner_id.is_(None) if self.partner_id is None else model.partner_id == self.partner_id)
        query = query.filter(model.company_id == self.company_id)
        if self.legal_entity_id is None:
            return query.filter(model.legal_entity_id.is_(None))
        return query.filter(model.legal_entity_id == self.legal_entity_id)

    def assets(self, db: Session):
        model = self.asset_model
        if self.kind == PLATFORM:
            return db.query(model).filter(model.owner_type == PLATFORM)
        if self.kind == PARTNER:
            return db.query(model).filter(model.owner_type == PARTNER, model.partner_id == self.partner_id)
        return db.query(model).filter(model.company_id == self.company_id)

    def lock_key(self) -> int:
        # Deterministic across processes (Python's hash() of strings is randomised per process).
        raw = f"{self.kind}:{self.company_id or 0}:{self.partner_id or 0}:{self.legal_entity_id or 0}"
        return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:4], "big") & 0x7FFFFFFF


PLATFORM_OWNER = Owner(PLATFORM)


def tenant_owner(company_id: int) -> Owner:
    return Owner(TENANT, company_id=company_id)


def entity_owner(company_id: int, legal_entity_id: int) -> Owner:
    return Owner(LEGAL_ENTITY, company_id=company_id, legal_entity_id=legal_entity_id)


def partner_owner(partner_id: int) -> Owner:
    return Owner(PARTNER, partner_id=partner_id)


# ---------------------------------------------------------------------------
# Field values
# ---------------------------------------------------------------------------
def _column(field_name: str) -> str:
    return ASSET_FIELDS.get(field_name, field_name)


def field_value(profile, field_name: str):
    return getattr(profile, _column(field_name)) if profile is not None else None


def _is_set(value) -> bool:
    return value is not None and value != ""


def validate_field(field_name: str, value: Any) -> Any:
    """Normalise one incoming value; None/"" clears the field (inherit)."""
    if field_name not in BRAND_FIELDS:
        raise bad_request(f"Unknown brand field: {field_name}", "BRAND_FIELD_UNKNOWN")
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    label = FIELD_LABELS[field_name]
    if field_name in TEXT_FIELDS:
        _, low, high = TEXT_FIELDS[field_name]
        value = " ".join(str(value).split())
        if not low <= len(value) <= high:
            raise bad_request(f"{label} must be {low}-{high} characters.", "BRAND_FIELD_INVALID")
        if any(ch in value for ch in "<>{}"):
            raise bad_request(f"{label} cannot contain < > {{ }}.", "BRAND_FIELD_INVALID")
        return value
    if field_name in URL_FIELDS:
        value = str(value).strip()
        if len(value) > 500 or not re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?(/[^\s<>\"']*)?$", value):
            raise bad_request(f"{label} must be an https:// address.", "BRAND_FIELD_INVALID")
        return value
    if field_name in ENUM_FIELDS:
        value = str(value).upper()
        if value not in ENUM_FIELDS[field_name]:
            raise bad_request(f"{label} must be one of {', '.join(ENUM_FIELDS[field_name])}.", "BRAND_FIELD_INVALID")
        return value
    if field_name in COLOR_FIELDS:
        value = str(value).strip().upper()
        if not brand_theme.is_hex(value):
            raise bad_request(f"{label} must be a colour like #0F766E.", "BRAND_FIELD_INVALID")
        return value
    if field_name == "support_email":
        try:
            return validate_email(str(value).strip(), check_deliverability=False).normalized
        except EmailNotValidError:
            raise bad_request("Support email is not a valid email address.", "BRAND_FIELD_INVALID")
    if field_name == "support_phone":
        value = re.sub(r"[\s().-]", "", str(value))
        if not E164.match(value):
            raise bad_request("Support phone must be in international format, e.g. +919876543210.", "BRAND_FIELD_INVALID")
        return value
    if field_name == "default_locale":
        value = str(value).strip()
        if value not in SUPPORTED_LOCALES:
            raise bad_request("Choose a supported language.", "BRAND_FIELD_INVALID")
        return value
    if field_name == "show_powered_by":
        if not isinstance(value, bool):
            raise bad_request("Show 'Powered by' must be true or false.", "BRAND_FIELD_INVALID")
        return value
    if field_name in ASSET_FIELDS:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise bad_request(f"{label}: invalid image reference.", "BRAND_FIELD_INVALID")
    return value


def entitlement_for_field(field_name: str, value: Any) -> str:
    if field_name == "show_powered_by":
        return "branding.remove_powered_by" if value is False else "branding.logo_colors"
    return FIELD_ENTITLEMENT.get(field_name, "branding.logo_colors")


def locale_direction(locale: Optional[str]) -> str:
    return "rtl" if (locale or "en").split("-")[0].lower() in RTL_LANGUAGES else "ltr"


# ---------------------------------------------------------------------------
# Loading published chains (system session; cached per process)
# ---------------------------------------------------------------------------
def _published(db: Session, owner: Owner):
    return owner.profiles(db).filter(owner.model.status == "PUBLISHED").first()


def _asset_dict(asset) -> Optional[dict]:
    if asset is None:
        return None
    return {
        "id": asset.id, "kind": asset.kind, "sha256": asset.sha256, "ext": asset.ext, "width": asset.width,
        "height": asset.height, "derivatives": asset.derivatives or {},
    }


def _profile_dict(db: Session, owner: Owner, profile) -> Optional[dict]:
    """Plain, session-independent copy of a profile with its assets resolved."""
    if profile is None:
        return None
    data = {f: field_value(profile, f) for f in BRAND_FIELDS}
    asset_ids = [data[k] for k in ASSET_FIELDS if data[k]]
    assets = {}
    if asset_ids:
        model = owner.asset_model
        assets = {a.id: a for a in owner.assets(db).filter(model.id.in_(asset_ids)).all()}
    for kind in ASSET_FIELDS:
        data[kind] = _asset_dict(assets.get(data[kind])) if data[kind] else None
    return {
        "id": profile.id, "version": profile.version, "status": profile.status, "owner": owner.kind,
        "fields": data, "locked_fields": list(getattr(profile, "locked_fields", None) or []),
        "powered_by_label": getattr(profile, "powered_by_label", None),
        "published_at": profile.published_at.isoformat() if profile.published_at else None,
    }


@dataclass
class Chain:
    company_id: Optional[int]
    company_name: Optional[str]
    company_status: str
    plan: str
    entitlements: frozenset
    partner: Optional[dict]
    platform_profile: Optional[dict]
    partner_profile: Optional[dict]
    tenant_profile: Optional[dict]
    brand_disabled: bool = False


_chain_cache: Dict[Optional[int], Tuple[float, Chain]] = {}
_chain_lock = threading.Lock()


def invalidate_cache(company_id: Optional[int] = None) -> None:
    with _chain_lock:
        if company_id is None:
            _chain_cache.clear()
        else:
            _chain_cache.pop(company_id, None)


def load_chain(company_id: Optional[int], *, tenant_profile_override=None, use_cache: bool = True) -> Chain:
    """The published PLATFORM -> PARTNER -> TENANT chain for a company (or the platform alone)."""
    now = time.monotonic()
    if use_cache and tenant_profile_override is None:
        with _chain_lock:
            hit = _chain_cache.get(company_id)
            if hit and hit[0] > now:
                return hit[1]
    with system_session() as db:
        platform = _profile_dict(db, PLATFORM_OWNER, _published(db, PLATFORM_OWNER))
        if company_id is None:
            chain = Chain(None, None, "active", "trial", entitlements_for("trial"), None, platform, None, None)
        else:
            company = db.get(Company, company_id)
            if company is None:
                raise not_found("Company")
            partner = db.get(Partner, company.partner_id) if company.partner_id else None
            partner_profile = None
            if partner is not None and partner.status == "active":
                owner = partner_owner(partner.id)
                partner_profile = _profile_dict(db, owner, _published(db, owner))
            tenant = tenant_profile_override
            if tenant is None:
                owner = tenant_owner(company_id)
                tenant = _profile_dict(db, owner, _published(db, owner))
            grants = partner.tenant_entitlements if partner is not None else None
            chain = Chain(
                company_id=company.id,
                company_name=company.name,
                company_status=company.status,
                plan=normalize_plan(company.subscription_plan),
                entitlements=entitlements_for(company.subscription_plan, grants),
                partner={"id": partner.id, "name": partner.name, "status": partner.status} if partner else None,
                platform_profile=platform,
                partner_profile=partner_profile,
                tenant_profile=tenant,
                brand_disabled=company.brand_disabled_at is not None,
            )
    if use_cache and tenant_profile_override is None:
        with _chain_lock:
            _chain_cache[company_id] = (now + settings.brand_cache_seconds, chain)
    return chain


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
@dataclass
class EffectiveBrand:
    values: Dict[str, Any]
    sources: Dict[str, dict]
    tokens: Optional[dict]
    version: str
    chain: Chain
    warnings: List[str] = field(default_factory=list)


def _level_value(profile: Optional[dict], field_name: str):
    if profile is None:
        return None
    return profile["fields"].get(field_name)


def resolve(chain: Chain) -> EffectiveBrand:
    values: Dict[str, Any] = {}
    sources: Dict[str, dict] = {}
    partner_profile = chain.partner_profile
    tenant_profile = None if chain.brand_disabled else chain.tenant_profile
    locked = set(partner_profile["locked_fields"]) if partner_profile else set()
    partner_name = chain.partner["name"] if chain.partner else None

    for name in BRAND_FIELDS:
        partner_value = _level_value(partner_profile, name)
        tenant_value = _level_value(tenant_profile, name)
        platform_value = _level_value(chain.platform_profile, name)
        if platform_value is None and name in PLATFORM_DEFAULTS:
            platform_value = PLATFORM_DEFAULTS[name]
        if name in locked and _is_set(partner_value):
            values[name], sources[name] = partner_value, {"level": PARTNER, "reason": "LOCKED", "by": partner_name}
            continue
        if _is_set(tenant_value):
            needed = entitlement_for_field(name, tenant_value)
            if needed in chain.entitlements:
                values[name], sources[name] = tenant_value, {"level": TENANT, "reason": "SET"}
                continue
            sources_reason = {"reason": "NOT_ENTITLED", "entitlement": needed}
        else:
            sources_reason = {}
        if _is_set(partner_value):
            values[name], sources[name] = partner_value, {"level": PARTNER, "reason": "SET", "by": partner_name, **sources_reason}
        else:
            values[name], sources[name] = platform_value, {"level": PLATFORM, "reason": "DEFAULT", **sources_reason}

    # The company name is per tenant: it never inherits from the partner or platform unless locked.
    if chain.company_name and sources["display_name"]["level"] != TENANT and sources["display_name"]["reason"] != "LOCKED":
        values["display_name"] = chain.company_name
        sources["display_name"] = {"level": TENANT, "reason": "COMPANY_NAME"}

    tokens = None
    colour_levels = {sources[k]["level"] for k in ("primary_color", "accent_color", "sidebar_style")}
    if colour_levels != {PLATFORM} or (chain.platform_profile and _is_set(_level_value(chain.platform_profile, "primary_color"))):
        tokens = brand_theme.derive_tokens(values["primary_color"], values.get("accent_color"), values.get("sidebar_style") or "DARK")

    version = _version_hash(chain)
    return EffectiveBrand(values, sources, tokens, version, chain, list(tokens["warnings"]) if tokens else [])


def _version_hash(chain: Chain) -> str:
    parts = [
        chain.platform_profile and chain.platform_profile["id"],
        chain.partner_profile and chain.partner_profile["id"],
        None if chain.brand_disabled else (chain.tenant_profile and chain.tenant_profile["id"]),
        chain.plan, sorted(chain.entitlements), chain.company_name,
        chain.tenant_profile and chain.tenant_profile.get("status"),
    ]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def effective_for_company(company_id: Optional[int]) -> EffectiveBrand:
    return resolve(load_chain(company_id))


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
ASSET_URL_PREFIX = "/api/v1/public/brand-assets/"


def asset_url(asset: Optional[dict], derivative: Optional[str] = None) -> Optional[str]:
    if not asset:
        return None
    if derivative:
        d = (asset.get("derivatives") or {}).get(derivative)
        if not d:
            return None
        return ASSET_URL_PREFIX + brand_asset_service.file_name(d["sha256"], d["ext"])
    return ASSET_URL_PREFIX + brand_asset_service.file_name(asset["sha256"], asset["ext"])


def _logos(values: Dict[str, Any]) -> dict:
    light, dark, mark = values.get("logo_light"), values.get("logo_dark"), values.get("mark")
    return {
        "light": asset_url(light, "display") or asset_url(light),
        "dark": asset_url(dark, "display") or asset_url(dark),
        "mark": asset_url(mark, "pwa-192") or asset_url(mark),
        "email": asset_url(values.get("email_logo"), "email") or asset_url(light, "email"),
        "login_image": asset_url(values.get("login_image")),
    }


def _icons(values: Dict[str, Any]) -> dict:
    mark = values.get("mark")
    if not mark:
        return {}
    return {
        name: asset_url(mark, name)
        for name in ("favicon-16", "favicon-32", "favicon-48", "favicon-ico", "apple-touch-180", "pwa-192", "pwa-512", "maskable-512")
    }


def public_payload(brand: EffectiveBrand, *, host_kind: str, host_status: str = "ACTIVE") -> dict:
    """Only the allow-listed public fields (01 §12). No ids, contacts, plans or drafts."""
    v = brand.values
    chain = brand.chain
    partner_label = chain.partner_profile.get("powered_by_label") if chain.partner_profile else None
    platform_label = (chain.platform_profile or {}).get("powered_by_label") or PLATFORM_POWERED_BY
    locale = v.get("default_locale") or "en"
    return {
        "version": brand.version,
        "host_kind": host_kind,
        "host_status": host_status,
        "product_name": v["product_name"],
        "display_name": v["display_name"],
        "short_name": v.get("short_name") or (v["product_name"] or "")[:12],
        "tagline": v.get("tagline"),
        "logos": _logos(v),
        "icons": _icons(v),
        "tokens": {"light": brand.tokens["light"], "dark": brand.tokens["dark"]} if brand.tokens else None,
        "theme_color": brand.tokens["theme_color"] if brand.tokens else PLATFORM_DEFAULTS["primary_color"],
        "font": v.get("font") or "INTER",
        "sidebar_style": v.get("sidebar_style") or "DARK",
        "radius": v.get("radius") or "ROUNDED",
        "support_url": v.get("support_url"),
        "help_center_url": v.get("help_center_url"),
        "terms_url": v.get("terms_url"),
        "privacy_url": v.get("privacy_url"),
        "status_page_url": v.get("status_page_url"),
        "show_powered_by": bool(v.get("show_powered_by", True)),
        "powered_by_label": partner_label or platform_label,
        "default_locale": locale,
        "dir": locale_direction(locale),
    }


def platform_public_payload(host_kind: str, host_status: str = "ACTIVE") -> dict:
    return public_payload(effective_for_company(None), host_kind=host_kind, host_status=host_status)


# ---------------------------------------------------------------------------
# Admin views (branding:manage)
# ---------------------------------------------------------------------------
def _serialize_profile(db: Session, owner: Owner, profile) -> Optional[dict]:
    data = _profile_dict(db, owner, profile)
    if data is None:
        return None
    data["row_version"] = profile.row_version
    data["contrast_report"] = profile.contrast_report
    data["change_summary"] = profile.change_summary
    data["published_by"] = profile.published_by
    data["created_by"] = profile.created_by
    data["restored_from_version"] = profile.restored_from_version
    for kind in ASSET_FIELDS:
        asset = data["fields"][kind]
        if asset:
            asset["url"] = asset_url(asset, "display") or asset_url(asset)
    return data


def entitlement_view(chain: Chain) -> List[dict]:
    return [
        {
            "key": key, "label": label, "enabled": key in chain.entitlements,
            "available_on": None if chain.partner else MINIMUM_PLAN[key],
            "decided_by_partner": bool(chain.partner),
        }
        for key, label in ENTITLEMENTS.items()
    ]


def admin_view(db: Session, company_id: int) -> dict:
    owner = tenant_owner(company_id)
    draft = owner.profiles(db).filter(BrandProfile.status == "DRAFT").first()
    published = owner.profiles(db).filter(BrandProfile.status == "PUBLISHED").first()
    chain = load_chain(company_id, use_cache=False)
    effective = resolve(chain)
    preview_chain = load_chain(company_id, tenant_profile_override=_profile_dict(db, owner, draft), use_cache=False) if draft else chain
    preview = resolve(preview_chain)
    partner_locks = chain.partner_profile["locked_fields"] if chain.partner_profile else []
    return {
        "draft": _serialize_profile(db, owner, draft),
        "published": _serialize_profile(db, owner, published),
        "effective": _admin_effective(effective),
        "preview": _admin_effective(preview),
        "sources": effective.sources,
        "preview_sources": preview.sources,
        "locked_fields": partner_locks,
        "partner": chain.partner,
        "plan": chain.plan,
        "entitlements": entitlement_view(chain),
        "field_entitlements": {f: entitlement_for_field(f, None) for f in BRAND_FIELDS},
        "unpublished_changes": len(diff_profiles(_profile_dict(db, owner, published), _profile_dict(db, owner, draft))) if draft else 0,
        "brand_disabled": chain.brand_disabled,
        "inherited": {
            **{
                f: (_level_value(chain.partner_profile, f) if _is_set(_level_value(chain.partner_profile, f))
                    else (_level_value(chain.platform_profile, f) or PLATFORM_DEFAULTS.get(f)))
                for f in BRAND_FIELDS if f not in ASSET_FIELDS
            },
            # Without its own value the company name is shown, never the platform's.
            "display_name": chain.company_name,
        },
    }


def _admin_effective(brand: EffectiveBrand) -> dict:
    payload = public_payload(brand, host_kind=TENANT)
    payload["contrast_report"] = brand.tokens["contrast_report"] if brand.tokens else []
    payload["warnings"] = brand.warnings
    payload["exact"] = brand.tokens["exact"] if brand.tokens else {"primary": PLATFORM_DEFAULTS["primary_color"], "accent": None}
    payload["values"] = {
        k: v for k, v in brand.values.items() if k not in ASSET_FIELDS
    }
    payload["legal_footer"] = brand.values.get("legal_footer")
    payload["support_email"] = brand.values.get("support_email")
    payload["support_phone"] = brand.values.get("support_phone")
    payload["email_sender_name"] = brand.values.get("email_sender_name")
    payload["document_logo"] = asset_url(brand.values.get("document_logo"), "document") or asset_url(brand.values.get("logo_light"), "document")
    return payload


def diff_profiles(before: Optional[dict], after: Optional[dict]) -> List[dict]:
    changes = []
    for name in BRAND_FIELDS:
        old = (before or {"fields": {}})["fields"].get(name)
        new = (after or {"fields": {}})["fields"].get(name)
        if name in ASSET_FIELDS:
            old_sha, new_sha = (old or {}).get("sha256"), (new or {}).get("sha256")
            if old_sha != new_sha:
                changes.append({"field": name, "label": FIELD_LABELS[name],
                                "from": "image" if old_sha else None, "to": "new image" if new_sha else None})
        elif old != new:
            changes.append({"field": name, "label": FIELD_LABELS[name], "from": old, "to": new})
    # Platform and partner profiles also carry their field locks and the "powered by" credit. An edit of only
    # those is a real change: without this the operator could never publish it ("no unpublished changes").
    # Tenant and legal-entity profiles have neither (empty on both sides), so nothing is added for them.
    old_locks = sorted((before or {}).get("locked_fields") or [])
    new_locks = sorted((after or {}).get("locked_fields") or [])
    if old_locks != new_locks:
        changes.append({"field": "locked_fields", "label": "Locked fields", "from": old_locks, "to": new_locks})
    old_label = (before or {}).get("powered_by_label") or None
    new_label = (after or {}).get("powered_by_label") or None
    if old_label != new_label:
        changes.append({"field": "powered_by_label", "label": "Powered-by label", "from": old_label, "to": new_label})
    return changes


# ---------------------------------------------------------------------------
# Draft editing, publishing and rollback
# ---------------------------------------------------------------------------
def _lock_owner(db: Session, owner: Owner) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(:ns, :key)"), {"ns": 5150, "key": owner.lock_key()})


def _copy_fields(source, target) -> None:
    for name in BRAND_FIELDS:
        column = _column(name)
        setattr(target, column, getattr(source, column) if source is not None else None)


def _next_version(db: Session, owner: Owner) -> int:
    model = owner.model
    current = owner.profiles(db).with_entities(func.max(model.version)).scalar()
    return (current or 0) + 1


def _new_profile(owner: Owner, **values):
    model = owner.model
    kwargs = {"owner_type": owner.kind}
    if owner.is_commercial:
        kwargs["partner_id"] = owner.partner_id
    else:
        kwargs["company_id"] = owner.company_id
        kwargs["legal_entity_id"] = owner.legal_entity_id
    kwargs.update(values)
    return model(**kwargs)


def get_or_create_draft(db: Session, owner: Owner, actor: str, actor_user_id: Optional[int] = None):
    model = owner.model
    draft = owner.profiles(db).filter(model.status == "DRAFT").with_for_update().first()
    if draft is not None:
        return draft
    _lock_owner(db, owner)
    draft = owner.profiles(db).filter(model.status == "DRAFT").first()
    if draft is not None:
        return draft
    published = owner.profiles(db).filter(model.status == "PUBLISHED").first()
    draft = _new_profile(owner, version=_next_version(db, owner), status="DRAFT", created_by=actor, row_version=1)
    if not owner.is_commercial:
        draft.created_by_user_id = actor_user_id
    _copy_fields(published, draft)
    if owner.is_commercial and published is not None:
        draft.locked_fields = list(published.locked_fields or [])
        draft.powered_by_label = published.powered_by_label
    db.add(draft)
    db.flush()
    return draft


def allowed_fields(owner: Owner) -> Tuple[str, ...]:
    return ENTITY_FIELDS if owner.kind == LEGAL_ENTITY else BRAND_FIELDS


def save_draft(
    db: Session,
    owner: Owner,
    changes: Dict[str, Any],
    *,
    actor: str,
    actor_user_id: Optional[int] = None,
    expected_row_version: Optional[int] = None,
    chain: Optional[Chain] = None,
    locked_fields: Optional[Iterable[str]] = None,
    powered_by_label: Optional[str] = None,
):
    """Apply a partial update to the owner's draft (created from the published version if needed)."""
    draft = get_or_create_draft(db, owner, actor, actor_user_id)
    if expected_row_version is not None and draft.row_version != expected_row_version:
        raise conflict("Someone else changed this draft. Reload to see their changes.", "BRAND_DRAFT_CHANGED")
    allowed = allowed_fields(owner)
    locked = set()
    if chain is not None and chain.partner_profile and owner.kind == TENANT:
        locked = set(chain.partner_profile["locked_fields"])
    for name, raw in changes.items():
        if name not in allowed:
            raise bad_request(f"{FIELD_LABELS.get(name, name)} cannot be set here.", "BRAND_FIELD_NOT_ALLOWED")
        value = validate_field(name, raw)
        current = getattr(draft, _column(name))
        if value == current:
            continue
        if name in locked:
            raise AppError(403, f"{FIELD_LABELS[name]} is locked by {chain.partner['name']}.", "BRAND_FIELD_LOCKED")
        if chain is not None and owner.kind == TENANT and value is not None:
            needed = entitlement_for_field(name, value)
            if needed not in chain.entitlements:
                raise AppError(403, f"{FIELD_LABELS[name]} is not included in your plan.", "BRAND_NOT_ENTITLED")
        if owner.kind == LEGAL_ENTITY and chain is not None and value is not None and "documents.entity_branding" not in chain.entitlements:
            raise AppError(403, "Document branding per legal entity is not included in your plan.", "BRAND_NOT_ENTITLED")
        if name in ASSET_FIELDS and value is not None:
            asset = owner.assets(db).filter(owner.asset_model.id == value).first()
            if asset is None or asset.kind != name:
                raise bad_request(f"{FIELD_LABELS[name]}: upload the image first.", "BRAND_ASSET_MISSING")
        setattr(draft, _column(name), value)
    if owner.is_commercial:
        if locked_fields is not None:
            unknown = set(locked_fields) - set(BRAND_FIELDS)
            if unknown:
                raise bad_request(f"Unknown fields cannot be locked: {', '.join(sorted(unknown))}")
            draft.locked_fields = sorted(set(locked_fields)) if owner.kind == PARTNER else []
        if powered_by_label is not None:
            label = " ".join(str(powered_by_label).split())[:80] or None
            draft.powered_by_label = label
    draft.row_version += 1
    db.flush()
    return draft


def discard_draft(db: Session, owner: Owner) -> None:
    owner.profiles(db).filter(owner.model.status == "DRAFT").delete(synchronize_session=False)


def validate_draft(db: Session, owner: Owner, chain: Optional[Chain]) -> dict:
    draft = owner.profiles(db).filter(owner.model.status == "DRAFT").first()
    published = owner.profiles(db).filter(owner.model.status == "PUBLISHED").first()
    errors: List[str] = []
    warnings: List[str] = []
    if draft is None:
        return {"valid": False, "errors": ["There are no unpublished changes."], "warnings": [], "changes": []}
    snapshot = _profile_dict(db, owner, draft)
    for kind in ASSET_FIELDS:
        asset = snapshot["fields"][kind]
        if getattr(draft, _column(kind)) and asset is None:
            errors.append(f"{FIELD_LABELS[kind]} is missing. Upload it again.")
        elif asset and brand_asset_service.read_asset(asset["sha256"], asset["ext"]) is None:
            errors.append(f"{FIELD_LABELS[kind]} file is missing or damaged. Upload it again.")
    if chain is not None and owner.kind == TENANT:
        locked = set(chain.partner_profile["locked_fields"]) if chain.partner_profile else set()
        for name in BRAND_FIELDS:
            value = snapshot["fields"][name]
            if not _is_set(value):
                continue
            if name in locked:
                warnings.append(f"{FIELD_LABELS[name]} is locked by {chain.partner['name']}; your value is not used.")
            elif entitlement_for_field(name, value) not in chain.entitlements:
                warnings.append(f"{FIELD_LABELS[name]} is not included in your plan; your value is not used.")
    primary = snapshot["fields"].get("primary_color")
    contrast_report: List[dict] = []
    if primary:
        derived = brand_theme.derive_tokens(primary, snapshot["fields"].get("accent_color"),
                                            snapshot["fields"].get("sidebar_style") or "DARK")
        contrast_report = derived["contrast_report"]
        warnings.extend(derived["warnings"])
        if not all(item["pass"] for item in contrast_report):
            errors.append("The colours could not be adjusted to readable contrast.")
    changes = diff_profiles(_profile_dict(db, owner, published), snapshot)
    if not changes:
        errors.append("There are no unpublished changes.")
    return {"valid": not errors, "errors": errors, "warnings": warnings, "changes": changes,
            "contrast_report": contrast_report, "row_version": draft.row_version}


def publish(
    db: Session,
    owner: Owner,
    *,
    actor: str,
    actor_user_id: Optional[int] = None,
    expected_row_version: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    chain: Optional[Chain] = None,
):
    model = owner.model
    _lock_owner(db, owner)
    if idempotency_key:
        done = owner.profiles(db).filter(model.idempotency_key == idempotency_key).first()
        if done is not None:
            return done, False
    draft = owner.profiles(db).filter(model.status == "DRAFT").with_for_update().first()
    if draft is None:
        raise conflict("There is no draft to publish. It may have just been published by someone else.", "BRAND_NO_DRAFT")
    if expected_row_version is not None and draft.row_version != expected_row_version:
        raise conflict("The draft changed since you reviewed it. Review the changes and publish again.", "BRAND_DRAFT_CHANGED")
    report = validate_draft(db, owner, chain)
    if not report["valid"]:
        raise AppError(422, " ".join(report["errors"]), "BRAND_INVALID")
    current = owner.profiles(db).filter(model.status == "PUBLISHED").with_for_update().first()
    if current is not None:
        current.status = "ARCHIVED"
        db.flush()
    _finalize(draft, report, actor, actor_user_id, idempotency_key, owner)
    db.flush()
    invalidate_cache(owner.company_id if not owner.is_commercial else None)
    return draft, True


def _finalize(profile, report: dict, actor: str, actor_user_id, idempotency_key, owner: Owner) -> None:
    profile.status = "PUBLISHED"
    profile.published_at = utcnow()
    profile.published_by = actor
    profile.change_summary = report["changes"]
    profile.contrast_report = report.get("contrast_report") or []
    profile.idempotency_key = idempotency_key
    primary = getattr(profile, "primary_color")
    if primary:
        derived = brand_theme.derive_tokens(primary, profile.accent_color, profile.sidebar_style or "DARK")
        profile.computed_tokens = {"light": derived["light"], "dark": derived["dark"], "exact": derived["exact"]}
    else:
        profile.computed_tokens = None
    if not owner.is_commercial:
        profile.published_by_user_id = actor_user_id


def restore(db: Session, owner: Owner, profile_id: int, *, actor: str, actor_user_id: Optional[int] = None):
    """Rollback: publish a copy of an earlier version as a new version (history is never rewritten)."""
    model = owner.model
    _lock_owner(db, owner)
    source = owner.profiles(db).filter(model.id == profile_id, model.status.in_(("ARCHIVED", "PUBLISHED"))).first()
    if source is None:
        raise not_found("Brand version")
    current = owner.profiles(db).filter(model.status == "PUBLISHED").with_for_update().first()
    if current is not None and current.id == source.id:
        raise bad_request("This version is already live.")
    draft = owner.profiles(db).filter(model.status == "DRAFT").first()
    version = _next_version(db, owner)
    if draft is not None:
        # Keep the unpublished draft, numbered after the restored version.
        draft.version = version + 1
        db.flush()
    previous = _profile_dict(db, owner, current)
    if current is not None:
        current.status = "ARCHIVED"
        db.flush()
    copy = _new_profile(owner, version=version, status="PUBLISHED", created_by=actor, row_version=1)
    _copy_fields(source, copy)
    if owner.is_commercial:
        copy.locked_fields = list(source.locked_fields or [])
        copy.powered_by_label = source.powered_by_label
    db.add(copy)
    db.flush()
    report = {
        "changes": diff_profiles(previous, _profile_dict(db, owner, copy)),
        "contrast_report": source.contrast_report or [],
    }
    copy.restored_from_version = source.version
    _finalize(copy, report, actor, actor_user_id, None, owner)
    db.flush()
    invalidate_cache(owner.company_id if not owner.is_commercial else None)
    return copy


def history(db: Session, owner: Owner, limit: int = 50) -> List[dict]:
    model = owner.model
    rows = (
        owner.profiles(db)
        .filter(model.status.in_(("PUBLISHED", "ARCHIVED")))
        .order_by(model.version.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id, "version": r.version, "status": r.status,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "published_by": r.published_by, "changes": r.change_summary or [],
            "restored_from_version": r.restored_from_version,
            "primary_color": r.primary_color,
        }
        for r in rows
    ]


def add_asset(db: Session, owner: Owner, kind: str, data: bytes, filename: Optional[str], actor: str):
    processed = brand_asset_service.process_upload(data, kind)
    return _record_asset(db, owner, processed, filename, actor)


def _record_asset(db: Session, owner: Owner, processed, filename: Optional[str], actor: str):
    model = owner.asset_model
    kwargs = dict(
        kind=processed.kind, sha256=processed.sha256, ext=processed.ext, mime=processed.mime,
        width=processed.width, height=processed.height, bytes=processed.bytes,
        original_filename=(filename or "")[:255] or None, derivatives=processed.derivatives, created_by=actor,
    )
    if owner.is_commercial:
        kwargs.update(owner_type=owner.kind, partner_id=owner.partner_id)
    else:
        kwargs.update(company_id=owner.company_id)
    asset = model(**kwargs)
    db.add(asset)
    db.flush()
    return asset


def ensure_mark(db: Session, owner: Owner, actor: str, actor_user_id: Optional[int] = None) -> None:
    """Before publishing a tenant brand without a square mark, generate one from the initials (01 §5.2)."""
    if owner.kind != TENANT:
        return
    draft = owner.profiles(db).filter(BrandProfile.status == "DRAFT").first()
    if draft is None or draft.mark_asset_id:
        return
    chain = load_chain(owner.company_id, tenant_profile_override=_profile_dict(db, owner, draft), use_cache=False)
    brand = resolve(chain)
    if brand.sources["mark"]["level"] != PLATFORM and brand.values.get("mark"):
        return  # a partner mark applies
    background, foreground = brand_theme.button_colors(brand.values["primary_color"])
    processed = brand_asset_service.initials_mark(brand.values["display_name"], background, foreground)
    asset = _record_asset(db, owner, processed, "generated-initials.png", "system")
    draft.mark_asset_id = asset.id
    db.flush()


def serialize_admin_profile(db: Session, owner: Owner, profile) -> Optional[dict]:
    return _serialize_profile(db, owner, profile)


def document_brand(company_id: int, legal_entity_id: Optional[int]) -> dict:
    """What a document issued now should look like: tenant brand plus the entity's document overrides."""
    brand = effective_for_company(company_id)
    entity_profile = None
    if legal_entity_id is not None:
        owner = entity_owner(company_id, legal_entity_id)
        with system_session() as db:
            entity_profile = _profile_dict(db, owner, _published(db, owner))
    values = brand.values
    logo = values.get("document_logo") or values.get("logo_light")
    # Documents use the exact brand colour (01 §5.7); a legal entity may set its own.
    colour = values.get("primary_color") or PLATFORM_DEFAULTS["primary_color"]
    footer = values.get("legal_footer")
    entitled = "documents.entity_branding" in brand.chain.entitlements
    if entity_profile and entitled:
        fields = entity_profile["fields"]
        logo = fields.get("document_logo") or logo
        colour = fields.get("accent_color") or colour
        footer = fields.get("legal_footer") or footer
    logo_file = None
    if logo:
        derivative = (logo.get("derivatives") or {}).get("document")
        logo_file = derivative or {"sha256": logo["sha256"], "ext": logo["ext"]}
    return {
        "tenant_profile_id": brand.chain.tenant_profile["id"] if brand.chain.tenant_profile else None,
        "entity_profile_id": entity_profile["id"] if (entity_profile and entitled) else None,
        "brand_version": brand.version,
        "display_name": values["display_name"],
        "product_name": values["product_name"],
        "logo": {"sha256": logo_file["sha256"], "ext": logo_file["ext"]} if logo_file else None,
        # Exact colour for the thin strip (no text on it); darkened band for white headings.
        "accent_strip": colour,
        "accent": brand_theme.accessible_accent_hex(colour),
        "legal_footer": footer,
        "show_powered_by": bool(values.get("show_powered_by", True)),
        "powered_by_label": (brand.chain.partner_profile or {}).get("powered_by_label")
        or (brand.chain.platform_profile or {}).get("powered_by_label") or PLATFORM_POWERED_BY,
    }
