"""Property-based tests of the formula language (3.3 §4.6: "the parser is fuzz-tested").

* arbitrary text never raises anything but FormulaError (parse and check);
* identifiers containing "__" never parse (EC-CMP-08);
* generated valid expressions render and re-parse to the same tree, and evaluate
  deterministically (same result twice) without raising;
* arithmetic matches the 28-digit Decimal context exactly, and the rounding helpers
  keep their contracts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.payroll.formula import (
    CALC_CONTEXT,
    ComponentSig,
    EvalContext,
    FormulaError,
    MoneyValue,
    TypeEnv,
    check,
    evaluate,
    parse,
    render,
    shape,
)

FUZZ = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
ALLOWED_RUNTIME_CODES = {
    "TYPE_MISMATCH", "CURRENCY_MISMATCH", "DIVISION_BY_ZERO", "NULL_VALUE", "LIMIT_EXCEEDED", "INVALID_ARGUMENT",
    "UNKNOWN_REFERENCE", "NO_BAND_MATCH", "TABLE_NOT_FOUND", "PARSE_ERROR",
}
LANGUAGE_ALPHABET = "abcxyzABCXYZ_019.,()%\"+-*/<>= \t\nDATEIFMNOTRU"
TOKENS = [
    "BASIC", "HRA", "att.paid_days", "att.late_marks", "emp.grade", "40%", "1", "0", "2.5", "-", "+", "*", "/", "=",
    "<>", "<", ">=", "(", ")", ",", "AND", "OR", "NOT", "TRUE", "IF(", "MIN(", "ROUND(", "DIVIDE(", "COALESCE(",
    'SUM_TAG("OT_BASE")', '"G3"', "DATE(", "__", "%", ".", "SWITCH(",
]
ENV = TypeEnv(
    "INR",
    components={"BASIC": ComponentSig("INR", 4), "HRA": ComponentSig("INR", 4), "BONUS_USD": ComponentSig("USD", 4)},
    tags={"OT_BASE": {"BASIC"}},
    expect="ANY",
)
CTX = EvalContext.build(
    "INR",
    components={"BASIC": MoneyValue(Decimal("50000.00"), "INR"), "HRA": MoneyValue(Decimal("20000.00"), "INR")},
    rates={"BASIC": MoneyValue(Decimal("50000.00"), "INR")},
    tags={"OT_BASE": ["BASIC"]},
    variables={"att.paid_days": Decimal(28), "att.lop_days": Decimal(0), "att.late_marks": None,
               "emp.grade": "G3", "period.end": date(2026, 11, 30)},
    expect="ANY",
)


def _safe_parse(source: str) -> None:
    try:
        tree = parse(source)
    except FormulaError as exc:
        assert exc.code in {"PARSE_ERROR", "LIMIT_EXCEEDED", "UNKNOWN_REFERENCE"}
        return
    assert shape(parse(render(tree))) == shape(tree)


@FUZZ
@given(st.text(max_size=300))
def test_arbitrary_text_only_raises_formula_error(source: str) -> None:
    _safe_parse(source)


@FUZZ
@given(st.text(alphabet=LANGUAGE_ALPHABET, max_size=120))
def test_language_alphabet_text_only_raises_formula_error(source: str) -> None:
    _safe_parse(source)


@FUZZ
@given(st.lists(st.sampled_from(TOKENS), max_size=40))
def test_token_soup_only_raises_formula_error(tokens: list[str]) -> None:
    source = " ".join(tokens)
    _safe_parse(source)
    try:
        check(source, ENV)
    except FormulaError:
        pass


@FUZZ
@given(st.text(max_size=200))
def test_check_on_arbitrary_text_only_raises_formula_error(source: str) -> None:
    try:
        check(source, ENV)
    except FormulaError:
        pass


@FUZZ
@given(st.from_regex(r"[A-Za-z]?[A-Za-z0-9_]*__[A-Za-z0-9_]*", fullmatch=True))
def test_ec_cmp_08_double_underscore_identifiers_never_parse(name: str) -> None:
    """EC-CMP-08: any identifier containing "__" is a PARSE_ERROR, alone or called."""
    for source in (name, f"{name}()", f'{name}("os")', f"BASIC + {name}", f"emp.{name.lower()}"):
        try:
            parse(source)
        except FormulaError as exc:
            assert exc.code == "PARSE_ERROR"
        else:
            raise AssertionError(f"{source!r} parsed")


# -- generated valid expressions ---------------------------------------------------------------------

LITERALS = st.decimals(min_value=Decimal("-100000"), max_value=Decimal("100000"), places=2, allow_nan=False,
                       allow_infinity=False).map(lambda d: f"({d})" if d < 0 else str(d))
ATOMS = st.one_of(
    LITERALS,
    st.sampled_from(["BASIC", "HRA", "att.paid_days", "att.lop_days", "att.late_marks", "40%", "12.5%",
                     'SUM_TAG("OT_BASE")', 'RATE("BASIC")', "BONUS_USD"]),
)


def _extend(children: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    pair = st.tuples(children, children)
    return st.one_of(
        st.tuples(children, st.sampled_from(["+", "-", "*", "/"]), children).map(lambda t: f"({t[0]} {t[1]} {t[2]})"),
        pair.map(lambda t: f"MIN({t[0]}, {t[1]})"),
        pair.map(lambda t: f"MAX({t[0]}, {t[1]})"),
        children.map(lambda c: f"ABS({c})"),
        children.map(lambda c: f"ROUND({c}, 2)"),
        pair.map(lambda t: f"DIVIDE({t[0]}, {t[1]}, 0)"),
        pair.map(lambda t: f"COALESCE({t[0]}, {t[1]})"),
        st.tuples(children, children, children, st.sampled_from(["<", ">=", "=", "<>"])).map(
            lambda t: f"IF({t[0]} {t[3]} {t[1]}, {t[2]}, {t[0]})"),
        pair.map(lambda t: f"-({t[0]} + {t[1]})"),
    )


EXPRESSIONS = st.recursive(ATOMS, _extend, max_leaves=12)


@FUZZ
@given(EXPRESSIONS)
def test_generated_expressions_round_trip_and_evaluate_deterministically(source: str) -> None:
    tree = parse(source)
    assert shape(parse(render(tree))) == shape(tree)
    first = evaluate(tree, CTX)
    second = evaluate(parse(source), CTX)
    assert first == second
    if first.error is not None:
        assert first.error.code in ALLOWED_RUNTIME_CODES
        assert first.value is None
    else:
        assert first.value is not None


NUMBERS = st.decimals(min_value=Decimal("-1e12"), max_value=Decimal("1e12"), places=6, allow_nan=False,
                      allow_infinity=False)


def _lit(d: Decimal) -> str:
    return f"({d})" if d < 0 else str(d)


@FUZZ
@given(NUMBERS, NUMBERS, st.sampled_from(["+", "-", "*", "/"]))
def test_arithmetic_matches_the_28_digit_context(a: Decimal, b: Decimal, op: str) -> None:
    result = evaluate(parse(f"{_lit(a)} {op} {_lit(b)}"), CTX)
    if op == "/" and b.is_zero():
        assert result.error is not None and result.error.code == "DIVISION_BY_ZERO"
        return
    operation = {"+": CALC_CONTEXT.add, "-": CALC_CONTEXT.subtract, "*": CALC_CONTEXT.multiply,
                 "/": CALC_CONTEXT.divide}[op]
    assert result.value == operation(a, b)


@FUZZ
@given(NUMBERS, st.integers(min_value=0, max_value=4))
def test_round_keeps_at_most_places_decimals(x: Decimal, places: int) -> None:
    rounded = evaluate(parse(f"ROUND({_lit(x)}, {places})"), CTX).value
    assert isinstance(rounded, Decimal)
    assert -rounded.as_tuple().exponent <= places  # type: ignore[operator]
    assert abs(rounded - x) <= Decimal(1).scaleb(-places) / 2


@FUZZ
@given(NUMBERS, st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000"), places=2))
def test_floor_and_ceil_bracket_the_value(x: Decimal, step: Decimal) -> None:
    low = evaluate(parse(f"FLOOR({_lit(x)}, {step})"), CTX).value
    high = evaluate(parse(f"CEIL({_lit(x)}, {step})"), CTX).value
    assert isinstance(low, Decimal) and isinstance(high, Decimal)
    assert low <= x <= high
    assert high - low in (Decimal(0), step)
    assert (low / step) == (low / step).to_integral_value()


@FUZZ
@given(st.dates(min_value=date(1950, 1, 1), max_value=date(2100, 12, 31)),
       st.dates(min_value=date(1950, 1, 1), max_value=date(2100, 12, 31)))
def test_date_differences_are_antisymmetric(d1: date, d2: date) -> None:
    def call(name: str, a: date, b: date) -> Decimal:
        text = f"{name}(DATE({a.year}, {a.month}, {a.day}), DATE({b.year}, {b.month}, {b.day}))"
        result = evaluate(parse(text), CTX).value
        assert isinstance(result, Decimal)
        return result

    assert call("DAYS_BETWEEN", d1, d2) == Decimal((d2 - d1).days)
    assert call("MONTHS_BETWEEN", d1, d2) == -call("MONTHS_BETWEEN", d2, d1)
    assert call("YEARS_BETWEEN", d1, d2) == -call("YEARS_BETWEEN", d2, d1)
