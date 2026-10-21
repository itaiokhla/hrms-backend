"""Step-up re-authentication for high-risk payroll actions (3.7 §3.2, 3.9 §9; EC-BNK-06, EC-SEC-11).

A user proves presence again with a TOTP code (users with MFA, through auth_service.accept_totp, so a
code is never accepted twice) or, without MFA, with a six-digit code emailed to them for this session
(request_email_code). Success stamps user_sessions.step_up_at on the *current* session only, so a token
from another session gains nothing. `require_step_up(max_age_seconds=300)` guards finalize, payment
release, bank changes and unmasking: a stale or missing step-up is a 403 STEP_UP_REQUIRED (never a 401,
which the frontend would treat as an expired session).
"""

import html
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict
from app.core.principal import Principal, get_principal
from app.core.rate_limit import limiter
from app.core.security import hash_token, tokens_equal
from app.database.db import get_db
from app.models.security_keys import StepUpChallenge
from app.models.user import User, UserSession
from app.services import auth_service
from app.services.audit_log_service import AuditLogService

AUDIT_MODULE = "Authentication"
DEFAULT_MAX_AGE_SECONDS = 300
CODE_TTL = timedelta(minutes=10)
CODE_MAX_ATTEMPTS = 5
TOTP, EMAIL_OTP = "TOTP", "EMAIL_OTP"


def _session(db: Session, principal: Principal) -> UserSession:
    row = db.get(UserSession, principal.session_id) if principal.session_id else None
    if row is None or row.user_id != principal.user_id:
        raise AppError(403, "Sign in again to continue.", "STEP_UP_REQUIRED")
    return row


def _user(db: Session, principal: Principal) -> User:
    user = db.get(User, principal.user_id)
    if user is None:
        raise AppError(403, "Sign in again to continue.", "STEP_UP_REQUIRED")
    return user


def _expires(row: UserSession, max_age_seconds: int) -> Optional[datetime]:
    return None if row.step_up_at is None else row.step_up_at + timedelta(seconds=max_age_seconds)


def current(db: Session, principal: Principal, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> dict:
    row = _session(db, principal)
    user = _user(db, principal)
    expires = _expires(row, max_age_seconds)
    return {
        "active": expires is not None and expires > clock.utcnow(),
        "method": row.step_up_method,
        "verified_at": row.step_up_at,
        "expires_at": expires,
        "available_methods": [TOTP] if user.mfa_enabled else [EMAIL_OTP],
    }


def ensure_step_up(db: Session, principal: Principal, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> None:
    """Raise 403 STEP_UP_REQUIRED unless this session stepped up within `max_age_seconds`."""
    row = _session(db, principal)
    expires = _expires(row, max_age_seconds)
    if expires is None or expires <= clock.utcnow():
        raise AppError(403, "Confirm it's you to continue.", "STEP_UP_REQUIRED")


def require_step_up(max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS):
    """Route dependency: the principal, after checking a recent step-up of the current session."""

    def dependency(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> Principal:
        ensure_step_up(db, principal, max_age_seconds)
        return principal

    return dependency


def request_email_code(db: Session, principal: Principal, ip_address: Optional[str] = None) -> dict:
    """Create an emailed code for a user without MFA; returns {"code": ..., ...} for the route to send after commit."""
    user = _user(db, principal)
    row = _session(db, principal)
    if user.mfa_enabled:
        raise conflict("Use the code from your authenticator app.", "MFA_ENROLLED")
    limiter.check(f"stepup-email:{principal.company_id}:{principal.user_id}", 3, 900)
    now = clock.utcnow()
    db.query(StepUpChallenge).filter(
        StepUpChallenge.user_id == user.id, StepUpChallenge.consumed_at.is_(None)
    ).update({StepUpChallenge.consumed_at: now}, synchronize_session=False)
    code = f"{secrets.randbelow(10**6):06d}"
    db.add(StepUpChallenge(user_id=user.id, session_id=row.id, code_hash=hash_token(code), expires_at=now + CODE_TTL))
    AuditLogService.log(
        db, principal, AUDIT_MODULE, "STEP_UP_CODE_SENT", f"{user.email} requested a step-up code by email",
        "user", user.id, ip_address, details={"method": EMAIL_OTP},
    )
    return {"code": code, "email": user.email, "name": user.full_name, "expires_in_seconds": int(CODE_TTL.total_seconds())}


def send_email_code(company_id: int, email: str, name: str, code: str) -> bool:
    """Email the code with the tenant's branded layout (called after the challenge committed)."""
    from app.services import email_branding, email_templates
    from app.services.email_service import EmailService, OutgoingEmail

    brand = email_branding.email_brand(company_id)
    first = (name or email).split()[0]
    minutes = int(CODE_TTL.total_seconds() // 60)
    intro = f"Hello {first}, use this code to confirm a sensitive payroll action in {brand['product_name']}."
    detail = (f"Code: {code}\nIt expires in {minutes} minutes and works only in the browser session that asked "
              "for it. If you did not ask for it, change your password and tell your administrator.")
    signature = f"The {brand['display_name']} team"
    escape = html.escape
    message = OutgoingEmail(
        to=email,
        subject=f"Your {brand['product_name']} confirmation code",
        html=email_templates._layout_html(brand, escape(intro), escape(detail), escape(signature), None, None),
        text=email_templates._layout_text(brand, intro, detail, signature, None, None),
        from_name=brand["sender_name"],
        reply_to=brand.get("support_email"),
        template_key="auth.step_up_code",
        company_id=company_id,
    )
    return EmailService.deliver(message, f"code={code}")


def verify(db: Session, principal: Principal, code: str, ip_address: Optional[str] = None) -> dict:
    """Check a TOTP or emailed code and stamp the current session; the caller commits in both outcomes."""
    row = _session(db, principal)
    user = _user(db, principal)
    limiter.check(f"stepup:{principal.company_id}:{principal.user_id}", 5, 900)
    code = (code or "").strip()
    now = clock.utcnow()
    if user.mfa_enabled:
        method = TOTP
        ok = auth_service.accept_totp(db, user, code)
    else:
        method = EMAIL_OTP
        ok = _accept_email_code(db, user, row, code, now)
    if not ok:
        AuditLogService.log(
            db, principal, AUDIT_MODULE, "STEP_UP_FAILED", f"{user.email} entered a wrong step-up code",
            "user", user.id, ip_address, details={"method": method},
        )
        return {"ok": False, "method": method}
    row.step_up_at = now
    row.step_up_method = method
    AuditLogService.log(
        db, principal, AUDIT_MODULE, "STEP_UP_VERIFIED", f"{user.email} confirmed their identity ({method})",
        "user", user.id, ip_address, details={"method": method},
    )
    return {"ok": True, "method": method, "verified_at": now,
            "expires_at": now + timedelta(seconds=DEFAULT_MAX_AGE_SECONDS)}


def _accept_email_code(db: Session, user: User, session_row: UserSession, code: str, now: datetime) -> bool:
    challenge = (
        db.query(StepUpChallenge)
        .filter(StepUpChallenge.user_id == user.id, StepUpChallenge.session_id == session_row.id,
                StepUpChallenge.consumed_at.is_(None))
        .order_by(StepUpChallenge.id.desc())
        .with_for_update()
        .first()
    )
    if challenge is None or challenge.expires_at <= now or not code:
        return False
    challenge.attempts += 1
    if challenge.attempts > CODE_MAX_ATTEMPTS:
        challenge.consumed_at = now
        return False
    if not tokens_equal(hash_token(code), challenge.code_hash):
        if challenge.attempts >= CODE_MAX_ATTEMPTS:
            challenge.consumed_at = now
        return False
    challenge.consumed_at = now
    return True


def invalid_code() -> AppError:
    return bad_request("That code is not valid. Check it and try again.", "STEP_UP_INVALID")
