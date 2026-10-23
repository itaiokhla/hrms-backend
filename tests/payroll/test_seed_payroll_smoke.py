"""The three-company payroll seed (scripts/seed_payroll_three_companies.py, Goal 2 §4-§5) on a fresh test
database, through the real API (the app client), twice.

The first pass must build the whole dataset without a refused or missing step: three companies with their
organisation, roles and scopes, compensation, approved inputs, banking, the I-15 attestation, and regular runs
taken through calculate -> approve by the independent approver -> finalize, plus an open draft run. The second
pass must change nothing: no step reports a creation and no domain table gains or loses a row. The read-only
checks of scripts/verify_payroll_seed.py then hold, including row-level isolation through the application role.

The application's start-up registers the country packs' employer choices (``packs.register_settings``); the
in-process test client does not run start-up events, so the fixture does the same and removes them afterwards.
"""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import seed_payroll_three_companies as seed  # noqa: E402
import verify_payroll_seed as verify  # noqa: E402

# Domain tables the second pass must leave exactly as they are (sessions, audit and idempotency records
# legitimately grow with every login and request).
PLATFORM_PASSWORD = "Platf0rm-Seed-Test-Only!"  # test database only
DOMAIN_TABLES = (
    "companies", "legal_entities", "branches", "departments", "pay_groups", "pay_calendars", "pay_periods",
    "work_week_patterns", "employees", "employee_assignments", "users", "roles", "role_permissions",
    "user_role_assignments", "user_role_assignment_targets", "brand_profiles", "holidays", "setting_values",
    "policies", "policy_versions", "policy_assignments", "pay_components", "pay_component_versions",
    "salary_templates", "salary_template_versions", "compensation_assignments", "leaves", "attendance",
    "overtime_entries", "wage_inputs", "tasks", "task_rate_cards", "reimbursement_claims", "loans",
    "bank_accounts", "bank_account_change_requests", "bank_verifications", "funding_accounts",
    "statutory_attestations", "payroll_runs", "payroll_run_employees", "payroll_lines", "payroll_approvals",
    "payment_obligations", "documents", "arrears_proposals",
)


@pytest.fixture
def pack_settings():
    from app.payroll import packs
    from app.payroll.settings_registry import unregister_pack_settings

    added = packs.register_settings(environment="test")
    yield
    for country in sorted({key.split(".")[2] for key in added}):
        unregister_pack_settings(country)


def _counts():
    from app.database.db import system_engine

    with system_engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in DOMAIN_TABLES}


def _seed(app, store):
    from app.core.rate_limit import limiter

    created = []

    def log(line: str) -> None:
        if line.startswith("[payroll-seed]   +"):
            created.append(line)

    from tests.white_label_helpers import PLATFORM_HOST, PROXY_KEY

    # The platform operator sets each company's subscription plan (white-label branding needs it), as on the
    # stack; its console answers on the platform host only (trusted proxy headers, as the frontend sends).
    ctx = seed.run_seed(lambda: TestClient(app, base_url="http://testserver/api/v1/"), store=store,
                        outbox_dir=Path(os.environ["EMAIL_OUTBOX_DIR"]), platform_password=PLATFORM_PASSWORD,
                        before_login=limiter.reset, log=log,
                        make_platform_client=lambda: TestClient(app, base_url="http://testserver/api/v1/", headers={
                            "X-HRMS-Host": PLATFORM_HOST, "X-HRMS-Proxy-Key": PROXY_KEY}))
    return ctx, created


def test_seed_builds_the_three_company_dataset_and_a_second_run_changes_nothing(app, pack_settings):
    from app.database.db import engine, system_engine

    from app.cli import create_platform_admin

    create_platform_admin(seed.PLATFORM_EMAIL, "Platform Operations", PLATFORM_PASSWORD)
    store: dict = {}
    first, created = _seed(app, store)
    assert first.blocked == [], first.blocked
    assert first.stats["created"] == len(created) and len(created) > 300
    for key, spec in seed.COMPANIES.items():
        company = store["companies"][key]
        # Every account has its own random password (never one shared password).
        passwords = [a["password"] for a in company["accounts"].values()]
        assert len(set(passwords)) == len(passwords) and all(len(p) >= 20 for p in passwords)
        assert set(spec["accounts"].values()) <= set(company["accounts"])
        runs = company["runs"]
        assert runs["finalized"]["status"] in verify.FINALIZED and runs["draft"]["status"] == "DRAFT"
        if spec.get("previous_run"):
            assert runs["previous"]["status"] in verify.FINALIZED
    before = _counts()

    second, created_again = _seed(app, store)
    assert created_again == [] and second.stats["created"] == 0
    assert second.blocked == []
    assert _counts() == before

    with system_engine.connect() as conn:
        results = verify.run_checks(conn, engine.connect)
    failed = [(r.company, r.name, r.detail) for r in results if not r.ok]
    assert failed == []
    assert len(results) > 250
