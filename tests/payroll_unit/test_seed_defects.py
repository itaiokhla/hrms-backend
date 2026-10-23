"""Defects found by the three-company seed (pure: engine, formula language, IN fixture pack).

1. The default overtime rate (OVERTIME_POLICY.rate_formula) divided an hourly OT_BASE rate by days × hours
   again: 2 h at 48.00/h paid cents. ``SUM_TAG_HOURLY("OT_BASE")`` converts each rate by its unit; a
   salaried employee's rate is exactly 3.1 §5.5's ``SUM_TAG_RATE("OT_BASE") / period.basis_days /
   emp.standard_hours_per_day``. A formula that cannot convert the units of the OT base raises a WARNING.
2. IN fixture pack: withholding ON, gross > 0 and no earning tagged TAXABLE_INCOME → TAXABLE_BASE_EMPTY.
3. Decision I-20: the balancing total of a period is the pay basis ÷ periods per year (``ctc.period``), so a
   bi-weekly group no longer pays the monthly gross every two weeks.

Every expectation is computed by hand in the test's docstring or comment.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.payroll import formula as fm
from app.payroll.engine import calculate
from app.payroll.engine.earnings import rate_unit
from app.payroll.engine.snapshot import StructureLine
from app.payroll.engine.statutory import compute
from app.payroll.packs import in_fixture
from app.payroll.packs.base import PackContext, earning
from app.payroll.policy_types import DEFAULT_OT_RATE_FORMULA, default_fields
from tests.payroll_unit.test_engine_fixtures import (
    MULTIPLIERS,
    NOV_END,
    NOV_START,
    catalogue,
    component,
    days,
    example_a_structure,
    settings,
    snapshot,
)

D = Decimal
LEGACY_OT_FORMULA = 'SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day'
TWO_HOURS = [{"entry_id": "ot-1", "date": "2026-11-05", "minutes": 120}]  # Thursday, a working day: 1.5x


def ot_policy(rate_formula: str | None = None) -> dict:
    fields = {"buckets": MULTIPLIERS, "min_minutes": 30, "rounding": {"increment_minutes": 15, "mode": "DOWN"}}
    if rate_formula is not None:
        fields["rate_formula"] = rate_formula
    return {"id": 31, "version": 1, "policy_hash": "ot-hash", "fields": fields}


def segment(compensation: dict, start: dt.date = NOV_START, end: dt.date = NOV_END) -> list[dict]:
    return [{"key": "S1", "start": start.isoformat(), "end": end.isoformat(), "assignment_id": 41,
             "compensation": compensation}]


def present_all_month() -> dict:
    """Every Monday-Saturday of November 2026 present for 8 h: 25 days, 200 worked (= payable) hours."""
    result = {}
    when = NOV_START
    while when <= NOV_END:
        if when.weekday() != 6:
            result[when] = {"attendance": {"status": "PRESENT", "worked_seconds": 28800}}
        when += dt.timedelta(days=1)
    return result


def run(compensation: dict, *, rate_formula: str | None = None, extra=(), attendance: bool = False, **changes):
    data = snapshot(
        segments=segment(compensation),
        components=catalogue(*extra),
        settings=settings(payroll__overtime__enabled=True, payroll__attendance__enabled=attendance),
        policies={"OVERTIME_POLICY": ot_policy(rate_formula)},
        overtime_entries=TWO_HOURS,
        days=days(per_day=present_all_month()) if attendance else days(),
        **changes,
    )
    return calculate(data)


def ot_line(result):
    lines = [line for line in result.lines if line.component == "OT_WORKDAY"]
    assert len(lines) == 1, [(i.code, i.message) for i in result.exceptions]
    return lines[0]


def issues(result, code):
    return [i for i in (*result.exceptions, *result.flags) if i.code == code]


HOURLY_WAGE = component("HOURLY_WAGE", category="BASE", tags=["OT_BASE", "PART_OF_GROSS"], calc_method="RATE_X_UNITS",
                        sort_order=10)
DAY_WAGE = component("DAY_WAGE", category="BASE", tags=["OT_BASE", "PART_OF_GROSS"], calc_method="RATE_X_UNITS",
                     sort_order=10)
PIECE_RATE = component("PIECE_RATE", category="BASE", tags=["OT_BASE", "PART_OF_GROSS"], calc_method="RATE_X_UNITS",
                       sort_order=12)
HOURLY_TOP_UP = component("HOURLY_TOP_UP", category="BASE", tags=["OT_BASE", "PART_OF_GROSS"],
                          calc_method="RATE_X_UNITS", sort_order=11)


def hourly(rate: str = "48.00", units: str = "att.payable_hours") -> dict:
    return {"id": 901, "version": 1, "structure_type": "HOURLY", "currency": "INR", "pay_basis_amount": rate,
            "lines": [{"component": "HOURLY_WAGE", "method": "RATE_X_UNITS", "params": {"rate": rate, "units": units}}]}


def daily(rate: str = "800.00") -> dict:
    return {"id": 902, "version": 1, "structure_type": "DAILY", "currency": "INR", "pay_basis_amount": rate,
            "lines": [{"component": "DAY_WAGE", "method": "RATE_X_UNITS",
                       "params": {"rate": rate, "units": "att.paid_days"}}]}


def gross(*lines: dict, amount: str | None = None) -> dict:
    return {"id": 903, "version": 1, "structure_type": "GROSS_BASED", "currency": "INR", "pay_basis_amount": amount,
            "lines": list(lines)}


# --------------------------------------------------------------------------- 1. overtime hourly rate


def test_default_is_the_unit_aware_formula():
    assert DEFAULT_OT_RATE_FORMULA == 'SUM_TAG_HOURLY("OT_BASE")'
    assert default_fields("OVERTIME_POLICY")["rate_formula"] == DEFAULT_OT_RATE_FORMULA
    fm.parse(DEFAULT_OT_RATE_FORMULA)  # the safe language; no eval


def test_salaried_monthly_overtime_is_unchanged():
    """Example A, BASIC 50,000.00 (OT_BASE), November (30 calendar days), 8 h: 50,000 ÷ 30 ÷ 8 = 208.3333…/h;
    2 h × 1.5 = 625.00. The legacy 3.1 §5.5 formula gives the same rate to the last digit."""
    new = run(example_a_structure())
    old = run(example_a_structure(), rate_formula=LEGACY_OT_FORMULA)
    assert ot_line(new).amount == D("625.00") == ot_line(old).amount
    assert ot_line(new).trace["ot_hourly_rate"]["rate"] == ot_line(old).trace["ot_hourly_rate"]["rate"]
    assert D(ot_line(new).trace["ot_hourly_rate"]["rate"]) == D(50000) / 30 / 8
    assert not issues(new, "OT_BASE_UNITS_MIXED") and not issues(old, "OT_BASE_UNITS_MIXED")


def test_hourly_worker_overtime_uses_the_hourly_rate_as_it_is():
    """48.00 per payable hour (200 h in November = 9,600.00); 2 h overtime × 1.5 × 48.00 = 144.00."""
    result = run(hourly(), extra=[HOURLY_WAGE], attendance=True)
    assert result.line("HOURLY_WAGE").amount == D("9600.00")
    assert ot_line(result).amount == D("144.00")
    assert D(ot_line(result).trace["ot_hourly_rate"]["rate"]) == D("48.00")
    assert not issues(result, "OT_BASE_UNITS_MIXED") and not issues(result, "OT_BASE_UNIT_UNKNOWN")


def test_daily_worker_overtime_divides_the_day_rate_by_standard_hours():
    """800.00 per paid day (30 days = 24,000.00); 800 ÷ 8 h = 100.00/h; 2 h × 1.5 × 100.00 = 300.00."""
    result = run(daily(), extra=[DAY_WAGE])
    assert result.line("DAY_WAGE").amount == D("24000.00")
    assert D(ot_line(result).trace["ot_hourly_rate"]["rate"]) == D("100")
    assert ot_line(result).amount == D("300.00")


def test_period_and_hourly_components_in_one_ot_base_are_each_converted():
    """BASIC 24,000.00 a month → 24,000 ÷ 30 ÷ 8 = 100.00/h, plus HOURLY_TOP_UP 10.00/h = 110.00/h;
    2 h × 1.5 × 110.00 = 330.00."""
    structure = gross({"component": "BASIC", "method": "FIXED", "amount": "24000.00"},
                      {"component": "HOURLY_TOP_UP", "method": "RATE_X_UNITS",
                       "params": {"rate": "10.00", "units": "COALESCE(att.payable_hours, 0)"}})
    result = run(structure, extra=[HOURLY_TOP_UP], attendance=True)
    assert D(ot_line(result).trace["ot_hourly_rate"]["rate"]) == D("110")
    assert ot_line(result).amount == D("330.00")
    assert not issues(result, "OT_BASE_UNITS_MIXED")


def test_legacy_formula_on_an_hourly_ot_base_is_flagged():
    """The seed's defect: 48.00 ÷ 30 days ÷ 8 h = 0.20/h, 2 h × 1.5 = 0.60. The formula is the company's, so the
    engine pays what it says, but the review shows a WARNING naming the unit problem."""
    result = run(hourly(), extra=[HOURLY_WAGE], attendance=True, rate_formula=LEGACY_OT_FORMULA)
    assert ot_line(result).amount == D("0.60")
    (warning,) = issues(result, "OT_BASE_UNITS_MIXED")
    assert warning.severity == "WARNING"
    assert warning.details["units"] == {"HOURLY_WAGE": "HOUR"} and warning.details["tag"] == "OT_BASE"
    assert 'SUM_TAG_HOURLY("OT_BASE")' in warning.message and "period.basis_days" in warning.message


def test_sum_tag_rate_mixing_units_without_basis_days_is_flagged():
    """SUM_TAG_RATE adds 24,000.00 (a month) and 10.00 (an hour) as if they were one unit."""
    structure = gross({"component": "BASIC", "method": "FIXED", "amount": "24000.00"},
                      {"component": "HOURLY_TOP_UP", "method": "RATE_X_UNITS",
                       "params": {"rate": "10.00", "units": "att.payable_hours"}})
    result = run(structure, extra=[HOURLY_TOP_UP], attendance=True, rate_formula='SUM_TAG_RATE("OT_BASE") / 208')
    (warning,) = issues(result, "OT_BASE_UNITS_MIXED")
    assert warning.details["units"] == {"BASIC": "PERIOD", "HOURLY_TOP_UP": "HOUR"}


def test_seed_two_tag_formula_is_not_flagged():
    """The seed's workaround divides only OT_BASE by the basis: 24,000 ÷ 30 ÷ 8 + 10.00 (OT_HOURLY_BASE, per
    hour) = 110.00/h; 2 h × 1.5 = 330.00, and no unit warning (the hourly tag is not divided by days)."""
    extra = component("HOURLY_EXTRA", category="BASE", tags=["OT_HOURLY_BASE", "PART_OF_GROSS"],
                      calc_method="RATE_X_UNITS", sort_order=11)
    structure = gross({"component": "BASIC", "method": "FIXED", "amount": "24000.00"},
                      {"component": "HOURLY_EXTRA", "method": "RATE_X_UNITS",
                       "params": {"rate": "10.00", "units": "att.payable_hours"}})
    formula = LEGACY_OT_FORMULA + ' + SUM_TAG_RATE("OT_HOURLY_BASE")'
    result = run(structure, extra=[extra], attendance=True, rate_formula=formula)
    assert ot_line(result).amount == D("330.00")
    assert not issues(result, "OT_BASE_UNITS_MIXED") and not issues(result, "OT_BASE_UNIT_UNKNOWN")


def test_unconvertible_unit_is_left_out_with_a_warning():
    """A piece rate (units from wage inputs) has no hourly equivalent: it is left out of the rate, which stays
    24,000 ÷ 30 ÷ 8 = 100.00/h (2 h × 1.5 = 300.00), and the review shows OT_BASE_UNIT_UNKNOWN."""
    structure = gross({"component": "BASIC", "method": "FIXED", "amount": "24000.00"},
                      {"component": "PIECE_RATE", "method": "RATE_X_UNITS",
                       "params": {"rate": "5.00", "units": 'UNITS("PIECE_RATE")'}})
    result = run(structure, extra=[PIECE_RATE])
    assert ot_line(result).amount == D("300.00")
    (warning,) = issues(result, "OT_BASE_UNIT_UNKNOWN")
    assert warning.severity == "WARNING" and warning.details["left_out"] == ["PIECE_RATE"]
    assert "left out" in " ".join(ot_line(result).trace["ot_hourly_rate"]["steps"])


@pytest.mark.parametrize("units,expected", [
    ("att.payable_hours", "HOUR"), ("COALESCE(att.worked_hours, 0)", "HOUR"), ("ot.hours_workday", "HOUR"),
    ("att.paid_days", "DAY"), ("MIN(att.present_days, 26)", "DAY"),
    ('UNITS("PIECES")', "OTHER"), ("task.units", "OTHER"), ("8", "OTHER"),
    ("att.paid_days * att.payable_hours", "OTHER"), ("BASIC", "OTHER"),
])
def test_rate_unit_is_read_from_the_units_expression(units, expected):
    line = StructureLine.from_dict({"component": "X", "method": "RATE_X_UNITS", "params": {"rate": "1",
                                                                                         "units": units}}, "l")
    assert rate_unit(line) == expected


def test_non_rate_x_units_lines_are_period_amounts():
    for line in ({"component": "X", "method": "FIXED", "amount": "1"},
                 {"component": "X", "method": "FORMULA", "params": {"formula": "att.payable_hours * 2"}}):
        assert rate_unit(StructureLine.from_dict(line, "l")) == "PERIOD"


def _ctx(rate_units=None, **variables):
    values = {"period.basis_days": D(30), "emp.standard_hours_per_day": D(8), **variables}
    return fm.EvalContext.build("INR", rates={"BASIC": fm.MoneyValue(D("24000.00"), "INR"),
                                              "HOURLY": fm.MoneyValue(D("48.00"), "INR"),
                                              "DAILY": fm.MoneyValue(D("800.00"), "INR")},
                                tags={"OT_BASE": ["BASIC", "HOURLY", "DAILY"]}, variables=values,
                                rate_units=rate_units)


def test_sum_tag_hourly_formula_semantics():
    """24,000 ÷ 30 ÷ 8 = 100 + 800 ÷ 8 = 100 + 48 = 248.00 per hour."""
    result = fm.evaluate(fm.parse('SUM_TAG_HOURLY("OT_BASE")'),
                         _ctx({"BASIC": "PERIOD", "HOURLY": "HOUR", "DAILY": "DAY"}))
    assert result.error is None and result.value == fm.MoneyValue(D("248"), "INR")


def test_sum_tag_hourly_without_units_equals_the_legacy_formula():
    """Formula tester (no units known): every rate is a period amount, so (24,000 + 48 + 800) ÷ 30 ÷ 8."""
    new = fm.evaluate(fm.parse('SUM_TAG_HOURLY("OT_BASE")'), _ctx())
    old = fm.evaluate(fm.parse(LEGACY_OT_FORMULA), _ctx())
    assert new.error is None and new.value == old.value


def test_sum_tag_hourly_needs_the_basis_and_hours_only_for_period_rates():
    hours_only = fm.evaluate(fm.parse('SUM_TAG_HOURLY("OT_BASE")'),
                             _ctx({"BASIC": "HOUR", "HOURLY": "HOUR", "DAILY": "HOUR"}, **{"period.basis_days": None}))
    assert hours_only.error is None and hours_only.value == fm.MoneyValue(D("24848.00"), "INR")
    missing = fm.evaluate(fm.parse('SUM_TAG_HOURLY("OT_BASE")'), _ctx(**{"period.basis_days": None}))
    assert missing.error is not None and missing.error.code == fm.ErrorCode.NULL_VALUE
    zero = fm.evaluate(fm.parse('SUM_TAG_HOURLY("OT_BASE")'), _ctx(**{"emp.standard_hours_per_day": D(0)}))
    assert zero.error is not None and zero.error.code == fm.ErrorCode.DIVISION_BY_ZERO


def test_sum_tag_hourly_is_checked_like_sum_tag_rate():
    env = fm.TypeEnv("INR", components={"BASIC": fm.ComponentSig("INR")}, tags={"OT_BASE": ["BASIC"]})
    analysis = fm.check('SUM_TAG_HOURLY("OT_BASE")', env)
    assert analysis.dependencies == frozenset({"BASIC"}) and analysis.tags == frozenset({"OT_BASE"})
    with pytest.raises(fm.FormulaError):
        fm.check("SUM_TAG_HOURLY(OT_BASE)", env)  # tags are quoted codes
    assert "SUM_TAG_HOURLY" in fm.FUNCTIONS


# --------------------------------------------------------------------------- 2. IN fixture: empty taxable base


def _in_context(*, tds: bool = True) -> PackContext:
    pack = in_fixture.PACK
    on = {in_fixture.EPF.switch_key: False, in_fixture.ESI.switch_key: False, in_fixture.PT.switch_key: False,
          in_fixture.TDS.switch_key: tds}
    return PackContext(version=pack.meta, currency="INR", period_start=dt.date(2026, 11, 1),
                       period_end=dt.date(2026, 11, 30), settings=on, employee={"branch_subdivision": "IN-MH"},
                       choice_defaults=pack.choice_defaults())


def _tds_issues(lines, *, tds: bool = True):
    _, found, _, taxable = compute(in_fixture.PACK, _in_context(tds=tds), lines)
    return [i for i in found if i.code == "TAXABLE_BASE_EMPTY"], taxable


def test_withholding_on_without_taxable_tag_is_reported():
    """BASIC 1,00,000.00 with no TAXABLE_INCOME tag: taxable income 0 is not silent."""
    found, taxable = _tds_issues((earning("BASIC", "100000.00", "INR", ("PART_OF_GROSS",)),))
    assert taxable == 0
    (issue,) = found
    assert issue.severity == "WARNING" and issue.component == "IN_TDS"
    assert issue.details["gross"] == "100000.00" and issue.details["components"] == ["BASIC"]
    assert "TAXABLE_INCOME" in issue.message


def test_tagged_earning_zero_gross_or_scheme_off_is_not_reported():
    tagged = (earning("BASIC", "100000.00", "INR", ("TAXABLE_INCOME",)),
              earning("BONUS", "5000.00", "INR", ("PART_OF_GROSS",), category="BONUS"))
    assert _tds_issues(tagged)[0] == []
    assert _tds_issues((earning("BASIC", "0.00", "INR"),))[0] == []
    assert _tds_issues((earning("BASIC", "100000.00", "INR"),), tds=False)[0] == []


# --------------------------------------------------------------------------- 3. pay basis per pay period (I-20)

BW_START, BW_END = dt.date(2026, 11, 2), dt.date(2026, 11, 15)


def short_period(compensation: dict, frequency: str, per_year: int, start=BW_START, end=BW_END, **changes):
    data = snapshot(period={"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat(),
                            "frequency": frequency, "periods_per_year": per_year},
                    segments=segment(compensation, start, end), days=days(start, end), **changes)
    return calculate(data)


def amounts(result):
    found = {}
    for line in result.lines:
        found[line.component] = found.get(line.component, D(0)) + line.amount
    return found


def test_gross_based_bi_weekly_balancing_is_the_per_period_gross():
    """Monthly gross 52,000.00 on a bi-weekly calendar: 52,000 × 12 ÷ 26 = 24,000.00 a period. BASIC is a FIXED
    per-period 10,000.00; FIXED_ALLOWANCE balances to 14,000.00 (before I-20: 42,000.00, the monthly gross)."""
    structure = gross({"component": "BASIC", "method": "FIXED", "amount": "10000.00"},
                      {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}, amount="52000.00")
    result = short_period(structure, "BI_WEEKLY", 26)
    got = amounts(result)
    assert (got["BASIC"], got["FIXED_ALLOWANCE"]) == (D("10000.00"), D("14000.00"))
    assert result.totals.gross == D("24000.00")
    assert "ctc.period" in result.line("FIXED_ALLOWANCE").trace["formula"]


def test_bi_weekly_27_period_year_divides_by_27():
    """52,000 × 12 ÷ 27 = 23,111.11 a period (3.5 §2.1: the actual number of periods)."""
    structure = gross({"component": "FIXED_ALLOWANCE", "method": "BALANCING"}, amount="52000.00")
    assert short_period(structure, "BI_WEEKLY", 27).totals.gross == D("23111.11")


def test_ctc_based_weekly_period_uses_annual_divided_by_52():
    """CTC 12,48,000 a year on a weekly calendar: 12,48,000 ÷ 52 = 24,000.00; BASIC = ctc.period × 50% =
    12,000.00, HRA 40% = 4,800.00, FIXED_ALLOWANCE = 24,000 − 12,000 − 4,800 = 7,200.00."""
    structure = example_a_structure("1248000", employer_pf="0")
    structure["ctc_employer_contributions"] = {}
    structure["lines"][0]["params"]["formula"] = "ctc.period * 50%"
    start, end = dt.date(2026, 11, 2), dt.date(2026, 11, 8)
    result = short_period(structure, "WEEKLY", 52, start, end)
    got = amounts(result)
    assert (got["BASIC"], got["HRA"], got["FIXED_ALLOWANCE"]) == (D("12000.00"), D("4800.00"), D("7200.00"))
    assert not issues(result, "CTC_MONTHLY_ON_NON_MONTHLY_PERIOD")


def test_ctc_monthly_formula_on_a_bi_weekly_period_is_flagged():
    """Example A (BASIC = ctc.monthly × 50%) on a bi-weekly calendar pays the monthly 50,000.00 every period; the
    balancing total 12,00,000 ÷ 26 = 46,153.85 is then below BASIC + HRA, so the run fails closed (EC-CMP-04)."""
    structure = example_a_structure()
    structure["ctc_employer_contributions"] = {}
    result = short_period(structure, "BI_WEEKLY", 26)
    (warning,) = issues(result, "CTC_MONTHLY_ON_NON_MONTHLY_PERIOD")
    assert warning.severity == "WARNING" and warning.component == "BASIC"
    assert warning.details["periods_per_year"] == 26 and "ctc.period" in warning.message
    assert any(i.component == "FIXED_ALLOWANCE" and i.severity == "BLOCKING" for i in result.exceptions)


def test_monthly_calendar_keeps_ctc_monthly_and_the_same_amounts():
    """Example A in November: FIXED_ALLOWANCE = 1,00,000 − 50,000 − 20,000 − 3,000 = 27,000.00 as before; the
    balancing formula still reads ctc.monthly, which equals ctc.period on a monthly calendar."""
    result = calculate(snapshot())
    assert amounts(result)["FIXED_ALLOWANCE"] == D("27000.00")
    assert "ctc.monthly" in result.line("FIXED_ALLOWANCE").trace["formula"]
    assert not issues(result, "CTC_MONTHLY_ON_NON_MONTHLY_PERIOD")
