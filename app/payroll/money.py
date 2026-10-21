"""Money: exact decimal amounts that always carry their ISO 4217 currency.

Payroll arithmetic never uses floats (contract §2). Amounts are Decimals
evaluated in CALC_CONTEXT (28 significant digits, half-even for intermediate
results) whatever the thread's decimal context is, and every amount carries a
currency validated against app.payroll.currencies:

    Money + Money, Money - Money      same currency only, else CurrencyMismatch
    Money * Decimal|int, Money / Decimal|int
    Money / Money -> Decimal          same currency only
    <, <=, >, >=                      same currency only

Rounding happens only at the stages of 3.2 §3.3: each line to the minor unit
with payroll.rounding.line_mode, statutory lines with the pack's rule (which
overrides company settings, EC-STA-13), net pay to
payroll.rounding.net_pay_increment with the difference returned for a
NET_ROUNDING line or a carry-forward (EC-MON-11), cash payments to the
currency's cash increment. Totals are plain sums of rounded lines and are never
re-rounded (EC-MON-10). Rounding to an increment is exact (integer quotient and
remainder, never a rounded division), and results carry exactly the currency's
minor-unit digits, so "1500" for JPY and "12.345" for KWD (EC-MON-01/02).
Products that are rounded straight away (FX conversions, percentage splits)
use exact_product and quantize_exact, so they are rounded once, never first to
28 digits and then again to the minor unit.
Serialisation and display never round: an amount with more digits than its
currency allows raises instead.
"""

from __future__ import annotations

import decimal
import re
import unicodedata
from copy import copy
from dataclasses import dataclass
from types import MappingProxyType
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Iterable, Mapping, NamedTuple, Sequence, Union

from babel.numbers import get_currency_symbol

from app.payroll import currencies
from app.payroll.money_errors import CurrencyError, CurrencyMismatch, LocaleError, MoneyError

__all__ = [
    "Allocation",
    "CALC_CONTEXT",
    "CurrencyError",
    "CurrencyMismatch",
    "DIFFERENCE_HANDLING",
    "LINE_ROUNDING_MODES",
    "LocaleError",
    "MAX_SIGNIFICANT_DIGITS",
    "MAX_STORABLE_AMOUNT",
    "Money",
    "MoneyError",
    "NET_PAY_ROUNDING_MODES",
    "NetRounding",
    "ROUNDING_MODES",
    "ROUNDING_STAGES",
    "Rounded",
    "STATUTORY_ROUNDING",
    "STORAGE_PRECISION",
    "STORAGE_SCALE",
    "allocate_percentages",
    "amount_str",
    "check_increment",
    "decimal_str",
    "ensure_storable",
    "exact_product",
    "format_money",
    "format_many",
    "is_storable",
    "minor_unit",
    "needs_iso_codes",
    "quantize_amount",
    "quantize_exact",
    "quantum",
    "round_cash",
    "round_line",
    "round_net",
    "round_statutory",
    "sum_money",
    "to_decimal",
    "totals_by_currency",
    "validate_net_pay_increment",
]

CALC_CONTEXT = decimal.Context(prec=28, rounding=ROUND_HALF_EVEN)
MAX_SIGNIFICANT_DIGITS = 28
# Only for the multiplication that rebuilds an increment multiple: any rounding there is a bug, not a result.
_EXACT_CONTEXT = decimal.Context(
    prec=MAX_SIGNIFICANT_DIGITS,
    rounding=ROUND_HALF_EVEN,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow, decimal.Inexact],
)
# Exact products of two values of at most 28 significant digits (at most 56 digits), rounded once afterwards.
_WIDE_PREC = 2 * MAX_SIGNIFICANT_DIGITS + 4
_WIDE_EXACT = decimal.Context(
    prec=_WIDE_PREC,
    rounding=ROUND_HALF_EVEN,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow, decimal.Inexact],
)
_WIDE_ROUNDING = decimal.Context(
    prec=_WIDE_PREC,
    rounding=ROUND_HALF_EVEN,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)

_DECIMAL_ROUNDING: Mapping[str, str] = MappingProxyType(
    {
        "HALF_UP": ROUND_HALF_UP,  # ties away from zero
        "HALF_EVEN": ROUND_HALF_EVEN,
        "UP": ROUND_UP,  # away from zero
        "DOWN": ROUND_DOWN,  # toward zero
        "CEILING": ROUND_CEILING,
        "FLOOR": ROUND_FLOOR,
    }
)
ROUNDING_MODES: frozenset[str] = frozenset(_DECIMAL_ROUNDING)
LINE_ROUNDING_MODES: tuple[str, ...] = ("HALF_UP", "HALF_EVEN", "UP", "DOWN")  # payroll.rounding.line_mode
NET_PAY_ROUNDING_MODES: tuple[str, ...] = ("HALF_UP", "UP", "DOWN")  # payroll.rounding.net_pay_mode
DIFFERENCE_HANDLING: tuple[str, ...] = ("ROUNDING_LINE", "CARRY_FORWARD")  # payroll.rounding.difference_handling
ROUNDING_STAGES: tuple[str, ...] = ("LINE", "STATUTORY", "NET", "CASH")
# Pack rules that override line_mode (3.2 §3.3); "up" is away from zero so a reversal cancels its original.
STATUTORY_ROUNDING: Mapping[str, tuple[str, Decimal]] = MappingProxyType(
    {
        "NEAREST_WHOLE_UNIT": ("HALF_UP", Decimal(1)),
        "UP_TO_WHOLE_UNIT": ("UP", Decimal(1)),
    }
)
MAX_NET_PAY_INCREMENT_UNITS = Decimal(100)  # V-13

STORAGE_PRECISION = 19  # NUMERIC(19,4), 3.2 §3.2
STORAGE_SCALE = 4
MAX_STORABLE_AMOUNT = Decimal("999999999999999.9999")
_STORAGE_QUANTUM = Decimal((0, (1,), -STORAGE_SCALE))

_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?")
_MAX_TEXT_LENGTH = 64
_NBSP = " "

Number = Union[Decimal, int, str]


def to_decimal(value: object) -> Decimal:
    """An exact Decimal from str, int or Decimal; floats, booleans, NaN and infinities raise MoneyError.

    Strings use ASCII decimal notation only (no whitespace, underscores or non-ASCII digits). Values with
    more than 28 significant digits, or of 10**28 and above, are rejected instead of being rounded later.
    """
    if isinstance(value, bool):
        raise MoneyError("INVALID_DECIMAL", "A boolean is not an amount.")
    if isinstance(value, float):
        raise MoneyError("FLOAT_NOT_ALLOWED", "Floats are never used for money; pass a decimal string or Decimal.")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        if len(value) > _MAX_TEXT_LENGTH or not _DECIMAL_TEXT.fullmatch(value):
            raise MoneyError("INVALID_DECIMAL", f"{value[:_MAX_TEXT_LENGTH]!r} is not a decimal number.")
        result = Decimal(value)
    else:
        raise MoneyError("INVALID_DECIMAL", f"Expected a decimal string, int or Decimal, got {type(value).__name__}.")
    if not result.is_finite():
        raise MoneyError("NOT_FINITE", "NaN and infinity are not amounts.")
    if not result.is_zero() and (_significant_digits(result) > MAX_SIGNIFICANT_DIGITS or result.adjusted() >= MAX_SIGNIFICANT_DIGITS):
        raise MoneyError("PRECISION_EXCEEDED", f"{decimal_str(result)} has more than {MAX_SIGNIFICANT_DIGITS} significant digits.")
    return _clean_zero(result)


def _significant_digits(value: Decimal) -> int:
    _, digits, exponent = value.as_tuple()
    count = len(digits)
    if isinstance(exponent, int) and exponent < 0:
        removable = -exponent
        for digit in reversed(digits):
            if digit != 0 or removable == 0:
                break
            count -= 1
            removable -= 1
    return count


def _clean_zero(value: Decimal) -> Decimal:
    return value.copy_abs() if value.is_zero() and value.is_signed() else value


def decimal_str(value: Decimal) -> str:
    """Plain decimal notation (never an exponent), keeping trailing zeros: Decimal("1E+3") -> "1000"."""
    return format(_clean_zero(value), "f")


def _rounding(mode: object) -> str:
    rounding = _DECIMAL_ROUNDING.get(mode) if isinstance(mode, str) else None
    if rounding is None:
        raise MoneyError("UNKNOWN_ROUNDING_MODE", f"{mode!r} is not one of {sorted(ROUNDING_MODES)}.")
    return rounding


def minor_unit(currency: str) -> int:
    """The ISO 4217 minor unit (number of decimals) of a currency that can hold amounts."""
    digits = currencies.require_money_currency(currency).minor_unit
    if digits is None:  # require_money_currency only returns currencies with a minor unit
        raise CurrencyError("NON_MONETARY_CURRENCY", f"{currency} has no minor unit.")
    return digits


def quantum(currency: str) -> Decimal:
    """The smallest amount of a currency: Decimal("1") for JPY, Decimal("0.01") for INR, Decimal("0.001") for KWD."""
    return Decimal((0, (1,), -minor_unit(currency)))


def check_increment(increment: Number, currency: str) -> Decimal:
    """A positive rounding increment that is a whole multiple of the currency's minor unit (so results stay representable)."""
    step = to_decimal(increment)
    if step <= 0:
        raise MoneyError("INVALID_INCREMENT", f"A rounding increment must be positive, got {decimal_str(step)}.")
    try:
        multiple = CALC_CONTEXT.remainder(step, quantum(currency)).is_zero()
    except decimal.InvalidOperation:
        multiple = False
    if not multiple:
        raise MoneyError(
            "INVALID_INCREMENT",
            f"{decimal_str(step)} is not a multiple of the {currency} minor unit {decimal_str(quantum(currency))}.",
        )
    return step


def validate_net_pay_increment(increment: Number, currency: str) -> Decimal:
    """payroll.rounding.net_pay_increment: a multiple of the minor unit and at most 100 units (V-13)."""
    step = check_increment(increment, currency)
    if step > MAX_NET_PAY_INCREMENT_UNITS:
        raise MoneyError("INVALID_INCREMENT", f"Net pay can be rounded to at most 100 {currency}, got {decimal_str(step)}.")
    return step


def _away_from_zero(mode: str, quotient: Decimal, remainder: Decimal, step: Decimal, context: decimal.Context) -> bool:
    if mode == "UP":
        return True
    if mode == "DOWN":
        return False
    if mode == "CEILING":
        return remainder > 0
    if mode == "FLOOR":
        return remainder < 0
    half = _WIDE_EXACT.divide(step, 2)  # exact: halving adds at most one digit; comparisons never round
    distance = remainder.copy_abs()
    if mode == "HALF_UP":
        return distance >= half
    return distance > half or (distance == half and not context.remainder(quotient, 2).is_zero())  # HALF_EVEN


def _round_to_step(value: Decimal, step: Decimal, mode: str, context: decimal.Context = CALC_CONTEXT) -> Decimal:
    quotient, remainder = context.divmod(value, step)  # quotient truncated toward zero, remainder has value's sign
    if not remainder.is_zero() and _away_from_zero(mode, quotient, remainder, step, context):
        quotient = context.add(quotient, 1 if remainder > 0 else -1)
    return (_WIDE_EXACT if context is _WIDE_ROUNDING else _EXACT_CONTEXT).multiply(quotient, step)


def _quantize(value: Decimal, currency: str, mode: str, increment: Number | None, context: decimal.Context) -> Decimal:
    rounding = _rounding(mode)
    unit = quantum(currency)
    try:
        if increment is None:
            result = value.quantize(unit, rounding=rounding, context=context)
        else:
            result = _round_to_step(value, check_increment(increment, currency), mode, context).quantize(unit, context=context)
    except (decimal.InvalidOperation, decimal.Inexact) as exc:
        raise MoneyError("PRECISION_EXCEEDED", f"{decimal_str(value)} {currency} cannot be rounded within 28 digits.") from exc
    if not result.is_zero() and (_significant_digits(result) > MAX_SIGNIFICANT_DIGITS or result.adjusted() >= MAX_SIGNIFICANT_DIGITS):
        raise MoneyError("PRECISION_EXCEEDED", f"{decimal_str(result)} {currency} has more than 28 significant digits.")
    return _clean_zero(result)


def quantize_amount(amount: Number, currency: str, mode: str = "HALF_UP", increment: Number | None = None) -> Decimal:
    """``amount`` rounded with ``mode`` to the currency's minor unit, or to a multiple of ``increment``.

    The result always has exactly the currency's minor-unit digits (Decimal("46667.00") for INR with
    increment 1) and is never negative zero.
    """
    return _quantize(to_decimal(amount), currency, mode, increment, CALC_CONTEXT)


def exact_product(left: Number, right: Number) -> Decimal:
    """``left * right`` with no rounding at all (up to 56 significant digits), for a value rounded once afterwards."""
    a, b = to_decimal(left), to_decimal(right)
    try:
        return _clean_zero(_WIDE_EXACT.multiply(a, b))
    except (decimal.InvalidOperation, decimal.Inexact, decimal.Overflow) as exc:  # unreachable for 28-digit inputs
        raise MoneyError("PRECISION_EXCEEDED", "The exact product does not fit 56 significant digits.") from exc


def quantize_exact(value: Decimal, currency: str, mode: str = "HALF_UP", increment: Number | None = None) -> Decimal:
    """Like quantize_amount, for an exact intermediate wider than 28 digits (exact_product): rounded once, never twice."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MoneyError("INVALID_DECIMAL", "quantize_exact takes a finite Decimal.")
    return _quantize(value, currency, mode, increment, _WIDE_ROUNDING)


def _exact_digits(value: Decimal, digits: int, currency: str) -> Decimal:
    unit = Decimal((0, (1,), -digits))
    try:
        exact = value.quantize(unit, rounding=ROUND_DOWN, context=CALC_CONTEXT)
    except decimal.InvalidOperation as exc:
        raise MoneyError("PRECISION_EXCEEDED", f"{decimal_str(value)} {currency} has too many digits.") from exc
    if exact != value:
        raise MoneyError(
            "AMOUNT_NOT_ROUNDED",
            f"{decimal_str(value)} has more than {digits} decimals for {currency}; round it at a defined stage first.",
        )
    return _clean_zero(exact)


def amount_str(amount: Number, currency: str) -> str:
    """The amount with exactly the currency's minor-unit digits ("1500" JPY, "12.345" KWD, "12.30" INR).

    Never rounds: an amount with more significant decimals than the currency allows raises AMOUNT_NOT_ROUNDED.
    """
    return decimal_str(_exact_digits(to_decimal(amount), minor_unit(currency), currency))


def is_storable(amount: Number) -> bool:
    """Whether a NUMERIC(19,4) money column holds the amount exactly (EC-MON-12)."""
    value = to_decimal(amount)
    return value.copy_abs() <= MAX_STORABLE_AMOUNT and value.quantize(_STORAGE_QUANTUM, context=CALC_CONTEXT) == value


def _checked(amount: Decimal) -> Decimal:
    if not amount.is_zero() and amount.adjusted() >= MAX_SIGNIFICANT_DIGITS:
        raise MoneyError("PRECISION_EXCEEDED", "The result exceeds 28 significant digits.")
    return _clean_zero(amount)


def _make(amount: Decimal, currency: str) -> Money:
    money = object.__new__(Money)
    object.__setattr__(money, "amount", _checked(amount))
    object.__setattr__(money, "currency", currency)
    return money


def _factor(value: object) -> Decimal | None:
    if isinstance(value, Money):
        raise MoneyError("INVALID_OPERATION", "Amounts can only be multiplied or divided by plain numbers.")
    if isinstance(value, float):
        raise MoneyError("FLOAT_NOT_ALLOWED", "Floats are never used for money; multiply by a Decimal.")
    if isinstance(value, (Decimal, int)) and not isinstance(value, bool):
        return to_decimal(value)
    return None


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount in one currency. Arithmetic never mixes currencies and never touches floats."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        amount = to_decimal(self.amount)
        currencies.require_money_currency(self.currency)
        object.__setattr__(self, "amount", amount)

    @classmethod
    def of(cls, amount: Number, currency: str) -> Money:
        return cls(to_decimal(amount), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return _make(Decimal((0, (0,), -minor_unit(currency))), currency)

    @classmethod
    def from_json(cls, data: Mapping[str, object], *, exact: bool = True) -> Money:
        """Parse {"amount": "12.34", "currency": "INR"}; a JSON number as amount is rejected (EC-MON-15)."""
        if not isinstance(data, Mapping) or set(data) != {"amount", "currency"}:
            raise MoneyError("INVALID_MONEY", 'Money must be an object {"amount": "<decimal string>", "currency": "<ISO 4217>"}.')
        amount = data["amount"]
        if not isinstance(amount, str):
            raise MoneyError("AMOUNT_NOT_STRING", "Money amounts must be decimal strings, never JSON numbers.")
        money = cls.of(amount, data["currency"])  # type: ignore[arg-type]
        if exact:
            amount_str(money.amount, money.currency)
        return money

    def _same(self, other: Money, operation: str) -> None:
        if other.currency != self.currency:
            raise CurrencyMismatch(self.currency, other.currency, operation)

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._same(other, "add")
        return _make(CALC_CONTEXT.add(self.amount, other.amount), self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._same(other, "subtract")
        return _make(CALC_CONTEXT.subtract(self.amount, other.amount), self.currency)

    def __neg__(self) -> Money:
        return _make(self.amount.copy_negate(), self.currency)

    def __pos__(self) -> Money:
        return self

    def __abs__(self) -> Money:
        return _make(self.amount.copy_abs(), self.currency)

    def __mul__(self, other: object) -> Money:
        factor = _factor(other)
        if factor is None:
            return NotImplemented
        return _make(CALC_CONTEXT.multiply(self.amount, factor), self.currency)

    __rmul__ = __mul__

    def __truediv__(self, other: object) -> Money | Decimal:
        if isinstance(other, Money):
            self._same(other, "divide")
            if other.amount.is_zero():
                raise MoneyError("DIVISION_BY_ZERO", f"Cannot divide by a zero {other.currency} amount.")
            return CALC_CONTEXT.divide(self.amount, other.amount)
        divisor = _factor(other)
        if divisor is None:
            return NotImplemented
        if divisor.is_zero():
            raise MoneyError("DIVISION_BY_ZERO", "Cannot divide an amount by zero.")
        return _make(CALC_CONTEXT.divide(self.amount, divisor), self.currency)

    def _compare(self, other: object) -> Decimal | None:
        if not isinstance(other, Money):
            return None
        self._same(other, "compare")
        return other.amount

    def __lt__(self, other: object) -> bool:
        value = self._compare(other)
        return NotImplemented if value is None else self.amount < value

    def __le__(self, other: object) -> bool:
        value = self._compare(other)
        return NotImplemented if value is None else self.amount <= value

    def __gt__(self, other: object) -> bool:
        value = self._compare(other)
        return NotImplemented if value is None else self.amount > value

    def __ge__(self, other: object) -> bool:
        value = self._compare(other)
        return NotImplemented if value is None else self.amount >= value

    def __bool__(self) -> bool:
        return not self.amount.is_zero()

    def __str__(self) -> str:
        return f"{decimal_str(self.amount)} {self.currency}"

    def is_zero(self) -> bool:
        return self.amount.is_zero()

    def is_negative(self) -> bool:
        return self.amount < 0

    def is_positive(self) -> bool:
        return self.amount > 0

    def round(self, mode: str = "HALF_UP", increment: Number | None = None) -> Money:
        """Rounded to the minor unit, or to a multiple of ``increment``, with ``mode``."""
        return _make(quantize_amount(self.amount, self.currency, mode, increment), self.currency)

    def to_json(self) -> dict[str, str]:
        """{"amount": "<exactly the minor-unit digits>", "currency": "XXX"}; raises if the amount is not rounded."""
        return {"amount": amount_str(self.amount, self.currency), "currency": self.currency}

    def format(self, locale: str = "en", *, use_code: bool = False) -> str:
        return format_money(self.amount, self.currency, locale, use_code=use_code)


def sum_money(items: Iterable[Money], currency: str) -> Money:
    """The total of amounts that must all be in ``currency``; zero for no items. Never sums across currencies."""
    total = Money.zero(currency)
    for item in items:
        if not isinstance(item, Money):
            raise MoneyError("INVALID_OPERATION", f"Only Money can be summed, got {type(item).__name__}.")
        total = total + item
    return total


def totals_by_currency(items: Iterable[Money]) -> dict[str, Money]:
    """One subtotal per currency, ordered by currency code; amounts in different currencies are never added (EC-MON-04)."""
    totals: dict[str, Money] = {}
    for item in items:
        if not isinstance(item, Money):
            raise MoneyError("INVALID_OPERATION", f"Only Money can be totalled, got {type(item).__name__}.")
        totals[item.currency] = totals[item.currency] + item if item.currency in totals else item
    return {code: totals[code] for code in sorted(totals)}


def ensure_storable(money: Money) -> Money:
    if not is_storable(money.amount):
        raise MoneyError(
            "AMOUNT_NOT_STORABLE",
            f"{decimal_str(money.amount)} {money.currency} does not fit a NUMERIC({STORAGE_PRECISION},{STORAGE_SCALE}) column.",
        )
    return money


@dataclass(frozen=True)
class Rounded:
    """One rounding step with the value before and after it, for the calculation trace (3.5 §8.3, EC-MON-10)."""

    money: Money
    unrounded: Decimal
    stage: str
    mode: str
    increment: Decimal

    @property
    def difference(self) -> Money:
        """What rounding added: rounded minus unrounded (may have more digits than the currency)."""
        return _make(CALC_CONTEXT.subtract(self.money.amount, self.unrounded), self.money.currency)

    def trace(self) -> dict[str, object]:
        return {
            "unrounded": decimal_str(self.unrounded),
            "rounding": {"stage": self.stage, "mode": self.mode, "increment": decimal_str(self.increment)},
            "amount": self.money.to_json(),
        }


def _rounded(value: Money, stage: str, mode: str, increment: Decimal | None) -> Rounded:
    step = quantum(value.currency) if increment is None else increment
    return Rounded(value.round(mode, increment), value.amount, stage, mode, step)


def round_line(value: Money, mode: str = "HALF_UP") -> Rounded:
    """A payroll line rounded to its currency's minor unit with payroll.rounding.line_mode."""
    if mode not in LINE_ROUNDING_MODES:
        raise MoneyError("UNSUPPORTED_ROUNDING_MODE", f"Line rounding uses one of {LINE_ROUNDING_MODES}, got {mode!r}.")
    return _rounded(value, "LINE", mode, None)


def round_statutory(value: Money, rule: str) -> Rounded:
    """A statutory line rounded by its pack rule ("NEAREST_WHOLE_UNIT", "UP_TO_WHOLE_UNIT"); overrides line_mode (EC-STA-13)."""
    if rule not in STATUTORY_ROUNDING:
        raise MoneyError("UNKNOWN_ROUNDING_RULE", f"{rule!r} is not one of {sorted(STATUTORY_ROUNDING)}.")
    mode, increment = STATUTORY_ROUNDING[rule]
    return _rounded(value, "STATUTORY", mode, increment)


class NetRounding(NamedTuple):
    """An amount rounded to an increment and what the rounding took off: ``rounded + difference == original``."""

    rounded: Money
    difference: Money  # original - rounded: carried forward to the next period, or the negated NET_ROUNDING line

    @property
    def line_amount(self) -> Money:
        """The NET_ROUNDING line that turns the unrounded net into the rounded net (rounded - original)."""
        return -self.difference


def round_net(net: Money, increment: Number | None = None, mode: str = "HALF_UP") -> NetRounding:
    """Net pay rounded to payroll.rounding.net_pay_increment (default: the minor unit) with net_pay_mode (EC-MON-11).

    The difference becomes a NET_ROUNDING line (``line_amount``) or is carried forward to the next period,
    as payroll.rounding.difference_handling says. Increment 1 rounds INR net pay to whole rupees.
    """
    if mode not in NET_PAY_ROUNDING_MODES:
        raise MoneyError("UNSUPPORTED_ROUNDING_MODE", f"Net pay rounding uses one of {NET_PAY_ROUNDING_MODES}, got {mode!r}.")
    step = None if increment is None else validate_net_pay_increment(increment, net.currency)
    rounded = net.round(mode, step)
    return NetRounding(rounded, net - rounded)


def round_cash(amount: Money, mode: str = "HALF_UP") -> NetRounding:
    """A cash payment rounded to the currency's cash increment (CHF 0.05, SEK 1); minor unit when it has none."""
    increment = currencies.get(amount.currency).cash_rounding_increment
    rounded = amount.round(mode, increment)
    return NetRounding(rounded, amount - rounded)


class Allocation(NamedTuple):
    shares: tuple[Money, ...]
    remainder: Money


def allocate_percentages(total: Money, percents: Sequence[Number]) -> Allocation:
    """Percentage shares of ``total`` rounded toward zero to the minor unit; the remainder account gets the rest.

    shares + remainder == total exactly, so rounding differences always land in the remainder account (3.7 §5).
    """
    parsed = [to_decimal(percent) for percent in percents]
    if any(percent < 0 or percent > 100 for percent in parsed):
        raise MoneyError("INVALID_PERCENT", "Each percentage must be between 0 and 100.")
    allocated = Decimal(0)
    for percent in parsed:
        allocated = CALC_CONTEXT.add(allocated, percent)
    if allocated > 100:
        raise MoneyError("INVALID_PERCENT", "Percentages of one amount cannot add up to more than 100.")
    shares = tuple(
        _make(quantize_exact(exact_product(total.amount, percent).scaleb(-2, context=_WIDE_EXACT), total.currency, "DOWN"), total.currency)
        for percent in parsed
    )
    return Allocation(shares, total - sum_money(shares, total.currency))


def needs_iso_codes(codes: Iterable[str]) -> bool:
    """Whether amounts must be shown with ISO codes rather than symbols: several currencies on one screen (EC-MON-16)."""
    return len(set(codes)) > 1


def _spaced(prefixes: tuple[str, str], suffixes: tuple[str, str], text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    # CLDR currencySpacing: a letter-like currency next to a digit gets a no-break space ("KWD 12.345", not "KWD12.345").
    def separable(char: str) -> bool:
        return not unicodedata.category(char).startswith(("S", "Z"))

    new_prefixes = tuple(p + _NBSP if p.endswith("¤") and separable(text[-1]) else p for p in prefixes)
    new_suffixes = tuple(_NBSP + s if s.startswith("¤") and separable(text[0]) else s for s in suffixes)
    return new_prefixes, new_suffixes


def format_money(
    amount: Number,
    currency: str,
    locale: str = "en",
    *,
    use_code: bool = False,
    fraction_digits: int | None = None,
) -> str:
    """Display text for an amount: CLDR grouping and symbol for ``locale``, exactly the currency's minor-unit digits.

    ``format_money("1234567", "INR", "en-IN")`` is "₹12,34,567.00"; ``use_code`` shows "USD 1,234.50" instead of
    "$1,234.50" (use it whenever several currencies share a page, EC-MON-16). ``fraction_digits`` may show more
    digits than the minor unit (rates), never fewer. Never rounds; for PDFs and exports, not for calculations.
    """
    minor = minor_unit(currency)
    digits = minor if fraction_digits is None else fraction_digits
    if isinstance(digits, bool) or not isinstance(digits, int) or not minor <= digits <= 12:
        raise MoneyError("INVALID_FRACTION_DIGITS", f"{currency} shows between {minor} and 12 decimals.")
    value = _exact_digits(to_decimal(amount), digits, currency)
    cldr = currencies.babel_locale(locale)
    pattern = copy(cldr.currency_formats["standard"])
    pattern.frac_prec = (digits, digits)
    prefixes, suffixes = pattern.prefix, pattern.suffix
    if use_code:
        prefixes = tuple(re.sub("(?<!¤)¤(?!¤)", "¤¤", p) for p in prefixes)
        suffixes = tuple(re.sub("(?<!¤)¤(?!¤)", "¤¤", s) for s in suffixes)
    text = currency if use_code else get_currency_symbol(currency, locale=cldr)
    pattern.prefix, pattern.suffix = _spaced(prefixes, suffixes, text)
    return pattern.apply(value, cldr, currency=currency, currency_digits=False, decimal_quantization=True)


def format_many(amounts: Sequence[Money], locale: str = "en") -> list[str]:
    """Display texts for amounts shown together: ISO codes as soon as two currencies appear (EC-MON-16)."""
    use_code = needs_iso_codes(item.currency for item in amounts)
    return [format_money(item.amount, item.currency, locale, use_code=use_code) for item in amounts]
