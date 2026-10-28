"""Up -> down -> up rehearsal of the program-goal revisions (AS 14 DoD AS-M0, X-M3, AS-M1; LH-M0, LH-M1).

Runs only when HRMS_MIGRATION_REHEARSAL=1 (it drops and recreates every table of the session's test database, so it
runs alone: ``HRMS_MIGRATION_REHEARSAL=1 scripts/pytest_serial.py tests/shifts/test_migration_rehearsal.py``).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
BASE = "0033_tax_profiles_unclaimed"

pytestmark = pytest.mark.skipif(os.environ.get("HRMS_MIGRATION_REHEARSAL") != "1",
                                reason="destructive rehearsal; set HRMS_MIGRATION_REHEARSAL=1 and run it alone")


def _alembic(*args: str) -> str:
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=os.environ.copy(),
                            capture_output=True, text=True)
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout + result.stderr


def _tables(url: str) -> set:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {t for (t,) in conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}
    finally:
        engine.dispose()


def test_program_revisions_go_down_to_0033_and_up_again(app):
    url = os.environ["MIGRATION_DATABASE_URL"]
    head = _tables(url)
    assert {"shift_versions", "shift_assignment_rules", "scheduled_jobs", "saved_views"} <= head
    _alembic("downgrade", BASE)
    base = _tables(url)
    assert not ({"shift_versions", "shift_assignment_rules", "scheduled_jobs", "saved_views",
                 "dashboard_preferences"} & base)
    _alembic("upgrade", "head")
    assert _tables(url) == head
    from app.cli import grant_app_role
    from app.database.db import system_engine

    owner = create_engine(url, isolation_level="AUTOCOMMIT")
    with owner.connect() as conn:
        grant_app_role(conn, make_role(os.environ["DATABASE_URL"]))
    owner.dispose()
    with system_engine.connect() as conn:
        seeded = conn.execute(text("SELECT count(*) FROM scheduled_jobs")).scalar()
    assert seeded > 0


def make_role(url: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).username
