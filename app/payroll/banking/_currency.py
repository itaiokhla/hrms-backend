"""Currency facts for the banking primitives, read from the shared ISO 4217 catalogue.

Payments and files carry exactly the currency's minor-unit digits ("1500" JPY, "12.345" KWD;
3.2 §3.4, EC-MON-01, EC-MON-02) and never a sub-unit the currency cannot carry (3.2 §3.3). Fund,
metal and historic codes are not payable (EC-MON-03). Arithmetic here runs in a wide local context
so products of NUMERIC(19,4) amounts and NUMERIC(12,6) rates are exact before any deliberate rounding.
"""

import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow

from app.payroll import currencies
from app.payroll.banking.errors import BankingError

EXACT = Context(prec=60, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])
AMOUNT_LIMIT = Decimal(10) ** 15  # NUMERIC(19,4) holds 15 integer digits

_CODE = re.compile(r"[A-Z]{3}")


def currency_code(value: object, error: type[BankingError]) -> str:
    """A normalised ISO 4217 code shape, or ``error("CURRENCY_INVALID")``."""
    code = value.strip().upper() if isinstance(value, str) else ""
    if _CODE.fullmatch(code) is None:
        raise error("CURRENCY_INVALID", "currency must be a three-letter ISO 4217 code")
    return code


def payable_minor_unit(code: str, error: type[BankingError]) -> int:
    """Minor-unit digits of a currency payroll may pay in; ``error("CURRENCY_NOT_PAYABLE")`` otherwise."""
    try:
        currencies.require_payroll_currency(code)
        minor_unit = currencies.get(code).minor_unit
    except (LookupError, ValueError):
        raise error("CURRENCY_NOT_PAYABLE", f"{code} is not a currency payroll can pay in") from None
    if minor_unit is None:
        raise error("CURRENCY_NOT_PAYABLE", f"{code} has no minor unit and cannot be paid")
    return minor_unit


def known_minor_unit(code: str) -> int | None:
    """Minor-unit digits of any known currency (statements may show non-payroll currencies)."""
    try:
        return currencies.get(code).minor_unit
    except (LookupError, ValueError):
        return None


def cash_increment(code: str) -> Decimal | None:
    """The currency's cash rounding increment (for example 0.05), when it has one (3.2 §2.2)."""
    increment = currencies.get(code).cash_rounding_increment
    return None if increment is None else Decimal(increment)


def quantum(digits: int) -> Decimal:
    return Decimal(1).scaleb(-digits)


def fits(amount: Decimal, digits: int) -> bool:
    """True when the amount has no non-zero digit below the minor unit."""
    return amount == amount.quantize(quantum(digits), context=EXACT)


def amount_text(amount: Decimal, digits: int) -> str:
    """Plain decimal text with exactly ``digits`` fraction digits (the amount must already fit)."""
    return f"{amount.quantize(quantum(digits), context=EXACT):f}"


def strict_decimal(value: object, error: type[BankingError], label: str) -> Decimal:
    """Decimal from a Decimal, int or decimal string; floats, bools, NaN and infinities are refused."""
    if isinstance(value, bool) or isinstance(value, float):
        raise error("INVALID_AMOUNT", f"{label} must be a Decimal, int or decimal string, never a float")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise error("INVALID_AMOUNT", f"{label} is not a decimal number") from None
    else:
        raise error("INVALID_AMOUNT", f"{label} must be a Decimal, int or decimal string")
    if not result.is_finite():
        raise error("INVALID_AMOUNT", f"{label} must be a finite number")
    return result
