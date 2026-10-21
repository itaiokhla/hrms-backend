"""Calculation methods of pay components (3.3 §3) expressed in the formula language.

Every method that computes from other values compiles to a canonical formula, so
it is checked, ordered and traced exactly like a hand-written one:

* ``PERCENT_OF``: ``BASIC * 40%``, ``(BASIC + DA) * 40%`` or ``SUM_TAG("TAG") * 40%``;
* ``RATE_X_UNITS``: ``(rate) * (units)``, for example ``ot.hourly_rate * 1.5`` ×
  ``ot.hours_workday``;
* ``SLAB``: ``SLAB("TABLE", base)`` (progressive) or ``BAND("TABLE", base)`` (flat band);
* ``INPUT``: ``INPUT("CODE")``, the approved wage inputs of the period;
* ``BALANCING``: ``ctc.monthly - (BASIC + HRA + …)``; it depends on every other CTC
  component and must not go below zero (``evaluator.check_floor``, EC-CMP-04);
* ``FORMULA``: the expression as written.

``FIXED`` (the assignment's amount) and ``SYSTEM`` (engine or country pack) have no
formula: ``method_formula`` returns ``None`` for them.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Sequence

from app.payroll.formula.ast import render
from app.payroll.formula.errors import ErrorCode, FormulaError
from app.payroll.formula.functions import CODE
from app.payroll.formula.parser import is_valid_component_code, parse
from app.payroll.formula.types import plain_decimal


class CalcMethod(StrEnum):
    FIXED = "FIXED"
    PERCENT_OF = "PERCENT_OF"
    FORMULA = "FORMULA"
    RATE_X_UNITS = "RATE_X_UNITS"
    SLAB = "SLAB"
    INPUT = "INPUT"
    BALANCING = "BALANCING"
    SYSTEM = "SYSTEM"


def _invalid(message: str) -> FormulaError:
    return FormulaError(ErrorCode.INVALID_ARGUMENT, message)


def _canonical(source: str) -> str:
    return render(parse(source))


def _expression(source: Any, what: str) -> str:
    """A parameter that must be one complete expression on its own.

    Parsing it alone first means ``"1) + (2"`` can never be spliced into the
    surrounding template to change the meaning of the generated formula.
    """
    if not isinstance(source, str) or not source.strip():
        raise _invalid(f"{what} must be an expression")
    return render(parse(source))


def _code(code: Any, what: str) -> str:
    if not isinstance(code, str) or not is_valid_component_code(code):
        raise _invalid(f"{what} must be an UPPER_SNAKE component code, got {code!r}")
    return code


def _quoted(code: Any, what: str) -> str:
    if not isinstance(code, str) or not CODE.fullmatch(code) or "__" in code:
        raise _invalid(f"{what} must be an UPPER_SNAKE code, got {code!r}")
    return f'"{code}"'


def _sum(codes: Sequence[str], what: str) -> str:
    if isinstance(codes, str) or not codes:
        raise _invalid(f"{what} needs at least one component code")
    checked = [_code(code, what) for code in codes]
    if len(set(checked)) != len(checked):
        raise _invalid(f"{what} lists a component twice")
    return checked[0] if len(checked) == 1 else f"({' + '.join(checked)})"


def percent_of(percent: Decimal, *, bases: Sequence[str] = (), tag: str | None = None) -> str:
    """``percent`` (40 means 40%) of the sum of ``bases`` or of ``SUM_TAG(tag)``."""
    if isinstance(percent, bool) or not isinstance(percent, (Decimal, int)) or not Decimal(percent).is_finite():
        raise _invalid("percent must be a Decimal, as in Decimal('40') for 40%")
    if Decimal(percent) < 0:
        raise _invalid("percent cannot be negative")
    if (tag is None) == (not bases):
        raise _invalid("PERCENT_OF needs either base components or a tag, not both")
    base = f"SUM_TAG({_quoted(tag, 'tag')})" if tag is not None else _sum(bases, "PERCENT_OF")
    return _canonical(f"{base} * {plain_decimal(Decimal(percent))}%")


def rate_x_units(rate: str, units: str) -> str:
    """Rate (an expression: amount, variable or formula) multiplied by units (an expression)."""
    rate_text = _expression(rate, "the RATE_X_UNITS rate")
    units_text = _expression(units, "the RATE_X_UNITS units")
    return _canonical(f"({rate_text}) * ({units_text})")


def slab(table: str, base: str, *, progressive: bool = True) -> str:
    """``SLAB`` (progressive) or ``BAND`` (flat band) of ``table`` applied to the ``base`` expression."""
    base_text = _expression(base, "the SLAB base")
    function = "SLAB" if progressive else "BAND"
    return _canonical(f"{function}({_quoted(table, 'table')}, ({base_text}))")


def input_amount(code: str) -> str:
    """The approved wage-input amount of the component for the period."""
    return _canonical(f"INPUT({_quoted(code, 'input code')})")


def balancing(others: Sequence[str], *, total: str = "ctc.monthly", own: str | None = None) -> str:
    """``total`` minus every other CTC component (3.3 §3; must stay >= 0, EC-CMP-04)."""
    if own is not None and own in others:
        raise _invalid(f"the balancing component {own} cannot be part of its own total")
    total_text = _expression(total, "the BALANCING total")
    return _canonical(f"({total_text}) - {_sum(others, 'BALANCING')}")


def method_formula(method: CalcMethod | str, params: Mapping[str, Any]) -> str | None:
    """Canonical formula of a component's calculation method; None for FIXED and SYSTEM.

    ``params`` per method: PERCENT_OF ``percent`` + ``bases`` or ``tag``; FORMULA
    ``formula``; RATE_X_UNITS ``rate``, ``units``; SLAB ``table``, ``base``,
    ``progressive`` (default True); INPUT ``code``; BALANCING ``others``, ``total``
    (default ``ctc.monthly``), ``own``.
    """
    try:
        kind = CalcMethod(method)
    except ValueError:
        raise _invalid(f"unknown calculation method {method!r}") from None
    try:
        if kind in (CalcMethod.FIXED, CalcMethod.SYSTEM):
            return None
        if kind is CalcMethod.PERCENT_OF:
            return percent_of(params["percent"], bases=params.get("bases", ()), tag=params.get("tag"))
        if kind is CalcMethod.FORMULA:
            formula = params["formula"]
            if not isinstance(formula, str):
                raise _invalid("FORMULA needs the formula text")
            return _canonical(formula)
        if kind is CalcMethod.RATE_X_UNITS:
            return rate_x_units(params["rate"], params["units"])
        if kind is CalcMethod.SLAB:
            return slab(params["table"], params["base"], progressive=bool(params.get("progressive", True)))
        if kind is CalcMethod.INPUT:
            return input_amount(params["code"])
        return balancing(params["others"], total=params.get("total", "ctc.monthly"), own=params.get("own"))
    except KeyError as exc:
        raise _invalid(f"{kind} needs the parameter {exc.args[0]!r}") from None
