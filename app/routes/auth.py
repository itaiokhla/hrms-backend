from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import bad_request, conflict, not_found, unauthorized
from app.core.principal import Principal, get_principal_any_subscription
from app.core.rate_limit import client_ip, limiter
from app.core.security import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    hash_password,
    password_problem,
    set_auth_cookies,
    utcnow,
)
from app.database.db import SessionLocal, bind_tenant, get_db, system_session
from app.models.user import User, UserSession
from app.schemas.auth import (
    AcceptInvitationRequest,
    ChangePasswordRequest,
    CompanySignupRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaLoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    VerifyOtpRequest,
)
from app.services import auth_service, company_service
from app.services.audit_log_service import AuditLogService
from app.services.me_service import build_me

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def _me_for(user_id: int, company_id: int, session_id: str) -> dict:
    from app.core.principal import build_principal
    from app.models.company import Company

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        user = db.get(User, user_id)
        principal = build_principal(db, user, session_id, db.get(Company, company_id))
        return build_me(db, principal)
    finally:
        db.close()


def _session_response(response: Response, result: auth_service.LoginResult) -> dict:
    issued = result.session
    set_auth_cookies(response, issued.access_token, issued.refresh_token)
    return {"mfa_required": False, "me": _me_for(result.user_id, result.company_id, issued.session_id)}


@router.post("/signup")
def signup_company(data: CompanySignupRequest, request: Request, response: Response):
    ip = client_ip(request)
    limiter.check(f"signup:{ip}", 5, 3600)
    email = auth_service.normalize_email(data.email)
    problem = password_problem(data.password, email)
    if problem:
        raise bad_request(problem, "WEAK_PASSWORD")
    with system_session() as db:
        if db.query(User.id).filter(func.lower(User.email) == email).first():
            raise conflict("An account with this email already exists. Sign in instead.", "EMAIL_TAKEN")
        company, user = company_service.provision_company(
            db,
            company_name=data.company_name,
            admin_full_name=data.full_name,
            admin_email=email,
            admin_password_hash=hash_password(data.password),
            timezone=data.timezone,
        )
        AuditLogService.log(
            db, None, "Company", "CREATED", f"Workspace '{company.name}' created by {email}",
            "company", company.id, ip, company_id=company.id, actor_id=user.id, actor_email=email,
        )
        result = auth_service.complete_login(db, user, ip, request.headers.get("user-agent"))
    return _session_response(response, result)


@router.post("/login")
def login(data: LoginRequest, request: Request, response: Response):
    ip = client_ip(request)
    limiter.check(f"login:ip:{ip}", 30, 300)
    limiter.check(f"login:email:{data.email.lower()}", 10, 900)
    result = auth_service.login(data.email, data.password, ip, request.headers.get("user-agent"))
    if result.mfa_token:
        return {"mfa_required": True, "mfa_token": result.mfa_token}
    return _session_response(response, result)


@router.post("/login/mfa")
def login_mfa(data: MfaLoginRequest, request: Request, response: Response):
    ip = client_ip(request)
    limiter.check(f"mfa:ip:{ip}", 20, 300)
    result = auth_service.login_mfa(data.mfa_token, data.code, ip, request.headers.get("user-agent"))
    return _session_response(response, result)


@router.post("/refresh")
def refresh(request: Request, response: Response, data: RefreshRequest = RefreshRequest()):
    token = request.cookies.get(REFRESH_COOKIE) or data.refresh_token
    if not token:
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    issued = auth_service.refresh(token)
    set_auth_cookies(response, issued.access_token, issued.refresh_token)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            cid, sid, _ = token.split(".", 2)
            db = SessionLocal()
            try:
                bind_tenant(db, int(cid))
                auth_service.revoke_session(db, sid, "logout")
                db.commit()
            finally:
                db.close()
        except (ValueError, TypeError):
            pass
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
def me(principal: Principal = Depends(get_principal_any_subscription), db: Session = Depends(get_db)):
    return build_me(db, principal)


@router.get("/sessions")
def list_sessions(principal: Principal = Depends(get_principal_any_subscription), db: Session = Depends(get_db)):
    rows = (
        db.query(UserSession)
        .filter(
            UserSession.user_id == principal.user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utcnow(),
        )
        .order_by(UserSession.last_seen_at.desc())
        .all()
    )
    return [
        {
            "id": s.id,
            "current": s.id == principal.session_id,
            "created_at": s.created_at.isoformat(),
            "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
            "ip_address": s.ip_address,
            "user_agent": s.user_agent,
        }
        for s in rows
    ]


@router.delete("/sessions/{session_id}")
def revoke_my_session(
    session_id: str,
    principal: Principal = Depends(get_principal_any_subscription),
    db: Session = Depends(get_db),
):
    row = db.get(UserSession, session_id)
    if row is None or row.user_id != principal.user_id:
        raise not_found("Session")
    auth_service.revoke_session(db, session_id, "user_revoked")
    db.commit()
    return {"ok": True}


@router.put("/change-password")
def change_password(
    data: ChangePasswordRequest,
    principal: Principal = Depends(get_principal_any_subscription),
    db: Session = Depends(get_db),
):
    user = db.get(User, principal.user_id)
    auth_service.change_password(db, user, data.current_password, data.new_password, principal.session_id)
    AuditLogService.log(db, principal, "Authentication", "PASSWORD_CHANGED", f"{user.email} changed their password", "user", user.id)
    db.commit()
    return {"message": "Password changed. Other sessions were signed out."}


@router.post("/forgot-password")
def forgot_password(data: ForgotPasswordRequest, request: Request):
    ip = client_ip(request)
    limiter.check(f"forgot:ip:{ip}", 10, 3600)
    limiter.check(f"forgot:email:{data.email.lower()}", 3, 900)
    auth_service.request_password_reset(data.email)
    return {"message": auth_service.RESET_REQUEST_MESSAGE}


@router.post("/verify-otp")
def verify_otp(data: VerifyOtpRequest, request: Request):
    limiter.check(f"otp:ip:{client_ip(request)}", 20, 900)
    token = auth_service.verify_reset_code(data.email, data.otp)
    return {"reset_token": token}


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest, request: Request):
    limiter.check(f"reset:ip:{client_ip(request)}", 10, 900)
    auth_service.reset_password(data.reset_token, data.new_password)
    return {"message": "Password updated. You can now sign in."}


@router.get("/invitations/{token}")
def get_invitation(token: str, request: Request):
    limiter.check(f"invite:ip:{client_ip(request)}", 30, 900)
    return auth_service.describe_invitation(token)


@router.post("/invitations/accept")
def accept_invitation(data: AcceptInvitationRequest, request: Request, response: Response):
    ip = client_ip(request)
    limiter.check(f"invite-accept:ip:{ip}", 10, 900)
    result = auth_service.accept_invitation(data.token, data.password, ip, request.headers.get("user-agent"))
    return _session_response(response, result)


@router.post("/mfa/setup")
def mfa_setup(principal: Principal = Depends(get_principal_any_subscription), db: Session = Depends(get_db)):
    from app.models.company import Company

    user = db.get(User, principal.user_id)
    result = auth_service.mfa_setup(user, db.get(Company, principal.company_id).name)
    db.commit()
    return result


@router.post("/mfa/enable")
def mfa_enable(
    data: MfaCodeRequest,
    principal: Principal = Depends(get_principal_any_subscription),
    db: Session = Depends(get_db),
):
    user = db.get(User, principal.user_id)
    auth_service.mfa_enable(db, user, data.code)
    AuditLogService.log(db, principal, "Authentication", "MFA_ENABLED", f"{user.email} enabled two-factor authentication", "user", user.id)
    db.commit()
    return {"mfa_enabled": True}


@router.post("/mfa/disable")
def mfa_disable(
    data: MfaDisableRequest,
    principal: Principal = Depends(get_principal_any_subscription),
    db: Session = Depends(get_db),
):
    user = db.get(User, principal.user_id)
    auth_service.mfa_disable(db, user, data.password, data.code)
    AuditLogService.log(db, principal, "Authentication", "MFA_DISABLED", f"{user.email} disabled two-factor authentication", "user", user.id)
    db.commit()
    return {"mfa_enabled": False}
