"""Adversarial review of partner delegation (Goal 2 section 15): what a delegated, read-only partner token can
reach across the WHOLE API (including the routes added after the delegation work: nominees, legal holds, tax
profiles, unclaimed pay), not only the handful of routes the feature tests name.

The sweeps call every GET route of the application with a delegated token and check what comes back and what was
written. They are exploratory by nature: the results are also written to ``logs/review_delegation_sweep.json``.
"""

import json
import re
from pathlib import Path

from sqlalchemy import event

from tests.test_partner_delegation import (  # noqa: F401  (fixtures and helpers)
    BASE,
    Delegate,
    alpha_staff,
    grant,
    open_tenant,
    sql,
    world,
)

LOGS = Path(__file__).resolve().parent.parent / "logs"
WRITE = re.compile(r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?([a-z_]+)\"?", re.I)
HARMLESS_WRITES = {"audit_logs"}


def get_routes(app):
    from fastapi.routing import APIRoute

    seen, routes = set(), []
    for route in app.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods and route.path.startswith("/api/v1/"):
            if route.path not in seen:
                seen.add(route.path)
                routes.append(route.path)
    return sorted(routes)


def concrete(path):
    return re.sub(r"\{[^}]+\}", "1", path)


def sweep(delegate, app):
    from app.database.db import engine, system_engine

    writes, results = [], {}
    current = {"path": None}

    def spy(conn, cursor, statement, parameters, context, executemany):
        match = WRITE.match(statement)
        if match and match.group(2).lower() not in HARMLESS_WRITES:
            writes.append((current["path"], match.group(1).upper(), match.group(2)))

    for eng in {engine, system_engine}:
        event.listen(eng, "before_cursor_execute", spy)
    try:
        for template in get_routes(app):
            current["path"] = template
            r = delegate.get(concrete(template))
            results[template] = r.status_code
    finally:
        for eng in {engine, system_engine}:
            event.remove(eng, "before_cursor_execute", spy)
    return results, writes


# Routes that answer 200 to a delegate holding ONE unrelated code (holiday:read): reference data every signed-in
# person sees (catalogues, departments, shifts, leave types, branding), and per-user lists that are empty for the
# operator (it is no tenant user: no notifications, approvals, exports or downloads, no employee record). Anything
# else answering 200 here ignores the delegated codes and must be reviewed before it is added to this list.
OPEN_TO_ANY_SIGNED_IN_PRINCIPAL = {
    "/api/v1/branding/current", "/api/v1/dashboard", "/api/v1/departments", "/api/v1/face/status",
    "/api/v1/holidays", "/api/v1/holidays/locations", "/api/v1/leaves/approvals", "/api/v1/leaves/types",
    "/api/v1/notifications", "/api/v1/org/catalog", "/api/v1/payroll/approvals", "/api/v1/payroll/approvals/counts",
    "/api/v1/payroll/catalogues/capabilities", "/api/v1/payroll/catalogues/countries",
    "/api/v1/payroll/catalogues/currencies", "/api/v1/payroll/downloads/mine", "/api/v1/payroll/exports",
    "/api/v1/payroll/uploads/limits", "/api/v1/public/brand", "/api/v1/shifts",
    # LH-M1 work-calendar reads: gated by holiday:read (the delegated code; self-service company-wide like
    # /api/v1/holidays above), 04-holiday/03 §5.9, 04-holiday/01 §8.5. Reference data, no personal data.
    "/api/v1/work-weeks", "/api/v1/work-weeks/templates", "/api/v1/work-weeks/{pattern_id:int}",
    "/api/v1/work-weeks/{pattern_id:int}/history", "/api/v1/work-weeks/{pattern_id:int}/versions",
    "/api/v1/branches/{branch_id:int}/calendar-links",
    "/api/v1/holiday-calendars", "/api/v1/holiday-calendars/{calendar_id:int}",
    "/api/v1/holiday-calendars/{calendar_id:int}/lineage", "/api/v1/holiday-calendars/{calendar_id:int}/holidays",
    "/api/v1/holiday-calendars/{calendar_id:int}/exclusions", "/api/v1/holiday-calendars/{calendar_id:int}/assignments",
    "/api/v1/holiday-calendar-assignments",
    # LH-M1 Goal 3: rules, years and history of a calendar (holiday:read; 11 §6.10).
    "/api/v1/holiday-calendars/rules/{rule_id:int}", "/api/v1/holiday-calendars/{calendar_id:int}/rules",
    "/api/v1/holiday-calendars/{calendar_id:int}/years/{year:int}",
    "/api/v1/holiday-calendars/{calendar_id:int}/years/{year:int}/diff",
    "/api/v1/holiday-calendars/{calendar_id:int}/history",
}


def test_a_minimal_delegation_reaches_no_route_beyond_its_codes_and_a_delegated_read_writes_only_the_audit_trail(
        world, alpha_staff):
    """Every GET route of the application (nominees, legal holds, tax profiles, unclaimed pay included) is called
    with a delegation of the single code holiday:read: none may answer 200 except the reference-data allowlist, none
    may fail with a server error (the operator has user id 0), and none may write anything but audit rows."""
    app, alpha = world["app"], world["alpha"]
    row = grant(alpha, codes=["holiday:read"])
    session = open_tenant(world["pn"], row["id"])
    delegate = Delegate(app, "acme-corp", session["access_token"])
    results, writes = sweep(delegate, app)
    LOGS.mkdir(exist_ok=True)
    (LOGS / "review_delegation_sweep.json").write_text(json.dumps(
        {"routes": len(results), "ok": sorted(p for p, s in results.items() if s == 200),
         "writes": sorted(set(writes))}, indent=1))
    assert len(results) > 250  # the whole API, not a handful of routes
    assert sorted(p for p, s in results.items() if s == 200 and p not in OPEN_TO_ANY_SIGNED_IN_PRINCIPAL) == []
    assert sorted(p for p, s in results.items() if s >= 500) == []
    assert sorted(set(writes)) == []
    # The new payroll features answer a delegate without their codes with 403, never data.
    for path in ("/api/v1/payroll/nominees", "/api/v1/payroll/legal-holds", "/api/v1/payroll/legal-holds/summary",
                 "/api/v1/payroll/tax-profiles", "/api/v1/payroll/unclaimed-payments"):
        assert delegate.get(path).status_code == 403, path


def test_a_broad_delegation_never_writes_beyond_the_audit_trail_and_never_fails_with_a_server_error(world, alpha_staff):
    """The same sweep with every delegable code (the real data routes answer
    now): reads must not write (no hidden state change behind a GET, such as a download marking something viewed
    or a document being re-issued) and must not crash on the operator's user id 0."""
    from app.services import partner_delegation_service as service

    from app.core.permission_registry import PERMISSIONS
    from tests.helpers import create_employee, invite_and_activate

    app, alpha = world["app"], world["alpha"]
    # Rita holds every delegable code company-wide (a custom role) plus the partner-access code.
    codes = sorted(p.code for p in PERMISSIONS if service.is_delegable(p.code))
    assert len(codes) > 15
    role = alpha.post("/api/v1/roles", json={"name": "Broad reader", "grants": [
        {"permission": c, "scope": "COMPANY"} for c in [*codes, service.MANAGE_CODE]]})
    assert role.status_code == 200, role.text
    rita = invite_and_activate(app, alpha, create_employee(alpha, "Rita"), [])
    assert alpha.put(f"/api/v1/users/{rita.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    row = grant(rita, codes=codes)
    session = open_tenant(world["pn"], row["id"])
    delegate = Delegate(app, "acme-corp", session["access_token"])
    results, writes = sweep(delegate, app)
    LOGS.mkdir(exist_ok=True)
    (LOGS / "review_delegation_sweep_broad.json").write_text(json.dumps(
        {"codes": len(codes), "routes": len(results), "ok": sorted(p for p, s in results.items() if s == 200),
         "server_errors": sorted(p for p, s in results.items() if s >= 500), "writes": sorted(set(writes))}, indent=1))
    assert sorted(set(writes)) == []
    assert sorted(p for p, s in results.items() if s >= 500) == []
