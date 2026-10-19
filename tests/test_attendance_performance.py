"""Volume behaviour: list, board and hours views stay at a fixed number of queries (no N+1)."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import event, text

from tests.attendance_helpers import set_policy
from tests.helpers import create_employee, signup_company

TODAY = date.today()


def _seed(company_id: int, employee_ids, samples_per_session: int = 20) -> None:
    """Insert realistic volumes directly (as the owner) so the test stays fast."""
    from app.database.db import system_engine

    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=8)
    with system_engine.begin() as conn:
        for index, employee_id in enumerate(employee_ids):
            closed = index % 2 == 0
            att = conn.execute(text(
                "INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, is_manual, source, "
                "status, tracking_state, created_at, updated_at) VALUES (:c, :e, :i, :o, :d, false, 'self', 'present', "
                ":t, now(), now()) RETURNING id"),
                {"c": company_id, "e": employee_id, "i": start, "o": start + timedelta(hours=8) if closed else None,
                 "d": TODAY, "t": "STOPPED" if closed else "ACTIVE"}).scalar()
            conn.execute(text(
                "INSERT INTO attendance_breaks (company_id, attendance_id, employee_id, kind, started_at, ended_at, is_paid, "
                "created_at, updated_at) VALUES (:c, :a, :e, 'LUNCH', :s, :t, false, now(), now())"),
                {"c": company_id, "a": att, "e": employee_id, "s": start + timedelta(hours=4), "t": start + timedelta(hours=4, minutes=45)})
            for k in range(samples_per_session):
                captured = k % 5 != 4
                conn.execute(text(
                    "INSERT INTO location_samples (company_id, attendance_id, employee_id, sample_type, status, latitude, "
                    "longitude, accuracy_m, received_at, slot_due_at, is_recovery, accuracy_class, movement_status, "
                    "review_status, created_at, updated_at) VALUES (:c, :a, :e, 'PERIODIC', :st, :lat, :lng, :acc, :r, :r, "
                    "false, :cls, :mv, :rv, now(), now())"),
                    {"c": company_id, "a": att, "e": employee_id, "st": "CAPTURED" if captured else "MISSED",
                     "lat": 12.97 if captured else None, "lng": 77.59 if captured else None, "acc": 20.0 if captured else None,
                     "r": start + timedelta(minutes=20 * (k + 1)), "cls": "PREFERRED" if captured else None,
                     "mv": ("SIGNIFICANT_MOVEMENT_REVIEW" if k == 3 else "WITHIN_RANGE") if captured else None,
                     "rv": "PENDING" if k == 3 else None})


def _statements(call) -> int:
    from app.database.db import engine

    count = {"n": 0}

    def tick(*_args, **_kwargs):
        count["n"] += 1

    event.listen(engine, "before_cursor_execute", tick)
    try:
        response = call()
        assert response.status_code == 200, response.text
    finally:
        event.remove(engine, "before_cursor_execute", tick)
    return count["n"]


def test_views_use_a_fixed_number_of_queries(app):
    admin = signup_company(app, "Volume Co")
    set_policy(admin, requireFaceForSelfCheckIn=False)
    employees = [create_employee(admin, f"Vol{i}")["id"] for i in range(40)]
    _seed(admin.company_id, employees)
    day = {"start_date": TODAY.isoformat(), "end_date": TODAY.isoformat()}

    def listing(limit):
        return lambda: admin.get("/api/v1/attendance", params={**day, "limit": limit})

    small, large = _statements(listing(5)), _statements(listing(40))
    assert small == large, f"attendance list grew from {small} to {large} queries"
    assert len(admin.get("/api/v1/attendance", params={**day, "limit": 40}).json()["items"]) == 40

    for path, params in (("/api/v1/attendance/summary/today", {}), ("/api/v1/attendance/hours", day),
                         ("/api/v1/attendance/location-review", {}), ("/api/v1/reports/attendance", day)):
        assert _statements(lambda: admin.get(path, params=params)) < 40, path  # bounded, not one per session

    summary = admin.get("/api/v1/attendance/hours", params=day).json()["totals"]
    assert summary["sessions"] == 40 and summary["lunch_seconds"] == 40 * 45 * 60
    assert len(admin.get("/api/v1/attendance/location-review", params={"limit": 500}).json()) == 40
