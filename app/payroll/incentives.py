"""Incentive plans (3.1 §5.9 ``INCENTIVE_PLAN``, 3.4 §13 wage-input source ``INCENTIVE_PLAN``): the pure rules.

``payroll.incentives.enabled`` ON: an incentive plan turns **approved achievement measures** into an incentive
earning. A measure is a wage input with ``source = INCENTIVE_PLAN`` whose ``units`` hold the measured achievement
(entered through the wage-input API or CSV import, approved through the approvals engine like every units input,
reserved at run lock and consumed exactly once by a finalized run through ``payroll_line_sources``, EC-VAR-01).
The plan (decision I-22, DECISIONS.md):

* **Payout window** (``payout_frequency``): whole calendar months counted from the first month of the pay
  calendar's tax year (January when the calendar declares none). MONTHLY = each month, QUARTERLY = three months,
  ANNUAL = twelve. A run pays a window only when the window's last day falls inside its pay period, and then
  pays every approved, unconsumed measure targeted at a pay period that starts on or after the window start and
  ends by the period end. Other runs pay no incentive and leave the measures untouched.
* **Target** (``targets``): the employee's own target wins over the target of the employee's grade; no target
  leaves ``inc.target`` NULL (a formula that needs it fails with an exception instead of paying a guess).
* **Formula** (``formula``): evaluated by the formula engine with the plan-only variables ``inc.measure`` (sum of
  the window's measures), ``inc.target`` and ``inc.achievement_pct`` (measure ÷ target × 100) besides the usual
  ``period.*``/``emp.*``/``att.*``/``ctc.*`` variables, the formula tables (``SLAB``/``BAND``/``LOOKUP``) and the
  employee's structure rates (``RATE("BASIC")``). A negative or failed result is a BLOCKING exception, never a
  silent zero.
* **Cap** (``cap``): the incentive of one payout is at most the cap, which must be in the pay currency (no
  implicit FX).
* **Measure source**: only ``IMPORT`` is supported. ``TASKS`` cannot be activated (no task measure can yet be
  consumed exactly once outside task pay); a plan that still says TASKS is a BLOCKING exception.

The module is pure: no database, settings, clock or network.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from app.payroll.formula.types import CALC_CONTEXT, DECIMAL
from app.payroll.formula.variables import VariableDef

MEASURE_SOURCE = "INCENTIVE_PLAN"
POLICY_TYPE = "INCENTIVE_PLAN"
SWITCH = "payroll.incentives.enabled"
SUPPORTED_MEASURE_SOURCES = ("IMPORT",)
PAYOUT_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "ANNUAL": 12}
# wage_inputs.units is NUMERIC(10, 4): a measure holds at most 999,999.9999 (enter large figures in thousands).
MEASURE_MAX = Decimal("999999.9999")
MEASURE_PLACES = 4
HUNDRED = Decimal(100)

LOCALS: dict[str, VariableDef] = {
    item.name: item for item in (
        VariableDef("inc.measure", DECIMAL, "Sum of the approved achievement measures of the payout window"),
        VariableDef("inc.target", DECIMAL, "The employee's target (own target, else the grade's); NULL when none",
                    nullable=True),
        VariableDef("inc.achievement_pct", DECIMAL, "inc.measure ÷ inc.target × 100; NULL without a positive target",
                    nullable=True),
    )
}


class IncentiveError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- payout windows


def _month_start(absolute_month: int) -> dt.date:
    """First day of a month numbered year × 12 + (month − 1)."""
    year, month0 = divmod(absolute_month, 12)
    return dt.date(year, month0 + 1, 1)


def _window_of(day: dt.date, months: int, anchor_month: int) -> tuple[dt.date, dt.date]:
    """The window of ``months`` calendar months, aligned on ``anchor_month``, that contains ``day``."""
    offset = anchor_month - 1
    relative = day.year * 12 + day.month - 1 - offset
    first = relative - relative % months + offset
    start = _month_start(first)
    last = _month_start(first + months - 1)
    return start, last.replace(day=calendar.monthrange(last.year, last.month)[1])


def payout_window(frequency: str, period_start: dt.date, period_end: dt.date,
                  tax_year_start: dt.date | None = None) -> tuple[dt.date, dt.date] | None:
    """The payout window whose last day falls inside the pay period [period_start, period_end], or None.

    Windows are whole calendar months aligned on the month of ``tax_year_start`` (January without one): a period
    that ends a window pays it; any other period pays no incentive.
    """
    months = PAYOUT_MONTHS.get(frequency)
    if months is None:
        raise IncentiveError("INCENTIVE_PLAN_INVALID", f"payout_frequency {frequency!r} is not one of "
                             f"{', '.join(PAYOUT_MONTHS)}")
    if period_end < period_start:
        raise IncentiveError("INCENTIVE_PLAN_INVALID", "the period ends before it starts")
    anchor = tax_year_start.month if tax_year_start is not None else 1
    start, end = _window_of(period_end, months, anchor)
    if end <= period_end:
        return start, end
    before = _window_of(start - dt.timedelta(days=1), months, anchor)
    if before[1] >= period_start:
        return before
    return None


def measure_in_window(target_start: dt.date, target_end: dt.date, window_start: dt.date, period_end: dt.date) -> bool:
    """Whether a measure targeted at the pay period [target_start, target_end] belongs to the payout."""
    return target_start >= window_start and target_end <= period_end


# --------------------------------------------------------------------------- targets


@dataclass(frozen=True)
class Target:
    value: Decimal | None
    source: str | None  # EMPLOYEE | GRADE | None


def resolve_target(targets: Sequence[Mapping[str, Any]], *, employee_id: int | None,
                   grade_id: int | None) -> Target:
    """The employee's own target, else the target of the employee's grade, else none."""
    own = grade = None
    for item in targets or ():
        value = item.get("target")
        if value is None:
            continue
        if employee_id is not None and item.get("employee_id") == employee_id:
            own = Decimal(str(value))
        elif grade_id is not None and item.get("grade_id") == grade_id and item.get("employee_id") is None:
            grade = Decimal(str(value))
    if own is not None:
        return Target(own, "EMPLOYEE")
    if grade is not None:
        return Target(grade, "GRADE")
    return Target(None, None)


def local_values(measure: Decimal, target: Decimal | None) -> dict[str, Any]:
    """The ``inc.*`` variables of one payout."""
    achievement = None
    if target is not None and target > 0:
        with localcontext(CALC_CONTEXT):
            achievement = measure / target * HUNDRED
    return {"inc.measure": measure, "inc.target": target, "inc.achievement_pct": achievement}


def check_measure(value: Decimal) -> Decimal:
    """A measure is a positive number with at most four decimals that fits wage_inputs.units."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise IncentiveError("MEASURE_INVALID", "The measure must be a number.")
    if value <= 0:
        raise IncentiveError("MEASURE_NOT_POSITIVE", "The measure must be greater than zero (a zero achievement "
                             "needs no measure).")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > MEASURE_PLACES:
        raise IncentiveError("MEASURE_INVALID", f"The measure allows at most {MEASURE_PLACES} decimal places.")
    if value > MEASURE_MAX:
        raise IncentiveError("MEASURE_TOO_LARGE", f"A measure holds at most {MEASURE_MAX:,} (enter large figures in "
                             "thousands, with the plan's targets in the same unit).")
    return value


def formula_problems(formula: str) -> list[str]:
    """Save-time check of a plan formula's ``inc.*`` references (the rest is checked when a run evaluates it)."""
    from app.payroll.formula import FormulaError, parse
    from app.payroll.formula.ast import VariableRef, walk

    try:
        expr = parse(formula)
    except FormulaError as error:
        return [f"The formula is not valid: {error.message}"]
    problems = []
    for node in walk(expr):
        if isinstance(node, VariableRef) and node.namespace == "inc" and node.name not in LOCALS:
            problems.append(f"{node.name} is not an incentive variable; use {', '.join(sorted(LOCALS))}.")
    return problems


__all__ = [
    "IncentiveError", "LOCALS", "MEASURE_MAX", "MEASURE_SOURCE", "PAYOUT_MONTHS", "POLICY_TYPE", "SWITCH",
    "SUPPORTED_MEASURE_SOURCES", "Target", "check_measure", "formula_problems", "local_values",
    "measure_in_window", "payout_window", "resolve_target",
]
