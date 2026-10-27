"""Marker scan (LH 14 WI-0.3; LH 07 §8.1 test 19, Gate 3; LH-SEC-20).

A marker string is written into a leave reason, approval remarks, a cancellation reason and the name of a
SENSITIVE-like leave type; after apply -> approve -> cancel it must not appear in log records, job payloads, outbox
payloads, notifications or audit descriptions / details.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from tests.attendance_helpers import owner_rows
from tests.helpers import create_employee, invite_and_activate, signup_company

MARKER = "ZQX-MARKER-7731"


def _working_day(offset_weeks: int) -> str:
    soon = (datetime.now(timezone.utc) + timedelta(days=40 + 7 * offset_weeks)).date()
    return (soon + timedelta(days=(7 - soon.weekday()) % 7)).isoformat()  # a Monday


def test_lh_sec_20_marker_never_leaves_the_request(app, caplog):
    caplog.set_level(logging.DEBUG)
    admin = signup_company(app, "Marker Co")
    boss = create_employee(admin, "Arjun", "Mehta")
    worker = create_employee(admin, "Priya", "Shah", manager_id=boss["id"])
    manager = invite_and_activate(app, admin, boss, ["manager"])
    employee = invite_and_activate(app, admin, worker, ["employee"])
    r = admin.post("/api/v1/leaves/types", json={"code": "wellbeing", "name": f"Wellbeing {MARKER}", "annual_quota": 5})
    assert r.status_code == 200, r.text

    day = _working_day(0)
    leave = employee.post("/api/v1/leaves/apply", headers={"Idempotency-Key": uuid.uuid4().hex}, json={
        "leave_type": "wellbeing", "start_date": day, "end_date": day, "reason": f"Personal {MARKER}"}).json()
    assert manager.post(f"/api/v1/leaves/{leave['id']}/approve", json={"remarks": f"OK {MARKER}"}).status_code == 200
    assert employee.post(f"/api/v1/leaves/{leave['id']}/cancel", json={"reason": f"Changed {MARKER}"}).status_code == 200

    leaks = []
    for table, columns in (("notifications", "title || ' ' || message"),
                           ("audit_logs", "description || ' ' || coalesce(details::text, '')"),
                           ("jobs", "coalesce(payload::text, '')"),
                           ("outbox_events", "coalesce(payload::text, '')")):
        for row in owner_rows(f"SELECT {columns} AS text FROM {table}"):
            if MARKER in (row["text"] or ""):
                leaks.append((table, row["text"][:120]))
    assert leaks == []
    assert all(MARKER not in record.getMessage() for record in caplog.records)
    # The reason itself is kept where it belongs (the request), so the scan proves containment, not loss.
    kept = owner_rows("SELECT reason, cancellation_reason FROM leaves WHERE id = :i", i=leave["id"])[0]
    assert MARKER in kept["reason"] and MARKER in kept["cancellation_reason"]
    assert json.dumps(kept)  # plain strings
