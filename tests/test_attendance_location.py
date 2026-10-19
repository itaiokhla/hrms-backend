"""Location evidence at events, periodic tracking schedule, validation and honesty rules."""

from datetime import timedelta

import pytest

from app.services import geo
from tests.attendance_helpers import (  # noqa: F401  (fake_clock is a fixture)
    BLR, act, employee_with_login, failure, fake_clock, fix, key, meta, owner_rows, sample, set_policy, today, tracking,
)
from tests.helpers import signup_company

DAY = "2026-10-19"


@pytest.fixture
def company(app):
    admin = signup_company(app, "Fieldwork Co")
    set_policy(admin, requireFaceForSelfCheckIn=False, locationPunchPolicy="REQUIRED", locationLunchPolicy="OPTIONAL",
               trackingEnabled=True, trackingIntervalMinutes=120, trackingGraceMinutes=15)
    emp = employee_with_login(app, admin, "Ravi")
    return {"admin": admin, "emp": emp, "app": app}


# Policy -----------------------------------------------------------------------
def test_policy_is_validated_and_admin_only(app, company):
    admin = company["admin"]
    for bad in ({"trackingIntervalMinutes": 5}, {"trackingIntervalMinutes": 481}, {"locationPunchPolicy": "SOMETIMES"},
                {"gpsPreferredAccuracyM": 200, "gpsAcceptableAccuracyM": 100}, {"trackingGraceMinutes": 120},
                {"trackingIntervalMinutes": "two hours"}, {"companyName": "Renamed"}):
        r = admin.patch("/api/v1/attendance/policy", json=bad)
        assert r.status_code == 400, (bad, r.text)
    assert admin.get("/api/v1/attendance/policy").json()["settings"]["trackingIntervalMinutes"] == 120
    hr = employee_with_login(app, admin, "Hema", roles=("hr_admin",))
    assert hr.patch("/api/v1/attendance/policy", json={"trackingEnabled": False}).status_code == 403
    # Every employee can read what is collected about them, but not change it.
    emp = company["emp"]
    seen = emp.get("/api/v1/attendance/policy").json()
    assert seen["policy"]["tracking"] == {"enabled": True, "interval_minutes": 120, "grace_minutes": 15,
                                          "pause_on_lunch": True, "movement_threshold_m": 1000}
    assert "settings" not in seen
    assert emp.patch("/api/v1/attendance/policy", json={"trackingEnabled": False}).status_code == 403
    logs = admin.get("/api/v1/audit-logs?module=Settings").json()["logs"]
    assert any("trackingEnabled (False -> True)" in log["description"] for log in logs)  # old and new values audited


def test_notice_and_required_location_at_punch_in(company, fake_clock):
    emp = company["emp"]
    fake_clock.at_ist(DAY, "09:00")
    no_notice = act(emp, "PUNCH_IN", {**meta(fake_clock, fix(fake_clock)), "location_consent": False})
    assert no_notice.status_code == 400 and no_notice.json()["code"] == "LOCATION_NOTICE_REQUIRED"
    missing = act(emp, "PUNCH_IN", meta(fake_clock))
    assert missing.status_code == 422 and missing.json()["code"] == "LOCATION_REQUIRED"
    denied = act(emp, "PUNCH_IN", meta(fake_clock, failure()))
    assert denied.status_code == 422 and "permission was denied" in denied.json()["detail"]
    # Face match or not, a required location is never silently skipped: the exception path is explicit.
    view = act(emp, "PUNCH_IN", meta(fake_clock, failure(), exception_reason="Phone GPS broken today")).json()
    assert view["events"][0]["location_status"] == "EXCEPTION"
    rows = owner_rows("SELECT status, latitude, exception_reason, review_status FROM location_samples")
    assert rows == [{"status": "PERMISSION_DENIED", "latitude": None, "exception_reason": "Phone GPS broken today",
                     "review_status": "PENDING"}]
    assert owner_rows("SELECT count(*) AS n FROM attendance")[0]["n"] == 1


def test_exceptions_can_be_disabled(company, fake_clock):
    set_policy(company["admin"], locationExceptionPolicy="BLOCK")
    fake_clock.at_ist(DAY, "09:00")
    r = act(company["emp"], "PUNCH_IN", meta(fake_clock, failure("TIMEOUT"), exception_reason="No signal in basement"))
    assert r.status_code == 422 and "Ask HR" in r.json()["detail"]


def test_captured_reading_is_stored_exactly(company, fake_clock):
    emp = company["emp"]
    fake_clock.at_ist(DAY, "09:00")
    r = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock, 12.971598, 77.594566, accuracy=18.5, age_seconds=4)))
    assert r.status_code == 200, r.text
    row = owner_rows("SELECT * FROM location_samples")[0]
    assert (row["latitude"], row["longitude"], row["accuracy_m"]) == (12.971598, 77.594566, 18.5)
    assert row["sample_type"] == "PUNCH_IN" and row["status"] == "CAPTURED" and row["fix_age_seconds"] == 4
    assert row["received_at"] == fake_clock.now and row["accuracy_class"] == "PREFERRED"
    assert row["movement_status"] == "BASELINE" and row["event_id"] is not None
    tracking_view = r.json()["tracking"]
    assert tracking_view["state"] == "ACTIVE" and tracking_view["last_capture"]["accuracy_m"] == 18.5


@pytest.mark.parametrize("location,code", [
    ({"latitude": 91}, "VALIDATION_ERROR"),
    ({"longitude": -180.5}, "VALIDATION_ERROR"),
    ({"accuracy_m": -5}, "VALIDATION_ERROR"),
    ({"accuracy_m": 0}, "VALIDATION_ERROR"),
    ({"latitude": "NaN"}, "VALIDATION_ERROR"),
    ({"latitude": 0, "longitude": 0}, "VALIDATION_ERROR"),
    ({"captured_at": "2026-10-19T03:29:00"}, "VALIDATION_ERROR"),  # no timezone
    ({"status": "TIMEOUT"}, "VALIDATION_ERROR"),  # a failure cannot carry coordinates
])
def test_nonsense_readings_are_rejected(company, fake_clock, location, code):
    fake_clock.at_ist(DAY, "09:00")
    r = act(company["emp"], "PUNCH_IN", meta(fake_clock, {**fix(fake_clock), **location}))
    assert r.status_code == 422 and r.json()["code"] == code, r.text
    assert owner_rows("SELECT count(*) AS n FROM attendance")[0]["n"] == 0


def test_stale_future_and_skewed_device_clocks(company, fake_clock):
    emp = company["emp"]
    fake_clock.at_ist(DAY, "09:00")
    stale = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock, age_seconds=600)))
    assert stale.status_code == 422 and stale.json()["code"] == "LOCATION_STALE"
    future = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock, age_seconds=-3600)))
    assert future.status_code == 422 and future.json()["code"] == "LOCATION_INVALID"
    body = meta(fake_clock, fix(fake_clock))
    body["client_sent_at"] = (fake_clock.now + timedelta(days=2)).isoformat()
    assert act(emp, "PUNCH_IN", body).json()["code"] == "LOCATION_INVALID"
    # A device clock 3 hours slow with a fresh fix is accepted; the skew is recorded, not trusted.
    skewed = fake_clock.now - timedelta(hours=3)
    body = meta(fake_clock, {**fix(fake_clock), "captured_at": (skewed - timedelta(seconds=2)).isoformat()})
    body["client_sent_at"] = skewed.isoformat()
    assert act(emp, "PUNCH_IN", body).status_code == 200
    row = owner_rows("SELECT clock_skew_seconds, fix_age_seconds, received_at FROM location_samples")[0]
    assert row["clock_skew_seconds"] == 3 * 3600 and row["fix_age_seconds"] == 2 and row["received_at"] == fake_clock.now


@pytest.mark.parametrize("lat,lng", [(90, 0), (-90, 0), (0, 180), (0, -180), (0, 77.5), (12.9, 0)])
def test_coordinate_boundaries_are_valid_readings(company, fake_clock, lat, lng):
    fake_clock.at_ist(DAY, "09:00")
    assert act(company["emp"], "PUNCH_IN", meta(fake_clock, fix(fake_clock, lat, lng))).status_code == 200


def test_low_accuracy_is_flagged_or_refused_by_policy(company, fake_clock):
    emp, admin = company["emp"], company["admin"]
    fake_clock.at_ist(DAY, "09:00")
    assert act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock, accuracy=2500))).status_code == 200
    assert owner_rows("SELECT accuracy_class FROM location_samples")[0]["accuracy_class"] == "LOW"
    set_policy(admin, locationRejectLowAccuracy=True)
    fake_clock.at_ist(DAY, "18:00")
    r = act(emp, "PUNCH_OUT", meta(fake_clock, fix(fake_clock, accuracy=2500)))
    assert r.status_code == 422 and r.json()["code"] == "LOCATION_LOW_ACCURACY"
    r = act(emp, "PUNCH_OUT", meta(fake_clock, fix(fake_clock, accuracy=2500), exception_reason="Laptop on office Wi-Fi"))
    assert r.status_code == 200 and r.json()["events"][-1]["location_status"] == "EXCEPTION"


def test_optional_lunch_location_records_failures_honestly(company, fake_clock):
    emp = company["emp"]
    fake_clock.at_ist(DAY, "09:00")
    act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock)))
    fake_clock.at_ist(DAY, "13:00")
    view = act(emp, "LUNCH_IN", meta(fake_clock, failure("POSITION_UNAVAILABLE", "Location unavailable"))).json()
    assert view["events"][-1]["location_status"] == "FAILED"
    fake_clock.at_ist(DAY, "13:30")
    view = act(emp, "LUNCH_OUT", meta(fake_clock)).json()  # nothing sent at all
    assert view["events"][-1]["location_status"] == "NOT_PROVIDED"
    rows = owner_rows("SELECT sample_type, status, latitude FROM location_samples WHERE sample_type <> 'PERIODIC' ORDER BY id")
    assert rows == [{"sample_type": "PUNCH_IN", "status": "CAPTURED", "latitude": BLR[0]},
                    {"sample_type": "LUNCH_IN", "status": "POSITION_UNAVAILABLE", "latitude": None}]
    # The 11:00 periodic slot had no reading by 11:15, so it was recorded as missed when lunch began.
    assert owner_rows("SELECT status, latitude FROM location_samples WHERE sample_type = 'PERIODIC'") == [
        {"status": "MISSED", "latitude": None}]


def test_movement_classification_uses_both_uncertainties(company, fake_clock):
    emp = company["emp"]
    fake_clock.at_ist(DAY, "09:00")
    att_id = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock, accuracy=20))).json()["record"]["id"]
    checks = [(30, 20, "WITHIN_RANGE"), (1040, 20, "UNCERTAIN"), (5000, 20, "SIGNIFICANT_MOVEMENT_REVIEW"),
              (5000, 2500, "LOW_ACCURACY")]
    for metres, accuracy, expected in checks:
        fake_clock.advance(minutes=121)
        lat, lng = geo.destination(*BLR, 45, metres)
        r = sample(emp, fake_clock, att_id, fix(fake_clock, lat, lng, accuracy=accuracy))
        assert r.status_code == 200, r.text
        row = owner_rows("SELECT movement_status, distance_from_punch_in_m, review_status FROM location_samples "
                         "ORDER BY id DESC LIMIT 1")[0]
        assert row["movement_status"] == expected, (metres, row)
        assert row["distance_from_punch_in_m"] == pytest.approx(metres, abs=0.01)
        assert row["review_status"] == ("PENDING" if expected == "SIGNIFICANT_MOVEMENT_REVIEW" else None)


def test_tracking_schedule_from_punch_in_to_punch_out(company, fake_clock):
    emp = company["emp"]
    fake_clock.at_ist(DAY, "09:00")
    att_id = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock))).json()["record"]["id"]
    status = tracking(emp)
    assert status["state"] == "ACTIVE" and status["schedule_state"] == "SCHEDULED"
    assert status["next_due_at"].startswith("2026-10-19T05:30")  # 11:00 IST

    fake_clock.at_ist(DAY, "10:00")
    early = sample(emp, fake_clock, att_id, fix(fake_clock))
    assert early.status_code == 409 and early.json()["code"] == "NOT_DUE"

    fake_clock.at_ist(DAY, "11:05")
    assert tracking(emp)["schedule_state"] == "DUE"
    idem = key()
    first = sample(emp, fake_clock, att_id, fix(fake_clock), idem)
    assert first.status_code == 200 and first.json()["next_due_at"].startswith("2026-10-19T07:35")  # 13:05 IST
    assert sample(emp, fake_clock, att_id, fix(fake_clock), idem).json()["replayed"] is True  # retried upload
    second_tab = sample(emp, fake_clock, att_id, fix(fake_clock))
    assert second_tab.status_code == 409 and second_tab.json()["code"] == "NOT_DUE"

    fake_clock.at_ist(DAY, "12:00")
    act(emp, "LUNCH_IN", meta(fake_clock, fix(fake_clock)))
    assert tracking(emp)["state"] == "PAUSED"
    paused = sample(emp, fake_clock, att_id, fix(fake_clock))
    assert paused.status_code == 409 and paused.json()["code"] == "TRACKING_NOT_ACTIVE"
    fake_clock.at_ist(DAY, "12:45")
    act(emp, "LUNCH_OUT", meta(fake_clock))
    status = tracking(emp)
    assert status["state"] == "ACTIVE" and status["next_due_at"].startswith("2026-10-19T09:15")  # 14:45 IST

    # The tab is suspended (or closed) for hours: both missed slots are recorded, nothing is invented.
    fake_clock.at_ist(DAY, "17:10")
    status = tracking(emp)
    assert status["schedule_state"] == "OVERDUE" and status["missed_count"] == 2
    recovery = sample(emp, fake_clock, att_id, fix(fake_clock))
    assert recovery.status_code == 200 and recovery.json()["schedule_state"] == "SCHEDULED"
    rows = owner_rows("SELECT status, latitude, is_recovery, slot_due_at FROM location_samples "
                      "WHERE sample_type = 'PERIODIC' ORDER BY id")
    assert [r["status"] for r in rows] == ["CAPTURED", "MISSED", "MISSED", "CAPTURED"]
    assert all(r["latitude"] is None for r in rows if r["status"] == "MISSED")
    assert rows[-1]["is_recovery"] is True

    fake_clock.at_ist(DAY, "17:30")
    view = act(emp, "PUNCH_OUT", meta(fake_clock, fix(fake_clock))).json()
    assert view["tracking"]["state"] == "NO_SESSION"
    assert owner_rows("SELECT tracking_state, tracking_stop_reason FROM attendance")[0] == {
        "tracking_state": "STOPPED", "tracking_stop_reason": "PUNCHED_OUT"}
    fake_clock.at_ist(DAY, "19:31")
    late = sample(emp, fake_clock, att_id, fix(fake_clock))
    assert late.status_code == 409 and late.json()["code"] == "SESSION_NOT_ACTIVE"


def test_failed_periodic_attempts_are_recorded_once_and_slot_stays_open(company, fake_clock):
    emp = company["emp"]
    fake_clock.at_ist(DAY, "09:00")
    att_id = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock))).json()["record"]["id"]
    fake_clock.at_ist(DAY, "11:01")
    for _ in range(3):
        assert sample(emp, fake_clock, att_id, failure("PERMISSION_DENIED")).status_code == 200
    assert sample(emp, fake_clock, att_id, failure("TIMEOUT", "Timeout expired")).status_code == 200
    assert sample(emp, fake_clock, att_id, fix(fake_clock)).status_code == 200
    rows = owner_rows("SELECT status FROM location_samples WHERE sample_type = 'PERIODIC' ORDER BY id")
    assert [r["status"] for r in rows] == ["PERMISSION_DENIED", "TIMEOUT", "CAPTURED"]


def test_policy_changes_apply_to_running_shifts(company, fake_clock):
    emp, admin = company["emp"], company["admin"]
    fake_clock.at_ist(DAY, "09:00")
    att_id = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock))).json()["record"]["id"]
    fake_clock.at_ist(DAY, "09:30")
    set_policy(admin, trackingIntervalMinutes=60)
    assert tracking(emp)["next_due_at"].startswith("2026-10-19T05:00")  # 10:30 IST: one new interval from the change
    fake_clock.at_ist(DAY, "09:40")
    set_policy(admin, trackingEnabled=False)
    status = tracking(emp)
    assert status["state"] == "STOPPED" and status["stop_reason"] == "POLICY_DISABLED"
    fake_clock.at_ist(DAY, "10:31")
    assert sample(emp, fake_clock, att_id, fix(fake_clock)).json()["code"] == "TRACKING_NOT_ACTIVE"


def test_suspension_and_role_revocation_stop_collection(company, fake_clock):
    from tests.helpers import role_id

    admin, app = company["admin"], company["app"]
    emp = company["emp"]
    other = employee_with_login(app, admin, "Gita")
    fake_clock.at_ist(DAY, "09:00")
    att_id = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock))).json()["record"]["id"]
    act(other, "PUNCH_IN", meta(fake_clock, fix(fake_clock)))
    assert admin.put(f"/api/v1/users/{emp.user_id}/status", json={"status": "suspended"}).status_code == 200
    fake_clock.at_ist(DAY, "11:05")
    assert sample(emp, fake_clock, att_id, fix(fake_clock)).status_code == 401  # revoked session: upload refused
    assert admin.put(f"/api/v1/users/{other.user_id}/roles", json={"role_ids": [role_id(admin, "auditor")]}).status_code == 200
    assert other.get("/api/v1/attendance/me/tracking").status_code == 403
    reasons = {r["tracking_stop_reason"] for r in owner_rows("SELECT tracking_stop_reason FROM attendance")}
    assert reasons == {"ACCOUNT_SUSPENDED", "PERMISSION_REVOKED"}


def test_disabled_company_collects_nothing(app, fake_clock):
    admin = signup_company(app, "Quiet Co")
    set_policy(admin, requireFaceForSelfCheckIn=False)  # tracking off by default
    emp = employee_with_login(app, admin, "Mira")
    fake_clock.at_ist(DAY, "09:00")
    view = act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock))).json()
    assert view["tracking"]["state"] == "NOT_APPLICABLE" and view["events"][0]["location_status"] == "NOT_REQUIRED"
    assert owner_rows("SELECT count(*) AS n FROM location_samples")[0]["n"] == 0  # minimum collection: nothing stored
    fake_clock.at_ist(DAY, "11:30")
    r = sample(emp, fake_clock, view["record"]["id"], fix(fake_clock))
    assert r.status_code == 409 and r.json()["code"] == "TRACKING_NOT_ACTIVE"


def test_sweep_detects_missed_slots_and_retention_redacts(company, fake_clock):
    from app.database.db import SessionLocal, bind_tenant
    from app.services import location_service

    emp, admin = company["emp"], company["admin"]
    fake_clock.at_ist(DAY, "09:00")
    act(emp, "PUNCH_IN", meta(fake_clock, fix(fake_clock)))
    company_id = emp.me()["company"]["id"]
    fake_clock.at_ist(DAY, "13:20")  # nobody has the page open
    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        result = location_service.sweep(db, company_id)
        db.commit()
    finally:
        db.close()
    assert result["missed_slots"] == 2 and result["expired"] == 0
    set_policy(admin, locationRetentionDays=7)
    fake_clock.advance(days=8)
    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        result = location_service.sweep(db, company_id)
        db.commit()
    finally:
        db.close()
    assert result["expired"] == 1 and result["redacted"] == 1
    row = owner_rows("SELECT latitude, longitude, accuracy_m, redacted_at FROM location_samples WHERE status = 'CAPTURED'")[0]
    assert row["latitude"] is None and row["longitude"] is None and row["redacted_at"] is not None
    assert row["accuracy_m"] == 15.0  # the outcome and its quality stay for audit


# The exact payloads src/lib/geolocation.ts sends when the browser refuses before asking
# (found by the Chrome suite: attempts 0 used to be rejected, blocking even the exception path).
@pytest.mark.parametrize("status,message", [
    ("PERMISSION_DENIED", "Location permission is blocked for this site"),
    ("INSECURE_CONTEXT", "Location needs a secure (HTTPS) connection"),
    ("UNSUPPORTED", "This browser has no location support"),
])
def test_browser_refusals_without_an_attempt_are_accepted(app, company, fake_clock, status, message):
    emp = company["emp"]
    fake_clock.at_ist(DAY, "09:00")
    refused = {"status": status, "attempts": 0, "error_message": message}
    r = act(emp, "PUNCH_IN", meta(fake_clock, refused))
    assert r.status_code == 422 and r.json()["code"] == "LOCATION_REQUIRED"  # still refused: the policy requires location
    r = act(emp, "PUNCH_IN", meta(fake_clock, refused, exception_reason="Browser blocks location on this PC"))
    assert r.status_code == 200, r.text
    row = owner_rows("SELECT status, attempts, latitude, error_message FROM location_samples")[0]
    assert row == {"status": status, "attempts": 0, "latitude": None, "error_message": message}
