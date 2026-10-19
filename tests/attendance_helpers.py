"""Helpers for attendance, lunch and location tests.

The fake clock replaces only app.core.clock.utcnow (attendance time-keeping),
so sessions, tokens and audit timestamps keep using the real clock.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
import pytz

from tests.helpers import create_employee, invite_and_activate

IST = pytz.timezone("Asia/Kolkata")
BLR = (12.9716, 77.5946)  # synthetic office location used throughout the tests
PATHS = {"PUNCH_IN": "check-in", "PUNCH_OUT": "check-out", "LUNCH_IN": "lunch-in", "LUNCH_OUT": "lunch-out"}


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def at_ist(self, day: str, hhmm: str, second: int = 0) -> datetime:
        y, m, d = (int(x) for x in day.split("-"))
        h, mi = (int(x) for x in hhmm.split(":"))
        self.now = IST.localize(datetime(y, m, d, h, mi, second)).astimezone(timezone.utc)
        return self.now

    def advance(self, **delta) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


@pytest.fixture
def fake_clock(monkeypatch):
    from app.core import clock

    fake = FakeClock(IST.localize(datetime(2026, 10, 19, 9, 0)).astimezone(timezone.utc))
    monkeypatch.setattr(clock, "utcnow", lambda: fake.now)
    return fake


def key() -> str:
    return uuid.uuid4().hex


def fix(clock: FakeClock, lat: float = BLR[0], lng: float = BLR[1], accuracy: float = 15.0, age_seconds: int = 3) -> dict:
    return {"status": "CAPTURED", "latitude": lat, "longitude": lng, "accuracy_m": accuracy,
            "captured_at": (clock.now - timedelta(seconds=age_seconds)).isoformat(), "attempts": 1}


def failure(status: str = "PERMISSION_DENIED", message: str = "User denied Geolocation") -> dict:
    return {"status": status, "error_message": message, "attempts": 1}


def meta(clock: FakeClock, location: Optional[dict] = None, **extra) -> dict:
    body = {"idempotency_key": key(), "client_sent_at": clock.now.isoformat(), "location_consent": True}
    if location is not None:
        body["location"] = location
    body.update(extra)
    return body


def act(client, action: str, body: Optional[dict] = None, image: Optional[bytes] = None):
    data = {"meta": json.dumps(body)} if body is not None else None
    files = {"file": ("face.jpg", image, "image/jpeg")} if image else None
    return client.post(f"/api/v1/attendance/me/{PATHS[action]}", data=data, files=files)


def sample(client, clock: FakeClock, attendance_id: int, location: dict, idem: Optional[str] = None):
    return client.post("/api/v1/attendance/me/location-samples", json={
        "idempotency_key": idem or key(), "attendance_id": attendance_id, "location": location,
        "client_sent_at": clock.now.isoformat(),
    })


def set_policy(admin, **values):
    r = admin.patch("/api/v1/attendance/policy", json=values)
    assert r.status_code == 200, r.text
    return r.json()


def employee_with_login(app, admin, first: str, roles=("employee",), **extra):
    from app.core.rate_limit import limiter

    limiter.reset()  # fixtures sign many accounts in from one test client address
    employee = create_employee(admin, first, **extra)
    return invite_and_activate(app, admin, employee, list(roles))


def today(client) -> dict:
    r = client.get("/api/v1/attendance/me/today")
    assert r.status_code == 200, r.text
    return r.json()


def tracking(client) -> dict:
    r = client.get("/api/v1/attendance/me/tracking")
    assert r.status_code == 200, r.text
    return r.json()


def owner_rows(sql: str, **params):
    """Read rows directly from the database as the owner (assertions only)."""
    from sqlalchemy import text

    from app.database.db import system_engine

    with system_engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]
