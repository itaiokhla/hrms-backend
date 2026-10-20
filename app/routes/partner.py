"""Partner console (02-multi-level-company §7): a reseller's administrators.

A partner administrator sees only its own partner: the tenants it sells to, as
aggregate counts, and its own brand (with the fields it locks for its tenants).
Owning the commercial relationship grants no access to tenant HR data: there is
no endpoint here that returns employees, payroll, attendance or bank records, and
tenant routes do not accept partner sessions. Bureau grants are not implemented.
"""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func

from app.core.errors import not_found
from app.core.principal import PlatformPrincipal, get_partner_principal, require_platform_host
from app.core.rate_limit import client_ip
from app.database.db import system_session
from app.models.branding import CommercialBrandProfile
from app.models.company import Company, Partner
from app.models.employee import Employee
from app.models.user import User
from app.routes import platform as platform_routes
from app.routes.platform import CommercialBrandWrite, PlatformLogin
from app.services import brand_service
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/api/v1/partner", tags=["Partner"], dependencies=[Depends(require_platform_host)])


@router.post("/auth/login")
def partner_login(data: PlatformLogin, request: Request, response: Response):
    return platform_routes.operator_login(data, request, response, "partner")


@router.post("/auth/refresh")
def partner_refresh(request: Request, response: Response):
    return platform_routes.platform_refresh(request, response)


@router.post("/auth/logout")
def partner_logout(request: Request, response: Response):
    return platform_routes.platform_logout(request, response)


def _partner(db, admin: PlatformPrincipal) -> Partner:
    partner = db.get(Partner, admin.partner_id)
    if partner is None:
        raise not_found("Partner")
    return partner


@router.get("/me")
def partner_me(admin: PlatformPrincipal = Depends(get_partner_principal)):
    with system_session() as db:
        partner = _partner(db, admin)
        return {"id": admin.admin_id, "email": admin.email, "full_name": admin.full_name,
                "partner": {"id": partner.id, "name": partner.name, "slug": partner.slug}}


@router.get("/companies")
def partner_companies(admin: PlatformPrincipal = Depends(get_partner_principal)):
    """This partner's tenants with aggregate counts only."""
    with system_session() as db:
        companies = db.query(Company).filter(Company.partner_id == admin.partner_id).order_by(Company.name).all()
        return {"items": [
            {"id": c.id, "name": c.name, "status": c.status, "subscription_status": c.subscription_status,
             "plan": c.subscription_plan, "created_at": c.created_at.isoformat(),
             "active_users": db.query(func.count(User.id)).filter(User.company_id == c.id, User.status == "active").scalar(),
             "active_employees": db.query(func.count(Employee.id)).filter(
                 Employee.company_id == c.id, Employee.is_active.is_(True)).scalar()}
            for c in companies
        ]}


@router.get("/brand")
def partner_brand(admin: PlatformPrincipal = Depends(get_partner_principal)):
    with system_session() as db:
        partner = _partner(db, admin)
        owner = brand_service.partner_owner(partner.id)
        published = owner.profiles(db).filter(CommercialBrandProfile.status == "PUBLISHED").first()
        draft = owner.profiles(db).filter(CommercialBrandProfile.status == "DRAFT").first()
        return {
            "brand": brand_service.serialize_admin_profile(db, owner, published),
            "brand_draft": brand_service.serialize_admin_profile(db, owner, draft),
            "brand_history": brand_service.history(db, owner, 20),
            "brand_fields": {f: brand_service.FIELD_LABELS[f] for f in brand_service.BRAND_FIELDS},
        }


@router.put("/brand/draft")
def partner_brand_draft(data: CommercialBrandWrite, admin: PlatformPrincipal = Depends(get_partner_principal)):
    with system_session() as db:
        owner = brand_service.partner_owner(_partner(db, admin).id)
        draft = brand_service.save_draft(db, owner, data.changes, actor=admin.email,
                                         expected_row_version=data.row_version, locked_fields=data.locked_fields,
                                         powered_by_label=data.powered_by_label)
        db.commit()
        return {"draft": brand_service.serialize_admin_profile(db, owner, draft),
                "validation": brand_service.validate_draft(db, owner, None)}


@router.post("/brand/publish")
def partner_brand_publish(request: Request, admin: PlatformPrincipal = Depends(get_partner_principal)):
    with system_session() as db:
        partner = _partner(db, admin)
        owner = brand_service.partner_owner(partner.id)
        profile, _ = brand_service.publish(db, owner, actor=admin.email)
        AuditLogService.log(db, None, "Partner", "BRAND_PUBLISHED",
                            f"{partner.name} published partner brand v{profile.version} (locks: {profile.locked_fields})",
                            "brand_profile", profile.id, client_ip(request), company_id=None, actor_type="partner",
                            actor_id=admin.admin_id, actor_email=admin.email)
        db.commit()
        brand_service.invalidate_cache()
        return {"version": profile.version, "brand": brand_service.serialize_admin_profile(db, owner, profile)}
