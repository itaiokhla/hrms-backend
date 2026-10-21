"""Overtime entries: filing, limits, approval through the engine, versions, comp-off, attendance proposals and
payroll reads (3.4 §9, 3.1 §5.5, 3.9 §3.5/§4; audit 03 §6).

EC-OT-02, EC-OT-03, EC-OT-07, EC-OT-09, EC-OT-10, EC-SEC-05.
"""

from contextlib import contextmanager
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.payroll import policy_types as pt
from app.payroll.services import overtime as ot
from tests.helpers import create_employee, invite_and_activate, signup_company

BASE = "/api/v1/overtime"
POLICIES = "/api/v1/payroll/policies"
OT_FIELDS = {
    "buckets": [
        {"day_type": "WORKDAY", "after_hours": "0", "multiplier": "1.5"},
        {"day_type": "WEEKLY_OFF", "after_hours": "0", "multiplier": "2"},
        {"day_type": "HOLIDAY", "after_hours": "0", "multiplier": "2"},
    ],
    "daily_threshold": "8",
    "min_minutes": 30,
    "rounding": {"increment_minutes": 15, "mode": "DOWN"},
    "comp_off_allowed": True,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


@contextmanager
def session(company_id):
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        yield db
    finally:
        db.close()


def today_of(company_id):
    from app.core.clock import company_today

    with session(company_id) as db:
        return company_today(db, company_id)


def tz_of(company_id):
    import pytz

    zone = sql("SELECT timezone FROM companies WHERE id = :c", c=company_id)[0][0]
    return pytz.timezone(zone or "UTC")


def at(company_id, day, hour, minute=0):
    return tz_of(company_id).localize(datetime.combine(day, time(hour, minute))).isoformat()


def file(client, **body):
    body.setdefault("reason", "Release night")
    return client.post(f"{BASE}/entries", json=body)


def filed(client, **body):
    r = file(client, **body)
    assert r.status_code == 201, r.text
    return r.json()


def entry(client, entry_id):
    r = client.get(f"{BASE}/entries/{entry_id}")
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, entry_id, **body):
    return client.post(f"{BASE}/entries/{entry_id}/approve", json=body or None)


def approved_rows(company_id, employee_id, start, end):
    with session(company_id) as db:
        return ot.approved_overtime(db, company_id, employee_id, start, end)


def audit_actions(company_id, entry_id):
    return [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = 'overtime_entry' "
                              "AND entity_id = :e ORDER BY id", c=company_id, e=str(entry_id))]


def changes(company_id, entry_id):
    """source_change_log rows of an entry (3.4 §16): (change, employee_id, date_from)."""
    return sql("SELECT change, employee_id, date_from FROM source_change_log WHERE company_id = :c "
               "AND record_type = 'overtime' AND record_id = :e ORDER BY id", c=company_id, e=entry_id)


def ot_policy(client, **fields):
    """Activate the starter overtime policy with OT_FIELDS (+ overrides) for everyone from 2024-01-01."""
    policy = next(p for p in client.get(POLICIES, params={"type": "OVERTIME_POLICY"}).json())
    pid = policy["id"]
    r = client.put(f"{POLICIES}/{pid}/assignments",
                   json={"rules": [{"criteria": {"everyone": True}, "priority": 0, "effective_from": "2024-01-01"}]})
    assert r.status_code == 200, r.text
    if policy["active_version"] is None:
        r = client.put(f"{POLICIES}/{pid}/versions/1", json={"fields": {**OT_FIELDS, **fields}})
        assert r.status_code == 200, r.text
        version = 1
    else:
        r = client.post(f"{POLICIES}/{pid}/versions", json={"fields": {**OT_FIELDS, **fields}})
        assert r.status_code == 201, r.text
        version = r.json()["version"]
    r = client.post(f"{POLICIES}/{pid}/versions/{version}/activate",
                    json={"effective_from": "2024-01-01", "reason": "Overtime test policy"})
    assert r.status_code == 200, r.text


def worked(company_id, employee_id, day, start_hour, end_hour):
    tz = tz_of(company_id)
    sql("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, is_manual, source, "
        "status, created_at, updated_at) VALUES (:c, :e, :i, :o, :d, true, 'manual', 'present', now(), now())",
        c=company_id, e=employee_id, d=day,
        i=tz.localize(datetime.combine(day, time(start_hour))), o=tz.localize(datetime.combine(day, time(end_hour))))


@pytest.fixture
def world(app):
    """Acme: founder admin; manager Mona -> Walt and Wendy; Lou outside Mona's team. Beta: Bob."""
    admin = signup_company(app, "Acme Corp")
    beta = signup_company(app, "Beta Industries")
    mona = create_employee(admin, "Mona")
    walt = create_employee(admin, "Walt", manager_id=mona["id"])
    wendy = create_employee(admin, "Wendy", manager_id=mona["id"])
    lou = create_employee(admin, "Lou")
    bob = create_employee(beta, "Bob")
    users = {
        "admin": admin,
        "beta": beta,
        "mona": invite_and_activate(app, admin, mona, ["manager"]),
        "walt": invite_and_activate(app, admin, walt, ["employee"]),
        "wendy": invite_and_activate(app, admin, wendy, ["employee"]),
        "lou": invite_and_activate(app, admin, lou, ["employee"]),
        "bob": invite_and_activate(app, beta, bob, ["employee"]),
    }
    cid = admin.company_id
    return {"cid": cid, "today": today_of(cid), "u": users,
            "emp": {"mona": mona, "walt": walt, "wendy": wendy, "lou": lou, "bob": bob}}


# ---------------------------------------------------------------------------
# Filing and approval
# ---------------------------------------------------------------------------
def test_ec_ot_02_employee_files_overtime_manager_approves_and_only_approved_is_read(world):
    u, cid, today = world["u"], world["cid"], world["today"]
    walt_id = world["emp"]["walt"]["id"]
    day = today - timedelta(days=1)
    created = filed(u["walt"], date=day.isoformat(), minutes=90)
    assert created["status"] == "PENDING_APPROVAL" and created["approval_request_id"]
    detail = entry(u["walt"], created["id"])
    assert detail["employee_id"] == walt_id and detail["pre_approval"] is False
    assert detail["source"] == "APPROVED_REQUESTS" and detail["approval"]["status"] == "PENDING"
    assert approved_rows(cid, walt_id, day, day) == []  # pending is never paid (EC-OT-02)

    awaiting = u["mona"].get(f"{BASE}/entries", params={"awaiting": True}).json()
    assert [i["id"] for i in awaiting["items"]] == [created["id"]] and awaiting["total"] == 1
    r = approve(u["mona"], created["id"], remarks="ok")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"
    assert entry(u["walt"], created["id"])["minutes_approved"] == 90

    rows = approved_rows(cid, walt_id, day, day)
    assert [(r["id"], r["minutes"], r["origin"], r["status"]) for r in rows] == [(created["id"], 90, "REQUEST",
                                                                                  "APPROVED")]
    assert len(rows[0]["content_hash"]) == 64 and rows[0]["date"] == day.isoformat()
    assert {"OVERTIME_REQUESTED", "OVERTIME_APPROVED"} <= set(audit_actions(cid, created["id"]))
    assert [tuple(c) for c in changes(cid, created["id"])] == [("UPDATED", walt_id, day)]  # draft runs see it


def test_future_date_is_a_pre_approval_and_reject_needs_a_reason(world):
    u, cid, today = world["u"], world["cid"], world["today"]
    created = filed(u["walt"], date=(today + timedelta(days=3)).isoformat(), minutes=120)
    assert entry(u["walt"], created["id"])["pre_approval"] is True
    assert u["mona"].post(f"{BASE}/entries/{created['id']}/reject", json={}).status_code == 422
    assert u["mona"].post(f"{BASE}/entries/{created['id']}/reject", json={"remarks": "  "}).status_code == 422
    r = u["mona"].post(f"{BASE}/entries/{created['id']}/reject", json={"remarks": "Not needed"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    walt_id = world["emp"]["walt"]["id"]
    assert approved_rows(cid, walt_id, today, today + timedelta(days=10)) == []  # EC-OT-02
    assert approve(u["mona"], created["id"]).json()["code"] == "OVERTIME_NOT_PENDING"
    assert "OVERTIME_REJECTED" in audit_actions(cid, created["id"])


def test_ec_ot_10_negative_too_long_and_malformed_minutes_are_rejected(world):
    walt, day = world["u"]["walt"], (world["today"] - timedelta(days=1)).isoformat()
    for minutes in (-1, 1441, "90", 90.5, True, None):
        r = file(walt, date=day, minutes=minutes)
        assert r.status_code == 422, (minutes, r.text)
    assert file(walt, date=day, minutes=60, status="APPROVED").status_code == 422  # server-owned field
    assert file(walt, date=day, minutes=60, reason=" ").status_code == 422
    far = (world["today"] - timedelta(days=500)).isoformat()
    assert file(walt, date=far, minutes=60).json()["code"] == "DATE_OUT_OF_RANGE"
    start = datetime.combine(world["today"] - timedelta(days=1), time(18)).isoformat()  # naive
    r = file(walt, date=day, minutes=60, start_at=start, end_at=start.replace("18:", "19:"))
    assert r.status_code == 400 and r.json()["code"] == "TIMEZONE_REQUIRED"
    r = file(walt, date=day, minutes=60, start_at=at(world["cid"], world["today"] - timedelta(days=1), 18))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_INPUT"


def test_ec_ot_10_overlapping_spans_and_more_than_24_hours_a_day_are_rejected(world):
    cid, walt = world["cid"], world["u"]["walt"]
    day = world["today"] - timedelta(days=2)
    first = filed(walt, date=day.isoformat(), minutes=120, start_at=at(cid, day, 18), end_at=at(cid, day, 20))
    r = file(walt, date=day.isoformat(), minutes=60, start_at=at(cid, day, 19), end_at=at(cid, day, 21))
    assert r.status_code == 409 and r.json()["code"] == "OVERLAPPING_ENTRIES"
    r = file(walt, date=day.isoformat(), minutes=90, start_at=at(cid, day, 20), end_at=at(cid, day, 21))
    assert r.status_code == 400 and r.json()["code"] == "INVALID_MINUTES"  # 90 minutes in a 60-minute span
    filed(walt, date=day.isoformat(), minutes=60, start_at=at(cid, day, 20), end_at=at(cid, day, 21))  # adjacent
    filed(walt, date=day.isoformat(), minutes=1200)
    r = file(walt, date=day.isoformat(), minutes=61)
    assert r.status_code == 400 and r.json()["code"] == "INVALID_MINUTES"  # 120 + 60 + 1200 + 61 > 1440
    # A rejected entry frees its minutes and its span.
    assert world["u"]["mona"].post(f"{BASE}/entries/{first['id']}/reject", json={"remarks": "No"}).status_code == 200
    filed(walt, date=day.isoformat(), minutes=60, start_at=at(cid, day, 18), end_at=at(cid, day, 19))


def test_ec_ot_10_database_guard_refuses_overlaps_and_over_24_hours(world):
    """The trigger backs the service check (concurrent requests, direct writes)."""
    from app.database.db import engine

    cid, walt_id = world["cid"], world["emp"]["walt"]["id"]
    day = world["today"] - timedelta(days=3)
    tz = tz_of(cid)
    start = tz.localize(datetime.combine(day, time(18)))

    def insert(conn, minutes, begin=None, end=None):
        return conn.execute(text(
            "INSERT INTO overtime_entries (company_id, employee_id, date, minutes_requested, start_at, end_at) "
            "VALUES (:c, :e, :d, :m, :s, :t) RETURNING id"), dict(c=cid, e=walt_id, d=day, m=minutes, s=begin, t=end))

    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.company_id', :c, true)"), {"c": str(cid)})
        insert(conn, 120, start, start + timedelta(hours=2))
        with pytest.raises(DBAPIError) as info:
            with conn.begin_nested():
                insert(conn, 60, start + timedelta(hours=1), start + timedelta(hours=3))
        assert info.value.orig.pgcode == "23P01"
        insert(conn, 1300)
        with pytest.raises(DBAPIError) as info:
            with conn.begin_nested():
                insert(conn, 30)
        assert info.value.orig.pgcode == "23514"
        for bad in ("minutes_requested = -5", "minutes_requested = 1441", "minutes_approved = 200",
                    "end_at = start_at - interval '1 minute'", "status = 'APPROVED'"):
            with pytest.raises(DBAPIError) as info:
                with conn.begin_nested():
                    conn.execute(text(f"UPDATE overtime_entries SET {bad} WHERE company_id = :c AND minutes_requested = 120"),
                                 {"c": cid})
            assert info.value.orig.pgcode == "23514", bad


# ---------------------------------------------------------------------------
# Authorization and scope
# ---------------------------------------------------------------------------
def test_manager_files_for_team_and_never_approves_own_or_self_filed_overtime(world):
    u = world["u"]
    day = (world["today"] - timedelta(days=1)).isoformat()
    own = filed(u["mona"], date=day, minutes=60)
    r = approve(u["mona"], own["id"])
    assert r.status_code == 403  # no self-approval
    for_walt = filed(u["mona"], employee_id=world["emp"]["walt"]["id"], date=day, minutes=45)
    assert entry(u["walt"], for_walt["id"])["requested_by_user_id"] == u["mona"].user_id
    assert approve(u["mona"], for_walt["id"]).status_code == 403  # the requester never approves
    assert approve(u["walt"], for_walt["id"]).status_code == 403  # nor the employee
    for entry_id in (own["id"], for_walt["id"]):
        r = approve(u["admin"], entry_id)
        assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    assert entry(u["walt"], for_walt["id"])["status"] == "APPROVED"


def test_ec_sec_05_manager_sees_and_acts_only_within_scope(world):
    u, emp = world["u"], world["emp"]
    day = (world["today"] - timedelta(days=1)).isoformat()
    walt_entry = filed(u["walt"], date=day, minutes=60)
    lou_entry = filed(u["lou"], date=day, minutes=60)
    listing = u["mona"].get(f"{BASE}/entries").json()
    assert {i["id"] for i in listing["items"]} == {walt_entry["id"]} and listing["total"] == 1
    assert u["mona"].get(f"{BASE}/entries/{lou_entry['id']}").status_code == 404
    assert approve(u["mona"], lou_entry["id"]).status_code == 404
    assert file(u["mona"], employee_id=emp["lou"]["id"], date=day, minutes=30).status_code == 404
    assert file(u["walt"], employee_id=emp["wendy"]["id"], date=day, minutes=30).status_code == 403
    assert u["walt"].get(f"{BASE}/entries/{lou_entry['id']}").status_code == 404
    assert {i["id"] for i in u["walt"].get(f"{BASE}/entries").json()["items"]} == {walt_entry["id"]}
    r = u["walt"].patch(f"{BASE}/entries/{lou_entry['id']}", json={"minutes": 30})
    assert r.status_code == 404
    # Another tenant sees nothing and cannot act.
    assert u["beta"].get(f"{BASE}/entries/{walt_entry['id']}").status_code == 404
    assert approve(u["beta"], walt_entry["id"]).status_code == 404
    assert u["beta"].get(f"{BASE}/entries").json()["total"] == 0
    assert file(u["beta"], employee_id=emp["walt"]["id"], date=day, minutes=30).status_code == 404
    assert u["bob"].post(f"{BASE}/entries/{walt_entry['id']}/cancel").status_code == 404


# ---------------------------------------------------------------------------
# Edits, versions, cancellation, consumption
# ---------------------------------------------------------------------------
def test_pending_entry_is_edited_in_place_and_its_approval_restarts(world):
    u = world["u"]
    day = world["today"] - timedelta(days=1)
    created = filed(u["walt"], date=day.isoformat(), minutes=60)
    before = entry(u["walt"], created["id"])
    r = u["walt"].patch(f"{BASE}/entries/{created['id']}", json={"minutes": 75, "row_version": 99})
    assert r.status_code == 409 and r.json()["code"] == "STALE_ROW_VERSION"
    r = u["walt"].patch(f"{BASE}/entries/{created['id']}", json={"minutes": 75, "row_version": before["row_version"]})
    assert r.status_code == 200, r.text
    after = entry(u["walt"], created["id"])
    assert after["minutes_requested"] == 75 and after["version"] == 1
    assert after["approval_request_id"] != before["approval_request_id"]
    old = u["walt"].get(f"/api/v1/payroll/approvals/{before['approval_request_id']}").json()
    assert old["status"] == "CANCELLED"
    assert u["walt"].patch(f"{BASE}/entries/{created['id']}", json={"date": None}).status_code == 422
    assert u["walt"].patch(f"{BASE}/entries/{created['id']}", json={"approved_by": 1}).status_code == 422
    assert approve(u["mona"], created["id"]).json()["status"] == "APPROVED"
    assert "OVERTIME_UPDATED" in audit_actions(world["cid"], created["id"])


def test_ec_ot_07_edit_after_approval_creates_a_new_version_that_supersedes_on_approval(world):
    u, cid = world["u"], world["cid"]
    walt_id = world["emp"]["walt"]["id"]
    day = world["today"] - timedelta(days=4)
    v1 = filed(u["walt"], date=day.isoformat(), minutes=120)
    assert approve(u["mona"], v1["id"]).status_code == 200
    with session(cid) as db:  # a finalized run paid it
        assert ot.consume_entries(db, cid, [v1["id"]], run_employee_id=501) == 1
        assert ot.consume_entries(db, cid, [v1["id"]], run_employee_id=501) == 0  # exactly once
        db.commit()
    with session(cid) as db, pytest.raises(Exception) as info:
        ot.consume_entries(db, cid, [v1["id"]], run_employee_id=502)
    assert getattr(info.value, "code", None) == "OVERTIME_NOT_CONSUMABLE"

    r = u["walt"].patch(f"{BASE}/entries/{v1['id']}", json={"minutes": 150})
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["id"] != v1["id"] and v2["version"] == 2 and v2["supersedes_id"] == v1["id"]
    assert v2["status"] == "PENDING_APPROVAL"
    # The approved version stays in force until the new one is approved.
    assert [r["id"] for r in approved_rows(cid, walt_id, day, day)] == [v1["id"]]
    r = u["walt"].patch(f"{BASE}/entries/{v1['id']}", json={"minutes": 160})
    assert r.status_code == 409 and r.json()["code"] == "VERSION_PENDING"
    # The pending version does not count twice against the 24-hour limit.
    filed(u["walt"], date=day.isoformat(), minutes=1440 - 150)

    assert approve(u["mona"], v2["id"]).status_code == 200
    assert entry(u["walt"], v1["id"])["status"] == "SUPERSEDED"
    rows = approved_rows(cid, walt_id, day, day)
    assert [(r["id"], r["minutes"], r["supersedes_id"]) for r in rows if r["id"] == v2["id"]] == [(v2["id"], 150,
                                                                                                   v1["id"])]
    assert v1["id"] not in [r["id"] for r in rows]
    assert entry(u["walt"], v1["id"])["consumed_by_run_employee_id"] == 501  # retro is the run's business
    assert "OVERTIME_SUPERSEDED" in audit_actions(cid, v1["id"])
    assert [c[0] for c in changes(cid, v1["id"])] == ["UPDATED", "VOIDED"]
    assert [c[0] for c in changes(cid, v2["id"])] == ["UPDATED"]
    assert "OVERTIME_VERSION_CREATED" in audit_actions(cid, v2["id"])


def test_cancel_pending_entry_and_requester_cancel_through_the_inbox(world):
    u = world["u"]
    day = (world["today"] - timedelta(days=1)).isoformat()
    first = filed(u["walt"], date=day, minutes=30)
    assert u["wendy"].post(f"{BASE}/entries/{first['id']}/cancel").status_code == 404
    r = u["walt"].post(f"{BASE}/entries/{first['id']}/cancel", json={"remarks": "Filed twice"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    assert u["walt"].post(f"{BASE}/entries/{first['id']}/cancel").json()["code"] == "OVERTIME_NOT_PENDING"
    second = filed(u["walt"], date=day, minutes=30)
    r = u["walt"].post(f"/api/v1/payroll/approvals/{second['approval_request_id']}/cancel")
    assert r.status_code == 200, r.text
    assert entry(u["walt"], second["id"])["status"] == "CANCELLED"


def test_approver_may_edit_minutes_down_never_up(world):
    u, cid = world["u"], world["cid"]
    day = world["today"] - timedelta(days=1)
    created = filed(u["walt"], date=day.isoformat(), minutes=120)
    r = approve(u["mona"], created["id"], minutes_approved=121)
    assert r.status_code == 400 and r.json()["code"] == "MINUTES_ABOVE_REQUESTED"
    assert approve(u["mona"], created["id"], minutes_approved="60").status_code == 422
    r = approve(u["walt"], created["id"], minutes_approved=30)
    assert r.status_code == 403
    assert entry(u["walt"], created["id"])["minutes_approved"] is None  # the refused edit was rolled back
    r = approve(u["mona"], created["id"], minutes_approved=60)
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    assert [r["minutes"] for r in approved_rows(cid, world["emp"]["walt"]["id"], day, day)] == [60]
    assert "OVERTIME_EDITED_BY_APPROVER" in audit_actions(cid, created["id"])


def test_ec_ot_09_comp_off_needs_a_policy_that_allows_it(world):
    u, cid = world["u"], world["cid"]
    day = world["today"] - timedelta(days=1)
    r = file(u["walt"], date=day.isoformat(), minutes=60, comp_off=True)
    assert r.status_code == 400 and r.json()["code"] == "COMP_OFF_NOT_ALLOWED"  # starter policy is a draft
    ot_policy(u["admin"])
    chosen = filed(u["walt"], date=day.isoformat(), minutes=60, comp_off=True)
    decided = filed(u["walt"], date=(day - timedelta(days=1)).isoformat(), minutes=45)
    assert approve(u["mona"], chosen["id"]).status_code == 200
    assert approve(u["mona"], decided["id"], comp_off=True).status_code == 200  # the approver decides
    rows = approved_rows(cid, world["emp"]["walt"]["id"], day - timedelta(days=1), day)
    assert [(r["id"], r["comp_off"]) for r in rows] == [(decided["id"], True), (chosen["id"], True)]


# ---------------------------------------------------------------------------
# Attendance proposals
# ---------------------------------------------------------------------------
def _past_wednesday(today):
    day = today - timedelta(days=7)
    return day - timedelta(days=(day.weekday() - 2) % 7)


def test_ec_ot_03_attendance_proposals_wait_for_a_manager_and_are_idempotent(world):
    u, cid = world["u"], world["cid"]
    walt_id = world["emp"]["walt"]["id"]
    wednesday = _past_wednesday(world["today"])
    ot_policy(u["admin"], source="ATTENDANCE_WITH_APPROVAL")
    worked(cid, walt_id, wednesday, 8, 18)  # 10 h against a daily threshold of 8 h
    worked(cid, walt_id, wednesday + timedelta(days=1), 9, 17)  # exactly 8 h: no overtime
    body = {"start": (wednesday - timedelta(days=2)).isoformat(), "end": (wednesday + timedelta(days=2)).isoformat()}
    assert u["walt"].post(f"{BASE}/proposals/generate", json=body).status_code == 403

    r = u["mona"].post(f"{BASE}/proposals/generate", json={**body, "employee_ids": [walt_id]})
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert [(c["employee_id"], c["date"], c["minutes"], c["status"]) for c in created] == [
        (walt_id, wednesday.isoformat(), 120, "PENDING_APPROVAL")]
    proposal = entry(u["mona"], created[0]["id"])
    assert proposal["source"] == "ATTENDANCE_WITH_APPROVAL" and proposal["minutes_computed"] == 120
    assert proposal["bucket"] == "WORKDAY" and proposal["requested_by_user_id"] is None
    assert approved_rows(cid, walt_id, wednesday, wednesday) == []

    again = u["mona"].post(f"{BASE}/proposals/generate", json={**body, "employee_ids": [walt_id]}).json()
    assert again["created"] == [] and {s["reason"] for s in again["skipped"]} == {"ALREADY_HAS_ENTRY"}
    r = u["mona"].post(f"{BASE}/proposals/generate", json={**body, "employee_ids": [world["emp"]["lou"]["id"]]})
    assert r.status_code == 404  # outside the manager's scope

    assert approve(u["mona"], created[0]["id"], minutes_approved=105).status_code == 200  # edited down
    rows = approved_rows(cid, walt_id, wednesday, wednesday)
    assert [(r["minutes"], r["origin"]) for r in rows] == [(105, "THRESHOLD")]
    assert "OVERTIME_PROPOSED" in audit_actions(cid, created[0]["id"])


def test_attendance_auto_policy_approves_proposals_and_requests_source_proposes_nothing(world):
    u, cid = world["u"], world["cid"]
    walt_id = world["emp"]["walt"]["id"]
    wednesday = _past_wednesday(world["today"])
    worked(cid, walt_id, wednesday, 8, 19)
    body = {"start": wednesday.isoformat(), "end": wednesday.isoformat(), "employee_ids": [walt_id]}
    r = u["admin"].post(f"{BASE}/proposals/generate", json=body)
    assert r.status_code == 200 and r.json()["created"] == []  # starter policy: requests only, no proposals
    ot_policy(u["admin"], source="APPROVED_REQUESTS")
    skipped = u["admin"].post(f"{BASE}/proposals/generate", json=body).json()["skipped"]
    assert {s["reason"] for s in skipped} == {"SOURCE_NOT_ATTENDANCE"}
    ot_policy(u["admin"], source="ATTENDANCE_AUTO")
    created = u["admin"].post(f"{BASE}/proposals/generate", json=body).json()["created"]
    assert [(c["status"], c["minutes"]) for c in created] == [("APPROVED", 180)]
    assert [r["minutes"] for r in approved_rows(cid, walt_id, wednesday, wednesday)] == [180]
    assert "OVERTIME_AUTO_APPROVED" in audit_actions(cid, created[0]["id"])
    assert [c[0] for c in changes(cid, created[0]["id"])] == ["CREATED"]


def test_attendance_auto_is_never_a_starter_profile_source(world):
    for profile in ("STARTER_V1", "LEGACY_V1"):
        for policy in pt.starter_policies(profile, leave_types=[]):
            if policy.type == "OVERTIME_POLICY":
                assert policy.fields["source"] != "ATTENDANCE_AUTO"
    policy = next(p for p in world["u"]["admin"].get(POLICIES, params={"type": "OVERTIME_POLICY"}).json())
    detail = world["u"]["admin"].get(f"{POLICIES}/{policy['id']}").json()
    assert {v["fields"]["source"] for v in detail["versions"]} == {"APPROVED_REQUESTS"}


def test_idempotency_key_replays_the_filing(world):
    walt = world["u"]["walt"]
    body = {"date": (world["today"] - timedelta(days=1)).isoformat(), "minutes": 30, "reason": "Deploy"}
    first = walt.post(f"{BASE}/entries", json=body, headers={"Idempotency-Key": "ot-1"})
    again = walt.post(f"{BASE}/entries", json=body, headers={"Idempotency-Key": "ot-1"})
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json() and again.headers.get("Idempotent-Replayed") == "true"
    other = walt.post(f"{BASE}/entries", json={**body, "minutes": 31}, headers={"Idempotency-Key": "ot-1"})
    assert other.status_code == 409
    assert walt.get(f"{BASE}/entries").json()["total"] == 1
