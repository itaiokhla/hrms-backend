"""Late marks, early exits and penalties (3.4 §11, 3.1 §5.7) and shift allowances (3.4 §12, 3.1 §5.8)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from app.payroll.time_pay import (
    LateMark,
    ShiftWorked,
    TimePayError,
    is_early_exit,
    is_late,
    late_penalty,
    night_minutes,
    parse_night_window,
    shift_allowance,
)

DAY = date(2026, 10, 1)
POLICY = {"free_late_marks": 2, "late_marks_per_unit": 3, "unit_days": "0.5", "cap_days_per_period": "2.0"}


def marks(count: int, regularised: int = 0, kind: str = "LATE", start: int = 0):
    return [LateMark(DAY + timedelta(days=start + i), kind, regularised=i < regularised) for i in range(count)]


# --------------------------------------------------------------------------- late marks


def test_golden_3_4_17_4_late_penalty_one_day():
    """3.4 §17.4: 9 marks, 1 regularised, 2 free, every 3 = 0.5 day, cap 2.0 → 6 chargeable → 1.0 day."""
    result = late_penalty(marks(9, regularised=1), POLICY)
    assert result.chargeable == 6
    assert result.penalty_lop_days == D("1.0")
    assert "floor(6 ÷ 3) × 0.5" in result.explanation


def test_day_fraction_cap_and_floor_boundaries():
    capped = late_penalty(marks(20), POLICY)  # 18 chargeable → 3.0 days, capped at 2.0
    assert (capped.uncapped_lop_days, capped.penalty_lop_days) == (D("3.0"), D("2.0"))
    below_unit = late_penalty(marks(4), POLICY)  # 2 chargeable < 3 per unit
    assert below_unit.chargeable == 2 and below_unit.penalty_lop_days == 0
    only_free = late_penalty(marks(2), POLICY)
    assert only_free.chargeable == 0 and only_free.free_marks_used == 2


def test_regularised_marks_kept_when_exemption_off():
    result = late_penalty(marks(9, regularised=1), {**POLICY, "exempt_if_regularized": False})
    assert result.chargeable == 7 and result.penalty_lop_days == D("1.0")


def test_early_exits_count_only_when_count_early_exit():
    mixed = marks(5) + marks(3, kind="EARLY_EXIT", start=10)
    ignored = late_penalty(mixed, POLICY)
    counted = late_penalty(mixed, {**POLICY, "count_early_exit": True})
    assert (ignored.chargeable, ignored.early_exits, ignored.counted_early_exits) == (3, 3, 0)
    assert (counted.chargeable, counted.penalty_lop_days) == (6, D("1.0"))


def test_absent_day_has_no_late_mark_and_basis_limit():
    result = late_penalty(marks(9), POLICY, absent_dates={DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)})
    assert result.dropped_on_absent_days == 3 and result.chargeable == 4
    limited = late_penalty(marks(20), POLICY, basis_days=D("30"), ordinary_lop_days=D("29"))
    assert limited.penalty_lop_days == D("1") and limited.issues[0].code == "LATE_PENALTY_LIMITED_BY_BASIS"


def test_fixed_amount_penalty_line():
    policy = {**POLICY, "penalty_type": "FIXED_AMOUNT", "amount_per_late": {"amount": "100.00", "currency": "INR"}}
    result = late_penalty(marks(9, regularised=1), policy, currency="INR")
    assert result.line is not None
    assert (result.line.component, result.line.kind, result.line.category) == ("LATE_PENALTY", "DEDUCTION",
                                                                               "PENALTY_IF_LEGAL")
    assert result.amount == D("600.00") and result.penalty_lop_days == 0
    assert len(result.line.sources) == 9


def test_fixed_amount_requires_amount_and_currency():
    with pytest.raises(TimePayError) as err:
        late_penalty(marks(3), {"penalty_type": "FIXED_AMOUNT"}, currency="INR")
    assert err.value.code == "INVALID_POLICY"
    with pytest.raises(TimePayError) as err:
        late_penalty(marks(3), {"penalty_type": "FIXED_AMOUNT", "amount_per_late": "100"})
    assert err.value.code == "INVALID_INPUT"
    with pytest.raises(TimePayError) as err:
        late_penalty(marks(3), {"penalty_type": "FIXED_AMOUNT",
                                "amount_per_late": {"amount": "1", "currency": "USD"}}, currency="INR")
    assert err.value.code == "CURRENCY_MISMATCH"


def test_switch_off_records_marks_without_changing_pay():
    result = late_penalty(marks(9), POLICY, enabled=False)
    assert not result.applied and result.chargeable == 7 and result.penalty_lop_days == 0 and result.line is None


def test_ec_var_10_pack_forbids_penalty_deductions():
    result = late_penalty(marks(9), POLICY, penalty_allowed=False)
    assert result.penalty_lop_days == 0 and result.issues[0].code == "LATE_PENALTY_NOT_ALLOWED"
    none_chargeable = late_penalty(marks(2), POLICY, penalty_allowed=False)
    assert none_chargeable.issues == ()


def test_late_and_early_exit_detection():
    start = datetime(2026, 10, 1, 9, 0)
    assert not is_late(start + timedelta(minutes=15), start, 15)
    assert is_late(start + timedelta(minutes=15, seconds=1), start, 15)
    assert not is_late(start + timedelta(hours=2), start, 15, flexible=True)
    end = datetime(2026, 10, 1, 18, 0)
    assert not is_early_exit(end - timedelta(minutes=15), end)
    assert is_early_exit(end - timedelta(minutes=16), end)
    assert is_early_exit(end - timedelta(minutes=6), end, {"early_exit_grace_minutes": 5})


def test_invalid_policy_and_mark_kind():
    with pytest.raises(TimePayError) as err:
        late_penalty(marks(3), {"late_marks_per_unit": 0})
    assert err.value.code == "INVALID_POLICY"
    with pytest.raises(TimePayError):
        late_penalty([LateMark(DAY, "ABSENT")], POLICY)


# --------------------------------------------------------------------------- shift allowance


def shift(day: date, start_hour: int, hours: int, **kwargs) -> ShiftWorked:
    start = datetime.combine(day, datetime.min.time()) + timedelta(hours=start_hour)
    return ShiftWorked(day, start, start + timedelta(hours=hours), **kwargs)


def test_night_window_crossing_midnight_and_breaks():
    assert night_minutes(shift(DAY, 20, 10)) == 480  # 22:00–06:00
    assert night_minutes(shift(DAY, 2, 6)) == 240  # 02:00–06:00 of the same morning
    brk = (datetime(2026, 10, 2, 1), datetime(2026, 10, 2, 2))
    assert night_minutes(shift(DAY, 20, 10, unpaid_breaks=(brk, brk))) == 420  # merged, counted once
    assert night_minutes(shift(DAY, 9, 8)) == 0


def test_night_window_parsing():
    assert parse_night_window("21:30–05:00")[0].minute == 30
    assert parse_night_window({"start": "23:00", "end": "07:00"})[1].hour == 7
    with pytest.raises(TimePayError) as err:
        parse_night_window("late")
    assert err.value.code == "INVALID_POLICY"


def test_per_shift_amount_with_min_minutes_boundary():
    policy = {"allowance_type": "PER_SHIFT_AMOUNT", "amount": "200", "min_minutes_in_window": 120}
    qualifies = shift(DAY, 20, 4)  # 22:00–24:00 = 120 minutes
    short = shift(DAY + timedelta(days=1), 21, 2, )  # 22:00–23:00 = 60 minutes
    result = shift_allowance([qualifies, short], policy, currency="INR")
    assert result.qualifying_night_shifts == 1 and result.total == D("200.00")
    assert [s.night_qualifies for s in result.shifts] == [True, False]


def test_percent_of_hourly_uses_base_rate_not_overtime_multiplier():
    """EC-HOL-02: the night percentage applies to the base hourly rate for night hours."""
    policy = {"allowance_type": "PERCENT_OF_HOURLY", "percent": "10"}
    result = shift_allowance([shift(DAY, 22, 8)], policy, currency="INR", hourly_rate=D("240.384615"))
    assert result.total == D("192.31")  # 10% × 240.384615 × 8 h
    with pytest.raises(TimePayError):
        shift_allowance([shift(DAY, 22, 8)], policy, currency="INR")


def test_fixed_monthly_if_any_and_weekend_amount():
    policy = {"allowance_type": "FIXED_MONTHLY_IF_ANY", "amount": "1500", "weekend_shift_amount": "300"}
    shifts = [shift(DAY, 22, 8), shift(DAY + timedelta(days=1), 22, 8),
              shift(DAY + timedelta(days=3), 9, 8, weekend=True)]
    result = shift_allowance(shifts, policy, currency="INR")
    assert (result.night_amount, result.weekend_amount, result.total) == (D("1500"), D("300"), D("1800.00"))
    assert len(result.lines) == 1 and "weekend" in result.lines[0].explanation
    none = shift_allowance([shift(DAY, 9, 8)], policy, currency="INR")
    assert none.lines == ()


def test_shift_allowance_switch_ranges_and_validation():
    policy = {"allowance_type": "PER_SHIFT_AMOUNT", "amount": "200"}
    result = shift_allowance([shift(DAY, 22, 8), shift(DAY + timedelta(days=10), 22, 8)], policy, currency="INR",
                             enabled=[(DAY, DAY + timedelta(days=5))])
    assert result.total == D("200.00")
    with pytest.raises(TimePayError):
        shift_allowance([], {"allowance_type": "PER_SHIFT_AMOUNT"}, currency="INR")
    with pytest.raises(TimePayError):
        shift_allowance([shift(DAY, 22, 8)] * 2, policy, currency="INR")
    aware = ShiftWorked(DAY, datetime(2026, 10, 1, 22, tzinfo=timezone.utc), datetime(2026, 10, 1, 21))
    with pytest.raises(TimePayError):
        night_minutes(aware)


# --------------------------------------------------------------------------- review additions


def test_ec_var_10_penalty_applies_where_the_pack_allows_it():
    result = late_penalty(marks(9), POLICY, penalty_allowed=True)
    assert result.applied and result.penalty_lop_days == D("1.0") and result.issues == ()


def test_fixed_amount_accepts_policy_types_money_and_zero_without_currency():
    policy = {**POLICY, "penalty_type": "FIXED_AMOUNT", "amount_per_late": {"amount": "0", "currency": None}}
    assert late_penalty(marks(9), policy, currency="INR").amount == 0
    jpy = late_penalty(marks(4), {**POLICY, "penalty_type": "FIXED_AMOUNT",
                                  "amount_per_late": {"amount": "500", "currency": "JPY"}}, currency="JPY")
    assert jpy.line is not None and jpy.line.to_dict()["amount"] == "1000"
    with pytest.raises(TimePayError) as err:
        late_penalty(marks(4), {**POLICY, "penalty_type": "FIXED_AMOUNT",
                                "amount_per_late": {"amount": "5", "currency": None}}, currency="INR")
    assert err.value.code == "CURRENCY_MISMATCH"
