"""Run a throwaway local PostgreSQL 16 for development without Neon.

    python scripts/local_postgres.py

Prints the owner URL and keeps the server running until Ctrl+C. Data lives
in ./.local-postgres. Use the printed URL as MIGRATION_DATABASE_URL and
SYSTEM_DATABASE_URL, then create the app role with `python -m app.cli
setup-db-roles` and use that role in DATABASE_URL.
"""

import signal
import sys
import time
from pathlib import Path

import psycopg2
import pgserver

DATA_DIR = Path(__file__).resolve().parents[1] / ".local-postgres"


def main() -> None:
    server = pgserver.get_server(str(DATA_DIR), cleanup_mode="stop")
    base = server.get_uri()
    conn = psycopg2.connect(base)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = 'hrms'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE hrms")
    conn.close()
    print("Local PostgreSQL is running.")
    print(f"Owner URL: {base.rsplit('/', 1)[0]}/hrms")
    print("Press Ctrl+C to stop.")
    signal.signal(signal.SIGINT, lambda *_: (server.cleanup(), sys.exit(0)))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
