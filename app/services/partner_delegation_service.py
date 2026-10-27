"""Partner operator delegated access (Goal 2 section 15: "only under explicit, current, audited tenant delegation").

A partner (reseller or payroll bureau) never gets implicit access to a tenant's HR or payroll data. A tenant
administrator with `settings:partner_access:manage` grants the tenant's own partner a delegation (step-up
required): specific read-only permission codes, optionally narrowed to legal entities and pay groups, a start
and an expiry of at most 30 days, and a reason. A partner operator then exchanges a current delegation for a
short-lived bearer token (`pt = delegate`); every request with that token is resolved against the live
delegation row, so expiry, revocation, a re-assigned partner or a suspended partner / operator / tenant
end the access on the very next call.

What a delegated principal can do:

* read only: every non-GET/HEAD/OPTIONS request is refused (the operator is not a tenant user, so there is no
  user to attribute a change to);
* only the delegated permission codes, at COMPANY scope or, for a narrowed delegation, at LEGAL_ENTITY /
  PAY_GROUP scope with exactly the delegated targets (a code that cannot be narrowed is refused at grant time);
* never money actions or identity changes: `is_delegable` allows only registry codes whose action is `read`
  or `read_masked`, and additionally excludes by code `payroll:payment:*`, `payroll:run:finalize`,
  `payroll:run:approve`, `payroll:bank_details:read_full`, user / role administration and the partner-access
  permission itself, even if a future registry change made one of them look like a read;
* no identity or session routes (`/auth`, `/users`, `/roles`, step-up, partner-access management).

Every request is written to the tenant's audit trail with the operator and the delegation id (actor type
`partner`), including refused ones.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import AppError, bad_request, conflict, not_found, unauthorized
from app.core.hosts import company_allowed_on_host, request_host
from app.core.permission_registry import PERMISSIONS_BY_CODE, Scope
from app.core.principal import SAFE_METHODS, DelegationContext, Principal, subscription_is_active
from app.core.rate_limit import limiter
from app.core.security import create_jwt, utcnow
from app.database.db import bind_tenant, system_session
from app.models.audit_log import AuditLog
from app.models.company import Company, Partner, PlatformAdmin, PlatformSession
from app.models.org import LegalEntity
from app.models.pay_org import PayGroup
from app.models.partner_delegations import (
    MAX_DELEGATION_DAYS,
    REASON_MAX,
    REASON_MIN,
    PartnerDelegation,
    PartnerDelegationTarget,
)
from app.models.user import User
from app.services.audit_log_service import AuditLogService

AUDIT_MODULE = "Partner access"
MANAGE_CODE = "settings:partner_access:manage"
TOKEN_TTL = timedelta(minutes=10)
START_TOLERANCE = timedelta(minutes=5)
MAX_OPEN_DELEGATIONS = 20
MAX_CODES = 60
MAX_TARGETS = 50

READ_ACTIONS = ("read", "read_masked")
# Excluded by code, whatever the action looks like (money movement and approval, full bank numbers, identity
# and access administration, this feature itself).
NEVER_DELEGABLE: FrozenSet[str] = frozenset({
    "payroll:run:finalize", "payroll:run:approve", "payroll:bank_details:read_full",
    "payroll:bank_details:approve", "payroll:bank_details:edit", "payroll:funding_accounts:manage",
    "settings:approve_changes", MANAGE_CODE, "delegation:manage", "domain:manage", "face:enroll",
    "roster:exempt",  # AS spine CV-6 (D-AS-48)
})
NEVER_DELEGABLE_PREFIXES = ("payroll:payment:", "user:", "role:")
DELEGABLE_SCOPES = (Scope.COMPANY, Scope.LEGAL_ENTITY, Scope.PAY_GROUP)
# Identity-bound and delegation-management routes a delegated principal never reaches.
BLOCKED_PREFIXES = ("/api/v1/auth", "/api/v1/users", "/api/v1/roles", "/api/v1/me", "/api/v1/payroll/step-up",
                    "/api/v1/partner-access")


def route_blocked(path: str) -> bool:
    """True for a path that is, or lies under, an identity-bound or delegation-management route."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in BLOCKED_PREFIXES)


def is_delegable(code: str) -> bool:
    """True when `code` may appear in a delegation: a registered, current, read-only permission."""
    perm = PERMISSIONS_BY_CODE.get(code)
    if perm is None or perm.deprecated:
        return False
    if code in NEVER_DELEGABLE or code.startswith(NEVER_DELEGABLE_PREFIXES):
        return False
    return perm.action.rsplit(":", 1)[-1] in READ_ACTIONS and Scope.COMPANY in perm.scopes


def narrowable_by(code: str) -> List[str]:
    perm = PERMISSIONS_BY_CODE[code]
    return [s.value for s in (Scope.LEGAL_ENTITY, Scope.PAY_GROUP) if s in perm.scopes]


def catalogue() -> List[dict]:
    """The delegable permissions for the grant dialog, in registry order."""
    return [
        {"code": p.code, "module": p.module, "label": p.label, "description": p.description,
         "sensitivity": p.sensitivity, "narrowable_by": narrowable_by(p.code)}
        for p in PERMISSIONS_BY_CODE.values() if is_delegable(p.code)
    ]


def build_grants(
    codes: Iterable[str], legal_entity_ids: Iterable[int], pay_group_ids: Iterable[int]
) -> Tuple[Dict[str, FrozenSet[str]], Dict[Tuple[str, str], FrozenSet[int]]]:
    """(grants, targets) of a delegation. Unnarrowed: COMPANY scope. Narrowed: only the targeted scopes the
    code supports, with exactly the delegated ids; a code that supports none of them gets nothing."""
    entities, groups = frozenset(legal_entity_ids), frozenset(pay_group_ids)
    narrowed = bool(entities or groups)
    grants: Dict[str, FrozenSet[str]] = {}
    targets: Dict[Tuple[str, str], FrozenSet[int]] = {}
    for code in codes:
        if not is_delegable(code):
            continue
        perm = PERMISSIONS_BY_CODE[code]
        if not narrowed:
            grants[code] = frozenset({Scope.COMPANY.value})
            continue
        scopes = set()
        if entities and Scope.LEGAL_ENTITY in perm.scopes:
            scopes.add(Scope.LEGAL_ENTITY.value)
            targets[(code, Scope.LEGAL_ENTITY.value)] = entities
        if groups and Scope.PAY_GROUP in perm.scopes:
            scopes.add(Scope.PAY_GROUP.value)
            targets[(code, Scope.PAY_GROUP.value)] = groups
        if scopes:
            grants[code] = frozenset(scopes)
    return grants, targets


def effective_status(row: PartnerDelegation, now: Optional[datetime] = None) -> str:
    now = now or utcnow()
    if row.status == "REVOKED":
        return "REVOKED"
    if row.expires_at <= now:
        return "EXPIRED"
    if row.starts_at > now:
        return "PENDING"
    return "ACTIVE"


def _label(code: str) -> str:
    perm = PERMISSIONS_BY_CODE.get(code)
    return perm.label if perm else code


def _targets(db: Session, delegation_id: int) -> Tuple[List[int], List[int]]:
    rows = db.query(PartnerDelegationTarget).filter(PartnerDelegationTarget.delegation_id == delegation_id).all()
    return (sorted(r.legal_entity_id for r in rows if r.kind == "LEGAL_ENTITY"),
            sorted(r.pay_group_id for r in rows if r.kind == "PAY_GROUP"))


def _names(db: Session, model, ids: List[int], attr: str) -> List[dict]:
    if not ids:
        return []
    rows = {r.id: r for r in db.query(model).filter(model.id.in_(ids)).all()}
    return [{"id": i, "name": getattr(rows[i], attr) if i in rows else None} for i in ids]


def serialize(db: Session, row: PartnerDelegation, partner_name: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    entity_ids, group_ids = _targets(db, row.id)
    users = {u.id: u for u in db.query(User).filter(User.id.in_([i for i in (row.granted_by_user_id, row.revoked_by_user_id) if i]))}
    return {
        "id": row.id,
        "partner_id": row.partner_id,
        "partner_name": partner_name,
        "status": effective_status(row, now),
        "permission_codes": list(row.permission_codes),
        "permissions": [{"code": c, "label": _label(c)} for c in row.permission_codes],
        "legal_entities": _names(db, LegalEntity, entity_ids, "legal_name"),
        "pay_groups": _names(db, PayGroup, group_ids, "name"),
        "narrowed": bool(entity_ids or group_ids),
        "read_only": True,
        "starts_at": row.starts_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "reason": row.reason,
        "granted_by": {"id": row.granted_by_user_id, "name": users[row.granted_by_user_id].full_name
                       if row.granted_by_user_id in users else None},
        "granted_at": row.created_at.isoformat(),
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "revoked_by": ({"id": row.revoked_by_user_id, "name": users[row.revoked_by_user_id].full_name
                        if row.revoked_by_user_id in users else None} if row.revoked_by_user_id else None),
        "revoke_reason": row.revoke_reason,
    }


def _partner_of(company_id: int) -> Optional[Partner]:
    with system_session() as sdb:
        company = sdb.get(Company, company_id)
        if company is None or company.partner_id is None:
            return None
        partner = sdb.get(Partner, company.partner_id)
        if partner is not None:
            sdb.expunge(partner)
        return partner


# ---------------------------------------------------------------------------
# Tenant administrator side
# ---------------------------------------------------------------------------
def overview(db: Session, principal: Principal) -> dict:
    partner = _partner_of(principal.company_id)
    now = utcnow()
    rows = db.query(PartnerDelegation).order_by(PartnerDelegation.created_at.desc(), PartnerDelegation.id.desc()).limit(200).all()
    name = partner.name if partner else None
    return {
        "partner": {"id": partner.id, "name": partner.name, "status": partner.status} if partner else None,
        "max_days": MAX_DELEGATION_DAYS,
        "reason_min": REASON_MIN,
        "catalogue": catalogue(),
        "targets": {
            "legal_entities": [{"id": e.id, "code": e.code, "name": e.legal_name}
                               for e in db.query(LegalEntity).filter(LegalEntity.status == "ACTIVE").order_by(LegalEntity.code)],
            "pay_groups": [{"id": g.id, "code": g.code, "name": g.name, "legal_entity_id": g.legal_entity_id}
                           for g in db.query(PayGroup).filter(PayGroup.status == "ACTIVE").order_by(PayGroup.code)],
        },
        "items": [serialize(db, r, name, now) for r in rows],
    }


def _validate_scope(db: Session, principal: Principal, codes: List[str], entity_ids: List[int], group_ids: List[int]) -> None:
    if not codes or len(codes) > MAX_CODES:
        raise bad_request("Choose at least one permission to delegate.", "DELEGATION_CODES_REQUIRED")
    bad = [c for c in codes if not is_delegable(c)]
    if bad:
        raise AppError(422, "These permissions cannot be delegated to a partner: " + ", ".join(bad), "NOT_DELEGABLE")
    # Delegating is not a way to gain access: the grantor must hold every code company-wide.
    exceeding = [c for c in codes if not principal.has_company_scope(c)]
    if exceeding:
        raise AppError(403, "You can only delegate permissions you hold company-wide yourself: " + ", ".join(exceeding),
                       "DELEGATION_EXCEEDS_GRANTOR")
    if len(entity_ids) > MAX_TARGETS or len(group_ids) > MAX_TARGETS:
        raise bad_request("Too many legal entities or pay groups.", "DELEGATION_TARGETS_INVALID")
    if entity_ids:
        found = {i for (i,) in db.query(LegalEntity.id).filter(LegalEntity.id.in_(entity_ids))}
        if found != set(entity_ids):
            raise AppError(422, "Unknown legal entity.", "DELEGATION_TARGETS_INVALID")
    if group_ids:
        found = {i for (i,) in db.query(PayGroup.id).filter(PayGroup.id.in_(group_ids))}
        if found != set(group_ids):
            raise AppError(422, "Unknown pay group.", "DELEGATION_TARGETS_INVALID")
    if entity_ids or group_ids:
        grants, _ = build_grants(codes, entity_ids, group_ids)
        unnarrowable = [c for c in codes if c not in grants]
        if unnarrowable:
            raise AppError(422, "These permissions cannot be limited to the chosen legal entities / pay groups: "
                           + ", ".join(unnarrowable), "NOT_NARROWABLE")


def grant(
    db: Session, principal: Principal, *, codes: List[str], legal_entity_ids: List[int], pay_group_ids: List[int],
    starts_at: Optional[datetime], expires_at: datetime, reason: str, ip_address: Optional[str] = None,
) -> PartnerDelegation:
    now = utcnow()
    company = db.get(Company, principal.company_id)
    partner = _partner_of(principal.company_id)
    if company is None or partner is None:
        raise conflict("This workspace is not managed by a partner.", "NO_PARTNER")
    if partner.status != "active":
        raise conflict("The partner is not active.", "PARTNER_INACTIVE")
    codes = sorted(set(codes))
    entity_ids, group_ids = sorted(set(legal_entity_ids)), sorted(set(pay_group_ids))
    reason = reason.strip()
    if not REASON_MIN <= len(reason) <= REASON_MAX:
        raise bad_request(f"Give a reason of {REASON_MIN} to {REASON_MAX} characters.", "DELEGATION_REASON_INVALID")
    start = starts_at or now
    if start < now - START_TOLERANCE:
        raise bad_request("The start cannot be in the past.", "DELEGATION_WINDOW_INVALID")
    start = max(start, now)
    if start > now + timedelta(days=MAX_DELEGATION_DAYS):
        raise bad_request(f"The start must be within {MAX_DELEGATION_DAYS} days.", "DELEGATION_WINDOW_INVALID")
    if expires_at <= start:
        raise bad_request("The expiry must be after the start.", "DELEGATION_WINDOW_INVALID")
    if expires_at - start > timedelta(days=MAX_DELEGATION_DAYS):
        raise bad_request(f"Access can last at most {MAX_DELEGATION_DAYS} days.", "DELEGATION_WINDOW_INVALID")
    _validate_scope(db, principal, codes, entity_ids, group_ids)
    open_count = (db.query(func.count(PartnerDelegation.id))
                  .filter(PartnerDelegation.status == "ACTIVE", PartnerDelegation.expires_at > now).scalar())
    if open_count >= MAX_OPEN_DELEGATIONS:
        raise conflict("Too many open delegations. Revoke one first.", "TOO_MANY_DELEGATIONS")

    row = PartnerDelegation(
        company_id=principal.company_id, partner_id=partner.id, permission_codes=codes, starts_at=start,
        expires_at=expires_at, reason=reason, status="ACTIVE", granted_by_user_id=principal.user_id,
    )
    db.add(row)
    db.flush()
    for entity_id in entity_ids:
        db.add(PartnerDelegationTarget(company_id=principal.company_id, delegation_id=row.id, kind="LEGAL_ENTITY",
                                       legal_entity_id=entity_id))
    for group_id in group_ids:
        db.add(PartnerDelegationTarget(company_id=principal.company_id, delegation_id=row.id, kind="PAY_GROUP",
                                       pay_group_id=group_id))
    AuditLogService.log(
        db, principal, AUDIT_MODULE, "DELEGATION_GRANTED",
        f"{principal.email} granted {partner.name} read-only access until {expires_at.isoformat()}",
        "partner_delegation", row.id, ip_address,
        details={"delegation_id": row.id, "partner_id": partner.id, "permission_codes": codes,
                 "legal_entity_ids": entity_ids, "pay_group_ids": group_ids, "starts_at": start,
                 "expires_at": expires_at, "reason": reason},
    )
    return row


def revoke(db: Session, principal: Principal, delegation_id: int, reason: str, ip_address: Optional[str] = None) -> PartnerDelegation:
    row = db.get(PartnerDelegation, delegation_id)
    if row is None:
        raise not_found("Delegation")
    now = utcnow()
    if effective_status(row, now) in ("REVOKED", "EXPIRED"):
        raise conflict("This delegation has already ended.", "DELEGATION_ENDED")
    reason = reason.strip()
    if not 3 <= len(reason) <= REASON_MAX:
        raise bad_request("Say why you are revoking this access.", "DELEGATION_REASON_INVALID")
    row.status, row.revoked_at, row.revoked_by_user_id, row.revoke_reason = "REVOKED", now, principal.user_id, reason
    AuditLogService.log(
        db, principal, AUDIT_MODULE, "DELEGATION_REVOKED",
        f"{principal.email} revoked partner access (delegation {row.id})", "partner_delegation", row.id, ip_address,
        details={"delegation_id": row.id, "partner_id": row.partner_id, "reason": reason},
    )
    return row


def activity(db: Session, principal: Principal, delegation_id: int, limit: int = 100) -> List[dict]:
    """What the partner did under one delegation (newest first), from the tenant's audit trail."""
    if db.get(PartnerDelegation, delegation_id) is None:
        raise not_found("Delegation")
    rows = (db.query(AuditLog)
            .filter(AuditLog.company_id == principal.company_id, AuditLog.actor_type == "partner",
                    AuditLog.details["delegation_id"].astext == str(delegation_id))
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(max(1, min(limit, 200))).all())
    return [{"id": r.id, "at": r.created_at.isoformat(), "action": r.action, "description": r.description,
             "operator_email": r.actor_email,
             "method": (r.details or {}).get("method"), "path": (r.details or {}).get("path")} for r in rows]


# ---------------------------------------------------------------------------
# Partner operator side
# ---------------------------------------------------------------------------
def _current_filters(partner_id: int, now: datetime):
    return (PartnerDelegation.partner_id == partner_id, PartnerDelegation.status == "ACTIVE",
            PartnerDelegation.starts_at <= now, PartnerDelegation.expires_at > now)


def current_for_partner(partner_id: int) -> List[dict]:
    """Delegations this partner holds right now, with aggregate facts only (no tenant HR data)."""
    now = utcnow()
    with system_session() as sdb:
        rows = (sdb.query(PartnerDelegation, Company)
                .join(Company, Company.id == PartnerDelegation.company_id)
                .filter(*_current_filters(partner_id, now), Company.partner_id == partner_id, Company.status == "active")
                .order_by(PartnerDelegation.expires_at, PartnerDelegation.id).all())
        out = []
        for row, company in rows:
            targets = sdb.query(PartnerDelegationTarget).filter(
                PartnerDelegationTarget.company_id == company.id, PartnerDelegationTarget.delegation_id == row.id).all()
            out.append({
                "id": row.id, "company": {"id": company.id, "name": company.name, "slug": company.slug},
                "permissions": [{"code": c, "label": _label(c)} for c in row.permission_codes],
                "narrowed": bool(targets), "read_only": True,
                "legal_entity_count": sum(1 for t in targets if t.kind == "LEGAL_ENTITY"),
                "pay_group_count": sum(1 for t in targets if t.kind == "PAY_GROUP"),
                "starts_at": row.starts_at.isoformat(), "expires_at": row.expires_at.isoformat(), "reason": row.reason,
            })
        return out


def _refusal(row: Optional[PartnerDelegation], company: Optional[Company], partner_id: int, company_id: int,
             now: datetime) -> Optional[str]:
    if row is None or row.company_id != company_id or row.partner_id != partner_id:
        return "unknown"
    if company is None or company.partner_id != partner_id:
        return "partner_changed"
    if row.status != "ACTIVE":
        return "revoked"
    if row.expires_at <= now:
        return "expired"
    if row.starts_at > now:
        return "not_started"
    return None


def issue_token(operator, delegation_id: int, ip_address: Optional[str] = None) -> dict:
    """Exchange a current delegation for a short-lived tenant-scoped bearer token."""
    limiter.check(f"delegate-token:{operator.admin_id}", 30, 900)
    now = utcnow()
    with system_session() as sdb:
        row = sdb.get(PartnerDelegation, delegation_id)
        # Another partner's (or a missing) delegation is simply not found.
        if row is None or row.partner_id != operator.partner_id:
            raise not_found("Delegation")
        company = sdb.get(Company, row.company_id)
        refusal = _refusal(row, company, operator.partner_id, row.company_id, now)
        if refusal or company.status != "active" or not subscription_is_active(company):
            raise AppError(403, "This delegation is not current.", "DELEGATION_NOT_CURRENT")
        ttl = min(TOKEN_TTL, row.expires_at - now)
        token = create_jwt(
            {"sub": str(operator.admin_id), "pt": "delegate", "did": str(row.id), "cid": str(row.company_id),
             "pid": str(operator.partner_id), "psid": operator.session_id},
            "access", ttl,
        )
        AuditLogService.log(
            sdb, None, AUDIT_MODULE, "DELEGATION_TOKEN_ISSUED",
            f"{operator.email} opened {company.name} under delegation {row.id}", "partner_delegation", row.id,
            ip_address, company_id=row.company_id, actor_type="partner", actor_id=operator.admin_id,
            actor_email=operator.email,
            details={"delegation_id": row.id, "partner_id": operator.partner_id, "expires_at": now + ttl},
        )
        sdb.commit()
        grants, targets = build_grants(row.permission_codes, *_ids(sdb, row))
        return {
            "access_token": token, "token_type": "bearer", "expires_at": (now + ttl).isoformat(),
            "delegation_expires_at": row.expires_at.isoformat(), "read_only": True,
            "company": {"id": company.id, "name": company.name, "slug": company.slug},
            "permissions": sorted(grants),
            "targets": {f"{code}@{scope}": sorted(ids) for (code, scope), ids in targets.items()},
        }


def _ids(db: Session, row: PartnerDelegation) -> Tuple[List[int], List[int]]:
    rows = db.query(PartnerDelegationTarget).filter(
        PartnerDelegationTarget.company_id == row.company_id, PartnerDelegationTarget.delegation_id == row.id).all()
    return ([r.legal_entity_id for r in rows if r.kind == "LEGAL_ENTITY"],
            [r.pay_group_id for r in rows if r.kind == "PAY_GROUP"])


# ---------------------------------------------------------------------------
# Per-request resolution (called from app.core.principal)
# ---------------------------------------------------------------------------
@dataclass
class _Operator:
    admin_id: int
    email: str
    full_name: str


def _live_operator(claims: dict, partner_id: int) -> _Operator:
    """The operator's platform session, account and partner must all still be valid."""
    try:
        admin_id, session_id = int(claims["sub"]), str(claims["psid"])
    except (KeyError, TypeError, ValueError):
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    with system_session() as sdb:
        session_row = sdb.get(PlatformSession, session_id)
        admin = sdb.get(PlatformAdmin, admin_id)
        partner = sdb.get(Partner, partner_id)
        if (
            session_row is None or admin is None or session_row.admin_id != admin.id
            or session_row.revoked_at is not None or session_row.expires_at <= utcnow()
            or not admin.is_active or admin.partner_id != partner_id
            or partner is None or partner.status != "active"
        ):
            raise unauthorized("Session expired", "SESSION_EXPIRED")
        return _Operator(admin.id, admin.email, admin.full_name)


def _audit_request(db: Session, context: DelegationContext, company_id: int, action: str, request: Request,
                   extra: Optional[dict] = None) -> None:
    details = {"delegation_id": context.delegation_id, "partner_id": context.partner_id,
               "method": request.method, "path": request.url.path, **(extra or {})}
    db.add(AuditLog(
        company_id=company_id, actor_type="partner", actor_id=context.operator_id, actor_email=context.operator_email,
        module=AUDIT_MODULE, action=action,
        description=f"{context.operator_email} {request.method} {request.url.path} under delegation {context.delegation_id}"[:500],
        entity_type="partner_delegation", entity_id=str(context.delegation_id),
        ip_address=(request.client.host if request.client else None), details=details,
    ))
    db.commit()


def resolve_delegate_principal(request: Request, db: Session, claims: dict) -> Principal:
    """The tenant-scoped, read-only principal of a partner operator, from the live delegation (never from the token)."""
    try:
        delegation_id, company_id, partner_id = int(claims["did"]), int(claims["cid"]), int(claims["pid"])
    except (KeyError, TypeError, ValueError):
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    context_host = request_host(request)
    if not company_allowed_on_host(context_host, company_id):
        raise unauthorized("Session expired", "SESSION_EXPIRED")
    operator = _live_operator(claims, partner_id)
    context = DelegationContext(delegation_id, partner_id, operator.admin_id, operator.email, str(claims["psid"]))

    bind_tenant(db, company_id)
    row = db.get(PartnerDelegation, delegation_id)
    company = db.get(Company, company_id)
    now = utcnow()
    refusal = _refusal(row, company, partner_id, company_id, now)
    if refusal is None and (company.status != "active"):
        refusal = "company_inactive"
    if refusal is not None:
        # Forged or foreign ids leave no tenant trace; a real delegation of this partner records the refusal.
        if row is not None and row.partner_id == partner_id and row.company_id == company_id:
            _audit_request(db, context, company_id, "DELEGATED_ACCESS_REFUSED", request, {"reason": refusal})
        raise AppError(403, "This delegation is not current.", "DELEGATION_NOT_CURRENT")

    path = request.url.path
    if request.method not in SAFE_METHODS:
        _audit_request(db, context, company_id, "DELEGATED_REQUEST_DENIED", request, {"reason": "read_only"})
        raise AppError(403, "Delegated partner access is read-only.", "DELEGATION_READ_ONLY")
    if route_blocked(path):
        _audit_request(db, context, company_id, "DELEGATED_REQUEST_DENIED", request, {"reason": "route_blocked"})
        raise AppError(403, "Delegated partner access cannot use this endpoint.", "DELEGATION_ROUTE_BLOCKED")

    entity_ids, group_ids = _ids(db, row)
    grants, targets = build_grants(row.permission_codes, entity_ids, group_ids)
    _audit_request(db, context, company_id, "DELEGATED_REQUEST", request)
    return Principal(
        user_id=0, company_id=company_id, session_id=None, email=operator.email,
        full_name=f"{operator.full_name} (partner)", employee_id=None, grants=grants, role_ids=frozenset(),
        role_keys=frozenset(), is_company_admin=False, permission_version=0,
        subscription_active=subscription_is_active(company), targets=targets, delegation=context,
    )
