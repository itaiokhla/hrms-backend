"""Tenant hosts: platform subdomains and verified custom domains (01-white-label §6).

Lifecycle of a custom domain:

    PENDING_DNS --(TXT + CNAME found)--> VERIFYING --(certificate)--> ACTIVE
        |  72 h without records -> FAILED     |  3 TLS failures -> FAILED
    ACTIVE --(daily re-check fails)--> DNS_BROKEN --(14 days)--> DISABLED
    any -> REMOVED (soft delete; sessions created on the host are revoked)

Ownership is proven by a TXT record (_hrms-challenge.<host> = hrms-verify=<token>)
and a hostname can be live for only one tenant at a time (partial unique index), so
a dangling CNAME cannot be taken over by another tenant.

DNS and TLS are pluggable: production uses real DNS and a TLS handshake probe
against the host (certificates are issued by the hosting layer, e.g. on-demand TLS
at the reverse proxy). The fixture resolver and the local-dev TLS provider exist for
development and tests only and are refused by the configuration elsewhere.
"""

import json
import logging
import secrets
import socket
import ssl
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from publicsuffixlist import PublicSuffixList
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core import hosts
from app.core.errors import AppError, bad_request, conflict, not_found
from app.core.security import utcnow
from app.database.db import SessionLocal, bind_tenant, system_session
from app.models.branding import TenantDomain
from app.models.company import Company
from app.models.user import UserSession

logger = logging.getLogger(__name__)

CHALLENGE_PREFIX = "_hrms-challenge"
TOKEN_PREFIX = "hrms-verify="
_psl = PublicSuffixList()


# ---------------------------------------------------------------------------
# DNS and TLS providers
# ---------------------------------------------------------------------------
class SystemResolver:
    def __init__(self, timeout: float = 5.0):
        import dns.resolver

        self._resolver = dns.resolver.Resolver()
        self._resolver.lifetime = timeout

    def txt(self, name: str) -> List[str]:
        import dns.exception

        try:
            answers = self._resolver.resolve(name, "TXT")
        except dns.exception.DNSException:
            return []
        return ["".join(part.decode("utf-8", "replace") for part in r.strings) for r in answers]

    def cname(self, name: str) -> Optional[str]:
        import dns.exception

        try:
            answers = self._resolver.resolve(name, "CNAME")
        except dns.exception.DNSException:
            return None
        return str(answers[0].target).rstrip(".").lower() if answers else None


class FixtureResolver:
    """Development/test only: records come from a JSON file that is re-read on every lookup."""

    def __init__(self, path: str):
        self.path = path

    def _records(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def txt(self, name: str) -> List[str]:
        return list(self._records().get(name.lower(), {}).get("TXT", []))

    def cname(self, name: str) -> Optional[str]:
        value = self._records().get(name.lower(), {}).get("CNAME")
        return value.rstrip(".").lower() if value else None


def resolver():
    if settings.domain_dns_provider == "fixture" and settings.environment in ("development", "test"):
        return FixtureResolver(settings.domain_dns_fixture_file)
    return SystemResolver()


def check_tls(host: str) -> Tuple[str, Optional[datetime], Optional[str]]:
    """(tls_status, expires_at, error). ISSUED only when a valid certificate for the host is served."""
    if settings.tls_provider == "local-dev" and settings.environment in ("development", "test"):
        return "ISSUED", None, None
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=8) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        return "PENDING", None, f"Certificate not valid for {host} yet: {exc.verify_message}"
    except (OSError, ssl.SSLError) as exc:
        return "PENDING", None, f"Could not complete a TLS handshake with {host}: {exc.__class__.__name__}"
    expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return "ISSUED", expires, None


def tls_mode() -> str:
    if settings.tls_provider == "local-dev" and settings.environment in ("development", "test"):
        return "SIMULATED"
    return "PROBE"


# ---------------------------------------------------------------------------
# Hostname rules
# ---------------------------------------------------------------------------
def validate_custom_hostname(raw: str) -> str:
    host = hosts.normalize_host(raw)
    if host is None or "." not in host:
        raise bad_request("Enter a hostname like hr.example.com (no https:// or path).", "DOMAIN_INVALID")
    if hosts.is_ip(host):
        raise bad_request("Use a hostname, not an IP address.", "DOMAIN_INVALID")
    base = settings.platform_base_domain
    if hosts.is_platform_host(host) or (base and (host == base or host.endswith("." + base))):
        raise bad_request("That address belongs to the platform. Use your own domain.", "DOMAIN_PLATFORM")
    if host.split(".")[0] in hosts.RESERVED_LABELS and host.count(".") == 1:
        raise bad_request("That hostname is reserved.", "DOMAIN_RESERVED")
    if _psl.is_public(host) or _psl.privatesuffix(host) is None:
        raise bad_request("That is a public suffix (like co.uk). Use a name inside your own domain.", "DOMAIN_PUBLIC_SUFFIX")
    return host


def is_apex(host: str) -> bool:
    return _psl.privatesuffix(host) == host


def _history(domain: TenantDomain, new_status: str, note: Optional[str] = None) -> None:
    if domain.status == new_status and not note:
        return
    entry = {"at": utcnow().isoformat(), "from": domain.status, "to": new_status}
    if note:
        entry["note"] = note[:300]
    domain.status_history = (list(domain.status_history or []) + [entry])[-50:]
    domain.status = new_status


def dns_records(domain: TenantDomain) -> List[dict]:
    if domain.kind != "CUSTOM":
        return []
    records = [{
        "type": "TXT", "name": f"{CHALLENGE_PREFIX}.{domain.hostname}",
        "value": f"{TOKEN_PREFIX}{domain.verification_token}", "purpose": "Proves you control this domain",
    }]
    records.append({
        "type": "CNAME", "name": domain.hostname,
        "value": settings.custom_domain_cname_target or "(platform CNAME target not configured)",
        "purpose": "Routes the domain to the platform" + ("" if not is_apex(domain.hostname) else
                   " (apex domains need an ALIAS/ANAME record at your DNS provider)"),
    })
    return records


def serialize(domain: TenantDomain) -> dict:
    return {
        "id": domain.id,
        "hostname": domain.hostname,
        "unicode_hostname": _to_unicode(domain.hostname),
        "kind": domain.kind,
        "status": domain.status,
        "is_primary": domain.is_primary,
        "redirect_to_primary": domain.redirect_to_primary,
        "enforce_custom_domain": domain.enforce_custom_domain,
        "verified_at": domain.verified_at.isoformat() if domain.verified_at else None,
        "last_checked_at": domain.last_checked_at.isoformat() if domain.last_checked_at else None,
        "last_error": domain.last_error,
        "dns_broken_since": domain.dns_broken_since.isoformat() if domain.dns_broken_since else None,
        "tls_status": domain.tls_status,
        "tls_mode": tls_mode(),
        "tls_expires_at": domain.tls_expires_at.isoformat() if domain.tls_expires_at else None,
        "records": dns_records(domain),
        "history": list(domain.status_history or []),
        "created_at": domain.created_at.isoformat() if domain.created_at else None,
        "apex": is_apex(domain.hostname) if domain.kind == "CUSTOM" else False,
    }


def _to_unicode(host: str) -> str:
    try:
        return host.encode("ascii").decode("idna")
    except UnicodeError:
        return host


# ---------------------------------------------------------------------------
# Platform subdomains
# ---------------------------------------------------------------------------
def subdomain_for(slug: str) -> Optional[str]:
    base = settings.platform_base_domain
    if not base or not slug or slug in hosts.RESERVED_LABELS:
        return None
    host = hosts.normalize_host(f"{slug}.{base}")
    return host if host and hosts.platform_subdomain_label(host) == slug else None


def ensure_platform_subdomain(db: Session, company: Company) -> Optional[TenantDomain]:
    """Create the tenant's <slug>.<base domain> host (ACTIVE, primary unless another host is)."""
    host = subdomain_for(company.slug)
    if host is None:
        return None
    existing = db.query(TenantDomain).filter(TenantDomain.hostname == host, TenantDomain.status != "REMOVED").first()
    if existing is not None:
        return existing if existing.company_id == company.id else None
    has_primary = db.query(TenantDomain.id).filter(
        TenantDomain.company_id == company.id, TenantDomain.is_primary.is_(True), TenantDomain.status != "REMOVED"
    ).first()
    row = TenantDomain(
        company_id=company.id, hostname=host, kind="PLATFORM_SUBDOMAIN", status="ACTIVE",
        is_primary=has_primary is None, tls_status="ISSUED", verified_at=utcnow(),
        status_history=[{"at": utcnow().isoformat(), "from": None, "to": "ACTIVE", "note": "platform subdomain"}],
    )
    db.add(row)
    db.flush()
    hosts.invalidate_host_cache()
    return row


def sync_platform_subdomains() -> dict:
    created = 0
    with system_session() as db:
        for company in db.query(Company).order_by(Company.id).all():
            before = db.query(TenantDomain.id).filter(
                TenantDomain.company_id == company.id, TenantDomain.kind == "PLATFORM_SUBDOMAIN",
                TenantDomain.status != "REMOVED",
            ).first()
            if before is None and ensure_platform_subdomain(db, company) is not None:
                created += 1
        db.commit()
    hosts.invalidate_host_cache()
    return {"created": created, "base_domain": settings.platform_base_domain or None}


# ---------------------------------------------------------------------------
# Links (invitations, password reset, emails) always use a verified host from the DB
# ---------------------------------------------------------------------------
def primary_host(company_id: int) -> Optional[str]:
    with system_session() as db:
        row = (
            db.query(TenantDomain.hostname)
            .filter(TenantDomain.company_id == company_id, TenantDomain.is_primary.is_(True),
                    TenantDomain.status == "ACTIVE")
            .first()
        )
    return row[0] if row else None


def base_url_for_host(host: str) -> str:
    platform = urlsplit(settings.frontend_url)
    port = f":{platform.port}" if platform.port else ""
    return f"{platform.scheme}://{host}{port}"


def public_base_url(company_id: Optional[int]) -> str:
    """https://<primary verified host> for a tenant; the platform URL when it has none."""
    if company_id is None:
        return settings.frontend_url.rstrip("/")
    host = primary_host(company_id)
    return base_url_for_host(host) if host else settings.frontend_url.rstrip("/")


# ---------------------------------------------------------------------------
# Tenant administration (RLS-bound session)
# ---------------------------------------------------------------------------
def list_domains(db: Session, company_id: int) -> List[dict]:
    rows = (
        db.query(TenantDomain)
        .filter(TenantDomain.company_id == company_id, TenantDomain.status != "REMOVED")
        .order_by(TenantDomain.is_primary.desc(), TenantDomain.created_at)
        .all()
    )
    return [serialize(r) for r in rows]


def get_domain(db: Session, company_id: int, domain_id: int, include_removed: bool = False) -> TenantDomain:
    query = db.query(TenantDomain).filter(TenantDomain.company_id == company_id, TenantDomain.id == domain_id)
    if not include_removed:
        query = query.filter(TenantDomain.status != "REMOVED")
    row = query.first()
    if row is None:
        raise not_found("Domain")
    return row


def add_custom_domain(db: Session, company_id: int, raw_hostname: str, user_id: Optional[int]) -> TenantDomain:
    host = validate_custom_hostname(raw_hostname)
    with system_session() as sysdb:
        taken = sysdb.query(TenantDomain.company_id).filter(
            TenantDomain.hostname == host, TenantDomain.status != "REMOVED"
        ).first()
    if taken is not None:
        # Same message whether this or another tenant holds it.
        raise conflict("That domain is already connected to a workspace.", "DOMAIN_TAKEN")
    row = TenantDomain(
        company_id=company_id, hostname=host, kind="CUSTOM", status="PENDING_DNS",
        verification_token=secrets.token_hex(32), tls_status="NONE", created_by_user_id=user_id,
        status_history=[{"at": utcnow().isoformat(), "from": None, "to": "PENDING_DNS"}],
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict("That domain is already connected to a workspace.", "DOMAIN_TAKEN")
    return row


def check_domain(db: Session, domain: TenantDomain, *, dns=None) -> TenantDomain:
    """Advance one domain through its state machine (idempotent)."""
    if domain.kind != "CUSTOM" or domain.status in ("REMOVED", "DISABLED", "FAILED"):
        return domain
    dns = dns or resolver()
    now = utcnow()
    domain.last_checked_at = now
    txt_ok = f"{TOKEN_PREFIX}{domain.verification_token}" in dns.txt(f"{CHALLENGE_PREFIX}.{domain.hostname}")
    target = settings.custom_domain_cname_target.strip(".").lower()
    cname = dns.cname(domain.hostname)
    route_ok = (cname == target) if target else True
    problems = []
    if not txt_ok:
        problems.append(f"TXT record {CHALLENGE_PREFIX}.{domain.hostname} not found or does not match")
    if not route_ok:
        problems.append(f"CNAME {domain.hostname} points to {cname or 'nothing'}, expected {target}")
    dns_ok = not problems

    if domain.status == "PENDING_DNS":
        if dns_ok:
            _history(domain, "VERIFYING", "DNS records found")
            domain.verified_at = now
            domain.tls_status = "PENDING"
        elif now - domain.created_at > timedelta(hours=settings.domain_verification_window_hours):
            _history(domain, "FAILED", "DNS records were not found within the verification window")
        domain.last_error = "; ".join(problems) or None
    if domain.status == "VERIFYING":
        if not dns_ok:
            domain.last_error = "; ".join(problems)
            return domain
        status, expires, error = check_tls(domain.hostname)
        domain.tls_status, domain.tls_expires_at = status, expires
        if status == "ISSUED":
            _history(domain, "ACTIVE", "certificate available" if tls_mode() == "PROBE" else "TLS simulated (development)")
            domain.last_error = None
            domain.tls_failures = 0
            hosts.invalidate_host_cache()
        else:
            domain.tls_failures += 1
            domain.last_error = error
            if domain.tls_failures >= 3:
                domain.tls_status = "FAILED"
                _history(domain, "FAILED", "certificate could not be issued")
    elif domain.status in ("ACTIVE", "DNS_BROKEN"):
        if dns_ok:
            if domain.status == "DNS_BROKEN":
                _history(domain, "ACTIVE", "DNS records restored")
                domain.dns_broken_since = None
                hosts.invalidate_host_cache()
            domain.last_error = None
            if domain.tls_expires_at and domain.tls_expires_at - now < timedelta(days=14):
                status, expires, error = check_tls(domain.hostname)
                domain.tls_status = "RENEWING" if status != "ISSUED" else "ISSUED"
                domain.tls_expires_at = expires or domain.tls_expires_at
                domain.last_error = error
        else:
            domain.last_error = "; ".join(problems)
            if domain.status == "ACTIVE":
                domain.dns_broken_since = now
                _history(domain, "DNS_BROKEN", domain.last_error)
                _notify_admins(db, domain, "Custom domain needs attention",
                               f"{domain.hostname}: {domain.last_error}. It will be disabled after "
                               f"{settings.domain_broken_grace_days} days.")
            elif now - (domain.dns_broken_since or now) > timedelta(days=settings.domain_broken_grace_days):
                _disable(db, domain, "DISABLED", "DNS broken for too long")
    db.flush()
    return domain


def _notify_admins(db: Session, domain: TenantDomain, title: str, message: str) -> None:
    from app.services import rbac_service
    from app.services.notification_service import NotificationService

    for user_id in rbac_service.active_admin_user_ids(db, domain.company_id):
        NotificationService.create(db, user_id, title, message, "system", "/settings/branding?tab=domains")


def revoke_host_sessions(db: Session, company_id: int, host: str, reason: str) -> int:
    result = db.execute(
        update(UserSession)
        .where(UserSession.company_id == company_id, UserSession.host == host, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoke_reason=reason)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def _disable(db: Session, domain: TenantDomain, status: str, note: str) -> None:
    was_primary = domain.is_primary
    _history(domain, status, note)
    domain.is_primary = False
    if status == "REMOVED":
        domain.removed_at = utcnow()
    domain.enforce_custom_domain = False
    domain.redirect_to_primary = False
    db.flush()
    revoke_host_sessions(db, domain.company_id, domain.hostname, f"domain_{status.lower()}")
    if was_primary:
        fallback = db.query(TenantDomain).filter(
            TenantDomain.company_id == domain.company_id, TenantDomain.kind == "PLATFORM_SUBDOMAIN",
            TenantDomain.status == "ACTIVE",
        ).first()
        if fallback is not None:
            fallback.is_primary = True
    hosts.invalidate_host_cache()


def remove_domain(db: Session, company_id: int, domain_id: int) -> TenantDomain:
    domain = get_domain(db, company_id, domain_id)
    if domain.kind == "PLATFORM_SUBDOMAIN":
        raise bad_request("The platform address cannot be removed.", "DOMAIN_PROTECTED")
    _disable(db, domain, "REMOVED", "removed by an administrator")
    return domain


def make_primary(db: Session, company_id: int, domain_id: int) -> TenantDomain:
    domain = get_domain(db, company_id, domain_id)
    if domain.status != "ACTIVE":
        raise bad_request("Only an active domain can be the primary address.", "DOMAIN_NOT_ACTIVE")
    for other in db.query(TenantDomain).filter(TenantDomain.company_id == company_id, TenantDomain.is_primary.is_(True)):
        if other.id != domain.id:
            other.is_primary = False
            other.redirect_to_primary = False
            other.enforce_custom_domain = False
    db.flush()
    domain.is_primary = True
    db.flush()
    hosts.invalidate_host_cache()
    return domain


def update_flags(db: Session, company_id: int, domain_id: int, *, redirect: Optional[bool], enforce: Optional[bool]) -> TenantDomain:
    domain = get_domain(db, company_id, domain_id)
    if (redirect or enforce) and not (domain.is_primary and domain.status == "ACTIVE"):
        raise bad_request("Make the domain the active primary address first.", "DOMAIN_NOT_PRIMARY")
    if enforce and domain.kind != "CUSTOM":
        raise bad_request("Only a custom domain can be enforced.", "DOMAIN_NOT_CUSTOM")
    if redirect is not None:
        domain.redirect_to_primary = redirect
    if enforce is not None:
        domain.enforce_custom_domain = enforce
    db.flush()
    hosts.invalidate_host_cache()
    return domain


def enforced_primary(company_id: int) -> Optional[str]:
    """Hostname that sign-ins must hand off to, when the tenant enforces its custom domain."""
    with system_session() as db:
        row = db.query(TenantDomain.hostname).filter(
            TenantDomain.company_id == company_id, TenantDomain.is_primary.is_(True),
            TenantDomain.status == "ACTIVE", TenantDomain.kind == "CUSTOM",
            TenantDomain.enforce_custom_domain.is_(True),
        ).first()
    return row[0] if row else None


def host_settings(company_id: int, host: str) -> dict:
    """Public routing facts for one tenant host (primary address and redirect flag)."""
    with system_session() as db:
        primary = db.query(TenantDomain).filter(
            TenantDomain.company_id == company_id, TenantDomain.is_primary.is_(True), TenantDomain.status == "ACTIVE"
        ).first()
    if primary is None or primary.hostname == host:
        return {"primary_url": None, "redirect_to_primary": False}
    return {"primary_url": base_url_for_host(primary.hostname), "redirect_to_primary": bool(primary.redirect_to_primary)}


def run_checks() -> List[str]:
    """Scheduled job: check every pending, verifying, active or broken custom domain."""
    output = []
    with system_session() as sysdb:
        targets = sysdb.query(TenantDomain.company_id, TenantDomain.id).filter(
            TenantDomain.kind == "CUSTOM",
            TenantDomain.status.in_(("PENDING_DNS", "VERIFYING", "ACTIVE", "DNS_BROKEN")),
        ).order_by(TenantDomain.company_id, TenantDomain.id).all()
    dns = resolver()
    for company_id, domain_id in targets:
        db = SessionLocal()
        try:
            bind_tenant(db, company_id)
            domain = db.get(TenantDomain, domain_id)
            before = domain.status
            check_domain(db, domain, dns=dns)
            db.commit()
            output.append(f"{domain.hostname}: {before} -> {domain.status}")
        except Exception as exc:  # one domain's failure must not stop the others
            db.rollback()
            output.append(f"domain {domain_id}: check failed: {exc}")
        finally:
            db.close()
    hosts.invalidate_host_cache()
    return output


def platform_disable_tenant_domains(db: Session, company_id: int, reason: str) -> int:
    """Abuse kill switch: disable every custom domain of a tenant (system session)."""
    count = 0
    for domain in db.query(TenantDomain).filter(
        TenantDomain.company_id == company_id, TenantDomain.kind == "CUSTOM",
        TenantDomain.status.in_(("PENDING_DNS", "VERIFYING", "ACTIVE", "DNS_BROKEN")),
    ):
        _disable(db, domain, "DISABLED", f"disabled by the platform: {reason}")
        count += 1
    return count


def raise_if_custom_not_entitled(entitlements) -> None:
    if "domains.custom" not in entitlements:
        raise AppError(403, "Custom domains are not included in your plan.", "DOMAIN_NOT_ENTITLED")
