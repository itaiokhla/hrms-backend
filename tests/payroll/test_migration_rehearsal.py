"""Payroll v2 migration rehearsal on representative legacy data (CHECKLIST #17, gate-migrations).

Runs scripts/rehearse_payroll_migrations.py against a scratch database of the test server: 0007 + legacy data,
upgrade head, downgrade 0027 + tasks + upgrade head, a refused downgrade while a data key exists, downgrade 0007,
upgrade head again. Every check of the script must pass; the script's checks are listed in its report.
"""

import importlib.util
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rehearse_payroll_migrations.py"
SCRATCH = "hrms_rehearsal_test"


def _rehearsal():
    spec = importlib.util.spec_from_file_location("rehearse_payroll_migrations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def owner_url():
    url = os.environ["MIGRATION_DATABASE_URL"]
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            if not conn.execute(text("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user")).scalar():
                pytest.skip("the test server does not allow creating a scratch database")
    except DBAPIError:
        pytest.skip("the test server is not reachable as the owner")
    finally:
        engine.dispose()
    return url


def test_ec_mig_14_rehearsal_upgrade_downgrade_upgrade_on_legacy_data_keeps_every_legacy_row(owner_url):
    """EC-MIG-01 EC-MIG-02 EC-MIG-03 EC-MIG-04 EC-MIG-05 EC-MIG-06 EC-MIG-08 EC-MIG-10 EC-MIG-14: the real
    alembic chain on a populated 0007 database. Legacy payroll (draft/approved/processed, a clipped net), issued
    payslips, salary structures, attendance, leave, employees and company_settings stay byte-identical through
    upgrade, downgrade and re-upgrade; the legacy migrations produce LEGACY_V1, permissions v2, compensation v2,
    the D-4 placement repair, leave days, encrypted TOTP secrets, the 0028 task backfill and the 0029 formula; the
    existing application role is granted every new tenant table by the migrations themselves; row-level security
    holds for the application role at head; the downgrade restores the 0007 schema exactly and is refused, all or
    nothing, while a data key protects encrypted payroll data; the second upgrade reproduces the first."""
    rehearsal = _rehearsal()
    messages = []
    try:
        report = rehearsal.rehearse(owner_url, SCRATCH, log=messages.append)
    except rehearsal.RehearsalError as error:
        failed = [c for c in getattr(error, "report", {}).get("checks", []) if not c["ok"]]
        pytest.fail(f"{error}\n" + "\n".join(f"{c['check']}: {c.get('detail')}" for c in failed))
    assert report["failed"] == 0
    names = [c["check"] for c in report["checks"]]
    # The rehearsal is not vacuous: each stage ran and the frozen legacy tables were compared at each one.
    for stage in ("head (1)", "partial down/up", "downgrade", "re-upgrade"):
        assert any(n.startswith(f"{stage}: payroll rows identical") for n in names), stage
    assert any("Refusing" in n or "refuses the downgrade" in n for n in names)
    assert report["legacy_rows"]["payroll"] == 8 and report["legacy_rows"]["salary_structure"] == 9
    assert report["v2_rows_after_upgrade"]["compensation"] == 8
    assert report["passed"] >= 150
