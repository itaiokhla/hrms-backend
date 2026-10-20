"""Company-user authentication: login, MFA, sessions, password reset, invitations."""

import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Union

import pyotp
from fastapi import status
from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import AppError, bad_request, conflict, unauthorized
from app.core.hosts import PLATFORM, HostContext, company_allowed_on_host
from app.core.principal import subscription_is_active
from app.core.security import (
    create_jwt,
    decode_jwt,
    hash_password,
    hash_token,
    new_token,
    password_problem,
    tokens_equal,
    ua_hash,
    utcnow,
    verify_password,
)
from app.database.db import SessionLocal, bind_tenant, system_session
from app.models.branding import AuthHandoffCode
from app.models.company import Company, PlatformAdmin
from app.models.user import PasswordResetOTP, User, UserInvitation, UserSession
from app.services.audit_log_service import AuditLogService
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)

GENERIC_LOGIN_ERROR = "Invalid email or password."
REFRESH_GRACE_SECONDS = 30
# Requests that did not come through the trusted proxy count as the platform host.
DIRECT = HostContext(host="", kind=PLATFORM, trusted=False)


@dataclass
class IssuedSession:
    access_token: str
    refresh_token: str
    session_id: str


@dataclass
class LoginResult:
    user_id: int
    company_id: int
    session: Optional[IssuedSession] = None
    mfa_token: Optional[str] = None
    handoff_url: Optional[str] = None


def _session_host(context: HostContext) -> Optional[str]:
    return context.host if context.trusted and context.host else None


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _access_token(user: User, session_id: str) -> str:
    return create_jwt(
        {"sub": str(user.id), "cid": user.company_id, "sid": session_id, "pt": "user"},
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def issue_session(
    db: Session, user: User, ip: Optional[str], user_agent: Optional[str], host: Optional[str] = None
) -> IssuedSession:
    session_id = str(uuid.uuid4())
    secret = new_token(32)
    db.add(
        UserSession(
            id=session_id,
            company_id=user.company_id,
            user_id=user.id,
            refresh_token_hash=hash_token(secret),
            expires_at=utcnow() + timedelta(days=settings.refresh_token_expire_days),
            last_seen_at=utcnow(),
            ip_address=(ip or "")[:64] or None,
            user_agent=(user_agent or "")[:255] or None,
            host=host,
        )
    )
    refresh = f"{user.company_id}.{session_id}.{secret}"
    return IssuedSession(_access_token(user, session_id), refresh, session_id)


def _register_failure(db: Session, user: User) -> None:
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= settings.max_failed_logins:
        user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        user.failed_login_count = 0
    db.commit()


def _check_login_state(user: User, company: Company) -> None:
    """Runs only after the password has been verified."""
    if user.status != "active":
        raise unauthorized("This account is not active. Contact your administrator.", "ACCOUNT_INACTIVE")
    if company.status != "active":
        raise AppError(status.HTTP_403_FORBIDDEN, "Your organization's workspace is suspended.", "COMPANY_SUSPENDED")


def login(
    email: str, password: str, ip: Optional[str], user_agent: Optional[str], context: HostContext = DIRECT
) -> LoginResult:
    email = normalize_email(email)
    with system_session() as db:
        user = db.query(User).filter(func.lower(User.email) == email).first()
        password_ok = verify_password(password, user.password_hash if user else None)
        if user is None:
            raise unauthorized(GENERIC_LOGIN_ERROR, "INVALID_CREDENTIALS")
        if not company_allowed_on_host(context, user.company_id):
            # EC-WL-14: a user of another tenant on this tenant's host gets the same answer as a
            # wrong password, and that tenant's failure counter and lockout are left untouched.
            raise unauthorized(GENERIC_LOGIN_ERROR, "INVALID_CREDENTIALS")
        if user.locked_until and user.locked_until > utcnow():
            if password_ok:
                raise AppError(
                    status.HTTP_423_LOCKED,
                    "Too many failed attempts. Try again in a few minutes.",
                    "ACCOUNT_LOCKED",
                )
            raise unauthorized(GENERIC_LOGIN_ERROR, "INVALID_CREDENTIALS")
        if not password_ok:
            _register_failure(db, user)
            AuditLogService.log(
                db, None, "Authentication", "LOGIN_FAILED", "Failed sign-in attempt",
                "user", user.id, ip, company_id=user.company_id, actor_id=user.id, actor_email=user.email,
            )
            db.commit()
            raise unauthorized(GENERIC_LOGIN_ERROR, "INVALID_CREDENTIALS")

        company = db.get(Company, user.company_id)
        _check_login_state(user, company)
        user.failed_login_count = 0
        user.locked_until = None

        if user.mfa_enabled:
            db.commit()
            token = create_jwt(
                # The second step must happen on the same host as the first.
                {"sub": str(user.id), "cid": user.company_id, "h": _session_host(context) or ""},
                "mfa",
                timedelta(minutes=settings.mfa_token_expire_minutes),
            )
            return LoginResult(user.id, user.company_id, mfa_token=token)

        return complete_login(db, user, ip, user_agent, context)


def complete_login(db: Session, user: User, ip, user_agent, context: HostContext = DIRECT) -> LoginResult:
    bind_tenant(db, user.company_id)
    target = _handoff_target(user.company_id, context)
    if target is not None:
        url = create_handoff(db, user, target, user_agent)
        AuditLogService.log(
            db, None, "Authentication", "LOGIN_HANDOFF", f"{user.email} signed in; handed off to {target}",
            "user", user.id, ip, company_id=user.company_id, actor_id=user.id, actor_email=user.email,
        )
        db.commit()
        return LoginResult(user.id, user.company_id, handoff_url=url)
    issued = issue_session(db, user, ip, user_agent, _session_host(context))
    user.last_login_at = utcnow()
    AuditLogService.log(
        db, None, "Authentication", "LOGIN", f"{user.email} signed in", "user", user.id, ip,
        company_id=user.company_id, actor_id=user.id, actor_email=user.email,
    )
    db.commit()
    return LoginResult(user.id, user.company_id, session=issued)


def login_mfa(mfa_token: str, code: str, ip, user_agent, context: HostContext = DIRECT) -> LoginResult:
    claims = decode_jwt(mfa_token, "mfa")
    if not claims or claims.get("h", "") != (_session_host(context) or ""):
        raise unauthorized("Verification expired. Sign in again.", "MFA_EXPIRED")
    with system_session() as db:
        user = db.get(User, int(claims["sub"]))
        if user is None or user.company_id != int(claims["cid"]) or not user.mfa_enabled:
            raise unauthorized("Verification expired. Sign in again.", "MFA_EXPIRED")
        if not company_allowed_on_host(context, user.company_id):
            raise unauthorized("Verification expired. Sign in again.", "MFA_EXPIRED")
        company = db.get(Company, user.company_id)
        _check_login_state(user, company)
        if not accept_totp(db, user, code):
            _register_failure(db, user)
            raise unauthorized("Invalid verification code.", "MFA_INVALID")
        return complete_login(db, user, ip, user_agent, context)


# ---------------------------------------------------------------------------
# Handoff to the tenant's enforced custom domain (01 §6.4 rule 2)
# ---------------------------------------------------------------------------
HANDOFF_INVALID = "This sign-in link has expired. Sign in again."


def _handoff_target(company_id: int, context: HostContext) -> Optional[str]:
    if not context.trusted or context.kind != PLATFORM:
        return None
    from app.services import domain_service

    target = domain_service.enforced_primary(company_id)
    return target if target and target != context.host else None


def create_handoff(db: Session, user: User, target_host: str, user_agent: Optional[str]) -> str:
    from app.services import domain_service

    code = new_token(32)
    db.add(
        AuthHandoffCode(
            code_hash=hash_token(code),
            user_id=user.id,
            company_id=user.company_id,
            target_host=target_host,
            ua_hash=ua_hash(user_agent),
            expires_at=utcnow() + timedelta(seconds=settings.handoff_code_ttl_seconds),
        )
    )
    return f"{domain_service.base_url_for_host(target_host)}/auth/handoff#code={code}"


def redeem_handoff(code: str, ip, user_agent, context: HostContext) -> LoginResult:
    """Exchange a handoff code on its target host for that host's own session (single use)."""
    if not context.trusted or context.kind != "TENANT" or not code:
        raise unauthorized(HANDOFF_INVALID, "HANDOFF_INVALID")
    with system_session() as db:
        now = utcnow()
        claimed = db.execute(
            update(AuthHandoffCode)
            .where(
                AuthHandoffCode.code_hash == hash_token(code.strip()),
                AuthHandoffCode.used_at.is_(None),
                AuthHandoffCode.expires_at > now,
                AuthHandoffCode.target_host == context.host,
                AuthHandoffCode.company_id == context.company_id,
                AuthHandoffCode.ua_hash == ua_hash(user_agent),
            )
            .values(used_at=now)
            .returning(AuthHandoffCode.user_id, AuthHandoffCode.company_id)
        ).first()
        if claimed is None:
            db.commit()
            raise unauthorized(HANDOFF_INVALID, "HANDOFF_INVALID")
        user = db.get(User, claimed.user_id)
        company = db.get(Company, claimed.company_id)
        if user is None or user.company_id != claimed.company_id:
            raise unauthorized(HANDOFF_INVALID, "HANDOFF_INVALID")
        _check_login_state(user, company)
        return complete_login(db, user, ip, user_agent, context)


def refresh(refresh_token: str, context: HostContext = DIRECT) -> IssuedSession:
    try:
        cid_text, session_id, secret = refresh_token.split(".", 2)
        company_id = int(cid_text)
    except (ValueError, AttributeError):
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    if not company_allowed_on_host(context, company_id):
        raise unauthorized("Session expired", "SESSION_EXPIRED")

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        session_row = db.get(UserSession, session_id)
        now = utcnow()
        if session_row is None or session_row.revoked_at is not None or session_row.expires_at <= now:
            raise unauthorized("Session expired", "SESSION_EXPIRED")
        if context.trusted and session_row.host and session_row.host != context.host:
            raise unauthorized("Session expired", "SESSION_EXPIRED")

        presented = hash_token(secret)
        if not tokens_equal(presented, session_row.refresh_token_hash):
            benign = (
                session_row.previous_refresh_token_hash
                and tokens_equal(presented, session_row.previous_refresh_token_hash)
                and session_row.rotated_at
                and (now - session_row.rotated_at).total_seconds() <= REFRESH_GRACE_SECONDS
            )
            if not benign:
                # A rotated-out token was replayed: assume theft, kill the session.
                session_row.revoked_at = now
                session_row.revoke_reason = "refresh_reuse"
                db.commit()
                raise unauthorized("Session expired", "SESSION_EXPIRED")

        user = db.get(User, session_row.user_id)
        company = db.get(Company, company_id)
        if user is None or user.status != "active" or company is None or company.status != "active":
            session_row.revoked_at = now
            session_row.revoke_reason = "account_inactive"
            db.commit()
            raise unauthorized("Session expired", "SESSION_EXPIRED")

        new_secret = new_token(32)
        session_row.previous_refresh_token_hash = session_row.refresh_token_hash
        session_row.refresh_token_hash = hash_token(new_secret)
        session_row.rotated_at = now
        session_row.last_seen_at = now
        db.commit()
        return IssuedSession(_access_token(user, session_id), f"{company_id}.{session_id}.{new_secret}", session_id)
    finally:
        db.close()


def revoke_session(db: Session, session_id: str, reason: str) -> None:
    row = db.get(UserSession, session_id)
    if row and row.revoked_at is None:
        row.revoked_at = utcnow()
        row.revoke_reason = reason


def revoke_user_sessions(db: Session, user_id: int, reason: str, except_session: Optional[str] = None) -> int:
    query = db.query(UserSession).filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if except_session:
        query = query.filter(UserSession.id != except_session)
    count = 0
    for row in query.all():
        row.revoked_at = utcnow()
        row.revoke_reason = reason
        count += 1
    return count


def change_password(db: Session, user: User, current_password: str, new_password: str, keep_session: str) -> None:
    if not verify_password(current_password, user.password_hash):
        raise bad_request("Current password is incorrect.", "INVALID_PASSWORD")
    problem = password_problem(new_password, user.email)
    if problem:
        raise bad_request(problem, "WEAK_PASSWORD")
    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    revoke_user_sessions(db, user.id, "password_changed", except_session=keep_session)


# ---------------------------------------------------------------------------
# MFA (TOTP)
# ---------------------------------------------------------------------------
def accept_totp(db: Session, account: Union[User, PlatformAdmin], code: str) -> bool:
    """Verify a TOTP code (one step of clock drift either way) and burn its time step.

    The conditional UPDATE makes every code single-use, even inside its validity
    window and across concurrent requests (RFC 6238 section 5.2), so an observed
    or phished code cannot be replayed. Used for company users and platform admins.
    """
    if not account.mfa_secret or not code:
        return False
    model = type(account)
    totp = pyotp.TOTP(account.mfa_secret)
    current = int(time.time()) // totp.interval
    for step in (current - 1, current, current + 1):
        if pyotp.utils.strings_equal(code.strip(), totp.generate_otp(step)):
            burned = db.execute(
                update(model)
                .where(model.id == account.id, or_(model.mfa_last_step.is_(None), model.mfa_last_step < step))
                .values(mfa_last_step=step)
            )
            return burned.rowcount == 1
    return False


def mfa_setup(user: User, company_name: str) -> dict:
    if user.mfa_enabled:
        raise conflict("Two-factor authentication is already enabled.")
    from app.services import brand_service

    brand = brand_service.effective_for_company(user.company_id).values
    secret = pyotp.random_base32()
    user.mfa_secret = secret
    user.mfa_last_step = None
    # Authenticator label: "{product name} ({company})" (01 §5.9). Existing enrolments keep their label.
    issuer = f"{brand['product_name']} ({brand['display_name'] or company_name})".replace(":", " ")
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=issuer)
    return {"secret": secret, "otpauth_uri": uri}


def mfa_enable(db: Session, user: User, code: str) -> None:
    if not user.mfa_secret:
        raise bad_request("Start two-factor setup first.")
    if not accept_totp(db, user, code):
        raise bad_request("Invalid verification code.", "MFA_INVALID")
    user.mfa_enabled = True


def mfa_disable(db: Session, user: User, password: str, code: str) -> None:
    if not user.mfa_enabled:
        return
    if not verify_password(password, user.password_hash):
        raise bad_request("Password is incorrect.", "INVALID_PASSWORD")
    if not accept_totp(db, user, code):
        raise bad_request("Invalid verification code.", "MFA_INVALID")
    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_last_step = None


# ---------------------------------------------------------------------------
# Password reset: OTP -> single-use reset token -> new password
# ---------------------------------------------------------------------------
RESET_REQUEST_MESSAGE = "If an account exists for that email, a verification code has been sent."
OTP_MAX_ATTEMPTS = 5


def request_password_reset(email: str, context: HostContext = DIRECT) -> None:
    email = normalize_email(email)
    with system_session() as db:
        user = db.query(User).filter(func.lower(User.email) == email).first()
        if user is None or user.status != "active" or not company_allowed_on_host(context, user.company_id):
            return
        bind_tenant(db, user.company_id)
        db.query(PasswordResetOTP).filter(
            PasswordResetOTP.user_id == user.id, PasswordResetOTP.consumed_at.is_(None)
        ).update({PasswordResetOTP.consumed_at: utcnow()}, synchronize_session=False)
        otp = f"{secrets.randbelow(10**6):06d}"
        db.add(
            PasswordResetOTP(
                user_id=user.id,
                otp_hash=hash_token(otp),
                expires_at=utcnow() + timedelta(minutes=settings.password_reset_token_expire_minutes),
            )
        )
        db.commit()
        EmailService.send_password_reset_code(user.email, otp, company_id=user.company_id, name=user.full_name)


def verify_reset_code(email: str, otp: str, context: HostContext = DIRECT) -> str:
    email = normalize_email(email)
    invalid = bad_request("Invalid or expired code.", "OTP_INVALID")
    with system_session() as db:
        user = db.query(User).filter(func.lower(User.email) == email).first()
        if user is None or not company_allowed_on_host(context, user.company_id):
            raise invalid
        bind_tenant(db, user.company_id)
        record = (
            db.query(PasswordResetOTP)
            .filter(PasswordResetOTP.user_id == user.id, PasswordResetOTP.consumed_at.is_(None))
            .order_by(PasswordResetOTP.id.desc())
            .first()
        )
        if record is None or record.expires_at <= utcnow():
            raise invalid
        record.attempts += 1
        if record.attempts > OTP_MAX_ATTEMPTS:
            record.consumed_at = utcnow()
            db.commit()
            raise invalid
        if not tokens_equal(hash_token(otp.strip()), record.otp_hash):
            db.commit()
            raise invalid
        record.verified_at = utcnow()
        db.commit()
        return create_jwt(
            {"sub": str(user.id), "cid": user.company_id, "otp": record.id},
            "pwreset",
            timedelta(minutes=settings.password_reset_token_expire_minutes),
        )


def reset_password(reset_token: str, new_password: str, context: HostContext = DIRECT) -> None:
    claims = decode_jwt(reset_token, "pwreset")
    invalid = bad_request("This reset link has expired. Request a new code.", "RESET_EXPIRED")
    if not claims or not company_allowed_on_host(context, int(claims["cid"])):
        raise invalid
    with system_session() as db:
        bind_tenant(db, int(claims["cid"]))
        record = db.get(PasswordResetOTP, int(claims["otp"]))
        user = db.get(User, int(claims["sub"]))
        if (
            record is None
            or user is None
            or record.user_id != user.id
            or record.verified_at is None
            or record.consumed_at is not None
            or record.expires_at <= utcnow()
        ):
            raise invalid
        problem = password_problem(new_password, user.email)
        if problem:
            raise bad_request(problem, "WEAK_PASSWORD")
        record.consumed_at = utcnow()
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utcnow()
        user.failed_login_count = 0
        user.locked_until = None
        revoke_user_sessions(db, user.id, "password_reset")
        AuditLogService.log(
            db, None, "Authentication", "PASSWORD_RESET", f"{user.email} reset their password", "user", user.id,
            company_id=user.company_id, actor_id=user.id, actor_email=user.email,
        )
        db.commit()


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
def create_invitation(db: Session, user: User, invited_by: Optional[int]) -> str:
    db.query(UserInvitation).filter(
        UserInvitation.user_id == user.id, UserInvitation.accepted_at.is_(None), UserInvitation.revoked_at.is_(None)
    ).update({UserInvitation.revoked_at: utcnow()}, synchronize_session=False)
    token = new_token(32)
    db.add(
        UserInvitation(
            company_id=user.company_id,
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(hours=settings.invitation_expire_hours),
            invited_by_user_id=invited_by,
        )
    )
    return token


def _load_invitation(db: Session, token: str, context: HostContext = DIRECT):
    invitation = db.query(UserInvitation).filter(UserInvitation.token_hash == hash_token(token)).first()
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= utcnow()
    ):
        raise bad_request("This invitation is invalid or has expired.", "INVITE_INVALID")
    if not company_allowed_on_host(context, invitation.company_id):
        raise bad_request("This invitation is invalid or has expired.", "INVITE_INVALID")
    bind_tenant(db, invitation.company_id)
    user = db.get(User, invitation.user_id)
    company = db.get(Company, invitation.company_id)
    if user is None or user.status != "invited" or company is None or company.status != "active":
        raise bad_request("This invitation is invalid or has expired.", "INVITE_INVALID")
    return invitation, user, company


def describe_invitation(token: str, context: HostContext = DIRECT) -> dict:
    with system_session() as db:
        _, user, company = _load_invitation(db, token, context)
        return {"email": user.email, "full_name": user.full_name, "company_name": company.name}


def accept_invitation(token: str, password: str, ip, user_agent, context: HostContext = DIRECT) -> LoginResult:
    with system_session() as db:
        invitation, user, company = _load_invitation(db, token, context)
        problem = password_problem(password, user.email)
        if problem:
            raise bad_request(problem, "WEAK_PASSWORD")
        invitation.accepted_at = utcnow()
        user.password_hash = hash_password(password)
        user.password_changed_at = utcnow()
        user.status = "active"
        AuditLogService.log(
            db, None, "Users", "INVITE_ACCEPTED", f"{user.email} accepted their invitation", "user", user.id,
            company_id=company.id, actor_id=user.id, actor_email=user.email,
        )
        db.flush()
        return complete_login(db, user, ip, user_agent, context)


def describe_subscription(company: Company) -> dict:
    return {
        "status": company.subscription_status,
        "plan": company.subscription_plan,
        "trial_ends_at": company.trial_ends_at.isoformat() if company.trial_ends_at else None,
        "active": subscription_is_active(company),
    }
