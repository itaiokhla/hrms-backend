"""Read-only database assertions for the browser tests (local demo cluster only).

    python scripts/e2e_db.py <query> [name=value ...]

Prints the rows of one whitelisted, parameterised SELECT as JSON. It connects
as the owner of the local embedded PostgreSQL started by demo_stack.py, so
the tests can check what was really stored (exact coordinates, server times,
tenant ownership, de-duplication) independently of the API under test.
"""

import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from pgserver.utils import PostmasterInfo

DATA_DIR = Path(__file__).resolve().parents[1] / os.environ.get("E2E_PG_DATA", ".local-postgres")

QUERIES = {
    "session": """SELECT id, company_id, employee_id, date, check_in_time, check_out_time, closed_reason, tracking_state,
                         tracking_stop_reason, tracking_interval_minutes, tracking_next_due_at, tracking_overdue_since
                  FROM attendance WHERE id = %(id)s""",
    "events": """SELECT event_type, occurred_at, source, location_status, idempotency_key, company_id, employee_id, note
                 FROM attendance_events WHERE attendance_id = %(id)s ORDER BY id""",
    "samples": """SELECT id, sample_type, status, latitude, longitude, accuracy_m, captured_at, received_at, slot_due_at,
                         is_recovery, accuracy_class, movement_status, distance_from_punch_in_m, exception_reason,
                         review_status, company_id, employee_id
                  FROM location_samples WHERE attendance_id = %(id)s ORDER BY id""",
    "breaks": """SELECT started_at, ended_at, is_paid, end_reason FROM attendance_breaks
                 WHERE attendance_id = %(id)s ORDER BY started_at""",
    "company_samples": """SELECT count(*) AS samples, count(*) FILTER (WHERE latitude IS NOT NULL) AS with_coordinates
                          FROM location_samples WHERE company_id = %(company_id)s""",
    "audit": """SELECT module, action, actor_email, description FROM audit_logs
                WHERE company_id = %(company_id)s AND module = %(module)s ORDER BY id""",
    "company_sample_ids": """SELECT id FROM location_samples WHERE company_id = %(company_id)s ORDER BY id LIMIT 5""",
    "fabricated": """SELECT count(*) AS n FROM location_samples
                     WHERE status <> 'CAPTURED' AND (latitude IS NOT NULL OR longitude IS NOT NULL)""",
}


def main(argv) -> None:
    if not argv or argv[0] not in QUERIES:
        raise SystemExit(f"usage: e2e_db.py <{'|'.join(QUERIES)}> [name=value ...]")
    params = dict(arg.split("=", 1) for arg in argv[1:])
    info = PostmasterInfo.read_from_pgdata(DATA_DIR)
    if info is None or not info.is_running():
        raise SystemExit("The local demo database is not running (python scripts/demo_stack.py).")
    with psycopg2.connect(info.get_uri(database="hrms")) as conn:
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(QUERIES[argv[0]], params)
            print(json.dumps(cur.fetchall(), default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)))


if __name__ == "__main__":
    main(sys.argv[1:])
