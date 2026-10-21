"""Evaluation of formulas: golden vectors, every function, traces and run-time errors (3.3 §3, §4, §11).

Edge cases: EC-CMP-02, EC-CMP-03, EC-CMP-04, EC-CMP-05, EC-CMP-07, EC-CMP-08, EC-CMP-19, EC-CMP-20, EC-CMP-21.
"""

from __future__ import annotations

import builtins
import json
import pickle
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

import pytest

from app.payroll.formula import (
    ComponentSig,
    EvalContext,
    EvalResult,
    FormulaError,
    MoneyValue,
    Proration,
    Table,
    TableKind,
    TableRow,
    TypeEnv,
    check,
    check_floor,
    evaluate,
    parse,
)
from app.payroll.formula.ast import Binary, Number

D = Decimal


def inr(amount: str) -> MoneyValue:
    return MoneyValue(D(amount), "INR")


def run(source: str, ctx: EvalContext) -> EvalResult:
    return evaluate(parse(source), ctx)


def value(source: str, ctx: EvalContext) -> Any:
    result = run(source, ctx)
    assert result.error is None, result.error
    return result.value


def failure(source: str, ctx: EvalContext) -> FormulaError:
    result = run(source, ctx)
    assert result.error is not None, f"expected an error, got {result.value}"
    assert result.value is None
    return result.error


PT_BAND = Table(
    "PT_IN_MH",
    TableKind.BAND,
    (
        TableRow(lower=None, upper=D(7500), value=D(0)),
        TableRow(lower=D(7500), upper=D(10000), value=D(175)),
        TableRow(lower=D(10000), upper=None, value=D(200)),
    ),
    key_currency="INR",
    value_currency="INR",
    upper_inclusive=True,
)
TAX_SLAB = Table(
    "TAX_SLAB",
    TableKind.SLAB,
    (
        TableRow(lower=D(0), upper=D(250000), rate=D(0)),
        TableRow(lower=D(250000), upper=D(500000), rate=D("0.05")),
        TableRow(lower=D(500000), upper=None, rate=D("0.20")),
    ),
    key_currency="INR",
)
MEAL = Table("MEAL_BY_GRADE", TableKind.LOOKUP, (TableRow(key="G1", value=D(1200)), TableRow(key="G3", value=D(2000))),
             value_currency="INR")
GAPS = Table("GAPS", TableKind.BAND, (TableRow(lower=D(0), upper=D(100), value=D(1)),
                                       TableRow(lower=D(200), upper=D(300), value=D(2))))
TABLES = {t.code: t for t in (PT_BAND, TAX_SLAB, MEAL, GAPS)}


def ctx(**overrides: Any) -> EvalContext:
    values: dict[str, Any] = {
        "components": {"BASIC": inr("50000.00"), "HRA": inr("20000.00"), "DA": None},
        "rates": {"BASIC": inr("50000.00"), "HRA": inr("20000.00")},
        "tags": {"OT_BASE": ["BASIC"], "PART_OF_GROSS": ["BASIC", "HRA"]},
        "variables": {
            "att.paid_days": D(28), "att.present_days": D(22), "att.late_marks": None,
            "emp.standard_hours_per_day": D(8), "emp.years_of_service": D("7.5"), "emp.grade": "G3",
            "emp.is_joiner": True, "emp.hire_date": date(2019, 1, 31), "period.end": date(2026, 11, 30),
            "period.start": date(2026, 11, 1), "ctc.monthly": inr("100000.00"), "ctc.annual": inr("1200000.00"),
        },
        "inputs": {"JOINING_BONUS": inr("10000.00")},
        "tables": TABLES,
        "component": "TEST",
        "stage": 4,
    }
    values.update(overrides)
    return EvalContext.build("INR", **values)


# -- golden vectors --------------------------------------------------------------------------------


def test_trace_of_hra_at_40_percent() -> None:
    """3.3 §4.7 HRA example: exact steps and references for the formula tester (§4.8)."""
    result = run("BASIC * 40%", ctx())
    assert result.value == inr("20000.00")
    assert result.steps == ["BASIC = 50000.00 INR", "BASIC * 0.40 = 20000.00 INR"]
    assert result.references == {"BASIC": "50000.00 INR"}
    assert str(result.type) == "MONEY(INR)"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("BASIC * 40%", "20000.00"),
        ("IF(att.present_days >= 20, 1500, 0)", "1500"),
        ("MIN(att.present_days * 100, 2000)", "2000"),
        ('ROUND(SUM_TAG_RATE("OT_BASE") / 26 / emp.standard_hours_per_day, 2)', "240.38"),
        ("SWITCH(TRUE, emp.years_of_service >= 10, 3000, emp.years_of_service >= 5, 1500, 0)", "1500"),
        ("ROUND((BASIC + COALESCE(DA, 0)) / 26, 2)", "1923.08"),
        ('LOOKUP("MEAL_BY_GRADE", emp.grade, 0)', "2000"),
        ('IF(emp.is_joiner, INPUT("JOINING_BONUS"), 0)', "10000.00"),
    ],
)
def test_section_4_7_examples_evaluate(source: str, expected: str) -> None:
    """3.3 §4.7: every example evaluates to the exact amount in the component currency."""
    result = value(source, ctx())
    assert result == inr(expected)


def test_section_4_7_examples_negative_branches() -> None:
    """3.3 §4.7: the other branch of each conditional example."""
    low = ctx(variables={"att.present_days": D(19), "emp.years_of_service": D("4.99"), "emp.grade": "G9",
                         "emp.is_joiner": False, "emp.standard_hours_per_day": D(8)})
    assert value("IF(att.present_days >= 20, 1500, 0)", low) == inr("0")
    assert value("MIN(att.present_days * 100, 2000)", low) == inr("1900")
    assert value("SWITCH(TRUE, emp.years_of_service >= 10, 3000, emp.years_of_service >= 5, 1500, 0)", low) == inr("0")
    assert value('LOOKUP("MEAL_BY_GRADE", emp.grade, 0)', low) == inr("0")
    assert value('IF(emp.is_joiner, INPUT("JOINING_BONUS"), 0)', low) == inr("0")
    assert value("(BASIC + COALESCE(DA, 0)) / 26", ctx(components={"BASIC": inr("52000"), "DA": inr("2600")})) \
        == inr("2100")


def test_ot_hourly_rate_is_unrounded_until_round() -> None:
    """3.3 §4.4: intermediate values keep 28 significant digits; nothing is rounded without ROUND."""
    result = value('SUM_TAG_RATE("OT_BASE") / 26 / emp.standard_hours_per_day', ctx())
    assert result.amount == D("240.3846153846153846153846154")
    assert value("100 / 3 * 3", ctx()) == inr("99.99999999999999999999999999")
    assert value("ROUND(100 / 3, 2) * 3", ctx()) == inr("99.99")


def test_example_a_ctc_structure() -> None:
    """3.3 §11 Example A: CTC 12,00,000 INR; BASIC 50,000.00, HRA 20,000.00, employer PF 3,000.00, FA 27,000.00."""
    c = ctx(components={}, rates={})
    assert value("ctc.annual / 12", c) == inr("100000")
    basic = value("ctc.monthly * 50%", c)
    assert basic == inr("50000.00")
    c = ctx(components={"BASIC": basic})
    hra = value("BASIC * 40%", c)
    assert hra == inr("20000.00")
    pf = value("MIN(BASIC, 25000) * 12%", c)
    assert pf == inr("3000.00")
    c = ctx(components={"BASIC": basic, "HRA": hra, "EMPLOYER_PF": pf})
    fixed = value("ctc.monthly - BASIC - HRA - EMPLOYER_PF", c)
    assert fixed == inr("27000.00")
    c = ctx(components={"BASIC": basic, "HRA": hra, "FIXED_ALLOWANCE": fixed})
    assert value("BASIC + HRA + FIXED_ALLOWANCE", c) == inr("97000.00")
    assert check_floor(fixed) is None


def test_example_a_before_wage_ceiling_change() -> None:
    """3.3 §11 Example A: before 17 Sep 2026 the 15,000 ceiling gives PF 1,800.00 and FA 28,200.00."""
    c = ctx(components={"BASIC": inr("50000.00"), "HRA": inr("20000.00")})
    pf = value("MIN(BASIC, 15000) * 12%", c)
    assert pf == inr("1800.00")
    c = ctx(components={"BASIC": inr("50000.00"), "HRA": inr("20000.00"), "EMPLOYER_PF": pf})
    assert value("ctc.monthly - BASIC - HRA - EMPLOYER_PF", c) == inr("28200.00")


def test_example_a_two_lop_days_prorated() -> None:
    """3.3 §11 Example A: 2 LOP days in a 30-day month: 46,666.67, 18,666.67 and 25,200.00."""
    c = ctx(components={"BASIC": inr("50000.00"), "HRA": inr("20000.00"), "FIXED_ALLOWANCE": inr("27000.00")},
            proration=Proration(D(28), D(30)))
    raw = run("PRORATE(BASIC)", c)
    assert raw.value == inr("46666.66666666666666666666667")
    assert raw.steps[-1] == "PRORATE(BASIC) = 46666.66666666666666666666667 INR (50000.00 INR × 28 ÷ 30)"
    assert value("ROUND(PRORATE(BASIC), 2)", c) == inr("46666.67")
    assert value("ROUND(PRORATE(HRA), 2)", c) == inr("18666.67")
    assert value("ROUND(PRORATE(FIXED_ALLOWANCE), 2)", c) == inr("25200.00")


def test_example_b_hourly_worker() -> None:
    """3.3 §11 Example B: 250.00 × 182.50 = 45,625.00; OT 250.00 × 1.5 × 10 = 3,750.00; gross 49,375.00."""
    c = ctx(components={"HOURLY_RATE": inr("250.00")},
            variables={"att.payable_hours": D("182.50"), "ot.hourly_rate": inr("250.00"), "ot.hours_workday": D(10)})
    base = value("HOURLY_RATE * att.payable_hours", c)
    overtime = value("ot.hourly_rate * 1.5 * ot.hours_workday", c)
    assert base == inr("45625.00") and overtime == inr("3750.00")
    gross = value("BASE_PAY + OT_WORKDAY", ctx(components={"BASE_PAY": base, "OT_WORKDAY": overtime}))
    assert gross == inr("49375.00")


# -- functions -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("MIN(3, 1.5, 2)", "1.5"),
        ("MAX(3, 1.5, 7)", "7"),
        ("ABS(-4.25)", "4.25"),
        ("ROUND(2.345, 2)", "2.35"),
        ('ROUND(2.345, 2, "HALF_EVEN")', "2.34"),
        ('ROUND(-2.345, 2, "HALF_UP")', "-2.35"),
        ('ROUND(2.341, 2, "UP")', "2.35"),
        ('ROUND(2.349, 2, "DOWN")', "2.34"),
        ('ROUND(-2.341, 2, "CEILING")', "-2.34"),
        ('ROUND(-2.341, 2, "FLOOR")', "-2.35"),
        ("ROUND(12345, -2)", "12300"),
        ("ROUNDUP(2.001, 2)", "2.01"),
        ("ROUNDUP(-2.001, 2)", "-2.01"),
        ("ROUNDDOWN(2.999, 2)", "2.99"),
        ("ROUNDDOWN(-2.999, 0)", "-2"),
        ("FLOOR(1234.5, 100)", "1200"),
        ("FLOOR(-1234.5, 100)", "-1300"),
        ("FLOOR(7.3, 0.25)", "7.25"),
        ("CEIL(1201, 100)", "1300"),
        ("CEIL(-1250, 100)", "-1200"),
        ("CEIL(1200, 100)", "1200"),
        ("CLAMP(5, 1, 3)", "3"),
        ("CLAMP(-5, 1, 3)", "1"),
        ("CLAMP(2, 1, 3)", "2"),
        ("DIVIDE(10, 4, 0)", "2.5"),
        ("DIVIDE(10, 0, 7)", "7"),
        ("IF(1 < 2, 10, 20)", "10"),
        ("SWITCH(2, 1, 10, 2, 20, 30)", "20"),
        ("SWITCH(9, 1, 10, 2, 20, 30)", "30"),
        ('SWITCH(emp.grade, "G1", 1, "G3", 3, 0)', "3"),
        ("COALESCE(att.late_marks, 4)", "4"),
        ("YEAR(period.end)", "2026"),
        ("MONTH(period.end)", "11"),
        ("DAYS_BETWEEN(period.start, period.end)", "29"),
        ("DAYS_BETWEEN(period.end, period.start)", "-29"),
        ("MONTHS_BETWEEN(DATE(2026, 1, 15), DATE(2026, 3, 15))", "2"),
        ("MONTHS_BETWEEN(DATE(2026, 1, 31), DATE(2026, 2, 28))", "0"),
        ("MONTHS_BETWEEN(DATE(2026, 3, 15), DATE(2026, 1, 15))", "-2"),
        ("YEARS_BETWEEN(emp.hire_date, period.end)", "7"),
        ("YEARS_BETWEEN(DATE(2020, 2, 29), DATE(2021, 2, 28))", "0"),
        ("YEARS_BETWEEN(period.end, emp.hire_date)", "-7"),
        ("YEAR(DATE(YEAR(period.end), 4, 1))", "2026"),
    ],
)
def test_function_golden_values(source: str, expected: str) -> None:
    """3.3 §4.6: golden test for every function (number results are amounts for an AMOUNT formula)."""
    assert value(source, ctx(expect="NUMBER")) == D(expected)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('IN(emp.grade, "G1", "G3")', True),
        ('IN(emp.grade, "G1", "G2")', False),
        ("TRUE AND NOT FALSE", True),
        ("AND(TRUE, TRUE, FALSE)", False),
        ("OR(FALSE, FALSE, TRUE)", True),
        ("emp.hire_date < period.end", True),
        ('emp.grade <> "G3"', False),
        ("BASIC > 49999.99", True),
        ("BASIC = HRA * 2.5", True),
        ("DATE(2026, 11, 30) = period.end", True),
    ],
)
def test_boolean_functions(source: str, expected: bool) -> None:
    assert value(source, ctx(expect="BOOLEAN")) is expected


def test_money_functions_keep_currency() -> None:
    c = ctx()
    assert value("MIN(BASIC, 2000)", c) == inr("2000")
    assert value("MAX(BASIC, HRA)", c) == inr("50000.00")
    assert value("ABS(HRA - BASIC)", c) == inr("30000.00")
    assert value("CLAMP(BASIC, 0, 45000)", c) == inr("45000")
    assert value("FLOOR(BASIC / 7, 10)", c) == inr("7140")
    assert value("CEIL(BASIC / 7, 10)", c) == inr("7150")
    assert value("DIVIDE(BASIC, att.paid_days, 0)", c) == inr("1785.714285714285714285714286")
    assert value("ROUND(HRA / BASIC * 100, 0)", ctx(expect="NUMBER")) == D(40)


def test_logic_is_lazy_left_to_right() -> None:
    """IF, AND, OR, COALESCE and DIVIDE do not evaluate the branch they do not need."""
    c = ctx(variables={"att.paid_days": D(0), "att.late_marks": None})
    assert value("IF(att.paid_days > 0, BASIC / att.paid_days, 0)", c) == inr("0")
    assert value("att.paid_days = 0 OR BASIC / att.paid_days > 1", ctx(expect="BOOLEAN", variables={
        "att.paid_days": D(0)})) is True
    assert value("att.paid_days > 0 AND att.late_marks > 1", ctx(expect="BOOLEAN", variables={
        "att.paid_days": D(0), "att.late_marks": None})) is False
    assert value("COALESCE(BASIC, BASIC / att.paid_days)", c) == inr("50000.00")


def test_component_functions() -> None:
    """SUM_TAG reads prorated amounts, SUM_TAG_RATE and RATE the full-period rates (3.3 §4.2)."""
    c = ctx(components={"BASIC": inr("46666.67"), "HRA": inr("18666.67")},
            rates={"BASIC": inr("50000.00"), "HRA": inr("20000.00")},
            units={"OT_UNITS": D("12.5")}, prev={"BASIC": inr("50000.00")}, ytd={"BASIC": inr("350000.00")})
    assert value('SUM_TAG("PART_OF_GROSS")', c) == inr("65333.34")
    assert value('SUM_TAG_RATE("PART_OF_GROSS")', c) == inr("70000.00")
    assert value('RATE("BASIC")', c) == inr("50000.00")
    assert value('UNITS("OT_UNITS") * 100', c) == inr("1250.0")
    assert value('PREV("BASIC")', c) == inr("50000.00")
    assert value('YTD("BASIC") + BASIC', c) == inr("396666.67")
    result = run('INPUT("NO_INPUT")', ctx(inputs={"NO_INPUT": inr("0")}))
    assert result.value == inr("0")


def test_sum_tag_of_empty_tag_is_zero_in_pay_currency() -> None:
    assert value('SUM_TAG("EMPTY")', ctx(tags={"EMPTY": []})) == inr("0")


def test_prorate_variants() -> None:
    assert value("PRORATE(BASIC)", ctx(proration=Proration.none())) == inr("50000.00")
    capped = ctx(proration=Proration(D(31), D(30), cap_at_rate=True))
    result = run("PRORATE(BASIC)", capped)
    assert result.value == inr("50000.00") and "capped at the full rate" in result.steps[-1]
    assert failure("PRORATE(BASIC)", ctx()).code == "UNKNOWN_REFERENCE"
    assert failure("PRORATE(BASIC)", ctx(proration=Proration(D(0), D(0)))).code == "DIVISION_BY_ZERO"


def test_slab_is_progressive() -> None:
    """SLAB: Σ rate × portion; 600,000 -> 0 + 5% × 250,000 + 20% × 100,000 = 32,500.00."""
    result = run('SLAB("TAX_SLAB", BASIC * 12)', ctx())
    assert result.value == inr("32500.00")
    assert "0.05 × 250000" in result.steps[-1] and "0.2 × 100000" in result.steps[-1]
    assert value('SLAB("TAX_SLAB", 250000)', ctx()) == inr("0")
    assert value('SLAB("TAX_SLAB", 250001)', ctx()) == inr("0.05")


def test_band_is_flat_with_inclusive_upper_bounds() -> None:
    """BAND: the flat value of the band (PT-style "above 7,500 up to 10,000"); Example A's 200.00."""
    assert value('BAND("PT_IN_MH", 7500)', ctx()) == inr("0")
    assert value('BAND("PT_IN_MH", 10000)', ctx()) == inr("175")
    assert value('BAND("PT_IN_MH", 10000.01)', ctx()) == inr("200")
    assert value('BAND("PT_IN_MH", BASIC + HRA + 27000)', ctx()) == inr("200")
    assert run('BAND("PT_IN_MH", 9000)', ctx()).steps[-1].endswith("(band 7500–10000)")


def test_no_band_match_and_table_not_found() -> None:
    """3.3 §4.5: a value outside all bands or a missing table is a run-time error, never zero."""
    assert value('BAND("GAPS", 250)', ctx(expect="NUMBER")) == D(2)
    assert failure('BAND("GAPS", 150)', ctx(expect="NUMBER")).code == "NO_BAND_MATCH"
    assert failure('BAND("GAPS", -1)', ctx(expect="NUMBER")).code == "NO_BAND_MATCH"
    assert failure('BAND("GAPS", 300)', ctx(expect="NUMBER")).code == "NO_BAND_MATCH"
    missing = ctx(tables={})
    assert failure('LOOKUP("MEAL_BY_GRADE", emp.grade, 0)', missing).code == "TABLE_NOT_FOUND"


def test_table_provider_protocol() -> None:
    class Provider:
        def get_table(self, code: str) -> Table | None:
            return TABLES.get(code)

    assert value('LOOKUP("MEAL_BY_GRADE", "G1", 0)', ctx(tables=Provider())) == inr("1200")


@pytest.mark.parametrize(
    "rows",
    [
        (TableRow(lower=D(0), upper=D(10), rate=D("0.1")), TableRow(lower=D(20), upper=None, rate=D("0.2"))),
        (TableRow(lower=D(0), upper=D(10), rate=D("1.5")),),
        (TableRow(lower=D(0), upper=D(10), rate=D("0.1")), TableRow(lower=D(5), upper=None, rate=D("0.2"))),
        (TableRow(lower=D(10), upper=D(5), rate=D("0.1")),),
        (TableRow(lower=D(0), upper=D(10), value=D(1)),),
        (),
    ],
)
def test_malformed_slab_tables_are_rejected(rows: tuple[TableRow, ...]) -> None:
    with pytest.raises(FormulaError) as info:
        Table("BAD", TableKind.SLAB, rows)
    assert info.value.code == "INVALID_TABLE"


def test_malformed_lookup_and_band_tables_are_rejected() -> None:
    with pytest.raises(FormulaError):
        Table("BAD", TableKind.LOOKUP, (TableRow(key="A", value=D(1)), TableRow(key="A", value=D(2))))
    with pytest.raises(FormulaError):
        Table("BAD", TableKind.BAND, (TableRow(lower=D(0), upper=D(10), value=D(1)),
                                      TableRow(lower=D(5), upper=D(20), value=D(2))))
    with pytest.raises(FormulaError):
        Table("BAD", TableKind.BAND, (TableRow(lower=D(0), upper=D(10), value=1.5),))  # type: ignore[arg-type]
    with pytest.raises(FormulaError):
        Table("bad", TableKind.LOOKUP, (TableRow(key="A", value=D(1)),))


# -- trace, references, determinism ----------------------------------------------------------------


def test_references_include_unevaluated_branches() -> None:
    result = run('IF(emp.is_joiner, INPUT("JOINING_BONUS"), RATE("HRA"))', ctx())
    assert result.references == {"emp.is_joiner": "TRUE", 'INPUT("JOINING_BONUS")': "10000.00 INR",
                                 'RATE("HRA")': "20000.00 INR"}


def test_trace_uses_currency_minor_units_for_display() -> None:
    jpy = EvalContext.build("JPY", components={"BASIC": MoneyValue(D(300000), "JPY")}, minor_units={"JPY": 0})
    result = run("BASIC * 10%", jpy)
    assert result.steps == ["BASIC = 300000 JPY", "BASIC * 0.10 = 30000 JPY"]
    kwd = EvalContext.build("KWD", components={"BASIC": MoneyValue(D("1200.5"), "KWD")}, minor_units={"KWD": 3})
    assert run("BASIC", kwd).steps == ["BASIC = 1200.500 KWD"]


def test_result_to_dict_is_json_with_decimal_strings() -> None:
    payload = run("BASIC * 40%", ctx()).to_dict()
    assert json.loads(json.dumps(payload))["value"] == {"amount": "20000", "currency": "INR"}
    assert run("BASIC / 3", ctx()).to_dict()["value"] == {"amount": "16666.66666666666666666666667", "currency": "INR"}
    assert payload["error"] is None and payload["type"] == "MONEY(INR)"
    error_payload = run("BASIC / att.paid_days", ctx(variables={"att.paid_days": D(0)})).to_dict()
    assert error_payload["error"]["code"] == "DIVISION_BY_ZERO" and error_payload["value"] is None


def test_evaluation_ignores_the_ambient_decimal_context() -> None:
    """28-digit fixed context: a caller's decimal context never changes results."""
    expected = value("BASIC / 3", ctx())
    with localcontext() as local:
        local.prec = 5
        local.rounding = "ROUND_DOWN"
        assert value("BASIC / 3", ctx()) == expected
    assert expected.amount == D("16666.66666666666666666666667")


def test_evaluation_is_deterministic() -> None:
    tree = parse('ROUND(SUM_TAG_RATE("OT_BASE") / 26 / emp.standard_hours_per_day * 1.5, 2)')
    first, second = evaluate(tree, ctx()), evaluate(tree, ctx())
    assert first == second and first.value == inr("360.58")


def test_unwrap_raises_the_error() -> None:
    result = run("BASIC / att.paid_days", ctx(variables={"att.paid_days": D(0)}))
    with pytest.raises(FormulaError) as info:
        result.unwrap()
    assert info.value.code == "DIVISION_BY_ZERO"
    assert run("BASIC", ctx()).unwrap() == inr("50000.00")


def test_decimal_overflow_is_limit_exceeded() -> None:
    big = "9" * 28
    assert failure(f"ROUND({big} * {big}, 12)", ctx(expect="NUMBER")).code == "LIMIT_EXCEEDED"


def test_hand_built_tree_over_the_limits_is_rejected() -> None:
    node: Any = Number(D(1), "1", False, 0, 1)
    for _ in range(MAX_HAND_BUILT := 260):
        node = Binary("+", node, Number(D(1), "1", False, 0, 1), 0, 1)
    assert MAX_HAND_BUILT * 2 + 1 > 500
    result = evaluate(node, ctx(expect="NUMBER"))
    assert result.error is not None and result.error.code == "LIMIT_EXCEEDED"


def test_long_flat_sum_evaluates() -> None:
    """249 additions of 1 (499 nodes) evaluate without recursion problems."""
    assert value(" + ".join(["1"] * 250), ctx(expect="NUMBER")) == D(250)


def test_static_errors_are_returned_not_raised() -> None:
    result = run("BASIC + 5%", ctx())
    assert result.error is not None and result.error.code == "TYPE_MISMATCH" and result.steps == []


def test_floats_are_never_accepted() -> None:
    with pytest.raises(TypeError):
        MoneyValue(1.5, "INR")  # type: ignore[arg-type]
    result = run("att.paid_days * 2", ctx(variables={"att.paid_days": 1.5}))
    assert result.error is not None and result.error.code == "TYPE_MISMATCH"
    with pytest.raises(TypeError):
        Proration(28.0, D(30))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "tree",
    [
        Number(0.1, "0.1", False, 0, 3),  # type: ignore[arg-type]
        Binary("+", Number(1.5, "1.5", False, 0, 3), Number(D(1), "1", False, 6, 7), 0, 7),  # type: ignore[arg-type]
        Number(D("NaN"), "NaN", False, 0, 3),
    ],
)
def test_hand_built_trees_with_floats_are_refused(tree: Any) -> None:
    """No float enters evaluation or analysis, even through a tree not produced by parse()."""
    with pytest.raises(TypeError):
        evaluate(tree, ctx(expect="NUMBER"))
    with pytest.raises(TypeError):
        check(tree, ctx().env)


@pytest.mark.parametrize(("units", "basis"), [(D("NaN"), D(30)), (D(28), D("Infinity")), (D(-1), D(30)),
                                              (D(28), D(-30))])
def test_proration_must_be_finite_and_not_negative(units: Decimal, basis: Decimal) -> None:
    with pytest.raises(ValueError):
        Proration(units, basis)


def test_zero_proration_basis_is_division_by_zero_not_zero_pay() -> None:
    result = run("PRORATE(BASIC)", ctx(proration=Proration(D(0), D(0))))
    assert result.error is not None and result.error.code == "DIVISION_BY_ZERO"


def test_formula_error_pickles_by_value() -> None:
    """Errors travel through job queues unchanged (code, position, path, issues)."""
    error = FormulaError("CYCLE_DETECTED", "circular dependency: A → A", position=3, end=4, path=["A", "A"])
    copy = pickle.loads(pickle.dumps(error))
    assert copy == error and copy.path == ["A", "A"] and copy.code == "CYCLE_DETECTED"


def test_missing_required_component_is_an_error_and_optional_is_null() -> None:
    sigs = {"BASIC": ComponentSig("INR", 4), "DA": ComponentSig("INR", 4, optional=True)}
    c = EvalContext(TypeEnv("INR", components=sigs), components={})
    assert failure("BASIC", c).code == "UNKNOWN_REFERENCE"
    assert failure("DA", c).code == "NULL_VALUE"
    assert value("COALESCE(DA, 0)", c) == inr("0")


def test_context_value_in_wrong_currency_is_currency_mismatch() -> None:
    sigs = {"BASIC": ComponentSig("INR", 4)}
    c = EvalContext(TypeEnv("INR", components=sigs), components={"BASIC": MoneyValue(D(100), "USD")})
    assert failure("BASIC * 2", c).code == "CURRENCY_MISMATCH"


# -- edge cases ------------------------------------------------------------------------------------


def test_ec_cmp_02_division_by_zero_is_exception_with_trace() -> None:
    """EC-CMP-02: zero paid days -> DIVISION_BY_ZERO returned with the trace; never a zero amount."""
    result = run("BASIC / att.paid_days * 26", ctx(variables={"att.paid_days": D(0)}))
    assert result.error is not None and result.error.code == "DIVISION_BY_ZERO"
    assert result.value is None
    assert result.steps == ["BASIC = 50000.00 INR", "att.paid_days = 0"]
    assert result.references == {"BASIC": "50000.00 INR", "att.paid_days": "0"}
    assert result.error.position == 0


def test_ec_cmp_02_divide_fallback_avoids_the_error() -> None:
    """EC-CMP-02 boundary: DIVIDE(a, b, fallback) chooses the fallback; a non-zero divisor divides."""
    zero = run("DIVIDE(BASIC, att.paid_days, 0)", ctx(variables={"att.paid_days": D(0)}))
    assert zero.value == inr("0") and zero.steps[-1].endswith("(divisor is 0: fallback)")
    assert value("BASIC / att.paid_days", ctx(variables={"att.paid_days": D(25)})) == inr("2000")
    zero_step = ctx(variables={"att.paid_days": D(3), "att.lop_days": D(0)}, expect="NUMBER")
    assert failure("FLOOR(att.paid_days, att.lop_days)", zero_step).code == "DIVISION_BY_ZERO"


def test_ec_cmp_03_switched_off_variable_is_null_value() -> None:
    """EC-CMP-03: att.late_marks with attendance OFF is NULL; arithmetic on it is NULL_VALUE."""
    error = failure("att.late_marks * 100", ctx())
    assert error.code == "NULL_VALUE" and "COALESCE" in error.message
    assert failure("att.late_marks > 3", ctx(expect="BOOLEAN")).code == "NULL_VALUE"
    missing = ctx(variables={})
    assert failure("ot.hours_total * 2", missing).code == "NULL_VALUE"


def test_ec_cmp_03_coalesce_supplies_the_default() -> None:
    """EC-CMP-03 boundary: COALESCE(att.late_marks, 0) evaluates; a present value is used."""
    assert value("COALESCE(att.late_marks, 0) * 100", ctx()) == inr("0")
    assert value("COALESCE(att.late_marks, 0) * 100", ctx(variables={"att.late_marks": 3})) == inr("300")
    references = run("COALESCE(att.late_marks, 0)", ctx()).references
    assert references == {"att.late_marks": "NULL"}


def test_ec_cmp_04_negative_balancing_component_is_negative_result() -> None:
    """EC-CMP-04: a balancing component below its floor of 0 -> NEGATIVE_RESULT (returned, not raised)."""
    low = ctx(components={"BASIC": inr("15000"), "HRA": inr("6000"), "EMPLOYER_PF": inr("1800")},
              variables={"ctc.monthly": inr("20000")})
    fixed = value("ctc.monthly - BASIC - HRA - EMPLOYER_PF", low)
    assert fixed == inr("-2800")
    error = check_floor(fixed, component="FIXED_ALLOWANCE", minor_units={"INR": 2})
    assert error is not None and error.code == "NEGATIVE_RESULT"
    assert error.message == "FIXED_ALLOWANCE is -2800.00 INR, below its floor of 0.00 INR"


def test_ec_cmp_04_zero_and_positive_pass_the_floor() -> None:
    """EC-CMP-04 boundary: exactly 0 and a positive amount pass; a money floor needs the same currency."""
    assert check_floor(inr("0")) is None
    assert check_floor(inr("0.01"), inr("0.01")) is None
    assert check_floor(inr("5"), inr("10")) is not None
    with pytest.raises(FormulaError) as info:
        check_floor(inr("5"), MoneyValue(D(1), "USD"))
    assert info.value.code == "CURRENCY_MISMATCH"


def test_ec_cmp_05_same_display_name_codes_stay_distinct() -> None:
    """EC-CMP-05: traces and references use codes, so two "Site allowance" components never merge."""
    c = ctx(components={"SITE_ALLOWANCE_NORTH": inr("500"), "SITE_ALLOWANCE_SOUTH": inr("700")})
    result = run("SITE_ALLOWANCE_NORTH + SITE_ALLOWANCE_SOUTH", c)
    assert result.value == inr("1200")
    assert list(result.references) == ["SITE_ALLOWANCE_NORTH", "SITE_ALLOWANCE_SOUTH"]


def test_ec_cmp_07_two_currencies_fail_at_evaluation_too() -> None:
    """EC-CMP-07: INR + USD is CURRENCY_MISMATCH; no implicit FX at run time."""
    c = ctx(components={"BASIC": inr("100"), "BONUS_USD": MoneyValue(D(100), "USD")})
    assert failure("BASIC + BONUS_USD", c).code == "CURRENCY_MISMATCH"
    assert failure("BONUS_USD * 2", c).code == "CURRENCY_MISMATCH"


def test_ec_cmp_07_same_currency_evaluates() -> None:
    """EC-CMP-07 boundary: a USD component evaluated in a USD context is fine."""
    usd = EvalContext.build("USD", components={"BONUS_USD": MoneyValue(D(100), "USD")})
    assert value("BONUS_USD * 2", usd) == MoneyValue(D(200), "USD")


def test_ec_cmp_08_evaluation_never_uses_eval_exec_or_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    """EC-CMP-08: evaluation is a tree walk; eval/exec/compile are never called."""

    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("eval/exec/compile must never be used")

    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    monkeypatch.setattr(builtins, "compile", forbidden)
    for source in ("BASIC * 40%", 'SLAB("TAX_SLAB", BASIC * 12)', "IF(att.present_days >= 20, 1500, 0)",
                   'ROUND(DIVIDE(BASIC, att.paid_days, 0), 2, "HALF_EVEN")'):
        assert run(source, ctx()).ok


def test_ec_cmp_08_strings_are_never_evaluated() -> None:
    """EC-CMP-08 boundary: evaluate only accepts parsed trees, and string literals stay data."""
    with pytest.raises(TypeError):
        evaluate("1 + 1", ctx())  # type: ignore[arg-type]
    assert value('IN("__import__", "os")', ctx(expect="BOOLEAN")) is False
    payload = '__import__("os").system("x")'
    lookup = run(f'LOOKUP("MEAL_BY_GRADE", "{payload.replace(chr(34), "")}", 0)', ctx())
    assert lookup.value == inr("0")


def test_ec_cmp_19_protected_attribute_rejected_at_evaluation() -> None:
    """EC-CMP-19: a company formula reading emp.gender fails even when the data is present."""
    c = ctx(variables={"emp.gender": "F"}, expect="NUMBER")
    assert failure('IF(emp.gender = "F", 1, 0)', c).code == "PROTECTED_ATTRIBUTE"


def test_ec_cmp_19_country_pack_evaluates_protected_attribute() -> None:
    """EC-CMP-19 boundary: a country-pack formula may use the date of birth (age-banded rates)."""
    c = ctx(variables={"emp.date_of_birth": date(1968, 5, 1), "period.end": date(2026, 11, 30)},
            allow_protected=True, expect="NUMBER")
    assert value("IF(YEARS_BETWEEN(emp.date_of_birth, period.end) >= 58, 0, 12%)", c) == D(0)


def test_ec_cmp_20_ytd_missing_is_zero() -> None:
    """EC-CMP-20: YTD at tax-year start is zero (none recorded), shown as such in the trace."""
    c = ctx(ytd={}, components={"BASIC": inr("50000"), "TDS": inr("0")})
    result = run('YTD("TDS")', c)
    assert result.value == inr("0") and result.steps == ['YTD("TDS") = 0.00 INR (none: 0)']


def test_ec_cmp_20_declared_previous_employment_ytd_is_used() -> None:
    """EC-CMP-20 boundary: a declared previous-employment YTD (supplied by the pack) is used as given."""
    c = ctx(ytd={"TDS": inr("42000.00")})
    assert value('YTD("TDS") + 1000', c) == inr("43000.00")


def test_ec_cmp_21_null_reference_is_typed_failure() -> None:
    """EC-CMP-21: an unavailable reference yields NULL_VALUE, never a crash or a silent zero."""
    error = failure("BASIC * att.late_marks", ctx())
    assert isinstance(error, FormulaError) and error.code == "NULL_VALUE"
    assert failure("COALESCE(att.late_marks, att.half_days)", ctx()).code == "NULL_VALUE"


def test_ec_cmp_21_unknown_percentage_base_at_evaluation() -> None:
    """EC-CMP-21 boundary: an undefined percentage base is UNKNOWN_REFERENCE at evaluation as well."""
    assert failure("UNDEFINED_BASE * 40%", ctx()).code == "UNKNOWN_REFERENCE"
