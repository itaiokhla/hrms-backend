"""Proration of full-period rates by paid units: bases, kinds and segments (3.4 §5, 3.3 §9).

A component with full-period rate R earns R × U ÷ N:

    basis          N (basis days)                              U (paid units)
    CALENDAR_DAYS  calendar days in the period                 employment_days − lop_days
    WORKING_DAYS   scheduled working days, whole period        scheduled_working_days − lop_days_on_working_days
    FIXED_30       30                                          employed all period: N − min(lop_days, N)
    FIXED_26       26                                          joined or left mid-period: min(N, counted − lop_days)
    CUSTOM_DAYS    payroll.lop.custom_days                     (same as FIXED_30)
    HOURS          standard hours in the period                payable_hours

Fixed bases and HOURS earn min(R, R × U ÷ N). Counted days are calendar days
for FIXED_30 and CUSTOM_DAYS and days excluding weekly offs for FIXED_26.
BY_EMPLOYMENT_DAYS uses the same formulas without LOP (standard hours while
employed, for HOURS); BY_PAID_HOURS always uses HOURS; NONE is never prorated.

A revision, transfer or FTE change inside the period splits it into segments
(EC-CMP-16). Every segment is divided by the full-period N. Under a fixed
basis a fully employed period gives each segment its calendar share of N, and
capped totals are scaled back to N, and segment amounts telescope from one
exact running total, so the segments add up exactly to one full period when
nothing is unpaid. Amounts are returned unrounded (28 significant digits) with a
structured explanation; ``round_segments`` rounds segment lines cumulatively so
the rounded lines still add up to the rounded total.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import StrEnum
from fractions import Fraction
from typing import Any

CALC_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])
ZERO = Decimal(0)
_ROUNDINGS = frozenset({ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_UP, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR})


class ProrationError(ValueError):
    """Invalid proration input; ``code`` is stable (INVALID_BASIS, ZERO_BASIS, CUSTOM_DAYS_REQUIRED, ...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Basis(StrEnum):
    CALENDAR_DAYS = "CALENDAR_DAYS"
    WORKING_DAYS = "WORKING_DAYS"
    FIXED_30 = "FIXED_30"
    FIXED_26 = "FIXED_26"
    CUSTOM_DAYS = "CUSTOM_DAYS"
    HOURS = "HOURS"


class ProrationKind(StrEnum):
    BY_PAID_DAYS = "BY_PAID_DAYS"
    BY_PAID_HOURS = "BY_PAID_HOURS"
    BY_EMPLOYMENT_DAYS = "BY_EMPLOYMENT_DAYS"
    NONE = "NONE"


FIXED_BASES = frozenset({Basis.FIXED_30, Basis.FIXED_26, Basis.CUSTOM_DAYS})
CAPPED_BASES = FIXED_BASES | {Basis.HOURS}
_FIXED_DAYS = {Basis.FIXED_30: Decimal(30), Basis.FIXED_26: Decimal(26)}
CUSTOM_DAYS_MIN, CUSTOM_DAYS_MAX = 1, 31


def decimal_value(value: object, what: str, error: type[ValueError] = ProrationError) -> Decimal:
    """Exact Decimal from a Decimal, int or decimal string; floats, booleans, NaN and infinity are refused.

    ``error`` is called as ``error("INVALID_NUMBER", message)`` so callers keep their own exception type.
    """
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise error("INVALID_NUMBER", f"{what} must be a Decimal, int or decimal string, got {type(value).__name__}")
    try:
        result = Decimal(value.strip()) if isinstance(value, str) else Decimal(value)
    except InvalidOperation:
        raise error("INVALID_NUMBER", f"{what} is not a decimal number: {value!r}") from None
    if not result.is_finite():
        raise error("INVALID_NUMBER", f"{what} must be finite, got {value!r}")
    return result


def plain(value: Decimal) -> str:
    """Canonical text for a quantity: no exponent and no trailing zeros ("28", "0.5", "46666.666...")."""
    if value == 0:
        return "0"
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value.normalize(), "f")


def canonical(value: Decimal) -> Decimal:
    """``value`` with the exponent of ``plain(value)``, so equal quantities also print identically."""
    return Decimal(plain(value))


def _basis(value: object) -> Basis:
    try:
        return Basis(value)
    except ValueError:
        raise ProrationError("INVALID_BASIS", f"unknown proration basis {value!r}") from None


def _kind(value: object) -> ProrationKind:
    try:
        return ProrationKind(value)
    except ValueError:
        raise ProrationError("INVALID_KIND", f"unknown proration kind {value!r}") from None


def _count(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProrationError("INVALID_FACTS", f"{what} must be a non-negative integer, got {value!r}")
    return value


def _quantity(value: object, what: str) -> Decimal:
    result = decimal_value(value, what)
    if result < 0:
        raise ProrationError("INVALID_FACTS", f"{what} must not be negative, got {value!r}")
    return result


@dataclass(frozen=True)
class PeriodFacts:
    """Whole-period facts for one employee, counted as if employed on every date of the period.

    ``standard_hours`` is the employee's standard hours on every non-weekly-off
    date (holidays included, because payable hours include paid holiday hours).
    """

    days: int
    working_days: int
    non_weekly_off_days: int
    standard_hours: Decimal
    employed_all_period: bool = True

    def __post_init__(self) -> None:
        days = _count(self.days, "days")
        working = _count(self.working_days, "working_days")
        non_weekly_off = _count(self.non_weekly_off_days, "non_weekly_off_days")
        if days < 1:
            raise ProrationError("INVALID_FACTS", "a period has at least one day")
        if not working <= non_weekly_off <= days:
            raise ProrationError("INVALID_FACTS", "working_days <= non_weekly_off_days <= days must hold")
        if not isinstance(self.employed_all_period, bool):
            raise ProrationError("INVALID_FACTS", "employed_all_period must be a bool")
        object.__setattr__(self, "standard_hours", _quantity(self.standard_hours, "standard_hours"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "working_days": self.working_days,
            "non_weekly_off_days": self.non_weekly_off_days,
            "standard_hours": plain(self.standard_hours),
            "employed_all_period": self.employed_all_period,
        }


@dataclass(frozen=True)
class UnitFacts:
    """Employment, LOP and hours of one segment (or of the whole employment in the period).

    ``lop_days`` and ``lop_days_on_working_days`` already include penalty LOP;
    ``standard_hours`` covers the employed non-weekly-off dates.
    """

    employment_days: Decimal
    scheduled_working_days: Decimal
    non_weekly_off_days: Decimal
    lop_days: Decimal = ZERO
    lop_days_on_working_days: Decimal = ZERO
    payable_hours: Decimal = ZERO
    standard_hours: Decimal = ZERO

    def __post_init__(self) -> None:
        for name in (
            "employment_days",
            "scheduled_working_days",
            "non_weekly_off_days",
            "lop_days",
            "lop_days_on_working_days",
            "payable_hours",
            "standard_hours",
        ):
            object.__setattr__(self, name, _quantity(getattr(self, name), name))
        if not self.scheduled_working_days <= self.non_weekly_off_days <= self.employment_days:
            raise ProrationError(
                "INVALID_FACTS", "scheduled_working_days <= non_weekly_off_days <= employment_days must hold"
            )
        if self.lop_days > self.employment_days or self.lop_days_on_working_days > self.lop_days:
            raise ProrationError("INVALID_FACTS", "lop_days_on_working_days <= lop_days <= employment_days must hold")

    def to_dict(self) -> dict[str, str]:
        return {
            "employment_days": plain(self.employment_days),
            "scheduled_working_days": plain(self.scheduled_working_days),
            "non_weekly_off_days": plain(self.non_weekly_off_days),
            "lop_days": plain(self.lop_days),
            "lop_days_on_working_days": plain(self.lop_days_on_working_days),
            "payable_hours": plain(self.payable_hours),
            "standard_hours": plain(self.standard_hours),
        }


@dataclass(frozen=True)
class ProrationExplanation:
    """How an amount was prorated; ``to_dict`` matches the line trace (3.5 §8.3)."""

    method: ProrationKind
    basis: Basis | None
    rate: Decimal
    basis_days: Decimal | None
    paid_units: Decimal | None
    capped: bool
    formula: str
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "basis": self.basis.value if self.basis is not None else None,
            "rate": format(self.rate, "f"),
            "basis_days": plain(self.basis_days) if self.basis_days is not None else None,
            "paid_units": plain(self.paid_units) if self.paid_units is not None else None,
            "capped": self.capped,
            "formula": self.formula,
            "note": self.note,
        }


def effective_basis(basis: Basis | str, kind: ProrationKind | str) -> Basis:
    """The basis a component actually uses: BY_PAID_HOURS always prorates by HOURS."""
    return Basis.HOURS if _kind(kind) is ProrationKind.BY_PAID_HOURS else _basis(basis)


def _custom_days(value: object) -> int:
    if value is None:
        raise ProrationError("CUSTOM_DAYS_REQUIRED", "payroll.lop.custom_days is required for the CUSTOM_DAYS basis")
    if isinstance(value, bool) or not isinstance(value, int) or not CUSTOM_DAYS_MIN <= value <= CUSTOM_DAYS_MAX:
        raise ProrationError(
            "INVALID_CUSTOM_DAYS", f"custom days must be an integer {CUSTOM_DAYS_MIN}-{CUSTOM_DAYS_MAX}, got {value!r}"
        )
    return value


def basis_days(basis: Basis | str, period: PeriodFacts, *, custom_days: int | None = None) -> Decimal:
    """N for ``basis`` (3.4 §5.1); a zero basis (no working days, no standard hours) is ZERO_BASIS."""
    chosen = _basis(basis)
    if not isinstance(period, PeriodFacts):
        raise ProrationError("INVALID_FACTS", "period must be PeriodFacts")
    if chosen is Basis.CALENDAR_DAYS:
        n = Decimal(period.days)
    elif chosen is Basis.WORKING_DAYS:
        n = Decimal(period.working_days)
    elif chosen is Basis.CUSTOM_DAYS:
        n = Decimal(_custom_days(custom_days))
    elif chosen is Basis.HOURS:
        n = period.standard_hours
    else:
        n = _FIXED_DAYS[chosen]
    if n <= 0:
        raise ProrationError("ZERO_BASIS", f"the {chosen.value} basis is zero for this period")
    return n


def _apportion(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """Split ``total`` in proportion to ``weights``; the last positive weight takes the remainder, so the sum is exact."""
    weight_sum = sum((Fraction(weight) for weight in weights), Fraction(0))
    if weight_sum <= 0:
        raise ProrationError("ZERO_BASIS", "nothing to apportion")
    whole = Fraction(total)
    running: list[Fraction] = []
    cumulative = Fraction(0)
    for weight in weights:
        cumulative += Fraction(weight)
        running.append(whole * cumulative / weight_sum)
    return _differences(running, total)


def _differences(running: Sequence[Fraction], total: Decimal) -> list[Decimal]:
    """Differences of running totals quantized to one exponent (28 significant digits of ``total``), exact by design."""
    with localcontext(CALC_CONTEXT):
        if total == 0:
            return [ZERO for _ in running]
        exponent = total.adjusted() - (CALC_CONTEXT.prec - 1)
        scale = Fraction(10) ** -exponent
        shares: list[Decimal] = []
        previous = ZERO
        for value in running:
            current = Decimal(round(value * scale)).scaleb(exponent)
            shares.append(current - previous)
            previous = current
    return shares


@dataclass(frozen=True)
class _Units:
    basis: Basis
    n: Decimal
    units: tuple[Decimal, ...]
    notes: tuple[str, ...]


def _fixed_units(
    basis: Basis, n: Decimal, period: PeriodFacts, facts: Sequence[UnitFacts], no_lop: bool
) -> tuple[list[Decimal], list[str]]:
    by_weekdays = basis is Basis.FIXED_26
    label = "days excluding weekly offs" if by_weekdays else "calendar days"
    counted = [f.non_weekly_off_days if by_weekdays else f.employment_days for f in facts]
    lops = [ZERO if no_lop else f.lop_days for f in facts]
    if not period.employed_all_period:
        notes = [f"min({plain(n)}, {plain(c)} {label} − {plain(lop)} LOP days)" for c, lop in zip(counted, lops)]
        return [min(n, c - lop) for c, lop in zip(counted, lops)], notes
    whole = Decimal(period.non_weekly_off_days if by_weekdays else period.days)
    if sum(counted, ZERO) != whole:
        raise ProrationError(
            "SEGMENTS_DO_NOT_COVER_PERIOD",
            f"employed all period, but the segments count {plain(sum(counted, ZERO))} of {plain(whole)} {label}",
        )
    if len(facts) == 1:
        return [n - lops[0]], [f"{plain(n)} − min({plain(lops[0])}, {plain(n)})"]
    shares = _apportion(n, counted)
    notes = [
        f"{plain(share)} of {plain(n)} for {plain(c)} of {plain(whole)} {label} − {plain(lop)} LOP days"
        for share, c, lop in zip(shares, counted, lops)
    ]
    return [share - lop for share, lop in zip(shares, lops)], notes


def _segments(segments: Sequence[UnitFacts]) -> tuple[UnitFacts, ...]:
    facts = tuple(segments)
    if not facts:
        raise ProrationError("NO_SEGMENTS", "at least one segment is required")
    if not all(isinstance(f, UnitFacts) for f in facts):
        raise ProrationError("INVALID_FACTS", "segments must be UnitFacts")
    return facts


def _compute_units(
    basis: Basis | str,
    kind: ProrationKind | str,
    period: PeriodFacts,
    segments: Sequence[UnitFacts],
    custom_days: int | None,
) -> _Units:
    chosen_kind = _kind(kind)
    if chosen_kind is ProrationKind.NONE:
        raise ProrationError("NOT_PRORATED", "components with proration NONE are never prorated")
    effective = effective_basis(basis, chosen_kind)
    facts = _segments(segments)
    no_lop = chosen_kind is ProrationKind.BY_EMPLOYMENT_DAYS
    with localcontext(CALC_CONTEXT):
        n = basis_days(effective, period, custom_days=custom_days)
        for name, limit in (
            ("employment_days", period.days),
            ("non_weekly_off_days", period.non_weekly_off_days),
            ("scheduled_working_days", period.working_days),
        ):
            total = sum((getattr(f, name) for f in facts), ZERO)
            if total > limit:
                raise ProrationError(
                    "SEGMENTS_EXCEED_PERIOD", f"the segments have {plain(total)} {name} but the period has {limit}"
                )
        if effective is Basis.CALENDAR_DAYS:
            lops = [ZERO if no_lop else f.lop_days for f in facts]
            raw = [f.employment_days - lop for f, lop in zip(facts, lops)]
            notes = [f"{plain(f.employment_days)} employment days − {plain(lop)} LOP days" for f, lop in zip(facts, lops)]
        elif effective is Basis.WORKING_DAYS:
            lops = [ZERO if no_lop else f.lop_days_on_working_days for f in facts]
            raw = [f.scheduled_working_days - lop for f, lop in zip(facts, lops)]
            notes = [
                f"{plain(f.scheduled_working_days)} scheduled working days − {plain(lop)} LOP days on working days"
                for f, lop in zip(facts, lops)
            ]
        elif effective is Basis.HOURS:
            raw = [f.standard_hours if no_lop else f.payable_hours for f in facts]
            label = "standard hours while employed" if no_lop else "payable hours"
            notes = [f"{plain(hours)} {label}" for hours in raw]
        else:
            raw, notes = _fixed_units(effective, n, period, facts, no_lop)
        units = [max(ZERO, value) for value in raw]
        notes = [note if value >= 0 else f"{note}; no paid units left" for note, value in zip(notes, raw)]
        if effective in CAPPED_BASES and len(units) > 1 and sum(units, ZERO) > n:
            units = _apportion(n, units)
            notes = [f"{note}; scaled so the segments total {plain(n)}" for note in notes]
    return _Units(effective, n, tuple(units), tuple(notes))


def segment_units(
    basis: Basis | str,
    kind: ProrationKind | str,
    period: PeriodFacts,
    segments: Sequence[UnitFacts],
    *,
    custom_days: int | None = None,
) -> tuple[Decimal, ...]:
    """Paid units U of each segment; all are divided by the same full-period N."""
    return _compute_units(basis, kind, period, segments, custom_days).units


def paid_units(
    basis: Basis | str,
    kind: ProrationKind | str,
    period: PeriodFacts,
    facts: UnitFacts,
    *,
    custom_days: int | None = None,
) -> Decimal:
    """Paid units U for an employee whose period is not split into segments."""
    return segment_units(basis, kind, period, (facts,), custom_days=custom_days)[0]


def prorate(
    rate: Decimal,
    basis: Basis | str,
    n: Decimal,
    u: Decimal,
    *,
    cap_at_rate: bool,
    kind: ProrationKind | str = ProrationKind.BY_PAID_DAYS,
    note: str | None = None,
) -> tuple[Decimal, ProrationExplanation]:
    """R × U ÷ N, or min(R, R × U ÷ N) when ``cap_at_rate``; unrounded, with its explanation."""
    chosen_basis, chosen_kind = _basis(basis), _kind(kind)
    with localcontext(CALC_CONTEXT):
        r = decimal_value(rate, "rate")
        basis_n = decimal_value(n, "basis days")
        units = decimal_value(u, "paid units")
        if basis_n <= 0:
            raise ProrationError("ZERO_BASIS", "basis days must be positive")
        if units < 0:
            raise ProrationError("NEGATIVE_UNITS", "paid units must not be negative")
        capped = cap_at_rate and units > basis_n
        amount = r if capped or units == basis_n else r * units / basis_n
    shown = f"{format(r, 'f')} × {plain(units)} ÷ {plain(basis_n)}"
    formula = f"min({format(r, 'f')}, {shown})" if cap_at_rate else shown
    return amount, ProrationExplanation(chosen_kind, chosen_basis, r, basis_n, units, capped, formula, note)


def prorate_segments(
    rates: Sequence[Decimal],
    basis: Basis | str,
    kind: ProrationKind | str,
    period: PeriodFacts,
    segments: Sequence[UnitFacts],
    *,
    custom_days: int | None = None,
    zero_pay: bool = False,
) -> tuple[tuple[Decimal, ProrationExplanation], ...]:
    """Earned amount of every segment, each segment with its own rate (EC-CMP-16).

    ``zero_pay`` is the zero-pay guard with ``payroll.lop.zero_paid_days = PAY_ZERO``
    (3.4 §5.3): the engine passes it for LOP_APPLICABLE components, which are zero.
    """
    chosen_basis, chosen_kind = _basis(basis), _kind(kind)
    values = [decimal_value(rate, "rate") for rate in rates]
    facts = _segments(segments)
    if len(values) != len(facts):
        raise ProrationError("SEGMENT_MISMATCH", f"{len(values)} rates for {len(facts)} segments")
    if zero_pay:
        note = "No paid working days: LOP-applicable components are zero (payroll.lop.zero_paid_days = PAY_ZERO)"
        return tuple(
            (ZERO, ProrationExplanation(chosen_kind, chosen_basis, r, None, None, False, "0 (zero-pay guard)", note))
            for r in values
        )
    if chosen_kind is ProrationKind.NONE:
        return tuple(
            (r, ProrationExplanation(chosen_kind, None, r, None, None, False, f"{format(r, 'f')} (not prorated)"))
            for r in values
        )
    computed = _compute_units(chosen_basis, chosen_kind, period, facts, custom_days)
    cap = computed.basis in CAPPED_BASES
    results = [
        prorate(r, computed.basis, computed.n, units, cap_at_rate=cap, kind=chosen_kind, note=note)
        for r, units, note in zip(values, computed.units, computed.notes)
    ]
    if len(results) == 1 or any(explanation.capped for _, explanation in results):
        return tuple(results)
    amounts = _telescoped(values, computed.units, computed.n)
    return tuple((amount, explanation) for amount, (_, explanation) in zip(amounts, results))


def _telescoped(rates: Sequence[Decimal], units: Sequence[Decimal], n: Decimal) -> list[Decimal]:
    """Segment k earns C(k) − C(k−1), where C(k) = Σ R·U ÷ N over the first k segments.

    The running totals are exact fractions quantized to one exponent (28
    significant digits of the whole), so the differences and their sum are exact
    and the segments add up to the period total rounded once.
    """
    running: list[Fraction] = []
    cumulative = Fraction(0)
    for rate, unit in zip(rates, units):
        cumulative += Fraction(rate) * Fraction(unit) / Fraction(n)
        running.append(cumulative)
    with localcontext(CALC_CONTEXT):
        total = Decimal(cumulative.numerator) / Decimal(cumulative.denominator)
    return _differences(running, total)


def round_segments(
    amounts: Sequence[Decimal], quantum: Decimal, rounding: str = ROUND_HALF_UP
) -> tuple[Decimal, ...]:
    """Round segment amounts so the rounded lines add up to the rounded total (cumulative rounding).

    ``quantum`` is the currency's minor unit (Decimal("0.01"), Decimal("1") for JPY, ...).
    """
    step = decimal_value(quantum, "quantum")
    if step <= 0:
        raise ProrationError("INVALID_QUANTUM", "quantum must be positive")
    if rounding not in _ROUNDINGS:
        raise ProrationError("INVALID_ROUNDING", f"unknown rounding mode {rounding!r}")
    rounded: list[Decimal] = []
    with localcontext(CALC_CONTEXT):
        total = ZERO
        previous = ZERO
        for value in amounts:
            total += decimal_value(value, "amount")
            current = (total / step).quantize(Decimal(1), rounding=rounding) * step
            rounded.append(current - previous)
            previous = current
    return tuple(rounded)


def prorate_component(
    rate: Decimal,
    basis: Basis | str,
    kind: ProrationKind | str,
    period: PeriodFacts,
    facts: UnitFacts,
    *,
    custom_days: int | None = None,
    zero_pay: bool = False,
) -> tuple[Decimal, ProrationExplanation]:
    """Earned amount of one component over an unsplit period."""
    return prorate_segments(
        (rate,), basis, kind, period, (facts,), custom_days=custom_days, zero_pay=zero_pay
    )[0]
