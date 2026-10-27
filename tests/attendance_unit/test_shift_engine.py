"""Pure shift engine (WI-AS-1.2): golden vectors GV-AS-SHF-* and the pure AS-SHF / AS-TZ cases.

Owner doc ``docs/attendance-shift/03-shifts/01-shift-definitions.md`` (§6 halves, §8 anchor zone,
§9 instances, §10 scheduled minutes, §21 vectors); cast of spine §0.3 / owner doc §2.4. No database.
"""

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
import pytz

from app.services import shift_engine as se

B = se.BreakRuleData
V = se.ShiftVersionData
S = se.SegmentData
IST, NY, GST = "Asia/Kolkata", "America/New_York", "Asia/Dubai"
D = dt.date


def utc(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc)


def local(instant, zone):
    return instant.astimezone(pytz.timezone(zone)).strftime("%Y-%m-%d %H:%M")


def clock(instant, zone):
    return instant.astimezone(pytz.timezone(zone)).strftime("%H:%M")


def lunch_fixed(start, end, minutes=60, paid=False):
    return B("LUNCH", "FIXED_WINDOW", minutes, paid, start=start, end=end)


def floating(kind, start, end, minutes, paid, next_day=False):
    return B(kind, "FLOATING_WINDOW", minutes, paid, window_start=start, window_end=end, next_day=next_day)


# Cast catalogue (owner doc §2.4; versions valid on 2026-11-01)
GEN = V(start_time="09:00", end_time="18:00", break_rules=(lunch_fixed("13:00", "14:00"),), shift_id=101, shift_version_id=1001)
A = V(start_time="06:00", end_time="14:00", break_rules=(floating("LUNCH", "09:30", "11:30", 30, True),), grace_in_minutes=10)
B_SHIFT = V(start_time="14:00", end_time="22:00", break_rules=(floating("LUNCH", "17:30", "19:30", 30, True),), grace_in_minutes=10)
C = V(start_time="22:00", end_time="06:00", break_rules=(floating("LUNCH", "01:30", "03:30", 30, True, next_day=True),))
FLEX_BLR = V(kind="FLEXIBLE", start_time="08:00", end_time="20:00", core_start_time="11:00", core_end_time="16:00",
             expected_net_minutes=480,
             break_rules=(B("LUNCH", "AUTO_DEDUCT", 45, False, auto_deduct_after_minutes=360),))
US_E1 = V(start_time="09:00", end_time="18:00", anchor_timezone=NY,
          break_rules=(floating("LUNCH", "12:00", "14:00", 60, False),))
FIELD = V(kind="OPEN", expected_net_minutes=480)
M6 = V(start_time="08:00", end_time="14:00", break_rules=(floating("REST", "10:00", "12:00", 15, True),))
E6 = V(start_time="14:00", end_time="20:00", break_rules=(floating("REST", "16:00", "18:00", 15, True),))
N12 = V(start_time="20:00", end_time="08:00", break_rules=(floating("REST", "00:00", "04:00", 15, True, next_day=True),))
SPD = V(kind="SPLIT", start_time="08:00", end_time="20:00",
        segments=({"kind": "WORK", "start": "08:00", "end": "12:00", "next_day": False},
                  {"kind": "WORK", "start": "16:00", "end": "20:00", "next_day": False}))
OC = V(start_time="20:00", end_time="08:00", segments=({"kind": "ON_CALL", "start": "20:00", "end": "08:00", "next_day": True},))
SITE_D = V(start_time="08:00", end_time="17:00", break_rules=(lunch_fixed("13:00", "14:00"),), outdoor=True)
BD9 = V(start_time="10:00", end_time="19:00", break_rules=(lunch_fixed("14:00", "15:00"),))
BSPLIT = V(kind="SPLIT", start_time="10:00", end_time="22:00",
           segments=(S("WORK", "10:00", "14:00"), S("WORK", "17:00", "22:00")))
BC12D = V(start_time="07:00", end_time="19:00", break_rules=(floating("REST", "12:00", "14:00", 30, True),))
BC12N = V(start_time="19:00", end_time="07:00", break_rules=(floating("REST", "00:00", "04:00", 30, True, next_day=True),))
NY_OPEN = V(start_time="08:30", end_time="17:00", break_rules=(B("LUNCH", "PUNCHED", 30, False),))
NY_CLOSE = V(start_time="13:30", end_time="22:00", break_rules=(B("LUNCH", "PUNCHED", 30, False),))
WH_NIGHT = V(start_time="22:00", end_time="06:00", break_rules=(B("LUNCH", "PUNCHED", 30, False),))
GENERAL_SHIFT = V(start_time="09:00", end_time="18:00", unpaid_break_minutes=None)

CAST = [GEN, A, B_SHIFT, C, FLEX_BLR, US_E1, FIELD, M6, E6, N12, SPD, OC, SITE_D, BD9, BSPLIT, BC12D, BC12N,
        NY_OPEN, NY_CLOSE, WH_NIGHT, GENERAL_SHIFT]


def test_cast_catalogue_validates():
    for version in CAST:
        assert se.validate_version(version) == (), version


# --------------------------------------------------------------------------- golden vectors (§21)


def test_gv_as_shf_01_gen_fixed_lunch_halves():
    inst = se.build_instance(GEN, D(2026, 11, 5), IST)
    assert (inst.start_at, inst.end_at) == (utc(2026, 11, 5, 3, 30), utc(2026, 11, 5, 12, 30))
    assert inst.scheduled_net_minutes == 480 and inst.gross_minutes == 540
    assert se.scheduled_net_minutes(GEN, D(2026, 11, 5), IST) == 480
    h1, h2 = inst.first_half, inst.second_half
    assert (clock(h1.start_at, IST), clock(h1.end_at, IST), h1.minutes) == ("09:00", "13:00", 240)
    assert (clock(h2.start_at, IST), clock(h2.end_at, IST), h2.minutes) == ("14:00", "18:00", 240)
    assert se.HALVES_FIXED_BREAK in inst.explain  # halves rule 2
    assert se.half_boundary(inst) == utc(2026, 11, 5, 7, 30)
    assert (inst.shift_id, inst.shift_version_id, inst.crosses_midnight) == (101, 1001, False)


@pytest.mark.parametrize("shift_date, gross, scheduled", [
    (D(2026, 10, 31), 540, 510),  # DST ends Sun 2026-11-01 02:00
    (D(2027, 3, 13), 420, 390),  # DST starts Sun 2027-03-14 02:00
    (D(2026, 11, 7), 480, 450),
])
def test_gv_as_shf_02_wh_night_dst_nights(shift_date, gross, scheduled):
    inst = se.build_instance(WH_NIGHT, shift_date, NY)
    assert inst.gross_minutes == gross
    assert inst.scheduled_net_minutes == scheduled == se.scheduled_net_minutes(WH_NIGHT, shift_date, NY)
    assert inst.crosses_midnight and inst.shift_date == shift_date


def test_gv_as_shf_02_wh_night_centred_break_halves_on_dst_end():
    inst = se.build_instance(WH_NIGHT, D(2026, 10, 31), NY)
    assert (inst.start_at, inst.end_at) == (utc(2026, 11, 1, 2), utc(2026, 11, 1, 11))
    assert inst.first_half == se.HalfData(utc(2026, 11, 1, 2), utc(2026, 11, 1, 6, 15), 255)
    assert inst.second_half == se.HalfData(utc(2026, 11, 1, 6, 45), utc(2026, 11, 1, 11), 255)
    assert se.HALVES_CENTRED in inst.explain
    # HOUR-type FULL leave charges 8.50 h (LH DST vector)
    assert Decimal(inst.scheduled_net_minutes) / 60 == Decimal("8.5")


@pytest.mark.parametrize("shift_date, start, end, start_utc", [
    (D(2026, 10, 26), "2026-10-26 18:30", "2026-10-27 03:30", utc(2026, 10, 26, 13)),  # New York on EDT
    (D(2026, 11, 2), "2026-11-02 19:30", "2026-11-03 04:30", utc(2026, 11, 2, 14)),  # New York on EST
])
def test_gv_as_shf_03_us_e1_anchored_new_york(shift_date, start, end, start_utc):
    inst = se.build_instance(US_E1, shift_date, IST)
    assert (local(inst.start_at, IST), local(inst.end_at, IST)) == (start, end)
    assert inst.start_at == start_utc
    assert inst.shift_date == shift_date and inst.anchor_date == shift_date  # shift date = IST start date
    assert (inst.clock_timezone, inst.work_timezone) == (NY, IST)
    assert inst.gross_minutes == 540 and inst.scheduled_net_minutes == 480
    lunch = inst.breaks[0]  # the floating lunch window is in New York time too
    assert (clock(lunch.window_start_at, NY), clock(lunch.window_end_at, NY)) == ("12:00", "14:00")


def _tiles(versions):
    ordered = sorted(versions, key=lambda v: v.effective_from)
    return ordered[-1].effective_to is None and all(
        a.effective_to is not None and a.effective_to + dt.timedelta(days=1) == b.effective_from
        for a, b in zip(ordered, ordered[1:]))


def test_gv_as_shf_04_bd9_temporary_ramadan_version():
    """Engine part of the vector: the three versions tile and schedule 480 -> 360 -> 480 (the save is AS service)."""
    ramadan = V(start_time="10:00", end_time="16:00")
    versions = [
        V(**{**BD9.__dict__, "version_no": 1, "effective_from": D(2000, 1, 1), "effective_to": D(2027, 2, 7)}),
        V(**{**ramadan.__dict__, "version_no": 2, "effective_from": D(2027, 2, 8), "effective_to": D(2027, 3, 9)}),
        V(**{**BD9.__dict__, "version_no": 3, "effective_from": D(2027, 3, 10), "effective_to": None}),
    ]
    assert _tiles(versions)
    minutes = {}
    for day in (D(2027, 2, 7), D(2027, 2, 8), D(2027, 3, 9), D(2027, 3, 10)):
        version = se.version_on(versions, day)
        minutes[day] = (version.version_no, se.scheduled_net_minutes(version, day, GST))
    assert minutes == {D(2027, 2, 7): (1, 480), D(2027, 2, 8): (2, 360), D(2027, 3, 9): (2, 360), D(2027, 3, 10): (3, 480)}
    assert se.version_on(versions, D(1999, 12, 31)) is None


def test_gv_as_shf_04b_day_0900_version_keeps_break():
    day_0900 = V(start_time="09:00", end_time="18:00", break_rules=(lunch_fixed("13:00", "14:00"),),
                 effective_from=D(2000, 1, 1), effective_to=D(2027, 2, 7))
    short = V(start_time="09:00", end_time="16:00", break_rules=(lunch_fixed("13:00", "14:00"),),
              effective_from=D(2027, 2, 8), effective_to=D(2027, 3, 9))
    assert se.validate_version(short) == ()
    assert se.scheduled_net_minutes(se.version_on([day_0900, short], D(2027, 2, 7)), D(2027, 2, 7), GST) == 480
    leave_day = D(2027, 2, 16)
    minutes = se.scheduled_net_minutes(se.version_on([day_0900, short], leave_day), leave_day, GST)
    assert minutes == 360
    assert (Decimal(minutes) / 60).quantize(Decimal("0.01")) == Decimal("6.00")  # FULL HOUR-type leave = 6.00 h


def test_gv_as_shf_05_split_halves_and_mirrors():
    spd = se.build_instance(SPD, D(2026, 11, 3), IST)
    assert spd.scheduled_net_minutes == 480
    assert (clock(spd.first_half.start_at, IST), clock(spd.first_half.end_at, IST), spd.first_half.minutes) == ("08:00", "12:00", 240)
    assert (clock(spd.second_half.start_at, IST), clock(spd.second_half.end_at, IST), spd.second_half.minutes) == ("16:00", "20:00", 240)
    bsplit = se.build_instance(BSPLIT, D(2026, 11, 6), GST)
    assert bsplit.scheduled_net_minutes == 540
    assert (bsplit.first_half.minutes, bsplit.second_half.minutes) == (240, 300)
    assert (clock(bsplit.second_half.start_at, GST), clock(bsplit.second_half.end_at, GST)) == ("17:00", "22:00")
    assert se.HALVES_SPLIT_SEGMENTS in bsplit.explain
    assert se.legacy_mirror(SPD, D(2026, 11, 2), IST) == se.LegacyMirror("08:00", "20:00", Decimal("8.00"), None, False)
    assert se.legacy_mirror(BSPLIT, D(2026, 11, 2), GST) == se.LegacyMirror("10:00", "22:00", Decimal("9.00"), None, False)


def test_gv_as_shf_06_site_d_unequal_halves():
    inst = se.build_instance(SITE_D, D(2026, 11, 4), IST)
    assert inst.scheduled_net_minutes == 480
    assert (clock(inst.first_half.start_at, IST), clock(inst.first_half.end_at, IST), inst.first_half.minutes) == ("08:00", "13:00", 300)
    assert (clock(inst.second_half.start_at, IST), clock(inst.second_half.end_at, IST), inst.second_half.minutes) == ("14:00", "17:00", 180)


def test_gv_as_shf_07_flex_blr_core_midpoint_and_fallback():
    inst = se.build_instance(FLEX_BLR, D(2026, 11, 2), IST)
    assert inst.scheduled_net_minutes == 480 and inst.gross_minutes is None and inst.segments == ()
    assert clock(se.half_boundary(inst), IST) == "13:30"
    assert (inst.first_half.minutes, inst.second_half.minutes) == (240, 240)
    assert (clock(inst.core_start_at, IST), clock(inst.core_end_at, IST)) == ("11:00", "16:00")
    no_expected = V(**{**FLEX_BLR.__dict__, "expected_net_minutes": None})
    assert se.build_instance(no_expected, D(2026, 11, 2), IST).scheduled_net_minutes is None  # unknown, never 0
    assert se.scheduled_net_minutes(no_expected, D(2026, 11, 2), IST, fte=Decimal("0.5"), standard_minutes=480) == 240
    with pytest.raises(se.ShiftEngineError) as err:
        se.scheduled_net_minutes(no_expected, D(2026, 11, 2), IST)
    assert err.value.code == "STANDARD_MINUTES_REQUIRED"


def test_gv_as_shf_08_on_call_scheduled_zero():
    inst = se.build_instance(OC, D(2026, 11, 7), IST)
    assert inst.scheduled_net_minutes == 0 and inst.gross_minutes == 0
    assert (inst.start_at, inst.end_at) == (utc(2026, 11, 7, 14, 30), utc(2026, 11, 8, 2, 30))
    assert (local(inst.match_start_at, IST), local(inst.match_end_at, IST)) == ("2026-11-07 18:00", "2026-11-08 12:00")
    assert [s.kind for s in inst.segments] == ["ON_CALL"]
    # §6.3: no WORK minutes, halves 20:00-02:00 / 02:00-08:00 with 0 / 0
    assert (clock(inst.first_half.end_at, IST), inst.first_half.minutes, inst.second_half.minutes) == ("02:00", 0, 0)


def test_gv_as_shf_12_dst_gap_and_overlap_starts():
    gap = se.build_instance(V(start_time="02:30", end_time="10:30"), D(2027, 3, 14), NY)
    assert gap.start_at == utc(2027, 3, 14, 7, 30) and gap.end_at == utc(2027, 3, 14, 14, 30)
    assert gap.gross_minutes == 420 and gap.scheduled_net_minutes == 420
    assert f"{se.DST_GAP_FORWARD}:segments[0].start" in gap.explain
    overlap = se.build_instance(V(start_time="01:30", end_time="09:30"), D(2026, 11, 1), NY)
    assert overlap.start_at == utc(2026, 11, 1, 5, 30) and overlap.end_at == utc(2026, 11, 1, 14, 30)  # fold = 0
    assert overlap.gross_minutes == 540
    assert f"{se.DST_AMBIGUOUS_FIRST}:segments[0].start" in overlap.explain


def test_gv_as_shf_13_mirrors_by_kind():
    on = D(2026, 11, 2)
    assert se.legacy_mirror(FLEX_BLR, on, IST) == se.LegacyMirror("08:00", "20:00", Decimal("8.75"), 45, True)
    assert se.legacy_mirror(FIELD, on, IST) == se.LegacyMirror("04:00", "04:00", Decimal("8.00"), 0, True)
    assert se.legacy_mirror(SPD, on, IST) == se.LegacyMirror("08:00", "20:00", Decimal("8.00"), None, False)
    assert se.legacy_mirror(US_E1, on, IST) == se.LegacyMirror("19:30", "04:30", Decimal("9.00"), 60, False)
    # §2.4 mirror column: the anchored mirror follows the owner's local today (EDT week)
    assert se.legacy_mirror(US_E1, D(2026, 10, 26), IST).start_time == "18:30"
    assert se.legacy_mirror(GEN, on, IST) == se.LegacyMirror("09:00", "18:00", Decimal("9.00"), 60, False)
    assert se.legacy_mirror(A, on, IST) == se.LegacyMirror("06:00", "14:00", Decimal("8.00"), 0, False)
    assert se.legacy_mirror(OC, on, IST).duration_hours == Decimal("12.00")  # all-ON_CALL mirrors its span
    assert se.legacy_mirror(GENERAL_SHIFT, on, IST) == se.LegacyMirror("09:00", "18:00", Decimal("9.00"), None, False)


def test_gv_as_shf_15_bulk_field_scheduled_minutes():
    """Engine part only: Nitin's scheduled minutes move 540 (placement GENERAL_SHIFT) -> 480 (FIELD rule)."""
    day = D(2026, 11, 2)
    assert se.scheduled_net_minutes(GENERAL_SHIFT, day, IST) == 540
    assert se.scheduled_net_minutes(FIELD, day, IST) == 480


def test_gv_as_shf_22_fixed_window_break_across_dst():
    gap_night = V(start_time="22:00", end_time="06:00",
                  break_rules=(B("LUNCH", "FIXED_WINDOW", 60, False, start="02:00", end="03:00", next_day=True),))
    a = se.build_instance(gap_night, D(2027, 3, 13), NY)
    assert (a.breaks[0].start_at, a.breaks[0].end_at) == (utc(2027, 3, 14, 7), utc(2027, 3, 14, 8))
    assert (a.start_at, a.end_at, a.gross_minutes, a.scheduled_net_minutes) == (utc(2027, 3, 14, 3), utc(2027, 3, 14, 10), 420, 360)
    overlap_night = V(start_time="22:00", end_time="06:00",
                      break_rules=(B("LUNCH", "FIXED_WINDOW", 60, False, start="01:00", end="02:00", next_day=True),))
    b = se.build_instance(overlap_night, D(2026, 10, 31), NY)
    assert (b.breaks[0].start_at, b.breaks[0].end_at) == (utc(2026, 11, 1, 5), utc(2026, 11, 1, 6))
    assert (b.start_at, b.end_at, b.gross_minutes, b.scheduled_net_minutes) == (utc(2026, 11, 1, 2), utc(2026, 11, 1, 11), 540, 480)
    for inst in (a, b):
        assert inst.breaks[0].end_at - inst.breaks[0].start_at == dt.timedelta(minutes=60)


def test_gv_as_shf_24_long_duty_24h():
    duty24 = V(start_time="08:00", end_time="08:00", long_duty=True)
    assert se.validate_version(duty24) == ()
    ist = se.build_instance(duty24, D(2026, 11, 2), IST)
    assert ist.start_at == utc(2026, 11, 2, 2, 30) and ist.gross_minutes == 1440 and ist.crosses_midnight
    assert (local(ist.match_start_at, IST), local(ist.match_end_at, IST)) == ("2026-11-02 06:00", "2026-11-03 12:00")
    fall = se.build_instance(duty24, D(2026, 10, 31), NY)
    assert (fall.start_at, fall.end_at, fall.gross_minutes) == (utc(2026, 10, 31, 12), utc(2026, 11, 1, 13), 1500)
    spring = se.build_instance(duty24, D(2027, 3, 13), NY)
    assert (spring.start_at, spring.end_at, spring.gross_minutes) == (utc(2027, 3, 13, 13), utc(2027, 3, 14, 12), 1380)
    assert se.legacy_mirror(duty24, D(2026, 11, 2), IST).duration_hours == Decimal("24.00")


# --------------------------------------------------------------------------- edge cases (pure ones of AS-M1)


def test_as_shf_01_temporary_version_scheduled_minutes():
    """Pure part: v1 480 / v2 360 / v3 480 by date; the save, return copy and leave recount are service work."""
    v1 = V(**{**BD9.__dict__, "effective_from": D(2000, 1, 1), "effective_to": D(2027, 2, 7)})
    v2 = V(start_time="10:00", end_time="16:00", effective_from=D(2027, 2, 8), effective_to=D(2027, 3, 9))
    v3 = V(**{**BD9.__dict__, "effective_from": D(2027, 3, 10)})
    assert _tiles([v1, v2, v3])
    days = {D(2027, 2, 5): 480, D(2027, 2, 16): 360, D(2027, 3, 5): 360, D(2027, 3, 10): 480}
    for day, expected in days.items():
        assert se.scheduled_net_minutes(se.version_on([v1, v2, v3], day), day, GST) == expected


def test_as_shf_08_long_duty_validation_and_mirror():
    issues = se.validate_version(V(start_time="08:00", end_time="08:00"))
    assert [(i.field, i.code) for i in issues] == [("end_time", "EQUALS_START_TIME")]
    split_duty = V(kind="SPLIT", start_time="08:00", end_time="08:00", long_duty=True,
                   segments=(S("WORK", "08:00", "20:00"), S("WORK", "20:30", "08:00")))
    assert "long_duty" in {i.field for i in se.validate_version(split_duty)}
    assert se.validate_version(V(start_time="08:00", end_time="09:00", long_duty=True))[0].code == "LONG_DUTY_INVALID"
    duty24 = V(start_time="08:00", end_time="08:00", long_duty=True)
    assert duty24.crosses_midnight is True


def test_as_shf_11_fixed_window_length_survives_dst():
    gap_night = V(start_time="22:00", end_time="06:00",
                  break_rules=(B("LUNCH", "FIXED_WINDOW", 60, False, start="02:00", end="03:00", next_day=True),))
    inst = se.build_instance(gap_night, D(2027, 3, 13), NY)
    brk = inst.breaks[0]
    assert (clock(brk.start_at, NY), clock(brk.end_at, NY)) == ("03:00", "04:00")  # moved forward, still 60 min
    assert inst.scheduled_net_minutes == inst.gross_minutes - 60
    # Clipped when the moved window would run past its WORK segment
    near_end = V(start_time="20:00", end_time="03:30",
                 break_rules=(B("LUNCH", "FIXED_WINDOW", 60, False, start="02:15", end="03:15", next_day=True),))
    assert se.validate_version(near_end) == ()
    clipped = se.build_instance(near_end, D(2027, 3, 13), NY)
    assert clipped.breaks[0].start_at == utc(2027, 3, 14, 7, 15)
    assert clipped.breaks[0].end_at == clipped.end_at == utc(2027, 3, 14, 7, 30)
    assert f"{se.BREAK_WINDOW_CLIPPED}:0" in clipped.explain


def test_as_shf_16_flex_blr_band_matching_window():
    inst = se.build_instance(FLEX_BLR, D(2026, 11, 2), IST)
    assert (local(inst.match_start_at, IST), local(inst.match_end_at, IST)) == ("2026-11-02 06:00", "2026-11-03 00:00")
    punch_in = pytz.timezone(IST).localize(dt.datetime(2026, 11, 2, 6, 40))
    punch_out = pytz.timezone(IST).localize(dt.datetime(2026, 11, 2, 22, 30))
    assert inst.match_start_at <= punch_in <= inst.match_end_at
    assert inst.match_start_at <= punch_out <= inst.match_end_at  # both attach to Monday


def test_as_shf_19_invalid_definitions_refused():
    def fields(version):
        return {(i.field, i.code) for i in se.validate_version(version)}

    in_gap = V(**{**BSPLIT.__dict__, "break_rules": (lunch_fixed("14:30", "15:00", minutes=30),)})
    assert ("break_rules[0]", "BREAK_OUTSIDE_WORK") in fields(in_gap)  # (a)
    split_in_lunch = V(**{**GEN.__dict__, "half_split_time": "13:30"})
    assert ("half_split_time", "INSIDE_FIXED_BREAK") in fields(split_in_lunch)  # (b)
    all_unpaid = V(start_time="09:00", end_time="18:00",
                   break_rules=tuple(B("LUNCH" if i == 0 else "REST", "PUNCHED", 180, False) for i in range(3)))
    assert ("break_rules", "UNPAID_NOT_LESS_THAN_WORK") in fields(all_unpaid)  # (c)
    short_gap = V(kind="SPLIT", start_time="10:00", end_time="22:00",
                  segments=(S("WORK", "10:00", "14:00"), S("WORK", "14:25", "22:00")))
    assert ("segments[1].start", "SPLIT_GAP_TOO_SHORT") in fields(short_gap)  # (d)
    # (e) kind lock and (f) archived code re-use need the catalogue (DB service)


def test_as_tz_02_zone_transfer_switch_rule():
    effective = D(2026, 11, 16)  # Kiran Joshi, Pune -> Dubai
    assert se.zone_change_shift_date(utc(2026, 11, 15, 18, 20), effective, IST, GST) == se.ZoneSwitch("OLD", IST, D(2026, 11, 15))
    assert se.zone_change_shift_date(utc(2026, 11, 15, 18, 50), effective, IST, GST) == se.ZoneSwitch("NEW", GST, D(2026, 11, 16))
    # reverse move Dubai -> Pune: 19:00Z is Sun 23:00 GST, still the old placement
    assert se.zone_change_shift_date(utc(2026, 11, 15, 19), effective, GST, IST) == se.ZoneSwitch("OLD", GST, D(2026, 11, 15))


@pytest.mark.parametrize("shift_date, start, end", [
    (D(2026, 10, 30), "2026-10-30 18:30", "2026-10-31 03:30"),
    (D(2026, 11, 2), "2026-11-02 19:30", "2026-11-03 04:30"),
    (D(2027, 3, 12), "2027-03-12 19:30", "2027-03-13 04:30"),
    (D(2027, 3, 15), "2027-03-15 18:30", "2027-03-16 03:30"),
    (D(2026, 11, 1), "2026-11-01 19:30", "2026-11-02 04:30"),  # Sunday of the fall-back (09:00 is EST)
    (D(2027, 3, 14), "2027-03-14 18:30", "2027-03-15 03:30"),  # Sunday of the spring-forward (09:00 is EDT)
])
def test_as_tz_04_us_e1_both_clock_changes(shift_date, start, end):
    inst = se.build_instance(US_E1, shift_date, IST)
    assert inst.anchor_date == shift_date and inst.shift_date == shift_date
    assert (local(inst.start_at, IST), local(inst.end_at, IST)) == (start, end)
    assert inst.gross_minutes == 540 and inst.scheduled_net_minutes == 480


# --------------------------------------------------------------------------- worked examples (§6.3, §9.3, §9.6, §10)


@pytest.mark.parametrize("version, shift_date, zone, start_utc, end_utc, gross, scheduled", [
    (C, D(2026, 11, 2), IST, utc(2026, 11, 2, 16, 30), utc(2026, 11, 3, 0, 30), 480, 480),
    (US_E1, D(2026, 10, 26), IST, utc(2026, 10, 26, 13), utc(2026, 10, 26, 22), 540, 480),
    (N12, D(2026, 11, 6), IST, utc(2026, 11, 6, 14, 30), utc(2026, 11, 7, 2, 30), 720, 720),
])
def test_overnight_examples_section_9_3(version, shift_date, zone, start_utc, end_utc, gross, scheduled):
    inst = se.build_instance(version, shift_date, zone)
    assert (inst.start_at, inst.end_at, inst.gross_minutes, inst.scheduled_net_minutes) == (start_utc, end_utc, gross, scheduled)
    assert inst.crosses_midnight and inst.shift_date == shift_date


def test_c_shift_matching_window_takes_tuesday_0020():
    inst = se.build_instance(C, D(2026, 11, 2), IST)
    assert (local(inst.match_start_at, IST), local(inst.match_end_at, IST)) == ("2026-11-02 20:00", "2026-11-03 10:00")
    tue_0020 = pytz.timezone(IST).localize(dt.datetime(2026, 11, 3, 0, 20))
    assert inst.match_start_at <= tue_0020 <= inst.match_end_at
    lunch = inst.breaks[0]  # next_day floating window
    assert (local(lunch.window_start_at, IST), local(lunch.window_end_at, IST)) == ("2026-11-03 01:30", "2026-11-03 03:30")


@pytest.mark.parametrize("version, shift_date, zone, h1, h2, minutes", [
    (A, D(2026, 11, 4), IST, ("06:00", "10:00"), ("10:00", "14:00"), (240, 240)),
    (C, D(2026, 11, 2), IST, ("22:00", "02:00"), ("02:00", "06:00"), (240, 240)),
    (US_E1, D(2026, 10, 26), IST, ("18:30", "22:30"), ("23:30", "03:30"), (240, 240)),
    (WH_NIGHT, D(2026, 10, 31), NY, ("22:00", "01:15"), ("01:45", "06:00"), (255, 255)),
])
def test_half_examples_section_6_3(version, shift_date, zone, h1, h2, minutes):
    inst = se.build_instance(version, shift_date, zone)
    assert (clock(inst.first_half.start_at, zone), clock(inst.first_half.end_at, zone)) == h1
    assert (clock(inst.second_half.start_at, zone), clock(inst.second_half.end_at, zone)) == h2
    assert (inst.first_half.minutes, inst.second_half.minutes) == minutes


def test_open_field_halves_and_window():
    inst = se.build_instance(FIELD, D(2026, 11, 2), IST)
    assert inst.start_at is None and inst.end_at is None and inst.gross_minutes is None
    assert (inst.first_half.minutes, inst.second_half.minutes) == (240, 240)
    assert inst.first_half.start_at is None and se.HALVES_OPEN_WORKED in inst.explain
    assert (local(inst.match_start_at, IST), local(inst.match_end_at, IST)) == ("2026-11-02 04:00", "2026-11-03 04:00")
    with pytest.raises(se.ShiftEngineError) as err:
        se.half_boundary(inst)
    assert err.value.code == "NO_CLOCK_BOUNDARY"
    custom_day_start = se.build_instance(FIELD, D(2026, 11, 2), IST, open_day_start_time="05:30")
    assert local(custom_day_start.match_start_at, IST) == "2026-11-02 05:30"


@pytest.mark.parametrize("version, shift_date, zone, window", [
    (GEN, D(2026, 11, 5), IST, ("2026-11-05 07:00", "2026-11-05 22:00")),
    (SPD, D(2026, 11, 3), IST, ("2026-11-03 06:00", "2026-11-04 00:00")),
])
def test_matching_windows_section_9_6(version, shift_date, zone, window):
    inst = se.build_instance(version, shift_date, zone)
    assert (local(inst.match_start_at, zone), local(inst.match_end_at, zone)) == window


@pytest.mark.parametrize("version, zone, expected", [
    (GEN, IST, 480), (A, IST, 480), (B_SHIFT, IST, 480), (C, IST, 480), (FLEX_BLR, IST, 480), (FIELD, IST, 480),
    (SPD, IST, 480), (US_E1, IST, 480), (BD9, GST, 480), (SITE_D, IST, 480), (NY_OPEN, NY, 480), (NY_CLOSE, NY, 480),
    (WH_NIGHT, NY, 450), (M6, IST, 360), (E6, IST, 360), (N12, IST, 720), (BC12D, GST, 720), (BC12N, GST, 720),
    (BSPLIT, GST, 540), (OC, IST, 0), (GENERAL_SHIFT, IST, 540),
])
def test_cast_scheduled_minutes_section_10(version, zone, expected):
    assert se.scheduled_net_minutes(version, D(2026, 11, 11), zone) == expected


def test_scheduled_minutes_fte_and_no_shift():
    day = D(2026, 11, 2)
    assert se.scheduled_net_minutes(GEN, day, IST, fte=Decimal("0.75")) == 360
    assert se.scheduled_net_minutes(WH_NIGHT, D(2026, 11, 7), NY, fte=Decimal("0.5")) == 225
    assert se.scheduled_net_minutes(NY_OPEN, day, NY, fte=Decimal("0.333")) == 160  # 159.84 -> ROUND_HALF_UP
    assert se.scheduled_net_minutes(V(start_time="09:00", end_time="09:45"), day, IST, fte=Decimal("0.5")) == 23  # 22.5
    assert se.scheduled_net_minutes(None, day, IST, standard_minutes=480) == 480  # resolution NONE (Kavya)
    with pytest.raises(se.ShiftEngineError):
        se.scheduled_net_minutes(GEN, day, IST, fte=0.5)  # floats are refused


def test_half_split_time_wins():
    with_split = V(**{**GEN.__dict__, "half_split_time": "12:00"})
    assert se.validate_version(with_split) == ()
    inst = se.build_instance(with_split, D(2026, 11, 5), IST)
    assert clock(se.half_boundary(inst), IST) == "12:00"
    assert (inst.first_half.minutes, inst.second_half.minutes) == (180, 300)  # lunch 13-14 falls in H2
    assert se.HALVES_SPLIT_TIME in inst.explain


def test_split_first_segment_exceeds_half():
    long_first = V(kind="SPLIT", start_time="06:00", end_time="20:00",
                   segments=(S("WORK", "06:00", "13:00"), S("WORK", "18:00", "20:00")))
    inst = se.build_instance(long_first, D(2026, 11, 3), IST)
    assert inst.scheduled_net_minutes == 540
    assert clock(se.half_boundary(inst), IST) == "10:30"  # start + half of net (270)
    assert (inst.first_half.minutes, inst.second_half.minutes) == (270, 270)


def test_anchor_date_previous_and_next():
    # Anchor zone behind the work zone: 20:00 Los Angeles is next morning in India -> A = D - 1
    la = V(start_time="20:00", end_time="02:00", anchor_timezone="America/Los_Angeles")
    inst = se.build_instance(la, D(2026, 10, 26), IST)
    assert inst.anchor_date == D(2026, 10, 25) and local(inst.start_at, IST) == "2026-10-26 08:30"
    assert se.ANCHOR_DATE_PREVIOUS in inst.explain
    # Anchor zone ahead of the work zone: 09:00 Tokyo is the previous evening in New York -> A = D + 1
    tokyo = V(start_time="09:00", end_time="18:00", anchor_timezone="Asia/Tokyo")
    inst = se.build_instance(tokyo, D(2026, 10, 26), NY)
    assert inst.anchor_date == D(2026, 10, 27) and local(inst.start_at, NY) == "2026-10-26 20:00"
    assert se.ANCHOR_DATE_NEXT in inst.explain


def test_custom_time_entries():
    entry = se.RosterEntryData(roster_entry_id=61004, version=2, shift_id=101, shift_version_id=1001,
                               custom_start_time="10:00", custom_end_time="13:00")
    inst = se.build_instance(GEN, D(2026, 11, 4), IST, entry)
    assert (inst.roster_entry_id, inst.roster_version, inst.shift_id) == (61004, 2, 101)
    assert (clock(inst.start_at, IST), clock(inst.end_at, IST)) == ("10:00", "13:00")
    assert inst.breaks[0].mode == "PUNCHED" and f"{se.BREAK_AS_PUNCHED}:0" in inst.explain  # lunch outside the span
    assert inst.scheduled_net_minutes == 120  # the lunch is still deducted (§5.4)
    store_block = se.RosterEntryData(custom_segments=({"kind": "WORK", "start": "10:00", "end": "14:00"},),
                                     break_defaults=(B("REST", "PUNCHED", 15, True),))
    block = se.build_instance(None, D(2026, 11, 4), NY, store_block)
    assert block.kind == "FIXED" and block.scheduled_net_minutes == 240 and block.shift_id is None
    anchored_custom = se.build_instance(US_E1, D(2026, 11, 2), IST, se.RosterEntryData(
        custom_start_time="18:00", custom_end_time="23:00"))
    assert anchored_custom.clock_timezone == IST and clock(anchored_custom.start_at, IST) == "18:00"
    with pytest.raises(se.ShiftEngineError) as err:
        se.build_instance(None, D(2026, 11, 4), IST)
    assert err.value.code == "SHIFT_REQUIRED"


def test_segment_validation_rules():
    def codes(version):
        return {(i.field, i.code) for i in se.validate_version(version)}

    reordered = V(kind="SPLIT", start_time="16:00", end_time="12:00",
                  segments=(S("WORK", "16:00", "20:00", False), S("WORK", "08:00", "12:00", False)))
    assert ("segments[1].next_day", "NEXT_DAY_MISMATCH") in codes(reordered)
    too_long = V(kind="SPLIT", start_time="06:00", end_time="08:00",
                 segments=(S("WORK", "06:00", "20:00"), S("WORK", "21:00", "08:00")))
    assert ("segments", "SPAN_OVER_24H") in codes(too_long)
    assert ("segments", "NOT_ALLOWED") in codes(V(**{**FLEX_BLR.__dict__, "segments": (S("WORK", "08:00", "20:00"),)}))
    assert ("anchor_timezone", "NOT_ALLOWED") in codes(V(kind="OPEN", anchor_timezone=NY))
    assert ("core_start_time", "REQUIRED") in codes(V(kind="FLEXIBLE", start_time="08:00", end_time="20:00"))
    assert ("core_end_time", "CORE_TOO_SHORT") in codes(
        V(kind="FLEXIBLE", start_time="08:00", end_time="20:00", core_start_time="11:00", core_end_time="11:20"))
    assert ("anchor_timezone", "INVALID_TIMEZONE") in codes(V(start_time="09:00", end_time="18:00", anchor_timezone="Mars/Base"))


def test_local_time_resolution_rules():
    assert se.resolve_local_time(NY, dt.datetime(2027, 3, 14, 2, 30)) == utc(2027, 3, 14, 7, 30)  # gap -> forward
    assert se.resolve_local_time(NY, dt.datetime(2026, 11, 1, 1, 30)) == utc(2026, 11, 1, 5, 30)  # overlap -> first
    assert se.resolve_local_time(IST, dt.datetime(2026, 11, 5, 9)) == utc(2026, 11, 5, 3, 30)
    with pytest.raises(se.ShiftEngineError):
        se.build_instance(GEN, D(2026, 11, 5), "No/Such_Zone")


def test_engine_is_pure():
    """IG-AS-02: the engine imports only the standard library and pytz (no app.*, no SQLAlchemy)."""
    source = Path(se.__file__).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= {"__future__", "datetime", "collections", "dataclasses", "decimal", "typing", "pytz"}
