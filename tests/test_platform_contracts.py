"""Contracts every current and future module must satisfy.

These tests read the running application and the migrated database, so a new route
or table that skips the tenancy and authorization foundation fails here without
anyone having to remember to write a test for it. See docs/saas/E-future-module-foundation.md.
"""

import os

from fastapi.routing import APIRoute
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Every API route without an authenticated principal, reviewed one by one. Adding a route
# here is a security decision: it must be reachable before sign-in by design.
PUBLIC_ROUTES = {
    ("GET", "/api/v1/public/brand"),
    ("GET", "/api/v1/public/brand-assets/{name}"),
    ("POST", "/api/v1/auth/signup"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/login/mfa"),
    ("POST", "/api/v1/auth/handoff"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/verify-otp"),
    ("POST", "/api/v1/auth/reset-password"),
    ("GET", "/api/v1/auth/invitations/{token}"),
    ("POST", "/api/v1/auth/invitations/accept"),
    ("POST", "/api/v1/platform/auth/login"),
    ("POST", "/api/v1/platform/auth/refresh"),
    ("POST", "/api/v1/platform/auth/logout"),
    ("POST", "/api/v1/partner/auth/login"),
    ("POST", "/api/v1/partner/auth/refresh"),
    ("POST", "/api/v1/partner/auth/logout"),
    # Payroll v2. Payout-provider callbacks carry no session: HMAC signature, timestamp window and replay
    # protection authenticate them (tests/payroll/test_payment_webhooks.py).
    ("POST", "/api/v1/webhooks/payments/{adapter}"),
    # Anyone holding a document may check it: issuer, type, date, number and masked name only, never
    # amounts; rate limited per IP and limited to the host's tenant (tests/payroll/test_document_isolation.py).
    ("GET", "/api/v1/documents/verify/{public_id}"),
    # AS-M2: a kiosk pairs itself with the one-time claim code an attendance admin created (no user session by design,
    # SP-AS-207); rate limited per address and host, failures counted per device (tests/attendance/test_kiosk_token.py).
    ("POST", "/api/v1/attendance/kiosk/pair"),
    # AS-M7 (G-17): biometric terminals and agents carry no user session. ADMS push authenticates with the serial plus
    # the per-device secret (unknown serials are quarantined, same 401 as a wrong secret); agents sign each batch
    # with HMAC-SHA256 (tests/attendance/test_device_ingest.py).
    ("GET", "/api/v1/iclock/cdata"),
    ("POST", "/api/v1/iclock/cdata"),
    ("GET", "/api/v1/iclock/getrequest"),
    ("POST", "/api/v1/iclock/devicecmd"),
    ("GET", "/api/v1/iclock/ping"),
    ("POST", "/api/v1/attendance/ingest/punches"),
    # G-24 (IG-AS-129): calendar apps fetch the published schedule without a session. The URL token carries the company
    # id and a 256-bit secret stored only as a SHA-256 hash; unknown token or company -> the same 404; revocable from
    # My schedule (tests/shifts/test_remaining_goals.py::test_calendar_feed_serves_the_published_schedule_until_revoked).
    ("GET", "/api/v1/calendar/{token}.ics"),
}

# Routes outside /api/, reviewed one by one: firmware cannot change the ADMS path (04-attendance/01 §8.1).
NON_API_ROUTES = {
    ("GET", "/health"),
    ("GET", "/iclock/cdata"),
    ("POST", "/iclock/cdata"),
    ("GET", "/iclock/getrequest"),
    ("POST", "/iclock/devicecmd"),
    ("GET", "/iclock/ping"),
}

# Tables with a company_id column that are deliberately not tenant (RLS) tables.
NON_TENANT_COMPANY_COLUMNS = {
    "audit_logs": "written by platform and system actors too; read only through company-filtered queries",
    "auth_handoff_codes": "read and written only by the system role; the app role has no privilege on it",
    # Payroll v2 (0009): NULL company_id is a platform row.
    "fx_rates": "platform rates (company_id NULL) are shared; RLS limits tenant rows and writes to the own tenant",
    "jobs": "claimed across tenants by the worker through the system session; tenant reads filter company_id in "
            "app/payroll/services/jobs.py and payloads carry ids only",
    # AS-M7 (0055): serial -> (company, device) of activated devices, read before the tenant is known (H-1).
    "device_registry_index": "read and written only by the system role; the app role has no privilege on it",
}
SYSTEM_ONLY_TABLES = ("auth_handoff_codes", "platform_admins", "platform_sessions", "commercial_brand_profiles",
                      "commercial_brand_assets", "partners", "device_quarantine", "device_registry_index")


def _dependency_calls(dependant):
    calls = set()
    for dep in dependant.dependencies:
        calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


def test_every_api_route_requires_a_principal_unless_reviewed_as_public(app):
    from app.core import principal

    authenticated = {principal.get_principal, principal.get_principal_any_subscription,
                     principal.get_platform_principal, principal.get_partner_principal,
                     principal.get_device_principal}  # paired kiosks: device token (AS-M2)
    unprotected = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            if not _dependency_calls(route.dependant) & authenticated:
                unprotected |= {(method, route.path) for method in route.methods}
    assert unprotected == PUBLIC_ROUTES


def test_non_api_routes_are_reviewed(app):
    found = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and not route.path.startswith("/api/"):
            found |= {(method, route.path) for method in route.methods}
    assert found == NON_API_ROUTES


def test_operator_routes_are_bound_to_platform_hosts(app):
    from app.core.principal import require_platform_host

    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(("/api/v1/platform/", "/api/v1/partner/")):
            assert require_platform_host in _dependency_calls(route.dependant), route.path


def _owner():
    return create_engine(os.environ["MIGRATION_DATABASE_URL"])


def test_every_tenant_table_enforces_row_level_security(app):
    from app.models import TENANT_TABLES

    with _owner().connect() as conn:
        rls = dict(conn.execute(text(
            "SELECT c.relname, c.relrowsecurity AND EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'"
        )).fetchall())
        with_company = {name for (name,) in conn.execute(text(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' AND column_name = 'company_id'"
        ))}
        app_role = make_url(os.environ["DATABASE_URL"]).username
        bypass = conn.execute(text("SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = :r"), {"r": app_role}).scalar()
        readable = [table for table in SYSTEM_ONLY_TABLES if conn.execute(
            text("SELECT has_table_privilege(:r, :t, 'SELECT')"), {"r": app_role, "t": table}).scalar()]
    assert readable == []
    assert bypass is False
    assert [t for t in TENANT_TABLES if not rls.get(t)] == []
    # A table that stores company_id but is not a tenant table is a reviewed exception, never an accident.
    assert with_company - set(TENANT_TABLES) - {"companies"} == set(NON_TENANT_COMPANY_COLUMNS)


def test_foreign_keys_between_tenant_tables_include_the_company(app):
    """A reference from one tenant row to another must name the same company (composite key)."""
    from app.models import TENANT_TABLES

    with _owner().connect() as conn:
        rows = conn.execute(text(
            """
            SELECT con.conname, src.relname, dst.relname,
                   ARRAY(SELECT a.attname FROM unnest(con.conkey) k JOIN pg_attribute a
                         ON a.attrelid = con.conrelid AND a.attnum = k)
            FROM pg_constraint con
            JOIN pg_class src ON src.oid = con.conrelid
            JOIN pg_class dst ON dst.oid = con.confrelid
            WHERE con.contype = 'f'
            """
        )).fetchall()
    tenant = set(TENANT_TABLES)
    single = sorted(f"{src}.{','.join(cols)} -> {dst} ({name})" for name, src, dst, cols in rows
                    if src in tenant and dst in tenant and "company_id" not in cols)
    assert single == [], "\n".join(single)


def test_assistant_tables_are_tenant_tables_and_its_routes_need_a_tenant_user(app):
    """The AI assistant (docs/leave_agent.md §10, §18): conversations, messages and prepared actions are RLS tenant
    tables (checked with every other one above), and every /api/v1/agent route resolves a signed-in principal."""
    from app.core.principal import get_principal
    from app.models import TENANT_TABLES

    assert {"agent_threads", "agent_messages", "agent_actions"} <= set(TENANT_TABLES)
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/v1/agent/")]
    assert len(routes) >= 7
    assert all(get_principal in _dependency_calls(r.dependant) for r in routes)
