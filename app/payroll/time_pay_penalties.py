"""Late marks, early exits and penalties (3.4 §11, 3.1 §5.7).

    late mark     check-in after shift start + attendance.grace_minutes (flexible shifts are never late)
    early exit    punch-out before shift end − early_exit_grace_minutes
    chargeable    max(0, late_marks + (early_exits if count_early_exit) − regularised − free_late_marks)
    DAY_FRACTION  penalty_lop = floor(chargeable ÷ late_marks_per_unit) × unit_days, capped at cap_days_per_period
    FIXED_AMOUNT  amount_per_late × chargeable, as a PENALTY_IF_LEGAL deduction

Regularised marks are subtracted only when ``exempt_if_regularized`` is true, and
regularised early exits only when early exits count. A mark on a day that is
absent in the ledger is dropped (an absent day has no late mark) and penalty LOP
plus ordinary LOP never exceeds the basis. With the switch OFF, or where the pack
forbids penalty deductions (EC-VAR-10), marks are still counted and reported but
never change pay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Collection, List, Optional, Sequence, Tuple

from app.payroll.time_pay_common import (
    BLOCKING,
    DEDUCTION,
    INFO,
    INVALID_INPUT,
    INVALID_POLICY,
    Issue,
    TimePayError,
    TimePayLine,
    in_calc_context,
    make_line,
    policy_bool,
    policy_choice,
    policy_decimal,
    policy_int,
    policy_money,
    require_decimal,
    require_int,
    resolve_minor_unit,
    show,
)

LATE = "LATE"
EARLY_EXIT = "EARLY_EXIT"
MARK_KINDS = (LATE, EARLY_EXIT)
PENALTY_TYPES = ("DAY_FRACTION", "FIXED_AMOUNT")
LATE_PENALTY_COMPONENT = "LATE_PENALTY"


def is_late(check_in: datetime, shift_start: datetime, grace_minutes: int, *, flexible: bool = False) -> bool:
    """A late mark: check-in strictly after shift start + grace. Flexible shifts are never late."""
    require_int(grace_minutes, "grace_minutes", minimum=0, maximum=240)
    if flexible:
        return False
    return check_in > shift_start + timedelta(minutes=grace_minutes)


def is_early_exit(punch_out: datetime, shift_end: datetime, policy: Any = None, *, flexible: bool = False) -> bool:
    """An early exit: punch-out strictly before shift end − ``early_exit_grace_minutes`` (default 15)."""
    grace = policy_int(policy, "early_exit_grace_minutes", 15, minimum=0, maximum=240) or 0
    if flexible:
        return False
    return punch_out < shift_end - timedelta(minutes=grace)


@dataclass(frozen=True)
class LateMark:
    """One late mark or early exit as stored by attendance."""

    date: date
    kind: str = LATE
    regularised: bool = False
    ref: Optional[str] = None


@dataclass(frozen=True)
class LatePenaltyRules:
    free_late_marks: int
    penalty_type: str
    late_marks_per_unit: int
    unit_days: Decimal
    amount_per_late: Optional[Decimal]
    count_early_exit: bool
    cap_days_per_period: Decimal
    exempt_if_regularized: bool


def parse_late_penalty_policy(policy: Any, currency: Optional[str] = None) -> LatePenaltyRules:
    """Read a LATE_PENALTY_POLICY (3.1 §5.7); ``FIXED_AMOUNT`` needs ``amount_per_late`` in ``currency``."""
    penalty_type = policy_choice(policy, "penalty_type", "DAY_FRACTION", PENALTY_TYPES)
    amount = None
    if penalty_type == "FIXED_AMOUNT":
        if currency is None:
            raise TimePayError(INVALID_INPUT, "a FIXED_AMOUNT penalty needs the pay currency", "currency")
        amount = policy_money(policy, "amount_per_late", currency)
        if amount is None:
            raise TimePayError(INVALID_POLICY, "FIXED_AMOUNT needs amount_per_late", "amount_per_late")
    return LatePenaltyRules(
        free_late_marks=policy_int(policy, "free_late_marks", 2, minimum=0, maximum=1000) or 0,
        penalty_type=penalty_type,
        late_marks_per_unit=policy_int(policy, "late_marks_per_unit", 3, minimum=1, maximum=1000) or 1,
        unit_days=policy_decimal(policy, "unit_days", "0.5", minimum=Decimal(0), maximum=Decimal(31)) or Decimal(0),
        amount_per_late=amount,
        count_early_exit=policy_bool(policy, "count_early_exit", False),
        cap_days_per_period=policy_decimal(policy, "cap_days_per_period", "2.0", minimum=Decimal(0),
                                           maximum=Decimal(31)) or Decimal(0),
        exempt_if_regularized=policy_bool(policy, "exempt_if_regularized", True),
    )


@dataclass(frozen=True)
class LatePenalty:
    """Counts, the chargeable marks and the penalty (LOP days or a deduction line)."""

    applied: bool
    penalty_type: str
    late_marks: int
    early_exits: int
    counted_early_exits: int
    regularised: int
    dropped_on_absent_days: int
    free_marks_used: int
    chargeable: int
    penalty_lop_days: Decimal
    uncapped_lop_days: Decimal
    line: Optional[TimePayLine]
    explanation: str
    issues: Tuple[Issue, ...]

    @property
    def amount(self) -> Decimal:
        return self.line.amount if self.line else Decimal(0)


def _mark_source(mark: LateMark) -> dict:
    source = {"type": "late_mark", "date": mark.date.isoformat(), "kind": mark.kind,
              "regularised": "true" if mark.regularised else "false"}
    if mark.ref:
        source["id"] = mark.ref
    return source


@in_calc_context
def late_penalty(
    marks: Sequence[LateMark],
    policy: Any,
    *,
    enabled: bool = True,
    penalty_allowed: bool = True,
    currency: Optional[str] = None,
    absent_dates: Collection[date] = (),
    basis_days: Optional[Decimal] = None,
    ordinary_lop_days: Decimal = Decimal(0),
    minor_unit: Optional[int] = None,
    rounding: str = "HALF_UP",
    component: str = LATE_PENALTY_COMPONENT,
) -> LatePenalty:
    """The late-mark penalty for one employee and period (3.4 §11, §17.4).

    ``enabled`` is ``payroll.late_penalty.enabled`` for the period; ``penalty_allowed`` is the
    pack's ``penalty_deductions_allowed``. ``basis_days`` and ``ordinary_lop_days`` enforce
    "penalty LOP plus ordinary LOP never exceeds the basis" for DAY_FRACTION.
    """
    if currency is not None:
        minor_unit = resolve_minor_unit(currency, minor_unit)
    rules = parse_late_penalty_policy(policy, currency)
    ordinary = require_decimal(ordinary_lop_days, "ordinary_lop_days", minimum=Decimal(0))
    basis = None if basis_days is None else require_decimal(basis_days, "basis_days", positive=True)
    absent = frozenset(absent_dates)
    late = early = counted_early = regularised = dropped = 0
    for mark in marks:
        if mark.kind not in MARK_KINDS:
            raise TimePayError(INVALID_INPUT, f"{mark.date}: mark kind must be LATE or EARLY_EXIT", "kind")
        if mark.date in absent:
            dropped += 1
            continue
        counts = mark.kind == LATE or rules.count_early_exit
        if mark.kind == LATE:
            late += 1
        else:
            early += 1
            counted_early += 1 if rules.count_early_exit else 0
        if counts and mark.regularised and rules.exempt_if_regularized:
            regularised += 1
    countable = late + counted_early - regularised
    free_used = min(countable, rules.free_late_marks)
    chargeable = max(0, countable - rules.free_late_marks)
    counted = (f"{late} late mark(s)" + (f" + {counted_early} early exit(s)" if rules.count_early_exit else "")
               + (f" − {regularised} regularised" if regularised else "") + f" − {rules.free_late_marks} free")
    explanation = f"Chargeable = {counted} = {chargeable}"
    issues: List[Issue] = []
    if dropped:
        explanation += f"; {dropped} mark(s) on absent days ignored"

    if not enabled or not penalty_allowed:
        reason = "the late penalty switch is OFF" if not enabled else "the country pack forbids penalty deductions"
        if enabled and not penalty_allowed and chargeable:
            issues.append(Issue("LATE_PENALTY_NOT_ALLOWED", BLOCKING,
                                "Late penalties are ON but the country pack forbids penalty deductions; "
                                "the switch must be locked OFF"))
        return LatePenalty(False, rules.penalty_type, late, early, counted_early, regularised, dropped, free_used,
                           chargeable, Decimal(0), Decimal(0), None, f"{explanation}; no penalty because {reason}",
                           tuple(issues))

    if rules.penalty_type == "FIXED_AMOUNT":
        assert rules.amount_per_late is not None and currency is not None and minor_unit is not None
        line = None
        if chargeable:
            line = make_line(component, DEDUCTION, "PENALTY_IF_LEGAL", rules.amount_per_late * chargeable, currency,
                             minor_unit=minor_unit, rounding=rounding, method="RATE_X_UNITS",
                             explanation=f"{show(rules.amount_per_late)} × {chargeable} chargeable mark(s)",
                             units=Decimal(chargeable), rate=rules.amount_per_late,
                             sources=[_mark_source(m) for m in marks if m.date not in absent])
        return LatePenalty(True, rules.penalty_type, late, early, counted_early, regularised, dropped, free_used,
                           chargeable, Decimal(0), Decimal(0), line,
                           f"{explanation} → {show(rules.amount_per_late)} × {chargeable}", tuple(issues))

    units = chargeable // rules.late_marks_per_unit
    uncapped = units * rules.unit_days
    penalty = min(uncapped, rules.cap_days_per_period)
    explanation += (f" → floor({chargeable} ÷ {rules.late_marks_per_unit}) × {rules.unit_days} = "
                    f"{show(uncapped)} day(s)")
    if penalty < uncapped:
        explanation += f", capped at {show(rules.cap_days_per_period)}"
    if basis is not None and penalty + ordinary > basis:
        limited = max(Decimal(0), basis - ordinary)
        explanation += f", limited to {show(limited)} so penalty and ordinary LOP stay within {show(basis)} days"
        issues.append(Issue("LATE_PENALTY_LIMITED_BY_BASIS", INFO,
                            f"Penalty LOP reduced from {show(penalty)} to {show(limited)} day(s): ordinary LOP is "
                            f"already {show(ordinary)} of {show(basis)} days"))
        penalty = limited
    return LatePenalty(True, rules.penalty_type, late, early, counted_early, regularised, dropped, free_used,
                       chargeable, penalty, uncapped, None, f"{explanation}: {show(penalty)} day(s) of penalty LOP",
                       tuple(issues))
