"""The safe formula language of payroll components (3.3 §3–§5).

A small spreadsheet-like language for HR users, parsed into an immutable syntax
tree and evaluated with exact ``Decimal`` arithmetic. Nothing in a formula is ever
passed to ``eval``/``exec``/``compile``, SQL or JavaScript; the tokenizer accepts
only the characters of the language and rejects identifiers containing ``__``.

* ``parse(source) -> Expr``: syntax and limits (PARSE_ERROR, LIMIT_EXCEEDED).
* ``check(source, env) -> Analysis``: save-time gate (references, types,
  currencies, stages, protected attributes); raises FormulaError, returns warnings.
* ``evaluate(expr, ctx) -> EvalResult``: value, step-by-step trace and the value of
  every reference; data-dependent problems are returned in ``EvalResult.error``.
* ``dependency_order(deps, stage_of) -> list[str]``: calculation order, CYCLE_DETECTED
  with the full path.
* ``method_formula(method, params)``: the calculation methods of 3.3 §3 as formulas.

The net-to-gross solver (3.3 §6.3: bisection on gross, at most 50 iterations, one
minor unit tolerance, non-convergence is an exception) lives in the engine
(``app.payroll.engine``); it evaluates these formulas repeatedly and is the only
sanctioned way for an earlier stage to depend on net pay.

The package is pure: no database, settings, clock, randomness, network or
environment variables.
"""

from app.payroll.formula.analysis import Analysis, ComponentSig, Expect, TypeEnv, check, diagnose
from app.payroll.formula.ast import Expr, node_count, render, shape, walk
from app.payroll.formula.dependencies import dependency_order, expand_tags
from app.payroll.formula.errors import ErrorCode, FormulaError, FormulaIssue
from app.payroll.formula.evaluator import EvalContext, EvalResult, Proration, check_floor, evaluate
from app.payroll.formula.functions import FUNCTIONS, ROUNDING_MODES, FunctionDef
from app.payroll.formula.lexer import MAX_LENGTH, tokenize
from app.payroll.formula.methods import CalcMethod, method_formula
from app.payroll.formula.parser import MAX_DEPTH, MAX_NODES, is_valid_component_code, parse
from app.payroll.formula.tables import Table, TableKind, TableProvider, TableRow, TableSig
from app.payroll.formula.types import CALC_CONTEXT, FType, Kind, MoneyValue, Value, format_value, money
from app.payroll.formula.variables import NAMESPACES, PROTECTED_ATTRIBUTES, VARIABLES, Stage, VariableDef

__all__ = [
    "CALC_CONTEXT",
    "FUNCTIONS",
    "MAX_DEPTH",
    "MAX_LENGTH",
    "MAX_NODES",
    "NAMESPACES",
    "PROTECTED_ATTRIBUTES",
    "ROUNDING_MODES",
    "VARIABLES",
    "Analysis",
    "CalcMethod",
    "ComponentSig",
    "ErrorCode",
    "EvalContext",
    "EvalResult",
    "Expect",
    "Expr",
    "FType",
    "FormulaError",
    "FormulaIssue",
    "FunctionDef",
    "Kind",
    "MoneyValue",
    "Proration",
    "Stage",
    "Table",
    "TableKind",
    "TableProvider",
    "TableRow",
    "TableSig",
    "TypeEnv",
    "Value",
    "VariableDef",
    "check",
    "check_floor",
    "dependency_order",
    "diagnose",
    "evaluate",
    "expand_tags",
    "format_value",
    "is_valid_component_code",
    "method_formula",
    "money",
    "node_count",
    "parse",
    "render",
    "shape",
    "tokenize",
    "walk",
]
