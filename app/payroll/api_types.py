"""Pydantic types for money and decimals at the payroll API boundary (3.2 §3.4).

Money crosses the API only as {"amount": "<decimal string>", "currency": "XXX"}:

    StrictModel   extra="forbid", exactly like app.schemas.auth.StrictModel, so
                  clients cannot over-post server-owned fields
    DecimalStr    a Decimal that arrives as a plain decimal string ("12.50",
                  "-3", no exponent or whitespace) and leaves as one; a JSON
                  number, boolean or null is a 422 (EC-MON-15)
    MoneyIn       request money: string amount, known currency that can hold
                  amounts, at most the currency's minor-unit digits unless the
                  model allows extra precision (rates, inputs rounded later),
                  never a historic code unless the model imports old records
                  (EC-MON-14), optionally a payroll currency only (EC-MON-03)
    MoneyOut      response money with exactly the currency's minor-unit digits
                  ("1500" JPY, "12.345" KWD, EC-MON-01/02); an unrounded amount
                  raises instead of being rounded silently

FastAPI validates parsed JSON in Python mode, so a JSON number reaches these
validators as int or float and is rejected; a Decimal object is accepted only
from Python code. Errors are PydanticCustomError with stable lower-case types
(``amount_not_string``, ``fund_currency``, ``amount_not_rounded``...) and the
domain code in ``ctx["code"]``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    PlainSerializer,
    PlainValidator,
    ValidationInfo,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.payroll import currencies
from app.payroll.money import Money, amount_str, decimal_str, to_decimal
from app.payroll.money_errors import CurrencyError, MoneyError

__all__ = [
    "DECIMAL_PATTERN",
    "DecimalStr",
    "MoneyIn",
    "MoneyOut",
    "PayMoneyIn",
    "PreciseMoneyIn",
    "StrictModel",
    "money_in_model",
    "parse_decimal_str",
]

DECIMAL_PATTERN = r"^-?[0-9]{1,28}(\.[0-9]{1,28})?$"
_PLAIN_DECIMAL = re.compile(DECIMAL_PATTERN)
_DECIMAL_SCHEMA: dict[str, Any] = {"type": "string", "pattern": DECIMAL_PATTERN, "examples": ["12345.67"]}


class StrictModel(BaseModel):
    """Rejects unknown fields so clients cannot over-post protected attributes."""

    model_config = ConfigDict(extra="forbid")


def _error(error: MoneyError) -> PydanticCustomError:
    return PydanticCustomError(error.code.lower(), error.message, {"code": error.code})


def parse_decimal_str(value: object, *, field: str = "value") -> Decimal:
    """An exact Decimal from a plain decimal string; anything else raises PydanticCustomError."""
    if isinstance(value, bool) or not isinstance(value, str):
        kind = "a JSON number" if isinstance(value, (int, float)) and not isinstance(value, bool) else type(value).__name__
        raise PydanticCustomError(
            "decimal_not_string",
            f"{field} must be a decimal string such as \"12.34\", not {kind}.",
            {"code": "DECIMAL_NOT_STRING"},
        )
    if not _PLAIN_DECIMAL.fullmatch(value):
        raise PydanticCustomError(
            "invalid_decimal",
            f"{field} must be plain decimal notation (digits, optional minus sign and decimal point).",
            {"code": "INVALID_DECIMAL"},
        )
    try:
        return to_decimal(value)
    except MoneyError as exc:
        raise _error(exc) from exc


def _validate_decimal(value: object, info: ValidationInfo) -> Decimal:
    if isinstance(value, Decimal) and info.mode == "python":
        try:
            return to_decimal(value)
        except MoneyError as exc:
            raise _error(exc) from exc
    return parse_decimal_str(value, field=info.field_name or "value")


DecimalStr = Annotated[
    Decimal,
    PlainValidator(_validate_decimal),
    PlainSerializer(decimal_str, return_type=str, when_used="always"),
    WithJsonSchema(_DECIMAL_SCHEMA),
]


class MoneyIn(StrictModel):
    """Request money. Subclasses (or money_in_model) change the class-level rules below."""

    allow_extra_precision: ClassVar[bool] = False
    payroll_currency_only: ClassVar[bool] = False
    allow_negative: ClassVar[bool] = True
    allow_historic: ClassVar[bool] = False  # only for imports of old records (EC-MON-14: historic codes are not selectable)

    # currency is validated first so the amount rules below know its minor unit
    currency: Annotated[str, WithJsonSchema({"type": "string", "pattern": "^[A-Z]{3}$", "examples": ["INR"]})]
    amount: Annotated[str, WithJsonSchema(_DECIMAL_SCHEMA)]

    @field_validator("currency", mode="before")
    @classmethod
    def _currency_is_known(cls, value: object) -> str:
        try:
            if cls.payroll_currency_only:
                info = currencies.require_payroll_currency(value)
            else:
                info = currencies.require_money_currency(value)
                if info.status == currencies.HISTORIC and not cls.allow_historic:
                    successor = f"; use {info.replaced_by}" if info.replaced_by else ""
                    raise CurrencyError("HISTORIC_CURRENCY", f"{info.code} has been withdrawn from ISO 4217{successor}.")
        except MoneyError as exc:
            raise _error(exc) from exc
        return info.code

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_fits_currency(cls, value: object, info: ValidationInfo) -> str:
        parsed = parse_decimal_str(value, field="amount")
        if parsed < 0 and not cls.allow_negative:
            raise PydanticCustomError("amount_negative", "amount cannot be negative here.", {"code": "AMOUNT_NEGATIVE"})
        currency = info.data.get("currency")
        if currency is not None and not cls.allow_extra_precision:
            try:
                amount_str(parsed, currency)
            except MoneyError as exc:
                raise _error(exc) from exc
        return value  # type: ignore[return-value]

    @property
    def decimal(self) -> Decimal:
        return to_decimal(self.amount)

    def to_money(self) -> Money:
        return Money(self.decimal, self.currency)


def money_in_model(
    name: str,
    *,
    allow_extra_precision: bool = False,
    payroll_currency_only: bool = False,
    allow_negative: bool = True,
    allow_historic: bool = False,
) -> type[MoneyIn]:
    """A MoneyIn variant with its own rules, for example ``money_in_model("SalaryIn", allow_negative=False)``."""
    namespace = {
        "__module__": __name__,
        "__doc__": MoneyIn.__doc__,
        "allow_extra_precision": allow_extra_precision,
        "payroll_currency_only": payroll_currency_only,
        "allow_negative": allow_negative,
        "allow_historic": allow_historic,
    }
    return type(name, (MoneyIn,), namespace)


class PayMoneyIn(MoneyIn):
    """Money in a currency payroll may calculate or pay in: fund, metal and historic codes are a 422 (EC-MON-03)."""

    payroll_currency_only: ClassVar[bool] = True


class PreciseMoneyIn(MoneyIn):
    """Money that is rounded at a later, defined stage (inputs, rate-based amounts): more decimals are allowed."""

    allow_extra_precision: ClassVar[bool] = True


class MoneyOut(BaseModel):
    """Response money; ``amount`` always carries exactly the currency's minor-unit digits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: Annotated[str, WithJsonSchema(_DECIMAL_SCHEMA)]
    currency: Annotated[str, WithJsonSchema({"type": "string", "pattern": "^[A-Z]{3}$"})]

    @model_validator(mode="before")
    @classmethod
    def _canonical(cls, data: object) -> object:
        if isinstance(data, Money):
            source: Mapping[str, object] = {"amount": data.amount, "currency": data.currency}
        elif isinstance(data, Mapping) and "amount" in data and "currency" in data:
            source = data
        else:
            return data
        try:
            amount = source["amount"]
            if isinstance(amount, str):
                amount = parse_decimal_str(amount, field="amount")
            elif not isinstance(amount, Decimal):
                raise MoneyError("INVALID_DECIMAL", "MoneyOut takes a Decimal or a decimal string amount.")
            money = Money(amount, source["currency"])  # type: ignore[arg-type]
            return {**source, **money.to_json()}
        except MoneyError as exc:
            raise _error(exc) from exc

    @classmethod
    def from_money(cls, money: Money) -> MoneyOut:
        return cls.model_validate(money)

    def to_money(self) -> Money:
        return Money(to_decimal(self.amount), self.currency)
