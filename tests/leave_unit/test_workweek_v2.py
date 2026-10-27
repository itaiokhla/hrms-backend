"""Work-week engine v2 (LH-M1 WI-1.7; owner 04-holiday/03 §3, §5.1, §6.3): kinds, half-day offs, cycles, rest days,
templates, versions and the weekly-rest check. Pure: no database, no clock.

Cases of LH 14 WI-1.13 for this file: LH-WWK-01, 05, 06, 13, 14, 16, 17, 20, 21, 22, 23, 25, 26, 27, 32 and
LH-TZ-01, 02, 10 (shift-minute cases combine this engine with the pure shift engine of AS-M1); plus LH-WWK-04.
"""

import datetime as dt
from decimal import Decimal

import pytest

from app.payroll import workweek as W
from app.payroll.workweek import (
    FIRST_HALF,
    FULL,
    NONE,
    SECOND_HALF,
    PatternVersion,
    RestLimits,
    WorkWeekError,
    WorkWeekPattern,
    is_rest_day,
    is_weekly_off,
    weekly_off_portion,
    working_portion,
)
from app.services import shift_engine as se

D = dt.date
IST, NY, LON = "Asia/Kolkata", "America/New_York", "Europe/London"
IN_LIMITS = RestLimits(max_days_per_week=6, max_consecutive_days=10)  # illustrative IN pack (§6.3)

NTH_2_4 = W.MON_SAT_2ND_4TH_SAT_OFF
ALT_SAT = WorkWeekPattern.from_json({"kind": "CYCLE", "cycle_length_days": 14, "anchor_date": "2027-01-04",
                                     "cycle_off_days": [6, 12, 13]})
HALF_SAT = WorkWeekPattern.from_json(dict(W.TEMPLATES["MON_SAT_HALF_SAT"].rules))
FRI_HALF = WorkWeekPattern.from_json(dict(W.TEMPLATES["FRI_HALF"].rules))
MON_THU = WorkWeekPattern.from_json(dict(W.TEMPLATES["MON_THU"].rules))


def saturdays(year, month):
    day = D(year, month, 1)
    out = []
    while day.month == month:
        if day.weekday() == W.SATURDAY:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------------------------- JSON and kinds
def test_existing_weekly_json_is_byte_identical():
    """Hash stability (SPINE §5.1): a WEEKLY pattern without halves or rest days serialises exactly as before."""
    assert W.MON_FRI.to_json() == {"weekly_off_days": [5, 6], "nth_weekday_offs": []}
    assert NTH_2_4.to_json() == {"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [2, 4]}]}
    assert WorkWeekPattern.from_json({"kind": "WEEKLY", "weekly_off_days": [6, 5]}).to_json() == W.MON_FRI.to_json()
    assert W.MON_FRI.content_hash() == W.rules_hash({"nth_weekday_offs": [], "weekly_off_days": [5, 6]})


@pytest.mark.parametrize("pattern", [ALT_SAT, HALF_SAT, FRI_HALF, NTH_2_4,
                                     WorkWeekPattern.from_json({"weekly_off_days": [5, 6], "rest_days": [6]})])
def test_round_trip_is_canonical(pattern):
    data = pattern.to_json()
    assert WorkWeekPattern.from_json(data) == pattern
    assert WorkWeekPattern.from_json(data).to_json() == data


def test_cycle_json_shape():
    assert ALT_SAT.to_json() == {"kind": "CYCLE", "cycle_length_days": 14, "anchor_date": "2027-01-04",
                                 "cycle_off_days": [6, 12, 13]}
    assert HALF_SAT.to_json()["half_day_offs"] == [{"weekday": 5, "portion": "SECOND_HALF"}]


@pytest.mark.parametrize(("data", "code"), [
    ({"kind": "CYCLE", "cycle_length_days": 14, "anchor_date": "2027-01-04", "cycle_off_days": [6],
      "weekly_off_days": [6]}, "KIND_MISMATCH"),
    ({"kind": "WEEKLY", "weekly_off_days": [6], "cycle_length_days": 7}, "KIND_MISMATCH"),
    ({"kind": "CYCLE", "cycle_length_days": 1, "anchor_date": "2027-01-04", "cycle_off_days": []}, "INVALID_CYCLE"),
    ({"kind": "CYCLE", "cycle_length_days": 57, "anchor_date": "2027-01-04", "cycle_off_days": []}, "INVALID_CYCLE"),
    ({"kind": "CYCLE", "cycle_length_days": 14, "cycle_off_days": [6]}, "ANCHOR_REQUIRED"),
    ({"kind": "CYCLE", "cycle_length_days": 4, "anchor_date": "2027-01-04", "cycle_off_days": [0, 1, 2, 3]},
     "NO_WORKING_DAYS"),
    ({"kind": "CYCLE", "cycle_length_days": 4, "anchor_date": "2027-01-04", "cycle_off_days": [4]}, "INVALID_PATTERN"),
    ({"kind": "CYCLE", "cycle_length_days": 4, "anchor_date": "2027-01-04", "cycle_off_days": [1],
      "half_day_offs": [{"cycle_day": 1, "portion": "FIRST_HALF"}]}, "REDUNDANT_RULE"),
    ({"kind": "FORTNIGHT"}, "INVALID_PATTERN"),
    ({"weekly_off_days": [6], "half_day_offs": [{"weekday": 6, "portion": "FIRST_HALF"}]}, "REDUNDANT_RULE"),  # V-5
    ({"weekly_off_days": [6], "half_day_offs": [{"weekday": 5, "portion": "EVENING"}]}, "INVALID_PORTION"),  # V-7
    ({"weekly_off_days": [6], "half_day_offs": [{"weekday": 5, "portion": "FIRST_HALF"},
                                                {"weekday": 5, "portion": "SECOND_HALF"}]}, "DUPLICATE_RULE"),
    # V-6: a half rule next to an nth full rule must list nth, disjoint from the full rule (LAST meets 4th/5th)
    ({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [2, 4]}],
      "half_day_offs": [{"weekday": 5, "portion": "SECOND_HALF"}]}, "OVERLAPPING_RULE"),
    ({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [2, 4]}],
      "half_day_offs": [{"weekday": 5, "portion": "SECOND_HALF", "nth": [1, 4]}]}, "OVERLAPPING_RULE"),
    ({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [-1]}],
      "half_day_offs": [{"weekday": 5, "portion": "SECOND_HALF", "nth": [5]}]}, "OVERLAPPING_RULE"),
    ({"weekly_off_days": [5, 6], "rest_days": [3]}, "INVALID_REST_DAYS"),
    ({"weekly_off_days": [6], "unknown": 1}, "INVALID_PATTERN"),
])
def test_v_rules_refuse_with_codes(data, code):
    with pytest.raises(WorkWeekError) as caught:
        WorkWeekPattern.from_json(data)
    assert caught.value.code == code


def test_half_rule_with_disjoint_occurrences_is_valid():
    """Mon-Sat, 2nd/4th Saturday off, other Saturdays afternoon off (§3.2 example)."""
    p = WorkWeekPattern.from_json({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [2, 4]}],
                                   "half_day_offs": [{"weekday": 5, "portion": "SECOND_HALF", "nth": [1, 3, 5]}]})
    jan = saturdays(2027, 1)  # 2, 9, 16, 23, 30
    assert [weekly_off_portion(p, d) for d in jan] == [SECOND_HALF, FULL, SECOND_HALF, FULL, SECOND_HALF]


# ---------------------------------------------------------------------------------------------- LH-WWK-05
def test_lh_wwk_05_nth_saturdays_versus_alternate_saturdays_january_2027():
    jan = saturdays(2027, 1)
    assert jan == [D(2027, 1, 2), D(2027, 1, 9), D(2027, 1, 16), D(2027, 1, 23), D(2027, 1, 30)]
    assert [is_weekly_off(NTH_2_4, d) for d in jan] == [False, True, False, True, False]
    assert [is_weekly_off(ALT_SAT, d) for d in jan] == [True, False, True, False, True]
    for day in (D(2027, 2, 13), D(2027, 2, 27)):  # the two patterns agree in February
        assert is_weekly_off(NTH_2_4, day) and is_weekly_off(ALT_SAT, day)
    # Priya's annual leave Thu 2027-01-07..Wed 2027-01-13 (WORKING_DAYS): 5 under nth, 6 under the cycle
    assert W.scheduled_working_days(NTH_2_4, D(2027, 1, 7), D(2027, 1, 13)) == 5
    assert W.scheduled_working_days(ALT_SAT, D(2027, 1, 7), D(2027, 1, 13)) == 6


@pytest.mark.parametrize(("year", "leap", "second", "fourth"), [
    (2024, True, D(2024, 2, 10), D(2024, 2, 24)),
    (2026, False, D(2026, 2, 14), D(2026, 2, 28)),
    (2027, False, D(2027, 2, 13), D(2027, 2, 27)),
    (2028, True, D(2028, 2, 12), D(2028, 2, 26)),
])
def test_lh_wwk_05_second_and_fourth_saturdays_in_leap_and_non_leap_years(year, leap, second, fourth):
    """February in leap and non-leap years, and every month: the 2nd Saturday is on days 8-14, the 4th on 22-28."""
    assert (D(year, 12, 31) - D(year, 1, 1)).days + 1 == (366 if leap else 365)
    feb_offs = [d for d in saturdays(year, 2) if is_weekly_off(NTH_2_4, d)]
    assert feb_offs == [second, fourth]
    if leap:  # 29 Feb is a weekday rule like any other date
        assert weekly_off_portion(NTH_2_4, D(year, 2, 29)) in (None, FULL)
    total = 0
    for month in range(1, 13):
        offs = [d for d in saturdays(year, month) if is_weekly_off(NTH_2_4, d)]
        assert len(offs) == 2 and 8 <= offs[0].day <= 14 and 22 <= offs[1].day <= 28
        total += len(offs)
    assert total == 24  # 24 Saturday offs a year whatever the year
    sundays = sum(1 for d in W.dates_between(D(year, 1, 1), D(year, 12, 31)) if d.weekday() == W.SUNDAY)
    assert len(W.weekly_off_dates(NTH_2_4, D(year, 1, 1), D(year, 12, 31))) == sundays + 24


def test_lh_wwk_05_29_february_2028_is_a_tuesday_working_day_and_counts():
    assert D(2028, 2, 29).weekday() == W.TUESDAY
    assert W.scheduled_working_days(NTH_2_4, D(2028, 2, 1), D(2028, 2, 29)) == 29 - 4 - 2  # 4 Sundays, 2 Saturdays
    assert W.scheduled_working_days(NTH_2_4, D(2027, 2, 1), D(2027, 2, 28)) == 28 - 4 - 2


# ---------------------------------------------------------------------------------------------- LH-WWK-06 / 14
def test_lh_wwk_06_cycle_dates_before_the_anchor():
    assert W.cycle_day(ALT_SAT, D(2027, 1, 2)) == 12 and is_weekly_off(ALT_SAT, D(2027, 1, 2))
    assert W.cycle_day(ALT_SAT, D(2026, 12, 19)) == 12 and is_weekly_off(ALT_SAT, D(2026, 12, 19))
    assert W.cycle_day(ALT_SAT, D(2027, 1, 4)) == 0 and not is_weekly_off(ALT_SAT, D(2027, 1, 4))


def test_lh_wwk_14_cycle_shift_pattern_with_anchor_and_crews():
    """Goal 1 reading of LH-WWK-14: rotating crews on one anchored cycle; crews are anchors k days apart (§3.4)."""
    four = WorkWeekPattern.from_json({**W.TEMPLATES["FOUR_ON_FOUR_OFF"].rules, "anchor_date": "2027-01-04"})
    crew_b = WorkWeekPattern.from_json({**W.TEMPLATES["FOUR_ON_FOUR_OFF"].rules, "anchor_date": "2027-01-08"})
    days = list(W.dates_between(D(2027, 1, 4), D(2027, 1, 19)))
    a = ["O" if is_weekly_off(four, d) else "W" for d in days]
    b = ["O" if is_weekly_off(crew_b, d) else "W" for d in days]
    assert "".join(a) == "WWWWOOOOWWWWOOOO"
    assert all(x != y for x, y in zip(a, b))  # crew B works exactly when crew A is off
    panama = WorkWeekPattern.from_json({**W.TEMPLATES["PANAMA_2_2_3"].rules, "anchor_date": "2027-01-04"})
    assert "".join("O" if is_weekly_off(panama, d) else "W" for d in W.dates_between(D(2027, 1, 4), D(2027, 1, 17))) \
        == "WWOOWWWOOWWOOO"
    dupont = WorkWeekPattern.from_json({**W.TEMPLATES["DUPONT"].rules, "anchor_date": "2027-01-04"})
    assert sum(1 for d in W.dates_between(D(2027, 1, 4), D(2027, 1, 31)) if not is_weekly_off(dupont, d)) == 14
    # The anchor never moves: the same date is the same cycle day in any year, before or after the anchor.
    assert W.cycle_day(four, D(2026, 1, 1)) == (D(2026, 1, 1) - D(2027, 1, 4)).days % 8


@pytest.mark.parametrize(("off", "holiday", "expected", "units"), [
    (SECOND_HALF, SECOND_HALF, FIRST_HALF, Decimal("0.50")),  # (a) charges 0.50
    (SECOND_HALF, FIRST_HALF, NONE, Decimal("0")),  # (b) charges 0 -> NO_WORKING_DAYS when alone
    (None, None, FULL, Decimal("1.00")),
    (FULL, None, NONE, Decimal("0")),
    (None, FULL, NONE, Decimal("0")),
    (FIRST_HALF, None, SECOND_HALF, Decimal("0.50")),
    (None, SECOND_HALF, FIRST_HALF, Decimal("0.50")),
    (FULL, FIRST_HALF, NONE, Decimal("0")),
])
def test_lh_wwk_14_half_day_weekly_off_with_half_day_holiday(off, holiday, expected, units):
    assert working_portion(off, holiday) == expected
    assert W.portion_units(expected) == units


def test_lh_wwk_14_store_saturdays_in_january_2027():
    assert weekly_off_portion(HALF_SAT, D(2027, 1, 16)) == SECOND_HALF
    assert working_portion(weekly_off_portion(HALF_SAT, D(2027, 1, 16)), SECOND_HALF) == FIRST_HALF
    assert working_portion(weekly_off_portion(HALF_SAT, D(2027, 1, 23)), FIRST_HALF) == NONE


# ---------------------------------------------------------------------------------------------- LH-WWK-13
def test_lh_wwk_13_nth_validation():
    with pytest.raises(WorkWeekError) as a:  # (a) {1,2,3,4,-1} is every Saturday (V-4 widened)
        WorkWeekPattern.from_json({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [1, 2, 3, 4, -1]}]})
    assert a.value.code == "EVERY_OCCURRENCE"
    with pytest.raises(WorkWeekError) as b:  # (b) nth rule on a weekday already off
        WorkWeekPattern.from_json({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 6, "nth": [2]}]})
    assert b.value.code == "REDUNDANT_RULE"
    fifth = WorkWeekPattern.from_json({"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [5]}]})
    sat_offs = [d for d in W.dates_between(D(2027, 1, 1), D(2027, 12, 31)) if d.weekday() == 5 and is_weekly_off(fifth, d)]
    assert sat_offs == [D(2027, 1, 30), D(2027, 5, 29), D(2027, 7, 31), D(2027, 10, 30)]  # (c)
    with pytest.raises(WorkWeekError) as d:  # (d)
        WorkWeekPattern.from_json({"weekly_off_days": [0, 1, 2, 3, 4, 5, 6]})
    assert d.value.code in ("NO_WORKING_DAYS", "INVALID_PATTERN")
    with pytest.raises(WorkWeekError) as d2:
        WorkWeekPattern(frozenset(range(7)))
    assert d2.value.code == "NO_WORKING_DAYS"


# ---------------------------------------------------------------------------------------------- LH-WWK-04 / 27
def test_lh_wwk_04_rest_days_narrow_which_offs_are_rest_days():
    sg = WorkWeekPattern.from_json({"weekly_off_days": [5, 6], "rest_days": [6]})
    assert is_weekly_off(sg, D(2027, 12, 25)) and not is_rest_day(sg, D(2027, 12, 25))  # Saturday: non-rest off
    assert is_rest_day(sg, D(2027, 12, 26))  # Sunday
    assert is_rest_day(W.MON_FRI, D(2027, 12, 25))  # absent = every full off is a rest day
    cycle = WorkWeekPattern.from_json({**ALT_SAT.to_json(), "rest_days": [13]})
    assert is_rest_day(cycle, D(2027, 1, 17)) and not is_rest_day(cycle, D(2027, 1, 16))


def test_lh_wwk_27_half_day_weekly_off_is_not_a_rest_day():
    day = D(2027, 5, 1)  # Saturday, SECOND_HALF off, FULL public holiday
    assert weekly_off_portion(HALF_SAT, day) == SECOND_HALF
    assert not is_weekly_off(HALF_SAT, day) and not is_rest_day(HALF_SAT, day)
    assert working_portion(weekly_off_portion(HALF_SAT, day), FULL) == NONE


# ---------------------------------------------------------------------------------------------- LH-WWK-16 / 17
def test_lh_wwk_16_seven_day_week_fails_both_rest_rules():
    seven = WorkWeekPattern.from_json({"weekly_off_days": []})
    found = W.check_version(None, seven, D(2027, 3, 1), IN_LIMITS)
    rules = {v.rule: v for v in found}
    assert rules["R-1"].count == 7 and rules["R-1"].limit == 6
    assert rules["R-2"].count > 10 and rules["R-2"].limit == 10
    assert all(v.code == "WEEKLY_REST_EXCEEDED" for v in found)
    assert W.check_version(None, W.MON_SAT, D(2027, 3, 1), IN_LIMITS) == []


def test_lh_wwk_16_declared_working_days_on_a_mon_fri_office():
    """Employee-specific check: holidays break runs (Fri 2027-12-24 is a holiday)."""
    def schedule(extra):
        out = []
        for d in W.dates_between(D(2027, 12, 6), D(2027, 12, 31)):
            works = (not is_weekly_off(W.MON_FRI, d) or d in extra) and d != D(2027, 12, 24)
            out.append((d, works))
        return out
    assert W.rest_check(schedule({D(2027, 12, 18)}), IN_LIMITS) == []
    found = W.rest_check(schedule({D(2027, 12, 18), D(2027, 12, 19)}), IN_LIMITS)
    r1 = next(v for v in found if v.rule == "R-1")
    r2 = next(v for v in found if v.rule == "R-2")
    assert r1.count == 7 and r1.start <= D(2027, 12, 13) and r1.end >= D(2027, 12, 19)
    assert (r2.start, r2.end, r2.count) == (D(2027, 12, 13), D(2027, 12, 23), 11)


def test_lh_wwk_17_limit_broken_only_at_the_version_junction():
    v2 = WorkWeekPattern.from_json({"kind": "CYCLE", "cycle_length_days": 14, "anchor_date": "2027-04-01",
                                    "cycle_off_days": [8, 9, 13]})
    eff = D(2027, 4, 1)
    consecutive = RestLimits(max_consecutive_days=10)  # the case checks R-2 only ("IN max_consecutive_days 10")
    assert W.junction_window(v2, eff, consecutive) == (D(2027, 3, 22), D(2027, 4, 25))
    # Each version alone passes (longest runs 6 and 8)
    assert W.check_version(None, W.MON_SAT, D(2027, 3, 1), consecutive) == []
    assert W.check_version(None, v2, eff, consecutive) == []
    found = W.check_version(W.MON_SAT, v2, eff, consecutive)
    r2 = [v for v in found if v.rule == "R-2"]
    assert [(v.start, v.end, v.count) for v in r2] == [(D(2027, 3, 29), D(2027, 4, 8), 11)]
    assert "11 days in a row" in r2[0].message()


# ---------------------------------------------------------------------------------------------- versions (LH-WWK-20, 08)
def test_lh_wwk_20_version_transition_keeps_past_dates():
    """Goal 1 reading of LH-WWK-20: a version from 2027-04-01 changes only dates on or after it."""
    v1 = PatternVersion(1, D(2000, 1, 1), None, NTH_2_4)
    plan = W.plan_version([v1], D(2027, 4, 1))
    assert plan.close == (1, D(2027, 3, 31)) and plan.delete == () and plan.new_version == 2
    versions = W.apply_plan([v1], plan, D(2027, 4, 1), W.MON_SAT)
    assert [(v.version, v.effective_from, v.effective_to) for v in versions] == [
        (1, D(2000, 1, 1), D(2027, 3, 31)), (2, D(2027, 4, 1), None)]
    # History: every date before the boundary still resolves to version 1 (2nd/4th Saturdays stay off)
    for day in (D(2026, 10, 10), D(2027, 3, 13), D(2027, 3, 27)):
        assert W.version_on(versions, day).version == 1 and is_weekly_off(W.version_on(versions, day).pattern, day)
    # From the boundary on, version 2 (Mon-Sat): the 2nd Saturday of April is a working day
    assert W.version_on(versions, D(2027, 4, 10)).version == 2
    assert not is_weekly_off(W.version_on(versions, D(2027, 4, 10)).pattern, D(2027, 4, 10))
    changes = W.class_changes(NTH_2_4, W.MON_SAT, D(2027, 4, 1), D(2027, 6, 30))
    assert changes["off_to_working"] == [D(2027, 4, 10), D(2027, 4, 24), D(2027, 5, 8), D(2027, 5, 22),
                                         D(2027, 6, 12), D(2027, 6, 26)]
    assert changes["working_to_off"] == []


def test_lh_wwk_20_twelve_hour_night_shift_belongs_to_its_start_date():
    """Catalogue LH-WWK-20: 19:00-07:00, 60 min unpaid; FULL HOUR leave 660 min, FIRST_HALF 330 min (evening part)."""
    night = se.ShiftVersionData(start_time="19:00", end_time="07:00", unpaid_break_minutes=60)
    inst = se.build_instance(night, D(2027, 1, 9), IST)
    assert inst.shift_date == D(2027, 1, 9) and inst.crosses_midnight
    assert inst.gross_minutes == 720 and inst.scheduled_net_minutes == 660
    assert Decimal(inst.scheduled_net_minutes) / 60 == Decimal("11")
    assert inst.first_half.minutes == 330 and inst.first_half.start_at < inst.second_half.start_at
    # The day class used is the shift date's: the Sunday (an off day) does not exempt the Saturday night shift.
    hospital = WorkWeekPattern.from_json({"weekly_off_days": [6]})
    assert not is_weekly_off(hospital, inst.shift_date) and is_weekly_off(hospital, D(2027, 1, 10))


def test_lh_wwk_08_same_effective_from_as_a_future_version_replaces_it():
    v1 = PatternVersion(1, D(2000, 1, 1), D(2027, 3, 31), W.MON_FRI)
    v2 = PatternVersion(2, D(2027, 4, 1), None, W.MON_SAT)
    plan = W.plan_version([v1, v2], D(2027, 4, 1))
    assert plan.close is None and plan.delete == (2,) and plan.new_version == 3
    after = W.apply_plan([v1, v2], plan, D(2027, 4, 1), NTH_2_4)
    assert [(v.version, v.effective_from, v.effective_to) for v in after] == [
        (1, D(2000, 1, 1), D(2027, 3, 31)), (3, D(2027, 4, 1), None)]


def test_tiling_rejects_gaps_and_overlaps():
    with pytest.raises(WorkWeekError):
        W.check_tiling([PatternVersion(1, D(2027, 1, 1), D(2027, 1, 31), W.MON_FRI),
                        PatternVersion(2, D(2027, 2, 2), None, W.MON_FRI)])
    with pytest.raises(WorkWeekError):
        W.check_tiling([PatternVersion(1, D(2027, 1, 1), None, W.MON_FRI),
                        PatternVersion(2, D(2027, 2, 1), None, W.MON_FRI)])


# ---------------------------------------------------------------------------------------------- shifts (§7)
def test_lh_wwk_01_half_day_boundary_break_centred_on_the_midpoint():
    gen = se.ShiftVersionData(start_time="09:00", end_time="18:00", unpaid_break_minutes=60)
    inst = se.build_instance(gen, D(2026, 11, 24), IST)
    assert inst.scheduled_net_minutes == 480
    assert (inst.first_half.minutes, inst.second_half.minutes) == (240, 240)
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert inst.second_half.start_at.astimezone(ist).strftime("%H:%M") == "14:00"
    assert inst.first_half.end_at.astimezone(ist).strftime("%H:%M") == "13:00"


def test_lh_wwk_21_compressed_four_day_week():
    shift = se.ShiftVersionData(start_time="07:00", end_time="17:30", unpaid_break_minutes=30)
    days = list(W.dates_between(D(2027, 3, 1), D(2027, 3, 5)))
    worked = [d for d in days if not is_weekly_off(MON_THU, d)]
    assert worked == days[:4] and is_weekly_off(MON_THU, D(2027, 3, 5))
    assert sum(se.scheduled_net_minutes(shift, d, NY) for d in worked) == 2400  # 40.00 h
    assert W.scheduled_working_days(MON_THU, D(2027, 3, 5), D(2027, 3, 5)) == 0  # Friday only -> NO_WORKING_DAYS
    # Thanksgiving Thu 2027-11-25 holiday leaves nothing; the day after is a weekly off anyway
    assert working_portion(weekly_off_portion(MON_THU, D(2027, 11, 25)), FULL) == NONE
    assert is_weekly_off(MON_THU, D(2027, 11, 26))


def test_lh_wwk_22_fixed_day_part_timer_keeps_fte_one():
    pat = WorkWeekPattern.from_json({"weekly_off_days": [2, 4, 5, 6]})
    shift = se.ShiftVersionData(start_time="09:00", end_time="17:00")
    assert not is_weekly_off(pat, D(2027, 3, 1))
    assert se.scheduled_net_minutes(shift, D(2027, 3, 1), NY) == 480
    assert se.scheduled_net_minutes(shift, D(2027, 3, 1), NY, fte=Decimal("0.6")) == 288  # FTE_DOUBLE_REDUCTION case
    assert W.scheduled_working_days(pat, D(2027, 3, 1), D(2027, 3, 7)) == 3


def test_lh_wwk_23_flexible_shift_and_no_shift_use_standard_hours():
    flexible = se.ShiftVersionData(kind="FLEXIBLE", start_time="08:00", end_time="20:00")
    assert se.scheduled_net_minutes(flexible, D(2027, 3, 2), NY, standard_minutes=480) == 480
    assert se.scheduled_net_minutes(None, D(2027, 3, 2), NY, standard_minutes=480) == 480


def test_lh_wwk_25_uae_week_friday_half_day():
    assert weekly_off_portion(FRI_HALF, D(2027, 1, 8)) == SECOND_HALF
    assert working_portion(weekly_off_portion(FRI_HALF, D(2027, 1, 8)), None) == FIRST_HALF
    assert W.portion_units(working_portion(SECOND_HALF, None)) == Decimal("0.50")
    assert working_portion(weekly_off_portion(FRI_HALF, D(2027, 12, 3)), FULL) == NONE
    assert is_weekly_off(FRI_HALF, D(2027, 1, 9)) and is_weekly_off(FRI_HALF, D(2027, 1, 10))


def test_lh_wwk_26_crew_offs_on_consecutive_holidays_leave_two_following_working_days():
    """Pattern side of the chain rule: the crew is off Thu 2027-12-02 and Fri 12-03, works Sat 12-04 and Sun 12-05."""
    crew = WorkWeekPattern.from_json({"kind": "CYCLE", "cycle_length_days": 8, "anchor_date": "2027-11-26",
                                      "cycle_off_days": [4, 5, 6, 7]})
    assert [is_weekly_off(crew, d) for d in W.dates_between(D(2027, 12, 2), D(2027, 12, 5))] == [True, True, False, False]
    taken, substitutes = set(), []
    for holiday in (D(2027, 12, 2), D(2027, 12, 3)):  # ascending, each assigned substitute joins the taken set
        day = holiday + dt.timedelta(days=1)
        while is_weekly_off(crew, day) or day in taken or day in (D(2027, 12, 2), D(2027, 12, 3)):
            day += dt.timedelta(days=1)
        taken.add(day)
        substitutes.append(day)
    assert substitutes == [D(2027, 12, 4), D(2027, 12, 5)]


def test_lh_wwk_02_ramadan_reduced_hours_are_a_temporary_shift_version_not_a_pattern_change():
    """Shift 09:00-18:00 (net 480) becomes 09:00-16:00 (net 360) for Ramadan 2027 only; the work week is unchanged."""
    usual = se.ShiftVersionData(start_time="09:00", end_time="18:00", unpaid_break_minutes=60)
    ramadan = se.ShiftVersionData(start_time="09:00", end_time="16:00", unpaid_break_minutes=60)
    versions = [(D(2000, 1, 1), D(2027, 2, 7), usual), (D(2027, 2, 8), D(2027, 3, 9), ramadan),
                (D(2027, 3, 10), None, usual)]

    def minutes(day):
        version = next(v for start, end, v in versions if start <= day and (end is None or day <= end))
        return se.scheduled_net_minutes(version, day, "Asia/Dubai")

    assert minutes(D(2027, 2, 16)) == 360  # FULL HOUR PTO charges 6.00 h
    assert minutes(D(2027, 2, 5)) == 480 and minutes(D(2027, 3, 10)) == 480
    assert not is_weekly_off(W.MON_FRI, D(2027, 2, 16))  # weekly offs do not move with the hours


@pytest.mark.parametrize(("pattern", "day", "declared", "expected"), [
    (W.MON_FRI, D(2027, 12, 18), FULL, None),  # Asha: Sat off -> working (declared_working_day)
    (NTH_2_4, D(2027, 12, 18), FULL, None),  # Ravi: 3rd Saturday, already working
    (HALF_SAT, D(2027, 12, 18), FULL, None),  # store staff: FULL declared on a SECOND_HALF off -> fully working
    (W.MON_FRI, D(2027, 12, 18), FIRST_HALF, SECOND_HALF),  # a half make-up day leaves the other half off
])
def test_lh_wwk_03_declared_working_day_reaches_weekly_patterns(pattern, day, declared, expected):
    assert W.apply_declared_working_day(pattern, weekly_off_portion(pattern, day), declared) == expected


def test_lh_wwk_10_declared_working_days_stop_applying_to_cycle_patterns():
    four = WorkWeekPattern.from_json({**W.TEMPLATES["FOUR_ON_FOUR_OFF"].rules, "anchor_date": "2027-07-01"})
    sunday = D(2027, 8, 22)
    off = weekly_off_portion(four, sunday)
    assert W.apply_declared_working_day(four, off, FULL) == off  # crews follow their cycle
    assert W.apply_declared_working_day(W.MON_SAT, weekly_off_portion(W.MON_SAT, sunday), FULL) is None


def test_lh_wwk_15_removing_a_declared_working_day_restores_the_weekly_off():
    day = D(2027, 12, 18)
    assert W.apply_declared_working_day(W.MON_FRI, weekly_off_portion(W.MON_FRI, day), FULL) is None
    assert W.apply_declared_working_day(W.MON_FRI, weekly_off_portion(W.MON_FRI, day), None) == FULL
    # Asha's leave Thu 12-16..Mon 12-20: 3 working days without the make-up day, 4 with it
    days = list(W.dates_between(D(2027, 12, 16), D(2027, 12, 20)))
    with_day = [d for d in days if W.apply_declared_working_day(
        W.MON_FRI, weekly_off_portion(W.MON_FRI, d), FULL if d == day else None) is None]
    assert len(with_day) == 4 and W.scheduled_working_days(W.MON_FRI, days[0], days[-1]) == 3


def test_lh_wwk_32_weekly_offs_count_half_days_as_half():
    marks = W.day_marks(HALF_SAT, D(2027, 1, 1), D(2027, 1, 31))
    full = sum(1 for m in marks if m.portion == FULL)
    half = sum(1 for m in marks if m.portion in (FIRST_HALF, SECOND_HALF))
    assert (full, half) == (5, 5)
    assert full + Decimal("0.5") * half == Decimal("7.5")
    assert 31 - (full + Decimal("0.5") * half) == Decimal("23.5")


def test_lh_tz_01_night_shift_date_is_the_local_start_date():
    c = se.ShiftVersionData(start_time="22:00", end_time="06:00")
    monday = se.build_instance(c, D(2027, 1, 25), IST)
    assert monday.shift_date == D(2027, 1, 25) and monday.crosses_midnight
    assert not is_weekly_off(W.MON_SAT, D(2027, 1, 25))  # an ordinary working shift though 6 h fall on 26 Jan
    assert se.build_instance(c, D(2027, 1, 26), IST).shift_date == D(2027, 1, 26)


@pytest.mark.parametrize(("day", "minutes", "hours"), [
    (D(2026, 10, 31), 510, Decimal("8.50")), (D(2027, 3, 13), 390, Decimal("6.50")), (D(2026, 11, 7), 450, Decimal("7.50")),
])
def test_lh_tz_02_new_york_night_shift_minutes_on_dst_nights(day, minutes, hours):
    night = se.ShiftVersionData(start_time="22:00", end_time="06:00", unpaid_break_minutes=30)
    assert se.scheduled_net_minutes(night, day, NY) == minutes
    assert (Decimal(minutes) / 60).quantize(Decimal("0.01")) == hours


@pytest.mark.parametrize(("version", "day", "zone", "gross"), [
    (("22:00", "06:00"), D(2026, 10, 24), LON, 540),
    (("22:00", "06:00"), D(2027, 3, 27), LON, 420),
    (("02:30", "10:30"), D(2027, 3, 14), NY, 420),
    (("01:30", "09:30"), D(2026, 11, 1), NY, 540),
])
def test_lh_tz_10_dst_skipped_and_repeated_hours(version, day, zone, gross):
    inst = se.build_instance(se.ShiftVersionData(start_time=version[0], end_time=version[1]), day, zone)
    assert inst.gross_minutes == gross and inst.shift_date == day


# ---------------------------------------------------------------------------------------------- templates, marks
def test_templates_catalogue():
    assert {"MON_FRI", "MON_SAT", "MON_SAT_2ND_4TH_SAT_OFF", "MON_SAT_HALF_SAT", "SUN_THU", "SAT_THU", "FRI_HALF",
            "MON_THU", "ALT_SAT_OFF", "FOUR_ON_FOUR_OFF", "PANAMA_2_2_3", "DUPONT"} == set(W.TEMPLATES)
    assert W.TEMPLATES["MON_SAT_2ND_4TH_SAT_OFF"].rules_for(D(2027, 1, 1)) == NTH_2_4.to_json()
    assert W.TEMPLATES["ALT_SAT_OFF"].rules_for(D(2027, 1, 1))["anchor_date"] == "2027-01-04"  # next Monday
    assert W.TEMPLATES["ALT_SAT_OFF"].rules_for(D(2027, 1, 4))["anchor_date"] == "2027-01-04"  # a Monday itself
    assert [t.code for t in W.TEMPLATES.values() if t.needs_half_day_offs] == ["MON_SAT_HALF_SAT", "FRI_HALF"]
    for code, preset in W.PRESETS.items():
        assert WorkWeekPattern.from_json(W.TEMPLATES[code].rules_for(D(2027, 1, 1))) == preset


def test_day_marks_for_the_ui_preview_of_january_2027():
    """09-ui-ux/02 §19 check: Jan 2027 Sun + 2nd/4th Sat gives 7 offs and 24 working days."""
    marks = W.day_marks(NTH_2_4, D(2027, 1, 1), D(2027, 1, 31))
    offs = [m.date.day for m in marks if m.portion == FULL]
    assert offs == [3, 9, 10, 17, 23, 24, 31] and len(marks) - len(offs) == 24
    assert marks[8].to_json() == {"date": "2027-01-09", "weekday": 5, "portion": "FULL", "rest_day": True,
                                  "cycle_day": None, "occurrence": 2}
    with pytest.raises(WorkWeekError) as caught:
        W.day_marks(NTH_2_4, D(2027, 1, 1), D(2028, 6, 1))
    assert caught.value.code == "RANGE_TOO_LONG"


def test_summary_text():
    assert NTH_2_4.summary() == "Sun off; Sat 2nd/4th off"
    assert W.MON_FRI.summary() == "Sat, Sun off"
    assert HALF_SAT.summary() == "Sun off; Sat afternoon off"
    assert ALT_SAT.summary() == "14-day cycle from 2027-01-04, 3 off"
