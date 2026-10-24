"""Incentive plans (3.1 §5.9 INCENTIVE_PLAN, decision I-22): payout windows, targets, measures and the engine stage.

Every expected amount is worked out by hand in the comments. Snapshot: November 2026, INR, tax year from April,
Example A (CTC 12,00,000: BASIC 50,000.00, HRA 20,000.00, FIXED_ALLOWANCE 27,000.00 after the 3,000.00 employer
PF inside CTC; gross 97,000.00).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.payroll import formula as fm
from app.payroll import incentives as inc
from app.payroll.engine import calculate
from app.payroll.engine.snapshot import Snapshot, SnapshotError
from app.payroll.policy_types import PolicyValidationError, validate_fields
from tests.payroll_unit.test_engine_fixtures import (
    NOV_END,
    NOV_START,
    catalogue,
    component,
    pack_stage,
    settings,
    snapshot,
    use_stages,
)

D = Decimal
d = dt.date
GROSS = D("97000.00")
INCENTIVE = component("INCENTIVE", category="VARIABLE", tags=["PART_OF_GROSS"], calc_method="INPUT", sort_order=62,
                      version_id=162, version=1)
TIERS = {"code": "INC_TIERS", "kind": "BAND", "rows": [
    {"upper": "80", "value": "0"}, {"lower": "80", "upper": "100", "value": "5000"},
    {"lower": "100", "upper": "120", "value": "10000"}, {"lower": "120", "value": "15000"}]}
COMMISSION = {"code": "INC_COMM", "kind": "SLAB", "rows": [
    {"lower": "0", "upper": "100", "rate": "0"}, {"lower": "100", "upper": "300", "rate": "0.05"},
    {"lower": "300", "rate": "0.08"}]}


@pytest.fixture(autouse=True)
def _test_stages(monkeypatch):
    use_stages(monkeypatch, statutory=pack_stage(epf="0.00", pt="0.00"))


def plan(formula: str, **fields) -> dict:
    return {"id": 77, "version": 2, "policy_hash": "plan-hash",
            "fields": {"measure_source": "IMPORT", "formula": formula, "targets": [], "payout_frequency": "MONTHLY",
                       "cap": None, "clawback_window_days": 0, "component": "INCENTIVE", **fields}}


def measure(ident: int, units: str, **extra) -> dict:
    return {"id": ident, "component": "INCENTIVE", "units": units, "currency": "INR", "source": "INCENTIVE_PLAN",
            "content_hash": f"h{ident}", **extra}


def window(target: str | None = "100", source: str | None = "EMPLOYEE", start=NOV_START, end=NOV_END) -> dict:
    return {"window_start": start.isoformat(), "window_end": end.isoformat(), "target": target,
            "target_source": source if target is not None else None}


def incentive_snapshot(formula: str, measures, *, target: str | None = "100", on: bool = True, block=True,
                       tables=(), extra_inputs=(), **plan_fields) -> dict:
    data = snapshot(settings=settings(payroll__incentives__enabled=on),
                    components=catalogue(INCENTIVE),
                    policies={"INCENTIVE_PLAN": plan(formula, **plan_fields)},
                    wage_inputs=[*measures, *extra_inputs], tables=list(tables))
    if block:
        data["incentive"] = window(target)
    return data


def amounts(result) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for line in result.lines:
        found[line.component] = found.get(line.component, D(0)) + line.amount
    return found


def codes(result, severity: str | None = None) -> set[str]:
    return {i.code for i in result.issues if severity is None or i.severity == severity}


# --------------------------------------------------------------------------- payout windows


@pytest.mark.parametrize(("frequency", "start", "end", "year_start", "expected"), [
    ("MONTHLY", d(2026, 10, 1), d(2026, 10, 31), None, (d(2026, 10, 1), d(2026, 10, 31))),
    # Calendar quarters (no tax-year start): October does not end Q4, December does.
    ("QUARTERLY", d(2026, 10, 1), d(2026, 10, 31), None, None),
    ("QUARTERLY", d(2026, 12, 1), d(2026, 12, 31), None, (d(2026, 10, 1), d(2026, 12, 31))),
    # Tax year from April: quarters Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar.
    ("QUARTERLY", d(2026, 6, 1), d(2026, 6, 30), d(2026, 4, 1), (d(2026, 4, 1), d(2026, 6, 30))),
    ("QUARTERLY", d(2027, 3, 1), d(2027, 3, 31), d(2026, 4, 1), (d(2027, 1, 1), d(2027, 3, 31))),
    # A weekly period straddling the quarter end pays the quarter that ends inside it.
    ("QUARTERLY", d(2027, 3, 28), d(2027, 4, 3), None, (d(2027, 1, 1), d(2027, 3, 31))),
    ("QUARTERLY", d(2027, 4, 4), d(2027, 4, 10), None, None),
    # A UK tax year (6 April) counts whole months from April: the year window is April-March.
    ("ANNUAL", d(2027, 3, 1), d(2027, 3, 31), d(2026, 4, 6), (d(2026, 4, 1), d(2027, 3, 31))),
    ("ANNUAL", d(2026, 12, 1), d(2026, 12, 31), d(2026, 4, 6), None),
    ("ANNUAL", d(2026, 12, 1), d(2026, 12, 31), None, (d(2026, 1, 1), d(2026, 12, 31))),
    # Semi-monthly: only the second half ends the month.
    ("MONTHLY", d(2027, 1, 1), d(2027, 1, 15), None, None),
    ("MONTHLY", d(2027, 1, 16), d(2027, 1, 31), None, (d(2027, 1, 1), d(2027, 1, 31))),
    ("MONTHLY", d(2028, 2, 1), d(2028, 2, 29), None, (d(2028, 2, 1), d(2028, 2, 29))),
])
def test_payout_window(frequency, start, end, year_start, expected):
    assert inc.payout_window(frequency, start, end, year_start) == expected


def test_payout_window_rejects_an_unknown_frequency():
    with pytest.raises(inc.IncentiveError) as error:
        inc.payout_window("WEEKLY", d(2026, 1, 1), d(2026, 1, 31))
    assert error.value.code == "INCENTIVE_PLAN_INVALID"


def test_measure_belongs_to_the_window_only_inside_it():
    assert inc.measure_in_window(d(2026, 10, 1), d(2026, 10, 31), d(2026, 10, 1), d(2026, 12, 31))
    assert not inc.measure_in_window(d(2026, 9, 1), d(2026, 9, 30), d(2026, 10, 1), d(2026, 12, 31))
    assert not inc.measure_in_window(d(2027, 1, 1), d(2027, 1, 31), d(2026, 10, 1), d(2026, 12, 31))


# --------------------------------------------------------------------------- targets and measures


def test_own_target_wins_over_the_grade_target():
    targets = [{"grade_id": 3, "employee_id": None, "target": "200"}, {"grade_id": None, "employee_id": 9,
                                                                      "target": "150"}]
    assert inc.resolve_target(targets, employee_id=9, grade_id=3) == inc.Target(D("150"), "EMPLOYEE")
    assert inc.resolve_target(targets, employee_id=8, grade_id=3) == inc.Target(D("200"), "GRADE")
    assert inc.resolve_target(targets, employee_id=8, grade_id=4) == inc.Target(None, None)
    assert inc.resolve_target([], employee_id=9, grade_id=None) == inc.Target(None, None)


def test_achievement_needs_a_positive_target():
    assert inc.local_values(D("115.5"), D("100"))["inc.achievement_pct"] == D("115.5")
    assert inc.local_values(D("50"), D("0"))["inc.achievement_pct"] is None
    assert inc.local_values(D("50"), None) == {"inc.measure": D("50"), "inc.target": None, "inc.achievement_pct": None}


@pytest.mark.parametrize(("value", "code"), [
    (D("0"), "MEASURE_NOT_POSITIVE"), (D("-1"), "MEASURE_NOT_POSITIVE"), (D("1.00001"), "MEASURE_INVALID"),
    (D("1000000"), "MEASURE_TOO_LARGE"), (D("NaN"), "MEASURE_INVALID"),
])
def test_measure_bounds(value, code):
    with pytest.raises(inc.IncentiveError) as error:
        inc.check_measure(value)
    assert error.value.code == code


def test_measure_at_the_column_limit_is_accepted():
    assert inc.check_measure(D("999999.9999")) == D("999999.9999")


# --------------------------------------------------------------------------- the policy and the formula language


def test_activation_needs_a_formula_with_known_incentive_variables():
    assert validate_fields("INCENTIVE_PLAN", {"formula": "inc.measure * 10"}, for_activation=True)
    with pytest.raises(PolicyValidationError) as error:
        validate_fields("INCENTIVE_PLAN", {"formula": "inc.measures * 10"}, for_activation=True)
    assert error.value.field == "formula"


def test_task_measures_cannot_be_activated_yet():
    assert validate_fields("INCENTIVE_PLAN", {"formula": "inc.measure", "measure_source": "TASKS"})   # draft ok
    with pytest.raises(PolicyValidationError) as error:
        validate_fields("INCENTIVE_PLAN", {"formula": "inc.measure", "measure_source": "TASKS"}, for_activation=True)
    assert error.value.field == "measure_source"


def test_incentive_variables_exist_only_in_plan_formulas():
    with pytest.raises(fm.FormulaError) as error:
        fm.check("inc.measure * 10", fm.TypeEnv("INR"))
    assert error.value.code == "UNKNOWN_REFERENCE"
    analysis = fm.check("COALESCE(inc.achievement_pct, 0) * 10", fm.TypeEnv("INR", locals=inc.LOCALS))
    assert "inc.achievement_pct" in analysis.variables
    warned = fm.check("inc.target * 2", fm.TypeEnv("INR", locals=inc.LOCALS))
    assert [w.code for w in warned.warnings] == ["NULL_VALUE"]                    # a target may be missing


def test_type_env_rejects_a_mislabelled_local():
    with pytest.raises(TypeError):
        fm.TypeEnv("INR", locals={"inc.x": inc.LOCALS["inc.measure"]})


# --------------------------------------------------------------------------- snapshot block


def test_snapshot_without_incentive_keeps_its_old_json_and_hash():
    parsed = Snapshot.from_dict(snapshot())
    assert "incentive" not in parsed.to_dict()
    assert parsed.incentive is None


def test_snapshot_incentive_block_round_trips():
    data = snapshot(incentive=window("120.5", "GRADE") | {"grade_id": 3})
    parsed = Snapshot.from_dict(data)
    assert parsed.incentive.target == D("120.5") and parsed.incentive.target_source == "GRADE"
    again = Snapshot.from_dict(parsed.to_dict())
    assert again == parsed and again.content_hash == parsed.content_hash
    assert again.content_hash != Snapshot.from_dict(snapshot()).content_hash


@pytest.mark.parametrize("bad", [
    {"window_start": "2026-11-30", "window_end": "2026-11-01"},
    {"window_start": "2026-11-01", "window_end": "2026-11-30", "target": "10"},
    {"window_start": "2026-11-01", "window_end": "2026-11-30", "target_source": "TEAM", "target": "1"},
    {"window_start": "2026-11-01"},
])
def test_snapshot_incentive_block_is_strict(bad):
    with pytest.raises(SnapshotError):
        Snapshot.from_dict(snapshot(incentive=bad))


# --------------------------------------------------------------------------- engine goldens


def test_band_tiers_over_two_measures():
    # Measures 70 + 45.5 = 115.5 against the employee's target 100: achievement 115.5 %, band [100, 120) -> 10,000.
    result = calculate(incentive_snapshot('BAND("INC_TIERS", inc.achievement_pct)',
                                          [measure(1, "70"), measure(2, "45.5")], tables=[TIERS]))
    assert result.ok, result.issues
    assert amounts(result)["INCENTIVE"] == D("10000.00")
    assert result.totals.gross == GROSS + D("10000.00")                         # 107,000.00
    [line] = [ln for ln in result.lines if ln.component == "INCENTIVE"]
    assert [s["id"] for s in line.sources] == [1, 2] and {s["type"] for s in line.sources} == {"wage_input"}
    assert line.kind == "EARNING" and "PART_OF_GROSS" in line.tags
    assert line.trace["measure"] == "115.5" and line.trace["target"] == "100"
    assert line.trace["achievement_pct"] == "115.5" and line.trace["policy"]["policy_hash"] == "plan-hash"
    assert line.explanation.startswith("Incentive for ") and "against target 100 (115.5%)" in line.explanation


def test_progressive_commission_slab_in_thousands():
    # Sales 450 (thousands): 0 x 100 + 0.05 x 200 + 0.08 x 150 = 10 + 12 = 22 -> x 1,000 = 22,000.00.
    result = calculate(incentive_snapshot('SLAB("INC_COMM", inc.measure) * 1000', [measure(5, "450")],
                                          target=None, tables=[COMMISSION]))
    assert result.ok, result.issues
    assert amounts(result)["INCENTIVE"] == D("22000.00")


def test_cap_applies_before_rounding():
    # 84,321.5 x 2.5 % = 2,108.0375 -> above the 2,000.00 cap -> 2,000.00 (unrounded value kept in the trace).
    result = calculate(incentive_snapshot("inc.measure * 2.5%", [measure(3, "84321.5")],
                                          cap={"amount": "2000.00", "currency": "INR"}))
    [line] = [ln for ln in result.lines if ln.component == "INCENTIVE"]
    assert line.amount == D("2000.00") and line.trace["capped"] is True
    assert D(line.trace["unrounded"]) == D("2108.0375")
    assert "capped at 2,000.00" in line.explanation


def test_below_the_cap_is_rounded_with_the_component_rounding():
    # 1,234.5 x 2.5 % = 30.8625 -> HALF_UP to 30.86.
    result = calculate(incentive_snapshot("inc.measure * 2.5%", [measure(3, "1234.5")],
                                          cap={"amount": "2000.00", "currency": "INR"}))
    assert amounts(result)["INCENTIVE"] == D("30.86")


def test_formula_reads_the_structure_rate():
    # Achievement 100 % >= 100 -> 10 % of the full-period BASIC rate 50,000.00 = 5,000.00.
    result = calculate(incentive_snapshot('IF(inc.achievement_pct >= 100, RATE("BASIC") * 10%, 0)',
                                          [measure(4, "100")]))
    assert result.ok, result.issues
    assert amounts(result)["INCENTIVE"] == D("5000.00")


def test_below_threshold_pays_a_zero_line_that_still_consumes_the_measure():
    # 79 % is in the [.., 80) band -> 0.00; the line exists so the measure is consumed once (EC-VAR-01).
    result = calculate(incentive_snapshot('BAND("INC_TIERS", inc.achievement_pct)', [measure(6, "79")],
                                          tables=[TIERS]))
    [line] = [ln for ln in result.lines if ln.component == "INCENTIVE"]
    assert line.amount == D("0.00") and [s["id"] for s in line.sources] == [6]


def test_missing_target_is_an_exception_never_a_guess():
    result = calculate(incentive_snapshot('BAND("INC_TIERS", inc.achievement_pct)', [measure(7, "90")],
                                          target=None, tables=[TIERS]))
    assert "INCENTIVE" not in amounts(result)
    [error] = [i for i in result.blocking if i.code == "FORMULA_ERROR" and i.component == "INCENTIVE"]
    assert error.details["error"]["code"] == "NULL_VALUE"
    fixed = calculate(incentive_snapshot('BAND("INC_TIERS", COALESCE(inc.achievement_pct, 0))', [measure(7, "90")],
                                         target=None, tables=[TIERS]))
    assert amounts(fixed)["INCENTIVE"] == D("0.00")


def test_switch_off_pays_nothing_and_says_so():
    result = calculate(incentive_snapshot("inc.measure * 10", [measure(8, "5")], on=False))
    assert "INCENTIVE" not in amounts(result)
    assert "INCENTIVES_DISABLED" in codes(result, "WARNING")
    assert result.totals.gross == GROSS


def test_switch_on_without_measures_pays_nothing():
    result = calculate(incentive_snapshot("inc.measure * 10", []))
    assert "INCENTIVE" not in amounts(result) and result.ok


def test_measures_never_reach_wage_input_lines_or_structure_inputs():
    # A manual BONUS input is still paid; the measure is not an INPUT amount (no INPUT_NEEDS_RATE) and pays 50.00.
    result = calculate(incentive_snapshot("inc.measure * 10", [measure(9, "5")],
                                          extra_inputs=[{"id": 10, "component": "BONUS", "amount": "1500.00"}]))
    assert result.ok, result.issues
    assert amounts(result)["BONUS"] == D("1500.00") and amounts(result)["INCENTIVE"] == D("50.00")
    assert "INPUT_NEEDS_RATE" not in codes(result)


def test_wage_inputs_switch_does_not_govern_measures():
    data = incentive_snapshot("inc.measure * 10", [measure(9, "5")])
    data["settings"]["payroll.wage_inputs.enabled"] = False
    assert amounts(calculate(data))["INCENTIVE"] == D("50.00")


@pytest.mark.parametrize(("change", "code"), [
    ({"block": False}, "INCENTIVE_WINDOW_MISSING"),
    ({"measure_source": "TASKS"}, "INCENTIVE_MEASURE_SOURCE_UNSUPPORTED"),
    ({"component": "BASIC_TYPO"}, "INCENTIVE_COMPONENT_INVALID"),
    ({"component": "OTHER_DEDUCTION"}, "INCENTIVE_COMPONENT_INVALID"),
    ({"cap": {"amount": "100.00", "currency": "USD"}}, "INCENTIVE_CAP_CURRENCY"),
    ({"formula": "inc.measure - 1000"}, "INCENTIVE_NEGATIVE"),
    ({"formula": "inc.measure * "}, "FORMULA_ERROR"),
    ({"formula": "inc.measure > 1"}, "FORMULA_ERROR"),                 # a condition is not an amount
])
def test_configuration_problems_block_instead_of_paying(change, code):
    change = dict(change)
    formula = change.pop("formula", "inc.measure * 10")
    block = change.pop("block", True)
    result = calculate(incentive_snapshot(formula, [measure(11, "5")], block=block, **change))
    assert code in codes(result, "BLOCKING"), result.issues
    assert "INCENTIVE" not in amounts(result)


def test_measure_of_another_component_blocks():
    result = calculate(incentive_snapshot("inc.measure * 10", [measure(12, "5", component="BONUS")]))
    assert "INCENTIVE_MEASURE_MISMATCH" in codes(result, "BLOCKING")


def test_plan_missing_with_measures_blocks():
    data = incentive_snapshot("inc.measure * 10", [measure(13, "5")])
    data["policies"] = {}
    result = calculate(data)
    assert "INCENTIVE_PLAN_MISSING" in codes(result, "BLOCKING")


def test_replay_is_deterministic():
    data = incentive_snapshot('BAND("INC_TIERS", inc.achievement_pct)', [measure(1, "70"), measure(2, "45.5")],
                              tables=[TIERS])
    assert calculate(data).calc_hash == calculate(Snapshot.from_dict(data).to_dict()).calc_hash
