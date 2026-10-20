"""Which host a request arrived on, and which tenant (if any) that host belongs to.

The browser only talks to the Next.js server, which proxies /api/v1 to this API.
The Next.js middleware overwrites ``X-HRMS-Host`` with the real ``Host`` header and
adds ``X-HRMS-Proxy-Key``. The original host is trusted only when that shared
secret matches; any other request (direct calls, tests, a forged header without
the key) counts as the platform host, which never restricts or brands by tenant.

Host -> tenant lookups read ``tenant_domains`` through the system session and are
cached briefly per process. Unknown hosts never resolve to a tenant.
"""

import hmac
import ipaddress
import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from fastapi import Request

from app.config import settings

HOST_HEADER = "x-hrms-host"
PROXY_KEY_HEADER = "x-hrms-proxy-key"

PLATFORM = "PLATFORM"
TENANT = "TENANT"
UNKNOWN = "UNKNOWN"

# Subdomains that can never become tenant hosts (01-white-label §6.1).
RESERVED_LABELS = frozenset(
    "www app api admin platform partner partners auth login sso mail smtp static assets cdn status help "
    "support docs billing security root".split()
)

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class HostContext:
    host: str
    kind: str  # PLATFORM | TENANT | UNKNOWN
    company_id: Optional[int] = None
    domain_id: Optional[int] = None
    trusted: bool = False

    @property
    def is_platform(self) -> bool:
        return self.kind == PLATFORM

    @property
    def is_tenant(self) -> bool:
        return self.kind == TENANT


def normalize_host(raw: Optional[str]) -> Optional[str]:
    """Lowercase IDNA (punycode) hostname without port or trailing dot, or None if invalid."""
    if not raw:
        return None
    value = raw.strip()
    if not value or len(value) > 300 or any(c in value for c in "/\\@?#") or any(c.isspace() for c in value):
        return None
    if value.startswith("["):  # [IPv6]:port
        end = value.find("]")
        if end < 0:
            return None
        try:
            return str(ipaddress.IPv6Address(value[1:end]))
        except ValueError:
            return None
    if value.count(":") == 1:
        value, port = value.split(":")
        if not port.isdigit():
            return None
    value = value.rstrip(".")
    try:
        ipaddress.IPv4Address(value)
        return value
    except ValueError:
        pass
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not value or len(value) > 253:
        return None
    if not all(_LABEL.match(label) for label in value.split(".")):
        return None
    return value


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_platform_host(host: str) -> bool:
    return host in settings.platform_hosts or (
        bool(settings.platform_base_domain) and host == settings.platform_base_domain
    )


def platform_subdomain_label(host: str) -> Optional[str]:
    """`acme` for `acme.<base domain>`; None for anything else (including deeper names)."""
    base = settings.platform_base_domain
    if not base or not host.endswith("." + base):
        return None
    label = host[: -len(base) - 1]
    return label if label and "." not in label else None


def proxy_key_valid(presented: Optional[str]) -> bool:
    secret = settings.proxy_shared_secret
    if not secret or not presented:
        return False
    return hmac.compare_digest(presented.encode(), secret.encode())


# ---------------------------------------------------------------------------
# Host -> tenant cache
# ---------------------------------------------------------------------------
_cache: Dict[str, Tuple[float, Optional[int], Optional[int]]] = {}
_cache_lock = threading.Lock()


def invalidate_host_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _lookup_tenant(host: str) -> Tuple[Optional[int], Optional[int]]:
    """(company_id, domain_id) of a serving domain, else (None, None).

    A domain whose DNS check broke keeps serving during its grace period (01 §6.3); it is
    disabled, and its sessions revoked, when the grace period ends.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(host)
        if hit and hit[0] > now:
            return hit[1], hit[2]
    from app.database.db import system_session
    from app.models.branding import TenantDomain
    from app.models.company import Company

    with system_session() as db:
        row = (
            db.query(TenantDomain.company_id, TenantDomain.id)
            .join(Company, Company.id == TenantDomain.company_id)
            .filter(TenantDomain.hostname == host, TenantDomain.status.in_(("ACTIVE", "DNS_BROKEN")))
            .first()
        )
    company_id, domain_id = (row[0], row[1]) if row else (None, None)
    with _cache_lock:
        if len(_cache) > 5000:
            _cache.clear()
        _cache[host] = (now + _CACHE_TTL_SECONDS, company_id, domain_id)
    return company_id, domain_id


def resolve_host(host: Optional[str], trusted: bool) -> HostContext:
    if not trusted or not host:
        return HostContext(host=host or "", kind=PLATFORM, trusted=False)
    if is_platform_host(host):
        return HostContext(host=host, kind=PLATFORM, trusted=True)
    company_id, domain_id = _lookup_tenant(host)
    if company_id is None:
        return HostContext(host=host, kind=UNKNOWN, trusted=True)
    return HostContext(host=host, kind=TENANT, company_id=company_id, domain_id=domain_id, trusted=True)


def request_host(request: Request) -> HostContext:
    """The host context of this request (computed once per request)."""
    cached = getattr(request.state, "host_context", None)
    if cached is not None:
        return cached
    trusted = proxy_key_valid(request.headers.get(PROXY_KEY_HEADER))
    host = normalize_host(request.headers.get(HOST_HEADER)) if trusted else None
    context = resolve_host(host, trusted)
    request.state.host_context = context
    return context


def company_allowed_on_host(context: HostContext, company_id: int) -> bool:
    """Tenant hosts serve exactly one tenant; the platform host serves every tenant."""
    if context.kind == PLATFORM:
        return True
    if context.kind == TENANT:
        return context.company_id == company_id
    return False
