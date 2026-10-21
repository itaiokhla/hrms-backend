"""Pay calendars (3.5 §2, 3.1 §5.1): periods, pay dates, attendance cycles, cutoffs, tax years, changes."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, localcontext

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.payroll import calendar as C
from app.payroll.calendar import (
    AttendanceCycleMode as A,
    BankingCalendar,
    CalendarError,
    CalendarRules,
    Frequency as F,
    PayCalendar,
    PayDayRule as R,
    PayDayShift as S,
    Weekday,
)

D = dt.date
MON_FRI = BankingCalendar(weekend=frozenset({5, 6}))
INDIA = "04-01"


def rules(**overrides: object) -> CalendarRules:
    values: dict[str, object] = {"banking_calendar": MON_FRI, "tax_year_start": INDIA}
    values.update(overrides)
    return CalendarRules(**values)  # type: ignore[arg-type]


def code_of(excinfo: pytest.ExceptionInfo[CalendarError]) -> str:
    return excinfo.value.code


def spans(periods: list[C.Period]) -> list[tuple[dt.date, dt.date]]:
    return [(p.start, p.end) for p in periods]


# --------------------------------------------------------------------------- EC-CAL-01 monthly clamping


def test_ec_cal_01_start_day_31_clamps_to_month_end_in_a_common_year() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(monthly_start_day=31), D(2027, 1, 31), D(2027, 5, 30))
    assert spans(periods) == [
        (D(2027, 1, 31), D(2027, 2, 27)),
        (D(2027, 2, 28), D(2027, 3, 30)),
        (D(2027, 3, 31), D(2027, 4, 29)),
        (D(2027, 4, 30), D(2027, 5, 30)),
    ]
    C.check_contiguous(periods)


def test_ec_cal_01_start_day_31_clamps_to_29_february_in_a_leap_year() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(monthly_start_day=31), D(2028, 2, 1), D(2028, 3, 1))
    assert spans(periods) == [(D(2028, 1, 31), D(2028, 2, 28)), (D(2028, 2, 29), D(2028, 3, 30))]


def test_ec_cal_01_start_day_30_clamps_only_in_february() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(monthly_start_day=30), D(2027, 1, 30), D(2027, 4, 29))
    assert [p.start for p in periods] == [D(2027, 1, 30), D(2027, 2, 28), D(2027, 3, 30)]


def test_ec_cal_01_boundary_start_day_28_never_clamps() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(monthly_start_day=28), D(2027, 1, 1), D(2027, 12, 31))
    assert {p.start.day for p in periods} == {28}


@pytest.mark.parametrize("day", [0, 32, -1, True, "1"])
def test_ec_cal_01_negative_start_day_out_of_range_is_rejected(day: object) -> None:
    with pytest.raises(CalendarError) as excinfo:
        rules(monthly_start_day=day)
    assert code_of(excinfo) == "INVALID_MONTHLY_START_DAY"


def test_monthly_start_day_1_gives_calendar_months() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(), D(2026, 1, 1), D(2026, 12, 31))
    assert len(periods) == 12
    assert all(p.start.day == 1 and p.end == C._month_end(p.start) for p in periods)


# --------------------------------------------------------------------------- EC-CAL-02 leap days


def test_ec_cal_02_february_2028_has_29_days_and_contains_the_leap_day() -> None:
    feb = C.PayCalendar(F.MONTHLY, rules()).period_containing(D(2028, 2, 29))
    assert (feb.start, feb.end, feb.days) == (D(2028, 2, 1), D(2028, 2, 29), 29)
    assert D(2028, 2, 29) in set(feb.attendance_dates())


def test_ec_cal_02_daily_tax_year_with_a_leap_day_has_366_periods() -> None:
    daily = rules(pay_day_rule=R.LAST_DAY, pay_day_shift=S.NO_SHIFT)
    assert C.periods_in_tax_year(F.DAILY, daily, "2027-28") == 366
    assert C.periods_in_tax_year(F.DAILY, daily, "2026-27") == 365


def test_ec_cal_02_negative_29_february_is_not_a_valid_tax_year_start() -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.parse_tax_year_start("02-29")
    assert code_of(excinfo) == "INVALID_TAX_YEAR_START"


def test_ec_cal_02_semi_monthly_second_half_of_february_ends_on_the_leap_day() -> None:
    periods = C.generate_periods(F.SEMI_MONTHLY, rules(), D(2028, 2, 1), D(2028, 2, 29))
    assert spans(periods) == [(D(2028, 2, 1), D(2028, 2, 15)), (D(2028, 2, 16), D(2028, 2, 29))]


# --------------------------------------------------------------------------- EC-CAL-03 pay-date shifting


SATURDAY_31_OCT = D(2026, 10, 31)


@pytest.mark.parametrize(
    ("shift", "expected"),
    [
        (S.PREVIOUS_BUSINESS_DAY, D(2026, 10, 30)),
        (S.NEXT_BUSINESS_DAY, D(2026, 11, 2)),
        (S.NEAREST_BUSINESS_DAY, D(2026, 10, 30)),
        (S.NO_SHIFT, SATURDAY_31_OCT),
    ],
)
def test_ec_cal_03_weekend_pay_date_shifts_per_rule(shift: S, expected: dt.date) -> None:
    assert SATURDAY_31_OCT.weekday() == 5
    r = rules(pay_day_rule=R.LAST_DAY, pay_day_shift=shift)
    assert C.pay_date(F.MONTHLY, r, D(2026, 10, 1), D(2026, 10, 31)) == expected
    assert C.nominal_pay_date(F.MONTHLY, r, D(2026, 10, 1), D(2026, 10, 31)) == SATURDAY_31_OCT


def test_ec_cal_03_nearest_from_sunday_goes_to_monday() -> None:
    assert C.shift_to_business_day(D(2026, 11, 1), S.NEAREST_BUSINESS_DAY, MON_FRI) == D(2026, 11, 2)


def test_ec_cal_03_nearest_tie_goes_to_the_earlier_day() -> None:
    banking = MON_FRI.with_holidays([D(2026, 10, 30)])  # Friday holiday: Thu and Mon are both 2 days away
    assert C.shift_to_business_day(SATURDAY_31_OCT, S.NEAREST_BUSINESS_DAY, banking) == D(2026, 10, 29)


def test_ec_cal_03_bank_holiday_moves_last_working_day() -> None:
    banking = MON_FRI.with_holidays([D(2026, 11, 30)])  # Monday 30 Nov
    period = C.generate_periods(F.MONTHLY, rules(banking_calendar=banking), D(2026, 11, 1), D(2026, 11, 30))[0]
    assert period.pay_date == D(2026, 11, 27)
    assert period.nominal_pay_date == D(2026, 11, 27)


def test_ec_cal_03_bank_holiday_shifted_next_skips_the_weekend() -> None:
    banking = MON_FRI.with_holidays([D(2026, 10, 30)])
    r = rules(banking_calendar=banking, pay_day_rule=R.DAY_OF_MONTH, pay_day_value=30,
              pay_day_shift=S.NEXT_BUSINESS_DAY)
    assert C.pay_date(F.MONTHLY, r, D(2026, 10, 1), D(2026, 10, 31)) == D(2026, 11, 2)


def test_ec_cal_03_negative_business_day_is_never_moved() -> None:
    for shift in S:
        assert C.shift_to_business_day(D(2026, 10, 30), shift, MON_FRI) == D(2026, 10, 30)


def test_ec_cal_03_negative_unknown_shift_and_no_business_day() -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.shift_to_business_day(SATURDAY_31_OCT, "SOMEDAY", MON_FRI)
    assert code_of(excinfo) == "INVALID_PAY_DAY_SHIFT"
    closed = MON_FRI.with_holidays(D(2026, 9, 1) + dt.timedelta(days=i) for i in range(100))
    with pytest.raises(CalendarError) as excinfo:
        C.shift_to_business_day(SATURDAY_31_OCT, S.PREVIOUS_BUSINESS_DAY, closed)
    assert code_of(excinfo) == "NO_BUSINESS_DAY"


def test_ec_cal_03_weekend_follows_the_country_friday_saturday() -> None:
    uae = BankingCalendar(weekend=frozenset({5, 6}))
    gulf_old = BankingCalendar(weekend=frozenset({4, 5}))  # Friday-Saturday weekend
    friday = D(2026, 10, 30)
    assert uae.is_business_day(friday) and not gulf_old.is_business_day(friday)
    assert C.shift_to_business_day(friday, S.PREVIOUS_BUSINESS_DAY, gulf_old) == D(2026, 10, 29)


@pytest.mark.parametrize("weekend", [frozenset(range(7)), frozenset({7}), frozenset({True}), "56"])
def test_banking_calendar_rejects_invalid_weekends(weekend: object) -> None:
    with pytest.raises(CalendarError) as excinfo:
        BankingCalendar(weekend=weekend)  # type: ignore[arg-type]
    assert code_of(excinfo) == "INVALID_BANKING_CALENDAR"


def test_banking_calendar_rejects_datetime_holidays() -> None:
    with pytest.raises(CalendarError):
        BankingCalendar(weekend=frozenset({5, 6}), holidays=frozenset({dt.datetime(2026, 1, 1)}))


# --------------------------------------------------------------------------- pay-day rules


def test_day_of_month_pay_date_clamps_to_short_months() -> None:
    r = rules(pay_day_rule=R.DAY_OF_MONTH, pay_day_value=31, pay_day_shift=S.NO_SHIFT)
    periods = C.generate_periods(F.MONTHLY, r, D(2027, 2, 1), D(2027, 4, 30))
    assert [p.pay_date for p in periods] == [D(2027, 2, 28), D(2027, 3, 31), D(2027, 4, 30)]


def test_day_of_month_uses_the_month_the_period_ends_in() -> None:
    r = rules(monthly_start_day=26, pay_day_rule=R.DAY_OF_MONTH, pay_day_value=1, pay_day_shift=S.NO_SHIFT)
    period = C.generate_periods(F.MONTHLY, r, D(2026, 11, 1), D(2026, 11, 1))[0]
    assert (period.start, period.end, period.pay_date) == (D(2026, 10, 26), D(2026, 11, 25), D(2026, 11, 1))


def test_days_after_period_end() -> None:
    r = rules(pay_day_rule=R.DAYS_AFTER_PERIOD_END, pay_day_value=5, pay_day_shift=S.NO_SHIFT)
    periods = C.generate_periods(F.WEEKLY, r, D(2026, 10, 19), D(2026, 10, 25))
    assert periods[0].pay_date == D(2026, 10, 30)


@pytest.mark.parametrize("frequency", [F.WEEKLY, F.BI_WEEKLY, F.SEMI_MONTHLY, F.DAILY])
def test_day_of_month_is_monthly_only(frequency: F) -> None:
    r = rules(pay_day_rule=R.DAY_OF_MONTH, pay_day_value=10, biweekly_anchor_date=D(2026, 1, 5))
    with pytest.raises(CalendarError) as excinfo:
        C.validate_rules(frequency, r)
    assert code_of(excinfo) == "PAY_DAY_RULE_UNSUPPORTED"


@pytest.mark.parametrize(("rule", "value", "code"), [
    (R.DAY_OF_MONTH, None, "PAY_DAY_VALUE_REQUIRED"),
    (R.DAY_OF_MONTH, 0, "INVALID_PAY_DAY_VALUE"),
    (R.DAYS_AFTER_PERIOD_END, None, "PAY_DAY_VALUE_REQUIRED"),
])
def test_pay_day_value_is_required_and_bounded(rule: R, value: int | None, code: str) -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.validate_rules(F.MONTHLY, rules(pay_day_rule=rule, pay_day_value=value))
    assert code_of(excinfo) == code


def test_pay_day_value_upper_bound() -> None:
    with pytest.raises(CalendarError) as excinfo:
        rules(pay_day_value=C.MAX_DAYS_AFTER_PERIOD_END + 1)
    assert code_of(excinfo) == "INVALID_PAY_DAY_VALUE"


def test_unknown_rule_values_are_rejected() -> None:
    for field, value, code in (("pay_day_rule", "FIRST_DAY", "INVALID_PAY_DAY_RULE"),
                               ("week_start_day", "MONDAY", "INVALID_WEEKDAY"),
                               ("attendance_cycle_mode", "WHATEVER", "INVALID_ATTENDANCE_MODE"),
                               ("tax_year_basis", "PAY_DAY", "INVALID_TAX_YEAR_BASIS")):
        with pytest.raises(CalendarError) as excinfo:
            rules(**{field: value})
        assert code_of(excinfo) == code
    with pytest.raises(CalendarError) as excinfo:
        C.generate_periods("FORTNIGHTLY", rules(), D(2026, 1, 1), D(2026, 1, 31))
    assert code_of(excinfo) == "INVALID_FREQUENCY"


def test_string_rule_values_become_enums() -> None:
    r = rules(pay_day_rule="LAST_DAY", week_start_day="SUN", pay_day_shift="NO_SHIFT")
    assert r.pay_day_rule is R.LAST_DAY and r.week_start_day is Weekday.SUN and r.week_start_index == 6


# --------------------------------------------------------------------------- EC-CAL-11 every frequency


def test_ec_cal_11_semi_monthly_split_day() -> None:
    periods = C.generate_periods(F.SEMI_MONTHLY, rules(), D(2026, 1, 1), D(2026, 2, 28))
    assert spans(periods) == [(D(2026, 1, 1), D(2026, 1, 15)), (D(2026, 1, 16), D(2026, 1, 31)),
                              (D(2026, 2, 1), D(2026, 2, 15)), (D(2026, 2, 16), D(2026, 2, 28))]
    split27 = C.generate_periods(F.SEMI_MONTHLY, rules(semi_monthly_split_day=27), D(2027, 2, 1), D(2027, 2, 28))
    assert spans(split27) == [(D(2027, 2, 1), D(2027, 2, 27)), (D(2027, 2, 28), D(2027, 2, 28))]


def test_ec_cal_11_negative_split_day_28_is_rejected() -> None:
    with pytest.raises(CalendarError) as excinfo:
        rules(semi_monthly_split_day=28)
    assert code_of(excinfo) == "INVALID_SPLIT_DAY"


def test_ec_cal_11_weekly_starts_on_week_start_day() -> None:
    periods = C.generate_periods(F.WEEKLY, rules(week_start_day=Weekday.SUN), D(2026, 10, 20), D(2026, 11, 2))
    assert spans(periods) == [(D(2026, 10, 18), D(2026, 10, 24)), (D(2026, 10, 25), D(2026, 10, 31)),
                              (D(2026, 11, 1), D(2026, 11, 7))]
    assert all(p.start.weekday() == 6 and p.days == 7 for p in periods)


def test_ec_cal_11_bi_weekly_counts_14_days_from_the_anchor_both_ways() -> None:
    r = rules(biweekly_anchor_date=D(2026, 7, 13))
    periods = C.generate_periods(F.BI_WEEKLY, r, D(2026, 6, 20), D(2026, 7, 27))
    assert spans(periods) == [(D(2026, 6, 15), D(2026, 6, 28)), (D(2026, 6, 29), D(2026, 7, 12)),
                              (D(2026, 7, 13), D(2026, 7, 26)), (D(2026, 7, 27), D(2026, 8, 9))]


def test_ec_cal_11_negative_bi_weekly_anchor_required_and_on_week_start_day() -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.generate_periods(F.BI_WEEKLY, rules(), D(2026, 1, 1), D(2026, 1, 31))
    assert code_of(excinfo) == "BIWEEKLY_ANCHOR_REQUIRED"
    with pytest.raises(CalendarError) as excinfo:
        C.generate_periods(F.BI_WEEKLY, rules(biweekly_anchor_date=D(2026, 7, 14)), D(2026, 1, 1), D(2026, 1, 31))
    assert code_of(excinfo) == "BIWEEKLY_ANCHOR_WEEKDAY"


def test_ec_cal_11_daily_is_one_day_each() -> None:
    periods = C.generate_periods(F.DAILY, rules(pay_day_rule=R.LAST_DAY, pay_day_shift=S.NEXT_BUSINESS_DAY),
                                 D(2026, 10, 30), D(2026, 11, 2))
    assert [p.start for p in periods] == [D(2026, 10, 30), D(2026, 10, 31), D(2026, 11, 1), D(2026, 11, 2)]
    assert all(p.start == p.end for p in periods)
    assert [p.pay_date for p in periods] == [D(2026, 10, 30), D(2026, 11, 2), D(2026, 11, 2), D(2026, 11, 2)]


_FREQUENCIES = st.sampled_from(list(F))
_RULES = st.builds(
    lambda start, split, week, mode, rule, shift, custom: rules(
        monthly_start_day=start, semi_monthly_split_day=split, week_start_day=week,
        biweekly_anchor_date=D(2026, 1, 5) + dt.timedelta(days=list(Weekday).index(week)),
        attendance_cycle_mode=mode, attendance_cycle_start_day=custom, pay_day_rule=rule, pay_day_shift=shift),
    st.integers(1, 31), st.integers(1, 27), st.sampled_from(list(Weekday)),
    st.sampled_from([A.SAME_AS_PERIOD, A.PREVIOUS_PERIOD]), st.sampled_from([R.LAST_DAY, R.LAST_WORKING_DAY]),
    st.sampled_from(list(S)), st.integers(1, 28))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(frequency=_FREQUENCIES, r=_RULES, start=st.dates(D(2024, 1, 1), D(2030, 12, 31)),
       length=st.integers(0, 120))
def test_ec_cal_11_property_periods_are_contiguous_and_cover_the_range(
        frequency: F, r: CalendarRules, start: dt.date, length: int) -> None:
    until = start + dt.timedelta(days=length)
    periods = C.generate_periods(frequency, r, start, until)
    C.check_contiguous(periods)
    C.check_attendance_contiguous(periods)
    assert periods[0].start <= start and periods[-1].end >= until
    assert all(p.end >= start and p.start <= until for p in periods)
    for earlier, later in zip(periods, periods[1:]):
        assert later.number_in_tax_year == (1 if earlier.is_last_in_tax_year else earlier.number_in_tax_year + 1)
        assert (later.tax_year != earlier.tax_year) == earlier.is_last_in_tax_year


# --------------------------------------------------------------------------- attendance cycles (3.5 §2.3)


def test_attendance_same_as_period() -> None:
    period = C.generate_periods(F.MONTHLY, rules(), D(2026, 11, 1), D(2026, 11, 30))[0]
    assert (period.attendance_start, period.attendance_end) == (period.start, period.end)
    assert period.attendance_days == 30 and period.in_attendance(D(2026, 11, 30))


def test_attendance_previous_period() -> None:
    period = C.generate_periods(F.MONTHLY, rules(attendance_cycle_mode=A.PREVIOUS_PERIOD), D(2026, 3, 1),
                                D(2026, 3, 31))[0]
    assert (period.attendance_start, period.attendance_end) == (D(2026, 2, 1), D(2026, 2, 28))


def test_attendance_custom_26th_to_25th_feeds_november() -> None:
    r = rules(attendance_cycle_mode=A.CUSTOM_START_DAY, attendance_cycle_start_day=26)
    periods = C.generate_periods(F.MONTHLY, r, D(2026, 11, 1), D(2027, 1, 31))
    assert [(p.attendance_start, p.attendance_end) for p in periods] == [
        (D(2026, 10, 26), D(2026, 11, 25)), (D(2026, 11, 26), D(2026, 12, 25)), (D(2026, 12, 26), D(2027, 1, 25))]
    assert not C.pay_date_warnings(periods)


def test_attendance_custom_start_day_defaults_to_26_like_the_registry() -> None:
    r = rules(attendance_cycle_mode=A.CUSTOM_START_DAY)
    period = C.generate_periods(F.MONTHLY, r, D(2026, 11, 1), D(2026, 11, 1))[0]
    assert period.attendance_start == D(2026, 10, 26)


def test_attendance_custom_start_day_1_is_the_whole_month() -> None:
    r = rules(monthly_start_day=15, attendance_cycle_mode=A.CUSTOM_START_DAY, attendance_cycle_start_day=1)
    periods = C.generate_periods(F.MONTHLY, r, D(2026, 11, 15), D(2027, 1, 14))
    assert [(p.attendance_start, p.attendance_end) for p in periods] == [
        (D(2026, 11, 1), D(2026, 11, 30)), (D(2026, 12, 1), D(2026, 12, 31))]


def test_attendance_custom_negative_unsupported_and_required() -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.validate_rules(F.WEEKLY, rules(attendance_cycle_mode=A.CUSTOM_START_DAY))
    assert code_of(excinfo) == "ATTENDANCE_CYCLE_UNSUPPORTED"
    with pytest.raises(CalendarError) as excinfo:
        C.validate_rules(F.MONTHLY, rules(attendance_cycle_mode=A.CUSTOM_START_DAY, attendance_cycle_start_day=None))
    assert code_of(excinfo) == "ATTENDANCE_START_DAY_REQUIRED"
    with pytest.raises(CalendarError) as excinfo:
        rules(attendance_cycle_start_day=29)
    assert code_of(excinfo) == "INVALID_ATTENDANCE_START_DAY"


@settings(max_examples=40, deadline=None)
@given(start_day=st.integers(1, 31), custom=st.integers(1, 28), begin=st.dates(D(2025, 1, 1), D(2029, 12, 31)))
def test_custom_attendance_cycles_never_skip_or_repeat_a_date(start_day: int, custom: int, begin: dt.date) -> None:
    r = rules(monthly_start_day=start_day, attendance_cycle_mode=A.CUSTOM_START_DAY, attendance_cycle_start_day=custom)
    periods = C.generate_periods(F.MONTHLY, r, begin, begin + dt.timedelta(days=400))
    C.check_attendance_contiguous(periods)
    assert all(p.attendance_end <= p.end for p in periods)


# --------------------------------------------------------------------------- EC-CAL-06 paid before attendance closes


def test_ec_cal_06_pay_date_before_period_end_warns_v14() -> None:
    r = rules(pay_day_rule=R.DAY_OF_MONTH, pay_day_value=25, pay_day_shift=S.NO_SHIFT)
    periods = C.generate_periods(F.MONTHLY, r, D(2026, 11, 1), D(2026, 12, 31))
    issues = C.pay_date_warnings(periods)
    assert [(i.code, i.severity, i.rule, i.period_start) for i in issues] == [
        ("PAY_DATE_BEFORE_ATTENDANCE_END", "WARNING", "V-14", D(2026, 11, 1)),
        ("PAY_DATE_BEFORE_ATTENDANCE_END", "WARNING", "V-14", D(2026, 12, 1))]
    assert "Consider a 26th–25th attendance cycle." in issues[0].message


def test_ec_cal_06_negative_no_warning_when_paid_at_period_end() -> None:
    at_end = rules(pay_day_rule=R.LAST_DAY, pay_day_shift=S.NO_SHIFT)
    assert C.pay_date_warnings(C.generate_periods(F.MONTHLY, at_end, D(2026, 1, 1), D(2026, 12, 31))) == ()


def test_ec_cal_06_last_working_day_before_a_weekend_month_end_warns() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(), D(2026, 1, 1), D(2026, 1, 31))  # 31 Jan 2026 is a Saturday
    assert periods[0].pay_date == D(2026, 1, 30)
    assert [i.period_start for i in C.pay_date_warnings(periods)] == [D(2026, 1, 1)]


# --------------------------------------------------------------------------- cutoffs (3.5 §2.4)


def test_cutoffs_from_pay_date_defaults() -> None:
    period = C.generate_periods(F.MONTHLY, rules(), D(2026, 11, 1), D(2026, 11, 30))[0]
    assert period.pay_date == D(2026, 11, 30)  # Monday
    assert period.input_cutoff == D(2026, 11, 25)
    assert period.approval_deadline == D(2026, 11, 28)
    assert period.payment_submit_by == D(2026, 11, 27)  # 1 business day before: Friday


def test_payment_lead_counts_business_days_only() -> None:
    banking = MON_FRI.with_holidays([D(2026, 11, 27)])
    r = rules(banking_calendar=banking, input_cutoff_days_before_pay_day=7, approval_days_before_pay_day=4,
              payment_lead_business_days=2)
    period = C.generate_periods(F.MONTHLY, r, D(2026, 11, 1), D(2026, 11, 30))[0]
    assert period.payment_submit_by == D(2026, 11, 25)  # Thu 26, then Wed 25 (Fri 27 holiday, weekend)
    assert MON_FRI.business_days_before(D(2026, 11, 29), 0) == D(2026, 11, 27)


@pytest.mark.parametrize(("inputs", "approval", "lead"), [(2, 2, 1), (5, 1, 1), (1, 2, 0), (0, 0, 0)])
def test_cutoffs_negative_out_of_order_is_v07(inputs: int, approval: int, lead: int) -> None:
    with pytest.raises(CalendarError) as excinfo:
        rules(input_cutoff_days_before_pay_day=inputs, approval_days_before_pay_day=approval,
              payment_lead_business_days=lead)
    assert code_of(excinfo) == "CUTOFF_ORDER"


def test_cutoffs_negative_ranges() -> None:
    with pytest.raises(CalendarError) as excinfo:
        rules(payment_lead_business_days=11)
    assert code_of(excinfo) == "INVALID_CUTOFF"
    with pytest.raises(CalendarError) as excinfo:
        rules(input_cutoff_days_before_pay_day=32)
    assert code_of(excinfo) == "INVALID_CUTOFF"


# --------------------------------------------------------------------------- EC-CAL-13 late inputs


@pytest.fixture()
def monthly_periods() -> list[C.Period]:
    return C.generate_periods(F.MONTHLY, rules(), D(2026, 10, 1), D(2026, 12, 31))


def test_ec_cal_13_on_time_input_stays_in_its_period(monthly_periods: list[C.Period]) -> None:
    october = monthly_periods[0]
    routed = C.route_input(monthly_periods, october, october.input_cutoff, late_policy="NEXT_PERIOD")
    assert routed == C.InputRouting(october, late=False)


def test_ec_cal_13_late_input_goes_to_next_period(monthly_periods: list[C.Period]) -> None:
    october, november = monthly_periods[0], monthly_periods[1]
    routed = C.route_input(monthly_periods, october, october.input_cutoff + dt.timedelta(days=1),
                           late_policy=C.LateInputPolicy.NEXT_PERIOD)
    assert routed.period is november and routed.late and routed.exception is None


def test_ec_cal_13_late_input_becomes_exception_per_setting(monthly_periods: list[C.Period]) -> None:
    october = monthly_periods[0]
    routed = C.route_input(monthly_periods, october, D(2026, 10, 28), late_policy="EXCEPTION")
    assert (routed.period, routed.late, routed.exception) == (october, True, "LATE_INPUT")


def test_ec_cal_13_negative_no_open_period_left(monthly_periods: list[C.Period]) -> None:
    october = monthly_periods[0]
    routed = C.route_input(monthly_periods, october, D(2027, 1, 30), late_policy="NEXT_PERIOD")
    assert routed.exception == "LATE_INPUT_NO_OPEN_PERIOD" and routed.period is october
    with pytest.raises(CalendarError) as excinfo:
        C.route_input(monthly_periods, october, D(2026, 10, 1), late_policy="DROP")
    assert code_of(excinfo) == "INVALID_LATE_INPUT_POLICY"


# --------------------------------------------------------------------------- tax years


@pytest.mark.parametrize(("day", "start", "label"), [
    (D(2026, 10, 20), "04-01", "2026-27"),
    (D(2026, 3, 31), "04-01", "2025-26"),
    (D(2026, 4, 1), "04-01", "2026-27"),
    (D(2026, 4, 5), "04-06", "2025-26"),
    (D(2026, 4, 6), "04-06", "2026-27"),
    (D(2026, 12, 31), "01-01", "2026"),
    (D(1999, 7, 1), "07-01", "1999-00"),
])
def test_tax_year_label(day: dt.date, start: str, label: str) -> None:
    assert C.tax_year_label(day, start) == label
    begin, end = C.tax_year_from_label(label, start)
    assert begin <= day <= end and C.tax_year_bounds(day, start) == (begin, end)


@pytest.mark.parametrize("start", ["4-01", "13-01", "04-31", "0401", 401, "04-1x"])
def test_tax_year_start_negative(start: object) -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.parse_tax_year_start(start)  # type: ignore[arg-type]
    assert code_of(excinfo) == "INVALID_TAX_YEAR_START"


@pytest.mark.parametrize("label", ["2026", "2026-28", "26-27", "", "abcd-ef"])
def test_tax_year_from_label_negative(label: str) -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.tax_year_from_label(label, INDIA)
    assert code_of(excinfo) == "INVALID_TAX_YEAR"


def test_monthly_numbering_in_an_indian_tax_year() -> None:
    periods = C.periods_of_tax_year(F.MONTHLY, rules(), "2026-27")
    assert [p.start for p in periods][0] == D(2026, 4, 1) and len(periods) == 12
    assert [p.number_in_tax_year for p in periods] == list(range(1, 13))
    assert [p.is_last_in_tax_year for p in periods] == [False] * 11 + [True]
    assert C.periods_of_tax_year(F.MONTHLY, rules(), D(2026, 10, 20)) == periods


# --------------------------------------------------------------------------- EC-CAL-04 52/53 and 26/27 periods


SUNDAY_PAY = {"pay_day_rule": R.LAST_DAY, "pay_day_shift": S.NO_SHIFT, "tax_year_start": "01-01"}


def _weekday_count(year: int, weekday: int) -> int:
    day, count = D(year, 1, 1), 0
    while day.year == year:
        count += day.weekday() == weekday
        day += dt.timedelta(days=1)
    return count


def test_ec_cal_04_weekly_tax_year_with_53_pay_dates() -> None:
    weekly = rules(**SUNDAY_PAY)
    assert C.periods_in_tax_year(F.WEEKLY, weekly, "2023") == 53 == _weekday_count(2023, 6)
    assert C.periods_in_tax_year(F.WEEKLY, weekly, "2026") == 52 == _weekday_count(2026, 6)
    periods = C.periods_of_tax_year(F.WEEKLY, weekly, "2023")
    assert periods[0].pay_date == D(2023, 1, 1) and periods[-1].pay_date == D(2023, 12, 31)
    assert periods[-1].number_in_tax_year == 53 and periods[-1].is_last_in_tax_year


def test_ec_cal_04_bi_weekly_tax_year_with_27_pay_dates() -> None:
    biweekly = rules(**{**SUNDAY_PAY, "week_start_day": Weekday.FRI, "biweekly_anchor_date": D(2025, 12, 19)})
    periods = C.periods_of_tax_year(F.BI_WEEKLY, biweekly, "2026")
    assert len(periods) == 27
    assert (periods[0].pay_date, periods[-1].pay_date) == (D(2026, 1, 1), D(2026, 12, 31))
    assert C.periods_in_tax_year(F.BI_WEEKLY, biweekly, "2027") == 26


def test_ec_cal_04_holiday_shift_across_the_boundary_adds_a_period() -> None:
    weekly = rules(**{**SUNDAY_PAY, "pay_day_shift": S.NEXT_BUSINESS_DAY})
    # 2026 has 52 Sundays; the Sunday 28 Dec 2025 pay date shifts to Monday 29 Dec and stays in 2025.
    assert C.periods_in_tax_year(F.WEEKLY, weekly, "2026") == 52
    late = rules(**{**SUNDAY_PAY, "pay_day_shift": S.NEXT_BUSINESS_DAY,
                    "banking_calendar": MON_FRI.with_holidays([D(2025, 12, 29), D(2025, 12, 30), D(2025, 12, 31)])})
    assert C.periods_in_tax_year(F.WEEKLY, late, "2026") == 53


def test_ec_cal_04_annual_amount_divided_by_actual_count_golden() -> None:
    with localcontext() as ctx:
        ctx.prec = 28
        expected = Decimal("520000") / Decimal(53)
    assert C.annual_to_period(Decimal("520000"), F.WEEKLY, 53) == expected
    assert str(expected) == "9811.320754716981132075471698"
    assert C.annual_to_period("520000", F.WEEKLY, 52) == Decimal("10000")
    assert C.annual_to_period(Decimal("520000"), F.BI_WEEKLY, 27) == Decimal("520000") / Decimal(27)


def test_ec_cal_04_fixed_divisor_from_the_pack_wins() -> None:
    assert C.annual_to_period(Decimal("520000"), F.WEEKLY, 53, fixed_divisor=52) == Decimal("10000")
    assert C.period_divisor(F.BI_WEEKLY, 27, fixed_divisor=26) == 26


def test_ec_cal_04_monthly_and_semi_monthly_divide_by_12_and_24() -> None:
    assert C.annual_to_period(Decimal("1200000"), F.MONTHLY) == Decimal("100000")
    assert C.annual_to_period(Decimal("1200000"), F.SEMI_MONTHLY, 24) == Decimal("50000")
    assert C.annual_to_period(1000, F.MONTHLY) == Decimal("1000") / Decimal(12)


def test_ec_cal_04_negative_divisor_inputs() -> None:
    for call, code in ((lambda: C.annual_to_period(Decimal(1), F.WEEKLY), "PERIOD_COUNT_REQUIRED"),
                       (lambda: C.annual_to_period(Decimal(1), F.MONTHLY, 13), "INVALID_PERIOD_COUNT"),
                       (lambda: C.annual_to_period(Decimal(1), F.WEEKLY, 0), "INVALID_PERIOD_COUNT"),
                       (lambda: C.annual_to_period(1.5, F.MONTHLY), "INVALID_AMOUNT"),  # type: ignore[arg-type]
                       (lambda: C.annual_to_period(True, F.MONTHLY), "INVALID_AMOUNT"),
                       (lambda: C.annual_to_period("NaN", F.MONTHLY), "INVALID_AMOUNT"),
                       (lambda: C.annual_to_period("1,000", F.MONTHLY), "INVALID_AMOUNT"),
                       (lambda: C.annual_to_period(Decimal(1), "YEARLY"), "INVALID_FREQUENCY")):
        with pytest.raises(CalendarError) as excinfo:
            call()
        assert code_of(excinfo) == code


# --------------------------------------------------------------------------- EC-CAL-05 tax year by pay date


def test_ec_cal_05_pay_date_decides_the_tax_year() -> None:
    r = rules(pay_day_rule=R.DAYS_AFTER_PERIOD_END, pay_day_value=5, pay_day_shift=S.NO_SHIFT)
    march = C.generate_periods(F.MONTHLY, r, D(2027, 3, 1), D(2027, 3, 31))[0]
    assert (march.pay_date, march.tax_year, march.number_in_tax_year) == (D(2027, 4, 5), "2027-28", 1)
    february = C.generate_periods(F.MONTHLY, r, D(2027, 2, 1), D(2027, 2, 28))[0]
    assert february.tax_year == "2026-27" and february.is_last_in_tax_year


def test_ec_cal_05_attendance_crossing_the_boundary_follows_the_pay_date() -> None:
    r = rules(attendance_cycle_mode=A.CUSTOM_START_DAY, attendance_cycle_start_day=26)
    april = C.generate_periods(F.MONTHLY, r, D(2027, 4, 1), D(2027, 4, 30))[0]
    assert april.attendance_start == D(2027, 3, 26) and april.tax_year == "2027-28"
    assert april.number_in_tax_year == 1


def test_ec_cal_05_negative_pack_rule_period_end_keeps_the_old_year() -> None:
    r = rules(pay_day_rule=R.DAYS_AFTER_PERIOD_END, pay_day_value=5, pay_day_shift=S.NO_SHIFT,
              tax_year_basis="PERIOD_END")
    march = C.generate_periods(F.MONTHLY, r, D(2027, 3, 1), D(2027, 3, 31))[0]
    assert (march.tax_year, march.number_in_tax_year, march.is_last_in_tax_year) == ("2026-27", 12, True)


# --------------------------------------------------------------------------- EC-CAL-08 re-validation at batch creation


def test_ec_cal_08_new_bank_holiday_moves_the_value_date() -> None:
    period = C.generate_periods(F.MONTHLY, rules(), D(2026, 11, 1), D(2026, 11, 30))[0]
    check = C.recheck_pay_date(period, rules(banking_calendar=MON_FRI.with_holidays([D(2026, 11, 30)])))
    assert (check.scheduled, check.current, check.changed) == (D(2026, 11, 30), D(2026, 11, 27), True)
    assert check.payment_submit_by == D(2026, 11, 26)
    assert [i.code for i in check.issues] == ["PAY_DATE_SHIFTED"]


def test_ec_cal_08_next_shift_warns_value_date_after_pay_date() -> None:
    r = rules(pay_day_rule=R.DAY_OF_MONTH, pay_day_value=16, pay_day_shift=S.NEXT_BUSINESS_DAY)
    period = C.generate_periods(F.MONTHLY, r, D(2026, 11, 1), D(2026, 11, 30))[0]
    assert period.pay_date == D(2026, 11, 16)
    later = rules(pay_day_rule=R.DAY_OF_MONTH, pay_day_value=16, pay_day_shift=S.NEXT_BUSINESS_DAY,
                  banking_calendar=MON_FRI.with_holidays([D(2026, 11, 16)]))
    check = C.recheck_pay_date(period, later)
    assert check.current == D(2026, 11, 17)
    assert [i.code for i in check.issues] == ["PAY_DATE_SHIFTED", "VALUE_DATE_AFTER_PAY_DATE"]


def test_ec_cal_08_no_shift_onto_a_holiday_is_reported() -> None:
    r = rules(pay_day_rule=R.LAST_DAY, pay_day_shift=S.NO_SHIFT)
    period = C.generate_periods(F.MONTHLY, r, D(2026, 11, 1), D(2026, 11, 30))[0]
    check = C.recheck_pay_date(period, rules(pay_day_rule=R.LAST_DAY, pay_day_shift=S.NO_SHIFT,
                                             banking_calendar=MON_FRI.with_holidays([D(2026, 11, 30)])))
    assert not check.changed and [i.code for i in check.issues] == ["PAY_DATE_NOT_BUSINESS_DAY"]


def test_ec_cal_08_negative_unchanged_calendar_reports_nothing() -> None:
    period = C.generate_periods(F.MONTHLY, rules(), D(2026, 11, 1), D(2026, 11, 30))[0]
    check = C.recheck_pay_date(period, rules())
    assert not check.changed and check.issues == () and check.payment_submit_by == period.payment_submit_by


# --------------------------------------------------------------------------- EC-CAL-09 / EC-CAL-10 local dates


class _NewYorkLike(dt.tzinfo):
    """UTC-5, or UTC-4 from 2026-03-08 07:00 UTC to 2026-11-01 06:00 UTC (US rules for 2026)."""

    _START = dt.datetime(2026, 3, 8, 7)
    _END = dt.datetime(2026, 11, 1, 6)

    def utcoffset(self, value: dt.datetime | None) -> dt.timedelta:
        return dt.timedelta(hours=-5) + self.dst(value)

    def dst(self, value: dt.datetime | None) -> dt.timedelta:
        if value is None:
            return dt.timedelta(0)
        naive = value.replace(tzinfo=None) + dt.timedelta(hours=5)
        return dt.timedelta(hours=1) if self._START <= naive < self._END else dt.timedelta(0)

    def tzname(self, value: dt.datetime | None) -> str:
        return "EDT" if self.dst(value) else "EST"

    def fromutc(self, value: dt.datetime) -> dt.datetime:
        naive = value.replace(tzinfo=None)
        offset = dt.timedelta(hours=-4 if self._START <= naive < self._END else -5)
        return (naive + offset).replace(tzinfo=self)


NEW_YORK = _NewYorkLike()
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc


def test_ec_cal_09_dst_change_on_a_period_boundary_keeps_local_dates() -> None:
    periods = C.generate_periods(F.WEEKLY, rules(week_start_day=Weekday.SUN), D(2026, 10, 25), D(2026, 11, 7))
    before = dt.datetime(2026, 11, 1, 3, 59, tzinfo=UTC)  # 31 Oct 23:59 EDT
    after = dt.datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 1 Nov 01:30 EST, after the fall-back
    assert C.local_date(before, NEW_YORK) == D(2026, 10, 31)
    assert C.local_date(after, NEW_YORK) == D(2026, 11, 1)
    assert C.period_for_instant(periods, before, NEW_YORK) is periods[0]
    assert C.period_for_instant(periods, after, NEW_YORK) is periods[1]
    assert periods[0].days == periods[1].days == 7


def test_ec_cal_09_negative_naive_datetimes_and_datetimes_as_dates_are_refused() -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.local_date(dt.datetime(2026, 11, 1, 1, 30), NEW_YORK)
    assert code_of(excinfo) == "NAIVE_DATETIME"
    with pytest.raises(CalendarError) as excinfo:
        C.generate_periods(F.MONTHLY, rules(), dt.datetime(2026, 11, 1, tzinfo=UTC), D(2026, 11, 30))
    assert code_of(excinfo) == "NOT_A_LOCAL_DATE"
    with pytest.raises(CalendarError) as excinfo:
        C.local_date(D(2026, 11, 1), UTC)  # type: ignore[arg-type]
    assert code_of(excinfo) == "NAIVE_DATETIME"


def test_ec_cal_10_entity_timezone_decides_the_period_not_the_server() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(), D(2026, 10, 1), D(2026, 11, 30))
    instant = dt.datetime(2026, 10, 31, 20, 0, tzinfo=UTC)  # 1 Nov 01:30 in India
    assert C.period_for_instant(periods, instant, IST) is periods[1]
    assert C.period_for_instant(periods, instant, UTC) is periods[0]


def test_ec_cal_10_negative_date_outside_the_periods() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(), D(2026, 10, 1), D(2026, 11, 30))
    assert C.period_for_date(periods, D(2026, 12, 1)) is None
    assert C.period_for_date(periods, D(2026, 9, 30)) is None
    assert C.period_for_date([], D(2026, 9, 30)) is None


# --------------------------------------------------------------------------- EC-CAL-12 no gap, no overlap


def test_ec_cal_12_generated_periods_pass_the_contiguity_guard() -> None:
    periods = C.generate_periods(F.SEMI_MONTHLY, rules(), D(2026, 1, 1), D(2027, 12, 31))
    C.check_contiguous(periods)
    C.check_attendance_contiguous(periods)


def test_ec_cal_12_negative_gap_and_overlap_are_refused() -> None:
    periods = C.generate_periods(F.MONTHLY, rules(), D(2026, 1, 1), D(2026, 4, 30))
    with pytest.raises(CalendarError, match="gap") as excinfo:
        C.check_contiguous([periods[0], periods[2]])
    assert code_of(excinfo) == "PERIODS_NOT_CONTIGUOUS"
    overlapping = C.Period(**{**_fields(periods[1]), "start": D(2026, 1, 31)})
    with pytest.raises(CalendarError, match="overlap"):
        C.check_contiguous([periods[0], overlapping])
    shifted = C.Period(**{**_fields(periods[1]), "attendance_start": D(2026, 2, 3)})
    with pytest.raises(CalendarError) as excinfo:
        C.check_attendance_contiguous([periods[0], shifted])
    assert code_of(excinfo) == "ATTENDANCE_NOT_CONTIGUOUS"


def _fields(period: C.Period) -> dict[str, object]:
    return {name: getattr(period, name) for name in C.Period.__dataclass_fields__}


def test_period_validation() -> None:
    period = C.generate_periods(F.MONTHLY, rules(), D(2026, 1, 1), D(2026, 1, 31))[0]
    for change, code in (({"end": D(2025, 12, 31)}, "INVALID_PERIOD"),
                         ({"attendance_end": D(2026, 2, 1)}, "INVALID_PERIOD"),
                         ({"number_in_tax_year": 0}, "INVALID_PERIOD"),
                         ({"pay_date": dt.datetime(2026, 1, 30)}, "NOT_A_LOCAL_DATE"),
                         ({"frequency": "YEARLY"}, "INVALID_FREQUENCY")):
        with pytest.raises(CalendarError) as excinfo:
            C.Period(**{**_fields(period), **change})
        assert code_of(excinfo) == code


def test_period_to_dict_is_json_ready() -> None:
    period = C.generate_periods(F.MONTHLY, rules(), D(2026, 11, 1), D(2026, 11, 30))[0]
    assert period.to_dict() == {
        "start": "2026-11-01", "end": "2026-11-30", "days": 30, "attendance_start": "2026-11-01",
        "attendance_end": "2026-11-30", "pay_date": "2026-11-30", "nominal_pay_date": "2026-11-30",
        "input_cutoff": "2026-11-25", "approval_deadline": "2026-11-28", "payment_submit_by": "2026-11-27",
        "tax_year": "2026-27", "number_in_tax_year": 8, "is_last_in_tax_year": False, "frequency": "MONTHLY",
        "is_transition": False,
    }


# --------------------------------------------------------------------------- ranges and settings


def test_range_negative() -> None:
    with pytest.raises(CalendarError) as excinfo:
        C.generate_periods(F.MONTHLY, rules(), D(2026, 2, 1), D(2026, 1, 1))
    assert code_of(excinfo) == "INVALID_RANGE"
    with pytest.raises(CalendarError) as excinfo:
        C.generate_periods(F.MONTHLY, rules(), D(2026, 1, 1), D(2026, 1, 1) + dt.timedelta(days=C.MAX_RANGE_DAYS + 1))
    assert code_of(excinfo) == "RANGE_TOO_LARGE"
    with pytest.raises(CalendarError) as excinfo:
        C.generate_periods(F.MONTHLY, rules(), D(1800, 1, 1), D(1800, 2, 1))
    assert code_of(excinfo) == "INVALID_RANGE"


def test_eighteen_months_ahead_fits_one_request() -> None:
    periods = C.generate_periods(F.WEEKLY, rules(), D(2026, 10, 20), D(2028, 4, 20))
    assert len(periods) == 79
    C.check_contiguous(periods)


def test_from_settings_maps_registry_keys() -> None:
    values = {
        "payroll.pay_frequency": "BI_WEEKLY", "payroll.calendar.week_start_day": "MON",
        "payroll.calendar.biweekly_anchor_date": "2026-07-13", "payroll.calendar.pay_day_rule": "DAYS_AFTER_PERIOD_END",
        "payroll.calendar.pay_day_value": 3, "payroll.calendar.pay_day_shift": "NEXT_BUSINESS_DAY",
        "payroll.attendance_cycle.mode": "PREVIOUS_PERIOD", "payroll.cutoff.input_days_before_pay_day": 6,
        "payroll.cutoff.approval_days_before_pay_day": 3, "payroll.cutoff.payment_lead_business_days": 2,
    }
    calendar = PayCalendar.from_settings(values, banking_calendar=MON_FRI, tax_year_start=INDIA)
    assert calendar.frequency is F.BI_WEEKLY
    assert calendar.rules.biweekly_anchor_date == D(2026, 7, 13)
    period = calendar.period_containing(D(2026, 7, 20))
    assert (period.start, period.end, period.pay_date) == (D(2026, 7, 13), D(2026, 7, 26), D(2026, 7, 29))
    assert (period.attendance_start, period.attendance_end) == (D(2026, 6, 29), D(2026, 7, 12))
    assert set(C.SETTING_FIELDS.values()) <= set(CalendarRules.__dataclass_fields__)


def test_from_settings_absent_keys_take_platform_defaults() -> None:
    calendar = PayCalendar.from_settings({}, banking_calendar=MON_FRI, tax_year_start=INDIA)
    r = calendar.rules
    assert calendar.frequency is F.MONTHLY
    assert (r.monthly_start_day, r.semi_monthly_split_day, r.week_start_day, r.pay_day_rule, r.pay_day_shift,
            r.attendance_cycle_mode, r.attendance_cycle_start_day, r.input_cutoff_days_before_pay_day,
            r.approval_days_before_pay_day, r.payment_lead_business_days) == (
        1, 15, Weekday.MON, R.LAST_WORKING_DAY, S.PREVIOUS_BUSINESS_DAY, A.SAME_AS_PERIOD, 26, 5, 2, 1)


def test_from_settings_negative_explicit_none_is_not_defaulted() -> None:
    values = {"payroll.attendance_cycle.mode": "CUSTOM_START_DAY", "payroll.attendance_cycle.start_day": None}
    with pytest.raises(CalendarError) as excinfo:
        PayCalendar.from_settings(values, banking_calendar=MON_FRI, tax_year_start=INDIA)
    assert code_of(excinfo) == "ATTENDANCE_START_DAY_REQUIRED"
    with pytest.raises(CalendarError) as excinfo:
        CalendarRules.from_settings({"payroll.calendar.monthly_start_day": None}, banking_calendar=MON_FRI,
                                    tax_year_start=INDIA)
    assert code_of(excinfo) == "INVALID_MONTHLY_START_DAY"
    with pytest.raises(CalendarError) as excinfo:
        CalendarRules.from_settings({"payroll.calendar.biweekly_anchor_date": "13/07/2026"},
                                    banking_calendar=MON_FRI, tax_year_start=INDIA)
    assert code_of(excinfo) == "NOT_A_LOCAL_DATE"


def test_rules_require_a_banking_calendar_and_tax_year_start() -> None:
    with pytest.raises(CalendarError) as excinfo:
        CalendarRules(banking_calendar=frozenset({5, 6}), tax_year_start=INDIA)  # type: ignore[arg-type]
    assert code_of(excinfo) == "INVALID_BANKING_CALENDAR"
    with pytest.raises(CalendarError) as excinfo:
        CalendarRules(banking_calendar=MON_FRI, tax_year_start="April")
    assert code_of(excinfo) == "INVALID_TAX_YEAR_START"
    with pytest.raises(CalendarError) as excinfo:
        C.validate_rules(F.MONTHLY, object())  # type: ignore[arg-type]
    assert code_of(excinfo) == "INVALID_RULES"


def test_error_codes_are_declared() -> None:
    assert {"CHANGE_LEAVES_GAP", "CUTOFF_ORDER", "NOT_A_LOCAL_DATE"} <= C.ERROR_CODES
    assert issubclass(CalendarError, ValueError)


# --------------------------------------------------------------------------- EC-CAL-07 frequency change


def _monthly() -> PayCalendar:
    return PayCalendar(F.MONTHLY, rules())


def _biweekly(**overrides: object) -> PayCalendar:
    return PayCalendar(F.BI_WEEKLY, rules(biweekly_anchor_date=D(2026, 7, 13), **overrides))


def test_ec_cal_07_monthly_to_bi_weekly_with_closing_and_opening_transitions() -> None:
    old = _monthly()
    stored = old.periods(D(2026, 4, 1), D(2026, 12, 31))
    change = C.plan_calendar_change(old, stored, _biweekly(), D(2026, 7, 15), D(2026, 9, 30))
    assert [p.start.month for p in change.kept] == [4, 5, 6]
    assert [p.start.month for p in change.dropped] == [7, 8, 9, 10, 11, 12]
    closing, opening = change.closing_transition, change.opening_transition
    assert closing is not None and opening is not None
    assert (closing.start, closing.end, closing.pay_date, closing.frequency) == (
        D(2026, 7, 1), D(2026, 7, 14), D(2026, 7, 14), F.MONTHLY)
    assert (opening.start, opening.end, opening.pay_date, opening.frequency) == (
        D(2026, 7, 15), D(2026, 7, 26), D(2026, 7, 24), F.BI_WEEKLY)
    assert closing.is_transition and opening.is_transition
    assert not any(p.is_transition for p in change.kept + change.new_periods)
    assert spans(list(change.new_periods)) == [
        (D(2026, 7, 27), D(2026, 8, 9)), (D(2026, 8, 10), D(2026, 8, 23)), (D(2026, 8, 24), D(2026, 9, 6)),
        (D(2026, 9, 7), D(2026, 9, 20)), (D(2026, 9, 21), D(2026, 10, 4))]
    all_periods = list(change.periods)
    C.check_contiguous(all_periods)
    C.check_attendance_contiguous(all_periods)
    assert [p.number_in_tax_year for p in all_periods] == list(range(1, len(all_periods) + 1))
    assert all(p.attendance_start == p.start and p.attendance_end == p.end for p in all_periods)


def test_ec_cal_07_change_on_a_natural_boundary_needs_no_transition() -> None:
    old = _monthly()
    stored = old.periods(D(2026, 4, 1), D(2026, 12, 31))
    weekly = PayCalendar(F.WEEKLY, rules())
    change = C.plan_calendar_change(old, stored, weekly, D(2026, 6, 1), D(2026, 6, 30))  # 1 June 2026 is a Monday
    assert change.closing_transition is None and change.opening_transition is None
    assert change.kept[-1].end == D(2026, 5, 31) and change.new_periods[0].start == D(2026, 6, 1)
    C.check_contiguous(list(change.periods))


def test_ec_cal_07_attendance_is_chained_across_a_mode_change() -> None:
    old = PayCalendar(F.MONTHLY, rules(attendance_cycle_mode=A.CUSTOM_START_DAY, attendance_cycle_start_day=26))
    stored = old.periods(D(2026, 4, 1), D(2026, 12, 31))
    weekly = PayCalendar(F.WEEKLY, rules())
    change = C.plan_calendar_change(old, stored, weekly, D(2026, 7, 6), D(2026, 7, 31))
    june, closing, first = change.kept[-1], change.closing_transition, change.new_periods[0]
    assert closing is not None
    assert (june.attendance_start, june.attendance_end) == (D(2026, 5, 26), D(2026, 6, 25))
    assert (closing.start, closing.end) == (D(2026, 7, 1), D(2026, 7, 5))
    assert closing.attendance_days == 0  # 26 Jun-25 Jun was already fed to June: nothing counted twice
    assert (first.attendance_start, first.attendance_end) == (D(2026, 6, 26), D(2026, 7, 12))
    C.check_attendance_contiguous(list(change.periods))


def test_ec_cal_07_numbering_and_last_flag_across_a_tax_year_boundary() -> None:
    old = _monthly()
    stored = old.periods(D(2026, 4, 1), D(2027, 3, 31))
    weekly = PayCalendar(F.WEEKLY, rules())
    change = C.plan_calendar_change(old, stored, weekly, D(2027, 4, 1), D(2027, 4, 30))
    assert change.kept[-1].is_last_in_tax_year and change.kept[-1].number_in_tax_year == 12
    opening = change.opening_transition
    assert opening is not None and (opening.start, opening.end) == (D(2027, 4, 1), D(2027, 4, 4))
    assert (opening.tax_year, opening.number_in_tax_year) == ("2027-28", 1)


def test_ec_cal_07_change_from_the_first_stored_day_replaces_everything() -> None:
    old = _monthly()
    stored = old.periods(D(2026, 4, 1), D(2026, 6, 30))
    change = C.plan_calendar_change(old, stored, PayCalendar(F.SEMI_MONTHLY, rules()), D(2026, 4, 1), D(2026, 4, 30))
    assert change.kept == () and len(change.dropped) == 3 and change.closing_transition is None
    assert change.new_periods[0].number_in_tax_year == 1
    C.check_contiguous(list(change.periods))


def test_ec_cal_07_change_right_after_the_last_stored_period() -> None:
    old = _monthly()
    stored = old.periods(D(2026, 4, 1), D(2026, 6, 30))
    check = C.validate_calendar_change(stored, D(2026, 7, 1))
    assert len(check.kept) == 3 and check.truncated is None and check.dropped == ()


def test_ec_cal_07_negative_change_that_would_leave_a_gap() -> None:
    stored = _monthly().periods(D(2026, 4, 1), D(2026, 6, 30))
    with pytest.raises(CalendarError) as excinfo:
        C.validate_calendar_change(stored, D(2026, 7, 2))
    assert code_of(excinfo) == "CHANGE_LEAVES_GAP"
    with pytest.raises(CalendarError) as excinfo:
        C.validate_calendar_change(stored, D(2026, 3, 31))
    assert code_of(excinfo) == "CHANGE_BEFORE_CALENDAR_START"
    with pytest.raises(CalendarError) as excinfo:
        C.validate_calendar_change([], D(2026, 3, 31))
    assert code_of(excinfo) == "NO_PERIODS"


def test_ec_cal_07_negative_stored_periods_with_a_gap_are_refused() -> None:
    stored = _monthly().periods(D(2026, 4, 1), D(2026, 6, 30))
    with pytest.raises(CalendarError) as excinfo:
        C.validate_calendar_change([stored[0], stored[2]], D(2026, 6, 15))
    assert code_of(excinfo) == "PERIODS_NOT_CONTIGUOUS"


def test_ec_cal_07_negative_locked_periods_cannot_change() -> None:
    stored = _monthly().periods(D(2026, 4, 1), D(2026, 12, 31))
    with pytest.raises(CalendarError) as excinfo:
        C.validate_calendar_change(stored, D(2026, 7, 15), locked_until=D(2026, 7, 31))
    assert code_of(excinfo) == "CHANGE_IN_LOCKED_PERIOD"
    with pytest.raises(CalendarError):
        C.validate_calendar_change(stored, D(2026, 7, 15), locked_until=D(2026, 7, 1))
    assert C.validate_calendar_change(stored, D(2026, 7, 15), locked_until=D(2026, 6, 30)).truncated is not None


def test_ec_cal_07_negative_pay_dates_may_not_go_backwards() -> None:
    old = PayCalendar(F.MONTHLY, rules(pay_day_rule=R.DAYS_AFTER_PERIOD_END, pay_day_value=30,
                                       pay_day_shift=S.NO_SHIFT))
    stored = old.periods(D(2026, 4, 1), D(2026, 12, 31))
    weekly = PayCalendar(F.WEEKLY, rules(pay_day_rule=R.LAST_DAY, pay_day_shift=S.NO_SHIFT))
    with pytest.raises(CalendarError) as excinfo:
        C.plan_calendar_change(old, stored, weekly, D(2026, 7, 6), D(2026, 8, 31))
    assert code_of(excinfo) == "PAY_DATES_OUT_OF_ORDER"


def test_ec_cal_07_negative_calendars_must_be_pay_calendars() -> None:
    stored = _monthly().periods(D(2026, 4, 1), D(2026, 6, 30))
    with pytest.raises(CalendarError) as excinfo:
        C.plan_calendar_change(_monthly(), stored, F.WEEKLY, D(2026, 6, 1), D(2026, 6, 30))  # type: ignore[arg-type]
    assert code_of(excinfo) == "INVALID_RULES"


_CHANGE_FREQUENCIES = st.sampled_from([F.MONTHLY, F.SEMI_MONTHLY, F.WEEKLY, F.BI_WEEKLY, F.DAILY])
_MODES = st.sampled_from([A.SAME_AS_PERIOD, A.PREVIOUS_PERIOD, A.CUSTOM_START_DAY])


def _calendar(frequency: F, mode: A, start_day: int, custom: int) -> PayCalendar:
    if frequency is not F.MONTHLY and mode is A.CUSTOM_START_DAY:
        mode = A.SAME_AS_PERIOD
    return PayCalendar(frequency, rules(monthly_start_day=start_day, attendance_cycle_mode=mode,
                                        attendance_cycle_start_day=custom, biweekly_anchor_date=D(2026, 1, 5),
                                        pay_day_rule=R.LAST_DAY, pay_day_shift=S.NO_SHIFT))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(old_frequency=_CHANGE_FREQUENCIES, new_frequency=_CHANGE_FREQUENCIES, old_mode=_MODES, new_mode=_MODES,
       start_day=st.integers(1, 31), custom=st.integers(1, 28), offset=st.integers(0, 100),
       horizon=st.integers(0, 60))
def test_ec_cal_07_property_no_gap_no_overlap_no_double_attendance(
        old_frequency: F, new_frequency: F, old_mode: A, new_mode: A, start_day: int, custom: int, offset: int,
        horizon: int) -> None:
    old = _calendar(old_frequency, old_mode, start_day, custom)
    new = _calendar(new_frequency, new_mode, start_day, custom)
    stored = old.periods(D(2026, 3, 1), D(2026, 7, 31))
    new_start = stored[0].start + dt.timedelta(days=offset)
    change = C.plan_calendar_change(old, stored, new, new_start, new_start + dt.timedelta(days=horizon))
    periods = list(change.periods)
    C.check_contiguous(periods)
    C.check_attendance_contiguous(periods)
    assert periods[0].start == stored[0].start and periods[-1].end >= new_start + dt.timedelta(days=horizon)
    assert periods[0].attendance_start == stored[0].attendance_start
    kept = len(change.kept)
    assert list(change.dropped) == stored[kept:]
    assert list(change.kept[:-1]) == stored[:kept - 1] if kept else True
    if kept:
        _same_dates(stored[kept - 1], change.kept[-1])
    assert all(p.end < new_start for p in change.kept) and all(p.start >= new_start for p in change.new_periods)
    for earlier, later in zip(periods, periods[1:]):
        assert later.pay_date >= earlier.pay_date
        assert later.number_in_tax_year == (1 if earlier.is_last_in_tax_year else earlier.number_in_tax_year + 1)


def _same_dates(stored: C.Period, kept: C.Period) -> None:
    assert {**_fields(stored), "is_last_in_tax_year": None} == {**_fields(kept), "is_last_in_tax_year": None}


def test_ec_cal_08_last_working_day_without_shift_is_recomputed() -> None:
    r = rules(pay_day_shift=S.NO_SHIFT)
    period = C.generate_periods(F.MONTHLY, r, D(2026, 11, 1), D(2026, 11, 30))[0]
    assert period.pay_date == D(2026, 11, 30)
    check = C.recheck_pay_date(period, rules(pay_day_shift=S.NO_SHIFT,
                                             banking_calendar=MON_FRI.with_holidays([D(2026, 11, 30)])))
    assert (check.current, check.changed, [i.code for i in check.issues]) == (
        D(2026, 11, 27), True, ["PAY_DATE_SHIFTED"])


def test_weekday_numbers_do_not_shadow_string_methods() -> None:
    assert [day.number for day in Weekday] == list(range(7))
    assert Weekday.MON.index("O") == 1 and Weekday.SUN == "SUN"
