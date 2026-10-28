"""Device-token kiosk (AS-M2 WI-AS-2.9; 04-attendance/01 §5; AS 07 §10; D-AS-25; BUG-AS-06, -62).

Registration with a one-time claim code, pairing without a user session (AS-SEC-03), claim-code failures counted per
device (AS-SEC-04), the cookie scope (HttpOnly, SameSite=Strict, Path=/api/v1/attendance/kiosk), token expiry and
sliding renewal (AS-SEC-06), suspension (K-3), the 60 s kiosk ticket (AS-CAP-07), explicit direction (BUG-AS-06),
GV-AS-CAP-07 on a real kiosk (AS-CAP-03), cross-branch flag (AS-CAP-08), method checks (AS-CAP-09) and the identify
budgets (BUG-AS-62, AS-REL-05).
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.attendance_helpers import IST, employee_with_login, fake_clock, owner_rows, set_policy  # noqa: F401
from tests.helpers import create_employee, new_client, signup_company

PAIR = "/api/v1/attendance/kiosk/pair"
IDENTIFY = "/api/v1/attendance/kiosk/identify"
PUNCH = "/api/v1/attendance/kiosk/punch"


@pytest.fixture
def pin_policy(monkeypatch):
    """A STANDARD-like capture policy whose kiosk methods include PIN (phase 2 activates such versions)."""
    from app.services import attendance_capture_policy as cp

    real, branch = cp.effective, cp.for_branch
    methods = {"value": frozenset({"PIN"})}
    monkeypatch.setattr(cp, "effective", lambda *a: replace(real(*a), kiosk_methods=methods["value"]))
    monkeypatch.setattr(cp, "for_branch", lambda *a: replace(branch(*a), kiosk_methods=methods["value"]))
    return methods


@pytest.fixture
def alpha(app, fake_clock):
    from app.core.rate_limit import limiter

    admin = signup_company(app, "Alpha Kiosk Co")  # Asia/Kolkata
    set_policy(admin, requireFaceForSelfCheckIn=False)
    shift = admin.post("/api/v1/shifts", json={"code": "US-E1", "name": "US East day", "kind": "FIXED", "version": {
        "effective_from": "2026-01-01", "reason": "BPO", "start_time": "09:00", "end_time": "18:00",
        "anchor_timezone": "America/New_York"}})
    assert shift.status_code == 201, shift.text
    sameer = create_employee(admin, "Sameer", "Khan", shift_id=shift.json()["id"], employee_code="4417")
    limiter.reset()
    return {"admin": admin, "sameer": sameer}


def register(admin, code="KIOSK-BLR-L2", **extra):
    r = admin.post("/api/v1/attendance/devices", json={"code": code, "name": "BPO floor entrance", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def paired_kiosk(app, admin, code="KIOSK-BLR-L2", **extra):
    device = register(admin, code, **extra)
    kiosk = new_client(app)
    r = kiosk.post(PAIR, json={"device_code": code, "claim_code": device["claim_code"]})
    assert r.status_code == 200, r.text
    return kiosk, device, r


def identify(kiosk, value="4417", method="PIN"):
    return kiosk.post(IDENTIFY, data={"method": method, "value": value})


def kiosk_punch(kiosk, direction, value="4417"):
    r = identify(kiosk, value)
    assert r.status_code == 200 and r.json()["recognised"], r.text
    return kiosk.post(PUNCH, json={"kiosk_ticket": r.json()["kiosk_ticket"], "direction": direction})


def test_as_sec_03_pairing_needs_no_user_session_and_sets_a_scoped_cookie(app, alpha):
    """AS-SEC-03 / K-1, K-2: a signed-in admin registers the kiosk (claim code shown once, 24 h); the tablet pairs
    without a session; the device-token cookie is HttpOnly, SameSite=Strict and scoped to the kiosk API."""
    device = register(alpha["admin"])
    assert device["status"] == "PENDING_ACTIVATION" and len(device["claim_code"]) == 9
    listed = alpha["admin"].get("/api/v1/attendance/devices").json()["items"][0]
    assert "claim_code" not in listed and listed["paired"] is False
    kiosk = new_client(app)
    r = kiosk.post(PAIR, json={"device_code": "kiosk-blr-l2", "claim_code": device["claim_code"].lower()})
    assert r.status_code == 200, r.text
    cookie = r.headers["set-cookie"]
    assert "hrms_kiosk_device=" in cookie and "HttpOnly" in cookie and "Path=/api/v1/attendance/kiosk" in cookie
    assert "samesite=strict" in cookie.lower()
    stored = owner_rows("SELECT status, token_hash, claim_code_hash, activated_at FROM attendance_devices")[0]
    assert stored["status"] == "ACTIVE" and stored["token_hash"] and stored["claim_code_hash"] is None
    token = kiosk.http.cookies.get("hrms_kiosk_device")
    assert token.split(".")[-1] not in stored["token_hash"]  # only a keyed hash is stored
    # single use: the same code again -> 422; the cookie never reaches other API paths
    again = new_client(app).post(PAIR, json={"device_code": "KIOSK-BLR-L2", "claim_code": device["claim_code"]})
    assert again.status_code == 422 and again.json()["code"] == "CLAIM_CODE_INVALID"
    assert kiosk.get("/api/v1/attendance/me/today").status_code == 401


def test_as_sec_04_claim_code_failures_are_counted_per_device(app, alpha):
    """AS-SEC-04 / K-5: unknown device code and wrong code answer the same 422 CLAIM_CODE_INVALID; five failures on the
    device row invalidate the code (the correct code then fails too); a reissued code works."""
    device = register(alpha["admin"])
    tablet = new_client(app)
    unknown = tablet.post(PAIR, json={"device_code": "NO-SUCH", "claim_code": device["claim_code"]})
    assert unknown.status_code == 422 and unknown.json()["code"] == "CLAIM_CODE_INVALID"
    for _ in range(5):
        r = tablet.post(PAIR, json={"device_code": "KIOSK-BLR-L2", "claim_code": "AAAA-BBBB"})
        assert r.status_code == 422 and r.json()["code"] == "CLAIM_CODE_INVALID"
    assert owner_rows("SELECT claim_code_failed_attempts FROM attendance_devices")[0]["claim_code_failed_attempts"] == 5
    r = tablet.post(PAIR, json={"device_code": "KIOSK-BLR-L2", "claim_code": device["claim_code"]})
    assert r.status_code == 422
    fresh = alpha["admin"].post(f"/api/v1/attendance/devices/{device['id']}/claim-code").json()
    from app.core.rate_limit import limiter

    limiter.reset()
    assert tablet.post(PAIR, json={"device_code": "KIOSK-BLR-L2", "claim_code": fresh["claim_code"]}).status_code == 200


def test_pairing_is_rate_limited_per_address(app, alpha):
    """SP-AS-055: 10 pairing attempts per 15 minutes per client address, then 429."""
    tablet = new_client(app)
    codes = [tablet.post(PAIR, json={"device_code": "X", "claim_code": "AAAA-BBBB"}).status_code for _ in range(10)]
    assert set(codes) == {422}
    r = tablet.post(PAIR, json={"device_code": "X", "claim_code": "AAAA-BBBB"})
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"


def test_claim_code_expires_after_24_hours(app, alpha, fake_clock):
    device = register(alpha["admin"])
    fake_clock.advance(hours=25)
    r = new_client(app).post(PAIR, json={"device_code": "KIOSK-BLR-L2", "claim_code": device["claim_code"]})
    assert r.status_code == 410 and r.json()["code"] == "CLAIM_CODE_EXPIRED"


def test_device_endpoints_need_the_token(app, alpha, pin_policy):
    """K-1: identify / punch without a paired token -> 401 DEVICE_AUTH_FAILED; a signed-in user is not a device."""
    for client in (new_client(app), alpha["admin"]):
        r = identify(client)
        assert r.status_code == 401 and r.json()["code"] == "DEVICE_AUTH_FAILED"
    forged = new_client(app)
    forged.http.cookies.set("hrms_kiosk_device", f"v1.{alpha['admin'].company_id}.1.not-the-secret",
                            path="/api/v1/attendance/kiosk")
    assert identify(forged).status_code == 401


def test_gv_as_cap_07_sameer_on_the_kiosk_across_the_us_dst_change(app, alpha, pin_policy, fake_clock):
    """GV-AS-CAP-07 / AS-CAP-03 / BUG-AS-02: PIN 4417 -> Check in Fri 2026-10-30 18:24:51 and Check out Sat 03:36:02
    are both shift date 2026-10-30; the next night's Check in is accepted (2026-10-31); after the US change IN
    2026-11-03 19:23:30 and OUT 2026-11-04 04:33:12 are shift date 2026-11-03. KIOSK punches carry verify_mode PIN,
    the device, geofence NOT_CHECKED and the server time to the second."""
    kiosk, device, _ = paired_kiosk(app, alpha["admin"])
    steps = [(("2026-10-30", "18:24", 51), "IN", "2026-10-30"), (("2026-10-31", "03:36", 2), "OUT", "2026-10-30"),
             (("2026-10-31", "18:25", 0), "IN", "2026-10-31"), (("2026-11-01", "03:31", 0), "OUT", "2026-10-31"),
             (("2026-11-03", "19:23", 30), "IN", "2026-11-03"), (("2026-11-04", "04:33", 12), "OUT", "2026-11-03")]
    for (day, hhmm, second), direction, shift_date in steps:
        fake_clock.at_ist(day, hhmm, second)
        r = kiosk_punch(kiosk, direction)
        assert r.status_code == 200, (day, hhmm, r.text)
        assert (r.json()["shift_date"], r.json()["direction"]) == (shift_date, direction)
    rows = owner_rows("SELECT * FROM attendance_punches ORDER BY id")
    assert [(r["source"], r["direction"], str(r["shift_date"])) for r in rows] == [
        ("KIOSK", d, s) for _, d, s in steps]
    assert {(r["verify_mode"], r["device_id"], r["geofence_result"]) for r in rows} == {("PIN", device["id"],
                                                                                          "NOT_CHECKED")}
    assert rows[0]["occurred_at"] == IST.localize(__import__("datetime").datetime(2026, 10, 30, 18, 24, 51))
    sessions = owner_rows("SELECT date, capture_source, source FROM attendance ORDER BY id")
    assert [str(s["date"]) for s in sessions] == ["2026-10-30", "2026-10-31", "2026-11-03"]
    assert {(s["capture_source"], s["source"]) for s in sessions} == {("KIOSK", "kiosk")}


def test_as_cap_07_ticket_expiry_replay_other_direction(app, alpha, pin_policy, fake_clock):
    """AS-CAP-07: (a) 70 s after identify -> 410 KIOSK_TICKET_EXPIRED, nothing written; (b) the same ticket twice ->
    replayed with one punch; (c) the same ticket with OUT -> 409 IDEMPOTENCY_CONFLICT; (d) another kiosk -> 401;
    (e) a suspended kiosk -> 401, nothing written."""
    kiosk, _, _ = paired_kiosk(app, alpha["admin"])
    other, _, _ = paired_kiosk(app, alpha["admin"], code="KIOSK-BLR-L3")
    fake_clock.at_ist("2026-11-03", "19:22", 10)
    ticket = identify(kiosk).json()["kiosk_ticket"]
    fake_clock.advance(seconds=70)
    late = kiosk.post(PUNCH, json={"kiosk_ticket": ticket, "direction": "IN"})
    assert late.status_code == 410 and late.json()["code"] == "KIOSK_TICKET_EXPIRED"
    assert owner_rows("SELECT count(*) AS n FROM attendance_punches")[0]["n"] == 0
    ticket = identify(kiosk).json()["kiosk_ticket"]
    assert other.post(PUNCH, json={"kiosk_ticket": ticket, "direction": "IN"}).status_code == 401
    first = kiosk.post(PUNCH, json={"kiosk_ticket": ticket, "direction": "IN"})
    assert first.status_code == 200, first.text
    double = kiosk.post(PUNCH, json={"kiosk_ticket": ticket, "direction": "IN"})
    assert double.status_code == 200 and double.json()["replayed"] is True
    assert double.json()["punch_id"] == first.json()["punch_id"]
    conflict = kiosk.post(PUNCH, json={"kiosk_ticket": ticket, "direction": "OUT"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert owner_rows("SELECT count(*) AS n FROM attendance_punches")[0]["n"] == 1
    device_id = owner_rows("SELECT id FROM attendance_devices WHERE code = 'KIOSK-BLR-L2'")[0]["id"]
    fake_clock.advance(minutes=5)
    ticket = identify(kiosk).json()["kiosk_ticket"]
    r = alpha["admin"].post(f"/api/v1/attendance/devices/{device_id}/suspend", json={"reason": "Tablet lost"})
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED", r.text
    gone = kiosk.post(PUNCH, json={"kiosk_ticket": ticket, "direction": "OUT"})
    assert gone.status_code == 401 and gone.json()["code"] == "DEVICE_AUTH_FAILED"
    assert owner_rows("SELECT count(*) AS n FROM attendance_punches")[0]["n"] == 1


def test_bug_as_06_direction_is_explicit_never_a_toggle(app, alpha, pin_policy, fake_clock):
    """BUG-AS-06 / K-6: identify offers the actions of the person's state; a second Check in is refused (409
    ALREADY_CHECKED_IN) instead of checking the person out. AS-CAP-10 (K-13): photo_allowed is false without a PUNCH_PHOTO
    acknowledgement and the punch still succeeds."""
    kiosk, _, _ = paired_kiosk(app, alpha["admin"])
    fake_clock.at_ist("2026-11-02", "18:25")
    first = identify(kiosk).json()
    assert first["allowed_actions"] == ["IN"]
    # AS-CAP-10 (K-13): without a recorded PUNCH_PHOTO acknowledgement the kiosk takes no photo; the punch still works.
    assert first["photo_allowed"] is False and first["employee"]["initials"] == "SK"
    assert kiosk_punch(kiosk, "IN").status_code == 200
    fake_clock.advance(minutes=30)
    assert identify(kiosk).json()["allowed_actions"] == ["BREAK_START", "OUT"]
    again = kiosk_punch(kiosk, "IN")
    assert again.status_code == 409 and again.json()["code"] == "ALREADY_CHECKED_IN"
    assert owner_rows("SELECT check_out_time FROM attendance")[0]["check_out_time"] is None
    assert kiosk_punch(kiosk, "BREAK_START").status_code == 200
    assert identify(kiosk).json()["allowed_actions"] == ["BREAK_END", "OUT"]
    assert kiosk_punch(kiosk, "OUT").status_code == 200  # an open break ends with the explicit Check out


def test_as_cap_09_methods_on_the_screen_and_on_the_person(app, alpha, pin_policy, fake_clock):
    """AS-CAP-09 / K-14: a method the kiosk does not offer -> 422 KIOSK_METHOD_NOT_ALLOWED at identify; the punch
    re-checks the person's own methods (LEGACY_V1 offers FACE only)."""
    kiosk, _, _ = paired_kiosk(app, alpha["admin"])
    fake_clock.at_ist("2026-11-02", "18:25")
    r = kiosk.post(IDENTIFY, data={"method": "CARD", "value": "4417"})
    assert r.status_code == 422 and r.json()["code"] == "KIOSK_METHOD_NOT_ALLOWED"
    ticket = identify(kiosk).json()["kiosk_ticket"]
    pin_policy["value"] = frozenset({"FACE"})
    refused = kiosk.post(PUNCH, json={"kiosk_ticket": ticket, "direction": "IN"})
    assert refused.status_code == 422 and refused.json()["code"] == "KIOSK_METHOD_NOT_ALLOWED"
    assert identify(kiosk).status_code == 422  # LEGACY_V1-like {FACE}: PIN not on the screen either


def test_as_cap_08_visitor_from_another_branch_is_flagged(app, alpha, pin_policy, fake_clock):
    """AS-CAP-08 / K-11: PIN identifies any active employee of the tenant; a kiosk of BR 21 punches a BR 23 visitor at
    the kiosk with evidence flag OTHER_BRANCH_KIOSK (default FLAG), no review item."""
    admin = alpha["admin"]
    entity = admin.post("/api/v1/legal-entities", json={"code": "LE11", "legal_name": "Alpha India", "country_code": "IN",
                                                        "base_currency": "INR", "timezone": "Asia/Kolkata"}).json()
    br21 = admin.post("/api/v1/branches", json={"legal_entity_id": entity["id"], "code": "BR21", "name": "Bengaluru",
                                                "timezone": "Asia/Kolkata"}).json()
    br23 = admin.post("/api/v1/branches", json={"legal_entity_id": entity["id"], "code": "BR23", "name": "Dubai",
                                                "timezone": "Asia/Dubai"}).json()
    create_employee(admin, "Farah", "Ali", legal_entity_id=entity["id"], branch_id=br23["id"], employee_code="5102")
    kiosk, _, _ = paired_kiosk(app, admin, branch_id=br21["id"])
    fake_clock.at_ist("2026-11-11", "10:00")
    assert kiosk_punch(kiosk, "IN", value="5102").status_code == 200
    row = owner_rows("SELECT evidence_flags FROM attendance_punches")[0]
    assert row["evidence_flags"] == ["OTHER_BRANCH_KIOSK"]
    assert owner_rows("SELECT count(*) AS n FROM attendance_review_items")[0]["n"] == 0


def test_as_sec_06_token_expiry_and_sliding_renewal(app, alpha, pin_policy, fake_clock):
    """K-4 / K-12 / AS-SEC-06: in the last 14 days a call renews the token silently (new cookie, new hash); an expired
    token -> 401 DEVICE_AUTH_FAILED."""
    kiosk, _, _ = paired_kiosk(app, alpha["admin"])
    before = owner_rows("SELECT token_hash, token_expires_at FROM attendance_devices")[0]
    fake_clock.advance(days=80)
    r = identify(kiosk)
    assert r.status_code == 200 and "hrms_kiosk_device=" in r.headers.get("set-cookie", "")
    after = owner_rows("SELECT token_hash, token_expires_at FROM attendance_devices")[0]
    assert after["token_hash"] != before["token_hash"] and after["token_expires_at"] > before["token_expires_at"]
    assert identify(kiosk).status_code == 200  # the renewed cookie works
    fake_clock.advance(days=91)
    expired = identify(kiosk)
    assert expired.status_code == 401 and expired.json()["code"] == "DEVICE_AUTH_FAILED"


def test_bug_as_62_identify_budgets(app, alpha, pin_policy, fake_clock):
    """BUG-AS-62 / AS-REL-05 / K-7a: 5 consecutive wrong values start a 30 s cooldown for that value only (the crew
    keeps punching); 60 identifies per minute per device, then 429 'The kiosk is busy'."""
    kiosk, _, _ = paired_kiosk(app, alpha["admin"])
    fake_clock.at_ist("2026-11-02", "18:25")
    for _ in range(5):
        r = identify(kiosk, value="9999")
        assert r.status_code == 200 and r.json()["recognised"] is False
    cooled = identify(kiosk, value="9999")
    assert cooled.status_code == 429 and cooled.json()["code"] == "RATE_LIMITED"
    assert identify(kiosk).status_code == 200  # another person is not locked out
    for _ in range(60 - 7):
        assert identify(kiosk).status_code == 200
    busy = identify(kiosk)
    assert busy.status_code == 429 and busy.json()["code"] == "RATE_LIMITED"
