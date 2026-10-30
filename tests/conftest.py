"""Test harness: real PostgreSQL with row-level security.

An embedded PostgreSQL 16 server (pgserver) is started once per run. The
baseline migration is applied as the owner role, and the application runs
as a separate NOSUPERUSER/NOBYPASSRLS role exactly like production, so the
RLS policies are genuinely exercised. Set TEST_DATABASE_ADMIN_URL to use an
existing PostgreSQL server (e.g. a Neon branch) instead.

HRMS_TEST_NO_DB=1 runs only the pure-logic suites (PURE_SUITES) without
starting PostgreSQL, so several such runs can happen at once. Database runs
share one server and database: run them one at a time (scripts/pytest_serial.py
waits for a machine-wide lock).

Tests marked ``module_dataset`` share one module-scoped dataset: the database is
emptied after the last such test of the module instead of after each test
(golden baselines seed three tenants once per module).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_ROLE = "hrms_app_test"
APP_ROLE_PASSWORD = "app-role-test-password"
DB_NAME = "hrms_test"
NO_DB = os.environ.get("HRMS_TEST_NO_DB") == "1"
PURE_SUITES = tuple(ROOT / "tests" / name for name in ("payroll_unit", "leave_unit", "attendance_unit"))
SHARED_DATASET_MARK = "module_dataset"

_server = None


def _start_postgres() -> str:
    global _server
    admin_url = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if admin_url:
        return admin_url
    import pgserver

    sys.path.insert(0, str(ROOT / "scripts"))
    from pg_tzdata import ensure_timezone_data

    ensure_timezone_data()  # pgserver ships without IANA zones; SQL AT TIME ZONE needs them (AS-M1 triggers)
    data_dir = os.path.join(tempfile.gettempdir(), "hrms_pg_tests")
    _server = pgserver.get_server(data_dir, cleanup_mode="stop")
    return _server.get_uri()


def pytest_configure(config):
    config.addinivalue_line("markers", f"{SHARED_DATASET_MARK}: keep the database between tests of the module")
    if NO_DB:
        return
    import psycopg2
    from sqlalchemy.engine import make_url

    admin_url = _start_postgres()
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (DB_NAME,))
    cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
    cur.execute(f"CREATE DATABASE {DB_NAME}")
    # Sessions in UTC whatever the cluster default (a fresh cluster takes the machine zone, e.g. +05:30; FU-AS-55).
    cur.execute(f"ALTER DATABASE {DB_NAME} SET timezone TO 'UTC'")
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
    if not cur.fetchone():
        cur.execute(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD %s NOSUPERUSER NOBYPASSRLS", (APP_ROLE_PASSWORD,))
    conn.close()

    base = make_url(admin_url)
    owner_url = base.set(database=DB_NAME).render_as_string(hide_password=False)
    app_url = base.set(database=DB_NAME, username=APP_ROLE, password=APP_ROLE_PASSWORD).render_as_string(
        hide_password=False
    )
    os.environ.update(
        {
            "ENVIRONMENT": "test",
            "SECRET_KEY": "test-secret-key-that-is-long-enough-for-hs256-signing",
            "DATABASE_URL": app_url,
            "SYSTEM_DATABASE_URL": owner_url,
            "MIGRATION_DATABASE_URL": owner_url,
            "UPLOAD_DIR": os.path.join(tempfile.gettempdir(), "hrms_test_uploads"),
            "EMAIL_OUTBOX_DIR": os.path.join(tempfile.gettempdir(), "hrms_test_outbox"),
            "PROXY_SHARED_SECRET": "test-proxy-secret-that-is-long-enough-123",
            "PLATFORM_HOSTS": "app.test.local,localhost",
            "PLATFORM_BASE_DOMAIN": "test.local",
            "CUSTOM_DOMAIN_CNAME_TARGET": "tenants.test.local",
            "DOMAIN_DNS_PROVIDER": "fixture",
            "DOMAIN_DNS_FIXTURE_FILE": os.path.join(tempfile.gettempdir(), "hrms_test_dns.json"),
            "TLS_PROVIDER": "local-dev",
            "FRONTEND_URL": "http://app.test.local:3000",
            "DB_POOL_SIZE": "5",
            "JOBS_MODE": "inline",
            # Test-only key (base64 of 32 bytes); production keys come from the secrets manager.
            "PAYROLL_MASTER_KEY": "dGVzdC1vbmx5LXBheXJvbGwtbWFzdGVyLWtleS0zMmI=",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")

    from sqlalchemy import create_engine

    from app.cli import grant_app_role

    owner = create_engine(owner_url, isolation_level="AUTOCOMMIT")
    with owner.connect() as c:
        grant_app_role(c, APP_ROLE)
    owner.dispose()


def pytest_unconfigure(config):
    if _server is not None:
        try:
            from app.database.db import engine, system_engine

            engine.dispose()
            system_engine.dispose()
        except Exception:
            pass
        _server.cleanup()


def pytest_collection_modifyitems(config, items):
    if not NO_DB:
        return
    skip = pytest.mark.skip(reason="needs the database (unset HRMS_TEST_NO_DB)")
    for item in items:
        parents = Path(str(item.fspath)).resolve().parents
        if not any(suite in parents for suite in PURE_SUITES):
            item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    # The test that really runs next (after -k / deselection), for the shared-dataset cleanup decision.
    item._hrms_next_item = nextitem
    yield


def _keeps_dataset(node) -> bool:
    nextitem = getattr(node, "_hrms_next_item", None)
    return (node.get_closest_marker(SHARED_DATASET_MARK) is not None and nextitem is not None
            and nextitem.get_closest_marker(SHARED_DATASET_MARK) is not None
            and getattr(nextitem, "module", None) is getattr(node, "module", None))


def _empty_written_tables(conn, names) -> None:
    """Delete the rows of every table a test wrote to and restart the sequences those tables own.

    Same end state as TRUNCATE ... RESTART IDENTITY CASCADE over all tables, but an untouched table costs
    one probe instead of new files for the table and each index (about 3 s per test on Windows).
    Triggers (foreign keys, append-only guards) are off for this cleanup transaction only; that needs a
    superuser, which the embedded test server provides.
    """
    from sqlalchemy import text

    conn.execute(text("SET LOCAL session_replication_role = replica"))
    probe = " UNION ALL ".join(f"SELECT '{n}' FROM (SELECT 1 FROM \"{n}\" LIMIT 1) AS x" for n in names)
    written = [row[0] for row in conn.execute(text(probe))]
    for name in written:
        conn.execute(text(f'DELETE FROM "{name}"'))
    # Every used sequence of these tables, also of a table whose rows the test already deleted itself.
    conn.execute(
        text(
            "SELECT setval(s.oid::regclass, q.seqstart, false) FROM pg_class s "
            "JOIN pg_sequence q ON q.seqrelid = s.oid "
            "JOIN pg_sequences ps ON ps.schemaname = s.relnamespace::regnamespace::text AND ps.sequencename = s.relname "
            "JOIN pg_depend d ON d.objid = s.oid AND d.classid = 'pg_class'::regclass "
            "AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i') "
            "JOIN pg_class t ON t.oid = d.refobjid "
            "WHERE s.relkind = 'S' AND t.relname = ANY(:names) AND ps.last_value IS NOT NULL"
        ),
        {"names": list(names)},
    )
    conn.execute(text("SET LOCAL session_replication_role = DEFAULT"))


_SCHEDULE_SEED = []


def _restore_scheduled_jobs(conn) -> None:
    """Re-seed the platform schedule that migration lh_m1_scheduler installs (the cleanup empties it)."""
    import importlib.util

    from sqlalchemy import text

    if not _SCHEDULE_SEED:
        path = ROOT / "alembic" / "versions" / "0039_lh_m1_scheduler.py"
        spec = importlib.util.spec_from_file_location("_lh_m1_scheduler", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        _SCHEDULE_SEED.extend(migration.SEED)
    for job_type, cadence, minute, local, enabled in _SCHEDULE_SEED:
        conn.execute(text(
            "INSERT INTO scheduled_jobs (job_type, cadence, at_minute, at_local_time, enabled, next_run_at) "
            "VALUES (:t, :c, :m, CAST(:l AS time), :e, CASE WHEN :e THEN now() END) ON CONFLICT (job_type) DO NOTHING"),
            {"t": job_type, "c": cadence, "m": minute, "l": local, "e": enabled})


@pytest.fixture(autouse=True)
def clean_database(request):
    if NO_DB:
        yield
        return
    from sqlalchemy import text

    from app.core.rate_limit import limiter
    from app.database.db import system_engine
    from app.models import Base

    from app.core.hosts import invalidate_host_cache
    from app.services.brand_service import invalidate_cache

    invalidate_host_cache()
    invalidate_cache()
    yield
    if _keeps_dataset(request.node):
        return
    names = [t.name for t in Base.metadata.sorted_tables]
    with system_engine.begin() as conn:
        try:
            with conn.begin_nested():
                _empty_written_tables(conn, names)
        except Exception:
            # Not a superuser (e.g. TEST_DATABASE_ADMIN_URL on a managed server): truncate everything.
            conn.execute(text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE"))
        # Restore the platform brand that migration 0006 installs.
        conn.execute(text(
            "INSERT INTO commercial_brand_profiles (owner_type, version, status, display_name, product_name, short_name, "
            "tagline, show_powered_by, powered_by_label, locked_fields, published_at, published_by, created_by, change_summary) "
            "VALUES ('PLATFORM', 1, 'PUBLISHED', 'Intertech HRMS', 'Intertech HRMS', 'HRMS', "
            "'People, attendance, leave and payroll in one place.', true, 'Powered by Intertech HRMS', '{}', now(), "
            "'migration 0006', 'migration 0006', '[]'::jsonb)"
        ))
        _restore_scheduled_jobs(conn)
    limiter.reset()
    invalidate_host_cache()
    invalidate_cache()


@pytest.fixture
def app():
    from app.main import app as fastapi_app

    return fastapi_app


from tests.helpers import *  # noqa: E402,F401,F403
