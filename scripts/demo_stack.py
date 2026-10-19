"""Run the complete backend locally on an embedded PostgreSQL 16 (no Neon needed).

    python scripts/demo_stack.py            # reuse the existing local database
    python scripts/demo_stack.py --fresh    # drop and recreate it first
    python scripts/demo_stack.py --fresh --data-dir .local-postgres-e2e --port 8100
                                            # an isolated second stack (e.g. for browser tests)

What it does:
  1. starts PostgreSQL from ./.local-postgres (created on first run),
  2. applies migrations as the owner role,
  3. creates/refreshes the RLS-restricted `hrms_app` role,
  4. ensures a platform operator account exists,
  5. starts uvicorn connected as `hrms_app`, so row-level security is enforced.

Backend output is mirrored to ./logs/demo-backend.log. In development mode,
password-reset codes and invitation links are written there instead of
being emailed. Press Ctrl+C to stop everything.
"""

import argparse
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path

import pgserver
import psycopg2
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / ".local-postgres"
LOG_FILE = ROOT / "logs" / "demo-backend.log"
DB_NAME = "hrms"
APP_ROLE = "hrms_app"
PLATFORM_EMAIL = "ops@hrms-platform.io"


def _stable_secret(name: str, size: int = 32) -> str:
    path = DATA_DIR / name
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(size)
    path.write_text(value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="drop and recreate the database")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=None, help="PostgreSQL data directory (default: .local-postgres)")
    args = parser.parse_args()
    global DATA_DIR, LOG_FILE
    if args.data_dir:
        DATA_DIR = (ROOT / args.data_dir).resolve()
        LOG_FILE = ROOT / "logs" / f"{DATA_DIR.name.lstrip('.')}-backend.log"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    server = pgserver.get_server(str(DATA_DIR), cleanup_mode="stop")
    base = server.get_uri()
    conn = psycopg2.connect(base)
    conn.autocommit = True
    with conn.cursor() as cur:
        # Disposable local cluster only (never real data): without fsync, a demo that was
        # killed rather than stopped restarts in seconds. With fsync on, crash recovery
        # re-syncs the data directory and stalls on Windows, because pgserver keeps its
        # log file open inside that directory.
        cur.execute("ALTER SYSTEM SET fsync = off")
        cur.execute("SELECT pg_reload_conf()")
        if args.fresh:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (DB_NAME,))
            cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {DB_NAME}")
    conn.close()

    app_password = _stable_secret(".app_password", 24)
    owner_url = make_url(base).set(database=DB_NAME).render_as_string(hide_password=False)
    app_url = make_url(base).set(database=DB_NAME, username=APP_ROLE, password=app_password).render_as_string(
        hide_password=False
    )
    env = dict(
        os.environ,
        ENVIRONMENT="development",
        SECRET_KEY=_stable_secret(".secret_key", 48),
        MIGRATION_DATABASE_URL=owner_url,
        SYSTEM_DATABASE_URL=owner_url,
        DATABASE_URL=owner_url,
        HRMS_APP_DB_PASSWORD=app_password,
        HRMS_PLATFORM_ADMIN_PASSWORD=os.environ.get("HRMS_PLATFORM_ADMIN_PASSWORD", "Platform@12345"),
        UPLOAD_DIR=str(ROOT / "uploads" / ("demo" if DATA_DIR.name == ".local-postgres" else DATA_DIR.name.lstrip("."))),
        # Never send real email from the demo stack.
        SMTP_HOST="",
        AUTH_RATE_LIMIT_SCALE=os.environ.get("AUTH_RATE_LIMIT_SCALE", "20"),
        # Accelerated test policy: lets browser tests use 1-minute tracking intervals.
        # Refused by the configuration in staging and production (minimum 15 minutes).
        LOCATION_TRACKING_MIN_INTERVAL_MINUTES=os.environ.get("LOCATION_TRACKING_MIN_INTERVAL_MINUTES", "1"),
        FRONTEND_URL=os.environ.get("FRONTEND_URL", "http://localhost:3000"),
    )
    python = sys.executable

    def run(*cmd: str) -> None:
        result = subprocess.run([python, *cmd], cwd=ROOT, env=env, capture_output=True, text=True)
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0 and "already exists" not in output:
            raise SystemExit(f"{' '.join(cmd)} failed:\n{output}")
        print(f"[demo] {' '.join(cmd[:3])}: ok")

    run("-m", "alembic", "upgrade", "head")
    run("-m", "app.cli", "setup-db-roles", "--app-role", APP_ROLE)
    run("-m", "app.cli", "create-platform-admin", "--email", PLATFORM_EMAIL, "--name", "Platform Operations")

    env["DATABASE_URL"] = app_url
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--port", str(args.port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def pump() -> None:
        for line in process.stdout:
            sys.stdout.write(line)
            log.write(line)
            log.flush()

    threading.Thread(target=pump, daemon=True).start()
    print(f"[demo] API: http://localhost:{args.port}  (log: {LOG_FILE})")
    print(f"[demo] Platform console: {PLATFORM_EMAIL} / {env['HRMS_PLATFORM_ADMIN_PASSWORD']}")
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
    finally:
        log.close()
        server.cleanup()


if __name__ == "__main__":
    main()
