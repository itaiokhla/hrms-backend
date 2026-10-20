"""White-label API: public brand, brand assets, brand editing, domains and email templates."""

from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, not_found
from app.core.hosts import PLATFORM, TENANT, UNKNOWN, request_host
from app.core.principal import Principal, get_principal_any_subscription, optional_principal, require
from app.core.rate_limit import client_ip, limiter
from app.core.security import BRAND_HINT_COOKIE, create_jwt, decode_jwt, verify_brand_hint
from app.database.db import get_db, system_session
from app.models.branding import BrandAsset, BrandProfile, CommercialBrandAsset, CommercialBrandProfile, EmailTemplateOverride
from app.models.company import Company
from app.schemas.auth import StrictModel
from app.services import (
    brand_asset_service,
    brand_service,
    domain_service,
    email_branding,
    email_templates,
)
from app.services.audit_log_service import AuditLogService
from app.services.email_service import EmailService

public = APIRouter(prefix="/api/v1/public", tags=["Public brand"])
router = APIRouter(prefix="/api/v1", tags=["Branding"])

BRAND_HINT_HEADER = "x-hrms-brand-hint"
PREVIEW_HEADER = "x-hrms-brand-preview"


# ---------------------------------------------------------------------------
# Public (host-resolved, no authentication)
# ---------------------------------------------------------------------------
def _preview_company(request: Request, token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    claims = decode_jwt(token, "brand_preview")
    context = request_host(request)
    if not claims or claims.get("h", "") != (context.host if context.trusted else ""):
        return None
    company_id = int(claims["cid"])
    if context.kind == TENANT and context.company_id != company_id:
        return None
    return company_id


@public.get("/brand")
def public_brand(request: Request, response: Response):
    """The effective published brand for this host (01 §12): allow-listed public fields only."""
    limiter.check(f"brand:ip:{client_ip(request)}", 600, 60)
    context = request_host(request)
    preview_company = _preview_company(request, request.headers.get(PREVIEW_HEADER))
    if preview_company is not None:
        with system_session() as db:
            payload = _preview_payload(db, preview_company, context.kind)
        response.headers["Cache-Control"] = "private, no-store"
        return payload

    if context.kind == UNKNOWN:
        payload = brand_service.platform_public_payload(UNKNOWN, "UNKNOWN")
    elif context.kind == TENANT:
        with system_session() as db:
            company = db.get(Company, context.company_id)
            status = "ACTIVE" if company is not None and company.status == "active" else "SUSPENDED"
        if status == "ACTIVE":
            payload = brand_service.public_payload(brand_service.effective_for_company(context.company_id),
                                                   host_kind=TENANT)
            payload.update(domain_service.host_settings(context.company_id, context.host))
        else:
            payload = brand_service.platform_public_payload(TENANT, "SUSPENDED")
    else:
        hint = request.headers.get(BRAND_HINT_HEADER) or request.cookies.get(BRAND_HINT_COOKIE)
        company_id = verify_brand_hint(hint, context.host) if hint else None
        payload = None
        if company_id is not None:
            with system_session() as db:
                company = db.get(Company, company_id)
                if company is not None and company.status == "active":
                    payload = brand_service.public_payload(brand_service.effective_for_company(company_id),
                                                           host_kind=PLATFORM)
        if payload is None:
            payload = brand_service.platform_public_payload(PLATFORM)
        payload["signed_in_brand"] = company_id is not None
    payload["login_scope"] = "TENANT" if context.kind == TENANT else ("NONE" if context.kind == UNKNOWN else "ANY")
    payload["is_platform_host"] = context.kind == PLATFORM
    response.headers["Cache-Control"] = "public, max-age=30"
    response.headers["Vary"] = "Host, X-HRMS-Host, X-HRMS-Brand-Hint, Cookie"
    response.headers["ETag"] = f'"{payload["version"]}"'
    return payload


def _preview_payload(db: Session, company_id: int, host_kind: str) -> dict:
    owner = brand_service.tenant_owner(company_id)
    draft = owner.profiles(db).filter(BrandProfile.status == "DRAFT").first()
    override = brand_service._profile_dict(db, owner, draft) if draft else None
    chain = brand_service.load_chain(company_id, tenant_profile_override=override, use_cache=False)
    payload = brand_service.public_payload(brand_service.resolve(chain), host_kind=host_kind)
    payload["preview"] = True
    payload["version"] = f"preview-{payload['version']}"
    return payload


def _asset_owners(db: Session, sha: str):
    """Every (owner, ever-published?) that references this file as a master or derivative."""
    owners = []
    # `sha` is validated hex, so it is safe inside the JSON path (derivative files).
    in_derivatives = f'$.* ? (@.sha256 == "{sha}")'
    tenant_rows = db.query(BrandAsset).filter(
        (BrandAsset.sha256 == sha) | BrandAsset.derivatives.op("@?")(in_derivatives)
    ).all()
    commercial_rows = db.query(CommercialBrandAsset).filter(
        (CommercialBrandAsset.sha256 == sha) | CommercialBrandAsset.derivatives.op("@?")(in_derivatives)
    ).all()
    for row in tenant_rows:
        owners.append(("TENANT", row.company_id, _ever_published(db, BrandProfile, row)))
    for row in commercial_rows:
        owners.append((row.owner_type, row.partner_id, _ever_published(db, CommercialBrandProfile, row)))
    return owners


def _ever_published(db: Session, model, asset) -> bool:
    column = getattr(model, f"{asset.kind}_asset_id")
    query = db.query(model.id).filter(column == asset.id, model.status.in_(("PUBLISHED", "ARCHIVED")))
    if model is BrandProfile:
        query = query.filter(BrandProfile.company_id == asset.company_id)
    return query.first() is not None


@public.get("/brand-assets/{name}")
def brand_asset(name: str, request: Request):
    """Serve a processed brand image by content hash, only to hosts or sessions allowed to see it."""
    parsed = brand_asset_service.parse_asset_name(name)
    if parsed is None:
        raise not_found("File")
    sha, ext = parsed
    context = request_host(request)
    with system_session() as db:
        owners = _asset_owners(db, sha)
        host_company = context.company_id if context.kind == TENANT else None
        if context.kind == PLATFORM and context.trusted:
            hint = request.cookies.get(BRAND_HINT_COOKIE)
            host_company = verify_brand_hint(hint, context.host) if hint else None
        public_ok = any(
            published and (kind in ("PLATFORM", "PARTNER") or (kind == "TENANT" and owner_id == host_company))
            for kind, owner_id, published in owners
        )
    cache = "public, max-age=31536000, immutable"
    if not public_ok:
        principal = optional_principal(request)
        tenant_owner_ids = {owner_id for kind, owner_id, _ in owners if kind == "TENANT"}
        if principal is None or principal.company_id not in tenant_owner_ids:
            raise not_found("File")
        published_for_me = any(p for kind, owner_id, p in owners if kind == "TENANT" and owner_id == principal.company_id)
        if not published_for_me and not principal.has("branding:manage"):
            raise not_found("File")
        cache = "private, max-age=31536000, immutable" if published_for_me else "private, no-store"
    data = brand_asset_service.read_asset(sha, ext)
    if data is None:
        raise not_found("File")
    return Response(
        content=data,
        media_type=brand_asset_service.MEDIA_TYPES[ext],
        headers={
            "Cache-Control": cache,
            "Vary": "Host, X-HRMS-Host, Cookie",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# Signed-in users: the published brand of their own tenant (live update)
# ---------------------------------------------------------------------------
@router.get("/branding/current")
def current_brand(principal: Principal = Depends(get_principal_any_subscription)):
    context_kind = TENANT
    payload = brand_service.public_payload(brand_service.effective_for_company(principal.company_id), host_kind=context_kind)
    return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# Brand editing (branding:manage)
# ---------------------------------------------------------------------------
class DraftUpdate(StrictModel):
    changes: Dict[str, Any] = Field(default_factory=dict)
    row_version: Optional[int] = None


class PublishRequest(StrictModel):
    row_version: Optional[int] = None


def _actor(principal: Principal) -> str:
    return principal.email


def _chain(company_id: int):
    return brand_service.load_chain(company_id, use_cache=False)


@router.get("/branding")
def get_branding(principal: Principal = Depends(require("branding:manage")), db: Session = Depends(get_db)):
    return JSONResponse(brand_service.admin_view(db, principal.company_id), headers={"Cache-Control": "private, no-store"})


@router.put("/branding/draft")
def update_draft(data: DraftUpdate, principal: Principal = Depends(require("branding:manage")),
                 db: Session = Depends(get_db)):
    owner = brand_service.tenant_owner(principal.company_id)
    draft = brand_service.save_draft(
        db, owner, data.changes, actor=_actor(principal), actor_user_id=principal.user_id,
        expected_row_version=data.row_version, chain=_chain(principal.company_id),
    )
    if data.changes:
        AuditLogService.log(db, principal, "Branding", "DRAFT", f"Edited brand draft: {', '.join(sorted(data.changes))}",
                            "brand_profile", draft.id)
    db.commit()
    return brand_service.admin_view(db, principal.company_id)


@router.delete("/branding/draft")
def discard_draft(principal: Principal = Depends(require("branding:manage")), db: Session = Depends(get_db)):
    brand_service.discard_draft(db, brand_service.tenant_owner(principal.company_id))
    AuditLogService.log(db, principal, "Branding", "DRAFT_DISCARDED", "Discarded unpublished brand changes")
    db.commit()
    return brand_service.admin_view(db, principal.company_id)


@router.post("/branding/assets")
async def upload_asset(kind: str = Form(...), file: UploadFile = File(...),
                       principal: Principal = Depends(require("branding:manage")), db: Session = Depends(get_db)):
    rule = brand_asset_service.KIND_RULES.get(kind)
    if rule is None:
        raise bad_request("Unknown image kind.", "BRAND_ASSET_KIND")
    data = await file.read(rule.max_bytes + 1)
    owner = brand_service.tenant_owner(principal.company_id)
    asset = brand_service.add_asset(db, owner, kind, data, file.filename, _actor(principal))
    AuditLogService.log(db, principal, "Branding", "ASSET_UPLOADED", f"Uploaded {rule.label} ({asset.width}x{asset.height})",
                        "brand_asset", asset.id)
    db.commit()
    return _asset_response(asset)


def _asset_response(asset) -> dict:
    data = brand_service._asset_dict(asset)
    data["url"] = brand_service.asset_url(data, "display") or brand_service.asset_url(data)
    data["derivative_urls"] = {name: brand_service.asset_url(data, name) for name in (asset.derivatives or {})}
    return data


@router.post("/branding/draft/validate")
def validate_draft(principal: Principal = Depends(require("branding:manage")), db: Session = Depends(get_db)):
    owner = brand_service.tenant_owner(principal.company_id)
    return brand_service.validate_draft(db, owner, _chain(principal.company_id))


@router.post("/branding/publish")
def publish(data: PublishRequest = Body(PublishRequest()), idempotency_key: Optional[str] = Header(None, max_length=80),
            principal: Principal = Depends(require("branding:manage")), db: Session = Depends(get_db)):
    owner = brand_service.tenant_owner(principal.company_id)
    brand_service.ensure_mark(db, owner, _actor(principal), principal.user_id)
    profile, created = brand_service.publish(
        db, owner, actor=_actor(principal), actor_user_id=principal.user_id, expected_row_version=data.row_version,
        idempotency_key=idempotency_key, chain=_chain(principal.company_id),
    )
    if created:
        changes = ", ".join(f"{c['label']}: {c['from'] or '—'} -> {c['to'] or '—'}" for c in profile.change_summary or [])
        AuditLogService.log(db, principal, "Branding", "PUBLISHED", f"Published brand v{profile.version}: {changes}"[:500],
                            "brand_profile", profile.id)
    db.commit()
    return {"version": profile.version, "published": brand_service.serialize_admin_profile(db, owner, profile),
            "brand": brand_service.public_payload(brand_service.effective_for_company(principal.company_id), host_kind=TENANT)}


@router.get("/branding/versions")
def versions(principal: Principal = Depends(require("branding:manage")), db: Session = Depends(get_db)):
    return brand_service.history(db, brand_service.tenant_owner(principal.company_id))


@router.post("/branding/versions/{profile_id}/restore")
def restore(profile_id: int, principal: Principal = Depends(require("branding:manage")), db: Session = Depends(get_db)):
    owner = brand_service.tenant_owner(principal.company_id)
    profile = brand_service.restore(db, owner, profile_id, actor=_actor(principal), actor_user_id=principal.user_id)
    AuditLogService.log(db, principal, "Branding", "RESTORED",
                        f"Restored brand v{profile.restored_from_version} as v{profile.version}", "brand_profile", profile.id)
    db.commit()
    return {"version": profile.version,
            "brand": brand_service.public_payload(brand_service.effective_for_company(principal.company_id), host_kind=TENANT)}


@router.post("/branding/preview-token")
def preview_token(request: Request, principal: Principal = Depends(require("branding:manage"))):
    context = request_host(request)
    token = create_jwt(
        {"cid": principal.company_id, "uid": principal.user_id, "h": context.host if context.trusted else ""},
        "brand_preview", timedelta(minutes=15),
    )
    return {"token": token, "expires_in": 900}


@router.get("/branding/resolve")
def explain(principal: Principal = Depends(require("branding:manage"))):
    """'Why is this value X?': the level each published field comes from (authorised users only)."""
    brand = brand_service.effective_for_company(principal.company_id)
    chain = brand.chain
    rows = []
    for name in brand_service.BRAND_FIELDS:
        rows.append({
            "field": name,
            "label": brand_service.FIELD_LABELS[name],
            "effective": _display(brand.values.get(name)),
            "source": brand.sources[name],
            "levels": {
                "PLATFORM": _display(brand_service._level_value(chain.platform_profile, name)
                                     or brand_service.PLATFORM_DEFAULTS.get(name)),
                "PARTNER": _display(brand_service._level_value(chain.partner_profile, name)),
                "TENANT": _display(brand_service._level_value(chain.tenant_profile, name)),
            },
            "locked_by_partner": bool(chain.partner_profile and name in chain.partner_profile["locked_fields"]),
        })
    return {"version": brand.version, "fields": rows, "partner": chain.partner, "plan": chain.plan}


def _display(value):
    if isinstance(value, dict) and "sha256" in value:
        return f"image {value['width']}×{value['height']}"
    return value


# ---------------------------------------------------------------------------
# Domains (domain:manage)
# ---------------------------------------------------------------------------
class DomainCreate(StrictModel):
    hostname: str = Field(..., min_length=3, max_length=300)


class DomainFlags(StrictModel):
    redirect_to_primary: Optional[bool] = None
    enforce_custom_domain: Optional[bool] = None


@router.get("/domains")
def list_domains(principal: Principal = Depends(require("domain:manage")), db: Session = Depends(get_db)):
    chain = _chain(principal.company_id)
    return {
        "domains": domain_service.list_domains(db, principal.company_id),
        "custom_entitled": "domains.custom" in chain.entitlements,
        "platform_base_domain": domain_service.settings.platform_base_domain or None,
        "cname_target": domain_service.settings.custom_domain_cname_target or None,
        "tls_mode": domain_service.tls_mode(),
    }


@router.post("/domains")
def add_domain(data: DomainCreate, principal: Principal = Depends(require("domain:manage")), db: Session = Depends(get_db)):
    domain_service.raise_if_custom_not_entitled(_chain(principal.company_id).entitlements)
    domain = domain_service.add_custom_domain(db, principal.company_id, data.hostname, principal.user_id)
    AuditLogService.log(db, principal, "Domains", "ADDED", f"Added custom domain {domain.hostname}", "domain", domain.id)
    db.commit()
    return domain_service.serialize(domain)


@router.get("/domains/{domain_id}")
def get_domain(domain_id: int, principal: Principal = Depends(require("domain:manage")), db: Session = Depends(get_db)):
    return domain_service.serialize(domain_service.get_domain(db, principal.company_id, domain_id, include_removed=True))


@router.post("/domains/{domain_id}/verify")
def verify_domain(domain_id: int, request: Request, principal: Principal = Depends(require("domain:manage")),
                  db: Session = Depends(get_db)):
    limiter.check(f"domain-verify:{principal.company_id}", 30, 300)
    domain = domain_service.get_domain(db, principal.company_id, domain_id)
    before = domain.status
    domain_service.check_domain(db, domain)
    if domain.status != before:
        AuditLogService.log(db, principal, "Domains", "STATUS", f"{domain.hostname}: {before} -> {domain.status}",
                            "domain", domain.id)
    db.commit()
    return domain_service.serialize(domain)


@router.post("/domains/{domain_id}/primary")
def make_primary(domain_id: int, principal: Principal = Depends(require("domain:manage")), db: Session = Depends(get_db)):
    domain = domain_service.make_primary(db, principal.company_id, domain_id)
    AuditLogService.log(db, principal, "Domains", "PRIMARY", f"{domain.hostname} is now the primary address", "domain", domain.id)
    db.commit()
    return domain_service.list_domains(db, principal.company_id)


@router.patch("/domains/{domain_id}")
def update_domain(domain_id: int, data: DomainFlags, principal: Principal = Depends(require("domain:manage")),
                  db: Session = Depends(get_db)):
    domain = domain_service.update_flags(db, principal.company_id, domain_id,
                                         redirect=data.redirect_to_primary, enforce=data.enforce_custom_domain)
    AuditLogService.log(db, principal, "Domains", "SETTINGS",
                        f"{domain.hostname}: redirect={domain.redirect_to_primary}, enforce={domain.enforce_custom_domain}",
                        "domain", domain.id)
    db.commit()
    return domain_service.serialize(domain)


@router.delete("/domains/{domain_id}")
def remove_domain(domain_id: int, principal: Principal = Depends(require("domain:manage")), db: Session = Depends(get_db)):
    domain = domain_service.remove_domain(db, principal.company_id, domain_id)
    AuditLogService.log(db, principal, "Domains", "REMOVED",
                        f"Removed {domain.hostname}; sessions on that host were signed out", "domain", domain.id)
    db.commit()
    return {"ok": True}


@router.get("/email-domains")
def email_domains(principal: Principal = Depends(require("domain:manage"))):
    """Custom sending domains need an email provider with domain-identity APIs (01 §7.2)."""
    chain = _chain(principal.company_id)
    return {
        "available": False,
        "entitled": "email.custom_domain" in chain.entitlements,
        "reason": "No email provider with sending-domain verification (SES, Postmark, SendGrid or Mailgun) is "
                  "configured. Emails are sent from the platform address with your company name as the sender "
                  "name and your support email as Reply-To.",
        "domains": [],
    }


# ---------------------------------------------------------------------------
# Email templates (email_template:manage)
# ---------------------------------------------------------------------------
class TemplateOverride(StrictModel):
    subject: Optional[str] = Field(None, max_length=200)
    intro: Optional[str] = Field(None, max_length=2000)
    signature: Optional[str] = Field(None, max_length=500)


def _template_entitled(company_id: int) -> bool:
    return "email.template_overrides" in _chain(company_id).entitlements


def _template_view(db: Session, company_id: int, key: str, locale: str) -> dict:
    template = email_templates.TEMPLATES[key]
    override = email_branding.load_override(db, company_id, key, locale)
    return {
        "key": key,
        "label": template.label,
        "locale": locale,
        "defaults": {"subject": template.subject, "intro": template.intro, "signature": template.signature},
        "override": override,
        "placeholders": list(template.placeholders),
        "entitled": _template_entitled(company_id),
    }


def _template(key: str):
    if key not in email_templates.TEMPLATES:
        raise not_found("Email template")
    return email_templates.TEMPLATES[key]


@router.get("/email-templates")
def list_templates(principal: Principal = Depends(require("email_template:manage")), db: Session = Depends(get_db)):
    overridden = {
        (r.template_key, r.locale)
        for r in db.query(EmailTemplateOverride).filter(EmailTemplateOverride.company_id == principal.company_id)
    }
    return [
        {"key": t.key, "label": t.label, "customized_locales": sorted(loc for k, loc in overridden if k == t.key)}
        for t in email_templates.TEMPLATES.values()
    ]


@router.get("/email-templates/{key}")
def get_template(key: str, locale: str = Query("en", max_length=35),
                 principal: Principal = Depends(require("email_template:manage")), db: Session = Depends(get_db)):
    _template(key)
    return _template_view(db, principal.company_id, key, locale)


@router.put("/email-templates/{key}")
def save_template(key: str, data: TemplateOverride, locale: str = Query("en", max_length=35),
                  principal: Principal = Depends(require("email_template:manage")), db: Session = Depends(get_db)):
    _template(key)
    if not _template_entitled(principal.company_id):
        raise AppError(403, "Editing email wording is not included in your plan.", "EMAIL_TEMPLATE_NOT_ENTITLED")
    if locale not in brand_service.SUPPORTED_LOCALES:
        raise bad_request("Choose a supported language.")
    values = {part: email_templates.validate_override(key, part, getattr(data, part)) for part in email_templates.EDITABLE_PARTS}
    row = db.query(EmailTemplateOverride).filter(
        EmailTemplateOverride.company_id == principal.company_id, EmailTemplateOverride.template_key == key,
        EmailTemplateOverride.locale == locale,
    ).first()
    if not any(values.values()):
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(EmailTemplateOverride(company_id=principal.company_id, template_key=key, locale=locale,
                                     updated_by_user_id=principal.user_id, version=1, **values))
    else:
        for part, value in values.items():
            setattr(row, part, value)
        row.version += 1
        row.updated_by_user_id = principal.user_id
    AuditLogService.log(db, principal, "Email templates", "UPDATED", f"Updated wording of {key} ({locale})")
    db.commit()
    return _template_view(db, principal.company_id, key, locale)


@router.post("/email-templates/{key}/preview")
def preview_template(key: str, data: TemplateOverride = Body(TemplateOverride()),
                     principal: Principal = Depends(require("email_template:manage"))):
    _template(key)
    overrides = {part: email_templates.validate_override(key, part, getattr(data, part)) for part in email_templates.EDITABLE_PARTS}
    brand = email_branding.email_brand(principal.company_id)
    context = email_templates.sample_context(key, brand["display_name"], brand["product_name"])
    rendered = email_templates.render(key, context, brand, overrides)
    return {"subject": rendered.subject, "html": rendered.html, "text": rendered.text,
            "from_name": brand["sender_name"], "reply_to": brand.get("support_email"), "fallback": rendered.used_fallback}


@router.post("/email-templates/{key}/test-send")
def test_send(key: str, request: Request, principal: Principal = Depends(require("email_template:manage"))):
    _template(key)
    limiter.check(f"email-test:{principal.user_id}", 5, 600)
    brand = email_branding.email_brand(principal.company_id)
    context = email_templates.sample_context(key, brand["display_name"], brand["product_name"])
    context["recipient"] = {"first_name": principal.full_name.split()[0], "name": principal.full_name, "email": principal.email}
    sent = EmailService.send_template(key, principal.email, context, principal.company_id)
    return {"sent": sent, "to": principal.email, "delivery": "smtp" if EmailService.configured() else "development outbox"}


# ---------------------------------------------------------------------------
# Legacy endpoints kept for compatibility (01 §8: "GET /settings/logo stays")
# ---------------------------------------------------------------------------
def legacy_logo_bytes(company_id: int) -> Optional[tuple]:
    brand = brand_service.effective_for_company(company_id)
    logo = brand.values.get("logo_light")
    if logo and brand.sources["logo_light"]["level"] == "TENANT":
        derivative = (logo.get("derivatives") or {}).get("display") or logo
        data = brand_asset_service.read_asset(derivative["sha256"], derivative["ext"])
        if data is not None:
            return data, brand_asset_service.MEDIA_TYPES[derivative["ext"]]
    return None

