"""Net-to-gross solver (3.3 §6.3, EC-CMP-15).

For NET_BASED structures and one-time payments "net of tax": find the gross whose net equals a
target, by bisection over whole minor units of the currency. ``net_of(gross)`` must be a
deterministic function (usually a full calculation of the employee with that gross) that does
not decrease as gross grows. At most 50 evaluations (bracket search included); converged when
``|net − target| ≤ one minor unit``; the smallest gross whose net reaches the target is preferred,
then the largest one just below it within the tolerance. Otherwise the result
carries a blocking ``NOT_CONVERGED`` issue: no payment for that employee until someone resolves
it. Every evaluation is kept in the trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.payroll.engine.lines import CalcIssue
from app.payroll.money import CALC_CONTEXT, MoneyError, amount_str, decimal_str, quantize_amount, quantum, to_decimal

MAX_ITERATIONS = 50
NOT_CONVERGED = "NOT_CONVERGED"


@dataclass(frozen=True)
class GrossUpResult:
    converged: bool
    gross: Optional[Decimal]                 # None when not converged
    net: Optional[Decimal]
    target_net: Decimal
    iterations: Tuple[Dict[str, str], ...]
    issue: Optional[CalcIssue]
    trace: Dict[str, Any]


def solve_gross(target_net: Decimal, net_of: Callable[[Decimal], Decimal], currency: str, *,
                lower: Optional[Decimal] = None, upper: Optional[Decimal] = None,
                max_iterations: int = MAX_ITERATIONS, component: Optional[str] = None) -> GrossUpResult:
    """The gross that yields ``target_net`` (see the module docstring). ``lower``/``upper`` bound the search
    (defaults: 0 and twice the target, doubled while the net stays below the target)."""
    target = to_decimal(target_net)
    unit = quantum(currency)
    if quantize_amount(target, currency) != target:
        raise MoneyError("AMOUNT_NOT_ROUNDED", f"The target net {decimal_str(target)} has more decimals than {currency}.")
    if not 1 <= max_iterations <= MAX_ITERATIONS:
        raise ValueError(f"max_iterations must be between 1 and {MAX_ITERATIONS}")
    steps: List[Dict[str, str]] = []
    cache: Dict[int, Decimal] = {}

    def evaluate(units: int) -> Optional[Decimal]:
        if units in cache:
            return cache[units]
        if len(steps) >= max_iterations:
            return None
        gross = Decimal(units) * unit
        net = to_decimal(net_of(gross))
        cache[units] = net
        steps.append({"n": str(len(steps) + 1), "gross": amount_str(gross, currency), "net": decimal_str(net)})
        return net

    def done(units: Optional[int], reason: Optional[str] = None) -> GrossUpResult:
        converged = units is not None
        gross = Decimal(units) * unit if converged else None
        net = cache.get(units) if converged else None
        issue = None if converged else CalcIssue(
            NOT_CONVERGED, "BLOCKING",
            f"Gross-up for a net of {decimal_str(target)} {currency} did not converge after {len(steps)} evaluations "
            f"({reason}); no payment until the amount is resolved.",
            {"target_net": amount_str(target, currency), "evaluations": len(steps), "reason": reason,
             "last": steps[-1] if steps else None}, component=component)
        trace = {"method": "NET_TO_GROSS", "target_net": amount_str(target, currency),
                 "tolerance": amount_str(unit, currency), "max_iterations": max_iterations, "converged": converged,
                 "iterations": list(steps), "gross": None if gross is None else amount_str(gross, currency)}
        return GrossUpResult(converged, gross, net, target, tuple(steps), issue, trace)

    with localcontext(CALC_CONTEXT):
        lo = int((to_decimal(lower) if lower is not None else Decimal(0)) / unit)
        hi = int((to_decimal(upper) if upper is not None else max(target * 2, unit)) / unit)
        if lo < 0 or hi < lo:
            raise ValueError("the search bounds must satisfy 0 <= lower <= upper")

        net_lo = evaluate(lo)
        if net_lo is None:
            return done(None, "iteration limit")
        if net_lo >= target:
            return done(lo) if net_lo - target <= unit else done(None, "the net at the lower bound is above the target")
        net_hi = evaluate(hi)
        while net_hi is not None and net_hi < target:
            if upper is not None:
                return done(None, "the target net is above the net at the upper bound")
            lo, hi = hi, hi * 2
            net_hi = evaluate(hi)
        if net_hi is None:
            return done(None, "iteration limit")
        while hi - lo > 1:                      # invariant: net(lo) < target <= net(hi)
            mid = (lo + hi) // 2
            net = evaluate(mid)
            if net is None:
                break
            if net >= target:
                hi = mid
            else:
                lo = mid
        if cache[hi] - target <= unit:
            return done(hi)
        if hi - lo <= 1 and target - cache[lo] <= unit:
            return done(lo)
        return done(None, "iteration limit" if hi - lo > 1 else
                    "no gross gives a net within one minor unit (net pay jumps around the target)")
