"""Static analysis and dependency order of formulas (3.3 §4.3–§4.5, §5, §11 Examples C and D).

Edge cases: EC-CMP-01, EC-CMP-03, EC-CMP-07, EC-CMP-19, EC-CMP-21.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.payroll.formula import (
    PROTECTED_ATTRIBUTES,
    CalcMethod,
    ComponentSig,
    FormulaError,
    Kind,
    Stage,
    TableKind,
    TableSig,
    TypeEnv,
    check,
    dependency_order,
    diagnose,
    expand_tags,
    method_formula,
    parse,
)
from app.payroll.formula.types import BOOLEAN, DECIMAL, STRING

COMPONENTS = {
    "BASIC": ComponentSig("INR", Stage.EARNINGS),
    "HRA": ComponentSig("INR", Stage.EARNINGS),
    "DA": ComponentSig("INR", Stage.EARNINGS, optional=True),
    "BONUS_USD": ComponentSig("USD", Stage.EARNINGS),
    "JOINING_BONUS": ComponentSig("INR", Stage.EARNINGS),
    "PF_EMPLOYEE": ComponentSig("INR", Stage.STATUTORY),
    "NET_PAY": ComponentSig("INR", Stage.NET),
}
TAGS = {"OT_BASE": {"BASIC", "HRA"}, "MIXED": {"BASIC", "BONUS_USD"}, "EMPTY": set(), "NETTISH": {"NET_PAY"}}
TABLES = {
    "MEAL_BY_GRADE": TableSig(TableKind.LOOKUP, value_currency="INR"),
    "PT_IN_MH": TableSig(TableKind.BAND, key_currency="INR", value_currency="INR"),
    "TAX_SLAB": TableSig(TableKind.SLAB, key_currency="INR"),
    "FACTORS": TableSig(TableKind.LOOKUP),
}


def env(**overrides: object) -> TypeEnv:
    values: dict[str, object] = {
        "currency": "INR",
        "components": COMPONENTS,
        "tags": TAGS,
        "tables": TABLES,
        "attributes": {"region": STRING, "shift_eligible": BOOLEAN, "site_factor": DECIMAL},
        "component": "SPECIAL",
        "stage": int(Stage.EARNINGS),
    }
    values.update(overrides)
    return TypeEnv(**values)  # type: ignore[arg-type]


def error_code(source: str, type_env: TypeEnv | None = None) -> str:
    with pytest.raises(FormulaError) as info:
        check(source, type_env or env())
    return info.value.code


# -- §4.7 examples ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("BASIC * 40%", Kind.MONEY),
        ("IF(att.present_days >= 20, 1500, 0)", Kind.INTEGER),
        ("MIN(att.present_days * 100, 2000)", Kind.DECIMAL),
        ('SUM_TAG_RATE("OT_BASE") / 26 / emp.standard_hours_per_day', Kind.MONEY),
        ("SWITCH(TRUE, emp.years_of_service >= 10, 3000, emp.years_of_service >= 5, 1500, 0)", Kind.INTEGER),
        ("(BASIC + COALESCE(DA, 0)) / 26", Kind.MONEY),
        ('LOOKUP("MEAL_BY_GRADE", emp.grade, 0)', Kind.MONEY),
        ('IF(emp.is_joiner, INPUT("JOINING_BONUS"), 0)', Kind.MONEY),
    ],
)
def test_section_4_7_examples_type_check(source: str, kind: Kind) -> None:
    """3.3 §4.7: every example formula passes the save-time check for an INR component."""
    analysis = check(source, env())
    assert analysis.ok
    assert analysis.type is not None and analysis.type.kind is kind
    if kind is Kind.MONEY:
        assert analysis.type.currency == "INR"


def test_references_are_collected() -> None:
    analysis = check(
        'BASIC + SUM_TAG("OT_BASE") + RATE("HRA") + INPUT("JOINING_BONUS") + PREV("BASIC") + YTD("BASIC") '
        '+ LOOKUP("MEAL_BY_GRADE", emp.grade, 0) + COALESCE(DA, 0) + UNITS("JOINING_BONUS") * 0 * BASIC',
        env(),
    )
    assert analysis.components == {"BASIC", "DA"}
    assert analysis.tags == {"OT_BASE"}
    assert analysis.rates == {"HRA"}
    assert analysis.inputs == {"JOINING_BONUS"}
    assert analysis.history == {"BASIC"}
    assert analysis.tables == {"MEAL_BY_GRADE"}
    assert analysis.variables == {"emp.grade"}
    assert {"SUM_TAG", "RATE", "INPUT", "PREV", "YTD", "LOOKUP", "COALESCE", "UNITS"} <= analysis.functions
    # SUM_TAG depends on every component with the tag; optional DA and wage inputs are not dependencies.
    assert analysis.dependencies == {"BASIC", "HRA"}
    payload = analysis.to_dict()
    assert json.loads(json.dumps(payload))["dependencies"] == ["BASIC", "HRA"]


def test_check_collects_every_error_and_raises_the_first() -> None:
    with pytest.raises(FormulaError) as info:
        check("UNKNOWN_A + att.nope + BASIC * BASIC", env())
    found = [(issue.code, issue.position) for issue in info.value.issues]
    assert info.value.code == "UNKNOWN_REFERENCE" and info.value.position == 0
    assert found == [("UNKNOWN_REFERENCE", 0), ("UNKNOWN_REFERENCE", 12), ("TYPE_MISMATCH", 23)]


def test_diagnose_never_raises() -> None:
    analysis = diagnose("BASIC +", env())
    assert not analysis.ok and analysis.errors[0].code == "PARSE_ERROR" and analysis.type is None
    analysis = diagnose("BASIC + 5%", env())
    assert [e.code for e in analysis.errors] == ["TYPE_MISMATCH"]


def test_check_accepts_a_parsed_tree() -> None:
    assert check(parse("BASIC * 2"), env()).type.kind is Kind.MONEY  # type: ignore[union-attr]


# -- unknown references ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    ["UNKNOWN_COMPONENT", "att.unknown", "foo.bar", "emp.attr.undeclared", 'LOOKUP("NO_SUCH_TABLE", emp.grade, 0)',
     'SUM_TAG("NO_SUCH_TAG")', 'RATE("NOPE")', 'INPUT("NOPE")', 'PREV("NOPE")', "NO_FN(1)", "emp.salary"],
)
def test_unknown_references(source: str) -> None:
    assert error_code(source) == "UNKNOWN_REFERENCE"


def test_unknown_reference_suggests_a_close_match() -> None:
    analysis = diagnose("BASC * 2", env())
    assert "did you mean BASIC" in analysis.errors[0].message


def test_ec_cmp_21_undefined_percentage_base_is_typed_failure() -> None:
    """EC-CMP-21: a percentage of an undefined base is UNKNOWN_REFERENCE, never a silent zero."""
    assert error_code("UNDEFINED_BASE * 40%") == "UNKNOWN_REFERENCE"


def test_ec_cmp_21_defined_percentage_base_passes() -> None:
    """EC-CMP-21 boundary: the same formula with a defined base is a money amount."""
    assert check("BASIC * 40%", env()).type.currency == "INR"  # type: ignore[union-attr]


# -- types -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    ["BASIC + 5%", "BASIC + att.paid_days", "IF(BASIC, 1, 0)", "BASIC * BASIC", "1 / BASIC", '"G3" + 1',
     "emp.hire_date + 1", 'emp.grade < "G3"', "NOT BASIC", "emp.is_joiner", "TRUE + 1", 'LOOKUP("MEAL_BY_GRADE", 3, 0)',
     'SLAB("PT_IN_MH", BASIC)', 'BAND("TAX_SLAB", BASIC)', "MIN(TRUE, FALSE)", 'IN(emp.grade, "G1", 2)',
     "IF(TRUE, BASIC, att.paid_days)", "ROUND(BASIC, 1.5)", "YEAR(BASIC)", "DATE(2026, 1, 1)", "emp.grade",
     'SLAB("TAX_SLAB", att.paid_days)', "BASIC > att.paid_days", "COALESCE(BASIC, att.paid_days)",
     "AND(att.paid_days, TRUE)"],
)
def test_type_mismatch(source: str) -> None:
    """3.3 §4.4: invalid type combinations are TYPE_MISMATCH at save time."""
    assert error_code(source) == "TYPE_MISMATCH"


def test_money_arithmetic_rules() -> None:
    assert check("BASIC * 1.5", env()).type.kind is Kind.MONEY  # type: ignore[union-attr]
    assert check("2 * BASIC", env()).type.kind is Kind.MONEY  # type: ignore[union-attr]
    assert check("BASIC / 2", env()).type.kind is Kind.MONEY  # type: ignore[union-attr]
    ratio = check("HRA / BASIC", env(expect="NUMBER")).type
    assert ratio is not None and ratio.kind is Kind.DECIMAL
    assert check("period.number_in_tax_year + 1", env(expect="NUMBER")).type.kind is Kind.INTEGER  # type: ignore[union-attr]
    assert check("period.number_in_tax_year / 2", env(expect="NUMBER")).type.kind is Kind.DECIMAL  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "source",
    ["BASIC + 500", "500 - BASIC", "BASIC > 1000", "1000 <= BASIC", "IF(TRUE, BASIC, 0)", "IF(TRUE, 0, BASIC)",
     "MIN(BASIC, 2000)", "MAX(0, BASIC, 1.5)", "CLAMP(BASIC, 0, 5000)", "COALESCE(DA, 0)",
     "SWITCH(emp.grade, \"G1\", BASIC, \"G2\", 100, 0)", "DIVIDE(BASIC, att.paid_days, 0)", "FLOOR(BASIC, 100)",
     "BASIC + IF(emp.is_joiner, 500, 0)", 'BAND("PT_IN_MH", 15000)', "BASIC = 0"],
)
def test_literal_promotion_to_money(source: str) -> None:
    """Decided rule: a plain number literal meeting MONEY is an amount in that currency."""
    result = check(source, env(expect="ANY")).type
    assert result is not None and result.kind in (Kind.MONEY, Kind.BOOLEAN)


def test_literal_promotion_does_not_apply_to_percent_or_variables() -> None:
    assert error_code("BASIC + 5%") == "TYPE_MISMATCH"
    assert error_code("BASIC + att.paid_days") == "TYPE_MISMATCH"
    assert error_code("BASIC + (1 + 2)") == "TYPE_MISMATCH"
    assert error_code("MIN(BASIC, att.paid_days)") == "TYPE_MISMATCH"


def test_decimal_result_is_an_amount_for_money_components() -> None:
    """A formula whose final type is DECIMAL is allowed for a money component (component currency)."""
    assert check("att.paid_days * 100", env()).type.kind is Kind.DECIMAL  # type: ignore[union-attr]
    assert error_code('emp.grade = "G3"') == "TYPE_MISMATCH"


def test_expectations() -> None:
    assert check('emp.grade = "G3"', env(expect="BOOLEAN")).ok
    assert error_code("BASIC", env(expect="BOOLEAN")) == "TYPE_MISMATCH"
    assert error_code("BASIC", env(expect="NUMBER")) == "TYPE_MISMATCH"
    assert check("emp.hire_date", env(expect="ANY")).type.kind is Kind.DATE  # type: ignore[union-attr]


def test_static_division_by_zero_and_invalid_arguments() -> None:
    assert error_code("BASIC / 0") == "DIVISION_BY_ZERO"
    assert error_code("FLOOR(BASIC, 0)") == "DIVISION_BY_ZERO"
    assert error_code("CEIL(BASIC, -1)") == "INVALID_ARGUMENT"
    assert error_code("ROUND(BASIC, 13)") == "INVALID_ARGUMENT"
    assert error_code("CLAMP(BASIC, 10, 5)") == "INVALID_ARGUMENT"
    assert check("DIVIDE(BASIC, 0, 0)", env()).ok


def test_table_kind_must_match_function() -> None:
    analysis = diagnose('BAND("TAX_SLAB", BASIC)', env())
    assert analysis.errors[0].code == "TYPE_MISMATCH" and "SLAB table" in analysis.errors[0].message
    assert check('SLAB("TAX_SLAB", BASIC)', env()).type.currency == "INR"  # type: ignore[union-attr]
    assert check('LOOKUP("FACTORS", emp.grade, 1) * BASIC', env()).type.kind is Kind.MONEY  # type: ignore[union-attr]


def test_custom_attributes_use_declared_types() -> None:
    assert check('emp.attr.region = "NORTH"', env(expect="BOOLEAN")).ok
    assert check("IF(emp.attr.shift_eligible, 500, 0)", env()).ok
    assert check("BASIC * emp.attr.site_factor", env()).type.kind is Kind.MONEY  # type: ignore[union-attr]


# -- EC-CMP-07 and Example D: currencies --------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    ["BASIC + BONUS_USD", "BONUS_USD - BASIC", "BASIC > BONUS_USD", "MIN(BASIC, BONUS_USD)",
     "IF(TRUE, BASIC, BONUS_USD)", "COALESCE(DA, BONUS_USD)", "BASIC / BONUS_USD", 'SUM_TAG("MIXED")',
     "BONUS_USD", "BONUS_USD * 2", 'SLAB("TAX_SLAB", BONUS_USD)', 'IN(BASIC, BONUS_USD)'],
)
def test_ec_cmp_07_money_of_two_currencies_is_currency_mismatch(source: str) -> None:
    """EC-CMP-07: combining INR and USD (or paying USD in an INR component) fails with CURRENCY_MISMATCH."""
    assert error_code(source) == "CURRENCY_MISMATCH"


def test_ec_cmp_07_same_currency_passes() -> None:
    """EC-CMP-07 boundary: the same operations within one currency pass; USD component in USD is fine."""
    assert check("BASIC + HRA", env()).ok
    assert check("BONUS_USD * 2", env(currency="USD")).type.currency == "USD"  # type: ignore[union-attr]
    assert check("HRA / BASIC", env(expect="NUMBER")).ok


def test_example_d_lookup_in_other_currency_is_currency_mismatch() -> None:
    """3.3 §11 Example D: AED pay group, LOOKUP of a USD table -> CURRENCY_MISMATCH (AED vs USD)."""
    aed = TypeEnv("AED", components={}, tables={"BASIC_USD": TableSig(TableKind.LOOKUP, value_currency="USD")},
                  component="BASIC", stage=int(Stage.EARNINGS))
    with pytest.raises(FormulaError) as info:
        check('LOOKUP("BASIC_USD", emp.grade, 0)', aed)
    assert info.value.code == "CURRENCY_MISMATCH"
    assert "AED vs USD" in info.value.message


def test_example_d_lookup_in_pay_currency_passes() -> None:
    """3.3 §11 Example D boundary: the same table held in AED is accepted."""
    aed = TypeEnv("AED", tables={"BASIC_AED": TableSig(TableKind.LOOKUP, value_currency="AED")})
    assert check('LOOKUP("BASIC_AED", emp.grade, 0)', aed).type.currency == "AED"  # type: ignore[union-attr]


# -- stages and self-reference ---------------------------------------------------------------------


@pytest.mark.parametrize("source", ["NET_PAY * 10%", 'RATE("PF_EMPLOYEE")', 'SUM_TAG("NETTISH")', "BASIC + NET_PAY"])
def test_stage_violation(source: str) -> None:
    """3.3 §5.3: an earning cannot reference a later stage (statutory, net)."""
    with pytest.raises(FormulaError) as info:
        check(source, env())
    assert info.value.code == "STAGE_VIOLATION"
    assert info.value.path is not None and info.value.path[0] == "SPECIAL"


def test_same_and_earlier_stages_are_allowed() -> None:
    assert check("BASIC + HRA", env()).ok
    net_env = env(component="NET_ROUNDING", stage=int(Stage.NET))
    assert check("NET_PAY - BASIC + PF_EMPLOYEE", net_env).ok


def test_self_reference_is_cycle() -> None:
    with pytest.raises(FormulaError) as info:
        check("BASIC * 2", env(component="BASIC"))
    assert info.value.code == "CYCLE_DETECTED" and info.value.path == ["BASIC", "BASIC"]
    assert error_code('SUM_TAG("OT_BASE")', env(component="HRA")) == "CYCLE_DETECTED"


# -- EC-CMP-19: protected attributes ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["gender", "religion", "caste", "marital_status", "nationality", "date_of_birth"],
)
def test_ec_cmp_19_protected_attribute_rejected(name: str) -> None:
    """EC-CMP-19: company formulas cannot read protected attributes, directly or as custom attributes."""
    assert error_code(f"IF(emp.{name} = emp.{name}, 1, 0)") == "PROTECTED_ATTRIBUTE"
    attrs = {name: STRING}
    assert error_code(f'IF(emp.attr.{name} = "X", 1, 0)', env(attributes=attrs)) == "PROTECTED_ATTRIBUTE"
    assert name in PROTECTED_ATTRIBUTES


@pytest.mark.parametrize("name", ["sex", "gender_identity", "ethnicity", "race", "dob", "birth_year", "age"])
def test_ec_cmp_19_protected_synonyms_cannot_be_copied_into_custom_attributes(name: str) -> None:
    """EC-CMP-19: the rule cannot be bypassed by storing protected data under another custom name."""
    attrs = {name: STRING}
    assert error_code(f'IF(emp.attr.{name} = "X", 1, 0)', env(attributes=attrs)) == "PROTECTED_ATTRIBUTE"
    assert check(f'IF(emp.attr.{name} = "X", 1, 0)', env(attributes=attrs, allow_protected=True)).ok


def test_ec_cmp_19_country_pack_may_read_protected_attributes() -> None:
    """EC-CMP-19 boundary: allow_protected (country packs) may use age-banded or nationality rules."""
    pack = env(allow_protected=True)
    assert check('IF(emp.nationality = "IN", 1, 0)', pack).ok
    assert check("IF(YEARS_BETWEEN(emp.date_of_birth, period.end) >= 58, 0, BASIC * 12%)", pack).ok


def test_ec_cmp_19_ordinary_employee_data_is_allowed() -> None:
    """EC-CMP-19 boundary: grade, employment type and custom attributes stay usable."""
    assert check('IF(emp.grade = "G3" AND emp.employment_type = "PERMANENT", 500, 0)', env()).ok


# -- EC-CMP-03: NULL warnings ----------------------------------------------------------------------


def test_ec_cmp_03_switchable_variable_without_coalesce_warns() -> None:
    """EC-CMP-03: att.late_marks is NULL when attendance is OFF; using it without COALESCE warns on save."""
    analysis = check("att.late_marks * 100", env())
    assert analysis.ok
    assert [w.code for w in analysis.warnings] == ["NULL_VALUE"]
    assert "payroll.attendance.enabled" in analysis.warnings[0].message
    assert analysis.warnings[0].severity == "WARNING"
    assert analysis.warnings[0].position == 0


@pytest.mark.parametrize("source", ["ot.hours_total * 2", "IF(att.present_days >= 20, 1500, 0)", "task.amount",
                                    "DA + BASIC", 'emp.grade = "G1"'])
def test_ec_cmp_03_other_nullable_sources_warn(source: str) -> None:
    analysis = check(source, env(expect="ANY"))
    assert any(w.code == "NULL_VALUE" for w in analysis.warnings)


@pytest.mark.parametrize("source", ["COALESCE(att.late_marks, 0) * 100", "att.paid_days * 100",
                                    "att.lop_days + 1", "COALESCE(DA, 0) + BASIC", "BASIC"])
def test_ec_cmp_03_coalesce_or_always_available_does_not_warn(source: str) -> None:
    """EC-CMP-03 boundary: COALESCE removes the warning; the day ledger is always available."""
    assert check(source, env()).warnings == ()


def test_empty_tag_warns() -> None:
    analysis = check('SUM_TAG("EMPTY")', env())
    assert [w.code for w in analysis.warnings] == ["EMPTY_TAG"]


# -- §5: dependency order --------------------------------------------------------------------------


def test_ec_cmp_01_example_c_cycle_path() -> None:
    """EC-CMP-01 / 3.3 §11 Example C: HRA = FIXED_ALLOWANCE * 10%, balancing FIXED_ALLOWANCE reads HRA."""
    deps = {"BASIC": set(), "HRA": {"FIXED_ALLOWANCE"}, "FIXED_ALLOWANCE": {"BASIC", "HRA"}}
    stages = {code: 4 for code in deps}
    with pytest.raises(FormulaError) as info:
        dependency_order(deps, stages)
    assert info.value.code == "CYCLE_DETECTED"
    assert info.value.path == ["HRA", "FIXED_ALLOWANCE", "HRA"]
    assert "HRA → FIXED_ALLOWANCE → HRA" in info.value.message


def test_ec_cmp_01_section_5_cycle_path_from_focus() -> None:
    """EC-CMP-01 / 3.3 §5.4: BASIC → HRA → FIXED_ALLOWANCE → BASIC, reported from the component being saved."""
    deps = {"HRA": {"BASIC"}, "FIXED_ALLOWANCE": {"HRA"}, "BASIC": {"FIXED_ALLOWANCE"}}
    stages = dict.fromkeys(deps, 4)
    with pytest.raises(FormulaError) as info:
        dependency_order(deps, stages, focus="BASIC")
    assert info.value.path == ["BASIC", "FIXED_ALLOWANCE", "HRA", "BASIC"]
    with pytest.raises(FormulaError) as again:
        dependency_order(deps, stages, focus="HRA")
    assert again.value.path == ["HRA", "BASIC", "FIXED_ALLOWANCE", "HRA"]


def test_ec_cmp_01_self_cycle_and_cycle_behind_a_component() -> None:
    with pytest.raises(FormulaError) as info:
        dependency_order({"A": {"A"}}, {"A": 4})
    assert info.value.path == ["A", "A"]
    with pytest.raises(FormulaError) as behind:
        dependency_order({"Z": {"B"}, "B": {"C"}, "C": {"B"}}, dict.fromkeys("ZBC", 4), focus="Z")
    assert behind.value.path == ["B", "C", "B"]


def test_ec_cmp_01_acyclic_structure_orders_without_error() -> None:
    """EC-CMP-01 boundary: the same structure without the HRA -> FIXED_ALLOWANCE edge is accepted."""
    deps = {"BASIC": set(), "HRA": {"BASIC"}, "FIXED_ALLOWANCE": {"BASIC", "HRA"}}
    assert dependency_order(deps, dict.fromkeys(deps, 4)) == ["BASIC", "HRA", "FIXED_ALLOWANCE"]


def test_order_groups_by_stage_and_breaks_ties_by_code() -> None:
    deps = {
        "TAX": {"GROSS_BASE"},
        "PF_EMPLOYEE": {"BASIC"},
        "GROSS_BASE": {"BASIC", "HRA"},
        "HRA": {"BASIC"},
        "CONVEYANCE": set(),
        "BASIC": set(),
        "NET_ROUNDING": {"TAX", "PF_EMPLOYEE"},
    }
    stages = {"BASIC": 4, "HRA": 4, "CONVEYANCE": 4, "GROSS_BASE": 4, "PF_EMPLOYEE": 6, "TAX": 7,
              "NET_ROUNDING": 9, "EMPLOYER_GRATUITY": 10}
    order = dependency_order(deps, stages)
    assert order == ["BASIC", "CONVEYANCE", "HRA", "GROSS_BASE", "PF_EMPLOYEE", "TAX", "NET_ROUNDING",
                     "EMPLOYER_GRATUITY"]
    reversed_deps = dict(reversed(list(deps.items())))
    assert dependency_order(reversed_deps, dict(reversed(list(stages.items())))) == order


def test_dependency_on_later_stage_is_stage_violation() -> None:
    with pytest.raises(FormulaError) as info:
        dependency_order({"BONUS": {"NET_PAY"}, "NET_PAY": set()}, {"BONUS": 4, "NET_PAY": 9})
    assert info.value.code == "STAGE_VIOLATION" and info.value.path == ["BONUS", "NET_PAY"]


def test_dependency_without_stage_is_unknown_reference() -> None:
    with pytest.raises(FormulaError) as info:
        dependency_order({"HRA": {"MISSING"}}, {"HRA": 4})
    assert info.value.code == "UNKNOWN_REFERENCE" and info.value.path == ["HRA", "MISSING"]


def test_expand_tags_makes_sum_tag_depend_on_every_tagged_component() -> None:
    """3.3 §5.1: SUM_TAG("X") depends on every component tagged X."""
    deps = expand_tags({"OT_RATE": set(), "BASIC": set()}, {"OT_RATE": {"OT_BASE"}},
                       {"OT_BASE": {"BASIC", "DA"}})
    assert deps == {"OT_RATE": {"BASIC", "DA"}, "BASIC": set()}
    assert dependency_order(deps, {"OT_RATE": 4, "BASIC": 4, "DA": 4}) == ["BASIC", "DA", "OT_RATE"]
    with pytest.raises(FormulaError) as info:
        expand_tags({}, {"X": {"NOPE"}}, {})
    assert info.value.code == "UNKNOWN_REFERENCE"


def test_analysis_dependencies_feed_dependency_order() -> None:
    """End to end: analyses of a structure's formulas give the graph; Example C via check()."""
    sigs = {code: ComponentSig("INR", 4) for code in ("BASIC", "HRA", "FIXED_ALLOWANCE")}
    formulas = {"BASIC": "ctc.monthly * 50%", "HRA": "FIXED_ALLOWANCE * 10%",
                "FIXED_ALLOWANCE": "ctc.monthly - BASIC - HRA"}
    deps = {code: set(check(text, TypeEnv("INR", components=sigs, component=code, stage=4)).dependencies)
            for code, text in formulas.items()}
    with pytest.raises(FormulaError) as info:
        dependency_order(deps, dict.fromkeys(formulas, 4), focus="HRA")
    assert info.value.path == ["HRA", "FIXED_ALLOWANCE", "HRA"]


# -- §3: calculation methods -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        ("PERCENT_OF", {"percent": Decimal("40"), "bases": ["BASIC"]}, "BASIC * 40%"),
        ("PERCENT_OF", {"percent": Decimal("12.5"), "bases": ["BASIC", "DA"]}, "(BASIC + DA) * 12.5%"),
        ("PERCENT_OF", {"percent": Decimal("50"), "tag": "OT_BASE"}, 'SUM_TAG("OT_BASE") * 50%'),
        ("FORMULA", {"formula": "if(att.present_days >= 20, 1500, 0)"}, "IF(att.present_days >= 20, 1500, 0)"),
        ("RATE_X_UNITS", {"rate": "ot.hourly_rate * 1.5", "units": "ot.hours_workday"},
         "ot.hourly_rate * 1.5 * ot.hours_workday"),
        ("RATE_X_UNITS", {"rate": "HOURLY_RATE", "units": "att.payable_hours + 2"},
         "HOURLY_RATE * (att.payable_hours + 2)"),
        ("SLAB", {"table": "PT_IN_MH", "base": 'SUM_TAG("OT_BASE")', "progressive": False},
         'BAND("PT_IN_MH", SUM_TAG("OT_BASE"))'),
        ("SLAB", {"table": "TAX_SLAB", "base": "BASIC * 12"}, 'SLAB("TAX_SLAB", BASIC * 12)'),
        ("INPUT", {"code": "BONUS"}, 'INPUT("BONUS")'),
        ("BALANCING", {"others": ["BASIC", "HRA", "EMPLOYER_PF"], "own": "FIXED_ALLOWANCE"},
         "ctc.monthly - (BASIC + HRA + EMPLOYER_PF)"),
        ("FIXED", {}, None),
        ("SYSTEM", {}, None),
    ],
)
def test_section_3_calculation_methods_compile_to_formulas(method: str, params: dict[str, object],
                                                           expected: str | None) -> None:
    """3.3 §3: every computed method is a canonical formula in the same language."""
    assert method_formula(method, params) == expected


def test_section_3_percent_of_checks_against_the_structure() -> None:
    formula = method_formula(CalcMethod.PERCENT_OF, {"percent": Decimal("40"), "bases": ["BASIC"]})
    assert formula is not None and check(formula, env()).type.currency == "INR"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("PERCENT_OF", {"percent": Decimal("40")}),
        ("PERCENT_OF", {"percent": Decimal("40"), "bases": ["BASIC"], "tag": "OT_BASE"}),
        ("PERCENT_OF", {"percent": 0.4, "bases": ["BASIC"]}),
        ("PERCENT_OF", {"percent": Decimal("-1"), "bases": ["BASIC"]}),
        ("PERCENT_OF", {"percent": Decimal("40"), "bases": ["basic"]}),
        ("PERCENT_OF", {"percent": Decimal("40"), "bases": ["BASIC", "BASIC"]}),
        ("PERCENT_OF", {"percent": Decimal("40"), "tag": "bad tag"}),
        ("RATE_X_UNITS", {"rate": "", "units": "1"}),
        ("SLAB", {"table": "T", "base": " "}),
        ("INPUT", {"code": "__import__"}),
        ("BALANCING", {"others": []}),
        ("BALANCING", {"others": ["BASIC", "FIXED_ALLOWANCE"], "own": "FIXED_ALLOWANCE"}),
        ("BALANCING", {}),
        ("NOT_A_METHOD", {}),
    ],
)
def test_section_3_invalid_method_parameters(method: str, params: dict[str, object]) -> None:
    with pytest.raises(FormulaError) as info:
        method_formula(method, params)
    assert info.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("RATE_X_UNITS", {"rate": "1) + (2", "units": "3"}),
        ("RATE_X_UNITS", {"rate": "HOURLY_RATE", "units": "2) * (BASIC"}),
        ("SLAB", {"table": "TAX_SLAB", "base": "BASIC), (1"}),
        ("BALANCING", {"others": ["BASIC"], "total": "ctc.monthly) * (2"}),
    ],
)
def test_section_3_method_parameters_cannot_be_spliced(method: str, params: dict[str, object]) -> None:
    """A parameter must be one complete expression; it can never rewrite the surrounding template."""
    with pytest.raises(FormulaError) as info:
        method_formula(method, params)
    assert info.value.code == "PARSE_ERROR"


def test_section_3_formula_method_rejects_injection() -> None:
    """EC-CMP-08 via the FORMULA method: the text still goes through the tokenizer."""
    with pytest.raises(FormulaError) as info:
        method_formula("FORMULA", {"formula": '__import__("os")'})
    assert info.value.code == "PARSE_ERROR"


def test_ec_cmp_01_example_c_through_the_balancing_method() -> None:
    """EC-CMP-01 / Example C: balancing FIXED_ALLOWANCE reads HRA, and HRA = FIXED_ALLOWANCE * 10%."""
    sigs = {code: ComponentSig("INR", 4) for code in ("BASIC", "HRA", "FIXED_ALLOWANCE")}
    formulas = {
        "BASIC": "ctc.monthly * 50%",
        "HRA": method_formula("PERCENT_OF", {"percent": Decimal("10"), "bases": ["FIXED_ALLOWANCE"]}),
        "FIXED_ALLOWANCE": method_formula("BALANCING", {"others": ["BASIC", "HRA"], "own": "FIXED_ALLOWANCE"}),
    }
    deps = {code: set(check(str(text), TypeEnv("INR", components=sigs, component=code, stage=4)).dependencies)
            for code, text in formulas.items()}
    assert deps["FIXED_ALLOWANCE"] == {"BASIC", "HRA"}
    with pytest.raises(FormulaError) as info:
        dependency_order(deps, dict.fromkeys(formulas, 4))
    assert info.value.code == "CYCLE_DETECTED" and info.value.path == ["HRA", "FIXED_ALLOWANCE", "HRA"]
