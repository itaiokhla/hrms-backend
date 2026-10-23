"""Independent verification of the seed-defect fixes (pure).

1. The overtime hourly rate (``SUM_TAG_HOURLY("OT_BASE")``) for monthly, hourly, daily and part-time workers,
   mixed OT bases, LOP, joiners and a bi-weekly period.
2. IN fixture pack: TDS ON with no TAXABLE_INCOME earning is never a silent zero.
3. Decision I-20: the per-period pay basis on weekly (52/53), bi-weekly (26/27), semi-monthly (24) and monthly
   (12) calendars, mid-period joiner proration and the compensation ``pay_period`` terms.

Every expectation is computed by hand in the docstring or comment of its test.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.payroll import packs
from app.payroll.engine import calculate
from app.payroll.engine.statutory import compute
from app.payroll.packs import in_fixture
from app.payroll.packs.base import PackContext, earning
from tests.payroll_unit.test_engine_fixtures import (
    MULTIPLIERS,
    NOV_END,
    NOV_START,
    absent,
    catalogue,
    component,
    days,
    example_a_structure,
    settings,
    snapshot,
)

D = Decimal
LEGACY = 'SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day'

HOURLY_WAGE = component("HOURLY_WAGE", category="BASE", tags=["OT_BASE", "PART_OF_GROSS"],
                        calc_method="RATE_X_UNITS", sort_order=10)
DAY_WAGE = component("DAY_WAGE", category="BASE", tags=["OT_BASE", "PART_OF_GROSS"], calc_method="RATE_X_UNITS",
                     sort_order=11)
LUMP = component("LUMP", category="BASE", tags=["OT_BASE", "PART_OF_GROSS"], calc_method="RATE_X_UNITS",
                 sort_order=12)


def ot_policy(rate_formula: str | None = None) -> dict:
    fields = {"buckets": MULTIPLIERS, "min_minutes": 30, "rounding": {"increment_minutes": 15, "mode": "DOWN"}}
    if rate_formula is not None:
        fields["rate_formula"] = rate_formula
    return {"id": 31, "version": 1, "policy_hash": "ot-hash", "fields": fields}


def worked_all_month(hours: int = 8) -> dict:
    """Monday-Saturday of November 2026 present (25 days, Sundays off)."""
    result, when = {}, NOV_START
    while when <= NOV_END:
        if when.weekday() != 6:
            result[when] = {"attendance": {"status": "PRESENT", "worked_seconds": hours * 3600}}
        when += dt.timedelta(days=1)
    return result


def structure(structure_type: str, *lines: dict, amount: str | None = None) -> dict:
    return {"id": 950, "version": 1, "structure_type": structure_type, "currency": "INR",
            "pay_basis_amount": amount, "lines": list(lines)}


def fixed(code: str, amount: str) -> dict:
    return {"component": code, "method": "FIXED", "amount": amount}


def rxu(code: str, rate: str, units: str) -> dict:
    return {"component": code, "method": "RATE_X_UNITS", "params": {"rate": rate, "units": units}}


def ot_run(compensation: dict, *, extra=(), attendance: bool = False, employee: dict | None = None,
           rate_formula: str | None = None, ot_date: str = "2026-11-05", start: dt.date = NOV_START,
           end: dt.date = NOV_END, day_list=None, period: dict | None = None, hours: int = 8):
    """2 h overtime on a working day (1.5×)."""
    data = snapshot(
        segments=[{"key": "S1", "start": start.isoformat(), "end": end.isoformat(), "assignment_id": 41,
                   "compensation": compensation}],
        components=catalogue(*extra),
        settings=settings(payroll__overtime__enabled=True, payroll__attendance__enabled=attendance),
        policies={"OVERTIME_POLICY": ot_policy(rate_formula)},
        overtime_entries=[{"entry_id": "ot-1", "date": ot_date, "minutes": 120}],
        days=day_list if day_list is not None else (days(per_day=worked_all_month(hours)) if attendance else days()),
    )
    if employee:
        data["employee"] = {**data["employee"], **employee}
    if period:
        data["period"] = {**data["period"], **period}
    return calculate(data)


def ot(result):
    lines = [line for line in result.lines if line.component == "OT_WORKDAY"]
    assert len(lines) == 1, [(i.code, i.message) for i in result.issues]
    return lines[0], D(lines[0].trace["ot_hourly_rate"]["rate"])


def codes(result) -> set[str]:
    return {i.code for i in result.issues}


UNIT_WARNINGS = {"OT_BASE_UNITS_MIXED", "OT_BASE_UNIT_UNKNOWN"}


# --------------------------------------------------------------------------- 1. overtime hourly rate


def test_monthly_salaried_fixed_basic():
    """BASIC 31,200.00 a month (OT_BASE), November 30 days, 8 h: 31,200 ÷ 30 ÷ 8 = 130.00/h; 2 × 1.5 × 130 = 390.00."""
    line, rate = ot(ot_run(structure("GROSS_BASED", fixed("BASIC", "31200.00"))))
    assert rate == D("130") and line.amount == D("390.00")


def test_hourly_worker():
    """60.00 per payable hour: OT 2 × 1.5 × 60 = 180.00; the hourly wage itself is 25 days × 8 h × 60 = 12,000.00."""
    result = ot_run(structure("HOURLY", rxu("HOURLY_WAGE", "60.00", "att.payable_hours"), amount="60.00"),
                    extra=[HOURLY_WAGE], attendance=True)
    line, rate = ot(result)
    assert rate == D("60") and line.amount == D("180.00")
    assert result.line("HOURLY_WAGE").amount == D("12000.00")
    assert not codes(result) & UNIT_WARNINGS


def test_daily_worker():
    """1,000.00 per paid day ÷ 8 h = 125.00/h; 2 × 1.5 × 125 = 375.00."""
    result = ot_run(structure("DAILY", rxu("DAY_WAGE", "1000.00", "att.paid_days"), amount="1000.00"),
                    extra=[DAY_WAGE])
    line, rate = ot(result)
    assert rate == D("125") and line.amount == D("375.00")
    assert not codes(result) & UNIT_WARNINGS


def test_part_time_salaried_on_a_four_hour_day():
    """FTE 0.5 on a 4 h day, BASIC 25,000.00: 25,000 ÷ 30 ÷ 4 = 208.3333…/h (same as a full-timer on 50,000 / 8 h);
    2 × 1.5 × 208.3333… = 625.00."""
    line, rate = ot(ot_run(structure("GROSS_BASED", fixed("BASIC", "25000.00")),
                           employee={"fte": "0.5", "standard_hours_per_day": "4"}))
    assert rate == D(25000) / 30 / 4 and line.amount == D("625.00")


def test_part_time_daily_and_hourly():
    """FTE 0.5, 4 h day: 400.00 per day ÷ 4 = 100.00/h → 300.00; 48.00/h stays 48.00/h → 2 × 1.5 × 48 = 144.00."""
    part_time = {"fte": "0.5", "standard_hours_per_day": "4"}
    line, rate = ot(ot_run(structure("DAILY", rxu("DAY_WAGE", "400.00", "att.paid_days")), extra=[DAY_WAGE],
                           employee=part_time))
    assert rate == D("100") and line.amount == D("300.00")
    line, rate = ot(ot_run(structure("HOURLY", rxu("HOURLY_WAGE", "48.00", "att.payable_hours")),
                           extra=[HOURLY_WAGE], attendance=True, employee=part_time, hours=4))
    assert rate == D("48") and line.amount == D("144.00")


def test_period_day_and_hour_rates_in_one_ot_base():
    """BASIC 24,000 ÷ 30 ÷ 8 = 100.00 + DAY_WAGE 80.00/day ÷ 8 = 10.00 + HOURLY_WAGE 10.00/h = 120.00/h;
    2 × 1.5 × 120 = 360.00, with no unit warning."""
    result = ot_run(structure("GROSS_BASED", fixed("BASIC", "24000.00"), rxu("DAY_WAGE", "80.00", "att.paid_days"),
                              rxu("HOURLY_WAGE", "10.00", "att.payable_hours")),
                    extra=[DAY_WAGE, HOURLY_WAGE], attendance=True)
    line, rate = ot(result)
    assert rate == D("120") and line.amount == D("360.00")
    assert not codes(result) & UNIT_WARNINGS


def test_lop_does_not_lower_the_rate():
    """3 LOP days: BASIC 24,000 × 27 ÷ 30 = 21,600.00, but the OT rate stays 24,000 ÷ 30 ÷ 8 = 100.00 → 300.00."""
    result = ot_run(structure("GROSS_BASED", fixed("BASIC", "24000.00")),
                    day_list=days(per_day=absent(dt.date(2026, 11, 10), dt.date(2026, 11, 11), dt.date(2026, 11, 12))))
    line, rate = ot(result)
    assert result.line("BASIC").amount == D("21600.00")
    assert rate == D("100") and line.amount == D("300.00")


def test_mid_period_joiner_keeps_the_full_period_rate():
    """Joined 16 Nov: BASIC 24,000 × 15 ÷ 30 = 12,000.00; OT on Thu 19 Nov at 24,000 ÷ 30 ÷ 8 = 100.00 → 300.00."""
    joined = dt.date(2026, 11, 16)
    result = ot_run(structure("GROSS_BASED", fixed("BASIC", "24000.00")), start=joined, ot_date="2026-11-19",
                    employee={"hire_date": joined.isoformat()},
                    day_list=days(segments=[("S1", joined, NOV_END)], employed_from=joined))
    line, rate = ot(result)
    assert result.line("BASIC").amount == D("12000.00")
    assert rate == D("100") and line.amount == D("300.00")


def test_unconvertible_only_ot_base_is_warned_not_silent():
    """A RATE_X_UNITS line with constant units (24,000 × 1) has no hourly equivalent: the rate is 0 and OT pays 0,
    but the review shows OT_BASE_UNIT_UNKNOWN naming LUMP."""
    result = ot_run(structure("GROSS_BASED", rxu("LUMP", "24000.00", "1")), extra=[LUMP])
    found = [i for i in result.issues if i.code == "OT_BASE_UNIT_UNKNOWN"]
    assert len(found) == 1 and found[0].severity == "WARNING" and found[0].details["left_out"] == ["LUMP"]


def test_legacy_formula_on_a_daily_worker_is_flagged():
    """800.00/day ÷ 30 ÷ 8 = 3.3333…/h → 2 × 1.5 = 10.00: the formula's value is paid, with OT_BASE_UNITS_MIXED."""
    result = ot_run(structure("DAILY", rxu("DAY_WAGE", "800.00", "att.paid_days")), extra=[DAY_WAGE],
                    rate_formula=LEGACY)
    line, _ = ot(result)
    assert line.amount == D("10.00")
    (warning,) = [i for i in result.issues if i.code == "OT_BASE_UNITS_MIXED"]
    assert warning.severity == "WARNING" and warning.details["units"] == {"DAY_WAGE": "DAY"}


def test_custom_salaried_formula_is_not_flagged():
    """A company formula on period amounts only (``SUM_TAG_RATE / 26 / 8``) has nothing to convert: no warning.
    24,000 ÷ 26 ÷ 8 = 115.384615…/h; 2 × 1.5 = 346.15."""
    result = ot_run(structure("GROSS_BASED", fixed("BASIC", "24000.00")),
                    rate_formula='SUM_TAG_RATE("OT_BASE") / 26 / 8')
    line, _ = ot(result)
    assert line.amount == D("346.15") and not codes(result) & UNIT_WARNINGS


def test_bi_weekly_salaried_overtime():
    """Bi-weekly 2-15 Nov (14 calendar days), BASIC FIXED 10,000.00 a period: 10,000 ÷ 14 ÷ 8 = 89.2857…/h;
    2 × 1.5 × 89.2857… = 267.857… → 267.86."""
    start, end = dt.date(2026, 11, 2), dt.date(2026, 11, 15)
    result = ot_run(structure("GROSS_BASED", fixed("BASIC", "10000.00"), {"component": "FIXED_ALLOWANCE",
                                                                         "method": "BALANCING"}, amount="52000.00"),
                    start=start, end=end, day_list=days(start, end),
                    period={"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat(),
                            "frequency": "BI_WEEKLY", "periods_per_year": 26})
    line, rate = ot(result)
    assert rate == D(10000) / 14 / 8 and line.amount == D("267.86")


# --------------------------------------------------------------------------- 2. no silent zero tax


def _pctx(tds: bool = True) -> PackContext:
    pack = in_fixture.PACK
    on = {in_fixture.EPF.switch_key: False, in_fixture.ESI.switch_key: False, in_fixture.PT.switch_key: False,
          in_fixture.TDS.switch_key: tds}
    return PackContext(version=pack.meta, currency="INR", period_start=dt.date(2026, 11, 1),
                       period_end=dt.date(2026, 11, 30), settings=on, employee={"branch_subdivision": "IN-MH"},
                       choice_defaults=pack.choice_defaults())


def _empty_base(lines, tds: bool = True):
    _, found, _, taxable = compute(in_fixture.PACK, _pctx(tds), lines)
    return [i for i in found if i.code == "TAXABLE_BASE_EMPTY"], taxable


def test_tagged_reimbursement_does_not_hide_an_empty_taxable_base():
    """A reimbursement is never taxable income here, so a TAXABLE_INCOME tag on it must not silence the warning:
    BASIC 60,000 + HRA 20,000 untagged → gross 80,000.00, taxable 0."""
    lines = (earning("BASIC", "60000.00", "INR", ("PART_OF_GROSS",)),
             earning("HRA", "20000.00", "INR", ("PART_OF_GROSS",)),
             earning("FUEL", "1500.00", "INR", ("TAXABLE_INCOME",), kind="REIMBURSEMENT", category="REIMBURSEMENT"))
    found, taxable = _empty_base(lines)
    assert taxable == 0 and len(found) == 1
    assert found[0].details["gross"] == "80000.00" and found[0].details["components"] == ["BASIC", "HRA"]


def test_negative_or_zero_net_gross_is_not_reported():
    """A recovery that takes gross earnings to 0 (5,000 − 5,000) is not an untagged positive gross."""
    lines = (earning("BASIC", "5000.00", "INR"), earning("RECOVERY", "-5000.00", "INR"))
    assert _empty_base(lines)[0] == []


def test_empty_taxable_base_surfaces_in_the_engine_result():
    """End to end with the IN fixture pack: TDS ON, Example A (no TAXABLE_INCOME tag in the catalogue) → the
    WARNING TAXABLE_BASE_EMPTY is in the run's issues and IN_TDS is not paid."""
    from app.payroll.packs.in_fixture import TDS

    pack = packs.find_pack("IN", on=dt.date(2026, 11, 30), environment="test")
    data = snapshot(settings=settings(**{TDS.switch_key.replace(".", "__"): True}), pack=packs.pack_snapshot(pack),
                    tax_profile={"regime": "NEW"},
                    employee={"employee_id": 501, "hire_date": "2022-04-01", "country": "IN",
                              "subdivision": "IN-MH", "standard_hours_per_day": "8"})
    result = calculate(data)
    found = [i for i in result.issues if i.code == "TAXABLE_BASE_EMPTY"]
    assert len(found) == 1 and found[0].severity == "WARNING"
    assert not any(line.component == "IN_TDS" for line in result.lines)


# --------------------------------------------------------------------------- 3. per-period pay basis (I-20)


def _period_run(compensation: dict, frequency: str, per_year: int, start: dt.date, end: dt.date, *,
                joined: dt.date | None = None):
    seg_start = joined or start
    data = snapshot(period={"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat(),
                            "frequency": frequency, "periods_per_year": per_year, "tax_year_start": "2026-04-01"},
                    segments=[{"key": "S1", "start": seg_start.isoformat(), "end": end.isoformat(),
                               "assignment_id": 41, "compensation": compensation}],
                    days=days(start, end, segments=[("S1", seg_start, end)], employed_from=joined))
    if joined:
        data["employee"] = {**data["employee"], "hire_date": joined.isoformat()}
    return calculate(data)


def _gross_balancing(amount: str = "52000.00") -> dict:
    return structure("GROSS_BASED", {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}, amount=amount)


@pytest.mark.parametrize("frequency,per_year,start,end,expected", [
    # 52,000 × 12 = 6,24,000 a year
    ("WEEKLY", 52, dt.date(2026, 11, 2), dt.date(2026, 11, 8), "12000.00"),     # ÷ 52
    ("WEEKLY", 53, dt.date(2026, 11, 2), dt.date(2026, 11, 8), "11773.58"),     # ÷ 53 = 11,773.5849…
    ("BI_WEEKLY", 26, dt.date(2026, 11, 2), dt.date(2026, 11, 15), "24000.00"),  # ÷ 26
    ("SEMI_MONTHLY", 24, dt.date(2026, 11, 1), dt.date(2026, 11, 15), "26000.00"),  # ÷ 24
    ("MONTHLY", 12, NOV_START, NOV_END, "52000.00"),                            # ÷ 12
])
def test_gross_based_per_period_by_frequency(frequency, per_year, start, end, expected):
    result = _period_run(_gross_balancing(), frequency, per_year, start, end)
    assert result.totals.gross == D(expected)
    assert not any(i.severity == "BLOCKING" and i.component == "FIXED_ALLOWANCE" for i in result.issues)


@pytest.mark.parametrize("frequency,per_year,start,end,expected", [
    # CTC 12,48,000 a year
    ("WEEKLY", 52, dt.date(2026, 11, 2), dt.date(2026, 11, 8), "24000.00"),
    ("BI_WEEKLY", 26, dt.date(2026, 11, 2), dt.date(2026, 11, 15), "48000.00"),
    ("SEMI_MONTHLY", 24, dt.date(2026, 11, 1), dt.date(2026, 11, 15), "52000.00"),
    ("MONTHLY", 12, NOV_START, NOV_END, "104000.00"),
])
def test_ctc_based_per_period_by_frequency(frequency, per_year, start, end, expected):
    ctc = structure("CTC_BASED", {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}, amount="1248000")
    assert _period_run(ctc, frequency, per_year, start, end).totals.gross == D(expected)


def test_a_year_of_bi_weekly_periods_pays_the_annual_gross():
    """26 × 24,000.00 = 6,24,000.00 = 12 × 52,000.00 (nothing lost or gained over the year)."""
    one = _period_run(_gross_balancing(), "BI_WEEKLY", 26, dt.date(2026, 11, 2), dt.date(2026, 11, 15))
    assert one.totals.gross * 26 == D("52000.00") * 12


def test_bi_weekly_mid_period_joiner_is_prorated_on_the_per_period_amount():
    """Bi-weekly 2-15 Nov (14 days), joined Mon 9 Nov → 7 of 14 days. Per period: BASIC FIXED 10,000.00 and
    FIXED_ALLOWANCE 52,000 × 12 ÷ 26 − 10,000 = 14,000.00; prorated ×7/14: 5,000.00 + 7,000.00 = 12,000.00."""
    s = structure("GROSS_BASED", fixed("BASIC", "10000.00"), {"component": "FIXED_ALLOWANCE", "method": "BALANCING"},
                  amount="52000.00")
    result = _period_run(s, "BI_WEEKLY", 26, dt.date(2026, 11, 2), dt.date(2026, 11, 15), joined=dt.date(2026, 11, 9))
    assert result.line("BASIC").amount == D("5000.00")
    assert result.line("FIXED_ALLOWANCE").amount == D("7000.00")
    assert result.totals.gross == D("12000.00")


def test_weekly_mid_period_joiner_ctc():
    """Weekly 2-8 Nov, CTC 12,48,000 → 24,000.00 a week; joined Thu 5 Nov → 4 of 7 days:
    24,000 × 4 ÷ 7 = 13,714.2857… → 13,714.29."""
    ctc = structure("CTC_BASED", {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}, amount="1248000")
    result = _period_run(ctc, "WEEKLY", 52, dt.date(2026, 11, 2), dt.date(2026, 11, 8), joined=dt.date(2026, 11, 5))
    assert result.totals.gross == D("13714.29")


def test_monthly_mid_period_joiner_is_unchanged():
    """Example A, joined 16 Nov (15 of 30 days): BASIC 25,000.00, HRA 10,000.00, FIXED_ALLOWANCE 27,000 ÷ 2 =
    13,500.00 — as before I-20."""
    s = example_a_structure()
    result = _period_run(s, "MONTHLY", 12, NOV_START, NOV_END, joined=dt.date(2026, 11, 16))
    got = {line.component: line.amount for line in result.lines}
    assert (got["BASIC"], got["HRA"], got["FIXED_ALLOWANCE"]) == (D("25000.00"), D("10000.00"), D("13500.00"))


def test_employer_contributions_inside_ctc_are_converted_to_the_period():
    """ctc_employer_contributions are monthly amounts (snapshot contract). Bi-weekly, CTC 12,00,000,
    BASIC = ctc.period × 50%, employer PF 3,000.00 a month = 3,000 × 12 ÷ 26 = 1,384.6153… a period.
    ctc.period = 46,153.8461…; BASIC 23,076.92; HRA 40% = 9,230.77;
    FIXED_ALLOWANCE = 46,153.8461… − 23,076.92 − 9,230.77 − 1,384.6153… = 12,461.5407… → 12,461.54.
    (Subtracting the monthly 3,000.00 would give 10,846.16 and under-pay CTC by 1,615.38 every period.)"""
    s = example_a_structure()
    s["lines"][0]["params"]["formula"] = "ctc.period * 50%"
    result = _period_run(s, "BI_WEEKLY", 26, dt.date(2026, 11, 2), dt.date(2026, 11, 15))
    got = {line.component: line.amount for line in result.lines}
    assert (got["BASIC"], got["HRA"]) == (D("23076.92"), D("9230.77"))
    assert got["FIXED_ALLOWANCE"] == D("12461.54")


def test_net_based_bi_weekly_reaches_the_target_net():
    """NET_BASED target 20,000.00 a pay period (pay basis unit PER_PAY_PERIOD) on a bi-weekly calendar with no
    deductions: the net of the period is 20,000.00."""
    s = structure("NET_BASED", {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}, amount="20000.00")
    start, end = dt.date(2026, 11, 2), dt.date(2026, 11, 15)
    data = snapshot(period={"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat(),
                            "frequency": "BI_WEEKLY", "periods_per_year": 26},
                    segments=[{"key": "S1", "start": start.isoformat(), "end": end.isoformat(), "assignment_id": 41,
                               "compensation": s}],
                    days=days(start, end), settings=settings(payroll__structures__net_based_enabled=True))
    result = calculate(data)
    assert result.totals.net == D("20000.00")
