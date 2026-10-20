from sqlalchemy.orm import Session

from app.core.permission_registry import REGISTRY_VERSION, Scope
from app.core.principal import Principal
from app.models.company import Company
from app.models.employee import Employee
from app.models.rbac import Role
from app.models.user import User
from app.services.auth_service import describe_subscription


def build_me(db: Session, principal: Principal) -> dict:
    user = db.get(User, principal.user_id)
    company = db.get(Company, principal.company_id)
    employee = db.get(Employee, principal.employee_id) if principal.employee_id else None
    roles = (
        db.query(Role).filter(Role.id.in_(list(principal.role_ids))).order_by(Role.name).all()
        if principal.role_ids
        else []
    )
    scope_order = [s.value for s in Scope]
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "mfa_enabled": user.mfa_enabled,
            "permission_version": user.permission_version,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        },
        "company": {
            "id": company.id,
            "name": company.name,
            "slug": company.slug,
            "timezone": company.timezone,
            "currency": company.currency,
            "subscription": describe_subscription(company),
        },
        "employee": (
            {
                "id": employee.id,
                "employee_code": employee.employee_code,
                "first_name": employee.first_name,
                "last_name": employee.last_name,
                "position": employee.position,
                "department": employee.department.name if employee.department else None,
                "has_photo": bool(employee.photo_path),
            }
            if employee
            else None
        ),
        "roles": [{"id": r.id, "name": r.name, "system_key": r.system_key} for r in roles],
        "permissions": {
            code: sorted(scopes, key=scope_order.index) for code, scopes in sorted(principal.grants.items())
        },
        "is_company_admin": principal.is_company_admin,
        "registry_version": REGISTRY_VERSION,
        "brand_version": _brand_version(principal.company_id),
    }


def _brand_version(company_id: int):
    from app.services import brand_service

    try:
        return brand_service.effective_for_company(company_id).version
    except Exception:  # branding must never block signing in
        return None
