"""The function catalogue of the formula language (3.3 §4.2) and its pure helpers.

``FUNCTIONS`` is the single list of callable names: the parser rejects any other
name (UNKNOWN_REFERENCE) and checks arity and the arguments that must be quoted
codes (tags, component codes, table codes, rounding modes). Typing rules live in
``analysis``, run-time semantics in ``evaluator``; the arithmetic they share
(rounding modes, FLOOR/CEIL to a step, date differences) is here.

Conventions decided for the language:

* ``ROUND(x, places[, mode])`` rounds to ``places`` decimals (-12..12; negative
  rounds to tens, hundreds, ...); ``mode`` is one of ``ROUNDING_MODES`` and
  defaults to HALF_UP. ROUNDUP/ROUNDDOWN round away from / toward zero.
* ``FLOOR(x, step)`` / ``CEIL(x, step)`` go to the multiple of ``step`` below /
  above ``x`` (toward −∞ / +∞); ``step`` must be positive.
* ``MONTHS_BETWEEN(d1, d2)`` counts completed months from ``d1`` to ``d2`` (the
  day of month must be reached), negative when ``d2`` is earlier;
  ``YEARS_BETWEEN`` is that divided by 12, truncated; ``DAYS_BETWEEN`` is d2 − d1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
)
from enum import StrEnum
from typing import Mapping

ROUNDING_MODES: dict[str, str] = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_EVEN": ROUND_HALF_EVEN,
    "UP": ROUND_UP,
    "DOWN": ROUND_DOWN,
    "CEILING": ROUND_CEILING,
    "FLOOR": ROUND_FLOOR,
}
MIN_PLACES, MAX_PLACES = -12, 12
CODE = re.compile(r"[A-Z][A-Z0-9_]*")


class Quoted(StrEnum):
    """Kinds of arguments that must be written as a quoted code."""

    TAG = "TAG"
    CODE = "CODE"
    TABLE = "TABLE"
    MODE = "MODE"


@dataclass(frozen=True)
class FunctionDef:
    name: str
    group: str
    signature: str
    description: str
    min_args: int
    max_args: int | None  # None: any number of arguments
    literals: Mapping[int, Quoted] = field(default_factory=dict)
    keyword: bool = False  # AND, OR and NOT are also infix/prefix operators


def _f(name: str, group: str, signature: str, description: str, min_args: int, max_args: int | None,
       literals: Mapping[int, Quoted] | None = None, keyword: bool = False) -> FunctionDef:
    return FunctionDef(name, group, signature, description, min_args, max_args, dict(literals or {}), keyword)


FUNCTIONS: dict[str, FunctionDef] = {
    f.name: f
    for f in (
        _f("MIN", "Math", "MIN(a, b, …)", "Smallest value", 2, None),
        _f("MAX", "Math", "MAX(a, b, …)", "Largest value", 2, None),
        _f("ABS", "Math", "ABS(x)", "Absolute value", 1, 1),
        _f("ROUND", "Math", "ROUND(x, places[, mode])", "Round to places decimals (default HALF_UP)", 2, 3,
           {2: Quoted.MODE}),
        _f("ROUNDUP", "Math", "ROUNDUP(x, places)", "Round away from zero", 2, 2),
        _f("ROUNDDOWN", "Math", "ROUNDDOWN(x, places)", "Round toward zero", 2, 2),
        _f("FLOOR", "Math", "FLOOR(x, step)", "Multiple of step at or below x", 2, 2),
        _f("CEIL", "Math", "CEIL(x, step)", "Multiple of step at or above x", 2, 2),
        _f("CLAMP", "Math", "CLAMP(x, lo, hi)", "x limited to the range lo..hi", 3, 3),
        _f("DIVIDE", "Math", "DIVIDE(a, b, fallback)", "a / b, or fallback when b is zero", 3, 3),
        _f("IF", "Logic", "IF(cond, then, else)", "then when cond is TRUE, else otherwise", 3, 3),
        _f("SWITCH", "Logic", "SWITCH(x, k1, v1, …, default)", "Value of the first key equal to x", 4, None),
        _f("IN", "Logic", "IN(x, v1, v2, …)", "TRUE when x equals one of the values", 2, None),
        _f("COALESCE", "Logic", "COALESCE(a, b, …)", "First value that is not NULL", 2, None),
        _f("AND", "Logic", "a AND b, AND(a, b, …)", "TRUE when every condition is TRUE", 2, None, keyword=True),
        _f("OR", "Logic", "a OR b, OR(a, b, …)", "TRUE when any condition is TRUE", 2, None, keyword=True),
        _f("NOT", "Logic", "NOT x", "TRUE when x is FALSE", 1, 1, keyword=True),
        _f("SUM_TAG", "Components", 'SUM_TAG("TAG")',
           "Current-run amounts of the components with the tag, after proration", 1, 1, {0: Quoted.TAG}),
        _f("SUM_TAG_RATE", "Components", 'SUM_TAG_RATE("TAG")',
           "Full-period rates of the components with the tag, before proration", 1, 1, {0: Quoted.TAG}),
        _f("SUM_TAG_HOURLY", "Components", 'SUM_TAG_HOURLY("TAG")',
           "Hourly rate from the components with the tag: period amounts ÷ period.basis_days ÷ "
           "emp.standard_hours_per_day, per-day rates ÷ emp.standard_hours_per_day, per-hour rates as they are",
           1, 1, {0: Quoted.TAG}),
        _f("RATE", "Components", 'RATE("CODE")', "Full-period rate of a component, before proration", 1, 1,
           {0: Quoted.CODE}),
        _f("PRORATE", "Components", "PRORATE(x)", "x prorated by the current component's proration", 1, 1),
        _f("INPUT", "Components", 'INPUT("CODE")', "Approved wage-input amount for the period (0 when none)", 1, 1,
           {0: Quoted.CODE}),
        _f("UNITS", "Components", 'UNITS("CODE")', "Approved wage-input units for the period (0 when none)", 1, 1,
           {0: Quoted.CODE}),
        _f("PREV", "Components", 'PREV("CODE")', "Amount in the last finalized period (0 when none)", 1, 1,
           {0: Quoted.CODE}),
        _f("YTD", "Components", 'YTD("CODE")',
           "Tax-year-to-date amount, including declared previous employment (0 when none)", 1, 1, {0: Quoted.CODE}),
        _f("SLAB", "Tables", 'SLAB("TABLE", x)', "Progressive amount from a SLAB table", 2, 2, {0: Quoted.TABLE}),
        _f("BAND", "Tables", 'BAND("TABLE", x)', "Flat value of the band containing x", 2, 2, {0: Quoted.TABLE}),
        _f("LOOKUP", "Tables", 'LOOKUP("TABLE", key, default)', "Value for key, or default when absent", 3, 3,
           {0: Quoted.TABLE}),
        _f("DATE", "Dates", "DATE(y, m, d)", "A date", 3, 3),
        _f("YEAR", "Dates", "YEAR(d)", "Year of a date", 1, 1),
        _f("MONTH", "Dates", "MONTH(d)", "Month of a date (1–12)", 1, 1),
        _f("DAYS_BETWEEN", "Dates", "DAYS_BETWEEN(d1, d2)", "d2 − d1 in days", 2, 2),
        _f("MONTHS_BETWEEN", "Dates", "MONTHS_BETWEEN(d1, d2)", "Completed months from d1 to d2", 2, 2),
        _f("YEARS_BETWEEN", "Dates", "YEARS_BETWEEN(d1, d2)", "Completed years from d1 to d2", 2, 2),
    )
}

#: Functions that read component amounts of the current run (dependencies for ordering).
COMPONENT_READERS = frozenset({"SUM_TAG", "SUM_TAG_RATE", "SUM_TAG_HOURLY", "RATE"})
#: Functions whose result is a reference shown with its value in the formula tester.
REFERENCE_FUNCTIONS = frozenset({"SUM_TAG", "SUM_TAG_RATE", "SUM_TAG_HOURLY", "RATE", "INPUT", "UNITS", "PREV",
                                 "YTD"})

#: Units of a full-period rate (``EvalContext.rate_units``) that ``SUM_TAG_HOURLY`` converts to an hourly rate:
#: PERIOD (an amount for the whole pay period: FIXED, FORMULA, ... and the default), DAY (a RATE_X_UNITS rate per
#: paid day) and HOUR (a RATE_X_UNITS rate per hour). Any other unit (pieces, unknown) cannot be converted.
RATE_UNIT_PERIOD, RATE_UNIT_DAY, RATE_UNIT_HOUR = "PERIOD", "DAY", "HOUR"
HOURLY_CONVERTIBLE_UNITS = frozenset({RATE_UNIT_PERIOD, RATE_UNIT_DAY, RATE_UNIT_HOUR})


def round_to(number: Decimal, places: int, mode: str, context: Context) -> Decimal:
    exponent = Decimal(1).scaleb(-places, context)
    return number.quantize(exponent, rounding=ROUNDING_MODES[mode], context=context)


def floor_to(number: Decimal, step: Decimal, context: Context, *, ceiling: bool) -> Decimal:
    """The multiple of a positive ``step`` at or below (or above) ``number``, computed exactly."""
    quotient = context.divide_int(number, step)
    remainder = context.remainder(number, step)
    if not remainder.is_zero():
        if ceiling and number > 0:
            quotient = context.add(quotient, 1)
        elif not ceiling and number < 0:
            quotient = context.subtract(quotient, 1)
    return context.multiply(quotient, step)


def months_between(start: date, end: date) -> int:
    if end < start:
        return -months_between(end, start)
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return months


def years_between(start: date, end: date) -> int:
    months = months_between(start, end)
    return months // 12 if months >= 0 else -((-months) // 12)
