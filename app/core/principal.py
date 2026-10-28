"""Authenticated principal resolution.

Company identity is derived exclusively from the server-issued session:
the signed access token names the session, the session row is checked for
revocation/expiry, and the company comes from the stored user record. No
request body, query parameter or header can choose the tenant.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, FrozenSet, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import Depends, Request, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.errors import AppError, forbidden, not_found, unauthorized
from app.core.hosts import company_allowed_on_host, normalize_host, request_host
from app.core.permission_registry import TARGETED_SCOPES, Scope, full_access_grants, scope_covers
from app.core.security import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CSRF_HEADER,
    PLATFORM_ACCESS_COOKIE,
    decode_jwt,
    tokens_equal,
    utcnow,
)
from app.database.db import bind_tenant, get_db
from app.models.company import Company, PlatformAdmin, PlatformSession
from app.models.employee import Employee
from app.models.rbac import Role, RolePermission, UserRoleAssignment
from app.models.user import User, UserSession

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ACTIVE_SUBSCRIPTIONS = {"trialing", "active"}


@dataclass(frozen=True)
class DelegationContext:
    """Set on a principal that is a partner operator acting under a tenant-granted delegation.

    The principal is read-only and limited to the delegated permission codes; user_id is 0 (the operator
    is not a tenant user) and every request is audited with these ids (app.services.partner_delegation_service).
    """

    delegation_id: int
    partner_id: int
    operator_id: int
    operator_email: str
    platform_session_id: str


@dataclass
class Principal:
    user_id: int
    company_id: int
    session_id: Optional[str]
    email: str
    full_name: str
    employee_id: Optional[int]
    grants: Dict[str, FrozenSet[str]]
    role_ids: FrozenSet[int]
    role_keys: FrozenSet[str]
    is_company_admin: bool
    permission_version: int
    subscription_active: bool = True
    extra: dict = field(default_factory=dict)
    # (permission, LEGAL_ENTITY|BRANCH|PAY_GROUP) -> ids covered by the user's role assignments.
    targets: Dict[Tuple[str, str], FrozenSet[int]] = field(default_factory=dict)
    # Only for a partner operator under a delegation (never a tenant user).
    delegation: Optional[DelegationContext] = None

    def scopes(self, code: str) -> FrozenSet[str]:
        return self.grants.get(code, frozenset())

    def scope_targets(self, code: str, scope: str) -> FrozenSet[int]:
        return self.targets.get((code, scope), frozenset())

    def has(self, code: str, scope: Optional[str] = None) -> bool:
        held = self.scopes(code)
        if not held:
            return False
        return True if scope is None else scope_covers(held, scope)

    def has_company_scope(self, code: str) -> bool:
        return Scope.COMPANY.value in self.scopes(code)


@dataclass
class PlatformPrincipal:
    admin_id: int
    email: str
    full_name: str
    session_id: str
    # Set for partner administrators (partner console only).
    partner_id: Optional[int] = None


def _extract_token(request: Request, cookie_name: str):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), False
    token = request.cookies.get(cookie_name)
    return (token, True) if token else (None, False)


def _check_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header or not tokens_equal(cookie, header):
        raise AppError(status.HTTP_403_FORBIDDEN, "CSRF validation failed", "CSRF_FAILED")
    # Behind the trusted proxy, a browser Origin must be exactly the host the request arrived on.
    context = request_host(request)
    origin = request.headers.get("origin")
    if context.trusted and origin and origin != "null":
        if normalize_host(urlsplit(origin).netloc) != context.host:
            raise AppError(status.HTTP_403_FORBIDDEN, "CSRF validation failed", "CSRF_FAILED")


def subscription_is_active(company: Company) -> bool:
    if company.subscription_status not in ACTIVE_SUBSCRIPTIONS:
        return False
    if company.subscription_status == "trialing" and company.trial_ends_at is not None:
        return company.trial_ends_at > utcnow()
    return True


def load_grants(db: Session, user_id: int):
    """Effective permissions: the additive union of all active role grants.

    A full-access role adds registry.full_access_grants(): every permission at its non-targeted
    scopes except payroll-confidential ones, which need an explicit payroll role (3.9 §5.1).
    """
    now = utcnow()
    rows = (
        db.query(
            Role.id,
            Role.system_key,
            Role.is_full_access,
            Role.is_protected,
            RolePermission.permission_code,
            RolePermission.scope,
        )
        .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .filter(
            UserRoleAssignment.user_id == user_id,
            UserRoleAssignment.effective_from <= now,
            or_(UserRoleAssignment.effective_until.is_(None), UserRoleAssignment.effective_until > now),
        )
        .all()
    )
    grants: Dict[str, set] = {}
    role_ids, role_keys = set(), set()
    full_access = False
    is_admin = False
    for role_id, system_key, is_full, is_protected, code, scope in rows:
        role_ids.add(role_id)
        if system_key:
            role_keys.add(system_key)
        if is_full:
            full_access = True
            if is_protected:
                is_admin = True
        if code and scope:
            grants.setdefault(code, set()).add(scope)
    if full_access:
        for code, scope in full_access_grants():
            grants.setdefault(code, set()).add(scope)
    return (
        {code: frozenset(scopes) for code, scopes in grants.items()},
        frozenset(role_ids),
        frozenset(role_keys),
        is_admin,
    )


def load_scope_targets(db: Session, user_id: int) -> Dict[Tuple[str, str], FrozenSet[int]]:
    """Targets of entity/branch/pay-group-scoped grants, per (permission, scope), from active role assignments."""
    from app.models.org import RoleAssignmentTarget

    now = utcnow()
    targeted = tuple(s.value for s in TARGETED_SCOPES)
    rows = (
        db.query(RolePermission.permission_code, RolePermission.scope, RoleAssignmentTarget.target_id)
        .join(UserRoleAssignment, UserRoleAssignment.role_id == RolePermission.role_id)
        .join(
            RoleAssignmentTarget,
            (RoleAssignmentTarget.user_role_assignment_id == UserRoleAssignment.id)
            & (RoleAssignmentTarget.scope == RolePermission.scope),
        )
        .filter(
            UserRoleAssignment.user_id == user_id,
            RolePermission.scope.in_(targeted),
            UserRoleAssignment.effective_from <= now,
            or_(UserRoleAssignment.effective_until.is_(None), UserRoleAssignment.effective_until > now),
        )
        .all()
    )
    targets: Dict[Tuple[str, str], set] = {}
    for code, scope, target_id in rows:
        targets.setdefault((code, scope), set()).add(target_id)
    return {key: frozenset(ids) for key, ids in targets.items()}


def build_principal(db: Session, user: User, session_id: Optional[str], company: Company) -> Principal:
    grants, role_ids, role_keys, is_admin = load_grants(db, user.id)
    targeted = {s.value for s in TARGETED_SCOPES}
    has_targeted = any(scopes & targeted for scopes in grants.values())
    targets = load_scope_targets(db, user.id) if has_targeted else {}
    employee_id = db.query(Employee.id).filter(Employee.user_id == user.id).scalar()
    extra = {}
    team_scopes = {Scope.TEAM.value, Scope.TEAM_TREE.value, Scope.ASSIGNED_TEAMS.value}
    if any(scopes & team_scopes for scopes in grants.values()):
        extra["local_today"] = principal_local_today(db, employee_id, company)
    return Principal(
        user_id=user.id,
        company_id=user.company_id,
        session_id=session_id,
        email=user.email,
        full_name=user.full_name,
        employee_id=employee_id,
        grants=grants,
        role_ids=role_ids,
        role_keys=role_keys,
        is_company_admin=is_admin,
        permission_version=user.permission_version,
        subscription_active=subscription_is_active(company),
        extra=extra,
        targets=targets,
    )


def principal_local_today(db: Session, employee_id: Optional[int], company: Company) -> date:
    """The user's local today: the linked employee's work zone, else the company zone (team as-of rules, X 01 §4.3)."""
    from app.core import clock

    if employee_id is not None:
        from app.services.org_structure_service import employee_local_today

        employee = db.get(Employee, employee_id)
        if employee is not None:
            return employee_local_today(db, employee)
    import pytz

    return clock.utcnow().astimezone(pytz.timezone(company.timezone or "UTC")).date()


def _resolve_principal(request: Request, db: Session) -> Principal:
    token, via_cookie = _extract_token(request, ACCESS_COOKIE)
    if not token:
        raise unauthorized()
    claims = decode_jwt(token, "access")
    if claims and claims.get("pt") == "delegate":
        # A partner operator under a tenant delegation: bearer token only, checked against the live
        # delegation on every request (expiry and revocation apply at once).
        from app.services import partner_delegation_service

        if via_cookie:
            raise unauthorized("Session expired", "SESSION_EXPIRED")
        principal = partner_delegation_service.resolve_delegate_principal(request, db, claims)
        request.state.principal = principal
        return principal
    if not claims or claims.get("pt") != "user":
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    try:
        company_id = int(claims["cid"])
        user_id = int(claims["sub"])
        session_id = str(claims["sid"])
    except (KeyError, TypeError, ValueError):
        raise unauthorized("Session expired", "SESSION_EXPIRED")

    if via_cookie:
        _check_csrf(request)

    # A tenant host serves only its own tenant; a session never moves between hosts.
    context = request_host(request)
    if not company_allowed_on_host(context, company_id):
        raise unauthorized("Session expired", "SESSION_EXPIRED")

    bind_tenant(db, company_id)
    now = utcnow()
    session_row = db.get(UserSession, session_id)
    if (
        session_row is None
        or session_row.user_id != user_id
        or session_row.revoked_at is not None
        or session_row.expires_at <= now
        or (context.trusted and session_row.host and session_row.host != context.host)
    ):
        raise unauthorized("Session expired", "SESSION_EXPIRED")

    user = db.get(User, user_id)
    if user is None or user.company_id != company_id:
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    if user.status != "active":
        raise unauthorized("This account is not active", "ACCOUNT_INACTIVE")

    company = db.get(Company, company_id)
    if company is None or company.status != "active":
        raise AppError(status.HTTP_403_FORBIDDEN, "Your organization's workspace is suspended.", "COMPANY_SUSPENDED")

    if session_row.last_seen_at is None or now - session_row.last_seen_at > timedelta(minutes=5):
        session_row.last_seen_at = now
        db.commit()

    principal = build_principal(db, user, session_id, company)
    request.state.principal = principal
    return principal


def optional_principal(request: Request) -> Optional[Principal]:
    """The signed-in user if the request carries a valid session, else None (never raises)."""
    from app.database.db import SessionLocal

    db = SessionLocal()
    try:
        return _resolve_principal(request, db)
    except AppError:
        return None
    finally:
        db.close()


def get_principal_any_subscription(request: Request, db: Session = Depends(get_db)) -> Principal:
    """For session/billing endpoints that must work while a subscription lapses."""
    return _resolve_principal(request, db)


def get_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    principal = _resolve_principal(request, db)
    if not principal.subscription_active:
        raise AppError(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Your organization's subscription is inactive. Contact your administrator.",
            "SUBSCRIPTION_INACTIVE",
        )
    return principal


def require(*codes: str):
    """Function-level authorization: caller must hold every listed permission."""

    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        for code in codes:
            if not principal.has(code):
                raise forbidden()
        return principal

    return dependency


def require_any(*codes: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not any(principal.has(code) for code in codes):
            raise forbidden()
        return principal

    return dependency


# ---------------------------------------------------------------------------
# Platform administrators (separate principal type, separate cookies)
# ---------------------------------------------------------------------------
def require_platform_host(request: Request) -> None:
    """The platform console exists only on the platform host (01 §6.4 rule 4)."""
    if not request_host(request).is_platform:
        raise not_found()


def _operator_principal(request: Request, kind: str) -> PlatformPrincipal:
    """Platform operator ("platform") or partner administrator ("partner"); never interchangeable."""
    from app.database.db import system_session
    from app.models.company import Partner

    require_platform_host(request)
    token, via_cookie = _extract_token(request, PLATFORM_ACCESS_COOKIE)
    if not token:
        raise unauthorized()
    claims = decode_jwt(token, "access")
    if not claims or claims.get("pt") != kind:
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    if via_cookie:
        _check_csrf(request)
    with system_session() as db:
        session_row = db.get(PlatformSession, str(claims.get("sid")))
        admin = db.get(PlatformAdmin, int(claims.get("sub", 0)))
        if (
            session_row is None
            or admin is None
            or session_row.admin_id != admin.id
            or session_row.revoked_at is not None
            or session_row.expires_at <= utcnow()
            or not admin.is_active
            # The account itself decides its kind, not the token: a re-scoped account loses access at once.
            or (admin.partner_id is not None) != (kind == "partner")
        ):
            raise unauthorized("Session expired", "SESSION_EXPIRED")
        if kind == "partner":
            partner = db.get(Partner, admin.partner_id)
            if partner is None or partner.status != "active":
                raise unauthorized("Session expired", "SESSION_EXPIRED")
        return PlatformPrincipal(admin.id, admin.email, admin.full_name, session_row.id, admin.partner_id)


def get_platform_principal(request: Request) -> PlatformPrincipal:
    return _operator_principal(request, "platform")


def get_partner_principal(request: Request) -> PlatformPrincipal:
    return _operator_principal(request, "partner")


# ---------------------------------------------------------------------------
# Paired kiosk devices (device token, AS-M2; secret / HMAC devices follow in AS-M7)
# ---------------------------------------------------------------------------
KIOSK_TOKEN_COOKIE = "hrms_kiosk_device"
KIOSK_COOKIE_PATH = "/api/v1/attendance/kiosk"


def get_device_principal(request: Request, db: Session = Depends(get_db)):
    """The paired kiosk behind ``/attendance/kiosk/identify|punch`` (04-attendance/01 K-1..K-4, K-12).

    The device-token cookie (``HttpOnly; Secure; SameSite=Strict; Path=/api/v1/attendance/kiosk``) names the tenant
    and device; the tenant is bound before the device row is read (RLS), and the secret is compared in constant time.
    It is never a user session and carries no permission. 401 ``DEVICE_AUTH_FAILED`` for a missing, wrong, expired or
    revoked token; 403 ``DEVICE_SUSPENDED``. A tenant host serves only its own tenant's kiosks.
    """
    from app.services import attendance_devices

    parsed = attendance_devices.parse_token(request.cookies.get(KIOSK_TOKEN_COOKIE))
    if parsed is None:
        raise AppError(status.HTTP_401_UNAUTHORIZED, "This kiosk is not paired.", "DEVICE_AUTH_FAILED")
    company_id, device_id, secret = parsed
    if not company_allowed_on_host(request_host(request), company_id):
        raise AppError(status.HTTP_401_UNAUTHORIZED, "This kiosk is not paired.", "DEVICE_AUTH_FAILED")
    bind_tenant(db, company_id)
    company = db.get(Company, company_id)
    if company is None or company.status != "active" or not subscription_is_active(company):
        raise AppError(status.HTTP_401_UNAUTHORIZED, "This kiosk is not active.", "DEVICE_AUTH_FAILED")
    device = attendance_devices.authenticate(db, device_id, secret)
    request.state.device_principal = device
    return device
