"""Pure day evaluation (AS-M3): golden vectors GV-AS-EVL-*, GV-AS-NGT-*, GV-AS-BRK-*, GV-AS-LAT-*.

Owner doc ``docs/attendance-shift/04-attendance/03-day-evaluation-calculations.md`` §15 (vector assumptions §15.1,
policy sets P-GEN, P-CREW, P-FLEX, P-BPO, P-CLINIC, P-SITE, P-STORE-AE, P-US). No database.
"""

import ast
import datetime as dt
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import pytz

from app.services import day_evaluation as de
from app.services import shift_engine as se

IST, NY, GST = "Asia/Kolkata", "America/New_York", "Asia/Dubai"
D = dt.date
B = se.BreakRuleData
V = se.ShiftVersionData


def at(zone, y, mo, d, h, mi=0, s=0):
    """Local wall time -> aware UTC instant."""
    return pytz.timezone(zone).localize(dt.datetime(y, mo, d, h, mi, s)).astimezone(dt.timezone.utc)


def floating(kind, start, end, minutes, paid, next_day=False, after=None):
    return B(kind, "FLOATING_WINDOW", minutes, paid, window_start=start, window_end=end, next_day=next_day,
             auto_deduct_after_minutes=after)


# Cast versions (03-shifts/01 §2.4) with the §15.1 vector assumptions (crew lunch presumed after 240).
GEN = V(start_time="09:00", end_time="18:00", break_rules=(B("LUNCH", "FIXED_WINDOW", 60, False, start="13:00",
                                                             end="14:00"),), shift_id=101, shift_version_id=1001)
A = V(start_time="06:00", end_time="14:00", break_rules=(floating("LUNCH", "09:30", "11:30", 30, True, after=240),),
      grace_in_minutes=10, grace_out_minutes=10)
B_SHIFT = V(start_time="14:00", end_time="22:00",
            break_rules=(floating("LUNCH", "17:30", "19:30", 30, True, after=240),), grace_in_minutes=10)
C = V(start_time="22:00", end_time="06:00",
      break_rules=(floating("LUNCH", "01:30", "03:30", 30, True, next_day=True, after=240),))
FLEX_BLR = V(kind="FLEXIBLE", start_time="08:00", end_time="20:00", core_start_time="11:00", core_end_time="16:00",
             expected_net_minutes=480, break_rules=(B("LUNCH", "AUTO_DEDUCT", 45, False, auto_deduct_after_minutes=360),))
US_E1 = V(start_time="09:00", end_time="18:00", anchor_timezone=NY,
          break_rules=(floating("LUNCH", "12:00", "14:00", 60, False),))
M6 = V(start_time="08:00", end_time="14:00", break_rules=(floating("REST", "10:00", "12:00", 15, True),))
E6 = V(start_time="14:00", end_time="20:00", break_rules=(floating("REST", "16:00", "18:00", 15, True),))
N12 = V(start_time="20:00", end_time="08:00", break_rules=(floating("REST", "00:00", "04:00", 15, True, next_day=True),))
SPD = V(kind="SPLIT", start_time="08:00", end_time="20:00",
        segments=({"kind": "WORK", "start": "08:00", "end": "12:00"}, {"kind": "WORK", "start": "16:00", "end": "20:00"}))
OC = V(start_time="20:00", end_time="08:00", segments=({"kind": "ON_CALL", "start": "20:00", "end": "08:00",
                                                        "next_day": True},))
SITE_D = V(start_time="08:00", end_time="17:00", break_rules=(B("LUNCH", "FIXED_WINDOW", 60, False, start="13:00",
                                                                end="14:00"),))
BSPLIT = V(kind="SPLIT", start_time="10:00", end_time="22:00",
           segments=({"kind": "WORK", "start": "10:00", "end": "14:00"}, {"kind": "WORK", "start": "17:00", "end": "22:00"}))
WH_NIGHT = V(start_time="22:00", end_time="06:00", break_rules=(B("LUNCH", "PUNCHED", 30, False),))

R = de.RulesParams
P_GEN = R(day_basis="HOURS", threshold_mode="ABSOLUTE", full_day_min_minutes=Decimal(450),
          half_day_min_minutes=Decimal(240))
P_CREW = R(threshold_mode="ABSOLUTE", full_day_min_minutes=Decimal(420), half_day_min_minutes=Decimal(240),
           grace_in_minutes=10, grace_out_minutes=10, late_half_day_after_minutes=120, ot_post_shift_buffer_minutes=15)
P_FLEX = R(threshold_mode="PERCENT_OF_EXPECTED", full_day_min_percent=90, half_day_min_percent=50,
           short_hours_enabled=True, short_tolerance_minutes=30)
P_BPO = R(threshold_mode="ABSOLUTE", full_day_min_minutes=Decimal(480), half_day_min_minutes=Decimal(240),
          grace_in_minutes=5)
P_CLINIC = R(threshold_mode="PERCENT_OF_EXPECTED", grace_in_minutes=10)
P_SITE = R(threshold_mode="PERCENT_OF_EXPECTED", grace_in_minutes=15)
P_STORE_AE = R(threshold_mode="PERCENT_OF_EXPECTED", grace_in_minutes=15, short_hours_enabled=True,
               short_tolerance_minutes=15)
P_US = R(threshold_mode="PERCENT_OF_EXPECTED", grace_in_minutes=15)
OT_SHIFT = de.OvertimeView(enabled=True)

FULL = de.ExpectationData("FULL", 480)
FAR = dt.datetime(2027, 6, 1, tzinfo=dt.timezone.utc)  # long after every close point


def sess(sid, start, end, *breaks, source="WEB", closed_at=None):
    return de.SessionData(sid, start, end, closed_at=closed_at, breaks=tuple(breaks), capture_source=source)


def brk(bid, kind, start, end, paid=False):
    return de.BreakData(bid, kind, start, end, paid)


def day(version, shift_date, sessions=(), *, zone=IST, expectation=FULL, rules=P_GEN, now=FAR, extra=(), **kw):
    instances = (se.build_instance(version, shift_date, zone),) + tuple(extra) if version is not None else tuple(extra)
    kw.setdefault("shift_grace_in_minutes", version.grace_in_minutes if version is not None else None)
    kw.setdefault("shift_grace_out_minutes", version.grace_out_minutes if version is not None else None)
    kw.setdefault("scheduled_minutes", expectation.expected_minutes)
    return de.evaluate_day(de.DayInputs(
        employee_id=7, shift_date=shift_date, work_timezone=zone, now=now, expectation=expectation,
        instances=instances, sessions=tuple(sessions), rules=rules, **kw))


def minutes(seconds):
    return None if seconds is None else seconds // 60


# --------------------------------------------------------------------------- GV-AS-EVL


def test_gv_evl_01_late_present():
    t = lambda h, m: at(IST, 2026, 11, 3, h, m)
    ev = day(GEN, D(2026, 11, 3), [sess(1, t(9, 20), t(18, 5), brk(1, "LUNCH", t(13, 0), t(14, 0)))])
    assert ev.evaluation_state == "FINAL" and ev.day_result == "PRESENT"
    assert ev.late_mark and ev.late_minutes == 20
    assert (ev.gross_minutes, ev.unpaid_break_minutes, ev.net_seconds) == (525, 60, 27900)
    assert ev.exception_codes == ("LATE_IN",)
    assert ev.ot_eligible_minutes == 465 and ev.night_minutes == 0
    assert ev.presence_basis == "SESSION"


def test_gv_evl_02_late_beyond_converts_to_half_day():
    t = lambda h, m: at(IST, 2026, 11, 4, h, m)
    rules = replace(P_GEN, late_half_day_after_minutes=120)
    ev = day(GEN, D(2026, 11, 4), [sess(1, t(11, 30), t(18, 0), brk(1, "LUNCH", t(13, 0), t(14, 0)))], rules=rules)
    assert ev.day_result == "HALF_DAY"
    assert (ev.first_half_result, ev.second_half_result) == ("ABSENT", "PRESENT")
    assert not ev.late_mark and ev.late_converted and ev.late_minutes == 150
    assert ev.exception_codes == ("ABSENT",)
    assert ev.net_seconds == 19800


def test_gv_evl_03_half_day_leave_moves_expected_start():
    t = lambda h, m: at(IST, 2026, 11, 24, h, m)
    exp = de.ExpectationData("SECOND_HALF", 240, ("LEAVE",))
    ev = day(GEN, D(2026, 11, 24), [sess(1, t(14, 10), t(18, 15))], expectation=exp)
    assert ev.expected_start_at == t(14, 0) and not ev.late_mark
    assert ev.net_seconds == 245 * 60
    assert (ev.first_half_result, ev.second_half_result, ev.day_result) == ("OFF", "PRESENT", "PRESENT")
    variant = day(GEN, D(2026, 11, 24), [sess(1, t(14, 10), t(18, 2))], expectation=exp)
    assert variant.day_result == "ABSENT"
    pct = day(GEN, D(2026, 11, 24), [sess(1, t(14, 10), t(18, 2))], expectation=exp,
              rules=replace(P_GEN, threshold_mode="PERCENT_OF_EXPECTED"))
    assert pct.day_result == "PRESENT" and pct.short_minutes == 8


def test_gv_evl_04_missing_punch_out():
    t = lambda h, m: at(IST, 2026, 11, 5, h, m)
    ev = day(GEN, D(2026, 11, 5), [sess(1, t(9, 2), None, closed_at=t(22, 0))])
    assert ev.day_result == "MISSING_PUNCH"
    assert (ev.first_half_result, ev.second_half_result) == ("MISSING_PUNCH", "MISSING_PUNCH")
    assert ev.net_seconds is None and ev.gross_seconds is None and ev.night_minutes == 0
    assert ev.ot_eligible_seconds == 0 and not ev.late_mark
    assert ev.exception_codes == ("MISSING_PUNCH_OUT",)
    fixed = day(GEN, D(2026, 11, 5), [sess(1, t(9, 2), t(18, 10), source="REQUEST")])  # approved MISSED_PUNCH
    assert fixed.day_result == "PRESENT" and fixed.net_minutes == 488 and fixed.exception_codes == ()


def test_gv_evl_05_split_shift_two_sessions():
    t = lambda h, m: at(IST, 2026, 11, 5, h, m)
    ev = day(SPD, D(2026, 11, 5), [sess(1, t(7, 58), t(12, 3)), sess(2, t(15, 55), t(20, 5))],
             rules=P_CLINIC, ot_view=OT_SHIFT)
    assert ev.day_result == "PRESENT" and ev.session_count == 2
    assert ev.net_minutes == 495 and ev.ot_eligible_minutes == 493 and ev.ot_candidate_minutes == 13
    assert not ev.late_mark and not ev.early_mark


def test_gv_evl_06_all_on_call_presumed():
    sat = D(2026, 11, 7)
    ev = day(OC, sat, [sess(1, at(IST, 2026, 11, 8, 1, 10), at(IST, 2026, 11, 8, 3, 40))],
             expectation=de.ExpectationData("FULL", 0), rules=P_CLINIC, ot_view=OT_SHIFT, scheduled_minutes=0)
    assert ev.day_result == "PRESENT" and ev.presence_basis == "PRESUMED"
    assert (ev.net_minutes, ev.night_minutes, ev.ot_eligible_minutes, ev.ot_candidate_minutes) == (150, 150, 150, 150)


def test_gv_evl_07_worked_on_leave():
    t = lambda h, m: at(IST, 2026, 11, 13, h, m)
    exp = de.ExpectationData("NONE", 0, ("LEAVE",))
    ev = day(FLEX_BLR, D(2026, 11, 13), [sess(1, t(10, 0), t(18, 0))], expectation=exp, rules=P_FLEX,
             scheduled_minutes=480)
    assert ev.day_result == "PRESENT"
    assert (ev.first_half_result, ev.second_half_result) == ("OFF", "OFF")
    assert ev.net_seconds == 26100 and ev.ot_eligible_minutes == 435
    assert ev.exception_codes == ("WORKED_ON_LEAVE",)
    with_ot = day(FLEX_BLR, D(2026, 11, 13), [sess(1, t(10, 0), t(18, 0))], expectation=exp, rules=P_FLEX,
                  scheduled_minutes=480, ot_view=OT_SHIFT)
    assert with_ot.ot_candidate_minutes == 0


def test_gv_evl_09_pending_leave_never_absent():
    exp = de.ExpectationData("FULL", 480, ("LEAVE_PENDING",))
    ev = day(FLEX_BLR, D(2026, 11, 16), [], expectation=exp, rules=P_FLEX, now=at(IST, 2026, 11, 17, 9, 0))
    assert ev.evaluation_state == "FINAL" and ev.day_result == "PENDING"
    assert ev.pending_reasons == ("LEAVE_PENDING",)
    assert ev.exception_codes == ("LEAVE_PENDING_PAST_DATE",)
    rejected = day(FLEX_BLR, D(2026, 11, 16), [], rules=P_FLEX, now=at(IST, 2026, 11, 17, 9, 0))
    assert rejected.day_result == "ABSENT" and set(rejected.exception_codes) == {"UNMARKED_DAY", "ABSENT"}


def test_gv_evl_10_rounding_nearest():
    t = lambda h, m: at(IST, 2026, 11, 17, h, m)
    rules = replace(P_GEN, rounding_mode="NEAREST", rounding_increment_minutes=15)
    ev = day(GEN, D(2026, 11, 17), [sess(1, t(8, 56), t(18, 5), brk(1, "LUNCH", t(13, 0), t(14, 0)))], rules=rules)
    assert ev.net_seconds == 28800 and ev.rounding_delta_minutes == -9
    assert ev.ot_eligible_minutes == 480 and not ev.late_mark and not ev.early_mark


@pytest.mark.parametrize("second_in, short_mark, net", [((17, 12), False, 533), ((17, 40), True, 505)])
def test_gv_evl_11_split_short_hours(second_in, short_mark, net):
    t = lambda h, m: at(GST, 2026, 11, 5, h, m)
    ev = day(BSPLIT, D(2026, 11, 5), [sess(1, t(9, 57), t(14, 2)), sess(2, t(*second_in), t(22, 0))],
             zone=GST, expectation=de.ExpectationData("FULL", 540), rules=P_STORE_AE)
    assert ev.day_result == "PRESENT" and ev.net_minutes == net and ev.short_mark is short_mark
    assert ("SHORT_HOURS" in ev.exception_codes) is short_mark


def test_gv_evl_13_capture_off_presumed():
    ev = day(GEN, D(2026, 11, 3), [], settings=de.EvalSettings(capture_enabled=False))
    assert ev.day_result == "PRESENT" and ev.presence_basis == "PRESUMED"
    assert ev.net_seconds is None and ev.exception_codes == ()


def test_gv_evl_14_flexible_long_day():
    ev = day(FLEX_BLR, D(2026, 11, 2), [sess(1, at(IST, 2026, 11, 2, 6, 40), at(IST, 2026, 11, 2, 22, 30))],
             rules=P_FLEX)
    assert ev.day_result == "PRESENT" and ev.net_minutes == 905
    assert ev.night_minutes == 30 and ev.ot_eligible_minutes == 825 and not ev.late_mark


def test_gv_evl_20_on_duty_second_half():
    t = lambda h, m: at(IST, 2026, 11, 19, h, m)
    duty = (de.OnDutyData(5, "ON_DUTY", "SECOND_HALF"),)
    ev = day(GEN, D(2026, 11, 19), [sess(1, t(9, 5), t(13, 0))], on_duty=duty)
    assert ev.expected_end_at == t(13, 0) and ev.expected_minutes == 240 and not ev.late_mark
    assert (ev.first_half_result, ev.second_half_result, ev.day_result) == ("ABSENT", "PRESENT", "HALF_DAY")
    pct = day(GEN, D(2026, 11, 19), [sess(1, t(9, 5), t(13, 0))], on_duty=duty,
              rules=replace(P_GEN, threshold_mode="PERCENT_OF_EXPECTED"))
    assert pct.day_result == "PRESENT" and pct.presence_basis == "SESSION"
    full = day(GEN, D(2026, 11, 19), [sess(1, t(9, 0), t(13, 0))], on_duty=duty)
    assert full.day_result == "PRESENT"


def test_gv_evl_21_missing_punch_after_closed_half():
    t = lambda h, m: at(IST, 2026, 11, 21, h, m)
    ev = day(A, D(2026, 11, 21), [sess(1, t(6, 1), t(10, 2)), sess(2, t(10, 31), None, closed_at=t(18, 0))],
             rules=P_CREW)
    assert ev.net_seconds is None and ev.net_closed_seconds == 14460
    assert (ev.first_half_result, ev.second_half_result, ev.day_result) == ("PRESENT", "MISSING_PUNCH", "MISSING_PUNCH")
    assert ev.exception_codes == ("MISSING_PUNCH_OUT",)


def test_gv_evl_22_double_shift_day_halves():
    t = lambda h, m: at(IST, 2026, 11, 14, h, m)
    second = se.build_instance(E6, D(2026, 11, 14), IST)
    ev = day(M6, D(2026, 11, 14), [sess(1, t(7, 58), t(14, 0))], expectation=de.ExpectationData("FULL", 720),
             rules=P_CLINIC, extra=(second,))
    assert (ev.first_half_result, ev.second_half_result, ev.day_result) == ("PRESENT", "ABSENT", "HALF_DAY")
    assert ev.early_mark and {"EARLY_OUT", "ABSENT"} <= set(ev.exception_codes)
    custom = se.build_instance(None, D(2026, 11, 14), IST,
                               se.RosterEntryData(custom_start_time="16:00", custom_end_time="20:00"))
    uneven = day(M6, D(2026, 11, 14), [sess(1, t(7, 58), t(14, 0))], expectation=de.ExpectationData("FULL", 600),
                 rules=P_CLINIC, extra=(custom,))
    assert uneven.first_half_result == "PRESENT" and uneven.day_result == "HALF_DAY"


@pytest.mark.parametrize("mode, eligible", [("UP", 495), ("DOWN", 480)])
def test_gv_evl_24_rounding_up_down(mode, eligible):
    t = lambda h, m: at(IST, 2026, 11, 17, h, m)
    rules = replace(P_GEN, rounding_mode=mode, rounding_increment_minutes=15)
    ev = day(GEN, D(2026, 11, 17), [sess(1, t(8, 56), t(18, 5), brk(1, "LUNCH", t(13, 0), t(14, 0)))], rules=rules)
    assert ev.rounding_delta_minutes == 6 and ev.net_minutes == 495 and ev.ot_eligible_minutes == eligible


def test_gv_evl_25_short_visit_on_holiday():
    t = lambda h, m: at(IST, 2027, 1, 26, h, m)
    ev = day(GEN, D(2027, 1, 26), [sess(1, t(11, 0), t(11, 7))], expectation=de.ExpectationData("NONE", 0, ("HOLIDAY",)))
    assert ev.day_result == "OFF"
    assert set(ev.exception_codes) == {"SHORT_VISIT", "NON_WORKING_DAY_WORKED"}


def test_gv_evl_26_override_absent_removes_ot():
    t = lambda h, m: at(IST, 2026, 11, 19, h, m)
    ev = day(SITE_D, D(2026, 11, 19), [sess(1, t(8, 0), t(19, 0), source="SUPERVISOR")], rules=P_SITE,
             ot_view=OT_SHIFT, overrides=(de.OverrideData(91, 1, "ABSENT", "ABSENT"),))
    assert ev.net_minutes == 600 and ev.day_result == "ABSENT"
    assert ev.ot_eligible_minutes == 0 and ev.ot_candidate_minutes == 0
    assert ev.exception_codes == ("ABSENT", "OVERTIME_ON_ABSENT_DAY")
    assert ev.override_request_id == 91


# --------------------------------------------------------------------------- GV-AS-NGT


def test_gv_ngt_01_night_shift():
    ev = day(C, D(2026, 11, 4), [sess(1, at(IST, 2026, 11, 4, 22, 7), at(IST, 2026, 11, 5, 6, 2), source="DEVICE")],
             rules=P_CREW, ot_view=OT_SHIFT)
    assert ev.day_result == "PRESENT" and not ev.late_mark
    assert (ev.net_minutes, ev.night_minutes, ev.ot_eligible_minutes, ev.ot_candidate_minutes) == (475, 473, 473, 0)


def test_gv_ngt_02_after_midnight_late_beyond():
    ev = day(C, D(2026, 11, 2), [sess(1, at(IST, 2026, 11, 3, 0, 20), at(IST, 2026, 11, 3, 6, 5))], rules=P_CREW)
    assert (ev.first_half_result, ev.second_half_result, ev.day_result) == ("ABSENT", "PRESENT", "HALF_DAY")
    assert ev.late_converted and not ev.late_mark and ev.late_minutes == 140
    assert ev.net_seconds == 20700 and ev.night_minutes == 340 and ev.ot_eligible_minutes == 340
    assert ev.exception_codes == ("ABSENT",)


def test_gv_ngt_03_dst_fall_back():
    sat = D(2026, 10, 31)
    utc = lambda d, h, m: dt.datetime(2026, 11, d, h, m, tzinfo=dt.timezone.utc)
    ev = day(WH_NIGHT, sat, [sess(1, utc(1, 1, 58), utc(1, 11, 3), brk(1, "LUNCH", utc(1, 7, 0), utc(1, 7, 30)))],
             zone=NY, expectation=de.ExpectationData("FULL", 510), rules=P_US)
    assert ev.day_result == "PRESENT"
    assert (ev.gross_minutes, ev.net_minutes, ev.night_minutes, ev.ot_eligible_minutes) == (545, 515, 510, 513)


def test_gv_ngt_05_anchored_bpo_shift():
    fri = day(US_E1, D(2026, 10, 30), [sess(1, at(IST, 2026, 10, 30, 18, 41), at(IST, 2026, 10, 31, 3, 41),
                                            brk(1, "LUNCH", at(IST, 2026, 10, 30, 23, 30), at(IST, 2026, 10, 31, 0, 30)))],
              rules=P_BPO)
    assert fri.day_result == "PRESENT" and fri.late_mark and fri.late_minutes == 11
    assert (fri.net_minutes, fri.night_minutes, fri.ot_eligible_minutes) == (480, 281, 480)
    mon = day(US_E1, D(2026, 11, 2), [sess(1, at(IST, 2026, 11, 2, 19, 28), at(IST, 2026, 11, 3, 4, 33),
                                           brk(1, "LUNCH", at(IST, 2026, 11, 3, 0, 0), at(IST, 2026, 11, 3, 1, 0)))],
              rules=P_BPO)
    assert mon.day_result == "PRESENT" and not mon.late_mark
    assert (mon.net_minutes, mon.night_minutes, mon.ot_eligible_minutes) == (485, 333, 483)


# --------------------------------------------------------------------------- GV-AS-BRK


def test_gv_brk_01_auto_deduct():
    t = lambda h, m: at(IST, 2026, 11, 4, h, m)
    ev = day(FLEX_BLR, D(2026, 11, 4), [sess(1, t(10, 5), t(19, 20))], rules=P_FLEX)
    assert ev.net_minutes == 510 and ev.auto_deducted_minutes == 45 and not ev.late_mark
    assert ev.day_result == "PRESENT" and ev.short_minutes == 0


def test_gv_brk_02_auto_deduct_top_up_never_double():
    t = lambda h, m: at(IST, 2026, 11, 5, h, m)
    ev = day(FLEX_BLR, D(2026, 11, 5), [sess(1, t(9, 40), t(18, 30), brk(1, "LUNCH", t(13, 0), t(13, 30)))],
             rules=P_FLEX)
    assert ev.net_minutes == 485 and ev.auto_deducted_minutes == 15


def test_gv_brk_04_overstay_union_with_fixed_window():
    t = lambda h, m: at(IST, 2026, 11, 12, h, m)
    ev = day(GEN, D(2026, 11, 12), [sess(1, t(8, 55), t(18, 0), brk(1, "LUNCH", t(13, 0), t(14, 20)))])
    assert ev.net_minutes == 465 and ev.day_result == "PRESENT"
    assert ev.exception_codes == ("BREAK_OVERSTAY",) and ev.ot_eligible_minutes == 460


def test_gv_brk_05_paid_floating_presumed_and_variants():
    t = lambda h, m: at(IST, 2026, 11, 22, h, m)
    sessions = [sess(1, t(5, 58), t(14, 1))]
    ev = day(A, D(2026, 11, 22), sessions, rules=P_CREW)
    assert (ev.net_minutes, ev.night_minutes, ev.ot_eligible_minutes) == (483, 2, 480)
    assert ev.exception_codes == () and ev.day_result == "PRESENT"
    no_after = replace(A, break_rules=(floating("LUNCH", "09:30", "11:30", 30, True),))
    missed = day(no_after, D(2026, 11, 22), sessions, rules=P_CREW)
    assert missed.exception_codes == ("BREAK_MISSED",) and missed.net_minutes == 483
    unpaid = replace(A, break_rules=(floating("LUNCH", "09:30", "11:30", 30, False, after=240),))
    deducted = day(unpaid, D(2026, 11, 22), sessions, rules=P_CREW)
    assert deducted.net_minutes == 453 and deducted.day_result == "PRESENT"


def test_gv_brk_06_break_off_window_matches_nearest_rule():
    ev = day(C, D(2026, 11, 6), [sess(1, at(IST, 2026, 11, 6, 21, 58), at(IST, 2026, 11, 7, 6, 3),
                                      brk(1, "LUNCH", at(IST, 2026, 11, 7, 0, 50), at(IST, 2026, 11, 7, 1, 20), True))],
             rules=P_CREW)
    assert ev.net_minutes == 485 and ev.exception_codes == ("BREAK_OFF_WINDOW",)


def test_gv_brk_07_fixed_window_worked_through():
    t = lambda h, m: at(IST, 2026, 11, 11, h, m)
    ev = day(GEN, D(2026, 11, 11), [sess(1, t(8, 55), t(18, 0))])
    assert ev.net_minutes == 485 and ev.exception_codes == ("BREAK_NOT_TAKEN",)


def test_gv_brk_08_paid_break_overstay_unpaid_excess():
    ev = day(N12, D(2026, 11, 13), [sess(1, at(IST, 2026, 11, 13, 20, 0), at(IST, 2026, 11, 14, 8, 2),
                                         brk(1, "REST", at(IST, 2026, 11, 14, 2, 0), at(IST, 2026, 11, 14, 3, 10), True))],
             expectation=de.ExpectationData("FULL", 720), rules=P_CLINIC)
    assert ev.net_minutes == 667 and "BREAK_OVERSTAY" in ev.exception_codes


# --------------------------------------------------------------------------- GV-AS-LAT


@pytest.mark.parametrize("second, mark", [(0, False), (30, True)])
def test_gv_lat_01_strict_second_grace(second, mark):
    start = at(IST, 2026, 11, 4, 14, 10, second)
    ev = day(B_SHIFT, D(2026, 11, 4), [sess(1, start, at(IST, 2026, 11, 4, 22, 0))], rules=P_CREW)
    assert ev.late_mark is mark and ev.late_minutes == 10


def test_gv_lat_02_policy_waiver_keeps_mark():
    t = lambda h, m: at(IST, 2026, 11, 11, h, m)
    ev = day(GEN, D(2026, 11, 11), [sess(1, t(9, 20), t(18, 40), brk(1, "LUNCH", t(13, 0), t(14, 0)))],
             rules=replace(P_GEN, late_waiver="NET_GE_EXPECTED"))
    assert ev.late_mark and ev.late_excused and ev.late_excuse_source == "POLICY"
    assert "LATE_IN" not in ev.exception_codes


def test_gv_lat_03_early_beyond_converts():
    t = lambda h, m: at(IST, 2026, 11, 18, h, m)
    ev = day(GEN, D(2026, 11, 18), [sess(1, t(9, 0), t(15, 30), brk(1, "LUNCH", t(13, 0), t(14, 0)))],
             rules=replace(P_GEN, early_half_day_before_minutes=120))
    assert (ev.first_half_result, ev.second_half_result, ev.day_result) == ("PRESENT", "ABSENT", "HALF_DAY")
    assert not ev.early_mark and ev.early_converted and ev.early_minutes == 150


def test_gv_lat_05_on_duty_edge_moves_expected_start():
    t = lambda h, m: at(IST, 2026, 11, 25, h, m)
    ev = day(GEN, D(2026, 11, 25), [sess(1, t(13, 55), t(18, 0))],
             on_duty=(de.OnDutyData(6, "ON_DUTY", "FIRST_HALF"),))
    assert ev.expected_start_at == t(14, 0) and not ev.late_mark
    assert ev.net_minutes == 240 and ev.day_result == "PRESENT" and ev.short_minutes == 0


@pytest.mark.parametrize("hour, minute, excused", [(12, 30, False), (11, 45, True)])
def test_gv_lat_06_excuse_covers_only_its_minutes(hour, minute, excused):
    t = lambda h, m: at(IST, 2026, 11, 17, h, m)
    ev = day(FLEX_BLR, D(2026, 11, 17), [sess(1, t(hour, minute), t(20, 0))],
             rules=replace(P_FLEX, flexible_late_basis="CORE_START"),
             excuses=(de.ExcuseData(44, "LATE", 60),))
    assert ev.late_mark and ev.late_excused is excused
    assert ("LATE_IN" in ev.exception_codes) is (not excused)
    if excused:
        assert ev.late_excuse_source == "REQUEST"


def test_gv_lat_07_late_under_one_minute():
    start = at(IST, 2026, 11, 9, 19, 30, 40)
    rules = replace(P_BPO, grace_in_minutes=0)
    ev = day(US_E1, D(2026, 11, 9), [sess(1, start, at(IST, 2026, 11, 10, 4, 30))], rules=rules)
    assert ev.late_mark and ev.late_minutes == 0
    excused = day(US_E1, D(2026, 11, 9), [sess(1, start, at(IST, 2026, 11, 10, 4, 30))], rules=rules,
                  excuses=(de.ExcuseData(45, "LATE", 1),))
    assert excused.late_excused


def test_gv_lat_08_tie_goes_to_first_half():
    t = lambda h, m: at(IST, 2026, 11, 11, h, m)
    ev = day(GEN, D(2026, 11, 11), [sess(1, t(10, 30), t(16, 30), brk(1, "LUNCH", t(13, 0), t(14, 0)))])
    assert (ev.first_half_result, ev.second_half_result, ev.day_result) == ("PRESENT", "ABSENT", "HALF_DAY")
    assert ev.late_mark and ev.early_mark


# --------------------------------------------------------------------------- states, LEGACY_V1, hashes, purity


def test_gv_brk_03_statutory_meal_late_and_rests_counted():
    """Step 5b with an illustrative US-CA pack (test values only): meal 1 must start within 300 min; rests by major
    fraction of 240 min."""
    t = lambda h, m: at(NY, 2026, 11, 11, h, m)
    shift = V(start_time="10:00", end_time="18:30", break_rules=(B("LUNCH", "PUNCHED", 30, False),))
    pack = (de.StatutoryBreak("MEAL", after_minutes=300, break_minutes=30, nth=1, must_start_by_minutes=300),
            de.StatutoryBreak("REST", after_minutes=240, break_minutes=10, count_basis="MAJOR_FRACTION"))
    sessions = [sess(1, t(10, 0), t(18, 30), brk(1, "REST", t(12, 0), t(12, 10), True),
                     brk(2, "LUNCH", t(15, 10), t(15, 40)), brk(3, "REST", t(17, 0), t(17, 10), True))]
    ev = day(shift, D(2026, 11, 11), sessions, zone=NY, rules=P_US, statutory_breaks=pack)
    assert ev.net_minutes == 480 and ev.meal_violations == 1 and ev.rest_violations == 0
    assert "MEAL_BREAK_LATE" in ev.exception_codes
    on_time = [sess(1, t(10, 0), t(18, 30), brk(1, "REST", t(12, 0), t(12, 10), True),
                    brk(2, "LUNCH", t(14, 30), t(15, 0)))]
    ev2 = day(shift, D(2026, 11, 11), on_time, zone=NY, rules=P_US, statutory_breaks=pack)
    assert ev2.meal_violations == 0 and ev2.rest_violations == 1 and "REST_BREAK_MISSED" in ev2.exception_codes
    waived = (replace(pack[0], waivable_if_day_minutes_le=600), replace(pack[1], waivable_if_day_minutes_le=600))
    ev3 = day(shift, D(2026, 11, 11), sessions, zone=NY, rules=P_US, statutory_breaks=waived)
    assert ev3.meal_violations == 0 and ev3.rest_violations == 0
    assert day(shift, D(2026, 11, 11), sessions, zone=NY, rules=P_US).meal_violations == 0  # no pack


def test_break_attestation_keeps_the_time():
    t = lambda h, m: at(IST, 2026, 11, 4, h, m)
    attested = replace(sess(1, t(10, 5), t(19, 20)), no_break_attested=True)
    rules = replace(P_FLEX, break_attestation=True)
    ev = day(FLEX_BLR, D(2026, 11, 4), [attested], rules=rules)
    assert ev.net_minutes == 555 and ev.auto_deducted_minutes == 0 and "BREAK_MISSED" in ev.exception_codes
    plain = day(FLEX_BLR, D(2026, 11, 4), [attested], rules=P_FLEX)  # attestation off: the deduction applies
    assert plain.net_minutes == 510


def test_open_day_live_late_status_and_pending_halves():
    t = lambda h, m: at(IST, 2026, 11, 3, h, m)
    ev = day(GEN, D(2026, 11, 3), [sess(1, t(9, 20), None)], now=t(12, 0))
    assert ev.evaluation_state == "OPEN" and ev.day_result == "PENDING"
    assert ev.late_mark and ev.late_minutes == 20  # live late status (BUG-AS-07)
    assert ev.pending_reasons == ("SHIFT_IN_PROGRESS",) and ev.net_minutes == 160


def test_provisional_on_device_sync_and_request_pending():
    t = lambda h, m: at(IST, 2026, 11, 7, h, m)
    ev = day(GEN, D(2026, 11, 7), [], device_ids_behind=(3,))
    assert ev.evaluation_state == "PROVISIONAL" and ev.pending_reasons == ("DEVICE_SYNC",)
    done = day(GEN, D(2026, 11, 7), [], device_ids_behind=(3,), late_log_grace_elapsed=True)
    assert done.evaluation_state == "FINAL" and "DEVICE_SYNC_PENDING" in done.exception_codes
    request = day(GEN, D(2026, 11, 7), [sess(1, t(9, 0), t(13, 0))], request_pending=True)
    assert request.evaluation_state == "PROVISIONAL" and "REQUEST_PENDING" in request.exception_codes


def test_not_employed_is_off():
    ev = day(GEN, D(2026, 11, 23), [], expectation=de.ExpectationData("NONE", 0, ("NOT_EMPLOYED",)))
    assert ev.day_result == "OFF" and ev.expected_portion == "NONE" and ev.exception_codes == ()


def test_legacy_v1_port_reads_payroll_thresholds():
    t = lambda h, m: at(IST, 2026, 11, 3, h, m)
    session = replace(sess(1, t(9, 20), t(14, 0), brk(1, "LUNCH", t(13, 0), t(13, 30))), legacy_status="late",
                      early_exit=True, early_exit_minutes=240, late_minutes=20)
    legacy = de.LegacyPayParams(derive_status_from_hours=True, full_day_min_hours=Decimal(8),
                                half_day_min_hours=Decimal(4), early_exit_grace_minutes=10)
    ev = day(GEN, D(2026, 11, 3), [session], rules_mode="LEGACY_V1", legacy=legacy)
    assert ev.rules_mode == "LEGACY_V1" and ev.day_result == "HALF_DAY"  # 4 h 10 min worked
    assert ev.net_seconds == 15000 and ev.late_mark and ev.early_mark
    assert (ev.first_half_result, ev.second_half_result) == ("PRESENT", "ABSENT")
    off = day(GEN, D(2026, 11, 3), [session], rules_mode="LEGACY_V1")
    assert off.day_result == "PRESENT"  # derive off: the stored status decides
    none = day(GEN, D(2026, 11, 3), [], rules_mode="LEGACY_V1")
    assert none.day_result == "UNMARKED" and none.exception_codes == ("UNMARKED_DAY",)


def test_legacy_v1_zero_length_session_is_zero_seconds():
    """Found by the G-07 reconcile on the time stack: punch-out == punch-in is 0 s worked in today's code."""
    t = lambda h, m: at(IST, 2026, 10, 23, h, m)
    ev = day(GEN, D(2026, 10, 23), [sess(1, t(9, 0), t(9, 0))], rules_mode="LEGACY_V1")
    assert ev.net_seconds == 0 and ev.gross_seconds == 0 and ev.day_result == "PRESENT"


def test_inputs_hash_ignores_now_but_tracks_phase():
    t = lambda h, m: at(IST, 2026, 11, 3, h, m)
    base = de.DayInputs(employee_id=7, shift_date=D(2026, 11, 3), work_timezone=IST, now=FAR, expectation=FULL,
                        instances=(se.build_instance(GEN, D(2026, 11, 3), IST),),
                        sessions=(sess(1, t(9, 0), t(18, 0)),), rules=P_GEN)
    assert de.inputs_hash(base) == de.inputs_hash(replace(base, now=FAR + dt.timedelta(days=3)))
    assert de.inputs_hash(base) != de.inputs_hash(replace(base, now=t(12, 0)))
    first, second = de.evaluate_day(base), de.evaluate_day(replace(base, now=FAR + dt.timedelta(days=1)))
    assert first.content_hash == second.content_hash and first.outputs() == second.outputs()
    changed = de.evaluate_day(replace(base, sessions=(sess(1, t(9, 30), t(18, 0)),)))
    assert changed.content_hash != first.content_hash
    assert set(de.changed_outputs(first.outputs(), changed.outputs())) >= {"late_mark", "net_seconds"}


def test_engine_is_pure():
    """L0: no database, no ORM, no clock reads inside the evaluator (rule 3)."""
    source = Path(de.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any(name.startswith(("sqlalchemy", "app.models", "app.core.database", "app.payroll"))
                   for name in imported)
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert not calls & {"dt.datetime.now", "datetime.now", "dt.datetime.utcnow", "dt.date.today", "date.today"}


def test_vocabularies_are_closed():
    assert de.DAY_RESULTS == ("PRESENT", "HALF_DAY", "ABSENT", "MISSING_PUNCH", "UNMARKED", "PENDING", "OFF")
    assert len(de.EXCEPTION_CODES) == len(set(de.EXCEPTION_CODES))
    assert "content_hash" in de.OUTPUT_COLUMNS and "inputs_hash" not in de.OUTPUT_COLUMNS
