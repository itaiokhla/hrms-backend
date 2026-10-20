"""SaaS platform administration.

Platform administrators are a separate principal type with separate cookies
and sessions. These endpoints manage tenant lifecycle and subscriptions and
expose only aggregate counts - there is deliberately no endpoint that
returns tenant HR records to a platform administrator.
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import EmailStr, Field
from sqlalchemy import func, or_

from app.config import settings
from app.core.errors import AppError, bad_request, not_found, unauthorized
from app.core.principal import PlatformPrincipal, get_platform_principal
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
from app.models.company import COMPANY_STATUSES, SUBSCRIPTION_STATUSES, Company, PlatformAdmin, PlatformSession
from app.models.employee import Employee
from app.models.user import User, UserSession
from app.schemas.auth import StrictModel
from app.services import auth_service
from app.services.audit_log_service import AuditLogService

router = APIRouter(prefix="/api/v1/platform", tags=["Platform"])


class PlatformLogin(StrictModel):
    email: EmailStr
    password: str = Field(..., max_length=128)
    mfa_code: Optional[str] = Field(None, max_length=8)


class CompanyUpdate(StrictModel):
    status: Optional[str] = None
    subscription_status: Optional[str] = None
    subscription_plan: Optional[str] = Field(None, max_length=40)
    trial_ends_at: Optional[datetime] = None


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
        {"sub": str(admin.id), "sid": session_id, "pt": "platform"},
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
    )
    return access, f"{session_id}.{secret}"


@router.post("/auth/login")
def platform_login(data: PlatformLogin, request: Request, response: Response):
    ip = client_ip(request)
    limiter.check(f"platform-login:{ip}", 10, 900)
    with system_session() as db:
        admin = db.query(PlatformAdmin).filter(func.lower(PlatformAdmin.email) == data.email.lower()).first()
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
        AuditLogService.log(db, None, "Platform", "LOGIN", f"Platform admin {admin.email} signed in",
                            company_id=None, actor_type="platform", actor_id=admin.id, actor_email=admin.email, ip_address=ip)
        db.commit()
        set_auth_cookies(response, access, refresh, platform=True)
        return {"mfa_required": False, "admin": {"id": admin.id, "email": admin.email, "full_name": admin.full_name}}


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
        access = create_jwt({"sub": str(admin.id), "sid": row.id, "pt": "platform"}, "access",
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
        return _company_row(db, company)


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
