"""Holiday and weekly-off pay, stacking with overtime, comp-off and non-salaried holiday pay (3.4 §8, §9.5; 3.1 §6.2)."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app.payroll.time_pay import (
    HOLIDAY,
    WEEKLY_OFF,
    WORKDAY,
    HolidayDay,
    NonWorkingDayWork,
    TimePayError,
    holiday_on_weekly_off,
    holiday_premiums,
    non_salaried_holiday_pay,
    overtime_hours,
    overtime_pay,
)

HOL = date(2026, 10, 2)
SUN = date(2026, 10, 4)
RATE = D("125.00")
# Multipliers are never assumed (contract K:157, decision I-13); these are the 3.1 §5.4 example values.
MULTIPLIERS = {"holiday_work_multiplier": "1.0", "weekly_off_work_multiplier": "0.5"}
OT = {"buckets": [{"day_type": WORKDAY, "after_hours": "0", "multiplier": "1.5"},
                  {"day_type": WEEKLY_OFF, "after_hours": "0", "multiplier": "2.0"},
                  {"day_type": HOLIDAY, "after_hours": "0", "multiplier": "2.0"}]}


def premiums(days, policy=None, *, holiday_on=None, weekly_on=None, ot_on=None, rate=RATE):
    return holiday_premiums(days, {**MULTIPLIERS, **(policy or {})}, hourly_rate=rate, standard_hours=D("8"), currency="INR",
                            holiday_pay_enabled=holiday_on, weekly_off_pay_enabled=weekly_on, overtime_enabled=ot_on)


def amounts(result):
    return {line.component: line.amount for line in result.lines}


# --------------------------------------------------------------------------- golden 3.4 §17.2 and stacking (EC-HOL-02)


def test_golden_3_4_17_2_holiday_premium_1000_and_overtime_750():
    """3.4 §17.2: 11 h on a holiday, multiplier 1.0, OT HOLIDAY 2.0× → premium 1,000.00 + overtime 750.00."""
    result = premiums([NonWorkingDayWork(HOL, HOLIDAY, 11 * 60)], {"holiday_work_multiplier": "1.0"})
    assert amounts(result) == {"HOLIDAY_PREMIUM": D("1000.00")}
    assert [(e.date, e.minutes, e.status) for e in result.overtime_entries] == [(HOL, 180, "APPROVED")]
    ot = overtime_hours(result.overtime_entries, OT, day_types={HOL: HOLIDAY})
    pay = overtime_pay(ot, RATE, "INR")
    assert [(line.component, line.amount) for line in pay.lines] == [("OT_HOLIDAY", D("750.00"))]


def test_ec_hol_02_multipliers_never_compound():
    result = premiums([NonWorkingDayWork(HOL, HOLIDAY, 11 * 60)], {"holiday_work_multiplier": "1.0"})
    ot = overtime_hours(result.overtime_entries, OT, day_types={HOL: HOLIDAY})
    total = result.total + overtime_pay(ot, RATE, "INR").total
    assert total == D("1750.00")  # never 125 × 1.0 × 2.0 × 3 on top, never a premium on overtime hours
    assert result.days[0].premium_minutes + result.days[0].overtime_minutes == 11 * 60


def test_ec_hol_02_same_premium_beyond_standard_when_configured():
    result = premiums([NonWorkingDayWork(HOL, HOLIDAY, 11 * 60)],
                      {"holiday_hours_beyond_standard": "SAME_PREMIUM"})
    assert amounts(result) == {"HOLIDAY_PREMIUM": D("1375.00")}
    assert result.overtime_entries == ()


def test_holiday_pay_on_overtime_off_premium_for_all_hours():
    """3.1 §6.2 row 2: holiday premium for all hours when overtime is OFF."""
    result = premiums([NonWorkingDayWork(HOL, HOLIDAY, 11 * 60)], ot_on=())
    assert amounts(result) == {"HOLIDAY_PREMIUM": D("1375.00")} and result.overtime_entries == ()


def test_holiday_pay_off_overtime_on_all_hours_to_overtime_bucket():
    result = premiums([NonWorkingDayWork(HOL, HOLIDAY, 6 * 60)], holiday_on=())
    assert result.lines == ()
    assert [(e.minutes, e.origin) for e in result.overtime_entries] == [(360, "HOLIDAY_WORK")]


def test_everything_off_nothing_extra():
    result = premiums([NonWorkingDayWork(HOL, HOLIDAY, 6 * 60)], holiday_on=(), ot_on=())
    assert result.lines == () and result.overtime_entries == ()
    assert "nothing extra" in result.days[0].rule


# --------------------------------------------------------------------------- weekly off (EC-HOL-09)


def test_ec_hol_09_weekly_off_switch_off_overtime_on_pays_weekly_off_bucket():
    result = premiums([NonWorkingDayWork(SUN, WEEKLY_OFF, 5 * 60)], weekly_on=())
    ot = overtime_hours(result.overtime_entries, OT, day_types={SUN: WEEKLY_OFF})
    pay = overtime_pay(ot, RATE, "INR")
    assert [(line.component, line.amount) for line in pay.lines] == [("OT_WEEKLY_OFF", D("1250.00"))]


def test_ec_hol_09_weekly_off_switch_off_overtime_off_pays_nothing():
    result = premiums([NonWorkingDayWork(SUN, WEEKLY_OFF, 5 * 60)], weekly_on=(), ot_on=())
    assert result.lines == () and result.overtime_entries == ()


def test_weekly_off_premium_multiplier_half_is_time_and_a_half():
    result = premiums([NonWorkingDayWork(SUN, WEEKLY_OFF, 6 * 60)])
    assert amounts(result) == {"WEEKLY_OFF_PREMIUM": D("375.00")}


# --------------------------------------------------------------------------- qualification


def test_minimum_hours_boundary():
    below = premiums([NonWorkingDayWork(HOL, HOLIDAY, 4 * 60 - 1)])
    at = premiums([NonWorkingDayWork(HOL, HOLIDAY, 4 * 60)])
    assert below.lines == () and amounts(at) == {"HOLIDAY_PREMIUM": D("500.00")}


def test_approval_required_flag():
    unapproved = NonWorkingDayWork(HOL, HOLIDAY, 6 * 60, approved=False)
    held = premiums([unapproved])
    assert held.lines == () and held.issues[0].code == "HOLIDAY_WORK_NOT_APPROVED"
    paid = premiums([unapproved], {"holiday_work_requires_approval": False})
    assert amounts(paid) == {"HOLIDAY_PREMIUM": D("750.00")}


def test_missing_hourly_rate_fails_closed_and_bad_policy_raises():
    with pytest.raises(TimePayError) as err:
        premiums([NonWorkingDayWork(HOL, HOLIDAY, 6 * 60)], rate=None)
    assert err.value.code == "INVALID_RATE"
    with pytest.raises(TimePayError) as err:
        premiums([NonWorkingDayWork(HOL, HOLIDAY, 6 * 60)], {"holiday_work_compensation": "CASH"})
    assert err.value.code == "INVALID_POLICY"


# --------------------------------------------------------------------------- comp-off (EC-HOL-07)


def test_ec_hol_07_comp_off_credit_expires_after_validity_days():
    result = premiums([NonWorkingDayWork(HOL, HOLIDAY, 8 * 60)],
                      {"holiday_work_compensation": "COMP_OFF", "comp_off_validity_days": 30})
    assert result.lines == ()
    grant = result.comp_off[0]
    assert (grant.hours, grant.days, grant.expires_on) == (D(8), D(1), HOL + timedelta(days=30))


def test_ec_hol_07_premium_and_comp_off_both_and_employee_choice():
    both = premiums([NonWorkingDayWork(HOL, HOLIDAY, 4 * 60)], {"holiday_work_compensation": "PREMIUM_AND_COMP_OFF"})
    assert amounts(both) == {"HOLIDAY_PREMIUM": D("500.00")} and both.comp_off[0].days == D("0.5")
    choice = {"holiday_work_compensation": "EMPLOYEE_CHOICE"}
    missing = premiums([NonWorkingDayWork(HOL, HOLIDAY, 4 * 60)], choice)
    assert missing.lines == () and missing.issues[0].code == "HOLIDAY_WORK_CHOICE_MISSING"
    chose = premiums([NonWorkingDayWork(HOL, HOLIDAY, 4 * 60, choice="COMP_OFF")], choice)
    assert chose.lines == () and len(chose.comp_off) == 1


# --------------------------------------------------------------------------- holiday on a weekly off (EC-HOL-01)


@pytest.mark.parametrize("rule, total, substitutes", [
    ("NONE", D("0"), ()), ("SUBSTITUTE_DAY", D("0"), (SUN,)), ("EXTRA_DAY_PAY", D("1923.08"), ()),
])
def test_ec_hol_01_holiday_on_weekly_off(rule, total, substitutes):
    result = holiday_on_weekly_off([SUN], {"holiday_on_weekly_off": rule}, daily_rate=D("50000") / 26,
                                   currency="INR", holiday_pay_enabled=None)
    assert result.total == total and result.substitute_days == substitutes


def test_ec_hol_01_switch_off_gives_nothing_and_rate_required():
    off = holiday_on_weekly_off([SUN], {"holiday_on_weekly_off": "EXTRA_DAY_PAY"}, daily_rate=D("100"),
                                currency="INR", holiday_pay_enabled=())
    assert off.lines == () and off.unaffected == (SUN,)
    with pytest.raises(TimePayError):
        holiday_on_weekly_off([SUN], {"holiday_on_weekly_off": "EXTRA_DAY_PAY"}, daily_rate=None, currency="INR",
                              holiday_pay_enabled=None)


# --------------------------------------------------------------------------- non-salaried holiday pay (EC-HOL-03, EC-HOL-10)


def hourly(holidays, policy=None, **kwargs):
    kwargs.setdefault("enabled", None)
    return non_salaried_holiday_pay(holidays, policy or {}, hourly_rate=D("100"), standard_hours=D("8"),
                                    currency="INR", **kwargs)


def test_ec_hol_03_absent_day_before_means_no_holiday_pay():
    policy = {"eligibility_adjacent_days": "DAY_BEFORE"}
    result = hourly([HolidayDay(HOL, "PUBLIC", present_before=False, present_after=True)], policy)
    assert result.lines == () and result.skipped[0].reason == "ABSENT_ON_ADJACENT_DAY"


def test_ec_hol_03_present_on_both_days_is_paid_standard_hours():
    result = hourly([HolidayDay(HOL, "PUBLIC", present_before=True, present_after=True)],
                    {"eligibility_adjacent_days": "BOTH"})
    assert result.total == D("800.00") and result.payable_minutes == 480


def test_ec_hol_03_unknown_adjacent_attendance_withholds_with_issue():
    result = hourly([HolidayDay(HOL, "PUBLIC", present_before=True)], {"eligibility_adjacent_days": "BOTH"})
    assert result.lines == () and result.issues[0].code == "HOLIDAY_PAY_ELIGIBILITY_UNKNOWN"


def test_ec_hol_10_holiday_after_exit_date_not_paid():
    result = hourly([HolidayDay(HOL, "PUBLIC"), HolidayDay(HOL + timedelta(days=20), "PUBLIC")],
                    employment_end=HOL + timedelta(days=1))
    assert result.paid == (HOL,) and result.skipped[0].reason == "NOT_EMPLOYED"


def test_average_daily_earnings_mode_and_missing_average():
    policy = {"non_salaried_holiday_pay": "AVERAGE_DAILY_EARNINGS"}
    paid = hourly([HolidayDay(HOL, "COMPANY")], policy, average_daily_earnings=D("912.345"))
    assert paid.total == D("912.35")
    missing = hourly([HolidayDay(HOL, "COMPANY")], policy)
    assert missing.lines == () and missing.issues[0].code == "AVERAGE_DAILY_EARNINGS_MISSING"


def test_optional_holiday_and_paid_types():
    result = hourly([HolidayDay(HOL, "OPTIONAL"), HolidayDay(SUN, "OPTIONAL", availed=True)],
                    {"paid_holiday_types": ["PUBLIC", "OPTIONAL_AVAILED"]})
    assert result.paid == (SUN,)
    company = hourly([HolidayDay(HOL, "COMPANY")], {"paid_holiday_types": ["PUBLIC"]})
    assert company.skipped[0].reason == "TYPE_NOT_PAID"


def test_ec_hol_08_policy_off_where_pack_requires_is_blocking():
    result = hourly([HolidayDay(HOL, "PUBLIC")], {"non_salaried_holiday_pay": "OFF"}, pack_requires=True)
    assert result.lines == () and result.issues[0].code == "HOLIDAY_PAY_REQUIRED_BY_LAW"
    switched_off = hourly([HolidayDay(HOL, "PUBLIC")], enabled=())
    assert switched_off.lines == ()
    locked_on = hourly([HolidayDay(HOL, "PUBLIC")], enabled=(), pack_requires=True)
    assert locked_on.total == D("800.00")


# --------------------------------------------------------------------------- review additions


def _raw_premiums(days, policy):
    return holiday_premiums(days, policy, hourly_rate=RATE, standard_hours=D("8"), currency="INR",
                            holiday_pay_enabled=None, weekly_off_pay_enabled=None, overtime_enabled=None)


def test_holiday_multiplier_is_never_assumed():
    """Contract K:157 / decision I-13: a premium to pay with no multiplier entered fails closed."""
    for blank in ({}, {"holiday_work_multiplier": None}):  # None is policy_types' "not entered"
        with pytest.raises(TimePayError) as err:
            _raw_premiums([NonWorkingDayWork(HOL, HOLIDAY, 6 * 60)], blank)
        assert (err.value.code, err.value.field) == ("INVALID_POLICY", "holiday_work_multiplier")
    comp_off_only = _raw_premiums([NonWorkingDayWork(HOL, HOLIDAY, 6 * 60)], {"holiday_work_compensation": "COMP_OFF"})
    assert comp_off_only.lines == () and len(comp_off_only.comp_off) == 1  # nothing to price, nothing assumed


def test_weekly_off_multiplier_is_never_assumed():
    with pytest.raises(TimePayError) as err:
        _raw_premiums([NonWorkingDayWork(SUN, WEEKLY_OFF, 6 * 60)], {"holiday_work_multiplier": "1.0"})
    assert err.value.field == "weekly_off_work_multiplier"


def test_ec_hol_08_pack_requirement_keeps_holiday_pay_on_when_the_switch_is_off():
    locked = hourly([HolidayDay(HOL, "PUBLIC")], enabled=(), pack_requires=True)
    assert locked.paid == (HOL,) and locked.total == D("800.00") and locked.issues == ()


def test_ec_hol_10_boundary_holiday_on_exit_date_paid_before_start_not():
    result = hourly([HolidayDay(HOL, "PUBLIC"), HolidayDay(HOL - timedelta(days=10), "PUBLIC")],
                    employment_start=HOL - timedelta(days=5), employment_end=HOL)
    assert result.paid == (HOL,) and [s.reason for s in result.skipped] == ["NOT_EMPLOYED"]
