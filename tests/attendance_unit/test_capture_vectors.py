"""Capture golden vectors and pure capture cases (AS-M2 WI-AS-2.9; 04-attendance/01 §15; 04-attendance/03 §3.2 step 3,
§5). Pure: shift instances are built with shift_engine, attribution is punch_pairing (no database).

GV-AS-CAP-07 / AS-CAP-03 (Sameer, US-E1 anchored America/New_York, kiosk across the US DST change), AS-CAP-02
(back-to-back instances), BUG-AS-02 (the next night's punch opens the next shift date), kiosk tickets (AS-CAP-07)
and the canonical PIN form (M-11).
"""

import datetime as dt
from types import SimpleNamespace

import pytest
import pytz

from app.services import punch_pairing
from app.services import shift_engine as se

IST = "Asia/Kolkata"
V = se.ShiftVersionData
US_E1 = V(start_time="09:00", end_time="18:00", anchor_timezone="America/New_York", shift_id=21, shift_version_id=2101)
M6 = V(start_time="08:00", end_time="14:00", shift_id=24, shift_version_id=2401)
E6 = V(start_time="14:00", end_time="20:00", shift_id=25, shift_version_id=2501)
C = V(start_time="22:00", end_time="06:00", shift_id=22, shift_version_id=2201)
FIELD = V(kind="OPEN", expected_net_minutes=480, shift_id=26, shift_version_id=2601)


def ist(y, mo, d, h, mi, s=0) -> dt.datetime:
    return pytz.timezone(IST).localize(dt.datetime(y, mo, d, h, mi, s)).astimezone(dt.timezone.utc)


def around(version, day: dt.date):
    return [se.build_instance(version, day + dt.timedelta(days=k), IST) for k in (-1, 0, 1)]


def shift_date(version, at: dt.datetime, direction: str) -> dt.date:
    local = at.astimezone(pytz.timezone(IST)).date()
    day, _ = punch_pairing.shift_date(around(version, local), at, direction, IST)
    return day


def test_gv_as_cap_07_sameer_kiosk_night_across_the_us_dst_change():
    """GV-AS-CAP-07 / AS-CAP-03: IN Fri 2026-10-30 18:24:51 and OUT Sat 2026-10-31 03:36:02 IST are both shift date
    2026-10-30 (EDT instance 18:30-03:30 IST); after the US change the EST instance is 19:30-04:30 IST: IN Tue
    2026-11-03 19:23:30 (6 min 30 s early, inside early_in_window_minutes 120) and OUT Wed 2026-11-04 04:33:12 are
    shift date 2026-11-03."""
    assert shift_date(US_E1, ist(2026, 10, 30, 18, 24, 51), "IN") == dt.date(2026, 10, 30)
    assert shift_date(US_E1, ist(2026, 10, 31, 3, 36, 2), "OUT") == dt.date(2026, 10, 30)
    instance = se.build_instance(US_E1, dt.date(2026, 11, 3), IST)
    assert instance.start_at == ist(2026, 11, 3, 19, 30)  # EST: one hour later in IST
    assert shift_date(US_E1, ist(2026, 11, 3, 19, 23, 30), "IN") == dt.date(2026, 11, 3)
    assert shift_date(US_E1, ist(2026, 11, 4, 4, 33, 12), "OUT") == dt.date(2026, 11, 3)


def test_bug_as_02_next_nights_punch_opens_the_next_shift_date():
    """BUG-AS-02: Sameer's next night (Sat 2026-10-31 18:2x IST) is a new shift date, never 'already complete';
    a C-shift IN after midnight (Sat 00:10) still belongs to Friday's 22:00 instance, so Saturday 21:55 is free."""
    assert shift_date(US_E1, ist(2026, 10, 31, 18, 25, 0), "IN") == dt.date(2026, 10, 31)
    assert shift_date(C, ist(2026, 10, 31, 0, 10), "IN") == dt.date(2026, 10, 30)
    assert shift_date(C, ist(2026, 10, 31, 21, 55), "IN") == dt.date(2026, 10, 31)


def test_as_cap_02_back_to_back_instances_out_to_nearest_end_in_to_nearest_start():
    """AS-CAP-02: M6 08:00-14:00 and E6 14:00-20:00 on 2026-11-10: OUT 14:00:02 -> M6 (nearest end), IN 14:00:20 ->
    E6 (nearest start); both shift date 2026-11-10."""
    day = dt.date(2026, 11, 10)
    instances = [se.build_instance(M6, day, IST), se.build_instance(E6, day, IST)]
    out = punch_pairing.attribute(instances, ist(2026, 11, 10, 14, 0, 2), "OUT")
    back_in = punch_pairing.attribute(instances, ist(2026, 11, 10, 14, 0, 20), "IN")
    assert out.shift_version_id == 2401 and back_in.shift_version_id == 2501
    assert out.shift_date == back_in.shift_date == day


def test_as_cap_open_shift_and_no_shift_use_the_local_date():
    """S-4 / AS-NGT-02: an OPEN shift or no shift never refuses a punch; the first IN of a date keeps its local date
    even before open_day_start_time."""
    at = ist(2026, 11, 9, 2, 30)
    assert punch_pairing.shift_date([], at, "IN", IST)[0] == dt.date(2026, 11, 9)
    day, instance = punch_pairing.shift_date(around(FIELD, dt.date(2026, 11, 9)), at, "IN", IST)
    assert day == dt.date(2026, 11, 9) and instance.kind == "OPEN"
    assert punch_pairing.open_window_date(at, IST, live_session_date=dt.date(2026, 11, 8)) == dt.date(2026, 11, 8)


def _device(company_id=7, device_id=3):
    return SimpleNamespace(company_id=company_id, device_id=device_id)


@pytest.fixture
def capture(monkeypatch):
    import os

    for key, value in {"DATABASE_URL": "postgresql://x:y@localhost:1/z", "SYSTEM_DATABASE_URL":
                       "postgresql://x:y@localhost:1/z", "SECRET_KEY": "pure-test-secret-key-long-enough-for-hs256",
                       "ENVIRONMENT": "test"}.items():
        os.environ.setdefault(key, value)
    from app.services import attendance_capture

    return attendance_capture


def test_as_cap_07_kiosk_ticket_expires_after_60_seconds_and_is_bound_to_its_device(capture):
    """AS-CAP-07 (a) 70 s later -> 410 KIOSK_TICKET_EXPIRED; (d) another kiosk -> 401 DEVICE_AUTH_FAILED; a tampered
    ticket -> 401."""
    from app.core.errors import AppError

    now = dt.datetime(2026, 11, 3, 13, 52, 10, tzinfo=dt.timezone.utc)
    issued = capture.issue_ticket(_device(), 41, "PIN", None, now)
    ticket = capture.read_ticket(_device(), issued["ticket"], now + dt.timedelta(seconds=59))
    assert (ticket.employee_id, ticket.method) == (41, "PIN") and len(ticket.nonce) == 32
    with pytest.raises(AppError) as late:
        capture.read_ticket(_device(), issued["ticket"], now + dt.timedelta(seconds=70))
    assert (late.value.status_code, late.value.code) == (410, "KIOSK_TICKET_EXPIRED")
    with pytest.raises(AppError) as other:
        capture.read_ticket(_device(device_id=4), issued["ticket"], now)
    assert (other.value.status_code, other.value.code) == (401, "DEVICE_AUTH_FAILED")
    version, body, signature = issued["ticket"].split(".")
    with pytest.raises(AppError) as forged:
        capture.read_ticket(_device(), f"{version}.{body}.{'0' * len(signature)}", now)
    assert forged.value.code == "DEVICE_AUTH_FAILED"


def test_kiosk_pin_canonical_form(capture):
    """M-11: trim, NFKC, upper case; numeric values without leading zeros (PIN 04417 is 4417)."""
    assert capture.canonical_value(" 04417 ") == "4417"
    assert capture.canonical_value("ａｂ12") == "AB12"
    assert capture.canonical_value("0000") == "0"
