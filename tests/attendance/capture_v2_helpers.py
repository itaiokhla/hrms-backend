"""Shared helpers of the AS-M2 phase-2 tests (sites, consents, capture policy, security)."""

import json
from datetime import timedelta

from sqlalchemy import text

from tests.attendance_helpers import key


def sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        return conn.execute(text(query), params)


def rows(query, **params):
    return [dict(r._mapping) for r in sql(query, **params)]


def activate_capture(admin, fields, *, criteria=None, priority=10, name="Capture test", effective_from="2026-01-01"):
    """Create, assign and activate a STANDARD ATTENDANCE_CAPTURE_POLICY version through the attendance API."""
    r = admin.post("/api/v1/attendance/policies", json={"type": "ATTENDANCE_CAPTURE_POLICY", "name": name,
                                                        "fields": fields})
    assert r.status_code == 201, r.text
    policy = r.json()
    r = admin.put(f"/api/v1/attendance/policies/{policy['id']}/assignments",
                  json={"rules": [{"criteria": criteria or {"everyone": True}, "priority": priority}]})
    assert r.status_code == 200, r.text
    r = admin.post(f"/api/v1/attendance/policies/{policy['id']}/versions/1/activate",
                   json={"effective_from": effective_from, "reason": "AS-M2 test"})
    assert r.status_code == 200, r.text
    return {**policy, "activation": r.json()}


def create_site(admin, status=201, **values):
    body = {"code": "BLR-HQ", "name": "Bengaluru Office", "kind": "OFFICE", "geometry_type": "CIRCLE",
            "center_lat": 12.9716, "center_lng": 77.5946, "radius_m": 120, "effective_from": "2026-01-01", **values}
    r = admin.post("/api/v1/attendance/sites", json=body)
    assert r.status_code == status, r.text
    return r.json()


def location(clock, lat, lng, accuracy, age_seconds=3):
    return {"status": "CAPTURED", "latitude": lat, "longitude": lng, "accuracy_m": accuracy,
            "captured_at": (clock.now - timedelta(seconds=age_seconds)).isoformat(), "attempts": 1}


def v2_punch(client, clock, direction="IN", headers=None, image=None, **extra):
    body = {"idempotency_key": key(), "client_sent_at": clock.now.isoformat(), "location_consent": True,
            "direction": direction, **extra}
    files = {"file": ("face.jpg", image, "image/jpeg")} if image else None
    return client.post("/api/v1/attendance/me/punch", data={"meta": json.dumps(body)}, files=files,
                       headers=headers or {})


def punches(employee_id):
    return rows("SELECT * FROM attendance_punches WHERE employee_id = :e ORDER BY id", e=employee_id)


def review_items(employee_id=None):
    if employee_id is None:
        return rows("SELECT * FROM attendance_review_items ORDER BY id")
    return rows("SELECT * FROM attendance_review_items WHERE employee_id = :e ORDER BY id", e=employee_id)


def step_up(client):
    user_id = getattr(client, "user_id", None) or client.me()["user"]["id"]
    sql("UPDATE user_sessions SET step_up_at = now(), step_up_method = 'EMAIL_OTP' WHERE user_id = :u", u=user_id)


def grant(company_id, role_key, code, scope="COMPANY"):
    """Add one grant to a seeded role of the tenant and refresh its holders (an explicit grant, e.g. evidence)."""
    role_id = sql("SELECT id FROM roles WHERE company_id = :c AND system_key = :k", c=company_id, k=role_key).scalar()
    sql("INSERT INTO role_permissions (company_id, role_id, permission_code, scope, created_at, updated_at) "
        "VALUES (:c, :r, :p, :s, now(), now())", c=company_id, r=role_id, p=code, s=scope)
    sql("UPDATE users SET permission_version = permission_version + 1 WHERE company_id = :c", c=company_id)
