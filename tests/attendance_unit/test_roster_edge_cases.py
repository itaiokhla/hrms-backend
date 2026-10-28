"""Edge cases AS-ROT-01..08 and AS-ROS-02, -10, -12, -13 (UNIT level of docs/attendance-shift/edge-cases.json).

Where the engine tests already hold the exact scenario of an edge case (a GV vector of 03-shifts/02 §15), the case
runs that same check under its edge-case id; the others are written here. API-level cases are in
tests/shifts/test_roster_edge_cases.py.
"""

import datetime as dt

from app.services import roster_rules as rr
from app.services import rotation_engine as rot
from app.services import shift_engine as se
from tests.attendance_unit import test_roster_rules as trr
from tests.attendance_unit import test_rotation_engine as tre

D = dt.date
PUNE, ANCHOR = tre.PUNE, tre.PUNE_ANCHOR
OFFSETS = {"A": 0, "B": 6, "C": 12, "D": 18}


def _shift(crew, day):
    return rot.shift_for_token(PUNE, rot.token_on(PUNE, ANCHOR, OFFSETS[crew], day))


def test_as_rot_01_cycle_index_before_the_anchor_uses_floor_modulo():
    day = D(2026, 11, 1)
    assert rot.cycle_index(PUNE, ANCHOR, 0, day) == 23 and _shift("A", day) is None  # (-1 + 0) mod 24 -> O
    assert rot.token_on(PUNE, ANCHOR, 6, day) == "M" and _shift("B", day) == tre.SHIFT_A  # (-1 + 6) = 5


def test_as_rot_02_leap_day_and_year_end_never_pause_the_cycle():
    leap = D(2028, 2, 29)
    assert (leap - ANCHOR).days == 484
    assert [_shift(c, leap) for c in "ABCD"] == [tre.SHIFT_A, tre.SHIFT_B, tre.SHIFT_C, None]  # idx 4, 10, 16, 22
    march = D(2028, 3, 1)
    assert [rot.cycle_index(PUNE, ANCHOR, OFFSETS[c], march) for c in "ABCD"] == [5, 11, 17, 23]
    assert [_shift(c, march) for c in "ABCD"] == [tre.SHIFT_A, tre.SHIFT_B, tre.SHIFT_C, None]
    year_end = D(2026, 12, 31)
    assert rot.cycle_index(PUNE, ANCHOR, 6, year_end) == 17 and _shift("B", year_end) == tre.SHIFT_C


def test_as_rot_03_kelly_24_48_maps_to_a_long_duty_shift_only():
    """AS-M1 accepts a 24-hour FIXED shift with the long-duty confirm (03-shifts/01 §3.2), so the K token maps."""
    kelly = rot.TEMPLATES["KELLY_24_48"].pattern({"K": 900})
    assert rot.validate_pattern(kelly, {900: rot.ShiftTimes("08:00", "08:00", long_duty=True)}) == []
    issues = rot.validate_pattern(kelly, {900: rot.ShiftTimes("08:00", "20:00")})
    assert issues and issues[0].rule == "TOKEN_UNMAPPED"
    duty = se.ShiftVersionData(start_time="08:00", end_time="08:00", long_duty=True)
    assert se.validate_version(duty) == () and se.validate_version(
        se.ShiftVersionData(start_time="08:00", end_time="08:00")) != ()


def test_as_rot_04_crew_transfer_seam_rest_runs_and_backward_rotation():
    trr.test_publish_lints_backward_rotation_on_transfer_seam()


def test_as_rot_05_individual_assignment_offset_starts_on_the_chosen_index():
    tre.test_gv_as_rot_05_individual_assignment_offset()


def test_as_rot_07_week_start_change_moves_the_worst_fixed_week():
    tre.test_gv_as_rot_10_week_start_change_moves_the_worst_week()


def test_as_rot_08_run_length_traps_warn_while_draft_and_block_once_verified():
    tre.test_gv_as_rot_08_staggered_off_day_moving_mon_to_sun_gives_12_days()
    items = tuple(rr.from_shift_instance(trr.instance(trr.SHIFT_A, D(2026, 11, 2) + dt.timedelta(days=i)),
                                         changed=True) for i in range(12))
    schedule = rr.ScheduleData((rr.EmployeeSchedule(5, trr.IST, items),))

    def runs(limit):
        return [r for r in rr.validate(schedule, rr.EmployeeLimits(max_consecutive_days=limit), rr.RuleContext())
                if r.rule_key == rr.WEEKLY_REST_EXCEEDED and r.details.get("check") == "CONSECUTIVE_DAYS"]

    draft = runs(rr.Limit(10, rr.WARN, "LH weekly_rest IN", verified=False))
    assert [(r.severity, r.measured) for r in draft] == [(rr.WARN, 12)] and draft[0].message_key.endswith(".not_verified")
    verified = runs(rr.Limit(10, rr.BLOCK, "LH weekly_rest IN", verified=True))
    assert [r.severity for r in verified] == [rr.BLOCK]
    blocking, _, _ = rr.summarize(verified, (), {}, {})
    assert blocking  # publishable only with a roster:exempt exemption
    blocking, _, _ = rr.summarize(verified, (), {}, {rr.WEEKLY_REST_EXCEEDED: {"exemption_ref": "EX-1"}})
    assert blocking == []


def test_as_ros_02_notice_in_elapsed_hours_on_the_spring_forward_week():
    trr.test_gv_as_ros_10_notice_days_dst_safe()


def test_as_ros_10_unknown_gender_falls_under_the_women_at_night_rule():
    assert rr.protected_groups_for(None, {"WOMEN"}) == {"WOMEN"}
    assert rr.protected_groups_for("", {"WOMEN"}) == {"WOMEN"}
    assert rr.protected_groups_for("Female", {"WOMEN"}) == {"WOMEN"}
    assert rr.protected_groups_for("Male", {"WOMEN"}) == frozenset()
    assert rr.protected_groups_for(None, ()) == frozenset()
    night = rr.from_shift_instance(trr.instance(trr.SHIFT_C, D(2026, 11, 3)), changed=True)
    employee = rr.EmployeeSchedule(9, trr.IST, (night,), protected_groups=rr.protected_groups_for(None, {"WOMEN"}))
    limits = rr.EmployeeLimits(night_work_groups={"WOMEN": rr.NightWorkRule(("19:00", "06:00"), True,
                                                                            rr.REQUIRE_REASON, verified=False)})
    found = [r for r in rr.validate(rr.ScheduleData((employee,)), limits, rr.RuleContext())
             if r.rule_key == rr.NIGHT_WORK_CONSENT_MISSING]
    assert [r.severity for r in found] == [rr.REQUIRE_REASON]


def test_as_ros_12_substitution_under_fixed_and_rolling_weeks():
    trr.test_gv_as_ros_04_kiran_substitution_rolling_vs_fixed_week()


def test_as_ros_13_weekly_rest_of_24_hours_after_a_night_shift():
    trr.test_gv_as_ros_17_weekly_rest_hours_from_end_of_last_night()
