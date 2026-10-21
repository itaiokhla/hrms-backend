"""Work-week patterns (3.4 §3): weekday sets, nth-weekday rules, scheduled working days."""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payroll import workweek as W
from app.payroll.workweek import (
    FRIDAY,
    LAST,
    MON_FRI,
    MON_SAT,
    MON_SAT_2ND_4TH_SAT_OFF,
    SAT_THU,
    SATURDAY,
    SUN_THU,
    SUNDAY,
    THURSDAY,
    WorkWeekError,
    WorkWeekPattern,
    is_weekly_off,
    scheduled_working_days,
)

FEB_1, FEB_28 = dt.date(2026, 2, 1), dt.date(2026, 2, 28)  # February 2026 starts on a Sunday


@pytest.mark.parametrize(
    ("pattern", "off_weekdays"),
    [(MON_FRI, {5, 6}), (MON_SAT, {6}), (SUN_THU, {4, 5}), (SAT_THU, {4})],
)
def test_presets_mark_their_weekends(pattern: WorkWeekPattern, off_weekdays: set[int]) -> None:
    week = [dt.date(2026, 2, 2) + dt.timedelta(days=i) for i in range(7)]  # Monday .. Sunday
    assert {day.weekday() for day in week if is_weekly_off(pattern, day)} == off_weekdays
    assert W.PRESETS["MON_FRI"] is MON_FRI


def test_february_2026_has_24_working_days_on_a_six_day_week() -> None:
    """3.4 §5.2: Monday-Saturday, February 2026 → 24 working days and 4 Sundays."""
    assert scheduled_working_days(MON_SAT, FEB_1, FEB_28) == 24
    assert W.weekly_off_dates(MON_SAT, FEB_1, FEB_28) == tuple(dt.date(2026, 2, d) for d in (1, 8, 15, 22))
    assert scheduled_working_days(MON_SAT, dt.date(2026, 2, 16), FEB_28) == 12
    assert scheduled_working_days(MON_FRI, FEB_1, FEB_28) == 20


def test_second_and_fourth_saturday_off() -> None:
    saturdays = [dt.date(2026, 2, d) for d in (7, 14, 21, 28)]
    assert [is_weekly_off(MON_SAT_2ND_4TH_SAT_OFF, day) for day in saturdays] == [False, True, False, True]
    assert is_weekly_off(MON_SAT_2ND_4TH_SAT_OFF, dt.date(2026, 2, 15))  # Sundays stay off
    assert scheduled_working_days(MON_SAT_2ND_4TH_SAT_OFF, FEB_1, FEB_28) == 22


def test_last_and_fifth_occurrence_rules() -> None:
    last_friday = WorkWeekPattern(frozenset({SUNDAY}), ((FRIDAY, frozenset({LAST})),))
    assert is_weekly_off(last_friday, dt.date(2026, 1, 30))  # last Friday of January (also the 5th)
    assert not is_weekly_off(last_friday, dt.date(2026, 1, 23))
    assert is_weekly_off(last_friday, dt.date(2026, 2, 27))  # last Friday of February is the 4th
    fifth_saturday = WorkWeekPattern(frozenset({SUNDAY}), ((SATURDAY, frozenset({5})),))
    assert is_weekly_off(fifth_saturday, dt.date(2026, 1, 31))
    assert not any(is_weekly_off(fifth_saturday, dt.date(2026, 2, d)) for d in (7, 14, 21, 28))
    assert W.occurrence_in_month(dt.date(2026, 1, 31)) == 5
    assert W.is_last_occurrence_in_month(dt.date(2026, 1, 25)) and not W.is_last_occurrence_in_month(dt.date(2026, 1, 24))


def test_paid_holidays_reduce_working_days_only_on_working_weekdays() -> None:
    on_working_day, on_sunday = dt.date(2026, 2, 16), dt.date(2026, 2, 22)
    assert scheduled_working_days(MON_SAT, FEB_1, FEB_28, {on_working_day}) == 23
    assert scheduled_working_days(MON_SAT, FEB_1, FEB_28, {on_sunday}) == 24
    assert not W.is_scheduled_working_day(MON_SAT, on_working_day, {on_working_day})
    assert W.is_scheduled_working_day(MON_SAT, dt.date(2026, 2, 17), {on_working_day})


def test_ec_att_21_employee_work_week_decides_the_working_days() -> None:
    """EC-ATT-21: a 5-day shift week and a 6-day week see the same company calendar differently."""
    saturday_holiday = {dt.date(2026, 2, 14)}
    assert scheduled_working_days(MON_SAT, FEB_1, FEB_28, saturday_holiday) == 23
    assert scheduled_working_days(MON_FRI, FEB_1, FEB_28, saturday_holiday) == 20


def test_ec_att_21_holiday_on_the_patterns_weekly_off_changes_nothing() -> None:
    """EC-ATT-21 (negative): a Friday holiday is a working day lost for MON_SAT, not for SAT_THU."""
    friday_holiday = {dt.date(2026, 2, 13)}
    assert scheduled_working_days(SAT_THU, FEB_1, FEB_28, friday_holiday) == scheduled_working_days(SAT_THU, FEB_1, FEB_28)
    assert scheduled_working_days(MON_SAT, FEB_1, FEB_28, friday_holiday) == 23


def test_patterns_are_canonical_values_and_round_trip_json() -> None:
    a = WorkWeekPattern([SUNDAY], [(SATURDAY, {4, 2})])
    b = WorkWeekPattern(frozenset({SUNDAY}), ((SATURDAY, frozenset({2, 4})),))
    assert a == b and hash(a) == hash(b) and a == MON_SAT_2ND_4TH_SAT_OFF
    data = a.to_json()
    assert data == {"weekly_off_days": [6], "nth_weekday_offs": [{"weekday": 5, "nth": [2, 4]}]}
    assert WorkWeekPattern.from_json(data) == a
    seven_day_week = WorkWeekPattern()
    assert WorkWeekPattern.from_json(seven_day_week.to_json()) == seven_day_week
    assert not any(is_weekly_off(seven_day_week, day) for day in W.dates_between(FEB_1, FEB_28))


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"weekly_off_days": frozenset({7})}, "INVALID_WEEKDAY"),
        ({"weekly_off_days": frozenset({-1})}, "INVALID_WEEKDAY"),
        ({"weekly_off_days": frozenset({True})}, "INVALID_WEEKDAY"),
        ({"weekly_off_days": "56"}, "INVALID_PATTERN"),
        ({"weekly_off_days": frozenset(range(7))}, "NO_WORKING_DAYS"),
        ({"nth_weekday_offs": ((SATURDAY, frozenset({0})),)}, "INVALID_OCCURRENCE"),
        ({"nth_weekday_offs": ((SATURDAY, frozenset({6})),)}, "INVALID_OCCURRENCE"),
        ({"nth_weekday_offs": ((SATURDAY, frozenset()),)}, "EMPTY_RULE"),
        ({"weekly_off_days": frozenset({SATURDAY}), "nth_weekday_offs": ((SATURDAY, frozenset({2})),)}, "REDUNDANT_RULE"),
        ({"nth_weekday_offs": ((SATURDAY, frozenset({2})), (SATURDAY, frozenset({4})))}, "DUPLICATE_RULE"),
        ({"nth_weekday_offs": ((THURSDAY, frozenset({1, 2, 3, 4, 5})),)}, "EVERY_OCCURRENCE"),
        ({"nth_weekday_offs": ((SATURDAY,),)}, "INVALID_PATTERN"),
    ],
)
def test_invalid_patterns_are_refused_with_codes(kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(WorkWeekError) as caught:
        WorkWeekPattern(**kwargs)  # type: ignore[arg-type]
    assert caught.value.code == code


@pytest.mark.parametrize(
    "data",
    [
        {"weekly_off_days": [6], "extra": 1},
        {"weekly_off_days": "6"},
        {"weekly_off_days": [6, 6]},
        {"nth_weekday_offs": [{"weekday": 5}]},
        {"nth_weekday_offs": [{"weekday": 5, "nth": [2, 2]}]},
        {"nth_weekday_offs": [{"weekday": 5, "nth": [[2]]}]},
        [6],
    ],
)
def test_from_json_refuses_malformed_patterns(data: object) -> None:
    with pytest.raises(WorkWeekError):
        WorkWeekPattern.from_json(data)  # type: ignore[arg-type]


def test_dates_must_be_dates_and_ranges_ordered() -> None:
    with pytest.raises(WorkWeekError) as caught:
        is_weekly_off(MON_SAT, dt.datetime(2026, 2, 1, 9, 0))  # type: ignore[arg-type]
    assert caught.value.code == "INVALID_DATE"
    with pytest.raises(WorkWeekError) as caught:
        scheduled_working_days(MON_SAT, FEB_28, FEB_1)
    assert caught.value.code == "INVALID_RANGE"
    with pytest.raises(WorkWeekError) as caught:
        scheduled_working_days(MON_SAT, FEB_1, FEB_28, {dt.datetime(2026, 2, 2)})  # type: ignore[arg-type]
    assert caught.value.code == "INVALID_DATE"
    assert scheduled_working_days(MON_SAT, FEB_28, FEB_28) == 1


patterns = st.builds(
    lambda off, rule_day, nths: WorkWeekPattern(
        frozenset(off), () if rule_day in off or not nths else ((rule_day, frozenset(nths)),)
    ),
    st.sets(st.integers(0, 6), max_size=3),
    st.integers(0, 6),
    st.sets(st.sampled_from([1, 2, 3, 4, LAST]), max_size=3),
)


@settings(max_examples=200, deadline=None)
@given(
    pattern=patterns,
    start=st.dates(dt.date(2024, 1, 1), dt.date(2028, 12, 31)),
    length=st.integers(0, 70),
    holiday_offsets=st.sets(st.integers(0, 70), max_size=8),
)
def test_property_every_date_is_working_weekly_off_or_holiday(
    pattern: WorkWeekPattern, start: dt.date, length: int, holiday_offsets: set[int]
) -> None:
    end = start + dt.timedelta(days=length)
    holidays = {start + dt.timedelta(days=offset) for offset in holiday_offsets if offset <= length}
    offs = W.weekly_off_dates(pattern, start, end)
    working = W.scheduled_working_dates(pattern, start, end, holidays)
    holidays_on_working_weekdays = {day for day in holidays if not is_weekly_off(pattern, day)}
    assert len(working) + len(offs) + len(holidays_on_working_weekdays) == length + 1
    assert set(working).isdisjoint(offs) and set(working).isdisjoint(holidays)
    assert all(1 <= W.occurrence_in_month(day) <= 5 for day in offs)
    assert WorkWeekPattern.from_json(pattern.to_json()) == pattern
