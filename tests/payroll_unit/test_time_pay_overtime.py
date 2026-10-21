"""Overtime: thresholds, buckets/tiers, minimums, rounding, caps, comp-off, midnight (3.4 §9, 3.1 §5.5)."""

from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.payroll.time_pay import (
    HOLIDAY,
    WEEKLY_OFF,
    WORKDAY,
    OvertimeEntry,
    TimePayError,
    WorkedDay,
    default_hourly_rate,
    overtime_hours,
    overtime_pay,
    parse_overtime_policy,
    resolve_overtime_rate,
    round_minutes,
    threshold_overtime,
    validate_overtime_entries,
)

MON = date(2026, 10, 5)  # a Monday
# No multiplier is ever assumed (contract K:157): every policy states its buckets.
OT = {"buckets": [{"day_type": WORKDAY, "after_hours": "0", "multiplier": "1.5"},
                  {"day_type": WEEKLY_OFF, "after_hours": "0", "multiplier": "2.0"},
                  {"day_type": HOLIDAY, "after_hours": "0", "multiplier": "2.0"}]}
WEEK = [MON + timedelta(days=i) for i in range(7)]


def workdays(*dates: date) -> dict:
    return {d: WORKDAY for d in dates}


def entry(entry_id: str, day: date, minutes: int, **kwargs) -> OvertimeEntry:
    return OvertimeEntry(entry_id, day, minutes, **kwargs)


# --------------------------------------------------------------------------- golden vectors


def test_golden_3_4_17_1_daily_then_weekly_thresholds_pay_1500():
    """3.4 §17.1: 10/9/9/11/9/8 h, daily 9 h, weekly 48 h → 3 daily + 5 weekly = 8 h × 125 × 1.5 = 1,500.00."""
    policy = {**OT, "source": "ATTENDANCE_AUTO", "daily_threshold": "9", "weekly_threshold_hours": "48",
              "threshold_combination": "DAILY_THEN_WEEKLY"}
    worked = [10, 9, 9, 11, 9, 8]
    days = [WorkedDay(WEEK[i], h * 3600) for i, h in enumerate(worked)]
    proposal = threshold_overtime(days, policy)
    by_date = {d.date: d for d in proposal.days}
    assert [by_date[WEEK[i]].daily_ot_seconds / 3600 for i in range(6)] == [1, 0, 0, 2, 0, 0]
    assert [by_date[WEEK[i]].weekly_ot_seconds / 3600 for i in range(6)] == [0, 0, 0, 0, 0, 5]
    assert proposal.total_ot_seconds == 8 * 3600
    assert all(e.status == "APPROVED" for e in proposal.entries)

    rate = default_hourly_rate(D("26000.00"), 26, 8)
    assert rate == D("125")
    ot = overtime_hours(proposal.entries, policy, day_types=workdays(*WEEK[:6]))
    assert ot.hours_workday == 8
    pay = overtime_pay(ot, rate, "INR")
    assert [line.to_dict()["amount"] for line in pay.lines] == ["1500.00"]
    assert pay.lines[0].component == "OT_WORKDAY"


def test_golden_3_5_17_overtime_line_1442_31():
    """3.5 §17: 50,000 ÷ 26 ÷ 8 = 240.3846… × 1.5 × 4 h = 1,442.31 (rate never rounded first)."""
    rate = default_hourly_rate(D("50000"), 26, 8)
    assert str(rate).startswith("240.384615384")
    ot = overtime_hours([entry("e1", MON, 240)], OT, day_types=workdays(MON))
    line = overtime_pay(ot, rate, "INR").lines[0]
    assert line.amount == D("1442.31")
    assert line.unrounded.quantize(D("0.0001")) == D("1442.3077")
    assert line.units == D("4.0000")


# --------------------------------------------------------------------------- thresholds (EC-OT-03)


def test_ec_ot_03_each_hour_counted_once_daily_then_weekly():
    days = [WorkedDay(WEEK[i], h * 3600) for i, h in enumerate([10, 9, 9, 11, 9, 8])]
    daily_only = threshold_overtime(days, {**OT, "daily_threshold": "9"})
    weekly_then = threshold_overtime(days, {**OT, "daily_threshold": "9", "weekly_threshold_hours": "48",
                                            "threshold_combination": "DAILY_THEN_WEEKLY"})
    assert daily_only.total_ot_seconds == 3 * 3600
    assert weekly_then.total_ot_seconds == 8 * 3600  # not 3 + (56 − 48) = 11
    assert weekly_then.total_ot_seconds <= sum(d.worked_seconds for d in days) - 48 * 3600


def test_ec_ot_03_weekly_only_allocates_latest_days_first():
    days = [WorkedDay(WEEK[i], h * 3600) for i, h in enumerate([10, 10, 10, 10, 10, 2])]
    result = threshold_overtime(days, {**OT, "weekly_threshold_hours": "48", "threshold_combination": "WEEKLY"})
    assert {d.date: d.weekly_ot_seconds / 3600 for d in result.days if d.weekly_ot_seconds} == {WEEK[5]: 2, WEEK[4]: 2}
    assert [e.status for e in result.entries] == ["PROPOSED", "PROPOSED"]  # APPROVED_REQUESTS needs approval


def test_ec_ot_03_boundary_exactly_at_weekly_threshold_is_no_overtime():
    days = [WorkedDay(WEEK[i], 8 * 3600) for i in range(6)]
    result = threshold_overtime(days, {**OT, "daily_threshold": "8", "weekly_threshold_hours": "48",
                                       "threshold_combination": "DAILY_THEN_WEEKLY"})
    assert result.total_ot_seconds == 0 and result.entries == ()


def test_paid_leave_counts_toward_weekly_threshold_only_when_flagged():
    days = [WorkedDay(WEEK[i], 9 * 3600) for i in range(5)]
    plain = threshold_overtime(days, {**OT, "weekly_threshold_hours": "40", "threshold_combination": "WEEKLY"})
    days[0] = WorkedDay(WEEK[0], 9 * 3600, leave_seconds_toward_threshold=4 * 3600)
    credited = threshold_overtime(days, {**OT, "weekly_threshold_hours": "40", "threshold_combination": "WEEKLY"})
    assert plain.weekly_ot_seconds == 5 * 3600
    assert credited.weekly_ot_seconds == 9 * 3600


def test_threshold_ignores_holiday_and_weekly_off_hours_and_uses_shift_duration():
    days = [WorkedDay(WEEK[0], 10 * 3600, shift_hours=D("8")), WorkedDay(WEEK[6], 6 * 3600, day_type=WEEKLY_OFF)]
    result = threshold_overtime(days, OT)
    assert result.total_ot_seconds == 2 * 3600
    with pytest.raises(TimePayError) as err:
        threshold_overtime([WorkedDay(WEEK[0], 3600)], OT)
    assert err.value.code == "INVALID_INPUT"


def test_threshold_rejects_duplicate_dates_and_bad_policy():
    with pytest.raises(TimePayError) as err:
        threshold_overtime([WorkedDay(MON, 3600, shift_hours=D(8))] * 2, OT)
    assert err.value.code == "INVALID_INPUT"
    with pytest.raises(TimePayError) as err:
        parse_overtime_policy({**OT, "threshold_combination": "WEEKLY"})
    assert err.value.code == "INVALID_POLICY"


# --------------------------------------------------------------------------- approval and switch (EC-OT-01/02)


def test_ec_ot_02_only_approved_minutes_are_paid():
    entries = [entry("a", MON, 120), entry("p", WEEK[1], 120, status="PROPOSED"),
               entry("r", WEEK[2], 120, status="REJECTED")]
    ot = overtime_hours(entries, OT, day_types=workdays(*WEEK[:3]))
    assert ot.paid_minutes == 120
    assert {(s.entry_id, s.reason) for s in ot.excluded} == {("p", "NOT_APPROVED"), ("r", "NOT_APPROVED")}


def test_ec_ot_02_nothing_approved_means_no_line():
    ot = overtime_hours([entry("p", MON, 120, status="PENDING")], OT, day_types=workdays(MON))
    assert overtime_pay(ot, D("100"), "INR").lines == ()


def test_ec_ot_01_switch_off_mid_period_pays_days_while_on():
    """EC-OT-01 / EC-CFG-08: switched OFF on the 20th → overtime on days 1–19 is still paid (DAY grain)."""
    d10, d19, d20 = date(2026, 10, 10), date(2026, 10, 19), date(2026, 10, 20)
    enabled = [(date(2026, 10, 1), d19)]
    ot = overtime_hours([entry("a", d10, 60), entry("b", d19, 60), entry("c", d20, 60)], OT,
                        day_types=workdays(d10, d19, d20), enabled=enabled)
    assert ot.paid_minutes == 120
    assert [(s.entry_id, s.reason) for s in ot.recorded_only] == [("c", "SWITCH_OFF")]


def test_ec_cfg_08_switch_never_on_records_but_pays_nothing():
    ot = overtime_hours([entry("a", MON, 60)], OT, day_types=workdays(MON), enabled=())
    assert ot.paid_minutes == 0 and ot.recorded_only[0].minutes == 60


def test_ec_ot_06_exempt_policy_rejected_for_legally_covered_workers():
    with pytest.raises(TimePayError) as err:
        overtime_hours([entry("a", MON, 60)], {"exempt": True}, day_types=workdays(MON), legally_covered=True)
    assert err.value.code == "EXEMPT_NOT_ALLOWED"
    ot = overtime_hours([entry("a", MON, 60)], {"exempt": True}, day_types=workdays(MON))
    assert ot.exempt and ot.paid_minutes == 0 and ot.recorded_only[0].reason == "EXEMPT"


# --------------------------------------------------------------------------- minimums and rounding


@pytest.mark.parametrize("minutes, increment, mode, expected", [
    (44, 15, "DOWN", 30), (45, 15, "DOWN", 45), (37, 15, "NEAREST", 30), (38, 15, "NEAREST", 45),
    (31, 15, "UP", 45), (30, 15, "UP", 30), (44, 0, "DOWN", 44),
])
def test_round_minutes(minutes, increment, mode, expected):
    assert round_minutes(minutes, increment, mode) == expected


def test_min_minutes_dropped_before_rounding():
    policy = {**OT, "min_minutes": 30, "rounding": {"increment_minutes": 15, "mode": "UP"}}
    ot = overtime_hours([entry("short", MON, 29), entry("ok", WEEK[1], 31)], policy, day_types=workdays(*WEEK[:2]))
    assert [(s.entry_id, s.reason) for s in ot.excluded] == [("short", "BELOW_MIN_MINUTES")]
    assert ot.paid_minutes == 45


def test_rounding_to_zero_is_excluded():
    ot = overtime_hours([entry("a", MON, 10)], {**OT, "min_minutes": 0, "rounding": {"increment_minutes": 15}},
                        day_types=workdays(MON))
    assert ot.paid_minutes == 0 and ot.excluded[0].reason == "ROUNDED_TO_ZERO"


# --------------------------------------------------------------------------- caps (EC-OT-04)


def test_ec_ot_04_cap_exceeded_raises_exception_by_default():
    ot = overtime_hours([entry("a", MON, 180)], {**OT, "caps": {"per_day": "2"}}, day_types=workdays(MON))
    assert ot.paid_minutes == 180  # kept for the reviewer; the issue blocks approval
    assert [(i.code, i.severity) for i in ot.issues] == [("OT_CAP_EXCEEDED", "BLOCKING")]


def test_ec_ot_04_truncate_cuts_at_the_cap():
    policy = {**OT, "caps": {"per_week": "3"}, "cap_action": "TRUNCATE"}
    ot = overtime_hours([entry("a", MON, 120), entry("b", WEEK[1], 120)], policy, day_types=workdays(*WEEK[:2]))
    assert ot.paid_minutes == 180
    assert [(s.entry_id, s.minutes, s.reason) for s in ot.excluded] == [("b", 60, "CAP_TRUNCATED")]
    assert ot.issues[0].code == "OT_CAP_TRUNCATED" and ot.issues[0].severity == "WARNING"


def test_ec_ot_04_boundary_exactly_at_cap_and_prior_usage():
    at_cap = overtime_hours([entry("a", MON, 120)], {**OT, "caps": {"per_day": "2"}}, day_types=workdays(MON))
    assert at_cap.issues == ()
    policy = {**OT, "caps": {"per_month": "10"}, "cap_action": "TRUNCATE"}
    prior = {("per_month", date(2026, 10, 1)): 9 * 60}
    ot = overtime_hours([entry("a", MON, 120)], policy, day_types=workdays(MON), prior_minutes=prior)
    assert ot.paid_minutes == 60


def test_quarter_cap_window():
    policy = {**OT, "caps": {"per_quarter": "1"}}
    ot = overtime_hours([entry("a", date(2026, 11, 2), 120)], policy, day_types=workdays(date(2026, 11, 2)))
    assert ot.issues[0].date == date(2026, 10, 1)


# --------------------------------------------------------------------------- impossible entries (EC-OT-10)


def test_ec_ot_10_negative_minutes_rejected():
    with pytest.raises(TimePayError) as err:
        validate_overtime_entries([entry("a", MON, -30)])
    assert err.value.code == "INVALID_MINUTES"


def test_ec_ot_10_more_than_24_hours_in_a_day_rejected():
    with pytest.raises(TimePayError) as err:
        validate_overtime_entries([entry("a", MON, 800), entry("b", MON, 700)])
    assert err.value.code == "INVALID_MINUTES"
    with pytest.raises(TimePayError):
        validate_overtime_entries([entry("a", MON, 1441)])


def test_ec_ot_10_overlapping_entries_rejected_adjacent_allowed():
    a = entry("a", MON, 60, start=datetime(2026, 10, 5, 18), end=datetime(2026, 10, 5, 19))
    b = entry("b", MON, 60, start=datetime(2026, 10, 5, 18, 30), end=datetime(2026, 10, 5, 19, 30))
    c = entry("c", MON, 60, start=datetime(2026, 10, 5, 19), end=datetime(2026, 10, 5, 20))
    with pytest.raises(TimePayError) as err:
        validate_overtime_entries([a, b])
    assert err.value.code == "OVERLAPPING_ENTRIES"
    validate_overtime_entries([a, c])


def test_ec_ot_10_minutes_longer_than_span_and_float_minutes_rejected():
    with pytest.raises(TimePayError) as err:
        validate_overtime_entries([entry("a", MON, 90, start=datetime(2026, 10, 5, 18), end=datetime(2026, 10, 5, 19))])
    assert err.value.code == "INVALID_MINUTES"
    with pytest.raises(TimePayError):
        validate_overtime_entries([entry("a", MON, 60.0)])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- comp-off (EC-OT-09)


def test_ec_ot_09_comp_off_is_credited_not_paid():
    policy = {**OT, "comp_off_allowed": True, "comp_off_ratio": "1.5"}
    ot = overtime_hours([entry("a", MON, 120, comp_off=True), entry("b", WEEK[1], 60)], policy,
                        day_types=workdays(*WEEK[:2]))
    assert ot.paid_minutes == 60
    assert ot.comp_off_credit_hours == D("3")
    assert overtime_pay(ot, D("100"), "INR").lines[0].amount == D("150.00")


def test_ec_ot_09_comp_off_not_allowed_is_a_blocking_issue():
    ot = overtime_hours([entry("a", MON, 120, comp_off=True)], OT, day_types=workdays(MON))
    assert ot.paid_minutes == 0 and ot.comp_off == ()
    assert ot.issues[0].code == "OT_COMP_OFF_NOT_ALLOWED"


# --------------------------------------------------------------------------- midnight (EC-OT-05)


NYE, NYD = date(2026, 12, 31), date(2027, 1, 1)
NIGHT = entry("n", NYE, 480, start=datetime(2026, 12, 31, 20), end=datetime(2027, 1, 1, 4))


def test_ec_ot_05_split_at_midnight_puts_hours_after_midnight_in_holiday_bucket():
    policy = {**OT, "cross_midnight": "SPLIT_AT_MIDNIGHT"}
    ot = overtime_hours([NIGHT], policy, day_types={NYE: WORKDAY, NYD: HOLIDAY})
    assert ot.minutes_for(WORKDAY) == 240 and ot.minutes_for(HOLIDAY) == 240
    pay = overtime_pay(ot, D("100"), "INR")
    assert {line.component: line.amount for line in pay.lines} == {"OT_WORKDAY": D("600.00"), "OT_HOLIDAY": D("800.00")}


def test_ec_ot_05_shift_start_date_keeps_every_hour_on_the_start_date():
    ot = overtime_hours([NIGHT], OT, day_types={NYE: WORKDAY, NYD: HOLIDAY})
    assert ot.minutes_for(WORKDAY) == 480 and ot.minutes_for(HOLIDAY) == 0


def test_ec_ot_05_missing_day_type_fails_closed():
    with pytest.raises(TimePayError) as err:
        overtime_hours([NIGHT], {**OT, "cross_midnight": "SPLIT_AT_MIDNIGHT"}, day_types={NYE: WORKDAY})
    assert err.value.code == "INVALID_INPUT"


# --------------------------------------------------------------------------- buckets, tiers and rates


def test_tiers_by_after_hours():
    policy = {**OT, "buckets": [{"day_type": WORKDAY, "after_hours": "0", "multiplier": "1.5"},
                          {"day_type": WORKDAY, "after_hours": "2", "multiplier": "2.0"},
                          {"day_type": WEEKLY_OFF, "after_hours": "0", "multiplier": "2.0"},
                          {"day_type": HOLIDAY, "after_hours": "0", "multiplier": "2.5"}]}
    ot = overtime_hours([entry("a", MON, 150), entry("b", MON, 90)], policy, day_types=workdays(MON))
    assert [(b.multiplier, b.minutes) for b in ot.buckets] == [(D("1.5"), 120), (D("2.0"), 120)]
    line = overtime_pay(ot, D("100"), "INR").lines[0]
    assert line.amount == D("700.00")  # 100 × (1.5 × 2 + 2.0 × 2)
    assert "after 2 h" in line.explanation


def test_bucket_policy_validation():
    with pytest.raises(TimePayError) as err:
        parse_overtime_policy({"buckets": [{"day_type": WORKDAY, "after_hours": "0", "multiplier": "1.5"}]})
    assert err.value.code == "INVALID_POLICY"
    with pytest.raises(TimePayError):
        parse_overtime_policy({"buckets": [{"day_type": WORKDAY, "after_hours": "0", "multiplier": 1.5}]})


def test_ec_ot_08_pack_regular_rate_overrides_policy_formula():
    rate = resolve_overtime_rate(D("125"), pack_regular_rate=D("131.25"))
    assert rate.rate == D("131.25") and rate.source == "PACK_REGULAR_RATE"
    plain = resolve_overtime_rate(D("125"))
    assert plain.rate == D("125") and plain.source == "POLICY_FORMULA"


def test_one_source_per_date_prevents_double_counting():
    entries = [entry("t", MON, 60, origin="THRESHOLD"), entry("r", MON, 60)]
    with pytest.raises(TimePayError) as err:
        overtime_hours(entries, OT, day_types=workdays(MON))
    assert err.value.code == "DOUBLE_COUNTED_HOURS"


def test_overtime_pay_currency_minor_units_and_rate_validation():
    ot = overtime_hours([entry("a", MON, 90)], OT, day_types=workdays(MON))
    jpy = overtime_pay(ot, D("1234.5"), "JPY").lines[0]
    assert jpy.to_dict()["amount"] == "2778"  # 1234.5 × 1.5 × 1.5 = 2777.625
    kwd = overtime_pay(ot, D("1.2345"), "KWD").lines[0]
    assert kwd.to_dict()["amount"] == "2.778"
    with pytest.raises(TimePayError) as err:
        overtime_pay(ot, D("0"), "INR")
    assert err.value.code == "INVALID_RATE"
    with pytest.raises(TimePayError) as err:
        overtime_pay(ot, D("100"), "XAU")
    assert err.value.code == "INVALID_CURRENCY"
    with pytest.raises(TimePayError):
        overtime_pay(ot, D("100"), "INR", minor_unit=0)


def test_default_hourly_rate_rejects_floats_and_zero_basis():
    with pytest.raises(TimePayError):
        default_hourly_rate(26000.0, 26, 8)  # type: ignore[arg-type]
    with pytest.raises(TimePayError):
        default_hourly_rate(D("26000"), 0, 8)


# --------------------------------------------------------------------------- review additions


def test_no_overtime_multiplier_is_ever_assumed():
    """Contract K:157 / decision I-13: missing buckets or a blank multiplier fail closed."""
    with pytest.raises(TimePayError) as err:
        parse_overtime_policy({})
    assert (err.value.code, err.value.field) == ("INVALID_POLICY", "buckets")
    blank = {"buckets": [{**b, "multiplier": None} for b in OT["buckets"]]}  # policy_types draft shape
    with pytest.raises(TimePayError) as err:
        overtime_hours([entry("a", MON, 60)], blank, day_types=workdays(MON))
    assert err.value.code == "INVALID_POLICY" and err.value.field == "buckets[0].multiplier"
    assert parse_overtime_policy({"exempt": True}).tiers == ()  # exempt workers need no multiplier


def test_ec_ot_01_boundary_last_on_day_paid_first_off_day_recorded():
    d19, d20 = date(2026, 10, 19), date(2026, 10, 20)
    ot = overtime_hours([entry("b", d19, 60), entry("c", d20, 60)], OT, day_types=workdays(d19, d20),
                        enabled=[(date(2026, 10, 1), d19)])
    assert [a.entry_id for a in ot.allocations] == ["b"]
    assert [(s.entry_id, s.date, s.reason) for s in ot.recorded_only] == [("c", d20, "SWITCH_OFF")]


def test_ec_cfg_08_split_shift_pays_only_the_part_on_dates_while_on():
    """DAY grain with SPLIT_AT_MIDNIGHT: the after-midnight part follows its own date's switch value."""
    policy = {**OT, "cross_midnight": "SPLIT_AT_MIDNIGHT"}
    ot = overtime_hours([NIGHT], policy, day_types={NYE: WORKDAY, NYD: HOLIDAY}, enabled=[(date(2026, 12, 1), NYE)])
    assert ot.minutes_for(WORKDAY) == 240 and ot.minutes_for(HOLIDAY) == 0
    assert [(s.date, s.minutes, s.reason) for s in ot.recorded_only] == [(NYD, 240, "SWITCH_OFF")]
    all_on = overtime_hours([NIGHT], policy, day_types={NYE: WORKDAY, NYD: HOLIDAY}, enabled=None)
    assert all_on.paid_minutes == 480


def test_ec_ot_06_covered_workers_with_a_non_exempt_policy_are_paid():
    ot = overtime_hours([entry("a", MON, 60)], {**OT, "exempt": False}, day_types=workdays(MON), legally_covered=True)
    assert ot.paid_minutes == 60 and not ot.exempt


def test_ec_ot_08_invalid_pack_rate_is_rejected():
    with pytest.raises(TimePayError) as err:
        resolve_overtime_rate(D("125"), pack_regular_rate=D("0"))
    assert err.value.code == "INVALID_RATE"
    with pytest.raises(TimePayError):
        resolve_overtime_rate(D("125"), pack_regular_rate=131.25)  # type: ignore[arg-type]
