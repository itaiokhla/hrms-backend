import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import Response
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

# Only the automated test suite (ENVIRONMENT=test) uses bcrypt's minimum cost, so its thousands of
# sign-ups and sign-ins stay fast; every other environment keeps passlib's default cost of 12.
pwd_context = CryptContext(
    schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=4 if settings.environment == "test" else 12
)

ACCESS_COOKIE = "hrms_access"
REFRESH_COOKIE = "hrms_refresh"
PLATFORM_ACCESS_COOKIE = "hrms_pa_access"
PLATFORM_REFRESH_COOKIE = "hrms_pa_refresh"
CSRF_COOKIE = "hrms_csrf"
CSRF_HEADER = "x-csrf-token"
# Not a credential: tells the server-rendered layout on the platform host which tenant's
# published brand to paint first, so signed-in users never see a flash of the platform
# brand. Signed and bound to the host, so it cannot be used to display another tenant.
BRAND_HINT_COOKIE = "hrms_bh"

USER_REFRESH_PATH = "/api/v1/auth"
PLATFORM_REFRESH_PATH = "/api/v1/platform/auth"

_dummy_hash: Optional[str] = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    """Constant-work verification, also when the account does not exist."""
    global _dummy_hash
    if not password_hash:
        if _dummy_hash is None:
            _dummy_hash = pwd_context.hash(secrets.token_urlsafe(16))
        pwd_context.verify(password, _dummy_hash)
        return False
    try:
        return pwd_context.verify(password, password_hash)
    except ValueError:
        return False


PASSWORD_RULES = "Use at least 8 characters with at least one letter and one number."


def password_problem(password: str, email: Optional[str] = None) -> Optional[str]:
    if len(password) < 8 or len(password) > 128:
        return PASSWORD_RULES
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return PASSWORD_RULES
    if email and password.lower() == email.lower():
        return "Password must not be your email address."
    return None


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Keyed hash for refresh tokens, invitations and OTPs stored at rest."""
    return hmac.new(settings.secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def create_jwt(claims: Dict[str, Any], typ: str, expires_in: timedelta) -> str:
    now = utcnow()
    payload = {
        **claims,
        "typ": typ,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_jwt(token: str, typ: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
            options={"require_exp": True, "require_iat": True},
        )
    except JWTError:
        return None
    if payload.get("typ") != typ:
        return None
    return payload


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------
def _cookie_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"secure": settings.cookie_secure, "samesite": settings.cookie_samesite}
    if settings.cookie_domain:
        kwargs["domain"] = settings.cookie_domain
    return kwargs


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    *,
    platform: bool = False,
) -> str:
    access_name = PLATFORM_ACCESS_COOKIE if platform else ACCESS_COOKIE
    refresh_name = PLATFORM_REFRESH_COOKIE if platform else REFRESH_COOKIE
    refresh_path = PLATFORM_REFRESH_PATH if platform else USER_REFRESH_PATH
    kw = _cookie_kwargs()
    response.set_cookie(
        access_name,
        access_token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        path="/",
        **kw,
    )
    response.set_cookie(
        refresh_name,
        refresh_token,
        max_age=settings.refresh_token_expire_days * 86400,
        httponly=True,
        path=refresh_path,
        **kw,
    )
    csrf = new_token(24)
    # Readable by JavaScript on purpose: double-submit CSRF protection.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=settings.refresh_token_expire_days * 86400,
        httponly=False,
        path="/",
        **kw,
    )
    return csrf


def clear_auth_cookies(response: Response, *, platform: bool = False) -> None:
    kw = {k: v for k, v in _cookie_kwargs().items() if k == "domain"}
    if platform:
        response.delete_cookie(PLATFORM_ACCESS_COOKIE, path="/", **kw)
        response.delete_cookie(PLATFORM_REFRESH_COOKIE, path=PLATFORM_REFRESH_PATH, **kw)
    else:
        response.delete_cookie(ACCESS_COOKIE, path="/", **kw)
        response.delete_cookie(REFRESH_COOKIE, path=USER_REFRESH_PATH, **kw)
    response.delete_cookie(CSRF_COOKIE, path="/", **kw)


def brand_hint_value(company_id: int, host: str) -> str:
    mac = hmac.new(settings.secret_key.encode(), f"brand-hint:{company_id}:{host}".encode(), hashlib.sha256).hexdigest()
    return f"{company_id}.{mac[:32]}"


def verify_brand_hint(value: Optional[str], host: str) -> Optional[int]:
    try:
        company_text, _ = (value or "").split(".", 1)
        company_id = int(company_text)
    except ValueError:
        return None
    return company_id if tokens_equal(brand_hint_value(company_id, host), value) else None


def set_brand_hint(response: Response, company_id: int, host: str) -> None:
    response.set_cookie(
        BRAND_HINT_COOKIE,
        brand_hint_value(company_id, host),
        max_age=settings.refresh_token_expire_days * 86400,
        httponly=True,
        path="/",
        **_cookie_kwargs(),
    )


def clear_brand_hint(response: Response) -> None:
    kw = {k: v for k, v in _cookie_kwargs().items() if k == "domain"}
    response.delete_cookie(BRAND_HINT_COOKIE, path="/", **kw)


def ua_hash(user_agent: Optional[str]) -> str:
    return hashlib.sha256((user_agent or "").encode()).hexdigest()
