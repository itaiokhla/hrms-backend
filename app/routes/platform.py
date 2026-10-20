"""SaaS platform administration.

Platform administrators are a separate principal type with separate cookies
and sessions. These endpoints manage tenant lifecycle and subscriptions and
expose only aggregate counts - there is deliberately no endpoint that
returns tenant HR records to a platform administrator.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from pydantic import EmailStr, Field
from sqlalchemy import func, or_

from app.config import settings
from app.core.errors import AppError, bad_request, not_found, unauthorized
from app.core.principal import PlatformPrincipal, get_platform_principal, require_platform_host
from app.core.rate_limit import client_ip, limiter
from app.core.security import (
    PLATFORM_REFRESH_COOKIE,
    clear_auth_cookies,
    create_jwt,
    hash_token,
    new_token,
    set_auth_cookies,
    tokens_equal,
    utcnow,
    verify_password,
)
from app.database.db import system_session
from app.models.audit_log import AuditLog
from app.core import hosts
from app.core.entitlements import ENTITLEMENTS, PLANS
from app.models.branding import CommercialBrandProfile, TenantDomain
from app.models.company import (
    BILLING_MODES,
    COMPANY_STATUSES,
    PARTNER_STATUSES,
    SUBSCRIPTION_STATUSES,
    Company,
    Partner,
    PlatformAdmin,
    PlatformSession,
)
from app.models.employee import Employee
from app.models.user import User, UserSession
from app.schemas.auth import StrictModel
from app.services import auth_service, brand_service, domain_service
from app.services.audit_log_service import AuditLogService

# The platform console exists only on the platform host; elsewhere every route is a 404.
router = APIRouter(prefix="/api/v1/platform", tags=["Platform"], dependencies=[Depends(require_platform_host)])


class PlatformLogin(StrictModel):
    email: EmailStr
    password: str = Field(..., max_length=128)
    mfa_code: Optional[str] = Field(None, max_length=8)


class CompanyUpdate(StrictModel):
    status: Optional[str] = None
    subscription_status: Optional[str] = None
    subscription_plan: Optional[str] = Field(None, max_length=40)
    trial_ends_at: Optional[datetime] = None
    # 0 detaches the tenant from its partner.
    partner_id: Optional[int] = None


def _kind(admin: PlatformAdmin) -> str:
    return "partner" if admin.partner_id is not None else "platform"


def _issue(db, admin: PlatformAdmin, request: Request):
    session_id = str(uuid.uuid4())
    secret = new_token(32)
    db.add(
        PlatformSession(
            id=session_id,
            admin_id=admin.id,
            refresh_token_hash=hash_token(secret),
            expires_at=utcnow() + timedelta(hours=12),
            last_seen_at=utcnow(),
            ip_address=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:255],
        )
    )
    access = create_jwt(
        {"sub": str(admin.id), "sid": session_id, "pt": _kind(admin)},
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
    )
    return access, f"{session_id}.{secret}"


@router.post("/auth/login")
def platform_login(data: PlatformLogin, request: Request, response: Response):
    return operator_login(data, request, response, "platform")


def operator_login(data: PlatformLogin, request: Request, response: Response, kind: str):
    """Sign in a platform operator or a partner administrator; each only through its own console."""
    ip = client_ip(request)
    limiter.check(f"platform-login:{ip}", 10, 900)
    with system_session() as db:
        admin = db.query(PlatformAdmin).filter(func.lower(PlatformAdmin.email) == data.email.lower()).first()
        if admin is not None and _kind(admin) != kind:
            # Same generic answer as an unknown account; the other console's account is not touched.
            verify_password(data.password, None)
            raise unauthorized("Invalid email or password.", "INVALID_CREDENTIALS")
        ok = verify_password(data.password, admin.password_hash if admin else None)
        if admin is None or not ok or not admin.is_active:
            if admin is not None and not ok:
                admin.failed_login_count += 1
                if admin.failed_login_count >= settings.max_failed_logins:
                    admin.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
                    admin.failed_login_count = 0
                db.commit()
            raise unauthorized("Invalid email or password.", "INVALID_CREDENTIALS")
        if admin.locked_until and admin.locked_until > utcnow():
            raise AppError(status.HTTP_423_LOCKED, "Too many failed attempts. Try again later.", "ACCOUNT_LOCKED")
        if admin.mfa_enabled:
            if not data.mfa_code:
                return {"mfa_required": True}
            if not auth_service.accept_totp(db, admin, data.mfa_code):
                raise unauthorized("Invalid verification code.", "MFA_INVALID")
        admin.failed_login_count = 0
        admin.last_login_at = utcnow()
        access, refresh = _issue(db, admin, request)
        label = "Partner admin" if kind == "partner" else "Platform admin"
        AuditLogService.log(db, None, "Platform", "LOGIN", f"{label} {admin.email} signed in",
                            company_id=None, actor_type=kind, actor_id=admin.id, actor_email=admin.email, ip_address=ip)
        db.commit()
        set_auth_cookies(response, access, refresh, platform=True)
        return {"mfa_required": False, "admin": {"id": admin.id, "email": admin.email, "full_name": admin.full_name,
                                                 "partner_id": admin.partner_id}}


@router.post("/auth/refresh")
def platform_refresh(request: Request, response: Response):
    token = request.cookies.get(PLATFORM_REFRESH_COOKIE)
    try:
        session_id, secret = (token or "").split(".", 1)
    except ValueError:
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    with system_session() as db:
        row = db.get(PlatformSession, session_id)
        if row is None or row.revoked_at or row.expires_at <= utcnow():
            raise unauthorized("Session expired", "SESSION_EXPIRED")
        if not tokens_equal(hash_token(secret), row.refresh_token_hash):
            row.revoked_at, row.revoke_reason = utcnow(), "refresh_reuse"
            db.commit()
            raise unauthorized("Session expired", "SESSION_EXPIRED")
        admin = db.get(PlatformAdmin, row.admin_id)
        if admin is None or not admin.is_active:
            raise unauthorized("Session expired", "SESSION_EXPIRED")
        new_secret = new_token(32)
        row.previous_refresh_token_hash, row.refresh_token_hash, row.rotated_at = row.refresh_token_hash, hash_token(new_secret), utcnow()
        db.commit()
        access = create_jwt({"sub": str(admin.id), "sid": row.id, "pt": _kind(admin)}, "access",
                            timedelta(minutes=settings.access_token_expire_minutes))
        set_auth_cookies(response, access, f"{row.id}.{new_secret}", platform=True)
    return {"ok": True}


@router.post("/auth/logout")
def platform_logout(request: Request, response: Response):
    token = request.cookies.get(PLATFORM_REFRESH_COOKIE) or ""
    session_id = token.split(".", 1)[0]
    if session_id:
        with system_session() as db:
            row = db.get(PlatformSession, session_id)
            if row and not row.revoked_at:
                row.revoked_at, row.revoke_reason = utcnow(), "logout"
                db.commit()
    clear_auth_cookies(response, platform=True)
    return {"ok": True}


@router.get("/me")
def platform_me(admin: PlatformPrincipal = Depends(get_platform_principal)):
    return {"id": admin.admin_id, "email": admin.email, "full_name": admin.full_name}


def _company_row(db, company: Company) -> dict:
    users = db.query(func.count(User.id)).filter(User.company_id == company.id).scalar()
    active_users = db.query(func.count(User.id)).filter(User.company_id == company.id, User.status == "active").scalar()
    employees = db.query(func.count(Employee.id)).filter(Employee.company_id == company.id, Employee.is_active.is_(True)).scalar()
    return {
        "id": company.id,
        "name": company.name,
        "slug": company.slug,
        "status": company.status,
        "subscription_status": company.subscription_status,
        "subscription_plan": company.subscription_plan,
        "trial_ends_at": company.trial_ends_at.isoformat() if company.trial_ends_at else None,
        "timezone": company.timezone,
        "partner_id": company.partner_id,
        "brand_disabled": company.brand_disabled_at is not None,
        "brand_disabled_reason": company.brand_disabled_reason,
        "hosts": [d.hostname for d in db.query(TenantDomain).filter(
            TenantDomain.company_id == company.id, TenantDomain.status == "ACTIVE").order_by(TenantDomain.is_primary.desc())],
        "created_at": company.created_at.isoformat(),
        "user_count": users,
        "active_user_count": active_users,
        "employee_count": employees,
    }


@router.get("/companies")
def list_companies(
    q: Optional[str] = Query(None, max_length=100),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: PlatformPrincipal = Depends(get_platform_principal),
):
    with system_session() as db:
        query = db.query(Company)
        if q:
            term = f"%{q}%"
            query = query.filter(or_(Company.name.ilike(term), Company.slug.ilike(term)))
        total = query.count()
        companies = query.order_by(Company.created_at.desc()).offset(skip).limit(limit).all()
        return {"total": total, "items": [_company_row(db, c) for c in companies]}


@router.patch("/companies/{company_id}")
def update_company(company_id: int, data: CompanyUpdate, request: Request,
                   admin: PlatformPrincipal = Depends(get_platform_principal)):
    with system_session() as db:
        company = db.get(Company, company_id)
        if company is None:
            raise not_found("Company")
        changes = data.model_dump(exclude_unset=True)
        if "status" in changes and changes["status"] not in COMPANY_STATUSES:
            raise bad_request("Invalid status")
        if "subscription_status" in changes and changes["subscription_status"] not in SUBSCRIPTION_STATUSES:
            raise bad_request("Invalid subscription status")
        if "subscription_plan" in changes and changes["subscription_plan"] not in PLANS:
            raise bad_request(f"Plan must be one of {', '.join(PLANS)}")
        if "partner_id" in changes:
            partner_id = changes.pop("partner_id") or None
            if partner_id is not None and db.get(Partner, partner_id) is None:
                raise not_found("Partner")
            company.partner_id = partner_id
        for key, value in changes.items():
            setattr(company, key, value)
        if changes.get("status") in ("suspended", "closed"):
            # Suspension takes effect immediately for every signed-in user.
            db.query(UserSession).filter(
                UserSession.company_id == company.id, UserSession.revoked_at.is_(None)
            ).update({UserSession.revoked_at: utcnow(), UserSession.revoke_reason: "company_suspended"},
                     synchronize_session=False)
        AuditLogService.log(
            db, None, "Platform", "COMPANY_UPDATED", f"{company.name}: {changes}", "company", company.id,
            client_ip(request), company_id=None, actor_type="platform", actor_id=admin.admin_id, actor_email=admin.email,
        )
        db.commit()
        brand_service.invalidate_cache(company.id)
        hosts.invalidate_host_cache()
        return _company_row(db, company)


class KillSwitch(StrictModel):
    disabled: bool
    reason: Optional[str] = Field(None, max_length=255)
    include_domains: bool = True


@router.post("/companies/{company_id}/brand-kill-switch")
def brand_kill_switch(company_id: int, data: KillSwitch, request: Request,
                      admin: PlatformPrincipal = Depends(get_platform_principal)):
    """Abuse response (01 §15.12): the tenant falls back to the platform brand; custom domains are disabled."""
    with system_session() as db:
        company = db.get(Company, company_id)
        if company is None:
            raise not_found("Company")
        if data.disabled and not (data.reason or "").strip():
            raise bad_request("Give a reason for disabling this brand.")
        company.brand_disabled_at = utcnow() if data.disabled else None
        company.brand_disabled_reason = (data.reason or "").strip()[:255] or None if data.disabled else None
        disabled_domains = 0
        if data.disabled and data.include_domains:
            disabled_domains = domain_service.platform_disable_tenant_domains(db, company.id, company.brand_disabled_reason)
        AuditLogService.log(
            db, None, "Platform", "BRAND_KILL_SWITCH",
            f"{company.name}: brand {'disabled' if data.disabled else 'restored'}"
            f"{f' ({company.brand_disabled_reason})' if data.disabled else ''}; {disabled_domains} custom domain(s) disabled",
            "company", company.id, client_ip(request), company_id=None, actor_type="platform", actor_id=admin.admin_id,
            actor_email=admin.email,
        )
        db.commit()
        brand_service.invalidate_cache(company.id)
        hosts.invalidate_host_cache()
        return _company_row(db, company)


# ---------------------------------------------------------------------------
# Partners (resellers / bureaus). Owning a tenant relationship grants no HR access.
# ---------------------------------------------------------------------------
class PartnerWrite(StrictModel):
    name: Optional[str] = Field(None, min_length=2, max_length=150)
    status: Optional[str] = None
    billing_mode: Optional[str] = None
    tenant_entitlements: Optional[Dict[str, bool]] = None


def _partner_row(db, partner: Partner) -> dict:
    tenants = db.query(Company).filter(Company.partner_id == partner.id).order_by(Company.name).all()
    owner = brand_service.partner_owner(partner.id)
    published = owner.profiles(db).filter(CommercialBrandProfile.status == "PUBLISHED").first()
    draft = owner.profiles(db).filter(CommercialBrandProfile.status == "DRAFT").first()
    return {
        "id": partner.id,
        "name": partner.name,
        "slug": partner.slug,
        "status": partner.status,
        "billing_mode": partner.billing_mode,
        "tenant_entitlements": partner.tenant_entitlements or {},
        # Aggregate counts only: partners never see tenant HR records without a grant.
        "tenants": [
            {"id": c.id, "name": c.name, "status": c.status, "plan": c.subscription_plan,
             "active_employees": db.query(func.count(Employee.id)).filter(
                 Employee.company_id == c.id, Employee.is_active.is_(True)).scalar()}
            for c in tenants
        ],
        "brand": brand_service.serialize_admin_profile(db, owner, published),
        "brand_draft": brand_service.serialize_admin_profile(db, owner, draft),
        "brand_history": brand_service.history(db, owner, 20),
    }


def _validate_partner(data: dict) -> dict:
    if "status" in data and data["status"] not in PARTNER_STATUSES:
        raise bad_request("Invalid partner status")
    if "billing_mode" in data and data["billing_mode"] not in BILLING_MODES:
        raise bad_request("Invalid billing mode")
    if "tenant_entitlements" in data:
        unknown = set(data["tenant_entitlements"]) - set(ENTITLEMENTS)
        if unknown:
            raise bad_request(f"Unknown entitlements: {', '.join(sorted(unknown))}")
    return data


@router.get("/partners")
def list_partners(admin: PlatformPrincipal = Depends(get_platform_principal)):
    with system_session() as db:
        return {
            "partners": [_partner_row(db, p) for p in db.query(Partner).order_by(Partner.name).all()],
            "entitlements": ENTITLEMENTS,
            "plans": list(PLANS),
            "brand_fields": {f: brand_service.FIELD_LABELS[f] for f in brand_service.BRAND_FIELDS},
        }


@router.post("/partners")
def create_partner(data: PartnerWrite, request: Request, admin: PlatformPrincipal = Depends(get_platform_principal)):
    values = _validate_partner(data.model_dump(exclude_unset=True))
    if not values.get("name"):
        raise bad_request("Partner name is required")
    with system_session() as db:
        from app.services.company_service import slugify

        slug = slugify(values["name"])
        if db.query(Partner.id).filter(Partner.slug == slug).first():
            raise bad_request("A partner with this name already exists")
        partner = Partner(name=values["name"].strip(), slug=slug, status=values.get("status", "active"),
                          billing_mode=values.get("billing_mode", "PLATFORM_BILLS_TENANT"),
                          tenant_entitlements=values.get("tenant_entitlements") or {})
        db.add(partner)
        db.flush()
        AuditLogService.log(db, None, "Platform", "PARTNER_CREATED", f"Partner {partner.name} created", "partner",
                            partner.id, client_ip(request), company_id=None, actor_type="platform",
                            actor_id=admin.admin_id, actor_email=admin.email)
        db.commit()
        return _partner_row(db, partner)


@router.patch("/partners/{partner_id}")
def update_partner(partner_id: int, data: PartnerWrite, request: Request,
                   admin: PlatformPrincipal = Depends(get_platform_principal)):
    values = _validate_partner(data.model_dump(exclude_unset=True))
    with system_session() as db:
        partner = db.get(Partner, partner_id)
        if partner is None:
            raise not_found("Partner")
        for key, value in values.items():
            setattr(partner, key, value.strip() if isinstance(value, str) and key == "name" else value)
        AuditLogService.log(db, None, "Platform", "PARTNER_UPDATED", f"Partner {partner.name}: {sorted(values)}",
                            "partner", partner.id, client_ip(request), company_id=None, actor_type="platform",
                            actor_id=admin.admin_id, actor_email=admin.email)
        db.commit()
        brand_service.invalidate_cache()
        return _partner_row(db, partner)


class PartnerAdminCreate(StrictModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=150)
    password: str = Field(..., min_length=8, max_length=128)


@router.post("/partners/{partner_id}/admins")
def create_partner_admin(partner_id: int, data: PartnerAdminCreate, request: Request,
                         admin: PlatformPrincipal = Depends(get_platform_principal)):
    """A partner administrator: partner console only, scoped to this partner."""
    from app.core.security import hash_password, password_problem

    problem = password_problem(data.password, data.email)
    if problem:
        raise bad_request(problem)
    with system_session() as db:
        partner = db.get(Partner, partner_id)
        if partner is None:
            raise not_found("Partner")
        email = data.email.strip().lower()
        if db.query(PlatformAdmin.id).filter(func.lower(PlatformAdmin.email) == email).first():
            raise bad_request("An operator account with this email already exists.")
        account = PlatformAdmin(email=email, full_name=data.full_name.strip(), password_hash=hash_password(data.password),
                                partner_id=partner.id)
        db.add(account)
        db.flush()
        AuditLogService.log(db, None, "Platform", "PARTNER_ADMIN_CREATED", f"{email} administers partner {partner.name}",
                            "partner", partner.id, client_ip(request), company_id=None, actor_type="platform",
                            actor_id=admin.admin_id, actor_email=admin.email)
        db.commit()
        return {"id": account.id, "email": account.email, "full_name": account.full_name, "partner_id": partner.id}


class CommercialBrandWrite(StrictModel):
    changes: Dict[str, Any] = Field(default_factory=dict)
    locked_fields: Optional[List[str]] = None
    powered_by_label: Optional[str] = Field(None, max_length=80)
    row_version: Optional[int] = None


def _commercial_owner(owner_kind: str, owner_id: Optional[int], db):
    if owner_kind == "platform":
        return brand_service.PLATFORM_OWNER
    if owner_kind == "partners" and owner_id and db.get(Partner, owner_id) is not None:
        return brand_service.partner_owner(owner_id)
    raise not_found("Brand owner")


@router.put("/brands/{owner_kind}/{owner_id}")
def save_commercial_brand(owner_kind: str, owner_id: int, data: CommercialBrandWrite, request: Request,
                          admin: PlatformPrincipal = Depends(get_platform_principal)):
    """Edit the platform brand (owner_kind=platform, owner_id=0) or a partner brand and its locks."""
    with system_session() as db:
        owner = _commercial_owner(owner_kind, owner_id, db)
        draft = brand_service.save_draft(db, owner, data.changes, actor=admin.email,
                                         expected_row_version=data.row_version, locked_fields=data.locked_fields,
                                         powered_by_label=data.powered_by_label)
        db.commit()
        return {"draft": brand_service.serialize_admin_profile(db, owner, draft),
                "validation": brand_service.validate_draft(db, owner, None)}


@router.post("/brands/{owner_kind}/{owner_id}/assets")
async def upload_commercial_asset(owner_kind: str, owner_id: int, kind: str = Form(...), file: UploadFile = File(...),
                                  admin: PlatformPrincipal = Depends(get_platform_principal)):
    from app.services import brand_asset_service

    rule = brand_asset_service.KIND_RULES.get(kind)
    if rule is None:
        raise bad_request("Unknown image kind.")
    data = await file.read(rule.max_bytes + 1)
    with system_session() as db:
        owner = _commercial_owner(owner_kind, owner_id, db)
        asset = brand_service.add_asset(db, owner, kind, data, file.filename, admin.email)
        db.commit()
        payload = brand_service._asset_dict(asset)
        payload["url"] = brand_service.asset_url(payload, "display") or brand_service.asset_url(payload)
        return payload


@router.post("/brands/{owner_kind}/{owner_id}/publish")
def publish_commercial_brand(owner_kind: str, owner_id: int, request: Request,
                             admin: PlatformPrincipal = Depends(get_platform_principal)):
    with system_session() as db:
        owner = _commercial_owner(owner_kind, owner_id, db)
        profile, _ = brand_service.publish(db, owner, actor=admin.email)
        AuditLogService.log(db, None, "Platform", "BRAND_PUBLISHED",
                            f"Published {owner.kind.lower()} brand v{profile.version} (locks: {profile.locked_fields})",
                            "brand_profile", profile.id, client_ip(request), company_id=None, actor_type="platform",
                            actor_id=admin.admin_id, actor_email=admin.email)
        db.commit()
        brand_service.invalidate_cache()
        return {"version": profile.version, "brand": brand_service.serialize_admin_profile(db, owner, profile)}


@router.get("/audit-logs")
def platform_audit(limit: int = Query(100, ge=1, le=500), admin: PlatformPrincipal = Depends(get_platform_principal)):
    with system_session() as db:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.company_id.is_(None))
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {"id": r.id, "created_at": r.created_at.isoformat(), "actor_email": r.actor_email, "action": r.action,
             "description": r.description, "ip_address": r.ip_address}
            for r in rows
        ]
