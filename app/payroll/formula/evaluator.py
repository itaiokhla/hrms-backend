"""Evaluation of formulas: exact Decimal arithmetic with a step-by-step trace (3.3 §4.4–§4.8).

``evaluate(expr, ctx)`` first runs the typing pass of ``check`` against
``ctx.env`` (the meaning of a formula, including literal promotion, is decided
statically) and then walks the tree. Arithmetic uses a fresh copy of the 28-digit
``CALC_CONTEXT`` and is never rounded unless the formula calls ROUND. IF, SWITCH,
COALESCE, DIVIDE's fallback, AND and OR evaluate lazily, left to right, so
``IF(att.paid_days > 0, BASIC / att.paid_days, 0)`` cannot divide by zero.

Problems never become a silent zero and are never raised: ``EvalResult.error``
carries the FormulaError (DIVISION_BY_ZERO, NULL_VALUE, NO_BAND_MATCH,
TABLE_NOT_FOUND, …) with the trace up to that point, so the run can put the
employee in the exception queue (EC-CMP-02). ``EvalResult.unwrap()`` raises it
for callers that prefer exceptions. ``steps`` read like ``BASIC = 50000.00 INR``,
``BASIC * 0.40 = 20000.00 INR``; ``references`` maps every reference of the
formula (components, variables, SUM_TAG/RATE/INPUT/UNITS/PREV/YTD calls) to its
value, in source order, for the formula tester.

Context semantics: a structure component missing from ``components``/``rates`` is
an error (it must be calculated first); a component declared ``optional`` (outside
the employee's structure) is NULL. Variables missing or ``None`` are NULL. A code
missing from ``inputs``, ``units``, ``prev`` or ``ytd`` is 0 (no approved input,
nothing paid, YTD at tax-year start, EC-CMP-20); an explicit ``None`` is NULL.
Nothing reads a clock, the environment or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, DecimalException
from typing import Any, Collection, Mapping

from app.payroll.formula import ast
from app.payroll.formula.analysis import ComponentSig, Expect, Typer, TypeEnv
from app.payroll.formula.ast import (
    Binary,
    Boolean,
    Call,
    Compare,
    ComponentRef,
    DateLiteral,
    Expr,
    Logical,
    Number,
    String,
    Unary,
    VariableRef,
    render,
)
from app.payroll.formula.errors import ErrorCode, FormulaError
from app.payroll.formula.functions import (
    MAX_PLACES,
    HOURLY_CONVERTIBLE_UNITS,
    MIN_PLACES,
    RATE_UNIT_DAY,
    RATE_UNIT_HOUR,
    RATE_UNIT_PERIOD,
    REFERENCE_FUNCTIONS,
    floor_to,
    months_between,
    round_to,
    years_between,
)
from app.payroll.formula.parser import enforce_limits
from app.payroll.formula.tables import Table, TableKind, TableSource, describe_band, resolve_table
from app.payroll.formula.types import (
    BOOLEAN,
    DATE,
    DECIMAL,
    STRING,
    FType,
    MoneyValue,
    Value,
    clean,
    format_value,
    money,
    new_context,
    plain_decimal,
    to_money,
    to_number,
    to_value,
    value_to_json,
)
from app.payroll.formula.variables import CUSTOM_ATTRIBUTE_PREFIX, VARIABLES

_MISSING = object()


@dataclass(frozen=True)
class Proration:
    """The current component's proration for PRORATE(x) = x × units ÷ basis (3.4 §5.1).

    ``cap_at_rate`` applies the fixed-basis rule min(R, R × U ÷ N) (by magnitude).
    Units and basis are finite and not negative; a zero basis is a run-time
    DIVISION_BY_ZERO (data problem), never a silent zero.
    """

    units: Decimal
    basis: Decimal
    cap_at_rate: bool = False

    def __post_init__(self) -> None:
        for name in ("units", "basis"):
            raw = getattr(self, name)
            if isinstance(raw, bool) or not isinstance(raw, (Decimal, int)):
                raise TypeError(f"proration {name} must be Decimal or int")
            number = Decimal(raw)
            if not number.is_finite() or number < 0:
                raise ValueError(f"proration {name} must be a finite number >= 0, got {raw}")
            object.__setattr__(self, name, number)

    @classmethod
    def none(cls) -> Proration:
        """Proration NONE: PRORATE(x) is x."""
        return cls(Decimal(1), Decimal(1))


@dataclass(frozen=True)
class EvalContext:
    """Everything one evaluation may read. See the module docstring for missing-value rules."""

    env: TypeEnv
    components: Mapping[str, Any] = field(default_factory=dict)  # current-run amounts, after proration
    rates: Mapping[str, Any] = field(default_factory=dict)  # full-period rates, before proration
    variables: Mapping[str, Any] = field(default_factory=dict)  # "att.paid_days", "emp.attr.region", …
    inputs: Mapping[str, Any] = field(default_factory=dict)  # approved wage-input amounts this period
    units: Mapping[str, Any] = field(default_factory=dict)  # approved wage-input units this period
    prev: Mapping[str, Any] = field(default_factory=dict)  # amounts of the last finalized period
    ytd: Mapping[str, Any] = field(default_factory=dict)  # tax-year-to-date amounts
    tables: TableSource = field(default_factory=dict)  # tables effective for the period
    proration: Proration | None = None
    minor_units: Mapping[str, int] = field(default_factory=dict)  # display only: decimals shown per currency
    # unit of each full-period rate for SUM_TAG_HOURLY: PERIOD (default), DAY or HOUR; anything else is skipped
    rate_units: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.env, TypeEnv):
            raise TypeError("env must be a TypeEnv")
        if self.proration is not None and not isinstance(self.proration, Proration):
            raise TypeError("proration must be a Proration")

    @classmethod
    def build(
        cls,
        currency: str,
        *,
        components: Mapping[str, Any] | None = None,
        rates: Mapping[str, Any] | None = None,
        tags: Mapping[str, Collection[str]] | None = None,
        variables: Mapping[str, Any] | None = None,
        inputs: Mapping[str, Any] | None = None,
        units: Mapping[str, Any] | None = None,
        prev: Mapping[str, Any] | None = None,
        ytd: Mapping[str, Any] | None = None,
        tables: TableSource | None = None,
        proration: Proration | None = None,
        attributes: Mapping[str, FType] | None = None,
        expect: Expect = "AMOUNT",
        allow_protected: bool = False,
        component: str | None = None,
        stage: int | None = None,
        minor_units: Mapping[str, int] | None = None,
        rate_units: Mapping[str, str] | None = None,
    ) -> EvalContext:
        """A context whose TypeEnv is derived from the values (formula tester, tests).

        Every code that has a value is a known component in the currency of that value;
        a component given as ``None`` is optional (NULL). Custom attribute types are taken
        from ``attributes`` or inferred from the ``emp.attr.*`` values.
        """
        components, rates = dict(components or {}), dict(rates or {})
        inputs, units, prev, ytd = dict(inputs or {}), dict(units or {}), dict(prev or {}), dict(ytd or {})
        variables, tags = dict(variables or {}), dict(tags or {})
        sigs: dict[str, ComponentSig] = {}
        for source in (components, rates, inputs, prev, ytd):
            for code, raw in source.items():
                if code in sigs:
                    continue
                if raw is None:
                    sigs[code] = ComponentSig(currency, optional=source is components)
                else:
                    sigs[code] = ComponentSig(to_money(raw, code).currency)
        for code in [*units, *(c for members in tags.values() for c in members)]:
            sigs.setdefault(code, ComponentSig(currency))
        if attributes is None:
            attributes = {
                name[len(CUSTOM_ATTRIBUTE_PREFIX):]: _infer_type(raw)
                for name, raw in variables.items()
                if name.startswith(CUSTOM_ATTRIBUTE_PREFIX)
            }
        env = TypeEnv(
            currency,
            components=sigs,
            tags=tags,
            tables=tables if tables is not None else {},
            attributes=attributes,
            component=component,
            stage=stage,
            allow_protected=allow_protected,
            expect=expect,
        )
        return cls(env, components, rates, variables, inputs, units, prev, ytd,
                   tables if tables is not None else {}, proration, dict(minor_units or {}), dict(rate_units or {}))


def _infer_type(raw: Any) -> FType:
    if isinstance(raw, bool):
        return BOOLEAN
    if isinstance(raw, (Decimal, int)):
        return DECIMAL
    if isinstance(raw, date):
        return DATE
    if hasattr(raw, "amount") and hasattr(raw, "currency"):
        return money(to_money(raw, "attribute").currency)
    return STRING


@dataclass(frozen=True)
class EvalResult:
    value: Value  # None only when ``error`` is set
    steps: list[str]
    references: dict[str, str]
    type: FType | None = None
    error: FormulaError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def unwrap(self) -> Value:
        if self.error is not None:
            raise self.error
        return self.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": value_to_json(self.value),
            "type": str(self.type) if self.type is not None else None,
            "steps": list(self.steps),
            "references": dict(self.references),
            "error": self.error.to_dict() if self.error is not None else None,
        }


def evaluate(expr: Expr, ctx: EvalContext) -> EvalResult:
    """Evaluate a parsed formula. Formula and data problems are returned in ``EvalResult.error``."""
    if not isinstance(expr, ast.NODE_TYPES):
        raise TypeError("expr must be a parsed formula (use parse())")
    if not isinstance(ctx, EvalContext):
        raise TypeError("ctx must be an EvalContext")
    try:
        enforce_limits(expr)
    except FormulaError as exc:
        return EvalResult(None, [], {}, None, exc)
    typer = Typer(ctx.env, deferred_tables=True, tables=ctx.tables)
    result_type = typer.run(expr)
    if typer.errors:
        return EvalResult(None, [], {}, None, FormulaError.from_issue(typer.errors[0], typer.errors))
    runner = _Runner(ctx, typer.types)
    value: Value = None
    error: FormulaError | None = None
    try:
        value = runner.result(expr)
    except FormulaError as exc:
        error = exc
    except DecimalException:
        error = FormulaError(ErrorCode.LIMIT_EXCEEDED, "a number is too large or too precise to calculate exactly",
                             position=expr.pos, end=expr.end)
    references = runner.references(expr)
    if error is not None:
        return EvalResult(None, runner.steps, references, None, error)
    final_type = typer.types.get(id(expr), result_type)
    if isinstance(value, MoneyValue):
        final_type = money(value.currency)
    return EvalResult(value, runner.steps, references, final_type, None)


def check_floor(
    value: Any,
    floor: Any = Decimal(0),
    *,
    component: str | None = None,
    minor_units: Mapping[str, int] | None = None,
) -> FormulaError | None:
    """NEGATIVE_RESULT when ``value`` is below ``floor`` (default 0), returned rather than raised.

    At save time the caller raises it (a negative balancing component rejects the
    assignment, EC-CMP-04); at run time it becomes an employee exception.
    """
    amount, currency = _floor_operand(value, "value")
    limit, floor_currency = _floor_operand(floor, "floor")
    if currency is not None and floor_currency is not None and currency != floor_currency:
        raise FormulaError(ErrorCode.CURRENCY_MISMATCH, f"floor is in {floor_currency} but the value is in {currency}")
    if currency is None and floor_currency is not None:
        raise FormulaError(ErrorCode.TYPE_MISMATCH, "a plain number cannot be compared with an amount floor")
    if amount >= limit:
        return None
    shown = format_value(MoneyValue(amount, currency) if currency else amount, minor_units)
    shown_floor = format_value(MoneyValue(limit, currency) if currency else limit, minor_units)
    subject = component or "the result"
    return FormulaError(ErrorCode.NEGATIVE_RESULT, f"{subject} is {shown}, below its floor of {shown_floor}")


def _floor_operand(raw: Any, what: str) -> tuple[Decimal, str | None]:
    if isinstance(raw, (Decimal, int)) and not isinstance(raw, bool):
        return to_number(raw, what), None
    money_value = to_money(raw, what)
    return money_value.amount, money_value.currency


class _Runner:
    def __init__(self, ctx: EvalContext, types: Mapping[int, FType]) -> None:
        self.ctx = ctx
        self.env = ctx.env
        self.types = types
        self.dc = new_context()
        self.steps: list[str] = []
        self.seen: dict[str, str] = {}

    # -- trace -----------------------------------------------------------------------------------

    def fmt(self, value: Value) -> str:
        return format_value(value, self.ctx.minor_units)

    def step(self, node: Expr, value: Value, note: str = "") -> Value:
        self.steps.append(f"{render(node, values=True)} = {self.fmt(value)}{note}")
        return value

    def reference(self, node: Expr, value: Value, note: str = "") -> Value:
        self.seen.setdefault(render(node), self.fmt(value))
        return self.step(node, value, note)

    def references(self, expr: Expr) -> dict[str, str]:
        """Every reference in source order, with its value; unevaluated ones are looked up quietly."""
        found: dict[str, str] = {}
        for node in ast.walk(expr):
            if not _is_reference(node):
                continue
            key = render(node)
            if key in found:
                continue
            if key in self.seen:
                found[key] = self.seen[key]
                continue
            probe = _Runner(self.ctx, self.types)
            try:
                found[key] = probe.fmt(probe.visit(node))
            except FormulaError as exc:
                found[key] = f"UNAVAILABLE ({exc.code})"
        return found

    # -- errors ----------------------------------------------------------------------------------

    @staticmethod
    def error(code: ErrorCode, message: str, node: Expr) -> FormulaError:
        return FormulaError(code, message, position=node.pos, end=node.end)

    def need(self, value: Value, node: Expr) -> Value:
        if value is None:
            text = render(node)
            raise self.error(
                ErrorCode.NULL_VALUE,
                f"{text} is NULL (unavailable) and cannot be used here; use COALESCE({text}, <default>)",
                node,
            )
        return value

    def number(self, value: Value, node: Expr, what: str) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, Decimal):
            raise self.error(ErrorCode.TYPE_MISMATCH, f"{what} needs a number", node)
        return value

    def same_currency(self, a: MoneyValue, b: MoneyValue, node: Expr) -> None:
        if a.currency != b.currency:
            raise self.error(
                ErrorCode.CURRENCY_MISMATCH,
                f"amounts in {a.currency} and {b.currency} cannot be combined ({a.currency} vs {b.currency})",
                node,
            )

    def expect_currency(self, value: MoneyValue, currency: str | None, node: Expr, what: str) -> MoneyValue:
        if currency is not None and value.currency != currency:
            raise self.error(
                ErrorCode.CURRENCY_MISMATCH,
                f"{what} is in {value.currency} but {currency} was expected ({currency} vs {value.currency}); "
                "amounts are never converted implicitly",
                node,
            )
        return value

    # -- entry -----------------------------------------------------------------------------------

    def result(self, expr: Expr) -> Value:
        value = self.visit(expr)
        if value is None:
            raise self.error(ErrorCode.NULL_VALUE, "the formula result is NULL (an input is unavailable); use COALESCE "
                             "to choose a default", expr)
        expect = self.env.expect
        if expect == "AMOUNT":
            if isinstance(value, MoneyValue):
                return self.expect_currency(value, self.env.currency, expr, "the result")
            amount = MoneyValue(self.number(value, expr, "an amount formula"), self.env.currency)
            self.steps.append(f"RESULT = {self.fmt(amount)}")
            return amount
        if expect == "NUMBER" and (isinstance(value, bool) or not isinstance(value, Decimal)):
            raise self.error(ErrorCode.TYPE_MISMATCH, "the formula must give a number", expr)
        if expect == "BOOLEAN" and not isinstance(value, bool):
            raise self.error(ErrorCode.TYPE_MISMATCH, "the formula must give TRUE or FALSE", expr)
        return value

    def visit(self, node: Expr) -> Value:
        if isinstance(node, Number):
            ftype = self.types.get(id(node))
            if ftype is not None and ftype.is_money:
                return MoneyValue(clean(node.value), ftype.currency)  # type: ignore[arg-type]
            return clean(node.value)
        if isinstance(node, (String, Boolean, DateLiteral)):
            return node.value
        if isinstance(node, ComponentRef):
            sig = self.env.components[node.code]
            return self.reference(node, self.component(self.ctx.components, node.code, sig, node, "amount"))
        if isinstance(node, VariableRef):
            return self.reference(node, self.variable(node))
        if isinstance(node, Unary):
            return self.step(node, self.unary(node))
        if isinstance(node, Binary):
            left = self.need(self.visit(node.left), node.left)
            right = self.need(self.visit(node.right), node.right)
            return self.step(node, self.arithmetic(node.op, left, right, node, node.right))
        if isinstance(node, Compare):
            left = self.need(self.visit(node.left), node.left)
            right = self.need(self.visit(node.right), node.right)
            return self.step(node, self.compare(node.op, left, right, node))
        if isinstance(node, Logical):
            return self.step(node, self.logical(node))
        if isinstance(node, Call):
            value, note = getattr(self, f"fn_{node.name.lower()}")(node)
            if node.name in REFERENCE_FUNCTIONS:
                return self.reference(node, value, note)
            return self.step(node, value, note)
        raise TypeError(f"not a formula node: {type(node).__name__}")

    # -- references ------------------------------------------------------------------------------

    def component(self, source: Mapping[str, Any], code: str, sig: ComponentSig, node: Expr, what: str) -> Value:
        raw = source.get(code, _MISSING)
        if raw is _MISSING or raw is None:
            if sig.optional:
                return None
            raise self.error(
                ErrorCode.UNKNOWN_REFERENCE,
                f"{code} has no {what} in this run; it must be calculated before the formulas that use it",
                node,
            )
        return self.expect_currency(to_money(raw, code), sig.currency, node, code)

    def variable(self, node: VariableRef) -> Value:
        name = node.name
        if name in self.env.locals:
            declared = self.env.locals[name].type
        elif name.startswith(CUSTOM_ATTRIBUTE_PREFIX):
            declared = self.env.attributes[name[len(CUSTOM_ATTRIBUTE_PREFIX):]]
        else:
            declared = VARIABLES[name].type
        if declared.is_money and declared.currency is None:
            declared = money(self.env.currency)
        value = to_value(self.ctx.variables.get(name), declared, name)
        if isinstance(value, MoneyValue):
            self.expect_currency(value, declared.currency, node, name)
        return value

    def amount_of(self, source: Mapping[str, Any], node: Call) -> tuple[Value, str]:
        code = _code(node)
        sig = self.env.components[code]
        raw = source.get(code, _MISSING)
        if raw is None:
            return None, ""
        if raw is _MISSING:
            return MoneyValue(Decimal(0), sig.currency), " (none: 0)"
        return self.expect_currency(to_money(raw, code), sig.currency, node, code), ""

    # -- operators -------------------------------------------------------------------------------

    def unary(self, node: Unary) -> Value:
        value = self.need(self.visit(node.operand), node.operand)
        if node.op == "NOT":
            if not isinstance(value, bool):
                raise self.error(ErrorCode.TYPE_MISMATCH, "NOT needs TRUE or FALSE", node)
            return not value
        if isinstance(value, MoneyValue):
            return MoneyValue(clean(self.dc.minus(value.amount)), value.currency)
        return clean(self.dc.minus(self.number(value, node, "minus")))

    def arithmetic(self, op: str, left: Value, right: Value, node: Expr, right_node: Expr) -> Value:
        dc = self.dc
        if op in ("+", "-"):
            combine = dc.add if op == "+" else dc.subtract
            if isinstance(left, MoneyValue) and isinstance(right, MoneyValue):
                self.same_currency(left, right, node)
                return MoneyValue(clean(combine(left.amount, right.amount)), left.currency)
            if isinstance(left, MoneyValue) or isinstance(right, MoneyValue):
                raise self.error(ErrorCode.TYPE_MISMATCH, "an amount and a plain number cannot be added", node)
            return clean(combine(self.number(left, node, op), self.number(right, node, op)))
        if op == "*":
            if isinstance(left, MoneyValue) and isinstance(right, MoneyValue):
                raise self.error(ErrorCode.TYPE_MISMATCH, "an amount cannot be multiplied by an amount", node)
            if isinstance(left, MoneyValue):
                return MoneyValue(clean(dc.multiply(left.amount, self.number(right, node, "*"))), left.currency)
            if isinstance(right, MoneyValue):
                return MoneyValue(clean(dc.multiply(self.number(left, node, "*"), right.amount)), right.currency)
            return clean(dc.multiply(self.number(left, node, "*"), self.number(right, node, "*")))
        divisor = right.amount if isinstance(right, MoneyValue) else self.number(right, node, "/")
        if divisor.is_zero():
            raise self.error(
                ErrorCode.DIVISION_BY_ZERO,
                f"division by zero: {render(right_node)} is 0; use DIVIDE(a, b, fallback) when it can be 0",
                node,
            )
        if isinstance(left, MoneyValue) and isinstance(right, MoneyValue):
            self.same_currency(left, right, node)
            return clean(dc.divide(left.amount, divisor))
        if isinstance(right, MoneyValue):
            raise self.error(ErrorCode.TYPE_MISMATCH, "a number cannot be divided by an amount", node)
        if isinstance(left, MoneyValue):
            return MoneyValue(clean(dc.divide(left.amount, divisor)), left.currency)
        return clean(dc.divide(self.number(left, node, "/"), divisor))

    def comparable(self, a: Value, b: Value, node: Expr) -> tuple[Any, Any]:
        if isinstance(a, MoneyValue) and isinstance(b, MoneyValue):
            self.same_currency(a, b, node)
            return a.amount, b.amount
        if isinstance(a, MoneyValue) or isinstance(b, MoneyValue):
            raise self.error(ErrorCode.TYPE_MISMATCH, "an amount can only be compared with an amount", node)
        if isinstance(a, bool) != isinstance(b, bool) or type(a) is not type(b) and not (
                isinstance(a, Decimal) and isinstance(b, Decimal)):
            raise self.error(ErrorCode.TYPE_MISMATCH, "values of different types cannot be compared", node)
        return a, b

    def equal(self, a: Value, b: Value, node: Expr) -> bool:
        left, right = self.comparable(a, b, node)
        return bool(left == right)

    def compare(self, op: str, a: Value, b: Value, node: Expr) -> bool:
        left, right = self.comparable(a, b, node)
        if op == "=":
            return bool(left == right)
        if op == "<>":
            return bool(left != right)
        if isinstance(left, (bool, str)):
            raise self.error(ErrorCode.TYPE_MISMATCH, f"'{op}' cannot order these values", node)
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]

    def condition(self, node: Expr, what: str) -> bool:
        value = self.need(self.visit(node), node)
        if not isinstance(value, bool):
            raise self.error(ErrorCode.TYPE_MISMATCH, f"{what} needs TRUE or FALSE", node)
        return value

    def logical(self, node: Logical) -> bool:
        stop = node.op == "OR"
        for operand in node.operands:
            if self.condition(operand, node.op) is stop:
                return stop
        return not stop

    # -- math ------------------------------------------------------------------------------------

    def _ordered(self, node: Call) -> list[Value]:
        values = [self.need(self.visit(arg), arg) for arg in node.args]
        first = values[0]
        for value in values[1:]:
            self.comparable(first, value, node)
        return values

    @staticmethod
    def _key(value: Value) -> Any:
        return value.amount if isinstance(value, MoneyValue) else value

    def fn_min(self, node: Call) -> tuple[Value, str]:
        return min(self._ordered(node), key=self._key), ""

    def fn_max(self, node: Call) -> tuple[Value, str]:
        return max(self._ordered(node), key=self._key), ""

    def fn_clamp(self, node: Call) -> tuple[Value, str]:
        x, lo, hi = self._ordered(node)
        if self._key(lo) > self._key(hi):
            raise self.error(ErrorCode.INVALID_ARGUMENT, f"CLAMP needs lo <= hi, got {self.fmt(lo)} > {self.fmt(hi)}",
                             node)
        if self._key(x) < self._key(lo):
            return lo, ""
        if self._key(x) > self._key(hi):
            return hi, ""
        return x, ""

    def fn_abs(self, node: Call) -> tuple[Value, str]:
        value = self.need(self.visit(node.args[0]), node.args[0])
        if isinstance(value, MoneyValue):
            return MoneyValue(value.amount.copy_abs(), value.currency), ""
        return self.number(value, node, "ABS").copy_abs(), ""

    def _round(self, node: Call, mode: str) -> tuple[Value, str]:
        value = self.need(self.visit(node.args[0]), node.args[0])
        places_value = self.number(self.need(self.visit(node.args[1]), node.args[1]), node.args[1], node.name)
        if places_value != places_value.to_integral_value() or not MIN_PLACES <= places_value <= MAX_PLACES:
            raise self.error(ErrorCode.INVALID_ARGUMENT,
                             f"{node.name} places must be a whole number between {MIN_PLACES} and {MAX_PLACES}",
                             node.args[1])
        places = int(places_value)
        if isinstance(value, MoneyValue):
            return MoneyValue(clean(round_to(value.amount, places, mode, self.dc)), value.currency), ""
        return clean(round_to(self.number(value, node, node.name), places, mode, self.dc)), ""

    def fn_round(self, node: Call) -> tuple[Value, str]:
        mode = node.args[2].value if len(node.args) > 2 else "HALF_UP"  # type: ignore[union-attr]
        return self._round(node, mode)

    def fn_roundup(self, node: Call) -> tuple[Value, str]:
        return self._round(node, "UP")

    def fn_rounddown(self, node: Call) -> tuple[Value, str]:
        return self._round(node, "DOWN")

    def _step_to(self, node: Call, ceiling: bool) -> tuple[Value, str]:
        value = self.need(self.visit(node.args[0]), node.args[0])
        step = self.need(self.visit(node.args[1]), node.args[1])
        if isinstance(value, MoneyValue) and isinstance(step, MoneyValue):
            self.same_currency(value, step, node)
        step_amount = step.amount if isinstance(step, MoneyValue) else self.number(step, node, node.name)
        if step_amount.is_zero():
            raise self.error(ErrorCode.DIVISION_BY_ZERO, f"{node.name} step is 0", node.args[1])
        if step_amount < 0:
            raise self.error(ErrorCode.INVALID_ARGUMENT, f"{node.name} step must be positive", node.args[1])
        if isinstance(value, MoneyValue):
            return MoneyValue(clean(floor_to(value.amount, step_amount, self.dc, ceiling=ceiling)), value.currency), ""
        return clean(floor_to(self.number(value, node, node.name), step_amount, self.dc, ceiling=ceiling)), ""

    def fn_floor(self, node: Call) -> tuple[Value, str]:
        return self._step_to(node, ceiling=False)

    def fn_ceil(self, node: Call) -> tuple[Value, str]:
        return self._step_to(node, ceiling=True)

    def fn_divide(self, node: Call) -> tuple[Value, str]:
        a = self.need(self.visit(node.args[0]), node.args[0])
        b = self.need(self.visit(node.args[1]), node.args[1])
        divisor = b.amount if isinstance(b, MoneyValue) else self.number(b, node, "DIVIDE")
        if divisor.is_zero():
            return self.visit(node.args[2]), " (divisor is 0: fallback)"
        return self.arithmetic("/", a, b, node, node.args[1]), ""

    # -- logic -----------------------------------------------------------------------------------

    def fn_if(self, node: Call) -> tuple[Value, str]:
        chosen = node.args[1] if self.condition(node.args[0], "IF") else node.args[2]
        return self.visit(chosen), ""

    def fn_switch(self, node: Call) -> tuple[Value, str]:
        args = node.args
        subject = self.need(self.visit(args[0]), args[0])
        for index in range(1, len(args) - 1, 2):
            key = self.need(self.visit(args[index]), args[index])
            if self.equal(subject, key, node):
                return self.visit(args[index + 1]), ""
        return self.visit(args[-1]), " (default)"

    def fn_in(self, node: Call) -> tuple[Value, str]:
        subject = self.need(self.visit(node.args[0]), node.args[0])
        for arg in node.args[1:]:
            if self.equal(subject, self.need(self.visit(arg), arg), node):
                return True, ""
        return False, ""

    def fn_coalesce(self, node: Call) -> tuple[Value, str]:
        for arg in node.args:
            value = self.visit(arg)
            if value is not None:
                return value, ""
        return None, ""

    # -- components ------------------------------------------------------------------------------

    def _tag(self, node: Call, source: Mapping[str, Any], what: str) -> tuple[Value, str]:
        tag = _code(node)
        members = sorted(self.env.tags.get(tag, ()))
        target = self.types.get(id(node))
        total = MoneyValue(Decimal(0), target.currency if target is not None and target.currency
                           else self.env.currency)
        for code in members:
            value = self.component(source, code, self.env.components[code], node, what)
            if value is None:
                continue
            assert isinstance(value, MoneyValue)
            if total.amount.is_zero() and total.currency != value.currency and code == members[0]:
                total = MoneyValue(Decimal(0), value.currency)
            self.same_currency(total, value, node)
            total = MoneyValue(clean(self.dc.add(total.amount, value.amount)), total.currency)
        return total, ""

    def fn_sum_tag(self, node: Call) -> tuple[Value, str]:
        return self._tag(node, self.ctx.components, "amount")

    def fn_sum_tag_rate(self, node: Call) -> tuple[Value, str]:
        return self._tag(node, self.ctx.rates, "rate")

    def fn_sum_tag_hourly(self, node: Call) -> tuple[Value, str]:
        """The tagged full-period rates as one hourly rate, each converted by its unit (``ctx.rate_units``).

        PERIOD rates are added first and divided by ``period.basis_days`` and then by
        ``emp.standard_hours_per_day`` (so a salaried employee gets exactly
        ``SUM_TAG_RATE(tag) / period.basis_days / emp.standard_hours_per_day``), DAY rates are divided by
        ``emp.standard_hours_per_day`` and HOUR rates are used as they are. A rate in any other unit (pieces,
        unknown) cannot be converted: it is left out and named in the step (the engine raises a warning).
        """
        tag = _code(node)
        members = sorted(self.env.tags.get(tag, ()))
        target = self.types.get(id(node))
        currency = target.currency if target is not None and target.currency else self.env.currency
        sums: dict[str, Decimal] = {}
        skipped: list[str] = []
        for code in members:
            value = self.component(self.ctx.rates, code, self.env.components[code], node, "rate")
            if value is None:
                continue
            assert isinstance(value, MoneyValue)
            if not sums and value.currency != currency and code == members[0]:
                currency = value.currency
            self.same_currency(MoneyValue(Decimal(0), currency), value, node)
            unit = self.ctx.rate_units.get(code, RATE_UNIT_PERIOD)
            if unit not in HOURLY_CONVERTIBLE_UNITS:
                skipped.append(code)
                continue
            sums[unit] = clean(self.dc.add(sums.get(unit, Decimal(0)), value.amount))
        total = Decimal(0)
        parts: list[str] = []
        if RATE_UNIT_PERIOD in sums:
            basis = self._divisor("period.basis_days", node)
            hours = self._divisor("emp.standard_hours_per_day", node)
            total = clean(self.dc.divide(clean(self.dc.divide(sums[RATE_UNIT_PERIOD], basis)), hours))
            parts.append(f"{plain_decimal(sums[RATE_UNIT_PERIOD])} ÷ {plain_decimal(basis)} days ÷ "
                         f"{plain_decimal(hours)} h")
        if RATE_UNIT_DAY in sums:
            hours = self._divisor("emp.standard_hours_per_day", node)
            total = clean(self.dc.add(total, clean(self.dc.divide(sums[RATE_UNIT_DAY], hours))))
            parts.append(f"{plain_decimal(sums[RATE_UNIT_DAY])} per day ÷ {plain_decimal(hours)} h")
        if RATE_UNIT_HOUR in sums:
            total = clean(self.dc.add(total, sums[RATE_UNIT_HOUR]))
            parts.append(f"{plain_decimal(sums[RATE_UNIT_HOUR])} per hour")
        note = f" ({' + '.join(parts)})" if parts else ""
        if skipped:
            note += f" (left out, unit cannot be converted to hours: {', '.join(skipped)})"
        return MoneyValue(total, currency), note

    def _divisor(self, name: str, node: Call) -> Decimal:
        value = to_value(self.ctx.variables.get(name), DECIMAL, name)
        if value is None:
            raise self.error(ErrorCode.NULL_VALUE, f"{name} is NULL (unavailable); {node.name} needs it to convert "
                             "a rate to hours", node)
        number = self.number(value, node, node.name)
        if number.is_zero():
            raise self.error(ErrorCode.DIVISION_BY_ZERO, f"division by zero: {name} is 0", node)
        return number

    def fn_rate(self, node: Call) -> tuple[Value, str]:
        code = _code(node)
        return self.component(self.ctx.rates, code, self.env.components[code], node, "rate"), ""

    def fn_prorate(self, node: Call) -> tuple[Value, str]:
        value = self.need(self.visit(node.args[0]), node.args[0])
        proration = self.ctx.proration
        if proration is None:
            raise self.error(ErrorCode.UNKNOWN_REFERENCE,
                             "PRORATE needs the component's proration, which this calculation does not provide", node)
        if proration.basis.is_zero():
            raise self.error(ErrorCode.DIVISION_BY_ZERO, "the proration basis is 0", node)
        amount = value.amount if isinstance(value, MoneyValue) else self.number(value, node, "PRORATE")
        prorated = self.dc.divide(self.dc.multiply(amount, proration.units), proration.basis)
        note = f" ({self.fmt(value)} × {plain_decimal(proration.units)} ÷ {plain_decimal(proration.basis)}"
        if proration.cap_at_rate and prorated.copy_abs() > amount.copy_abs():
            prorated = amount
            note += ", capped at the full rate"
        note += ")"
        if isinstance(value, MoneyValue):
            return MoneyValue(clean(prorated), value.currency), note
        return clean(prorated), note

    def fn_input(self, node: Call) -> tuple[Value, str]:
        return self.amount_of(self.ctx.inputs, node)

    def fn_units(self, node: Call) -> tuple[Value, str]:
        code = _code(node)
        raw = self.ctx.units.get(code, _MISSING)
        if raw is None:
            return None, ""
        if raw is _MISSING:
            return Decimal(0), " (none: 0)"
        return to_number(raw, f'UNITS("{code}")'), ""

    def fn_prev(self, node: Call) -> tuple[Value, str]:
        return self.amount_of(self.ctx.prev, node)

    def fn_ytd(self, node: Call) -> tuple[Value, str]:
        return self.amount_of(self.ctx.ytd, node)

    # -- tables ----------------------------------------------------------------------------------

    def table(self, node: Call, kind: TableKind) -> Table:
        code = _code(node)
        found = resolve_table(self.ctx.tables, code)
        if not isinstance(found, Table):
            raise self.error(ErrorCode.TABLE_NOT_FOUND, f"table {code} is not available for this period", node)
        if found.kind is not kind:
            raise self.error(ErrorCode.TYPE_MISMATCH, f'{code} is a {found.kind} table; read it with {found.kind}()',
                             node)
        return found

    def bound(self, table: Table, node: Call) -> tuple[Value, Decimal]:
        x = self.need(self.visit(node.args[1]), node.args[1])
        if table.key_currency is not None:
            if not isinstance(x, MoneyValue):
                raise self.error(ErrorCode.TYPE_MISMATCH,
                                 f"{table.code} is banded by amounts in {table.key_currency}", node.args[1])
            self.expect_currency(x, table.key_currency, node.args[1], render(node.args[1]))
            return x, x.amount
        if isinstance(x, MoneyValue):
            raise self.error(ErrorCode.TYPE_MISMATCH, f"{table.code} is banded by plain numbers", node.args[1])
        return x, self.number(x, node, table.code)

    def _table_value(self, table: Table, amount: Decimal) -> Value:
        return MoneyValue(clean(amount), table.value_currency) if table.value_currency else clean(amount)

    def fn_slab(self, node: Call) -> tuple[Value, str]:
        table = self.table(node, TableKind.SLAB)
        _, x = self.bound(table, node)
        total, parts = table.slab(x)
        note = f" ({' + '.join(parts)})" if parts else " (below the first slab)"
        value = MoneyValue(clean(total), table.key_currency) if table.key_currency else clean(total)
        return value, note

    def fn_band(self, node: Call) -> tuple[Value, str]:
        table = self.table(node, TableKind.BAND)
        shown, x = self.bound(table, node)
        row = table.band(x)
        if row is None or row.value is None:
            raise self.error(ErrorCode.NO_BAND_MATCH, f"{self.fmt(shown)} is outside every band of {table.code}", node)
        return self._table_value(table, row.value), f" (band {describe_band(row)})"

    def fn_lookup(self, node: Call) -> tuple[Value, str]:
        table = self.table(node, TableKind.LOOKUP)
        key = self.need(self.visit(node.args[1]), node.args[1])
        if not isinstance(key, str):
            raise self.error(ErrorCode.TYPE_MISMATCH, "LOOKUP keys are text", node.args[1])
        found = table.lookup(key)
        if found is None:
            return self.visit(node.args[2]), f' (key "{key}" not in {table.code}: default)'
        return self._table_value(table, found), f' (key "{key}")'

    # -- dates -----------------------------------------------------------------------------------

    def _date(self, node: Expr) -> date:
        value = self.need(self.visit(node), node)
        if not isinstance(value, date):
            raise self.error(ErrorCode.TYPE_MISMATCH, "a date is needed", node)
        return value

    def _whole(self, node: Expr) -> int:
        value = self.number(self.need(self.visit(node), node), node, "DATE")
        if value != value.to_integral_value():
            raise self.error(ErrorCode.INVALID_ARGUMENT, "DATE needs whole numbers", node)
        return int(value)

    def fn_date(self, node: Call) -> tuple[Value, str]:
        year, month, day = (self._whole(arg) for arg in node.args)
        try:
            return date(year, month, day), ""
        except ValueError:
            raise self.error(ErrorCode.INVALID_ARGUMENT, f"DATE({year}, {month}, {day}) is not a valid date",
                             node) from None
        except OverflowError:
            raise self.error(ErrorCode.INVALID_ARGUMENT, "DATE arguments are out of range", node) from None

    def fn_year(self, node: Call) -> tuple[Value, str]:
        return Decimal(self._date(node.args[0]).year), ""

    def fn_month(self, node: Call) -> tuple[Value, str]:
        return Decimal(self._date(node.args[0]).month), ""

    def fn_days_between(self, node: Call) -> tuple[Value, str]:
        start, end = self._date(node.args[0]), self._date(node.args[1])
        return Decimal((end - start).days), ""

    def fn_months_between(self, node: Call) -> tuple[Value, str]:
        return Decimal(months_between(self._date(node.args[0]), self._date(node.args[1]))), ""

    def fn_years_between(self, node: Call) -> tuple[Value, str]:
        return Decimal(years_between(self._date(node.args[0]), self._date(node.args[1]))), ""


def _code(node: Call) -> str:
    first = node.args[0]
    assert isinstance(first, String)
    return first.value


def _is_reference(node: Expr) -> bool:
    return isinstance(node, (ComponentRef, VariableRef)) or (
        isinstance(node, Call) and node.name in REFERENCE_FUNCTIONS
    )
