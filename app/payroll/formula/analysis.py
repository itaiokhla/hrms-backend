"""Static analysis of formulas: references, types, currencies, stages, protected data (3.3 §4–§5).

``check(source, env)`` is the save-time gate: it raises FormulaError for the first
problem (all problems are in ``error.issues``) and otherwise returns an
``Analysis`` with the result type, every reference, the run-time dependencies and
warnings. ``diagnose`` returns the same analysis without raising, for the editor.
The typing pass also runs at the start of every evaluation, so a formula can never
be evaluated with a meaning different from the one that was checked.

Type rules (3.3 §4.4): MONEY ± MONEY needs one currency (CURRENCY_MISMATCH, never
implicit FX); MONEY × DECIMAL and MONEY ÷ DECIMAL give MONEY; MONEY ÷ MONEY gives
DECIMAL; INTEGER is promoted to DECIMAL except in INTEGER + - × INTEGER; BOOLEAN
only in conditions; DATE only in comparisons and date functions; STRING only in
= <>, IN, SWITCH keys and table keys.

Literal promotion (decided here): a plain number literal (``500``, ``0.5``; not
``5%``, a variable or a calculation) meeting MONEY in ``+``/``-``, a comparison,
an IF/SWITCH/COALESCE branch, a MIN/MAX/CLAMP argument, a FLOOR/CEIL step, a
DIVIDE fallback or a LOOKUP default becomes MONEY in that currency, and so does a
choice made only of such literals (``IF(c, 1500, 0)``). ``BASIC + 5%`` and
``BASIC + att.paid_days`` stay TYPE_MISMATCH. With ``expect="AMOUNT"`` a formula
whose result is a plain number is an amount in the component currency.

Warnings (``Analysis.warnings``, never blocking): NULL_VALUE when a value that can
be NULL (a variable whose source can be switched off, data that can be empty, a
component outside the structure) reaches arithmetic, a comparison, a function
argument or the result without COALESCE (EC-CMP-03); EMPTY_TAG when SUM_TAG reads
a tag no component in the structure carries.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Collection, Iterable, Literal, Mapping

from app.payroll.formula import ast
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
from app.payroll.formula.errors import ErrorCode, FormulaError, FormulaIssue
from app.payroll.formula.functions import FUNCTIONS, MAX_PLACES, MIN_PLACES
from app.payroll.formula.parser import enforce_limits, parse
from app.payroll.formula.tables import Table, TableKind, TableSig, TableSource, resolve_table
from app.payroll.formula.types import (
    ANY,
    BOOLEAN,
    DATE,
    DECIMAL,
    INTEGER,
    FType,
    Kind,
    money,
    validate_currency,
)
from app.payroll.formula.variables import (
    CUSTOM_ATTRIBUTE_PREFIX,
    NAMESPACES,
    VARIABLES,
    Stage,
    is_protected,
)

Expect = Literal["AMOUNT", "NUMBER", "BOOLEAN", "ANY"]
EXPECTS: tuple[str, ...] = ("AMOUNT", "NUMBER", "BOOLEAN", "ANY")
EMPTY_TAG = "EMPTY_TAG"
_NUMERIC = frozenset({Kind.DECIMAL, Kind.INTEGER})
_ORDERED = frozenset({Kind.MONEY, Kind.DECIMAL, Kind.INTEGER, Kind.DATE})


@dataclass(frozen=True)
class ComponentSig:
    """What a formula may know about another component."""

    currency: str
    stage: int | None = None
    optional: bool = False  # not in every employee's structure: NULL when absent, never a dependency

    def __post_init__(self) -> None:
        validate_currency(self.currency)
        if self.stage is not None and (isinstance(self.stage, bool) or not isinstance(self.stage, int)):
            raise TypeError("stage must be an int")


@dataclass(frozen=True)
class TypeEnv:
    """Static environment of one formula.

    ``currency``: the component / pay-group currency the formula's amount is in.
    ``components``: every component code the formula may reference.
    ``tags``: every known tag mapped to the structure's components that carry it.
    ``tables``: table signatures (or tables, or a TableProvider) by code.
    ``attributes``: declared custom ``emp.attr.<name>`` attributes and their types.
    ``component``/``stage``: the component being defined, for self-reference and
    STAGE_VIOLATION checks. ``allow_protected``: country-pack formulas only.
    ``expect``: AMOUNT (money or a plain number), NUMBER, BOOLEAN or ANY.
    """

    currency: str
    components: Mapping[str, ComponentSig] = field(default_factory=dict)
    tags: Mapping[str, Collection[str]] = field(default_factory=dict)
    tables: TableSource = field(default_factory=dict)
    attributes: Mapping[str, FType] = field(default_factory=dict)
    component: str | None = None
    stage: int | None = None
    allow_protected: bool = False
    expect: Expect = "AMOUNT"

    def __post_init__(self) -> None:
        validate_currency(self.currency)
        if self.expect not in EXPECTS:
            raise ValueError(f"expect must be one of {EXPECTS}, got {self.expect!r}")
        if self.stage is not None and (isinstance(self.stage, bool) or not isinstance(self.stage, int)):
            raise TypeError("stage must be an int")
        for code, sig in self.components.items():
            if not isinstance(sig, ComponentSig):
                raise TypeError(f"components[{code!r}] must be a ComponentSig")
        for name, ftype in self.attributes.items():
            if not isinstance(ftype, FType) or ftype.kind in (Kind.NULL, Kind.ANY):
                raise TypeError(f"attributes[{name!r}] must be a concrete FType")
        object.__setattr__(self, "tags", {tag: frozenset(codes) for tag, codes in self.tags.items()})


@dataclass(frozen=True)
class Analysis:
    source: str | None
    expr: Expr | None
    type: FType | None  # type of the expression; None when it does not parse
    components: frozenset[str]  # components read directly (BASIC)
    rates: frozenset[str]  # RATE("X")
    tags: frozenset[str]  # SUM_TAG / SUM_TAG_RATE
    inputs: frozenset[str]  # INPUT / UNITS
    history: frozenset[str]  # PREV / YTD
    variables: frozenset[str]
    tables: frozenset[str]
    functions: frozenset[str]
    dependencies: frozenset[str]  # structure components that must be calculated first
    errors: tuple[FormulaIssue, ...]
    warnings: tuple[FormulaIssue, ...]
    node_count: int
    depth: int

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type) if self.type is not None else None,
            "components": sorted(self.components),
            "rates": sorted(self.rates),
            "tags": sorted(self.tags),
            "inputs": sorted(self.inputs),
            "history": sorted(self.history),
            "variables": sorted(self.variables),
            "tables": sorted(self.tables),
            "functions": sorted(self.functions),
            "dependencies": sorted(self.dependencies),
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "node_count": self.node_count,
            "depth": self.depth,
        }


def check(source: str | Expr, env: TypeEnv) -> Analysis:
    """Save-time validation. Raises FormulaError (first problem; all in ``.issues``)."""
    analysis = diagnose(source, env)
    if analysis.errors:
        raise FormulaError.from_issue(analysis.errors[0], analysis.errors)
    return analysis


def diagnose(source: str | Expr, env: TypeEnv) -> Analysis:
    """Every error and warning of a formula, without raising (formula editor)."""
    if not isinstance(env, TypeEnv):
        raise TypeError("env must be a TypeEnv")
    text = source if isinstance(source, str) else None
    if isinstance(source, str):
        try:
            expr = parse(source)
        except FormulaError as exc:
            return _empty(text, exc.to_issue())
    elif isinstance(source, ast.NODE_TYPES):
        expr = source
        try:
            enforce_limits(expr)
        except FormulaError as exc:
            return _empty(None, exc.to_issue())
    else:
        raise TypeError("source must be a formula string or a parsed expression")
    typer = Typer(env)
    result = typer.run(expr)
    return Analysis(
        source=text,
        expr=expr,
        type=result,
        components=frozenset(typer.components),
        rates=frozenset(typer.rates),
        tags=frozenset(typer.tags),
        inputs=frozenset(typer.inputs),
        history=frozenset(typer.history),
        variables=frozenset(typer.variables),
        tables=frozenset(typer.tables),
        functions=frozenset(typer.functions),
        dependencies=frozenset(typer.dependencies),
        errors=tuple(typer.errors),
        warnings=tuple(typer.warnings),
        node_count=ast.node_count(expr),
        depth=ast.depth(expr),
    )


def _empty(source: str | None, issue: FormulaIssue) -> Analysis:
    nothing: frozenset[str] = frozenset()
    return Analysis(source, None, None, nothing, nothing, nothing, nothing, nothing, nothing, nothing, nothing,
                    nothing, (issue,), (), 0, 0)


@dataclass(frozen=True)
class _Info:
    type: FType
    nulls: tuple[Expr, ...] = ()  # references that may make this value NULL
    promotable: bool = False  # a plain number literal, or a choice made only of them


_UNKNOWN = _Info(ANY)


def describe(ftype: FType) -> str:
    kind = ftype.kind
    if kind is Kind.MONEY:
        return f"an amount in {ftype.currency}" if ftype.currency else "an amount"
    return {
        Kind.DECIMAL: "a number",
        Kind.INTEGER: "a whole number",
        Kind.BOOLEAN: "a TRUE/FALSE value",
        Kind.DATE: "a date",
        Kind.STRING: "text",
        Kind.NULL: "NULL",
        Kind.ANY: "an unknown value",
    }[kind]


def _stage_name(stage: int) -> str:
    try:
        return f"stage {stage} ({Stage(stage).name.lower().replace('_', ' ')})"
    except ValueError:
        return f"stage {stage}"


class Typer:
    """One typing pass over a tree. ``types`` maps id(node) to its effective type after promotion."""

    def __init__(self, env: TypeEnv, *, deferred_tables: bool = False, tables: TableSource | None = None) -> None:
        self.env = env
        self.deferred_tables = deferred_tables  # evaluation: an unknown table is TABLE_NOT_FOUND at run time
        self.table_source: TableSource = tables if tables is not None else env.tables
        self.types: dict[int, FType] = {}
        self.errors: list[FormulaIssue] = []
        self.warnings: list[FormulaIssue] = []
        self._warned: set[int] = set()
        self.components: set[str] = set()
        self.rates: set[str] = set()
        self.tags: set[str] = set()
        self.inputs: set[str] = set()
        self.history: set[str] = set()
        self.variables: set[str] = set()
        self.tables: set[str] = set()
        self.functions: set[str] = set()
        self.dependencies: set[str] = set()
        self._table_cache: dict[str, Table | TableSig | None] = {}

    # -- reporting -------------------------------------------------------------------------------

    def fail(self, code: ErrorCode, message: str, node: Expr, path: Iterable[str] | None = None) -> _Info:
        self.errors.append(
            FormulaIssue(code.value, message, "ERROR", node.pos, node.end, tuple(path) if path is not None else None)
        )
        return _UNKNOWN

    def consume(self, *infos: _Info) -> None:
        """A value that may be NULL is used where NULL is an error: warn once per reference."""
        for info in infos:
            for ref in info.nulls:
                if id(ref) in self._warned:
                    continue
                self._warned.add(id(ref))
                self.warnings.append(
                    FormulaIssue(ErrorCode.NULL_VALUE.value, self._null_message(ref), "WARNING", ref.pos, ref.end)
                )

    def _null_message(self, ref: Expr) -> str:
        text = render(ref)
        fix = f"use COALESCE({text}, <default>) to choose a value"
        if isinstance(ref, ComponentRef):
            return f"{text} is not in every salary structure and is NULL when absent; {fix}"
        if isinstance(ref, VariableRef):
            definition = VARIABLES.get(ref.name)
            if definition is not None and definition.source is not None:
                return f"{text} is NULL when {definition.source} is OFF; {fix}"
            return f"{text} can be empty for some employees; {fix}"
        return f"{text} can be NULL; {fix}"

    # -- entry -----------------------------------------------------------------------------------

    def run(self, expr: Expr) -> FType:
        info = self.infer(expr)
        self.consume(info)
        result = info.type
        if result.kind is Kind.ANY:
            return result
        env = self.env
        if env.expect == "AMOUNT":
            if result.is_money and result.currency != env.currency:
                self.fail(
                    ErrorCode.CURRENCY_MISMATCH,
                    f"the formula gives an amount in {result.currency} but this component is paid in "
                    f"{env.currency} ({env.currency} vs {result.currency}); use a contract-currency assignment "
                    "with an FX rule, never an implicit conversion",
                    expr,
                )
            elif not result.is_money and not result.is_numeric:
                self.fail(ErrorCode.TYPE_MISMATCH, f"the formula must give an amount, not {describe(result)}", expr)
        elif env.expect == "NUMBER" and not result.is_numeric:
            self.fail(ErrorCode.TYPE_MISMATCH, f"the formula must give a number, not {describe(result)}", expr)
        elif env.expect == "BOOLEAN" and result.kind is not Kind.BOOLEAN:
            self.fail(ErrorCode.TYPE_MISMATCH, f"the formula must give TRUE or FALSE, not {describe(result)}", expr)
        return result

    def infer(self, node: Expr) -> _Info:
        info = self._infer(node)
        self.types[id(node)] = info.type
        return info

    def _infer(self, node: Expr) -> _Info:
        if isinstance(node, Number):
            return _Info(INTEGER if node.integer else DECIMAL, promotable=not node.percent)
        if isinstance(node, String):
            return _Info(FType(Kind.STRING))
        if isinstance(node, Boolean):
            return _Info(BOOLEAN)
        if isinstance(node, DateLiteral):
            return _Info(DATE)
        if isinstance(node, ComponentRef):
            self.components.add(node.code)
            sig = self.component(node.code, node, "it")
            if sig is None:
                return _UNKNOWN
            if not sig.optional:
                self.dependencies.add(node.code)
            return _Info(money(sig.currency), (node,) if sig.optional else ())
        if isinstance(node, VariableRef):
            return self.variable(node)
        if isinstance(node, Unary):
            return self.unary(node)
        if isinstance(node, Binary):
            left, right = self.infer(node.left), self.infer(node.right)
            self.consume(left, right)
            return self.arithmetic(node.op, node, (node.left, left), (node.right, right))
        if isinstance(node, Compare):
            return self.comparison(node)
        if isinstance(node, Logical):
            infos = [self.infer(operand) for operand in node.operands]
            self.consume(*infos)
            for operand, info in zip(node.operands, infos):
                self.boolean(info, operand, node.op)
            return _Info(BOOLEAN)
        if isinstance(node, Call):
            problem = call_shape_problem(node)
            if problem is not None:
                return self.fail(ErrorCode.PARSE_ERROR, problem, node)
            self.functions.add(node.name)
            return getattr(self, f"call_{node.name.lower()}")(node)
        raise TypeError(f"not a formula node: {type(node).__name__}")

    # -- references ------------------------------------------------------------------------------

    def component(self, code: str, node: Expr, via: str) -> ComponentSig | None:
        """A component read in the current run: must exist, not be this component, not be later."""
        env = self.env
        sig = env.components.get(code)
        if sig is None:
            close = difflib.get_close_matches(code, sorted(env.components), n=1)
            hint = f"; did you mean {close[0]}?" if close else ""
            self.fail(ErrorCode.UNKNOWN_REFERENCE, f"unknown component {code}{hint}", node)
            return None
        if env.component is not None and code == env.component:
            message = f"{code} cannot refer to itself" if via == "it" else (
                f"{code} cannot depend on itself ({via} includes {code})")
            self.fail(ErrorCode.CYCLE_DETECTED, message, node, (code, code))
            return None
        if env.stage is not None and sig.stage is not None and sig.stage > env.stage:
            owner = env.component or "this formula"
            self.fail(
                ErrorCode.STAGE_VIOLATION,
                f"{code} is calculated in {_stage_name(sig.stage)}, after {owner} in {_stage_name(env.stage)}; "
                "a formula may only use components of its own or earlier stages",
                node,
                (owner, code),
            )
            return None
        return sig

    def known(self, code: str, node: Expr) -> ComponentSig | None:
        sig = self.env.components.get(code)
        if sig is None:
            self.fail(ErrorCode.UNKNOWN_REFERENCE, f"unknown component {code}", node)
        return sig

    def variable(self, node: VariableRef) -> _Info:
        env, name = self.env, node.name
        self.variables.add(name)
        if is_protected(name) and not env.allow_protected:
            return self.fail(
                ErrorCode.PROTECTED_ATTRIBUTE,
                f"{name} is a protected attribute; company formulas cannot use it (only country packs, where the "
                "law depends on it)",
                node,
            )
        if name.startswith(CUSTOM_ATTRIBUTE_PREFIX):
            attribute = name[len(CUSTOM_ATTRIBUTE_PREFIX):]
            ftype = env.attributes.get(attribute)
            if ftype is None:
                return self.fail(ErrorCode.UNKNOWN_REFERENCE, f"unknown custom attribute {attribute}", node)
            return _Info(self.resolve(ftype), (node,))
        namespace = node.namespace
        if namespace not in NAMESPACES:
            return self.fail(
                ErrorCode.UNKNOWN_REFERENCE,
                f"unknown namespace {namespace}; formulas can read {', '.join(NAMESPACES)}",
                node,
            )
        definition = VARIABLES.get(name)
        if definition is None:
            close = difflib.get_close_matches(name, [v for v in VARIABLES if v.startswith(namespace + ".")], n=1)
            hint = f"; did you mean {close[0]}?" if close else ""
            return self.fail(ErrorCode.UNKNOWN_REFERENCE, f"unknown variable {name}{hint}", node)
        return _Info(self.resolve(definition.type), (node,) if definition.may_be_null else ())

    def resolve(self, ftype: FType) -> FType:
        return money(self.env.currency) if ftype.is_money and ftype.currency is None else ftype

    # -- promotion and unification ---------------------------------------------------------------

    def promote(self, node: Expr, currency: str) -> None:
        target = money(currency)
        self.types[id(node)] = target
        if isinstance(node, Call):
            for child in _value_positions(node):
                self.promote(child, currency)

    def unify(self, items: list[tuple[Expr, _Info]], node: Expr, what: str,
              allowed: frozenset[Kind] | None = None) -> _Info:
        infos = [info for _, info in items]
        if any(info.type.kind is Kind.ANY for info in infos):
            return _UNKNOWN
        kinds = {info.type.kind for info in infos}
        if allowed is not None and not kinds <= allowed:
            bad = next(info.type for info in infos if info.type.kind not in allowed)
            return self.fail(ErrorCode.TYPE_MISMATCH, f"{what} cannot use {describe(bad)}", node)
        currencies: list[str] = []
        for info in infos:
            if info.type.is_money and info.type.currency not in currencies:
                currencies.append(info.type.currency)  # type: ignore[arg-type]
        if len(currencies) > 1:
            return self.fail(
                ErrorCode.CURRENCY_MISMATCH,
                f"{what} mixes amounts in {currencies[0]} and {currencies[1]} ({currencies[0]} vs {currencies[1]}); "
                "amounts are never converted implicitly",
                node,
            )
        if currencies:
            currency = currencies[0]
            for expr, info in items:
                if info.type.is_money:
                    continue
                if info.promotable:
                    self.promote(expr, currency)
                    continue
                return self.fail(
                    ErrorCode.TYPE_MISMATCH,
                    f"{what} mixes an amount in {currency} with {describe(info.type)} ({render(expr)}); only a plain "
                    "number such as 500 can stand for an amount",
                    node,
                )
            return _Info(money(currency))
        if kinds <= _NUMERIC:
            return _Info(INTEGER if kinds == {Kind.INTEGER} else DECIMAL,
                         promotable=all(info.promotable for info in infos))
        if len(kinds) == 1:
            return _Info(infos[0].type)
        first = infos[0].type
        second = next(
            info.type for info in infos
            if (info.type.kind not in _NUMERIC if first.is_numeric else info.type.kind is not first.kind)
        )
        return self.fail(ErrorCode.TYPE_MISMATCH, f"{what} mixes {describe(first)} and {describe(second)}", node)

    def boolean(self, info: _Info, node: Expr, what: str) -> None:
        if info.type.kind not in (Kind.BOOLEAN, Kind.ANY):
            self.fail(ErrorCode.TYPE_MISMATCH, f"{what} needs a TRUE/FALSE condition, not {describe(info.type)}", node)

    def numeric_or_money(self, info: _Info, node: Expr, what: str) -> bool:
        kind = info.type.kind
        if kind is Kind.ANY:
            return False
        if kind is Kind.MONEY or kind in _NUMERIC:
            return True
        self.fail(ErrorCode.TYPE_MISMATCH, f"{what} needs a number or an amount, not {describe(info.type)}", node)
        return False

    # -- operators -------------------------------------------------------------------------------

    def unary(self, node: Unary) -> _Info:
        info = self.infer(node.operand)
        self.consume(info)
        if node.op == "NOT":
            self.boolean(info, node.operand, "NOT")
            return _Info(BOOLEAN)
        if not self.numeric_or_money(info, node.operand, "minus"):
            return _UNKNOWN
        return _Info(info.type)

    def arithmetic(self, op: str, node: Expr, left: tuple[Expr, _Info], right: tuple[Expr, _Info],
                   *, zero_is_error: bool = True) -> _Info:
        (left_node, a), (right_node, b) = left, right
        ta, tb = a.type, b.type
        if Kind.ANY in (ta.kind, tb.kind):
            return _UNKNOWN
        verb = {"+": "add", "-": "subtract", "*": "multiply", "/": "divide"}[op]
        for info, operand in ((a, left_node), (b, right_node)):
            if not info.type.is_money and not info.type.is_numeric:
                return self.fail(ErrorCode.TYPE_MISMATCH, f"cannot {verb} {describe(info.type)} ({render(operand)})",
                                 operand)
        if op in ("+", "-"):
            if ta.is_money or tb.is_money:
                return self.unify([left, right], node, f"'{op}'")
            return _Info(INTEGER if ta.kind is tb.kind is Kind.INTEGER else DECIMAL)
        if op == "*":
            if ta.is_money and tb.is_money:
                return self.fail(ErrorCode.TYPE_MISMATCH, "an amount cannot be multiplied by an amount", node)
            if ta.is_money or tb.is_money:
                return _Info(ta if ta.is_money else tb)
            return _Info(INTEGER if ta.kind is tb.kind is Kind.INTEGER else DECIMAL)
        if zero_is_error and isinstance(right_node, Number) and right_node.value.is_zero():
            return self.fail(ErrorCode.DIVISION_BY_ZERO,
                             "division by zero; use DIVIDE(a, b, fallback) when the divisor can be 0", node)
        if ta.is_money and tb.is_money:
            if ta.currency != tb.currency:
                return self.fail(
                    ErrorCode.CURRENCY_MISMATCH,
                    f"cannot divide an amount in {ta.currency} by one in {tb.currency} ({ta.currency} vs "
                    f"{tb.currency}); amounts are never converted implicitly",
                    node,
                )
            return _Info(DECIMAL)
        if tb.is_money:
            return self.fail(ErrorCode.TYPE_MISMATCH, "a number cannot be divided by an amount", node)
        return _Info(ta if ta.is_money else DECIMAL)

    def comparison(self, node: Compare) -> _Info:
        left, right = self.infer(node.left), self.infer(node.right)
        self.consume(left, right)
        unified = self.unify([(node.left, left), (node.right, right)], node, f"the comparison '{node.op}'")
        kind = unified.type.kind
        if kind is Kind.ANY:
            return _UNKNOWN
        if node.op not in ("=", "<>") and kind not in _ORDERED:
            return self.fail(ErrorCode.TYPE_MISMATCH, f"'{node.op}' cannot order {describe(unified.type)}; "
                             "use = or <>", node)
        return _Info(BOOLEAN)

    # -- functions: math -------------------------------------------------------------------------

    def _ordered_values(self, node: Call) -> _Info:
        infos = [self.infer(arg) for arg in node.args]
        self.consume(*infos)
        return self.unify(list(zip(node.args, infos)), node, node.name, _ORDERED)

    call_min = _ordered_values
    call_max = _ordered_values

    def call_clamp(self, node: Call) -> _Info:
        info = self._ordered_values(node)
        lo, hi = node.args[1], node.args[2]
        if isinstance(lo, Number) and isinstance(hi, Number) and lo.value > hi.value:
            return self.fail(ErrorCode.INVALID_ARGUMENT, "CLAMP needs lo <= hi", node)
        return info

    def call_abs(self, node: Call) -> _Info:
        info = self.infer(node.args[0])
        self.consume(info)
        if not self.numeric_or_money(info, node.args[0], "ABS"):
            return _UNKNOWN
        return _Info(info.type)

    def _round(self, node: Call) -> _Info:
        x, places = (self.infer(arg) for arg in node.args[:2])
        if len(node.args) > 2:
            self.infer(node.args[2])
        self.consume(x, places)
        ok = self.numeric_or_money(x, node.args[0], node.name)
        place_node = node.args[1]
        if places.type.kind is not Kind.INTEGER:
            if places.type.kind is not Kind.ANY:
                self.fail(ErrorCode.TYPE_MISMATCH, f"{node.name} places must be a whole number", place_node)
            return _UNKNOWN
        if isinstance(place_node, Number) and not MIN_PLACES <= place_node.value <= MAX_PLACES:
            return self.fail(ErrorCode.INVALID_ARGUMENT,
                             f"{node.name} places must be between {MIN_PLACES} and {MAX_PLACES}", place_node)
        if not ok:
            return _UNKNOWN
        return _Info(x.type if x.type.is_money else DECIMAL)

    call_round = _round
    call_roundup = _round
    call_rounddown = _round

    def _step(self, node: Call) -> _Info:
        x, step = (self.infer(arg) for arg in node.args)
        self.consume(x, step)
        step_node = node.args[1]
        if isinstance(step_node, Number):
            if step_node.value.is_zero():
                return self.fail(ErrorCode.DIVISION_BY_ZERO, f"{node.name} step cannot be 0", step_node)
            if step_node.value < 0:
                return self.fail(ErrorCode.INVALID_ARGUMENT, f"{node.name} step must be positive", step_node)
        if not (self.numeric_or_money(x, node.args[0], node.name)
                and self.numeric_or_money(step, step_node, node.name)):
            return _UNKNOWN
        if x.type.is_money or step.type.is_money:
            unified = self.unify(list(zip(node.args, (x, step))), node, node.name)
            return _Info(unified.type)
        return _Info(INTEGER if x.type.kind is step.type.kind is Kind.INTEGER else DECIMAL)

    call_floor = _step
    call_ceil = _step

    def call_divide(self, node: Call) -> _Info:
        a, b, fallback = (self.infer(arg) for arg in node.args)
        self.consume(a, b)
        quotient = self.arithmetic("/", node, (node.args[0], a), (node.args[1], b), zero_is_error=False)
        if quotient.type.kind is Kind.ANY or fallback.type.kind is Kind.ANY:
            return _UNKNOWN
        unified = self.unify([(node, quotient), (node.args[2], fallback)], node, "DIVIDE")
        if unified.type.kind is Kind.ANY:
            return _UNKNOWN
        return _Info(quotient.type, fallback.nulls)

    # -- functions: logic ------------------------------------------------------------------------

    def call_if(self, node: Call) -> _Info:
        condition = self.infer(node.args[0])
        self.consume(condition)
        self.boolean(condition, node.args[0], "IF")
        then, otherwise = self.infer(node.args[1]), self.infer(node.args[2])
        unified = self.unify([(node.args[1], then), (node.args[2], otherwise)], node, "IF")
        if unified.type.kind is Kind.ANY:
            return _UNKNOWN
        return _Info(unified.type, then.nulls + otherwise.nulls, unified.promotable)

    def call_switch(self, node: Call) -> _Info:
        args = node.args
        keys = [args[0], *args[1:-1:2]]
        values = [*args[2:-1:2], args[-1]]
        key_infos = [self.infer(key) for key in keys]
        self.consume(*key_infos)
        self.unify(list(zip(keys, key_infos)), node, "SWITCH keys")
        value_infos = [self.infer(value) for value in values]
        unified = self.unify(list(zip(values, value_infos)), node, "SWITCH")
        if unified.type.kind is Kind.ANY:
            return _UNKNOWN
        return _Info(unified.type, tuple(ref for info in value_infos for ref in info.nulls), unified.promotable)

    def call_in(self, node: Call) -> _Info:
        infos = [self.infer(arg) for arg in node.args]
        self.consume(*infos)
        if self.unify(list(zip(node.args, infos)), node, "IN").type.kind is Kind.ANY:
            return _UNKNOWN
        return _Info(BOOLEAN)

    def call_coalesce(self, node: Call) -> _Info:
        infos = [self.infer(arg) for arg in node.args]
        unified = self.unify(list(zip(node.args, infos)), node, "COALESCE")
        if unified.type.kind is Kind.ANY:
            return _UNKNOWN
        nulls = tuple(ref for info in infos for ref in info.nulls) if all(info.nulls for info in infos) else ()
        return _Info(unified.type, nulls, unified.promotable)

    # -- functions: components -------------------------------------------------------------------

    def _tag(self, node: Call) -> _Info:
        tag = _code(node)
        self.tags.add(tag)
        members = self.env.tags.get(tag)
        if members is None:
            return self.fail(ErrorCode.UNKNOWN_REFERENCE, f"unknown tag {tag}", node.args[0])
        currencies: list[str] = []
        failed = False
        for code in sorted(members):
            sig = self.component(code, node, f'{node.name}("{tag}")')
            if sig is None:
                failed = True
                continue
            self.dependencies.add(code)
            if sig.currency not in currencies:
                currencies.append(sig.currency)
        if failed:
            return _UNKNOWN
        if len(currencies) > 1:
            return self.fail(
                ErrorCode.CURRENCY_MISMATCH,
                f'{node.name}("{tag}") adds components in {currencies[0]} and {currencies[1]} '
                f"({currencies[0]} vs {currencies[1]})",
                node,
            )
        if not members:
            self.warnings.append(FormulaIssue(
                EMPTY_TAG, f'no component in this structure has the tag {tag}; {node.name}("{tag}") is 0',
                "WARNING", node.pos, node.end))
        return _Info(money(currencies[0] if currencies else self.env.currency))

    call_sum_tag = _tag
    call_sum_tag_rate = _tag

    def call_rate(self, node: Call) -> _Info:
        code = _code(node)
        self.rates.add(code)
        sig = self.component(code, node, f'RATE("{code}")')
        if sig is None:
            return _UNKNOWN
        if not sig.optional:
            self.dependencies.add(code)
        return _Info(money(sig.currency), (node,) if sig.optional else ())

    def call_prorate(self, node: Call) -> _Info:
        info = self.infer(node.args[0])
        self.consume(info)
        if not self.numeric_or_money(info, node.args[0], "PRORATE"):
            return _UNKNOWN
        return _Info(info.type if info.type.is_money else DECIMAL)

    def _amount_of(self, node: Call, bucket: set[str]) -> _Info:
        code = _code(node)
        bucket.add(code)
        sig = self.known(code, node.args[0])
        return _Info(money(sig.currency)) if sig is not None else _UNKNOWN

    def call_input(self, node: Call) -> _Info:
        return self._amount_of(node, self.inputs)

    def call_units(self, node: Call) -> _Info:
        code = _code(node)
        self.inputs.add(code)
        return _Info(DECIMAL) if self.known(code, node.args[0]) is not None else _UNKNOWN

    def call_prev(self, node: Call) -> _Info:
        return self._amount_of(node, self.history)

    call_ytd = call_prev

    # -- functions: tables -----------------------------------------------------------------------

    def table(self, node: Call, kind: TableKind) -> TableSig | None:
        code = _code(node)
        self.tables.add(code)
        if code not in self._table_cache:
            self._table_cache[code] = resolve_table(self.table_source, code)
        found = self._table_cache[code]
        if found is None:
            if not self.deferred_tables:
                self.fail(ErrorCode.UNKNOWN_REFERENCE, f"unknown table {code}", node.args[0])
            return None
        sig = found.signature if isinstance(found, Table) else found
        if not isinstance(sig, TableSig):
            raise TypeError(f"table source returned {type(found).__name__} for {code}")
        if sig.kind is not kind:
            self.fail(ErrorCode.TYPE_MISMATCH, f'{code} is a {sig.kind} table; read it with {sig.kind}("{code}", …)',
                      node.args[0])
            return None
        return sig

    def _bounded(self, node: Call, kind: TableKind) -> _Info:
        sig = self.table(node, kind)
        x = self.infer(node.args[1])
        self.consume(x)
        if sig is None or x.type.kind is Kind.ANY:
            return _UNKNOWN
        x_node = node.args[1]
        code = _code(node)
        if sig.key_currency is not None:
            if x.type.is_money and x.type.currency != sig.key_currency:
                return self.fail(
                    ErrorCode.CURRENCY_MISMATCH,
                    f"{code} is banded in {sig.key_currency} but the value is in {x.type.currency} "
                    f"({sig.key_currency} vs {x.type.currency})",
                    x_node,
                )
            if not x.type.is_money:
                if not x.promotable:
                    return self.fail(ErrorCode.TYPE_MISMATCH,
                                     f"{code} is banded by amounts in {sig.key_currency}, not {describe(x.type)}",
                                     x_node)
                self.promote(x_node, sig.key_currency)
        elif not x.type.is_numeric:
            return self.fail(ErrorCode.TYPE_MISMATCH, f"{code} is banded by plain numbers, not {describe(x.type)}",
                             x_node)
        if kind is TableKind.SLAB:
            return _Info(money(sig.key_currency) if sig.key_currency else DECIMAL)
        return _Info(money(sig.value_currency) if sig.value_currency else DECIMAL)

    def call_slab(self, node: Call) -> _Info:
        return self._bounded(node, TableKind.SLAB)

    def call_band(self, node: Call) -> _Info:
        return self._bounded(node, TableKind.BAND)

    def call_lookup(self, node: Call) -> _Info:
        sig = self.table(node, TableKind.LOOKUP)
        key, default = self.infer(node.args[1]), self.infer(node.args[2])
        self.consume(key)
        if key.type.kind not in (Kind.STRING, Kind.ANY):
            self.fail(ErrorCode.TYPE_MISMATCH, f"LOOKUP keys are text, not {describe(key.type)}", node.args[1])
            return _UNKNOWN
        if sig is None or default.type.kind is Kind.ANY:
            return _UNKNOWN
        value_type = money(sig.value_currency) if sig.value_currency else DECIMAL
        probe = _Info(value_type)
        unified = self.unify([(node, probe), (node.args[2], default)], node, "LOOKUP default")
        if unified.type.kind is Kind.ANY:
            return _UNKNOWN
        return _Info(value_type, default.nulls)

    # -- functions: dates ------------------------------------------------------------------------

    def call_date(self, node: Call) -> _Info:
        infos = [self.infer(arg) for arg in node.args]
        self.consume(*infos)
        for arg, info in zip(node.args, infos):
            if info.type.kind not in (Kind.INTEGER, Kind.ANY):
                return self.fail(ErrorCode.TYPE_MISMATCH, f"DATE needs whole numbers, not {describe(info.type)}", arg)
        return _Info(DATE)

    def _dates(self, node: Call) -> _Info:
        infos = [self.infer(arg) for arg in node.args]
        self.consume(*infos)
        for arg, info in zip(node.args, infos):
            if info.type.kind not in (Kind.DATE, Kind.ANY):
                return self.fail(ErrorCode.TYPE_MISMATCH, f"{node.name} needs a date, not {describe(info.type)}", arg)
        return _Info(INTEGER)

    call_year = _dates
    call_month = _dates
    call_days_between = _dates
    call_months_between = _dates
    call_years_between = _dates


def call_shape_problem(node: Call) -> str | None:
    """Arity and quoted arguments of a call (always true for parsed trees; guards hand-built ones)."""
    spec = FUNCTIONS.get(node.name)
    if spec is None or spec.keyword:
        return f"unknown function {node.name}"
    count = len(node.args)
    if count < spec.min_args or (spec.max_args is not None and count > spec.max_args) or (
            node.name == "SWITCH" and count % 2):
        return f"{spec.signature} cannot take {count} arguments"
    for index in spec.literals:
        if index < count and not isinstance(node.args[index], String):
            return f"{spec.signature} needs a quoted code as argument {index + 1}"
    return None


def _code(node: Call) -> str:
    first = node.args[0]
    assert isinstance(first, String)
    return first.value


def _value_positions(node: Call) -> tuple[Expr, ...]:
    """Arguments whose value can become the call's value (where promotion applies)."""
    args = node.args
    if node.name == "IF":
        return args[1:]
    if node.name == "SWITCH":
        return (*args[2:-1:2], args[-1])
    if node.name in ("COALESCE", "MIN", "MAX", "CLAMP"):
        return args
    return ()
