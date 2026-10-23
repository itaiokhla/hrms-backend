"""Regression (found by the golden verification suite, tests/payroll/verification/test_golden_calculations.py::
test_overtime_daily_then_weekly_thresholds_and_hourly_pay_from_attendance): an HOURLY worker's overtime hour derived
from attendance was paid twice - once at the hourly rate inside ``att.payable_hours`` (every worked hour) and again
in OT_WORKDAY at the multiplier - i.e. 2.5x instead of 1.5x. 3.4 §9.4: "An OT multiplier is the total rate for the
hour, because overtime hours are not included in salary"; §9.5: workday hours beyond the threshold go to the
WORKDAY bucket only. ``att.payable_hours`` now excludes the paid overtime minutes that come from recorded worked
time (origins THRESHOLD / HOLIDAY_WORK / WEEKLY_OFF_WORK). A REQUEST's hours need not be in attendance and are
left as they were.

Hand-computed: November 2026 has 30 days, Sundays 1, 8, 15, 22, 29 -> 25 Mon-Sat working days. 8 h on every
working day except Monday 2 Nov: 10 h. Daily threshold 9 h (DAILY) -> 1 h of overtime on 2 Nov.
  worked hours = 24 x 8 + 10 = 202; payable hours = 202 - 1 = 201; BASIC = 201 x 250.00 = 50,250.00
  OT = 1 x 250.00 x 1.5 = 375.00; gross = 50,625.00 (was 202 x 250 + 375 = 50,875.00).
"""

from __future__ import annotations

from decimal import Decimal

from app.payroll.engine import calculate
from tests.payroll_unit.test_engine_fixtures import days, overtime_policy, settings, snapshot, use_stages
from tests.payroll_unit.test_engine_golden import all_workdays, amounts, nov, segment, worked

D = Decimal
HOURLY = {"id": 951, "version": 1, "structure_type": "HOURLY", "currency": "INR", "pay_basis_amount": "250.00",
          "lines": [{"component": "BASIC", "method": "RATE_X_UNITS",
                     "params": {"rate": "250.00", "units": "att.payable_hours"}}]}
AUTO = {"source": "ATTENDANCE_AUTO", "daily_threshold": "9", "threshold_combination": "DAILY",
        "rate_formula": 'RATE("BASIC")'}


def _data(entries, policy=AUTO):
    return snapshot(
        employee={"employee_id": 503, "hire_date": "2023-01-01", "country": "IN", "salaried": False,
                  "standard_hours_per_day": "8"},
        segments=[segment(HOURLY)],
        days=days(per_day=all_workdays(28800, overrides={nov(2): worked(36000)})),
        settings=settings(payroll__attendance__enabled=True, payroll__overtime__enabled=True),
        policies={"OVERTIME_POLICY": overtime_policy(**policy)},
        overtime_entries=entries,
    )


def _check(result, basic_hours):
    got = amounts(result)
    assert result.line("BASIC").units == D(basic_hours)
    assert got["BASIC"] == D(basic_hours) * D("250.00")
    assert got["OT_WORKDAY"] == 1 * D("250.00") * D("1.5") == D("375.00")
    assert result.totals.gross == got["BASIC"] + got["OT_WORKDAY"]


def test_threshold_overtime_from_the_ledger_is_not_also_a_payable_hour(monkeypatch):
    use_stages(monkeypatch)
    result = calculate(_data([]))
    _check(result, 24 * 8 + 10 - 1)                       # 201 h -> 50,250.00
    assert result.totals.gross == D("50625.00")


def test_stored_threshold_entry_is_not_also_a_payable_hour(monkeypatch):
    use_stages(monkeypatch)
    stored = [{"entry_id": "overtime:1", "date": nov(2).isoformat(), "minutes": 60, "status": "APPROVED",
               "origin": "THRESHOLD"}]
    _check(calculate(_data(stored)), 201)


def test_requested_overtime_hours_are_left_as_they_were(monkeypatch):
    """APPROVED_REQUESTS: the request's hour is not taken from the ledger, so payable hours stay 202 (the request
    may cover time that is not in attendance); only the 1 h request is paid as overtime."""
    use_stages(monkeypatch)
    policy = {"source": "APPROVED_REQUESTS", "rate_formula": 'RATE("BASIC")'}
    request = [{"entry_id": "ot-r", "date": nov(2).isoformat(), "minutes": 60}]
    _check(calculate(_data(request, policy)), 202)
