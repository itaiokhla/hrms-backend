"""Shared primitives for the time-based pay rules (``app.payroll.time_pay``).

Pure and deterministic: ``Decimal`` arithmetic in a fixed 28-digit context, no
floats, no clock, no environment. Policies arrive as plain mappings (or objects
with attributes) using the field names of 3.1 §5; a missing or ``None`` field
takes its documented default, while a present but invalid value raises
``TimePayError(INVALID_POLICY)``, so pay is never calculated on a silently
substituted default (EC-CFG-09). Legal multipliers (overtime buckets, holiday and
weekly-off work) have no default: contract K:157 forbids assuming a universal
rate, so a missing multiplier raises when it is needed (decision I-13).

Problems the spec turns into review items are returned as ``Issue`` records;
impossible inputs and broken configuration raise ``TimePayError`` with a stable
``code``. Amounts are rounded once, at the line, to the currency's minor unit
(3.2 §3.3) and every line keeps its unrounded value for the trace. Durations
are whole minutes; hours are derived (minutes ÷ 60) and money is computed from
minutes so that the division happens last.
"""

from __future__ import annotations

import functools
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
    InvalidOperation,
    localcontext,
)
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple, TypeVar

from app.payroll.currencies import require_payroll_currency
from app.payroll.money_errors import MoneyError

CALC_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)

WORKDAY = "WORKDAY"
WEEKLY_OFF = "WEEKLY_OFF"
HOLIDAY = "HOLIDAY"
DAY_TYPES = (WORKDAY, WEEKLY_OFF, HOLIDAY)

BLOCKING = "BLOCKING"
WARNING = "WARNING"
INFO = "INFO"

EARNING = "EARNING"
DEDUCTION = "DEDUCTION"
REIMBURSEMENT = "REIMBURSEMENT"

ROUNDING_MODES = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_EVEN": ROUND_HALF_EVEN,
    "UP": ROUND_UP,
    "DOWN": ROUND_DOWN,
    "CEILING": ROUND_CEILING,
    "FLOOR": ROUND_FLOOR,
}

# Stable error codes raised as TimePayError.code
INVALID_POLICY = "INVALID_POLICY"
INVALID_INPUT = "INVALID_INPUT"
INVALID_NUMBER = "INVALID_NUMBER"
INVALID_MINUTES = "INVALID_MINUTES"
OVERLAPPING_ENTRIES = "OVERLAPPING_ENTRIES"
EXEMPT_NOT_ALLOWED = "EXEMPT_NOT_ALLOWED"
INVALID_RATE = "INVALID_RATE"
INVALID_SPLIT = "INVALID_SPLIT"
SPLIT_NOT_ALLOWED = "SPLIT_NOT_ALLOWED"
ACCEPTED_EXCEEDS_SUBMITTED = "ACCEPTED_EXCEEDS_SUBMITTED"
DUPLICATE_EXTERNAL_REF = "DUPLICATE_EXTERNAL_REF"
CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
INVALID_CURRENCY = "INVALID_CURRENCY"
INVALID_SCHEDULE = "INVALID_SCHEDULE"
DOUBLE_COUNTED_HOURS = "DOUBLE_COUNTED_HOURS"

MAX_MINUTES_PER_DAY = 24 * 60
MAX_SECONDS_PER_DAY = 25 * 3600  # a daylight-saving day can last 25 hours (EC-ATT-12)

DayRanges = Sequence[Tuple[date, Optional[date]]]
"""Dates on which a DAY-grain switch is ON: inclusive ``(start, end)`` pairs, ``end=None`` = open."""

T = TypeVar("T")


class TimePayError(ValueError):
    """Invalid input or configuration; ``code`` is stable and ``field`` names the offending value."""

    def __init__(self, code: str, message: str, field: Optional[str] = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.field = field


@dataclass(frozen=True)
class Issue:
    """A review item (run exception or flag) produced from data, never raised."""

    code: str
    severity: str
    message: str
    date: Optional[date] = None
    ref: Optional[str] = None
    details: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "date": self.date.isoformat() if self.date else None,
            "ref": self.ref,
            "details": dict(self.details),
        }


def in_calc_context(func: Callable[..., T]) -> Callable[..., T]:
    """Run ``func`` in the fixed payroll decimal context, whatever the caller's context is."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        with localcontext(CALC_CONTEXT):
            return func(*args, **kwargs)

    return wrapper


def switch_on(day: date, ranges: Optional[DayRanges]) -> bool:
    """DAY-grain switch value on ``day`` (3.1 §2.2). ``None`` means ON for every date."""
    if ranges is None:
        return True
    for start, end in ranges:
        if start <= day and (end is None or day <= end):
            return True
    return False


def check_ranges(ranges: Optional[DayRanges], name: str) -> None:
    if ranges is None:
        return
    for start, end in ranges:
        if not isinstance(start, date) or (end is not None and not isinstance(end, date)):
            raise TimePayError(INVALID_INPUT, f"{name} must contain (date, date | None) pairs", name)
        if end is not None and end < start:
            raise TimePayError(INVALID_INPUT, f"{name} has a range ending before it starts", name)


def to_decimal(value: Any, name: str, *, code: str = INVALID_NUMBER) -> Decimal:
    """``str | int | Decimal`` to a finite Decimal; floats and booleans are refused (money is never a float)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TimePayError(code, f"{name} must be a decimal string, int or Decimal, not {type(value).__name__}", name)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise TimePayError(code, f"{name} is not a decimal number: {value!r}", name) from None
    else:
        raise TimePayError(code, f"{name} must be a decimal string, int or Decimal", name)
    if not result.is_finite():
        raise TimePayError(code, f"{name} must be finite", name)
    return result


def require_decimal(
    value: Any,
    name: str,
    *,
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
    positive: bool = False,
    code: str = INVALID_INPUT,
) -> Decimal:
    result = to_decimal(value, name, code=code)
    if positive and result <= 0:
        raise TimePayError(code, f"{name} must be greater than zero", name)
    if minimum is not None and result < minimum:
        raise TimePayError(code, f"{name} must be at least {minimum}", name)
    if maximum is not None and result > maximum:
        raise TimePayError(code, f"{name} must be at most {maximum}", name)
    return result


def require_int(value: Any, name: str, *, minimum: Optional[int] = None, maximum: Optional[int] = None,
                code: str = INVALID_INPUT) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimePayError(code, f"{name} must be an integer", name)
    if minimum is not None and value < minimum:
        raise TimePayError(code, f"{name} must be at least {minimum}", name)
    if maximum is not None and value > maximum:
        raise TimePayError(code, f"{name} must be at most {maximum}", name)
    return value


def field_value(policy: Any, name: str, default: Any = None) -> Any:
    """A policy field by name from a mapping or an object; missing or ``None`` gives ``default``."""
    if policy is None:
        return default
    if isinstance(policy, Mapping):
        value = policy.get(name)
    else:
        value = getattr(policy, name, None)
    if hasattr(value, "value") and not isinstance(value, (str, int, Decimal)):
        value = value.value  # enum members from typed policy models
    return default if value is None else value


def policy_choice(policy: Any, name: str, default: str, allowed: Iterable[str]) -> str:
    value = field_value(policy, name, default)
    choices = tuple(allowed)
    if not isinstance(value, str) or value not in choices:
        raise TimePayError(INVALID_POLICY, f"{name} must be one of {', '.join(choices)}; got {value!r}", name)
    return value


def policy_decimal(policy: Any, name: str, default: Any, *, minimum: Optional[Decimal] = None,
                   maximum: Optional[Decimal] = None, positive: bool = False) -> Optional[Decimal]:
    value = field_value(policy, name, default)
    if value is None:
        return None
    return require_decimal(value, name, minimum=minimum, maximum=maximum, positive=positive, code=INVALID_POLICY)


def policy_int(policy: Any, name: str, default: Optional[int], *, minimum: Optional[int] = None,
               maximum: Optional[int] = None) -> Optional[int]:
    value = field_value(policy, name, default)
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    elif isinstance(value, Decimal) and value == value.to_integral_value():
        value = int(value)
    return require_int(value, name, minimum=minimum, maximum=maximum, code=INVALID_POLICY)


def policy_bool(policy: Any, name: str, default: bool) -> bool:
    value = field_value(policy, name, default)
    if not isinstance(value, bool):
        raise TimePayError(INVALID_POLICY, f"{name} must be true or false", name)
    return value


def policy_money(policy: Any, name: str, currency: str) -> Optional[Decimal]:
    """A money field: ``{"amount": "15000.00", "currency": "INR"}`` or a bare amount in the pay currency.

    A zero amount may omit its currency (zero is the same in every currency, as in
    ``policy_types.Money``); any other amount must be in the pay currency.
    """
    value = field_value(policy, name, None)
    if value is None:
        return None
    if isinstance(value, Mapping) or (hasattr(value, "amount") and hasattr(value, "currency")):
        amount = value.get("amount") if isinstance(value, Mapping) else value.amount
        money_currency = value.get("currency") if isinstance(value, Mapping) else value.currency
        if amount is None:
            raise TimePayError(INVALID_POLICY, f"{name} needs an amount", name)
        parsed = require_decimal(amount, name, minimum=Decimal(0), code=INVALID_POLICY)
        if money_currency is None and parsed == 0:
            return parsed
        if money_currency != currency:
            raise TimePayError(CURRENCY_MISMATCH, f"{name} is in {money_currency}, the pay currency is {currency}", name)
        return parsed
    return require_decimal(value, name, minimum=Decimal(0), code=INVALID_POLICY)


def check_currency(code: str, name: str = "currency") -> str:
    """A currency wages may be calculated in (ISO 4217, not a fund, historic or non-monetary code)."""
    try:
        require_payroll_currency(code)
    except MoneyError as exc:
        raise TimePayError(INVALID_CURRENCY, f"{name}: {exc.message}", name) from None
    return code


def check_minor_unit(minor_unit: int) -> int:
    return require_int(minor_unit, "minor_unit", minimum=0, maximum=4)


def resolve_minor_unit(currency: str, minor_unit: Optional[int] = None) -> int:
    """The currency's ISO 4217 minor unit; an explicit ``minor_unit`` must agree with the catalogue."""
    check_currency(currency)
    catalogue = require_payroll_currency(currency).minor_unit
    assert catalogue is not None  # require_payroll_currency only returns currencies with a minor unit
    if minor_unit is not None and check_minor_unit(minor_unit) != catalogue:
        raise TimePayError(INVALID_CURRENCY, f"{currency} has {catalogue} decimals, not {minor_unit}", "minor_unit")
    return catalogue


def check_rounding_mode(mode: str) -> str:
    if mode not in ROUNDING_MODES:
        raise TimePayError(INVALID_INPUT, f"rounding mode must be one of {', '.join(ROUNDING_MODES)}", "rounding")
    return mode


def quantum(minor_unit: int) -> Decimal:
    return Decimal(1).scaleb(-check_minor_unit(minor_unit))


def round_money(value: Decimal, minor_unit: int, mode: str = "HALF_UP") -> Decimal:
    """Round to the currency's minor unit (JPY 0, INR 2, KWD 3 decimals)."""
    with localcontext(CALC_CONTEXT):
        return value.quantize(quantum(minor_unit), rounding=ROUNDING_MODES[check_rounding_mode(mode)])


def amount_str(value: Decimal, minor_unit: int) -> str:
    return format(value.quantize(quantum(minor_unit)), "f")


def show(value: Decimal, places: int = 4) -> str:
    """Human figure for explanations: grouped digits, long fractions shortened with an ellipsis."""
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > places:
        return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), ",") + "…"
    return format(value, ",")


def hours(minutes: int | Decimal) -> Decimal:
    with localcontext(CALC_CONTEXT):
        return Decimal(minutes) / 60


def show_hours(minutes: int | Decimal) -> str:
    value = hours(minutes)
    if value == value.to_integral_value():
        return f"{int(value)} h"
    return f"{show(value.normalize(), 4)} h"


@dataclass(frozen=True)
class TimePayLine:
    """One proposed payroll line, shaped like the engine's line dict (ARCHITECTURE §4.8)."""

    component: str
    kind: str
    category: str
    amount: Decimal
    unrounded: Decimal
    currency: str
    minor_unit: int
    rounding_mode: str
    method: str
    explanation: str
    units: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    sources: Tuple[Mapping[str, str], ...] = ()

    def to_dict(self) -> dict:
        return {
            "component": self.component,
            "kind": self.kind,
            "category": self.category,
            "amount": amount_str(self.amount, self.minor_unit),
            "currency": self.currency,
            "units": None if self.units is None else str(self.units),
            "rate": None if self.rate is None else str(self.rate),
            "sources": [dict(source) for source in self.sources],
            "trace": {
                "method": self.method,
                "unrounded": str(self.unrounded),
                "rounding": {"stage": "LINE", "mode": self.rounding_mode, "increment": str(quantum(self.minor_unit))},
            },
            "explanation": self.explanation,
        }


def make_line(
    component: str,
    kind: str,
    category: str,
    unrounded: Decimal,
    currency: str,
    *,
    minor_unit: int,
    rounding: str,
    method: str,
    explanation: str,
    units: Optional[Decimal] = None,
    rate: Optional[Decimal] = None,
    sources: Sequence[Mapping[str, str]] = (),
) -> TimePayLine:
    amount = round_money(unrounded, minor_unit, rounding)
    return TimePayLine(component, kind, category, amount, unrounded, currency, minor_unit, rounding, method,
                       explanation, units, rate, tuple(sources))


def units_4dp(minutes: int | Decimal) -> Decimal:
    """Hours for the line's ``units`` column (NUMERIC(10,4)); amounts are always computed from minutes."""
    with localcontext(CALC_CONTEXT):
        return (Decimal(minutes) / 60).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
