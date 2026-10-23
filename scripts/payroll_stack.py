"""Isolated payroll E2E stack (ARCHITECTURE.md §12): PostgreSQL in .local-postgres-payroll, API on 8400.

    python scripts/payroll_stack.py start            # start in the background, wait until /health answers
    python scripts/payroll_stack.py stop             # stop the API and this stack's PostgreSQL
    python scripts/payroll_stack.py status
    python scripts/payroll_stack.py reset            # stop, delete ONLY .local-postgres-payroll (and the seed's
                                                     # credential guide of that database), migrate a new database
    python scripts/payroll_stack.py reset --start    # ... and start the stack afterwards
    python scripts/payroll_stack.py serve            # foreground (Ctrl+C stops everything)

Then seed it:  python scripts/seed_payroll_three_companies.py --api http://localhost:8400/api/v1

What it runs (never the user's demo stack: ports 8000/3000 and .local-postgres are refused):

* PostgreSQL from ``.local-postgres-payroll`` (created on first start), migrated with ``alembic upgrade head``
  as the owner, the RLS-restricted ``hrms_app`` role refreshed, a platform operator ensured;
* uvicorn on port 8400 connected as ``hrms_app`` (row-level security enforced) with ENVIRONMENT=development
  (so the IN fixture pack is allowed), JOBS_MODE=thread, SMTP off (rendered e-mails go to
  ``logs/outbox-payroll``), FRONTEND_URL http://localhost:3400;
* PAYROLL_MASTER_KEY (and the other stack values) from the git-ignored ``.local-secrets/payroll-stack.env``,
  generated on first use. Optional tenant hosts: ``start --tenant-hosts`` writes PLATFORM_BASE_DOMAIN=
  hrms.localhost and a 48-character PROXY_SHARED_SECRET into that file (the frontend on 3400 must send it).

Migrations applied before other agents finish their migrations drift: ``reset`` rebuilds the database from
scratch. Nothing here prints passwords or keys; the backend log is ``logs/payroll-stack-backend.log``.
"""

import argparse
import base64
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR_NAME = ".local-postgres-payroll"
DATA_DIR = ROOT / DATA_DIR_NAME
PORT = 8400
FRONTEND_PORT = 3400
SECRETS_DIR = ROOT / ".local-secrets"
STACK_ENV = SECRETS_DIR / "payroll-stack.env"
# The seed's credential guide describes the database: a reset removes it with the database it describes.
CREDENTIAL_FILES = (SECRETS_DIR / "payroll-credentials.json", SECRETS_DIR / "payroll-credentials.md")
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "payroll-stack-backend.log"
PID_FILE = LOG_DIR / "payroll-stack.pid"
OUTBOX_DIR = LOG_DIR / "outbox-payroll"
UPLOAD_DIR = ROOT / "uploads" / "payroll-stack"
DB_NAME = "hrms"
APP_ROLE = "hrms_app"
PLATFORM_EMAIL = "ops@hrms-platform.io"
FORBIDDEN_PORTS = (8000, 3000)
TENANT_HOST_KEYS = ("PLATFORM_HOSTS", "PLATFORM_BASE_DOMAIN", "PROXY_SHARED_SECRET")


def _guard() -> None:
    """Never touch the user's demo stack."""
    if DATA_DIR.resolve().name != DATA_DIR_NAME or DATA_DIR.resolve().parent != ROOT.resolve():
        raise SystemExit(f"Refusing to use {DATA_DIR}: only {DATA_DIR_NAME} under {ROOT} belongs to this stack.")
    if PORT in FORBIDDEN_PORTS or FRONTEND_PORT in FORBIDDEN_PORTS:
        raise SystemExit("Ports 8000/3000 belong to the user's demo stack.")


# ---------------------------------------------------------------------------- stack env (git-ignored)
def read_stack_env() -> dict:
    values = {}
    if STACK_ENV.exists():
        for line in STACK_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def ensure_stack_env(tenant_hosts: bool = False) -> dict:
    """Create .local-secrets/payroll-stack.env on first use (master key never printed)."""
    values = read_stack_env()
    changed = False
    if not values.get("PAYROLL_MASTER_KEY"):
        values["PAYROLL_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
        changed = True
    if not values.get("SECRET_KEY"):
        values["SECRET_KEY"] = secrets.token_urlsafe(48)
        changed = True
    if tenant_hosts and not values.get("PLATFORM_BASE_DOMAIN"):
        values.update(PLATFORM_HOSTS="localhost,127.0.0.1", PLATFORM_BASE_DOMAIN="hrms.localhost",
                      PROXY_SHARED_SECRET=secrets.token_urlsafe(36))
        changed = True
    if changed:
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        lines = ["# Payroll E2E stack (scripts/payroll_stack.py). Development only, git-ignored. Never commit or share."]
        lines += [f"{k}={v}" for k, v in sorted(values.items())]
        STACK_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return values


def tenant_hosts_enabled() -> bool:
    return bool(read_stack_env().get("PLATFORM_BASE_DOMAIN"))


# ---------------------------------------------------------------------------- PostgreSQL
def _server():
    import pgserver

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(str(DATA_DIR), cleanup_mode=None)


def _pg_stop() -> bool:
    """Stop this stack's PostgreSQL if it runs (pg_ctl on .local-postgres-payroll only)."""
    pid_file = DATA_DIR / "postmaster.pid"
    if not pid_file.exists():
        return False
    try:
        import psutil

        if not psutil.pid_exists(int(pid_file.read_text().splitlines()[0])):
            pid_file.unlink()  # stale: left by a killed server (no process to stop)
            return False
    except (OSError, ValueError, IndexError):
        pass
    from pgserver._commands import pg_ctl

    try:
        pg_ctl(["-w", "-m", "fast", "stop"], pgdata=DATA_DIR)
        return True
    except Exception as exc:  # noqa: BLE001 - already stopped
        print(f"[payroll-stack] PostgreSQL stop: {exc.__class__.__name__}")
        return False


def _database(server, fresh: bool = False) -> str:
    import psycopg2

    base = server.get_uri()
    conn = psycopg2.connect(base)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("ALTER SYSTEM SET fsync = off")  # disposable local cluster (as demo_stack.py)
        cur.execute("SELECT pg_reload_conf()")
        if fresh:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (DB_NAME,))
            cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if not cur.fetchone():
            cur.execute(f"CREATE DATABASE {DB_NAME}")
    conn.close()
    return base


def _stable_secret(name: str, size: int) -> str:
    path = DATA_DIR / name
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(size)
    path.write_text(value)
    return value


def build_env(server) -> dict:
    from sqlalchemy.engine import make_url

    stack = ensure_stack_env()
    base = server.get_uri()
    app_password = _stable_secret(".app_password", 24)
    owner_url = make_url(base).set(database=DB_NAME).render_as_string(hide_password=False)
    app_url = make_url(base).set(database=DB_NAME, username=APP_ROLE, password=app_password).render_as_string(
        hide_password=False)
    env = dict(os.environ)
    env.update(
        ENVIRONMENT="development",
        JOBS_MODE="thread",
        SECRET_KEY=stack["SECRET_KEY"],
        PAYROLL_MASTER_KEY=stack["PAYROLL_MASTER_KEY"],
        MIGRATION_DATABASE_URL=owner_url,
        SYSTEM_DATABASE_URL=owner_url,
        DATABASE_URL=owner_url,
        HRMS_APP_DB_PASSWORD=app_password,
        HRMS_PLATFORM_ADMIN_PASSWORD=os.environ.get("HRMS_PLATFORM_ADMIN_PASSWORD", "Platform@12345"),
        UPLOAD_DIR=str(UPLOAD_DIR),
        EMAIL_OUTBOX_DIR=str(OUTBOX_DIR),
        SMTP_HOST="",
        AUTH_RATE_LIMIT_SCALE=os.environ.get("AUTH_RATE_LIMIT_SCALE", "50"),
        FRONTEND_URL=f"http://localhost:{FRONTEND_PORT}",
        ALLOWED_ORIGINS=f"http://localhost:{FRONTEND_PORT},http://127.0.0.1:{FRONTEND_PORT}",
    )
    for key in TENANT_HOST_KEYS:
        if stack.get(key):
            env[key] = stack[key]
    env["_OWNER_URL"], env["_APP_URL"] = owner_url, app_url
    return env


def migrate(env: dict) -> None:
    python = sys.executable

    def run(*cmd: str) -> None:
        result = subprocess.run([python, *cmd], cwd=ROOT, env=env, capture_output=True, text=True)
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0 and "already exists" not in output:
            tail = "\n".join(output.splitlines()[-25:])
            raise SystemExit(f"{' '.join(cmd[:4])} failed:\n{tail}")
        print(f"[payroll-stack] {' '.join(cmd[:3])}: ok", flush=True)

    run("-m", "alembic", "upgrade", "head")
    run("-m", "app.cli", "setup-db-roles", "--app-role", APP_ROLE)
    run("-m", "app.cli", "create-platform-admin", "--email", PLATFORM_EMAIL, "--name", "Platform Operations")


# ---------------------------------------------------------------------------- API process
def _health(timeout: float = 2.0) -> bool:
    import httpx

    try:
        return httpx.get(f"http://localhost:{PORT}/health", timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    import psutil

    return psutil.pid_exists(pid)


def _kill_tree(pid: int) -> None:
    import psutil

    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for proc in children + [parent]:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children + [parent], timeout=10)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def serve() -> None:
    _guard()
    server = _server()
    _database(server)
    env = build_env(server)
    migrate(env)
    env["DATABASE_URL"] = env.pop("_APP_URL")
    env.pop("_OWNER_URL", None)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )

    def pump() -> None:
        for line in process.stdout:
            log.write(line)
            log.flush()

    threading.Thread(target=pump, daemon=True).start()
    print(f"[payroll-stack] API http://localhost:{PORT} (log {LOG_FILE})", flush=True)
    stop = threading.Event()

    def _terminate(*_):
        stop.set()

    signal.signal(signal.SIGTERM, _terminate)
    try:
        signal.signal(signal.SIGBREAK, _terminate)  # Windows console break
    except AttributeError:
        pass
    try:
        while not stop.is_set() and process.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            _kill_tree(process.pid)
        log.close()
        _pg_stop()


def start(tenant_hosts: bool, wait: int) -> None:
    _guard()
    ensure_stack_env(tenant_hosts)
    if _health():
        print(f"[payroll-stack] already running on {PORT}")
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    out = (LOG_DIR / "payroll-stack-supervisor.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"], cwd=ROOT, stdout=out,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
                            start_new_session=os.name != "nt")
    PID_FILE.write_text(str(proc.pid))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _health():
            print(f"[payroll-stack] running: API http://localhost:{PORT} (supervisor pid {proc.pid})")
            return
        if proc.poll() is not None:
            break
        time.sleep(2)
    tail = (LOG_DIR / "payroll-stack-supervisor.log").read_text(encoding="utf-8", errors="replace")[-3000:]
    raise SystemExit(f"[payroll-stack] the stack did not become healthy:\n{tail}")


def stop() -> None:
    _guard()
    pid = _pid()
    if _alive(pid):
        _kill_tree(pid)
    PID_FILE.unlink(missing_ok=True)
    # uvicorn started by an interrupted supervisor: find it by its port (8400 only).
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == PORT and conn.status == psutil.CONN_LISTEN and conn.pid:
                _kill_tree(conn.pid)
    except Exception:  # noqa: BLE001 - access denied on some systems; the pid file covers the normal case
        pass
    stopped = _pg_stop()
    print(f"[payroll-stack] stopped (PostgreSQL {'stopped' if stopped else 'was not running'})")


def reset(start_after: bool, tenant_hosts: bool, wait: int) -> None:
    """Stop, remove ONLY .local-postgres-payroll, create and migrate a new database."""
    _guard()
    stop()
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
        print(f"[payroll-stack] removed {DATA_DIR_NAME}")
    for path in CREDENTIAL_FILES:
        if path.exists():
            path.unlink()
            print(f"[payroll-stack] removed {path.name} (accounts of the old database)")
    server = _server()
    try:
        _database(server, fresh=True)
        migrate(build_env(server))
    finally:
        _pg_stop()
    if start_after:
        start(tenant_hosts, wait)


def status() -> None:
    _guard()
    pg = (DATA_DIR / "postmaster.pid").exists()
    print(f"[payroll-stack] API {'up' if _health() else 'down'} on {PORT}; PostgreSQL "
          f"{'running' if pg else 'stopped'} ({DATA_DIR_NAME}); tenant hosts "
          f"{'on' if tenant_hosts_enabled() else 'off'}; stack env {'present' if STACK_ENV.exists() else 'absent'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("start", "stop", "status", "reset", "serve"))
    parser.add_argument("--start", action="store_true", help="reset: start the stack afterwards")
    parser.add_argument("--tenant-hosts", action="store_true", help="enable <slug>.hrms.localhost tenant hosts")
    parser.add_argument("--wait", type=int, default=240, help="seconds to wait for /health")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    elif args.command == "start":
        start(args.tenant_hosts, args.wait)
    elif args.command == "stop":
        stop()
    elif args.command == "status":
        status()
    else:
        reset(args.start, args.tenant_hosts, args.wait)


if __name__ == "__main__":
    main()
