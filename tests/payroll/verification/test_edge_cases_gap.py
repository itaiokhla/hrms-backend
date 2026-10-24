"""Edge cases that had no test naming them (Goal 2 §21, gate-ec, 24 Oct 2026): backend assertions.

Each test names the edge-case id it proves (docs/payroll/implementation/edge-cases.json) and writes its expected
values out by hand. Worlds come from the existing suites (runs_world, fs_world, the settings ``org``, the white
label helpers); nothing here calls a service to compute an expected value. The pure engine half lives in
tests/payroll_unit/test_edge_cases_gap_pure.py.

Defects found and fixed with these tests (FOLLOW-UPS F-65..):
* EC-ORG-04 — a finalized payroll v2 run did not lock the legal entity's country or base currency
  (only legacy ``payroll`` rows did): ``org_structure_service.has_finalized_payroll`` now counts v2 runs.
* EC-ORG-21 — moving a tenant to another partner or to direct left no trace in the audit row (the partner change was
  popped before logging): the platform audit now records ``partner_id`` before/after.
* EC-VAR-12 — a bonus clawback (negative input) could take net pay below the protected net silently (pure tests).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import threading
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st
from sqlalchemy import text

from tests.helpers import create_employee, signup_company
from tests.payroll.test_final_settlement import CASES, case, fs_world, item, open_case  # noqa: F401 (fixture)
from tests.payroll.test_run_finalize import (  # noqa: F401  (fixtures)
    approved_run,
    fin_world,
    finalize as finalize_run,
    outbox_count,
    run_state,
    ytd,
)
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    PAY,
    RUNS,
    current_period,
    force_status,
    new_run,
    runs_world,
)
from tests.payroll.test_settings_engine import change, org, put, session, today_of, token, values  # noqa: F401
from tests.payroll.test_wage_inputs import role, set_setting, sql, user_with_role
from tests.white_label_helpers import (
    PLATFORM_HOST,
    HostClient,
    platform_admin,
    png,
    set_plan,
    tenant_host,
    upload,
    write_dns,
)

D = Decimal
ENTITIES = "/api/v1/legal-entities"


# ---------------------------------------------------------------------------
# Multi-level company (EC-ORG-03, -04, -13, -21, -25, -26)
# ---------------------------------------------------------------------------
def test_ec_org_04_a_finalized_v2_run_locks_the_entity_country_and_currency(runs_world):
    """EC-ORG-04: once a payroll v2 run of LE01 is finalized the entity's country and base currency (redenomination
    is not supported, so the currency is locked as well) cannot change (409 ORG_COUNTRY_LOCKED / ORG_CURRENCY_LOCKED, "create a new legal
    entity"). Before that a correction is allowed; a draft or cancelled run locks nothing; another tenant's entity
    is not affected and stays invisible. Regression: only legacy ``payroll`` rows used to lock the entity."""
    w = runs_world
    acme, le01, ae = w["acme"], w["le01"], w["ae"]
    original = acme.get(f"{ENTITIES}/{le01}").json()
    assert original["has_finalized_payroll"] is False
    run = new_run(acme, w["group"], w["period"]["id"])
    # A draft run locks nothing: the country can still be corrected (and put back).
    assert acme.patch(f"{ENTITIES}/{le01}", json={"country_code": "LK"}).status_code == 200
    assert acme.patch(f"{ENTITIES}/{le01}", json={"country_code": original["country_code"]}).status_code == 200

    force_status(run["id"], "FINALIZED", finalized_at=dt.datetime.now(dt.timezone.utc))
    assert acme.get(f"{ENTITIES}/{le01}").json()["has_finalized_payroll"] is True
    r = acme.patch(f"{ENTITIES}/{le01}", json={"country_code": "LK"})
    assert r.status_code == 409 and r.json()["code"] == "ORG_COUNTRY_LOCKED"
    assert "new legal entity" in r.json()["detail"]
    r = acme.patch(f"{ENTITIES}/{le01}", json={"base_currency": "USD"})
    assert r.status_code == 409 and r.json()["code"] == "ORG_CURRENCY_LOCKED"
    after = acme.get(f"{ENTITIES}/{le01}").json()
    assert (after["country_code"], after["base_currency"]) == (original["country_code"], original["base_currency"])
    # Other fields stay editable.
    assert acme.patch(f"{ENTITIES}/{le01}", json={"trade_name": "Acme Trading"}).status_code == 200
    # Every later state keeps the lock (PAID, CLOSED ...).
    force_status(run["id"], "CLOSED")
    assert acme.patch(f"{ENTITIES}/{le01}", json={"country_code": "LK"}).status_code == 409

    # The AE entity only has a cancelled run: not locked.
    ae_run = new_run(acme, w["ae_group"], current_period(acme, w["ae_group"])["id"])
    force_status(ae_run["id"], "CANCELLED", cancelled_at=dt.datetime.now(dt.timezone.utc))
    assert acme.get(f"{ENTITIES}/{ae}").json()["has_finalized_payroll"] is False
    assert acme.patch(f"{ENTITIES}/{ae}", json={"country_code": "OM"}).status_code == 200
    assert acme.patch(f"{ENTITIES}/{ae}", json={"country_code": "AE"}).status_code == 200

    # Tenant isolation: Beta's entity is unaffected by Acme's run, and Acme's entity is invisible to Beta.
    beta = w["beta"]["client"]
    assert beta.patch(f"{ENTITIES}/{w['beta']['entity']}", json={"country_code": "LK"}).status_code == 200
    assert beta.get(f"{ENTITIES}/{le01}").status_code == 404
    assert beta.patch(f"{ENTITIES}/{le01}", json={"trade_name": "Hijack"}).status_code == 404


def test_ec_org_09_a_branch_that_moves_state_becomes_a_new_branch_from_that_date(runs_world):
    """EC-ORG-09: the state (subdivision) of a branch decides its statutory rules. Before any approved payroll it
    can be corrected; once a finalized run paid people placed there it is locked (409 ORG_BRANCH_STATE_LOCKED,
    regression: it used to be rewritten in place, so a later recalculation of an old period would have used the
    new state). The move is a new branch for the new state and a transfer from the date of the move: the old
    placement ends the day before, the new one starts that day. Other edits of the branch stay allowed."""
    w = runs_world
    acme = w["acme"]
    emma = w["emp"]["Emma"]["id"]
    assert acme.get(f"{ENTITIES}/{w['le01']}").json()["country_code"] == "IN"
    current = next(a for a in acme.get(f"/api/v1/employees/{emma}/assignments").json() if a["is_current"])
    branch_url = f"/api/v1/branches/{current['branch_id']}"
    assert acme.patch(branch_url, json={"subdivision_code": "IN-MH"}).status_code == 200  # a correction
    run = new_run(acme, w["group"], w["period"]["id"])
    assert acme.patch(branch_url, json={"subdivision_code": "IN-GJ"}).status_code == 200  # a draft run locks nothing
    assert acme.patch(branch_url, json={"subdivision_code": "IN-MH"}).status_code == 200
    force_status(run["id"], "FINALIZED", finalized_at=dt.datetime.now(dt.timezone.utc))

    r = acme.patch(branch_url, json={"subdivision_code": "IN-KA"})
    assert r.status_code == 409 and r.json()["code"] == "ORG_BRANCH_STATE_LOCKED"
    branch = next(b for b in acme.get("/api/v1/branches").json() if b["id"] == current["branch_id"])
    assert branch["subdivision_code"] == "IN-MH"
    assert acme.patch(branch_url, json={"name": "Mumbai Office", "subdivision_code": "IN-MH"}).status_code == 200

    moved = acme.post("/api/v1/branches", json={"legal_entity_id": w["le01"], "code": "BLR", "name": "Bengaluru",
                                                "subdivision_code": "IN-KA"})
    assert moved.status_code == 200, moved.text
    move_day = date.fromisoformat(w["period"]["end"]) + timedelta(days=1)
    r = acme.post(f"/api/v1/employees/{emma}/assignments", json={
        "branch_id": moved.json()["id"], "effective_from": move_day.isoformat(), "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    history = {a["branch_id"]: a for a in acme.get(f"/api/v1/employees/{emma}/assignments").json()}
    assert history[current["branch_id"]]["effective_to"] == (move_day - timedelta(days=1)).isoformat()
    assert history[moved.json()["id"]]["effective_from"] == move_day.isoformat()
    beta = w["beta"]["client"]
    assert beta.patch(branch_url, json={"subdivision_code": "IN-KA"}).status_code == 404


def test_ec_org_03_a_legal_entity_is_never_deleted_only_deactivated(app, runs_world):
    """EC-ORG-03: there is no delete for legal entities (no DELETE route, 405); an entity with people placed in it
    cannot even be deactivated (ORG_ENTITY_IN_USE), the default entity never (ORG_ENTITY_DEFAULT); an unused one is
    deactivated, stays listed with its history and can be activated again."""
    from fastapi.routing import APIRoute

    w = runs_world
    acme = w["acme"]
    deletes = [r.path for r in app.routes if isinstance(r, APIRoute) and "DELETE" in r.methods
               and r.path.startswith("/api/v1/legal-entities")]
    assert deletes == []
    r = acme.delete(f"{ENTITIES}/{w['ae']}")
    assert r.status_code == 405
    assert acme.get(f"{ENTITIES}/{w['ae']}").status_code == 200
    r = acme.post(f"{ENTITIES}/{w['ae']}/deactivate")
    assert r.status_code == 409 and r.json()["code"] == "ORG_ENTITY_IN_USE"  # Gus is placed there
    r = acme.post(f"{ENTITIES}/{w['le01']}/deactivate")
    assert r.status_code == 400 and r.json()["code"] == "ORG_ENTITY_DEFAULT"
    spare = acme.post(ENTITIES, json={"code": "SP01", "legal_name": "Spare Holdings Ltd", "country_code": "IN",
                                      "base_currency": "INR", "timezone": "Asia/Kolkata"}).json()
    assert acme.post(f"{ENTITIES}/{spare['id']}/deactivate").json()["status"] == "INACTIVE"
    listed = {e["id"]: e["status"] for e in acme.get(ENTITIES).json()}
    assert listed[spare["id"]] == "INACTIVE" and w["ae"] in listed
    assert acme.post(f"{ENTITIES}/{spare['id']}/activate").json()["status"] == "ACTIVE"
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = 'legal_entity' "
                                 "AND entity_id = :e ORDER BY id", c=w["cid"], e=str(spare["id"]))]
    assert actions.count("ENTITY_STATUS") == 2


def test_ec_org_13_an_india_scoped_processor_never_reads_uae_runs(app, runs_world):
    """EC-ORG-13: a processor whose run and report permissions are scoped to LE01 (India) lists and counts only the
    India run; every read of the UAE run — detail, population, summary, employees, variance, exceptions, the
    finalization status and the draft register export — is 404 like an unknown id, while the same reads of the India
    run pass the scope check (200, or a 409 about the run's state)."""
    w = runs_world
    acme = w["acme"]
    clerk_role = role(acme, "India run clerk", "LEGAL_ENTITY", ("payroll:run:read", "payroll:reports:read"))
    india = user_with_role(app, acme, create_employee(acme, "Indira", hire_date="2024-01-15"), clerk_role, [w["le01"]])
    in_run = new_run(acme, w["group"], w["period"]["id"])
    ae_run = new_run(acme, w["ae_group"], current_period(acme, w["ae_group"])["id"])

    listed = india.get(RUNS).json()
    assert listed["total"] == 1 and [r["id"] for r in listed["items"]] == [in_run["id"]]
    assert india.get(RUNS, params={"pay_group_id": w["ae_group"]}).json()["total"] == 0
    assert acme.get(RUNS).json()["total"] == 2
    reads = ("", "/population", "/summary", "/employees", "/variance", "/exceptions", "/finalization")
    for suffix in reads:
        r = india.get(f"{RUNS}/{ae_run['id']}{suffix}")
        assert r.status_code == 404, (suffix, r.status_code, r.text)
        r = india.get(f"{RUNS}/{in_run['id']}{suffix}")
        assert r.status_code not in (403, 404), (suffix, r.status_code, r.text)
    export = f"{PAY}/runs/{{}}/documents/register/draft"
    assert india.get(export.format(ae_run["id"])).status_code == 404
    r = india.get(export.format(in_run["id"]))
    assert r.status_code == 409 and r.json()["code"] == "RUN_NOT_CALCULATED"  # in scope: only the state refuses


def test_ec_org_26_a_placement_without_a_branch_is_rejected(runs_world):
    """EC-ORG-26: a placement change without a branch is refused by the API (the field is required: 422; an
    empty branch: ORG_BRANCH_REQUIRED) and a row without a branch is refused by the database itself; the current
    placement is unchanged."""
    w = runs_world
    acme = w["acme"]
    emma = w["emp"]["Emma"]["id"]
    before = acme.get(f"/api/v1/employees/{emma}/assignments").json()
    body = {"effective_from": "2026-12-01", "reason": "TRANSFER", "legal_entity_id": w["le01"]}
    r = acme.post(f"/api/v1/employees/{emma}/assignments", json=body)
    assert r.status_code == 422 and "branch_id" in r.text
    r = acme.post(f"/api/v1/employees/{emma}/assignments", json={**body, "branch_id": 0})
    assert r.status_code == 400 and r.json()["code"] == "ORG_BRANCH_REQUIRED"
    assert acme.get(f"/api/v1/employees/{emma}/assignments").json() == before
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        with pytest.raises(Exception) as err:
            conn.execute(text(
                "INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, is_primary, "
                "reason, effective_from, created_at, updated_at) VALUES (:c, :e, :le, NULL, false, 'CORRECTION', "
                "'2030-01-01', now(), now())"), {"c": w["cid"], "e": emma, "le": w["le01"]})
        assert "branch_id" in str(err.value)


def test_ec_org_25_reporting_tree_queries_stop_at_the_depth_limit(app):
    """EC-ORG-25: a reporting line 71 people deep (root + 70) is walked by recursive SQL bounded by
    ``hierarchy.MAX_DEPTH`` (64): the root's descendants stop at 64 levels and the deepest person's management chain
    returns 64 managers, nearest first; nothing loads the whole tree."""
    from app.core import hierarchy
    from app.database.db import SessionLocal, bind_tenant

    acme = signup_company(app, "Deep Tree Co")
    cid = acme.company_id
    root = create_employee(acme, "Root")
    columns = [c for (c,) in sql("SELECT column_name FROM information_schema.columns WHERE table_name = 'employees' "
                                 "AND table_schema = current_schema() ORDER BY ordinal_position")]
    override = {"employee_code": ":code", "email": ":email", "manager_id": ":manager", "first_name": ":first",
                "user_id": "NULL"}
    copied = [c for c in columns if c != "id"]
    select_list = ", ".join(override.get(c, c) for c in copied)
    chain = [root["id"]]
    for n in range(1, 71):
        chain.append(sql(f"INSERT INTO employees ({', '.join(copied)}) SELECT {select_list} FROM employees "
                         "WHERE id = :root RETURNING id", root=root["id"], code=f"DEEP{n:03d}",
                         email=f"deep{n}@deeptree.io", manager=chain[-1], first=f"Level{n}")[0][0])
    assert hierarchy.MAX_DEPTH == 64
    db = SessionLocal()
    bind_tenant(db, cid)
    try:
        below_root = set(db.execute(hierarchy.descendants_select(cid, root["id"])).scalars())
        assert below_root == set(chain[1:65])  # levels 1..64 only
        managers = hierarchy.management_chain(db, cid, chain[-1])
        assert managers == list(reversed(chain[6:70]))  # 64 nearest managers of level 70
        assert hierarchy.would_create_cycle(db, cid, root["id"], chain[10]) is True
    finally:
        db.close()


def test_ec_org_21_a_tenant_moved_from_its_partner_to_direct_loses_the_partner_brand_and_link(app):
    """EC-ORG-21: a tenant under the partner PayEase shows the partner's locked product name and is listed in the
    partner console. Moved to direct (partner_id 0): the brand falls back to the platform, the lock disappears,
    the partner console no longer lists it (partners hold no other grant into a tenant) and the platform audit
    records the move with partner before/after (regression: the partner change used to be dropped from the audit)."""
    from app.cli import create_platform_admin

    ops = platform_admin(app)
    acme = signup_company(app, "Acme Corp", "admin@acme.io")
    partner = ops.post("/api/v1/platform/partners", json={
        "name": "PayEase", "tenant_entitlements": {"branding.product_name": True}}).json()
    assert ops.patch(f"/api/v1/platform/companies/{acme.company_id}", json={"partner_id": partner["id"]}).status_code == 200
    r = ops.put(f"/api/v1/platform/brands/partners/{partner['id']}", json={
        "changes": {"product_name": "PayEase HR"}, "locked_fields": ["product_name"]})
    assert r.status_code == 200, r.text
    assert ops.post(f"/api/v1/platform/brands/partners/{partner['id']}/publish").status_code == 200
    create_platform_admin("admin@payease.example", "PayEase Admin", "PartnerPass!2026", partner_slug=partner["slug"])
    console = HostClient(app, PLATFORM_HOST)
    assert console.post("/api/v1/partner/auth/login", json={"email": "admin@payease.example",
                                                            "password": "PartnerPass!2026"}).status_code == 200
    brand = lambda: HostClient(app, tenant_host("acme-corp")).get("/api/v1/public/brand").json()  # noqa: E731
    assert brand()["product_name"] == "PayEase HR"
    assert [c["name"] for c in console.get("/api/v1/partner/companies").json()["items"]] == ["Acme Corp"]
    assert "product_name" in acme.get("/api/v1/branding").json()["locked_fields"]

    moved = ops.patch(f"/api/v1/platform/companies/{acme.company_id}", json={"partner_id": 0})
    assert moved.status_code == 200 and moved.json()["partner_id"] is None
    assert brand()["product_name"] == "Intertech HRMS"
    view = acme.get("/api/v1/branding").json()
    assert "product_name" not in view["locked_fields"] and not view.get("partner")
    assert console.get("/api/v1/partner/companies").json()["items"] == []
    description, details, actor = sql(
        "SELECT description, details, actor_type FROM audit_logs WHERE action = 'COMPANY_UPDATED' AND entity_id = :c "
        "ORDER BY id DESC LIMIT 1", c=str(acme.company_id))[0]
    assert actor == "platform" and "partner_id" in description
    assert details == {"partner_id": {"before": partner["id"], "after": None}}


# ---------------------------------------------------------------------------
# White label (EC-WL-05, -08, -17, -26, -28)
# ---------------------------------------------------------------------------
def _public_brand(app, host):
    r = HostClient(app, host).get("/api/v1/public/brand")
    assert r.status_code == 200, r.text
    return r.json()


def test_ec_wl_05_right_to_left_locale_publishes_dir_rtl(app):
    """EC-WL-05 (backend half): a tenant whose default language is Arabic or Hebrew is served ``dir: rtl`` with its
    locale, whatever font the brand chose; an English locale stays ``ltr``. (The Noto font and the mirrored layout
    are checked in Chrome.)"""
    acme = signup_company(app, "Acme Corp")
    host = tenant_host("acme-corp")
    assert _public_brand(app, host)["dir"] == "ltr"
    for locale, direction in (("ar-AE", "rtl"), ("he", "rtl"), ("en-IN", "ltr"), ("ur", "rtl")):
        assert acme.put("/api/v1/branding/draft", json={"changes": {"default_locale": locale,
                                                                    "font": "INTER"}}).status_code == 200
        assert acme.post("/api/v1/branding/publish", json={}).status_code == 200
        live = _public_brand(app, host)
        assert (live["default_locale"], live["dir"], live["font"]) == (locale, direction, "INTER"), locale


def test_ec_wl_08_two_admins_publish_at_once_one_wins_and_the_other_sees_the_new_version(app):
    """EC-WL-08: two publishes of the same draft at the same time: exactly one creates a new version, the other
    gets 409 (BRAND_NO_DRAFT or BRAND_DRAFT_CHANGED); the loser reads the winning version back at once (the 409
    body itself carries no version — FOLLOW-UPS F-65)."""
    acme = signup_company(app, "Acme Corp")
    acme.put("/api/v1/branding/draft", json={"changes": {"tagline": "Race"}})
    before = len(acme.get("/api/v1/branding/versions").json())
    row_version = acme.get("/api/v1/branding").json()["draft"]["row_version"]
    results = []

    def go():
        r = acme.post("/api/v1/branding/publish", json={"row_version": row_version})
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(s for s, _ in results) == [200, 409], results
    winner = next(body for s, body in results if s == 200)
    loser = next(body for s, body in results if s == 409)
    assert loser["code"] in ("BRAND_NO_DRAFT", "BRAND_DRAFT_CHANGED")
    versions = acme.get("/api/v1/branding/versions").json()
    assert len(versions) == before + 1 and versions[0]["version"] == winner["version"]
    assert _public_brand(app, tenant_host("acme-corp"))["tagline"] == "Race"


def test_ec_wl_17_broken_custom_domain_alerts_admins_then_is_disabled_after_14_days(app):
    """EC-WL-17: an active custom domain whose DNS breaks becomes DNS_BROKEN and every active administrator gets an
    in-app alert (still served meanwhile); after the 14-day grace it is DISABLED, its sessions are revoked and the
    host shows the neutral "not configured" brand."""
    from app.core.security import utcnow
    from app.models.branding import TenantDomain
    from app.services import domain_service
    from app.database.db import system_session
    from tests.test_hosts_domains import _domain_records

    acme = signup_company(app, "Acme Corp", "admin@acme.io")
    set_plan(platform_admin(app), acme.company_id, "professional")
    domain = acme.post("/api/v1/domains", json={"hostname": "people.acme.com"}).json()
    write_dns(_domain_records(domain))
    assert acme.post(f"/api/v1/domains/{domain['id']}/verify").json()["status"] == "ACTIVE"
    browser = HostClient(app, "people.acme.com")
    assert browser.login("admin@acme.io").status_code == 200
    admin_user = acme.me()["user"]["id"]
    alerts = lambda: sql("SELECT count(*) FROM notifications WHERE user_id = :u AND title = "  # noqa: E731
                         "'Custom domain needs attention'", u=admin_user)[0][0]
    assert alerts() == 0
    write_dns({})
    assert acme.post(f"/api/v1/domains/{domain['id']}/verify").json()["status"] == "DNS_BROKEN"
    assert alerts() == 1
    assert browser.get("/api/v1/auth/me").status_code == 200
    with system_session() as db:
        db.get(TenantDomain, domain["id"]).dns_broken_since = utcnow() - timedelta(days=13)
        db.commit()
    domain_service.run_checks()
    assert acme.get(f"/api/v1/domains/{domain['id']}").json()["status"] == "DNS_BROKEN"  # inside the grace
    with system_session() as db:
        db.get(TenantDomain, domain["id"]).dns_broken_since = utcnow() - timedelta(days=15)
        db.commit()
    domain_service.run_checks()
    assert acme.get(f"/api/v1/domains/{domain['id']}").json()["status"] == "DISABLED"
    assert browser.get("/api/v1/auth/me").status_code == 401
    neutral = _public_brand(app, "people.acme.com")
    assert neutral["host_kind"] == "UNKNOWN" and neutral["display_name"] == "Intertech HRMS"


def test_ec_wl_26_platform_kill_switch_disables_brand_and_domains_and_is_audited(app):
    """EC-WL-26: the platform's abuse kill switch makes the tenant fall back to the platform brand, disables its
    active custom domain (neutral page there) and writes a platform audit row with the reason; another tenant's
    brand is untouched. Restoring brings the brand back but never re-activates the domain without fresh checks."""
    acme = signup_company(app, "Acme Corp", "admin@acme.io")
    beta = signup_company(app, "Beta Industries", "admin@beta.io")
    ops = platform_admin(app)
    set_plan(ops, acme.company_id, "professional")
    from tests.test_hosts_domains import _domain_records

    for client, tagline in ((acme, "Totally a bank"), (beta, "Beta brand")):
        client.put("/api/v1/branding/draft", json={"changes": {"tagline": tagline, "primary_color": "#0F766E"}})
        assert client.post("/api/v1/branding/publish", json={}).status_code == 200
    domain = acme.post("/api/v1/domains", json={"hostname": "hr.acme.com"}).json()
    write_dns(_domain_records(domain))
    assert acme.post(f"/api/v1/domains/{domain['id']}/verify").json()["status"] == "ACTIVE"
    assert _public_brand(app, "hr.acme.com")["tagline"] == "Totally a bank"

    r = ops.post(f"/api/v1/platform/companies/{acme.company_id}/brand-kill-switch",
                 json={"disabled": True, "reason": "impersonating a bank"})
    assert r.status_code == 200, r.text
    fallback = _public_brand(app, tenant_host("acme-corp"))
    assert fallback["tokens"] is None and fallback["tagline"] != "Totally a bank"
    assert acme.get(f"/api/v1/domains/{domain['id']}").json()["status"] == "DISABLED"
    assert _public_brand(app, "hr.acme.com")["host_kind"] == "UNKNOWN"
    assert _public_brand(app, tenant_host("beta-industries"))["tagline"] == "Beta brand"
    description, actor, company = sql(
        "SELECT description, actor_type, company_id FROM audit_logs WHERE action = 'BRAND_KILL_SWITCH' "
        "AND entity_id = :c ORDER BY id", c=str(acme.company_id))[0]
    assert actor == "platform" and company is None
    assert "impersonating a bank" in description and "1 custom domain(s) disabled" in description

    assert ops.post(f"/api/v1/platform/companies/{acme.company_id}/brand-kill-switch",
                    json={"disabled": False}).status_code == 200
    assert _public_brand(app, tenant_host("acme-corp"))["tagline"] == "Totally a bank"
    assert acme.get(f"/api/v1/domains/{domain['id']}").json()["status"] == "DISABLED"


def test_ec_wl_28_brand_asset_urls_are_content_hashed(app):
    """EC-WL-28 (backend half): every stored brand image is addressed by the SHA-256 of its bytes, so a changed logo
    has a new URL (browsers cannot keep showing the old file) and the old URL keeps serving the old bytes,
    cacheable as immutable."""
    acme = signup_company(app, "Acme Corp")
    first = upload(acme, "logo_light", png(420, 120, (10, 20, 200, 255))).json()
    second = upload(acme, "logo_light", png(420, 120, (200, 20, 10, 255))).json()
    assert first["url"] != second["url"]
    for asset in (first, second):
        name = asset["url"].rsplit("/", 1)[1]
        digest, ext = name.split(".")
        assert len(digest) == 64 and ext == "png"
        assert hashlib.sha256(acme.get(asset["url"]).content).hexdigest() == digest
    host = tenant_host("acme-corp")
    for asset in (first, second):
        acme.put("/api/v1/branding/draft", json={"changes": {"logo_light": asset["id"]}})
        assert acme.post("/api/v1/branding/publish", json={}).status_code == 200
        assert _public_brand(app, host)["logos"]["light"].endswith(asset["url"].rsplit("/", 1)[1])
    old = HostClient(app, host).get(first["url"])
    assert old.status_code == 200 and "immutable" in old.headers["cache-control"]
    assert hashlib.sha256(old.content).hexdigest() == first["url"].rsplit("/", 1)[1].split(".")[0]


# ---------------------------------------------------------------------------
# Overtime required by law (EC-OT-11) and pack-owned objects (EC-STA-18)
# ---------------------------------------------------------------------------
def test_ec_ot_11_overtime_mandated_by_law_is_locked_on_for_covered_workers(app, org, monkeypatch):
    """EC-OT-11 with a test-only country-pack constraint (real packs ship as DRAFT, decision I-07): AE law requires
    overtime pay. For the AE entity the switch shows REQUIRED_BY_LAW and switching it OFF is refused
    (STATUTORY_LOCK). While the tenant's starter value OFF contradicts the law, the AE worker's resolution fails
    closed (never paid as OFF); OFF cannot be saved tenant-wide either. With the tenant ON and an India override
    OFF, the AE worker resolves ON locked by COUNTRY_PACK and the India worker (not covered) resolves OFF."""
    from app.payroll import settings_registry as reg
    from app.payroll.services import settings as engine

    packs = importlib.import_module("app.payroll.packs")

    def setting_constraints(country_code, on):
        if country_code != "AE":
            return ()
        return (reg.LegalConstraint("payroll.overtime.enabled", "Test Overtime Act s.3", required_value=True),)

    monkeypatch.setattr(packs, "setting_constraints", setting_constraints, raising=False)
    admin, cid = org["admin"], org["cid"]
    item = values(admin, scope_type="LEGAL_ENTITY", scope_id=org["le2"])["payroll.overtime.enabled"]
    assert item["badge"]["code"] == "REQUIRED_BY_LAW" and item["badge"]["source"] == "Test Overtime Act s.3"
    r = put(admin, change("payroll.overtime.enabled", False, scope_type="LEGAL_ENTITY", scope_id=org["le2"]))
    assert r.status_code == 400 and r.json()["code"] == "STATUTORY_LOCK"
    on = today_of(cid)
    db = session(cid)
    try:
        # The tenant's saved OFF contradicts the law for AE workers: resolution fails closed (never pays as OFF).
        with pytest.raises(engine.SettingConfigError) as err:
            engine.resolve(db, cid, "payroll.overtime.enabled", employee_id=org["worker"], on=on)
        assert "Test Overtime Act s.3" in err.value.message and "ON applies" in err.value.message
    finally:
        db.close()
    # A tenant-wide value reaches the AE entity too, so OFF cannot be saved there either; the tenant is set ON
    # and the India entity (no such law) keeps overtime OFF with its own override.
    key = "payroll.overtime.enabled"
    r = put(admin, change(key, False, row_version=token(admin, key)))
    assert r.status_code == 400 and r.json()["code"] == "STATUTORY_LOCK"
    assert put(admin, change(key, True, row_version=token(admin, key))).status_code == 200
    assert put(admin, change(key, False, scope_type="LEGAL_ENTITY", scope_id=org["le1"])).status_code == 200
    db = session(cid)
    try:
        covered = engine.resolve(db, cid, "payroll.overtime.enabled", employee_id=org["worker"], on=on)
        assert (covered.value, covered.locked_by) == (True, "COUNTRY_PACK")
        other = engine.resolve(db, cid, "payroll.overtime.enabled", employee_id=org["stayer"], on=on)
        assert other.value is False and other.locked_by != "COUNTRY_PACK"
    finally:
        db.close()


def test_ec_sta_18_pack_owned_components_and_tables_cannot_be_edited(app):
    """EC-STA-18: a component owned by a country pack is read-only for the tenant (new version 403, archive 403);
    a tenant cannot create a component claiming a pack owner (unknown field, 422) or reuse the pack's code (409);
    and no tenant route writes pack tables (the only pack route is a GET)."""
    from fastapi.routing import APIRoute

    acme = signup_company(app, "Pack Co")
    cid = acme.company_id
    sql("INSERT INTO pay_components (company_id, code, kind, category, owner, status, created_at, updated_at) "
        "VALUES (:c, 'IN_EPF_EE', 'DEDUCTION', 'EMPLOYEE_CONTRIBUTION', 'PACK:IN', 'ACTIVE', now(), now())", c=cid)
    component_id = sql("SELECT id FROM pay_components WHERE company_id = :c AND code = 'IN_EPF_EE'", c=cid)[0][0]
    sql("INSERT INTO pay_component_versions (company_id, component_id, version, name, calc_method, effective_from, "
        "status, created_at, updated_at) VALUES (:c, :p, 1, 'EPF', 'SYSTEM', '2020-01-01', 'ACTIVE', now(), now())",
        c=cid, p=component_id)
    components = f"{PAY}/components"
    detail = acme.get(f"{components}/IN_EPF_EE").json()
    assert detail["read_only"] is True and detail["owner"] == "PACK:IN"
    assert acme.post(f"{components}/IN_EPF_EE/versions", json={"name": "Hacked"}).status_code == 403
    assert acme.patch(f"{components}/IN_EPF_EE", json={"status": "ARCHIVED"}).status_code == 403
    body = {"code": "IN_EPF_EE", "kind": "DEDUCTION", "category": "VOLUNTARY", "name": "Mine", "calc_method": "INPUT"}
    assert acme.post(components, json={**body, "owner": "PACK:IN"}).status_code == 422
    r = acme.post(components, json=body)
    assert r.status_code == 409 and r.json()["code"] == "COMPONENT_CODE_TAKEN"
    assert sql("SELECT count(*) FROM pay_component_versions WHERE component_id = :p", p=component_id)[0][0] == 1
    pack_routes = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) and "/packs" in r.path
                   for m in r.methods}
    assert pack_routes == {("GET", f"{PAY}/packs/tag-requirements")}


# ---------------------------------------------------------------------------
# Leave on exit (EC-LV-08)
# ---------------------------------------------------------------------------
def _leave_type_with_quota(company_id):
    return sql("SELECT code, annual_quota FROM leave_types WHERE company_id = :c AND annual_quota > 0 "
               "AND NOT is_unlimited AND kind IS NULL ORDER BY code LIMIT 1", c=company_id)[0]


def test_ec_lv_08_negative_leave_balance_on_exit_follows_the_setting(fs_world):
    """EC-LV-08: Ivan leaves having taken 3 days more than his yearly quota of one leave type (balance −3). The
    settlement checklist follows ``payroll.leave.negative_balance_on_exit``:
    EXCEPTION (default) → BLOCKED for review; WRITE_OFF → NOT_APPLICABLE with the write-off note; RECOVER without a
    daily rate in the leave pay policy → BLOCKED asking for the amount (never invented), and HR confirms a negative
    recovery of 1,500.00 taken within the protected net."""
    w = fs_world
    cid, ivan, last_day = w["cid"], w["ivan"], w["last_day"]
    code, quota = _leave_type_with_quota(cid)
    sql("INSERT INTO leave_balance_transactions (company_id, employee_id, leave_type, delta, reason, idempotency_key, "
        "effective_date, created_at, updated_at) VALUES (:c, :e, :t, :d, 'USE', 'ec-lv-08', :day, now(), now())",
        c=cid, e=ivan, t=code, d=-(quota + 3), day=last_day)
    created = open_case(w["acme"], ivan, last_day)
    negative = item(case(w["acme"], created["id"]), "NEGATIVE_LEAVE_BALANCE", code)
    assert negative["status"] == "BLOCKED" and D(negative["units"]) == D(-3)
    assert negative["details"]["negative_balance_on_exit"] == "EXCEPTION"
    assert [i["code"] for i in negative["issues"]] == ["NEGATIVE_LEAVE_BALANCE_REVIEW"]
    refresh = f"{CASES}/{created['id']}/checklist/refresh"

    set_setting(cid, "payroll.leave.negative_balance_on_exit", "WRITE_OFF")
    assert w["acme"].post(refresh, json={}).status_code == 200
    negative = item(case(w["acme"], created["id"]), "NEGATIVE_LEAVE_BALANCE", code)
    assert negative["status"] == "NOT_APPLICABLE" and "Written off" in negative["note"] and negative["amount"] is None

    set_setting(cid, "payroll.leave.negative_balance_on_exit", "RECOVER")
    assert w["acme"].post(refresh, json={}).status_code == 200
    negative = item(case(w["acme"], created["id"]), "NEGATIVE_LEAVE_BALANCE", code)
    assert negative["status"] == "BLOCKED" and negative["amount"] is None
    assert [i["code"] for i in negative["issues"]] == ["RECOVERY_RATE_REQUIRED"]
    assert "protected net" in negative["note"]
    url = f"{CASES}/{created['id']}/items/{negative['id']}/confirm"
    note = "3 days at 500.00 a day, agreed in the exit interview"
    r = w["acme"].post(url, json={"amount": {"amount": "1500.00", "currency": "INR"}, "note": note})
    assert r.status_code == 400 and r.json()["code"] == "AMOUNT_POSITIVE"  # a recovery is negative
    r = w["acme"].post(url, json={"amount": {"amount": "-1500.00", "currency": "INR"}, "note": note})
    assert r.status_code == 200, r.text
    confirmed = item(case(w["acme"], created["id"]), "NEGATIVE_LEAVE_BALANCE", code)
    assert confirmed["status"] == "CONFIRMED" and confirmed["amount"]["amount"] == "-1500.00"


# ---------------------------------------------------------------------------
# Reliability (EC-REL-07, EC-REL-10)
# ---------------------------------------------------------------------------
def test_ec_rel_07_a_failure_inside_finalization_commits_nothing_and_the_retry_finalizes_once(fin_world, monkeypatch):
    """EC-REL-07: the database connection is lost after the YTD rows were written but before the statutory
    liabilities (simulated OperationalError inside the finalization transaction). Nothing of the finalization is
    kept: the run is still APPROVED, no YTD, liabilities or outbox event exist. The retry with the same
    Idempotency-Key finalizes exactly once, and a further retry replays."""
    from sqlalchemy.exc import OperationalError

    from app.payroll.services import finalization

    w = fin_world
    acme, cid = w["acme"], w["cid"]
    run = approved_run(w, kind="IN_FIXTURE")
    emma = w["emp"]["Emma"]["id"]
    real = finalization._write_liabilities

    def failover(*args, **kwargs):
        raise OperationalError("COMMIT", {}, Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(finalization, "_write_liabilities", failover)
    try:
        response = finalize_run(acme, run["id"], key="failover-1", expect=500)
        assert response.status_code == 500
    except OperationalError:
        pass  # the test client re-raises server errors: the request failed either way
    assert run_state(run["id"])["status"] == "APPROVED"
    assert ytd(cid, emma, "TOTAL:NET") is None and outbox_count(cid, run["id"]) == 0
    assert sql("SELECT count(*) FROM statutory_liabilities WHERE run_id = :r", r=run["id"])[0][0] == 0

    monkeypatch.setattr(finalization, "_write_liabilities", real)
    first = finalize_run(acme, run["id"], key="failover-1")
    assert first.json()["status"] == "FINALIZED"
    net = sql("SELECT net FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e", r=run["id"], e=emma)[0][0]
    assert ytd(cid, emma, "TOTAL:NET") == net and outbox_count(cid, run["id"]) == 1
    again = finalize_run(acme, run["id"], key="failover-1")
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json() == first.json()
    assert ytd(cid, emma, "TOTAL:NET") == net and outbox_count(cid, run["id"]) == 1


def test_ec_rel_10_server_utc_is_authoritative_and_company_dates_come_from_it(monkeypatch):
    """EC-REL-10: "today" is never the server's local date. ``clock.company_today`` converts the one authoritative
    UTC instant (``security.utcnow``, timezone-aware) into the company's timezone: at 2026-03-31 20:00 UTC it is
    already 1 April (a new Indian tax year) in Asia/Kolkata, still 31 March in UTC and in America/New_York; a
    company without a timezone uses UTC. Tax years and periods follow that stored date."""
    from app.core import clock, security
    from app.payroll import calendar as pay_calendar

    instant = dt.datetime(2026, 3, 31, 20, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(security, "utcnow", lambda: instant)
    assert clock.utcnow() is instant and clock.utcnow().utcoffset() == dt.timedelta(0)

    class FakeDb:
        def __init__(self, tz):
            self.company = SimpleNamespace(timezone=tz)

        def get(self, _model, _id):
            return self.company

    assert clock.company_today(FakeDb("Asia/Kolkata"), 1) == dt.date(2026, 4, 1)
    assert clock.company_today(FakeDb("UTC"), 1) == dt.date(2026, 3, 31)
    assert clock.company_today(FakeDb("America/New_York"), 1) == dt.date(2026, 3, 31)
    assert clock.company_today(FakeDb(None), 1) == dt.date(2026, 3, 31)
    assert pay_calendar.tax_year_label(clock.company_today(FakeDb("Asia/Kolkata"), 1), "04-01") == "2026-27"
    assert pay_calendar.tax_year_label(clock.company_today(FakeDb("UTC"), 1), "04-01") == "2025-26"


# ---------------------------------------------------------------------------
# YTD at the tax-year boundary (EC-STA-22, property)
# ---------------------------------------------------------------------------
_AMOUNT = st.decimals(min_value=D("-50000"), max_value=D("50000"), places=2, allow_nan=False, allow_infinity=False)


@hsettings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.tuples(st.sampled_from(["2026-27", "2027-28"]),
                          st.lists(st.tuples(st.sampled_from(["BASIC", "ARREARS", "BONUS"]), _AMOUNT), min_size=1,
                                   max_size=4),
                          st.booleans()),
                min_size=1, max_size=6))
def test_ec_sta_22_ytd_equals_finalized_period_totals_with_reversals_and_arrears(runs):
    """EC-STA-22 (property): each generated run is finalized into its pay period's tax year; some are then reversed
    by a REVERSAL run carrying the negated lines (as the reversal service writes them). Summing the YTD increments
    of finalization (``finalization._ytd_rows``) per tax year equals, per component and for TOTAL:NET, the sum of
    the lines of the runs still in force in that year — arrears count in the tax year of the period that pays
    them, never in the original period's year."""
    from app.payroll.services import finalization

    ytd_totals: dict = {}
    expected: dict = {}

    def finalize(run_id, tax_year, items):
        run = SimpleNamespace(company_id=1, legal_entity_id=7, id=run_id)
        period = SimpleNamespace(tax_year=tax_year)
        net = sum((a for _, a in items), D(0))
        row = SimpleNamespace(employee_id=3, currency="INR", gross=net, deductions=D(0), reimbursements=D(0),
                              net=net, employer_cost=net)
        lines = [SimpleNamespace(kind="EARNING", component_code=c, component_id=None, amount=a) for c, a in items]
        for value in finalization._ytd_rows(run, period, row, lines, {}):
            key = (value["tax_year"], value["bucket"])
            ytd_totals[key] = ytd_totals.get(key, D(0)) + value["amount"]

    for index, (tax_year, items, reversed_later) in enumerate(runs):
        finalize(index * 2 + 1, tax_year, items)
        if reversed_later:
            finalize(index * 2 + 2, tax_year, [(c, -a) for c, a in items])
            continue
        for code, amount in items:
            for bucket in (code, "TOTAL:NET"):
                expected[(tax_year, bucket)] = expected.get((tax_year, bucket), D(0)) + amount

    def nonzero(mapping):
        return {k: v for k, v in mapping.items() if v != 0 and k[1] in ("BASIC", "ARREARS", "BONUS", "TOTAL:NET")}

    assert nonzero(ytd_totals) == nonzero(expected)
    for tax_year in ("2026-27", "2027-28"):  # earnings only: gross and net agree
        assert ytd_totals.get((tax_year, "TOTAL:GROSS"), D(0)) == ytd_totals.get((tax_year, "TOTAL:NET"), D(0))
