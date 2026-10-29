"""AS-M7 device protocol, pure (04-attendance/01 §7.3 M-11, §8.2 H-5 / H-8, §8.4 T-1..T-3, §7.2 health).

AS-DEV-03 / GV-AS-CAP-11 (fall-back hour in log order), AS-DEV-11 (canonical PINs), AS-TZ-05 (device zone, never the
server's), AS-DEV-05 (future-dated limit lives in ingest), T-3 spring-forward, ATTLOG parsing and rejects, natural
keys (P-2 / P-5a), the command allow-list text and FIRST_LAST selection with device roles.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import device_protocol as proto


def test_as_dev_11_canonical_user_ids():
    assert proto.canonical_user_id("ZKTECO", " 00417 ") == "417"
    assert proto.canonical_user_id("ESSL", "0042") == proto.canonical_user_id("ESSL", "42") == "42"
    assert proto.canonical_user_id("ZKTECO", "0000") == "0"
    assert proto.canonical_user_id("SUPREMA", " alp-0042 ") == "ALP-0042"
    assert proto.canonical_user_id(None, "ｅ１０１") == "E101"  # NFKC full-width
    with pytest.raises(proto.InvalidUserId):
        proto.canonical_user_id("ZKTECO", "ALP-0042")
    with pytest.raises(proto.InvalidUserId):
        proto.canonical_user_id("OTHER", "  ")
    assert proto.lenient_user_id("ZKTECO", "ab12") == "AB12"  # ingest never drops a punch (I-2)


def test_h5_attlog_lines_and_h8_rejects():
    body = ("101\t2026-11-11 09:01:02\t0\t1\t0\t0\n"
            "\n"
            "0042\t2026-11-11 18:05:00\t1\t15\n"
            "\t2026-11-11 09:00:00\t0\t1\n"
            "205\t2026-13-01 09:00:00\t0\t1\n"
            "206\t2026-11-11 09:00:00\tX\t1\n"
            "207\n")
    lines, rejects = proto.parse_attlog(body)
    assert [(l.line_no, l.pin, l.status, l.verify) for l in lines] == [(1, "101", 0, 1), (3, "0042", 1, 15)]
    assert lines[0].local_ts == datetime(2026, 11, 11, 9, 1, 2) and lines[0].work_code is None
    assert [(r.line_no, r.reason) for r in rejects] == [(4, "MISSING_PIN"), (5, "INVALID_DATETIME"),
                                                         (6, "INVALID_STATUS"), (7, "TOO_FEW_FIELDS")]


def test_t1_device_zone_conversion_as_tz_05():
    """The registry zone converts, never the server's; +05:30 cannot be an integer-hour handshake TimeZone."""
    assert proto.to_instant(datetime(2026, 11, 11, 9, 0), "Asia/Kolkata") == datetime(2026, 11, 11, 3, 30,
                                                                                         tzinfo=timezone.utc)
    assert proto.to_instant(datetime(2026, 11, 11, 7, 58), "Asia/Dubai") == datetime(2026, 11, 11, 3, 58,
                                                                                       tzinfo=timezone.utc)


def test_gv_as_cap_11_fall_back_hour_in_log_order():
    """Carlos (America/New_York), Sat 2026-10-31 night: BREAK_START 01:45 fold 0, BREAK_END 01:15 fold 1."""
    zone = "America/New_York"
    loc = proto.Localizer(zone)
    in_at, _ = loc.instant(datetime(2026, 10, 31, 21, 58))
    brk_start, _ = loc.instant(datetime(2026, 11, 1, 1, 45))
    brk_end, _ = loc.instant(datetime(2026, 11, 1, 1, 15))
    out_at, _ = loc.instant(datetime(2026, 11, 1, 6, 3))
    utc = timezone.utc
    assert in_at == datetime(2026, 11, 1, 1, 58, tzinfo=utc)
    assert brk_start == datetime(2026, 11, 1, 5, 45, tzinfo=utc)
    assert brk_end == datetime(2026, 11, 1, 6, 15, tzinfo=utc)
    assert out_at == datetime(2026, 11, 1, 11, 3, tzinfo=utc)
    assert (brk_end - brk_start) == timedelta(minutes=30)
    assert (out_at - in_at) == timedelta(minutes=545)


def test_t2_real_time_push_takes_the_occurrence_nearest_receipt():
    received = datetime(2026, 11, 1, 6, 20, tzinfo=timezone.utc)  # 01:20 EST, second occurrence
    loc = proto.Localizer("America/New_York", real_time=True, received_at=received)
    assert loc.instant(datetime(2026, 11, 1, 1, 19))[0] == datetime(2026, 11, 1, 6, 19, tzinfo=timezone.utc)


def test_t3_spring_forward_gap_uses_the_offset_before_and_flags_skew():
    at, flags = proto.Localizer("America/New_York").instant(datetime(2026, 3, 8, 2, 30))
    assert at == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc) and flags == ("CLOCK_SKEW",)


def test_direction_modes_and_verify_codes():
    assert proto.direction_of("DEVICE_STATE", 0) == "IN" and proto.direction_of("DEVICE_STATE", 5) == "OUT"
    assert proto.direction_of("DEVICE_STATE", 2) == "BREAK_START"
    assert proto.direction_of("DEVICE_STATE", 255) == "UNKNOWN"
    assert proto.direction_of("FIRST_LAST", 0) == "UNKNOWN"  # pairing decides (P-5b)
    assert proto.verify_mode_of(1) == "FINGER" and proto.verify_mode_of(15) == "FACE"
    assert proto.verify_mode_of(99) == "OTHER" and proto.verify_mode_of(None) is None


def test_p2_device_keys_have_no_server_value_and_p5a_import_keys_include_direction():
    ts = datetime(2026, 11, 11, 9, 1, 2)
    a = proto.device_natural_key(7, 3, "101", ts, 1, 0)
    assert a == proto.device_natural_key(7, 3, "101", ts, 1, 0)
    assert a != proto.device_natural_key(7, 4, "101", ts, 1, 0)
    at = datetime(2026, 11, 11, 3, 31, tzinfo=timezone.utc)
    assert proto.import_natural_key(7, None, "417", "IN", at) != proto.import_natural_key(7, None, "417", "OUT", at)
    assert proto.import_natural_key(7, None, "417", "IN", at) != proto.import_natural_key(7, None, "418", "IN", at)
    assert proto.import_natural_key(7, 5, None, "IN", at) == proto.import_natural_key(7, 5, "x", "IN", at)


def test_health_chips():
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    assert proto.health(None, None, now) == "UNKNOWN"
    assert proto.health(now - timedelta(minutes=5), 0, now) == "ONLINE"
    assert proto.health(now - timedelta(minutes=45), 0, now) == "STALE"
    assert proto.health(now - timedelta(minutes=95), 0, now) == "OFFLINE"
    assert proto.health(now - timedelta(minutes=1), 400, now) == "CLOCK_SKEW"


def test_command_allow_list_lines():
    local = datetime(2026, 11, 11, 9, 0)
    assert proto.command_line(5, "SYNC_TIME", {}, device_now_local=local) == "C:5:SET OPTIONS DateTime=2026-11-11 09:00:00"
    assert proto.command_line(6, "DELETE_USER", {"device_user_id": "417"},
                              device_now_local=local) == "C:6:DATA DELETE USERINFO PIN=417"
    assert "Name=Sameer Khan" in proto.command_line(7, "UPLOAD_USER", {"device_user_id": "4", "name": "Sameer Khan"},
                                                    device_now_local=local)
    with pytest.raises(ValueError):
        proto.command_line(8, "CLEAR_LOG", {}, device_now_local=local)  # never emitted (SR-AS-04)
    assert proto.parse_devicecmd("ID=5&Return=0&CMD=DATA\nID=x&Return=0\nID=6&Return=-1002") == [(5, "0"), (6, "-1002")]


def test_first_last_respects_device_roles():
    t = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
    rows = [(t, "UNKNOWN", "IN_OUT"), (t + timedelta(hours=4), "UNKNOWN", "IN_OUT"),
            (t + timedelta(hours=9), "UNKNOWN", "IN_OUT")]
    assert proto.first_last(rows) == (0, 2)
    assert proto.first_last([(t, "UNKNOWN", "OUT_ONLY"), (t + timedelta(hours=8), "UNKNOWN", "IN_ONLY")]) == (1, None)
    assert proto.first_last([(t, "OUT", "IN_OUT")]) == (None, None)
    assert proto.first_last([(t, "IN", "IN_OUT")]) == (0, None)


def test_backfill_start():
    assert proto.backfill_from(date(2026, 11, 11)) == date(2026, 11, 4)
