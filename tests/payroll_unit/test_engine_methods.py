"""Calculation methods and time-based lines through the whole pipeline (3.3 §3, §9; 3.4 §12).

SLAB, annual FIXED amounts, BY_PAID_HOURS proration, SYSTEM structure lines, the line
rounding of full-period values (BALANCING absorbs it, explanations reproduce amounts) and
the shift allowance with its DAY-grain switch.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.payroll.engine import calculate
from tests.payroll_unit.test_engine_fixtures import (
    NOV_END,
    NOV_START,
    absent,
    catalogue,
    component,
    days,
    example_a_structure,
    overtime_policy,
    settings,
    snapshot,
    use_stages,
)

D = Decimal
IST = "+05:30"


@pytest.fixture(autouse=True)
def _stages(monkeypatch):
    use_stages(monkeypatch)


def nov(day: int) -> dt.date:
    return dt.date(2026, 11, day)


def seg(compensation) -> dict:
    return {"key": "S1", "start": NOV_START.isoformat(), "end": NOV_END.isoformat(), "compensation": compensation}


def gross(amount: str, *lines: dict) -> dict:
    return {"id": 700, "version": 1, "structure_type": "GROSS_BASED", "currency": "INR", "pay_basis_amount": amount,
            "lines": list(lines)}


def amounts(result) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for line in result.lines:
        found[line.component] = found.get(line.component, D(0)) + line.amount
    return found


def codes(result) -> set[str]:
    return {i.code for i in result.issues}


# --------------------------------------------------------------------------- methods


def test_slab_method_is_progressive_over_the_earned_base():
    """SLAB 0% up to 20,000 then 10%: BASIC 50,000 → 3,000.00; with 2 LOP days the earned 46,666.67 → 2,666.67."""
    table = {"code": "ALLOWANCE_SLAB", "kind": "SLAB", "key_currency": "INR",
             "rows": [{"lower": "0", "upper": "20000", "rate": "0"}, {"lower": "20000", "upper": None, "rate": "0.10"}]}
    structure = gross("50000.00", {"component": "BASIC", "method": "FIXED", "amount": "50000.00", "order": 1},
                      {"component": "SLAB_ALLOWANCE", "method": "SLAB",
                       "params": {"table": "ALLOWANCE_SLAB", "base": "BASIC"}, "order": 2})
    extra = component("SLAB_ALLOWANCE", category="FIXED_ALLOWANCE")
    full = calculate(snapshot(segments=[seg(structure)], tables=[table], components=catalogue(extra)))
    assert amounts(full)["SLAB_ALLOWANCE"] == D("3000.00") and full.ok
    assert full.line("SLAB_ALLOWANCE").trace["method"] == "SLAB"
    lop = calculate(snapshot(segments=[seg(structure)], tables=[table], components=catalogue(extra),
                             days=days(per_day=absent(nov(12), nov(13)))))
    assert amounts(lop)["BASIC"] == D("46666.67") and amounts(lop)["SLAB_ALLOWANCE"] == D("2666.67")


def test_slab_with_unknown_table_is_blocking_never_zero():
    structure = gross("50000.00", {"component": "BASIC", "method": "FIXED", "amount": "50000.00"},
                      {"component": "SLAB_ALLOWANCE", "method": "SLAB", "params": {"table": "MISSING", "base": "BASIC"}})
    result = calculate(snapshot(segments=[seg(structure)],
                                components=catalogue(component("SLAB_ALLOWANCE", category="FIXED_ALLOWANCE"))))
    assert "SLAB_ALLOWANCE" not in amounts(result) and not result.ok
    assert [i for i in result.blocking if i.component == "SLAB_ALLOWANCE"][0].code == "FORMULA_ERROR"


def test_fixed_annual_amount_is_divided_by_periods_per_year_then_prorated():
    """1,00,000 a year ÷ 12 = 8,333.33 a month; 2 LOP days → 8,333.33 × 28 ÷ 30 = 7,777.77."""
    structure = gross("8333.33", {"component": "BASIC", "method": "FIXED", "annual_amount": "100000"})
    full = calculate(snapshot(segments=[seg(structure)]))
    assert full.line("BASIC").amount == D("8333.33") and full.line("BASIC").rate == D("8333.33")
    lop = calculate(snapshot(segments=[seg(structure)], days=days(per_day=absent(nov(12), nov(13)))))
    line = lop.line("BASIC")
    assert line.amount == D("7777.77")
    assert line.explanation == "8,333.33 × 28 ÷ 30 (2 LOP days: 12–13 Nov)"


def test_balancing_absorbs_line_rounding_so_the_structure_adds_up():
    """Gross 96,999.99: BASIC 48,499.995 → 48,500.00, HRA 19,400.00, so FIXED_ALLOWANCE takes 29,099.99."""
    structure = example_a_structure()
    structure.update(structure_type="GROSS_BASED", pay_basis_amount="96999.99", ctc_employer_contributions={})
    result = calculate(snapshot(segments=[seg(structure)]))
    got = amounts(result)
    assert (got["BASIC"], got["HRA"], got["FIXED_ALLOWANCE"]) == (D("48500.00"), D("19400.00"), D("29099.99"))
    assert result.totals.gross == D("96999.99")


def test_prorated_explanations_reproduce_their_amounts():
    """"R × u ÷ N" on every prorated line, rounded HALF_UP, is exactly the line amount (3.5 §8.3)."""
    structure = example_a_structure("1234567")
    result = calculate(snapshot(segments=[seg(structure)], days=days(per_day=absent(nov(3), nov(17), nov(18)))))
    pattern = re.compile(r"^([\d,]+\.\d\d) × ([\d.]+) ÷ ([\d.]+)")
    checked = 0
    for line in result.lines:
        found = pattern.match(line.explanation)
        if not found:
            continue
        rate, units, basis = (D(g.replace(",", "")) for g in found.groups())
        assert (rate * units / basis).quantize(D("0.01"), rounding=ROUND_HALF_UP) == line.amount, line.explanation
        checked += 1
    assert checked == 3


def test_by_paid_hours_prorates_by_payable_over_standard_hours():
    """25 working days × 8 h = 200 standard hours; one 4-hour day → 196 payable: 40,000 × 196 ÷ 200 = 39,200.00."""
    per_day = {}
    for day in range(1, 31):
        if nov(day).weekday() != 6:
            seconds = 14400 if day == 10 else 28800
            per_day[nov(day)] = {"attendance": {"status": "PRESENT", "session_id": f"s{day}", "worked_seconds": seconds}}
    structure = gross("40000.00", {"component": "WAGE", "method": "FIXED", "amount": "40000.00"})
    result = calculate(snapshot(segments=[seg(structure)], days=days(per_day=per_day),
                                settings=settings(payroll__attendance__enabled=True),
                                components=catalogue(component("WAGE", tags=["LOP_APPLICABLE"],
                                                               proration="BY_PAID_HOURS"))))
    line = result.line("WAGE")
    assert line.amount == D("39200.00") and line.units == D("196")
    assert line.explanation == "40,000.00 × 196 ÷ 200 hours"
    assert line.trace["proration"]["basis"] == "HOURS"


def test_system_structure_line_keeps_the_engine_line():
    """A SYSTEM line in the structure has no formula: the engine's own overtime line is produced."""
    structure = example_a_structure()
    structure["lines"].append({"component": "OT_WORKDAY", "method": "SYSTEM", "order": 9})
    data = snapshot(segments=[seg(structure)], settings=settings(payroll__overtime__enabled=True),
                    policies={"OVERTIME_POLICY": overtime_policy()},
                    overtime_entries=[{"entry_id": "a", "date": "2026-11-05", "minutes": 240}])
    line = calculate(data).line("OT_WORKDAY")
    assert line.amount == D("1442.31")  # 50,000 ÷ 26 ÷ 8 × 1.5 × 4
    assert line.explanation.startswith("50,000.00 ÷ 26 ÷ 8 = 240.3846…; ")


# --------------------------------------------------------------------------- shift allowance (3.4 §12)


def night_shift(day: int) -> dict:
    when = nov(day)
    return {"date": when.isoformat(), "start": f"{when.isoformat()}T22:00:00{IST}",
            "end": f"{(when + dt.timedelta(days=1)).isoformat()}T06:00:00{IST}", "ref": f"shift-{day}"}


SHIFT_POLICY = {"SHIFT_ALLOWANCE_POLICY": {"id": 61, "version": 1,
                                           "fields": {"allowance_type": "PER_SHIFT_AMOUNT", "amount": "200"}}}


def test_shift_allowance_per_qualifying_night_shift_with_day_grain_switch_ec_cfg_08():
    """Allowance ON 1-15 Nov: the night shifts of 3 and 10 Nov are paid (2 × 200), the one of 20 Nov is not."""
    data = snapshot(settings=settings(payroll__shift_allowance__enabled=True), policies=SHIFT_POLICY,
                    switch_ranges={"payroll.shift_allowance.enabled": [["2026-11-01", "2026-11-15"]]},
                    shifts=[night_shift(3), night_shift(10), night_shift(20)])
    result = calculate(data)
    line = result.line("SHIFT_ALLOWANCE")
    assert line.amount == D("400.00") and line.kind == "EARNING"
    assert result.totals.gross == D("97400.00") and line.explanation


def test_shift_allowance_switch_off_pays_nothing():
    data = snapshot(policies=SHIFT_POLICY, shifts=[night_shift(3)],
                    settings=settings(payroll__shift_allowance__enabled=False))
    result = calculate(data)
    assert "SHIFT_ALLOWANCE" not in amounts(result) and result.totals.gross == D("97000.00")


def test_shift_allowance_without_an_amount_is_a_configuration_error():
    policy = {"SHIFT_ALLOWANCE_POLICY": {"fields": {"allowance_type": "PER_SHIFT_AMOUNT"}}}
    result = calculate(snapshot(settings=settings(payroll__shift_allowance__enabled=True), policies=policy,
                                shifts=[night_shift(3)]))
    assert "CONFIG_ERROR" in codes(result) and not result.ok and "SHIFT_ALLOWANCE" not in amounts(result)
