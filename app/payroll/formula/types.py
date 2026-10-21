"""Types and run-time values of the formula language (3.3 §4.4).

Static kinds: MONEY (with a currency), DECIMAL, INTEGER (promoted to DECIMAL in
arithmetic), BOOLEAN, DATE and STRING. At run time a value is a ``MoneyValue``,
a ``Decimal``, a ``bool``, a ``datetime.date``, a ``str`` or ``None`` (NULL, an
unavailable variable). Floats are never accepted anywhere.

Arithmetic uses ``CALC_CONTEXT``: 28 significant digits, ROUND_HALF_EVEN, the
same context as ``app.payroll.money`` (ARCHITECTURE §4.1). Every evaluation works
on a fresh copy, so no mutable decimal state is shared, and intermediate values
are never rounded unless the formula calls ROUND.

The formula package keeps its own small ``MoneyValue`` instead of importing
``app.payroll.money.Money``: any object with ``amount`` (Decimal) and
``currency`` attributes is accepted as money input, and results expose the same
two attributes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow
from enum import StrEnum
from typing import Any, Mapping, Union

from app.payroll.formula.errors import ErrorCode, FormulaError

CALC_CONTEXT = Context(
    prec=28, rounding=ROUND_HALF_EVEN, Emax=999_999, Emin=-999_999, traps=[InvalidOperation, DivisionByZero, Overflow]
)
DEFAULT_MINOR_UNIT = 2
_CURRENCY = re.compile(r"[A-Z]{3}")
_PLAIN_LIMIT = 40  # beyond 10^±40 numbers are shown in scientific notation, never as pages of zeros


def new_context() -> Context:
    return CALC_CONTEXT.copy()


class Kind(StrEnum):
    MONEY = "MONEY"
    DECIMAL = "DECIMAL"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    STRING = "STRING"
    NULL = "NULL"
    ANY = "ANY"  # static only: the type of an expression that already has an error


@dataclass(frozen=True, slots=True)
class FType:
    kind: Kind
    currency: str | None = None  # MONEY only; None in a declaration means "the formula's pay currency"

    @property
    def is_money(self) -> bool:
        return self.kind is Kind.MONEY

    @property
    def is_numeric(self) -> bool:
        return self.kind in (Kind.DECIMAL, Kind.INTEGER)

    def __str__(self) -> str:
        if self.kind is Kind.MONEY and self.currency:
            return f"MONEY({self.currency})"
        return self.kind.value


def money(currency: str) -> FType:
    validate_currency(currency)
    return FType(Kind.MONEY, currency)


DECIMAL = FType(Kind.DECIMAL)
INTEGER = FType(Kind.INTEGER)
BOOLEAN = FType(Kind.BOOLEAN)
DATE = FType(Kind.DATE)
STRING = FType(Kind.STRING)
MONEY_PAY = FType(Kind.MONEY)
ANY = FType(Kind.ANY)


def validate_currency(code: str) -> str:
    if not isinstance(code, str) or not _CURRENCY.fullmatch(code):
        raise ValueError(f"currency must be a three-letter ISO 4217 code, got {code!r}")
    return code


@dataclass(frozen=True, slots=True)
class MoneyValue:
    """An exact amount in one currency. ``int`` amounts are converted; floats are refused."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        amount = self.amount
        if isinstance(amount, bool) or not isinstance(amount, (Decimal, int)):
            raise TypeError(f"money amounts must be Decimal or int, got {type(amount).__name__}")
        amount = Decimal(amount)
        if not amount.is_finite():
            raise ValueError("money amounts must be finite")
        object.__setattr__(self, "amount", amount)
        validate_currency(self.currency)

    def __str__(self) -> str:
        return f"{plain_decimal(self.amount)} {self.currency}"


Value = Union[MoneyValue, Decimal, bool, date, str, None]


def clean(number: Decimal) -> Decimal:
    """Drop the sign of a negative zero so "-0.00" never appears in results."""
    return number.copy_abs() if number.is_zero() and number.is_signed() else number


def kind_of(value: Value) -> Kind:
    if value is None:
        return Kind.NULL
    if isinstance(value, MoneyValue):
        return Kind.MONEY
    if isinstance(value, bool):
        return Kind.BOOLEAN
    if isinstance(value, Decimal):
        return Kind.INTEGER if value == value.to_integral_value() else Kind.DECIMAL
    if isinstance(value, date):
        return Kind.DATE
    if isinstance(value, str):
        return Kind.STRING
    raise TypeError(f"not a formula value: {type(value).__name__}")


def _mismatch(what: str, expected: str, raw: Any) -> FormulaError:
    return FormulaError(ErrorCode.TYPE_MISMATCH, f"{what} must be {expected}, got {type(raw).__name__}")


def to_number(raw: Any, what: str, *, integer: bool = False) -> Decimal:
    """A context number as Decimal; ``int`` is accepted, ``bool`` and ``float`` are not."""
    if isinstance(raw, bool) or not isinstance(raw, (Decimal, int)):
        raise _mismatch(what, "a whole number" if integer else "a decimal number", raw)
    number = Decimal(raw)
    if not number.is_finite():
        raise FormulaError(ErrorCode.TYPE_MISMATCH, f"{what} must be a finite number")
    if integer and number != number.to_integral_value():
        raise FormulaError(ErrorCode.TYPE_MISMATCH, f"{what} must be a whole number, got {plain_decimal(number)}")
    return clean(number)


def to_money(raw: Any, what: str) -> MoneyValue:
    """A context amount: a MoneyValue or any object with Decimal ``amount`` and str ``currency``."""
    if isinstance(raw, MoneyValue):
        return raw
    amount = getattr(raw, "amount", None)
    currency = getattr(raw, "currency", None)
    if isinstance(amount, bool) or not isinstance(amount, (Decimal, int)) or not isinstance(currency, str):
        raise _mismatch(what, "an amount with a currency", raw)
    try:
        return MoneyValue(Decimal(amount), currency)
    except (TypeError, ValueError) as exc:
        raise FormulaError(ErrorCode.TYPE_MISMATCH, f"{what}: {exc}") from None


def to_value(raw: Any, expected: FType, what: str) -> Value:
    """Validate a context value against its declared type; None stays NULL."""
    if raw is None:
        return None
    kind = expected.kind
    if kind is Kind.MONEY:
        return to_money(raw, what)
    if kind in (Kind.DECIMAL, Kind.INTEGER):
        return to_number(raw, what, integer=kind is Kind.INTEGER)
    if kind is Kind.BOOLEAN:
        if not isinstance(raw, bool):
            raise _mismatch(what, "TRUE or FALSE", raw)
        return raw
    if kind is Kind.DATE:
        if isinstance(raw, datetime) or not isinstance(raw, date):
            raise _mismatch(what, "a date", raw)
        return raw
    if kind is Kind.STRING:
        if not isinstance(raw, str):
            raise _mismatch(what, "text", raw)
        return raw
    raise FormulaError(ErrorCode.TYPE_MISMATCH, f"{what} has no usable type")


def plain_decimal(number: Decimal, min_places: int = 0) -> str:
    """Exact digits without exponent notation, padded to at least ``min_places`` decimals.

    Trailing zeros beyond ``min_places`` are dropped (20000.0000 shows as 20000.00
    for a two-decimal currency) but significant digits never are, so unrounded
    intermediate values stay visible in traces.
    """
    if not number.is_finite():
        return str(number)
    if number.is_zero():
        return "0" + ("." + "0" * min_places if min_places > 0 else "")
    if abs(number.adjusted()) > _PLAIN_LIMIT:
        return str(number.normalize(new_context()))
    sign, digits, exponent = number.as_tuple()
    coefficient = list(digits)
    while exponent < -min_places and len(coefficient) > 1 and coefficient[-1] == 0:
        coefficient.pop()
        exponent += 1
    if exponent > -min_places:
        coefficient.extend([0] * (exponent + min_places))
        exponent = -min_places
    return format(Decimal((sign, tuple(coefficient), exponent)), "f")


def format_value(value: Value, minor_units: Mapping[str, int] | None = None) -> str:
    """Display form used in traces: ``50000.00 INR``, ``28``, ``TRUE``, ``2026-11-30``, ``"G3"``, ``NULL``."""
    if value is None:
        return "NULL"
    if isinstance(value, MoneyValue):
        places = (minor_units or {}).get(value.currency, DEFAULT_MINOR_UNIT)
        return f"{plain_decimal(value.amount, places)} {value.currency}"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Decimal):
        return plain_decimal(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return f'"{value}"'
    raise TypeError(f"not a formula value: {type(value).__name__}")


def value_to_json(value: Value) -> Any:
    """JSON-safe form: money as ``{"amount": "...", "currency": "..."}`` with exact, unrounded digits."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, MoneyValue):
        return {"amount": plain_decimal(value.amount), "currency": value.currency}
    if isinstance(value, Decimal):
        return plain_decimal(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"not a formula value: {type(value).__name__}")
