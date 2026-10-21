"""Tokenizer and parser of the formula language (3.3 §4.1, §4.6; EC-CMP-08, EC-CMP-09)."""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

import pytest

from app.payroll.formula import FormulaError, MAX_DEPTH, MAX_LENGTH, MAX_NODES, node_count, parse, render, shape
from app.payroll.formula.ast import (
    Binary,
    Boolean,
    Call,
    Compare,
    ComponentRef,
    DateLiteral,
    Logical,
    Number,
    String,
    Unary,
    VariableRef,
    depth,
)
from app.payroll.formula.lexer import TokenKind, tokenize
from app.payroll.formula.parser import is_valid_component_code


def code_of(source: str) -> str:
    with pytest.raises(FormulaError) as info:
        parse(source)
    return info.value.code


# -- tokens and literals ---------------------------------------------------------------------------


def test_tokenize_kinds_and_positions() -> None:
    tokens = tokenize('BASIC * 40% + att.paid_days <> "G3"')
    kinds = [t.kind for t in tokens]
    assert kinds == [
        TokenKind.NAME, TokenKind.OPERATOR, TokenKind.NUMBER, TokenKind.PERCENT, TokenKind.OPERATOR,
        TokenKind.NAME, TokenKind.OPERATOR, TokenKind.STRING, TokenKind.EOF,
    ]
    assert (tokens[0].pos, tokens[0].end) == (0, 5)
    assert tokens[6].text == "<>"
    assert tokens[7].text == "G3"


def test_percent_literal_is_exact_decimal() -> None:
    node = parse("40%")
    assert isinstance(node, Number)
    assert node.value == Decimal("0.40") and str(node.value) == "0.40"
    assert node.percent and node.text == "40%"
    assert parse("12.5%").value == Decimal("0.125")  # type: ignore[union-attr]


def test_negative_literal_is_folded_into_number() -> None:
    node = parse("-5")
    assert isinstance(node, Number) and node.value == Decimal("-5") and (node.pos, node.end) == (0, 2)
    unary = parse("-BASIC")
    assert isinstance(unary, Unary) and unary.op == "-"


def test_keywords_and_functions_are_case_insensitive() -> None:
    assert shape(parse("if(true, 1, 0)")) == shape(parse("IF(TRUE, 1, 0)"))
    assert shape(parse("a.b = 1 and not x.y or false")) == shape(parse("a.b = 1 AND NOT x.y OR FALSE"))
    node = parse("Min(1, 2)")
    assert isinstance(node, Call) and node.name == "MIN"


def test_component_codes_must_be_upper_snake() -> None:
    assert isinstance(parse("FIXED_ALLOWANCE"), ComponentRef)
    assert code_of("basic") == "PARSE_ERROR"
    assert code_of("Basic") == "PARSE_ERROR"
    assert is_valid_component_code("OT_WORKDAY")
    assert not is_valid_component_code("IF")
    assert not is_valid_component_code("A__B")
    assert not is_valid_component_code("lower")


def test_namespaced_variables_must_be_lower_case() -> None:
    node = parse("emp.attr.region")
    assert isinstance(node, VariableRef) and node.namespace == "emp"
    assert code_of("ATT.paid_days") == "PARSE_ERROR"
    assert code_of("att.Paid_days") == "PARSE_ERROR"
    assert code_of("a.b.c.d") == "PARSE_ERROR"
    assert code_of("att.") == "PARSE_ERROR"


def test_date_literal() -> None:
    node = parse("DATE(2026, 9, 17)")
    assert isinstance(node, DateLiteral) and node.value == date(2026, 9, 17)
    assert node_count(node) == 1
    assert isinstance(parse("date(YEAR(period.end), 1, 1)"), Call)


@pytest.mark.parametrize("source", ["DATE(2026, 2, 30)", "DATE(2026, 13, 1)", "DATE(0, 1, 1)",
                                    "DATE(99999999999999999999, 1, 1)"])
def test_invalid_date_literal_is_parse_error(source: str) -> None:
    assert code_of(source) == "PARSE_ERROR"


def test_strings_and_booleans() -> None:
    node = parse('emp.grade = "G3"')
    assert isinstance(node, Compare) and isinstance(node.right, String) and node.right.value == "G3"
    assert isinstance(parse("TRUE"), Boolean)
    assert code_of('"unterminated') == "PARSE_ERROR"
    assert code_of('"back\\slash"') == "PARSE_ERROR"


# -- grammar ---------------------------------------------------------------------------------------


def test_precedence_follows_the_grammar() -> None:
    node = parse("A + B * C > 10 AND D = 1 OR E = 2")
    assert isinstance(node, Logical) and node.op == "OR"
    left = node.operands[0]
    assert isinstance(left, Logical) and left.op == "AND"
    comparison = left.operands[0]
    assert isinstance(comparison, Compare) and comparison.op == ">"
    additive = comparison.left
    assert isinstance(additive, Binary) and additive.op == "+"
    assert isinstance(additive.right, Binary) and additive.right.op == "*"


def test_arithmetic_is_left_associative() -> None:
    node = parse("A - B - C")
    assert isinstance(node, Binary) and isinstance(node.left, Binary)
    assert render(node) == "A - B - C"
    assert render(parse("A - (B - C)")) == "A - (B - C)"


def test_logical_chains_and_call_forms_are_one_node() -> None:
    chain = parse("x.a AND x.b AND x.c")
    assert isinstance(chain, Logical) and len(chain.operands) == 3
    call = parse("AND(x.a, x.b, x.c)")
    assert shape(call) == shape(chain)
    assert code_of("AND(x.a)") == "PARSE_ERROR"


def test_not_applies_to_the_next_primary() -> None:
    node = parse("NOT x.a AND x.b")
    assert isinstance(node, Logical) and isinstance(node.operands[0], Unary)
    assert code_of("NOT NOT x.a") == "PARSE_ERROR"


@pytest.mark.parametrize(
    "source",
    ["", "   ", "1 +", "(1", "1)", "()", "1 2", "A < B < C", "1 = = 2", "1 == 2", "MIN(1, , 2)", "MIN(1, 2",
     "%5", "5 + + 5", "--5", "BASIC.x", "1.", "1.2.3", "2x", "1e5", "MIN 1", "IF", "NULL", "AND", "TRUE(1)"],
)
def test_syntax_errors_are_parse_error(source: str) -> None:
    assert code_of(source) == "PARSE_ERROR"


def test_parse_error_carries_position() -> None:
    with pytest.raises(FormulaError) as info:
        parse("BASIC + # 2")
    assert info.value.code == "PARSE_ERROR" and info.value.position == 8


def test_thousands_separator_is_rejected() -> None:
    assert code_of("MIN(2,500, 3)") == "PARSE_ERROR"
    assert isinstance(parse("MIN(2, 500)"), Call)


@pytest.mark.parametrize("source", ["MIN(BASIC,2,500)", "MAX(1,00,000, 2)", "MIN(BASIC,25,000.50)",
                                    "CLAMP(BASIC,1,100)", "MAX(12,34,567, 1)"])
def test_grouped_numbers_are_rejected_wherever_they_appear(source: str) -> None:
    """2,500 and 1,00,000 would silently become several arguments, so they are a PARSE_ERROR."""
    assert code_of(source) == "PARSE_ERROR"


@pytest.mark.parametrize(
    ("source", "canonical"),
    [
        ("DATE(2026,2,28)", "DATE(2026, 2, 28)"),
        ("IF(att.present_days >= 20,1500,0)", "IF(att.present_days >= 20, 1500, 0)"),
        ("CLAMP(BASIC,0,100)", "CLAMP(BASIC, 0, 100)"),
        ("SWITCH(emp.grade,\"G1\",10,\"G2\",20,30)", 'SWITCH(emp.grade, "G1", 10, "G2", 20, 30)'),
        ("MIN(1.5,100)", "MIN(1.5, 100)"),
        ("MIN(2026,123)", "MIN(2026, 123)"),
        ("ROUND(BASIC,2)", "ROUND(BASIC, 2)"),
    ],
)
def test_grammar_allows_arguments_without_spaces(source: str, canonical: str) -> None:
    """3.3 §4.1: expression ("," expression)* needs no whitespace when no grouped number can be read."""
    assert render(parse(source)) == canonical


def test_unknown_function_is_unknown_reference() -> None:
    assert code_of("FOO(1)") == "UNKNOWN_REFERENCE"
    assert code_of("eval(1)") == "UNKNOWN_REFERENCE"
    assert code_of("IFF(TRUE, 1, 0)") == "UNKNOWN_REFERENCE"


@pytest.mark.parametrize(
    "source",
    ["ABS()", "ABS(1, 2)", "MIN(1)", "IF(TRUE, 1)", "ROUND(1)", "ROUND(1, 2, \"HALF_UP\", 4)", "CLAMP(1, 2)",
     "DIVIDE(1, 2)", "SWITCH(1, 1, 2)", "SWITCH(1, 1, 2, 3, 4)", "IN(1)", "COALESCE(1)", "DATE(2026, 1)",
     "SLAB(\"T\")", "LOOKUP(\"T\", \"k\")", "YEAR()", "DAYS_BETWEEN(period.end)"],
)
def test_arity_is_checked(source: str) -> None:
    assert code_of(source) == "PARSE_ERROR"


@pytest.mark.parametrize(
    "source",
    ["SUM_TAG(OT_BASE)", "RATE(BASIC)", 'SUM_TAG("ot_base")', "SLAB(T, 1)", 'INPUT("a b")', 'ROUND(1, 2, "NEAREST")',
     'ROUND(1, 2, MODE)', 'RATE("A__B")'],
)
def test_quoted_code_arguments_are_required(source: str) -> None:
    assert code_of(source) == "PARSE_ERROR"


def test_rounding_mode_is_canonical_upper_case() -> None:
    node = parse('ROUND(BASIC, 0, "half_even")')
    assert isinstance(node, Call) and isinstance(node.args[2], String) and node.args[2].value == "HALF_EVEN"


def test_tree_is_immutable_and_has_positions() -> None:
    node = parse("BASIC * 40%")
    assert isinstance(node, Binary)
    assert (node.pos, node.end) == (0, 11)
    assert (node.right.pos, node.right.end) == (8, 11)
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.op = "+"  # type: ignore[misc]


@pytest.mark.parametrize(
    "source",
    ["BASIC * 40%", "IF(att.present_days >= 20, 1500, 0)", "MIN(att.present_days * 100, 2000)",
     'SUM_TAG_RATE("OT_BASE") / 26 / emp.standard_hours_per_day',
     "SWITCH(TRUE, emp.years_of_service >= 10, 3000, emp.years_of_service >= 5, 1500, 0)",
     "(BASIC + COALESCE(DA, 0)) / 26", 'LOOKUP("MEAL_BY_GRADE", emp.grade, 0)',
     'IF(emp.is_joiner, INPUT("JOINING_BONUS"), 0)', "-(A + B) * -2", "NOT (A > 1) OR B = 2 AND C <> 3",
     "A - -5", "(A = B) = C", 'ROUND(A / 3, 2, "HALF_EVEN")', "DATE(2026, 1, 31) < period.end"],
)
def test_render_round_trips(source: str) -> None:
    """3.3 §4.7 examples and tricky precedence render to text that parses to the same tree."""
    tree = parse(source)
    assert shape(parse(render(tree))) == shape(tree)
    shown = parse(render(tree, values=True))
    if "%" in source:
        assert "%" not in render(tree, values=True)
        assert render(shown) == render(tree, values=True)
    else:
        assert shape(shown) == shape(tree)


# -- EC-CMP-08: injection ---------------------------------------------------------------------------


def test_ec_cmp_08_dunder_import_is_parse_error() -> None:
    """EC-CMP-08: __import__("os") is rejected by the tokenizer; nothing is executed."""
    assert code_of('__import__("os")') == "PARSE_ERROR"


@pytest.mark.parametrize(
    "source",
    ["BASIC.__class__", "A__B", "emp.__dict__", "x.__globals__", "__builtins__", "_private",
     "BASIC; DROP TABLE employees", "'; DROP TABLE payroll; --", "1 OR 1=1 --", "<script>alert(1)</script>",
     "os.system(\"rm -rf /\")", "BASIC[0]", "lambda: 1", "{}", "`id`", "$x", "BASIC @ 2", "a\\b", "1 ** 2",
     "exec('x')", "BASIC\x00", "BASIC × 2", "BASIC + 1"],
)
def test_ec_cmp_08_injection_attempts_are_parse_errors(source: str) -> None:
    """EC-CMP-08: code injection (Python, SQL, JavaScript, shell) never parses."""
    assert code_of(source) == "PARSE_ERROR"


def test_ec_cmp_08_unknown_call_is_rejected_not_executed() -> None:
    """EC-CMP-08 boundary: a call to a name that exists in Python is an unknown function, never run."""
    assert code_of('eval("1")') == "UNKNOWN_REFERENCE"
    assert code_of("open(1)") == "UNKNOWN_REFERENCE"


def test_ec_cmp_08_single_underscores_are_allowed() -> None:
    """EC-CMP-08 boundary: ordinary snake_case names still parse."""
    assert isinstance(parse("OT_WORKDAY_PAY"), ComponentRef)
    assert isinstance(parse("emp.attr.site_code"), VariableRef)


# -- EC-CMP-09: limits ------------------------------------------------------------------------------


def test_ec_cmp_09_length_limit_boundary() -> None:
    """EC-CMP-09: 2000 characters are accepted, 2001 are LIMIT_EXCEEDED."""
    ok = "1" + " " * (MAX_LENGTH - 1)
    assert len(ok) == MAX_LENGTH
    assert isinstance(parse(ok), Number)
    assert code_of(ok + " ") == "LIMIT_EXCEEDED"


def _sum(terms: int) -> str:
    return " + ".join(["1"] * terms)


def test_ec_cmp_09_node_limit_boundary() -> None:
    """EC-CMP-09: 500 nodes are accepted, 501 are LIMIT_EXCEEDED."""
    exactly = "-(" + _sum(250) + ")"
    assert node_count(parse(exactly)) == MAX_NODES
    assert code_of("-(" + _sum(250) + " + A)") == "LIMIT_EXCEEDED"
    assert code_of(_sum(251)) == "LIMIT_EXCEEDED"


def test_ec_cmp_09_depth_limit_boundary_for_calls() -> None:
    """EC-CMP-09: 50 levels of nesting are accepted, 51 are LIMIT_EXCEEDED."""
    ok = "ABS(" * (MAX_DEPTH - 1) + "1" + ")" * (MAX_DEPTH - 1)
    assert depth(parse(ok)) == MAX_DEPTH
    deep = "ABS(" * MAX_DEPTH + "1" + ")" * MAX_DEPTH
    assert code_of(deep) == "LIMIT_EXCEEDED"


def test_ec_cmp_09_depth_limit_boundary_for_parentheses() -> None:
    """EC-CMP-09: parentheses count as nesting even though they leave no node."""
    assert isinstance(parse("(" * MAX_DEPTH + "1" + ")" * MAX_DEPTH), Number)
    assert code_of("(" * (MAX_DEPTH + 1) + "1" + ")" * (MAX_DEPTH + 1)) == "LIMIT_EXCEEDED"


def test_ec_cmp_09_long_flat_sum_is_one_level() -> None:
    """EC-CMP-09 boundary: a sum of many components is not "deep"; a + b + c is one level."""
    tree = parse(" + ".join(f"C{i}" for i in range(120)))
    assert depth(tree) == 2
    nested = "1 - (" * 50 + "1" + ")" * 50
    assert code_of(nested) == "LIMIT_EXCEEDED"


def test_ec_cmp_09_number_precision_limit() -> None:
    """EC-CMP-09: literals keep within the 28-digit calculation precision."""
    assert parse("1" * 28).value == Decimal("1" * 28)  # type: ignore[union-attr]
    assert parse("0.000" + "1" * 28).value == Decimal("0.000" + "1" * 28)  # type: ignore[union-attr]
    assert code_of("1" * 29) == "LIMIT_EXCEEDED"
