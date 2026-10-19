"""Test harness: real PostgreSQL with row-level security.

An embedded PostgreSQL 16 server (pgserver) is started once per run. The
baseline migration is applied as the owner role, and the application runs
as a separate NOSUPERUSER/NOBYPASSRLS role exactly like production, so the
RLS policies are genuinely exercised. Set TEST_DATABASE_ADMIN_URL to use an
existing PostgreSQL server (e.g. a Neon branch) instead.
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

_server = None


def _start_postgres() -> str:
    global _server
    admin_url = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if admin_url:
        return admin_url
    import pgserver

    data_dir = os.path.join(tempfile.gettempdir(), "hrms_pg_tests")
    _server = pgserver.get_server(data_dir, cleanup_mode="stop")
    return _server.get_uri()


def pytest_configure(config):
    import psycopg2
    from sqlalchemy.engine import make_url

    admin_url = _start_postgres()
    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (DB_NAME,))
    cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
    cur.execute(f"CREATE DATABASE {DB_NAME}")
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
            "DB_POOL_SIZE": "5",
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


@pytest.fixture(autouse=True)
def clean_database():
    from sqlalchemy import text

    from app.core.rate_limit import limiter
    from app.database.db import system_engine
    from app.models import Base

    yield
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with system_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    limiter.reset()


@pytest.fixture
def app():
    from app.main import app as fastapi_app

    return fastapi_app


from tests.helpers import *  # noqa: E402,F401,F403
