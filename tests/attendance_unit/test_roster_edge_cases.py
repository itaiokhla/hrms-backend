"""Edge cases AS-ROT-01..08 and AS-ROS-02, -10, -12, -13 (UNIT level of docs/attendance-shift/edge-cases.json).

Where the engine tests already hold the exact scenario of an edge case (a GV vector of 03-shifts/02 §15), the case
runs that same check under its edge-case id; the others are written here. API-level cases are in
tests/shifts/test_roster_edge_cases.py.
"""

import datetime as dt
from decimal import Decimal

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


DUBAI = "Asia/Dubai"
SUMMER_BAN = rr.OutdoorBan(("12:30", "15:00"), "06-15", "09-15", rr.WARN, "seasonal.outdoor_midday_ban", False)


def _outdoor_results(day, start, end, ban=SUMMER_BAN, outdoor=True):
    from dataclasses import replace

    planned = replace(trr.custom(day, start, end, DUBAI, changed=True), outdoor=outdoor)
    schedule = rr.ScheduleData((rr.EmployeeSchedule(4101, DUBAI, (planned,)),))
    return [r for r in rr.validate(schedule, rr.EmployeeLimits(outdoor_midday_ban=ban), rr.RuleContext())
            if r.rule_key == rr.OUTDOOR_MIDDAY_BAN]


def test_as_cty_08_seasonal_windows_inclusive_ends_year_wrap_and_29_february():
    """GV-AS-CTY-08: (a) 13:00–17:00 on the last day of the season overlaps 12:30–15:00 by 2 h → WARN (not verified);
    (b) the day after the season → none; (c) a winter season 11-01..02-28 includes 29 February; (d) a night
    15 Sep 22:00 → 16 Sep 06:00 never meets the midday window; (e) a wrapped season holds 31 Dec and 1 Jan; an
    indoor shift is never affected."""
    (a,) = _outdoor_results(D(2026, 9, 15), "13:00", "17:00")
    assert a.severity == rr.WARN and a.measured == 2 and a.message_key.endswith(".not_verified")
    assert _outdoor_results(D(2026, 9, 16), "13:00", "17:00") == []
    winter = rr.OutdoorBan(("12:30", "15:00"), "11-01", "02-28", rr.BLOCK, None, True)
    (c,) = _outdoor_results(D(2028, 2, 29), "12:00", "16:00", ban=winter)
    assert c.severity == rr.BLOCK and c.measured == Decimal("2.5")
    assert _outdoor_results(D(2026, 9, 15), "22:00", "06:00") == []
    assert len(_outdoor_results(D(2027, 12, 31), "12:00", "16:00", ban=winter)) == 1
    assert len(_outdoor_results(D(2028, 1, 1), "12:00", "16:00", ban=winter)) == 1
    assert _outdoor_results(D(2026, 7, 1), "12:00", "16:00", outdoor=False) == []
    assert rr.season_contains("11-01", "02-28", D(2027, 3, 1)) is False


def test_as_cty_07_adolescent_limits_by_shift_date_and_the_18th_birthday():
    """GV-AS-CTY-04 / EC AS-CTY-07: under 18 on the shift date → the pack's forbidden window 19:00–08:00 and the
    3-hour stretch without a 60-minute interval apply (WARN while the pack is not verified, BLOCK once verified); the
    flag is decided once per instance by its shift date, so an adult (the birthday itself) is never limited."""
    from dataclasses import replace

    rule = rr.AdolescentRule(("19:00", "08:00"), None, Decimal(3), 60, rr.WARN, "night_work.adolescent", False)
    evening = replace(trr.custom(D(2026, 11, 10), "18:00", "21:00", trr.IST, changed=True), adolescent=True)
    adult = replace(evening, adolescent=False)

    def results(planned, adolescent_rule=rule):
        schedule = rr.ScheduleData((rr.EmployeeSchedule(4201, trr.IST, (planned,)),))
        return [r for r in rr.validate(schedule, rr.EmployeeLimits(adolescent=adolescent_rule), rr.RuleContext())
                if r.rule_key == rr.ADOLESCENT_LIMITS]

    (window,) = results(evening)
    assert window.details == {"limit": "FORBIDDEN_WINDOW"} and window.measured == 2 and window.severity == rr.WARN
    assert results(adult) == []  # 18 on the shift date: an adult from the birthday itself
    long_day = replace(trr.custom(D(2026, 11, 10), "09:00", "13:30", trr.IST, changed=True), adolescent=True)
    assert [r.details["limit"] for r in results(long_day)] == ["MAX_STRETCH"]  # 4.5 h, no interval
    verified = replace(rule, severity=rr.BLOCK, verified=True)
    assert {r.severity for r in results(evening, verified)} == {rr.BLOCK}


def test_as_cty_17_an_exemption_waives_its_fields_only_until_it_expires():
    """EC AS-CTY-17 (appendix-a §3.3, §5.10, §8.4): TG_IT_ITES_2024 (valid to 2028-05-30) waives the daily-hours,
    spread and women-at-night fields for an IN-TG establishment holding it. Entries up to 30 May are checked with the
    waiver, 31 May without it — decided per shift date, nothing retroactive. The pack lists it only for its own
    subdivisions, and a branch without a subdivision (an unknown input) never gets it."""
    from app.services import working_time_packs as wtp

    pack = wtp.pack("IN")
    (tg,) = wtp.held_exemptions(pack, "IN-TG", ["TG_IT_ITES_2024"])
    assert wtp.held_exemptions(pack, "in-ts", ["TG_IT_ITES_2024"]) == [tg]  # the older ISO code, any case
    assert wtp.held_exemptions(pack, "IN-MH", ["TG_IT_ITES_2024"]) == []
    assert wtp.held_exemptions(pack, None, ["TG_IT_ITES_2024"]) == []
    assert wtp.held_exemptions(pack, "IN-TG", []) == []  # not held
    waiver = rr.Waiver(tg["code"], frozenset(tg["waives"]), tg["valid_from"], tg["valid_to"])
    night = rr.NightWorkRule(("19:00", "06:00"), True, rr.REQUIRE_REASON, verified=False)
    limits = rr.EmployeeLimits(daily_max_hours=rr.Limit(Decimal(9), rr.WARN, "WORKING_TIME pack", False),
                               night_work_groups={"WOMEN": night}, waivers=(waiver,))

    def keys(day, lim=limits):
        long_day = trr.custom(day, "10:00", "21:00", trr.IST, changed=True)  # 11 h, 2 h inside 19:00-06:00
        employee = rr.EmployeeSchedule(4301, trr.IST, (long_day,), protected_groups=frozenset({"WOMEN"}))
        return sorted(r.rule_key for r in rr.validate(rr.ScheduleData((employee,)), lim, rr.RuleContext())
                      if r.rule_key in (rr.DAILY_HOURS_EXCEEDED, rr.NIGHT_WORK_CONSENT_MISSING))

    assert keys(D(2028, 5, 30)) == []  # last valid day: both fields waived
    assert keys(D(2028, 5, 31)) == [rr.DAILY_HOURS_EXCEEDED, rr.NIGHT_WORK_CONSENT_MISSING]  # expired
    assert keys(D(2024, 5, 29)) == [rr.DAILY_HOURS_EXCEEDED, rr.NIGHT_WORK_CONSENT_MISSING]  # not yet valid
    from dataclasses import replace

    assert keys(D(2028, 5, 30), replace(limits, waivers=())) == [rr.DAILY_HOURS_EXCEEDED,
                                                                rr.NIGHT_WORK_CONSENT_MISSING]
    # the 90-day expiry warning of the Settings overview
    assert wtp.expiring_exemptions(pack, D(2028, 3, 1), ["TG_IT_ITES_2024"]) == [
        {"code": "TG_IT_ITES_2024", "valid_to": "2028-05-30", "days_left": 90}]
    assert wtp.expiring_exemptions(pack, D(2028, 2, 29), ["TG_IT_ITES_2024"]) == []
