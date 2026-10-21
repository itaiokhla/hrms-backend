"""Pure tests for the API money types in app.payroll.api_types (3.2 §3.4, EC-MON-01/02/03/15)."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.payroll.api_types import DecimalStr, MoneyIn, MoneyOut, PayMoneyIn, PreciseMoneyIn, StrictModel, money_in_model
from app.payroll.money import Money

D = Decimal


class RateBody(StrictModel):
    rate: DecimalStr
    note: str | None = None


class SalaryBody(StrictModel):
    salary: PayMoneyIn


def errors(excinfo: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, tuple[Any, ...]]]:
    return [(error["type"], tuple(error["loc"])) for error in excinfo.value.errors()]


# --- EC-MON-15: JSON numbers ------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ['{"amount": 12.34, "currency": "INR"}', '{"amount": 12, "currency": "INR"}', '{"amount": 1e3, "currency": "INR"}'])
def test_ec_mon_15_json_number_rejected_by_money_in(raw: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate_json(raw)
    assert errors(excinfo) == [("decimal_not_string", ("amount",))]
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate(json.loads(raw))  # FastAPI validates parsed JSON in Python mode
    assert errors(excinfo) == [("decimal_not_string", ("amount",))]


@pytest.mark.parametrize("value", [True, None, [], {"value": "1"}, D("12.34")])
def test_ec_mon_15_non_string_amounts_rejected(value: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate({"amount": value, "currency": "INR"})
    assert errors(excinfo) == [("decimal_not_string", ("amount",))]


def test_ec_mon_15_decimal_string_accepted() -> None:
    body = MoneyIn.model_validate_json('{"amount": "12.34", "currency": "INR"}')
    assert body.amount == "12.34" and body.decimal == D("12.34")
    assert body.to_money() == Money.of("12.34", "INR")


def test_ec_mon_15_json_number_rejected_by_decimal_str() -> None:
    for raw in ('{"rate": 83.5}', '{"rate": 83}', '{"rate": true}', '{"rate": null}'):
        with pytest.raises(ValidationError) as excinfo:
            RateBody.model_validate_json(raw)
        assert errors(excinfo) == [("decimal_not_string", ("rate",))]
    assert RateBody.model_validate_json('{"rate": "83.912345678901"}').rate == D("83.912345678901")


@pytest.mark.parametrize("text", ["1e3", "1,000.00", " 12.34", "12.34 ", "+12.34", "12.", ".5", "NaN", "Infinity", "", "١٢", "1_000"])
def test_plain_decimal_notation_only(text: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate({"amount": text, "currency": "INR"})
    assert errors(excinfo) == [("invalid_decimal", ("amount",))]
    with pytest.raises(ValidationError):
        RateBody.model_validate({"rate": text})


def test_decimal_str_round_trip_and_schema() -> None:
    body = RateBody.model_validate({"rate": "2.50"})
    assert isinstance(body.rate, Decimal) and str(body.rate) == "2.50"
    assert body.model_dump_json() == '{"rate":"2.50","note":null}'
    assert RateBody.model_validate({"rate": D("1.5")}).rate == D("1.5")  # Python callers may pass a Decimal
    schema = RateBody.model_json_schema()["properties"]["rate"]
    assert schema["type"] == "string" and "pattern" in schema
    with pytest.raises(ValidationError) as excinfo:
        RateBody.model_validate({"rate": D("NaN")})
    assert errors(excinfo) == [("not_finite", ("rate",))]


# --- precision: EC-MON-01 / 02 ----------------------------------------------------------------------


@pytest.mark.parametrize("amount, currency", [("1500", "JPY"), ("12.345", "KWD"), ("12.34", "INR"), ("12.3", "INR"), ("-5.00", "USD"), ("1.2345", "CLF")])
def test_digits_up_to_minor_unit_accepted(amount: str, currency: str) -> None:
    assert MoneyIn.model_validate({"amount": amount, "currency": currency}).decimal == D(amount)


def test_ec_mon_01_jpy_rejects_decimals() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate({"amount": "1500.5", "currency": "JPY"})
    assert errors(excinfo) == [("amount_not_rounded", ("amount",))]
    assert MoneyIn.model_validate({"amount": "1500.0", "currency": "JPY"}).to_money().to_json()["amount"] == "1500"


def test_ec_mon_02_kwd_rejects_a_fourth_decimal() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate({"amount": "12.3456", "currency": "KWD"})
    assert errors(excinfo) == [("amount_not_rounded", ("amount",))]


def test_allow_extra_precision_for_inputs_rounded_later() -> None:
    assert PreciseMoneyIn.model_validate({"amount": "12.3456", "currency": "INR"}).decimal == D("12.3456")
    assert PreciseMoneyIn.model_validate({"amount": "1500.5", "currency": "JPY"}).to_money().amount == D("1500.5")


# --- currency rules: EC-MON-03 ----------------------------------------------------------------------


@pytest.mark.parametrize("currency, error", [("CLF", "fund_currency"), ("UYI", "fund_currency"), ("XAU", "non_monetary_currency"), ("HRK", "historic_currency"), ("ABC", "unknown_currency"), ("inr", "invalid_currency_code")])
def test_ec_mon_03_pay_money_rejects_non_payroll_currency(currency: str, error: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        SalaryBody.model_validate({"salary": {"amount": "1", "currency": currency}})
    assert errors(excinfo) == [(error, ("salary", "currency"))]
    assert excinfo.value.errors()[0]["ctx"]["code"] == error.upper()


def test_ec_mon_03_pay_money_accepts_circulating_currency() -> None:
    assert SalaryBody.model_validate({"salary": {"amount": "50000.00", "currency": "INR"}}).salary.to_money() == Money.of("50000.00", "INR")


def test_plain_money_in_accepts_fund_but_not_metals() -> None:
    assert MoneyIn.model_validate({"amount": "1.2345", "currency": "CLF"}).currency == "CLF"
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate({"amount": "1", "currency": "XAU"})
    assert errors(excinfo) == [("non_monetary_currency", ("currency",))]
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate({"amount": "1", "currency": 356})
    assert errors(excinfo) == [("invalid_currency_code", ("currency",))]


@pytest.mark.parametrize("code", ["HRK", "BGN", "VEF", "ZWL"])
def test_ec_mon_14_historic_code_not_selectable_in_money_in(code: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate({"amount": "1.00", "currency": code})
    assert errors(excinfo) == [("historic_currency", ("currency",))]
    assert excinfo.value.errors()[0]["ctx"]["code"] == "HISTORIC_CURRENCY"


def test_ec_mon_14_historic_code_readable_through_explicit_import_model() -> None:
    legacy_in = money_in_model("LegacyMoneyIn", allow_historic=True)
    assert legacy_in.model_validate({"amount": "1.00", "currency": "HRK"}).to_money() == Money.of("1.00", "HRK")
    assert MoneyOut.from_money(Money.of("7.5", "HRK")).amount == "7.50"  # old records still serialise
    with pytest.raises(ValidationError):
        money_in_model("LegacyPayIn", allow_historic=True, payroll_currency_only=True).model_validate({"amount": "1.00", "currency": "HRK"})


def test_money_in_model_factory() -> None:
    salary_in = money_in_model("SalaryIn", allow_negative=False, payroll_currency_only=True)
    assert issubclass(salary_in, MoneyIn) and salary_in.__name__ == "SalaryIn"
    with pytest.raises(ValidationError) as excinfo:
        salary_in.model_validate({"amount": "-0.01", "currency": "INR"})
    assert errors(excinfo) == [("amount_negative", ("amount",))]
    with pytest.raises(ValidationError):
        salary_in.model_validate({"amount": "1", "currency": "CLF"})
    assert salary_in.model_validate({"amount": "0", "currency": "INR"}).decimal == 0
    assert MoneyIn.model_validate({"amount": "-0.01", "currency": "INR"}).decimal == D("-0.01")


# --- strictness -------------------------------------------------------------------------------------


def test_strict_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MoneyIn.model_validate({"amount": "1.00", "currency": "INR", "company_id": 2})
    assert errors(excinfo) == [("extra_forbidden", ("company_id",))]
    with pytest.raises(ValidationError) as excinfo:
        RateBody.model_validate({"rate": "1", "approved_by": 3})
    assert errors(excinfo) == [("extra_forbidden", ("approved_by",))]
    assert StrictModel.model_config.get("extra") == "forbid"


def test_money_in_json_schema_uses_strings() -> None:
    schema = MoneyIn.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["amount"]["type"] == "string"
    assert schema["properties"]["currency"]["pattern"] == "^[A-Z]{3}$"


# --- MoneyOut ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("amount, currency, expected", [("1500", "JPY", "1500"), ("12.345", "KWD", "12.345"), ("12.3", "KWD", "12.300"), ("12.3", "INR", "12.30"), ("0", "INR", "0.00"), ("-0.00", "INR", "0.00")])
def test_money_out_has_exact_minor_unit_digits(amount: str, currency: str, expected: str) -> None:
    out = MoneyOut.from_money(Money.of(amount, currency))
    assert out.amount == expected
    assert json.loads(out.model_dump_json()) == {"amount": expected, "currency": currency}
    assert MoneyOut.model_validate({"amount": amount, "currency": currency}).amount == expected
    assert out.to_money() == Money.of(amount, currency)


def test_money_out_never_rounds_silently() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MoneyOut.from_money(Money.of("46666.6666", "INR"))
    assert errors(excinfo) == [("amount_not_rounded", ())]
    with pytest.raises(ValidationError) as excinfo:
        MoneyOut.model_validate({"amount": 12.5, "currency": "INR"})
    assert errors(excinfo) == [("invalid_decimal", ())]
    with pytest.raises(ValidationError):
        MoneyOut.model_validate({"amount": "1.00", "currency": "INR", "extra": 1})
    with pytest.raises(ValidationError):
        MoneyOut.model_validate({"amount": "1", "currency": "XAU"})


def test_money_out_is_immutable() -> None:
    out = MoneyOut.from_money(Money.of("1.00", "USD"))
    with pytest.raises(ValidationError):
        out.amount = "2.00"  # type: ignore[misc]


# --- properties -------------------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    st.decimals(min_value=D("-1e12"), max_value=D("1e12"), places=3, allow_nan=False, allow_infinity=False),
    st.sampled_from(["INR", "JPY", "KWD", "USD"]),
)
def test_property_money_round_trips_through_api_types(value: Decimal, currency: str) -> None:
    original = Money.of(value, currency).round("HALF_EVEN")
    wire = MoneyOut.from_money(original).model_dump_json()
    parsed = PayMoneyIn.model_validate_json(wire).to_money()
    assert parsed == original
    assert MoneyOut.from_money(parsed).model_dump_json() == wire
