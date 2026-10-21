"""Pure tests for app.payroll.fx (3.2 §4, 3.1 §5.13, EC-MON-04/05/06/07/08/09/17/19)."""

from __future__ import annotations

import dataclasses
from fractions import Fraction
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payroll import fx
from app.payroll.fx import Conversion, FxError, Rate, RateUnavailable
from app.payroll.money import Money, MoneyError, allocate_percentages, quantize_amount

D = Decimal
DAY = date(2026, 10, 20)


def rate(base: str, quote: str, value: str, *, as_of: date = DAY, rate_type: str = "MID", source: str = "ECB", status: str = "ACTIVE", **extra: object) -> Rate:
    return Rate(base, quote, D(value), rate_type, source, as_of, status=status, **extra)  # type: ignore[arg-type]


def code_of(excinfo: pytest.ExceptionInfo[BaseException]) -> str:
    return getattr(excinfo.value, "code")


USD_INR = rate("USD", "INR", "83.912345678901", fetched_at=datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc), rate_id=7)


# --- golden vectors -------------------------------------------------------------------------------


def test_golden_3_2_s3_2_rate_with_twelve_decimals_converts_and_records_basis() -> None:
    """3.2 §3.2: 1 USD = 83.912345678901 INR (NUMERIC(24,12)); 3.2 §4.3 conversion record."""
    assert fx.validate_rate(USD_INR) is USD_INR
    conversion = fx.convert(Money.of("1000.00", "USD"), USD_INR, "HALF_UP", purpose="CONTRACT_TO_PAY", conversion_date=DAY, max_age_hours=24)
    assert conversion.result.to_json() == {"amount": "83912.35", "currency": "INR"}
    assert conversion.unrounded == D("83912.345678901000")
    assert conversion.quotation == "1 USD = 83.912345678901 INR"
    assert fx.QUOTATION_BASIS == "1 base = rate quote"
    assert conversion.to_record() == {
        "source_amount": "1000.00",
        "source_currency": "USD",
        "target_currency": "INR",
        "purpose": "CONTRACT_TO_PAY",
        "fx_rate_id": 7,
        "rate": "83.912345678901",
        "quotation": "1 USD = 83.912345678901 INR",
        "rate_date": "2026-10-20",
        "rounding_mode": "HALF_UP",
        "result_amount": "83912.35",
    }


def test_settings_defaults_3_1_s5_13() -> None:
    assert fx.DEFAULT_MAX_RATE_AGE_HOURS == 24
    assert fx.DEFAULT_ANOMALY_THRESHOLD_PERCENT == D("5.0")
    assert fx.CONVERSION_DATE_RULES == ("PERIOD_END", "PAY_DATE", "CALCULATION_DATE")
    assert fx.RATE_TYPES == ("MID", "BUY", "SELL")


# --- EC-MON-05: missing rate ---------------------------------------------------------------------


def test_ec_mon_05_missing_rate_blocks_conversion() -> None:
    with pytest.raises(RateUnavailable) as excinfo:
        fx.convert(Money.of("100.00", "USD"), None, target="INR")
    assert code_of(excinfo) == "RATE_UNAVAILABLE" and excinfo.value.base == "USD" and excinfo.value.quote == "INR"
    with pytest.raises(RateUnavailable):
        fx.find_rate([], "USD", "INR", on=DAY)
    with pytest.raises(RateUnavailable):
        fx.validate_rate(None, expected_pair=("USD", "INR"))


def test_ec_mon_05_missing_rate_reported_as_data_never_one_to_one() -> None:
    found = fx.lookup_rate([rate("EUR", "INR", "90")], "USD", "INR", on=DAY)
    assert not found.ok and found.rate is None
    assert found.issue is not None and found.issue.code == "RATE_UNAVAILABLE"
    assert found.issue.to_dict()["base_currency"] == "USD"
    with pytest.raises(FxError) as excinfo:
        fx.lookup_rate([], "INR", "INR", on=DAY)  # same currency never gets a 1:1 rate
    assert code_of(excinfo) == "SAME_CURRENCY"


def test_ec_mon_05_existing_rate_is_found() -> None:
    found = fx.lookup_rate([USD_INR], "USD", "INR", on=DAY)
    assert found.ok and found.rate is USD_INR
    assert fx.find_rate([USD_INR], "USD", "INR", on=DAY) is USD_INR


# --- EC-MON-06: stale rate -----------------------------------------------------------------------


def test_ec_mon_06_rate_at_max_age_is_fresh() -> None:
    yesterday = rate("USD", "INR", "83.5", as_of=DAY - timedelta(days=1))
    assert fx.rate_age_hours(yesterday, DAY) == 24
    assert not fx.is_stale(yesterday, DAY, 24)
    assert not fx.is_stale(rate("USD", "INR", "83.5"), DAY, 0)
    assert fx.convert(Money.of("1.00", "USD"), yesterday, conversion_date=DAY, max_age_hours=24).result.amount == D("83.50")


def test_ec_mon_06_stale_rate_blocks_conversion() -> None:
    old = rate("USD", "INR", "83.5", as_of=DAY - timedelta(days=2))
    assert fx.is_stale(old, DAY, 24)
    with pytest.raises(FxError) as excinfo:
        fx.convert(Money.of("1.00", "USD"), old, conversion_date=DAY, max_age_hours=24)
    assert code_of(excinfo) == "RATE_STALE"
    found = fx.lookup_rate([old], "USD", "INR", on=DAY, max_age_hours=24)
    assert found.issue is not None and found.issue.code == "RATE_STALE" and found.issue.rate is old
    with pytest.raises(FxError) as excinfo:
        fx.find_rate([old], "USD", "INR", on=DAY, max_age_hours=24)
    assert code_of(excinfo) == "RATE_STALE"


def test_ec_mon_06_invalid_max_age_and_missing_date() -> None:
    for bad in (-1, True, "24", D("24")):
        with pytest.raises(FxError) as excinfo:
            fx.is_stale(USD_INR, DAY, bad)  # type: ignore[arg-type]
        assert code_of(excinfo) == "INVALID_SETTING"
    with pytest.raises(FxError) as excinfo:
        fx.convert(Money.of("1.00", "USD"), USD_INR, max_age_hours=24)
    assert code_of(excinfo) == "CONVERSION_DATE_REQUIRED"
    with pytest.raises(FxError) as excinfo:
        fx.is_stale(USD_INR, datetime(2026, 10, 20, tzinfo=timezone.utc), 24)  # type: ignore[arg-type]
    assert code_of(excinfo) == "INVALID_DATE"


def test_rate_dated_after_conversion_date_is_not_used() -> None:
    tomorrow = rate("USD", "INR", "83.5", as_of=DAY + timedelta(days=1))
    with pytest.raises(FxError) as excinfo:
        fx.convert(Money.of("1.00", "USD"), tomorrow, conversion_date=DAY)
    assert code_of(excinfo) == "RATE_AFTER_CONVERSION_DATE"
    assert fx.lookup_rate([tomorrow], "USD", "INR", on=DAY).issue.code == "RATE_UNAVAILABLE"  # type: ignore[union-attr]


# --- EC-MON-07: zero, negative, inverted -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad, error",
    [
        (rate("USD", "INR", "0"), "RATE_NOT_POSITIVE"),
        (rate("USD", "INR", "-83.5"), "RATE_NOT_POSITIVE"),
        (rate("INR", "INR", "1"), "SAME_CURRENCY"),
        (rate("USD", "INR", "83.1234567890123"), "RATE_PRECISION_EXCEEDED"),
        (rate("USD", "INR", "1000000000000"), "RATE_OUT_OF_RANGE"),
        (rate("USD", "INR", "83.5", rate_type="SPOT"), "INVALID_RATE_TYPE"),
        (rate("USD", "INR", "83.5", status="DRAFT"), "INVALID_RATE_STATUS"),
        (rate("USD", "INR", "83.5", source=" "), "INVALID_RATE_SOURCE"),
        (rate("USD", "INR", "83.5", source="DERIVED"), "INVALID_RATE_SOURCE"),
        (rate("USD", "INR", "83.5", fetched_at=datetime(2026, 10, 20, 16, 0)), "NAIVE_TIMESTAMP"),
        (rate("USD", "XAU", "0.0004"), "NON_MONETARY_CURRENCY"),
    ],
)
def test_ec_mon_07_invalid_rates_rejected(bad: Rate, error: str) -> None:
    with pytest.raises(MoneyError) as excinfo:
        fx.validate_rate(bad)
    assert code_of(excinfo) == error
    with pytest.raises(MoneyError):
        fx.convert(Money.of("1.00", bad.base), bad)


def test_ec_mon_07_inverted_pair_rejected_not_silently_inverted() -> None:
    inr_usd = rate("INR", "USD", "0.0119")
    with pytest.raises(FxError) as excinfo:
        fx.validate_rate(inr_usd, expected_pair=("USD", "INR"))
    assert code_of(excinfo) == "INVERTED_PAIR"
    with pytest.raises(FxError) as excinfo:
        fx.convert(Money.of("100.00", "USD"), inr_usd)
    assert code_of(excinfo) == "INVERTED_PAIR"
    with pytest.raises(FxError) as excinfo:
        fx.validate_rate(USD_INR, expected_pair=("EUR", "INR"))
    assert code_of(excinfo) == "PAIR_MISMATCH"


def test_ec_mon_07_float_rate_rejected_at_construction() -> None:
    with pytest.raises(MoneyError) as excinfo:
        Rate("USD", "INR", 83.5, "MID", "ECB", DAY)  # type: ignore[arg-type]
    assert code_of(excinfo) == "FLOAT_NOT_ALLOWED"


def test_ec_mon_07_valid_small_and_large_rates_accepted() -> None:
    assert fx.validate_rate(rate("IRR", "USD", "0.000023752969")).rate == D("0.000023752969")
    assert fx.validate_rate(rate("USD", "VND", "25345.5")).rate == D("25345.5")
    assert fx.quantize_rate(D("1") / D("3")) == D("0.333333333333")
    with pytest.raises(FxError) as excinfo:
        fx.quantize_rate(D("0.0000000000001"))
    assert code_of(excinfo) == "RATE_UNDERFLOW"


# --- EC-MON-08: anomaly ----------------------------------------------------------------------------


def test_ec_mon_08_move_at_threshold_needs_no_approval() -> None:
    previous = rate("USD", "INR", "83.00", as_of=DAY - timedelta(days=1))
    assert fx.move_percent(previous, rate("USD", "INR", "87.15")) == D("5")
    assert not fx.needs_anomaly_approval(previous, rate("USD", "INR", "87.15"), D("5.0"))
    assert not fx.needs_anomaly_approval(previous, rate("USD", "INR", "78.85"), "5.0")
    assert not fx.needs_anomaly_approval(None, rate("USD", "INR", "500"), D("5.0"))


def test_ec_mon_08_move_above_threshold_needs_approval() -> None:
    previous = rate("USD", "INR", "83.00", as_of=DAY - timedelta(days=1))
    new = rate("USD", "INR", "87.16")
    assert fx.needs_anomaly_approval(previous, new, D("5.0"))
    assessment = fx.assess_move(previous, new, D("5.0"))
    assert assessment.needs_approval and not assessment.possibly_inverted
    assert fx.initial_status(new, previous=previous, manual_rate_requires_approval=False, anomaly_threshold_percent=D("5.0")) == "PENDING_APPROVAL"
    assert fx.approval_reasons(new, previous=previous, manual_rate_requires_approval=False, anomaly_threshold_percent=D("5.0")) == ("ANOMALY",)
    pending = dataclasses.replace(new, status="PENDING_APPROVAL")
    with pytest.raises(FxError) as excinfo:
        fx.convert(Money.of("1.00", "USD"), pending)
    assert code_of(excinfo) == "RATE_NOT_APPROVED"
    assert fx.lookup_rate([pending], "USD", "INR", on=DAY).issue.code == "RATE_NOT_APPROVED"  # type: ignore[union-attr]


def test_ec_mon_08_quoted_the_wrong_way_round_is_flagged() -> None:
    previous = rate("USD", "INR", "83.00", as_of=DAY - timedelta(days=1))
    flipped = rate("USD", "INR", "0.012048192771")
    assert fx.assess_move(previous, flipped, D("5.0")).possibly_inverted


def test_ec_mon_08_comparison_errors() -> None:
    previous = rate("USD", "INR", "83.00", as_of=DAY - timedelta(days=1))
    with pytest.raises(FxError) as excinfo:
        fx.assess_move(previous, rate("USD", "INR", "83.1", rate_type="BUY"), D("5"))
    assert code_of(excinfo) == "PAIR_MISMATCH"
    with pytest.raises(FxError) as excinfo:
        fx.assess_move(rate("USD", "INR", "83"), rate("USD", "INR", "83", as_of=DAY - timedelta(days=1)), D("5"))
    assert code_of(excinfo) == "PREVIOUS_RATE_NEWER"
    with pytest.raises(FxError) as excinfo:
        fx.needs_anomaly_approval(previous, rate("USD", "INR", "83.1"), D("-1"))
    assert code_of(excinfo) == "INVALID_SETTING"
    with pytest.raises(MoneyError):
        fx.needs_anomaly_approval(previous, rate("USD", "INR", "83.1"), 5.0)


def test_manual_rate_approval_and_maker_checker() -> None:
    manual = rate("USD", "AED", "3.6725", source="MANUAL")
    assert fx.initial_status(manual, previous=None, manual_rate_requires_approval=True, anomaly_threshold_percent=D("5")) == "PENDING_APPROVAL"
    assert fx.initial_status(manual, previous=None, manual_rate_requires_approval=False, anomaly_threshold_percent=D("5")) == "ACTIVE"
    fx.check_approver(maker=11, approver=12)
    for maker, approver, error in ((11, 11, "MAKER_CANNOT_APPROVE"), (11, None, "APPROVER_REQUIRED")):
        with pytest.raises(FxError) as excinfo:
            fx.check_approver(maker=maker, approver=approver)
        assert code_of(excinfo) == error


# --- inverse and cross -----------------------------------------------------------------------------


def test_inverse_records_its_leg() -> None:
    derived = fx.inverse(USD_INR)
    assert (derived.base, derived.quote, derived.rate) == ("INR", "USD", D("0.011917197546"))
    assert derived.source == fx.DERIVED_SOURCE and derived.derived_from == (USD_INR,)
    record = derived.to_record()["derived_from"]
    assert record["method"] == "INVERSE" and record["legs"][0]["rate"] == "83.912345678901"
    assert record["unrounded"].startswith("0.0119171975459558")
    assert fx.validate_rate(derived) is derived


def test_inverse_only_from_mid_rates() -> None:
    with pytest.raises(FxError) as excinfo:
        fx.inverse(rate("USD", "INR", "83.5", rate_type="BUY"))
    assert code_of(excinfo) == "DERIVATION_REQUIRES_MID"


def test_cross_through_pivot_records_both_legs() -> None:
    eur_usd = rate("EUR", "USD", "1.08")
    usd_inr = rate("USD", "INR", "83.5", as_of=DAY - timedelta(days=1))
    derived = fx.cross(eur_usd, usd_inr, "USD")
    assert (derived.base, derived.quote, derived.rate) == ("EUR", "INR", D("90.180000000000"))
    assert derived.derived_from == (eur_usd, usd_inr) and derived.as_of == DAY - timedelta(days=1)
    assert derived.to_record()["derived_from"]["pivot"] == "USD"
    inr_usd = rate("INR", "USD", "0.012")
    assert fx.cross(eur_usd, inr_usd, "USD").rate == D("90.000000000000")
    assert fx.cross(inr_usd, eur_usd, "USD").rate == D("0.011111111111")


def test_cross_errors_and_status_propagation() -> None:
    eur_usd = rate("EUR", "USD", "1.08")
    with pytest.raises(FxError) as excinfo:
        fx.cross(eur_usd, rate("GBP", "INR", "105"), "USD")
    assert code_of(excinfo) == "PIVOT_NOT_IN_LEG"
    with pytest.raises(FxError) as excinfo:
        fx.cross(eur_usd, rate("USD", "EUR", "0.92"), "USD")
    assert code_of(excinfo) == "SAME_CURRENCY"
    pending = fx.cross(eur_usd, rate("USD", "INR", "83.5", status="PENDING_APPROVAL"), "USD")
    assert pending.status == "PENDING_APPROVAL"
    with pytest.raises(FxError):
        fx.convert(Money.of("1.00", "EUR"), pending)


# --- conversions (EC-MON-01/02/09/17/19) -----------------------------------------------------------


def test_ec_mon_01_conversion_into_jpy_has_no_decimals() -> None:
    result = fx.convert(Money.of("100.00", "USD"), rate("USD", "JPY", "151.234")).result
    assert result.to_json() == {"amount": "15123", "currency": "JPY"}


def test_ec_mon_02_conversion_into_kwd_has_three_decimals() -> None:
    result = fx.convert(Money.of("1000.00", "USD"), rate("USD", "KWD", "0.3067894")).result
    assert result.to_json() == {"amount": "306.789", "currency": "KWD"}
    down = fx.convert(Money.of("1000.00", "USD"), rate("USD", "KWD", "0.3067899"), "DOWN").result
    assert down.to_json()["amount"] == "306.789"


def test_conversion_validates_inputs() -> None:
    for kwargs, error in (({"mode": "ROUND"}, "UNKNOWN_ROUNDING_MODE"), ({"purpose": "PAYROLL"}, "INVALID_PURPOSE"), ({"target": "EUR"}, "PAIR_MISMATCH")):
        with pytest.raises(MoneyError) as excinfo:
            fx.convert(Money.of("1.00", "USD"), USD_INR, **kwargs)  # type: ignore[arg-type]
        assert code_of(excinfo) == error
    with pytest.raises(FxError) as excinfo:
        fx.convert(Money.of("1.00", "EUR"), USD_INR)
    assert code_of(excinfo) == "PAIR_MISMATCH"
    with pytest.raises(MoneyError):
        fx.convert(D("1.00"), USD_INR)  # type: ignore[arg-type]
    with pytest.raises(FxError) as excinfo:
        fx.convert(Money.of("1.00", "USD"), USD_INR).to_record()
    assert code_of(excinfo) == "PURPOSE_REQUIRED"


def test_ec_mon_19_each_conversion_stored_with_purpose_rate_basis_and_date() -> None:
    """Contract in USD, payroll in EUR, payment in GBP, reporting in INR."""
    contract = Money.of("5000.00", "USD")
    to_pay = fx.convert(contract, rate("USD", "EUR", "0.92"), purpose="CONTRACT_TO_PAY", conversion_date=DAY)
    to_payment = fx.convert(to_pay.result, rate("EUR", "GBP", "0.86"), purpose="PAY_TO_PAYMENT", conversion_date=DAY)
    to_report = fx.convert(to_pay.result, rate("EUR", "INR", "90.5", source="RBI"), purpose="REPORTING", conversion_date=DAY)
    records = [item.to_record() for item in (to_pay, to_payment, to_report)]
    assert [(r["source_currency"], r["target_currency"], r["result_amount"]) for r in records] == [
        ("USD", "EUR", "4600.00"),
        ("EUR", "GBP", "3956.00"),
        ("EUR", "INR", "416300.00"),
    ]
    assert [r["purpose"] for r in records] == ["CONTRACT_TO_PAY", "PAY_TO_PAYMENT", "REPORTING"]
    assert all(r["quotation"].startswith("1 ") and r["rate_date"] == "2026-10-20" and r["rounding_mode"] == "HALF_UP" for r in records)
    assert to_report.trace()["rate_source"] == "RBI"


def test_ec_mon_09_finalized_conversion_is_frozen_and_correction_is_explicit_retro() -> None:
    finalized = fx.convert(Money.of("1000.00", "USD"), rate("USD", "INR", "83.50"), purpose="CONTRACT_TO_PAY")
    with pytest.raises(dataclasses.FrozenInstanceError):
        finalized.result = Money.of("0", "INR")  # type: ignore[misc]
    corrected = fx.convert(Money.of("1000.00", "USD"), rate("USD", "INR", "83.75"), purpose="CONTRACT_TO_PAY")
    assert fx.retro_difference(finalized, corrected).to_json() == {"amount": "250.00", "currency": "INR"}
    assert finalized.result.to_json()["amount"] == "83500.00"


def test_ec_mon_09_retro_requires_matching_conversions() -> None:
    finalized = fx.convert(Money.of("1000.00", "USD"), rate("USD", "INR", "83.50"))
    for other in (
        fx.convert(Money.of("999.00", "USD"), rate("USD", "INR", "83.75")),
        fx.convert(Money.of("1000.00", "USD"), rate("USD", "EUR", "0.92")),
    ):
        with pytest.raises(FxError) as excinfo:
            fx.retro_difference(finalized, other)
        assert code_of(excinfo) == "CONVERSION_MISMATCH"


def test_ec_mon_17_split_payment_fx_snapshot_and_remainder() -> None:
    """30 % of an AED net goes to an INR account at the instruction-time rate; rounding stays with the remainder."""
    net = Money.of("10000.05", "AED")
    allocation = allocate_percentages(net, [D("30")])
    foreign = fx.convert(allocation.shares[0], rate("AED", "INR", "22.85"), purpose="PAY_TO_PAYMENT", conversion_date=DAY)
    assert allocation.shares[0].to_json()["amount"] == "3000.01"
    assert allocation.remainder.to_json()["amount"] == "7000.04"
    assert foreign.result.to_json() == {"amount": "68550.23", "currency": "INR"}
    assert foreign.to_record()["rate"] == "22.85"


# --- EC-MON-04: converted totals -------------------------------------------------------------------


def test_ec_mon_04_converted_total_is_labelled_with_source_rate_and_date() -> None:
    amounts = [Money.of("1000.00", "INR"), Money.of("10.00", "USD"), Money.of("5.00", "USD"), Money.of("1500", "JPY")]
    rates = [rate("INR", "USD", "0.012"), rate("JPY", "USD", "0.0067", source="BOJ")]
    total = fx.converted_total(amounts, "USD", rates, on=DAY)
    assert total.total.to_json() == {"amount": "37.05", "currency": "USD"}
    assert total.unconverted == Money.of("15.00", "USD")
    assert total.label == "Converted to USD: 1 INR = 0.012 USD (ECB, 2026-10-20); 1 JPY = 0.0067 USD (BOJ, 2026-10-20)"
    assert [item.purpose for item in total.conversions] == ["REPORTING", "REPORTING"]
    assert total.to_dict()["total"]["amount"] == "37.05"


def test_ec_mon_04_converted_total_never_drops_a_currency() -> None:
    with pytest.raises(RateUnavailable):
        fx.converted_total([Money.of("1.00", "INR"), Money.of("1", "JPY")], "USD", [rate("INR", "USD", "0.012")], on=DAY)
    same = fx.converted_total([Money.of("1.00", "USD")], "USD", [], on=DAY)
    assert same.total == Money.of("1.00", "USD") and same.label == "All amounts in USD"


# --- lookup and date rules ---------------------------------------------------------------------------


def test_lookup_picks_latest_approved_rate_not_after_date() -> None:
    older = rate("USD", "INR", "83.1", as_of=DAY - timedelta(days=3))
    latest = rate("USD", "INR", "83.4", as_of=DAY - timedelta(days=1))
    future = rate("USD", "INR", "83.9", as_of=DAY + timedelta(days=1))
    rejected = rate("USD", "INR", "99", status="REJECTED")
    sell = rate("USD", "INR", "84.0", rate_type="SELL")
    assert fx.find_rate([older, latest, future, rejected, sell], "USD", "INR", on=DAY) is latest
    assert fx.find_rate([older, latest, sell], "USD", "INR", on=DAY, rate_type="SELL") is sell


def test_lookup_reports_ambiguous_and_invalid_rates() -> None:
    first = rate("USD", "INR", "83.1")
    second = rate("USD", "INR", "83.2")
    assert fx.lookup_rate([first, second], "USD", "INR", on=DAY).issue.code == "RATE_AMBIGUOUS"  # type: ignore[union-attr]
    later = dataclasses.replace(second, fetched_at=datetime(2026, 10, 20, 17, tzinfo=timezone.utc))
    assert fx.find_rate([first, later], "USD", "INR", on=DAY) is later
    assert fx.lookup_rate([rate("USD", "INR", "0")], "USD", "INR", on=DAY).issue.code == "RATE_INVALID"  # type: ignore[union-attr]
    with pytest.raises(FxError) as excinfo:
        fx.lookup_rate([], "USD", "INR", on=DAY, rate_type="SPOT")
    assert code_of(excinfo) == "INVALID_RATE_TYPE"


@pytest.mark.parametrize("rule, expected", [("PERIOD_END", date(2026, 10, 31)), ("PAY_DATE", date(2026, 11, 1)), ("CALCULATION_DATE", date(2026, 10, 28))])
def test_conversion_date_rule(rule: str, expected: date) -> None:
    assert fx.resolve_conversion_date(rule, period_end=date(2026, 10, 31), pay_date=date(2026, 11, 1), calculation_date=date(2026, 10, 28)) == expected


def test_conversion_date_rule_errors() -> None:
    with pytest.raises(FxError) as excinfo:
        fx.resolve_conversion_date("TODAY", pay_date=DAY)
    assert code_of(excinfo) == "UNKNOWN_DATE_RULE"
    with pytest.raises(FxError) as excinfo:
        fx.resolve_conversion_date("PAY_DATE", period_end=DAY)
    assert code_of(excinfo) == "CONVERSION_DATE_MISSING"


# --- properties ------------------------------------------------------------------------------------

RATES = st.decimals(min_value=D("0.000001"), max_value=D("100000"), places=6, allow_nan=False, allow_infinity=False)
AMOUNTS = st.decimals(min_value=D("-1e9"), max_value=D("1e9"), places=2, allow_nan=False, allow_infinity=False)


@settings(max_examples=200, deadline=None)
@given(AMOUNTS, RATES, st.sampled_from(["HALF_UP", "HALF_EVEN", "DOWN", "UP"]))
def test_property_conversion_is_rate_times_amount_rounded_once(amount: Decimal, value: Decimal, mode: str) -> None:
    money = Money.of(format(amount, "f"), "USD")
    conversion: Conversion = fx.convert(money, rate("USD", "KWD", format(value, "f")), mode)
    assert conversion.unrounded == amount * value
    assert conversion.result.amount == quantize_amount(amount * value, "KWD", mode)
    assert conversion.result.to_json()["amount"].count(".") == 1 and len(conversion.result.to_json()["amount"].split(".")[1]) == 3


@settings(max_examples=200, deadline=None)
@given(RATES)
def test_property_inverse_times_rate_is_one_within_stored_precision(value: Decimal) -> None:
    original = rate("USD", "INR", format(value, "f"))
    try:
        derived = fx.inverse(original)
    except FxError as exc:
        assert exc.code in {"RATE_UNDERFLOW", "RATE_OUT_OF_RANGE"}
        return
    assert abs(derived.rate * original.rate - 1) <= original.rate * D("0.5e-12")


# --- review additions: exactness, no "last known" fallback ---------------------------------------


def test_golden_conversion_rounds_the_exact_product_once() -> None:
    """3.2 §3.3/§4.3: 1207670473444.95 USD x 83.912345678901 = 101338462233914.67499999999995 exactly.

    Rounding the product to 28 significant digits first would give ...914.675 and then ...914.68 (double rounding).
    """
    conversion = fx.convert(Money.of("1207670473444.95", "USD"), USD_INR, "HALF_UP", purpose="CONTRACT_TO_PAY")
    assert conversion.unrounded == D("101338462233914.67499999999995")
    assert conversion.result.to_json() == {"amount": "101338462233914.67", "currency": "INR"}
    assert conversion.trace()["unrounded"] == "101338462233914.67499999999995"
    assert fx.convert(Money.of("1207670473444.95", "USD"), USD_INR, "UP").result.amount == D("101338462233914.68")


def test_ec_mon_05_newest_rate_pending_blocks_instead_of_last_known() -> None:
    """3.2 §4.5: an unapproved latest rate blocks; yesterday's approved rate is never used silently."""
    yesterday = rate("USD", "INR", "83.1", as_of=DAY - timedelta(days=1))
    pending = rate("USD", "INR", "90", status="PENDING_APPROVAL")
    found = fx.lookup_rate([yesterday, pending], "USD", "INR", on=DAY)
    assert found.rate is None and found.issue is not None
    assert found.issue.code == "RATE_NOT_APPROVED" and found.issue.rate is pending
    with pytest.raises(FxError) as excinfo:
        fx.find_rate([yesterday, pending], "USD", "INR", on=DAY)
    assert code_of(excinfo) == "RATE_NOT_APPROVED"


def test_ec_mon_05_newest_rate_invalid_blocks_instead_of_last_known() -> None:
    yesterday = rate("USD", "INR", "83.1", as_of=DAY - timedelta(days=1))
    broken = rate("USD", "INR", "0")
    found = fx.lookup_rate([yesterday, broken], "USD", "INR", on=DAY)
    assert found.issue is not None and found.issue.code == "RATE_INVALID" and found.issue.rate is broken


def test_ec_mon_05_rejected_rate_is_ignored_and_approved_rate_used() -> None:
    """Boundary: a rejected rate is not a rate; the latest approved one remains usable (subject to staleness)."""
    yesterday = rate("USD", "INR", "83.1", as_of=DAY - timedelta(days=1))
    rejected = rate("USD", "INR", "90", status="REJECTED")
    assert fx.find_rate([yesterday, rejected], "USD", "INR", on=DAY, max_age_hours=24) is yesterday
    assert fx.lookup_rate([rejected], "USD", "INR", on=DAY).issue.code == "RATE_UNAVAILABLE"  # type: ignore[union-attr]


def test_ec_mon_05_later_approved_rate_on_same_day_supersedes_pending() -> None:
    pending = rate("USD", "INR", "90", status="PENDING_APPROVAL", fetched_at=datetime(2026, 10, 20, 9, tzinfo=timezone.utc))
    approved = rate("USD", "INR", "83.4", fetched_at=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    assert fx.find_rate([pending, approved], "USD", "INR", on=DAY) is approved


def test_ec_mon_08_threshold_comparison_is_exact() -> None:
    """A move of 5.0000000000333... % exceeds a threshold of 5.000000000033333333333333333 %, although both
    agree to 28 significant digits."""
    previous = rate("USD", "AED", "3", as_of=DAY - timedelta(days=1))
    new = rate("USD", "AED", "3.150000000001")
    assert fx.needs_anomaly_approval(previous, new, D("5.000000000033333333333333333"))
    assert not fx.needs_anomaly_approval(previous, new, D("5.000000000033333333333333334"))
    assert not fx.needs_anomaly_approval(previous, rate("USD", "AED", "3.15"), D("5"))


def test_conversion_record_requires_a_storable_result() -> None:
    """NUMERIC(19,4): a result that does not fit is never written as a conversion record (EC-MON-12)."""
    huge = fx.convert(Money.of("999999999999999", "JPY"), rate("JPY", "INR", "100"), purpose="REPORTING")
    with pytest.raises(MoneyError) as excinfo:
        huge.to_record()
    assert code_of(excinfo) == "AMOUNT_NOT_STORABLE"


def test_ec_mon_19_conversion_without_purpose_or_rate_cannot_be_recorded() -> None:
    """Negative: a conversion record always names its purpose, and a missing rate never becomes 1:1."""
    with pytest.raises(FxError) as excinfo:
        fx.convert(Money.of("5000.00", "USD"), rate("USD", "EUR", "0.92")).to_record()
    assert code_of(excinfo) == "PURPOSE_REQUIRED"
    with pytest.raises(RateUnavailable):
        fx.convert(Money.of("5000.00", "USD"), None, purpose="CONTRACT_TO_PAY", target="EUR")


def _correctly_rounded(value: Fraction) -> Decimal:
    scaled = value * 10**12
    return D(int(scaled + Fraction(1, 2))).scaleb(-12)  # half-up for a positive value


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=1, max_value=10**18))
def test_property_inverse_is_correctly_rounded(units: int) -> None:
    value = D(units).scaleb(-12)
    try:
        derived = fx.inverse(rate("USD", "INR", format(value, "f")))
    except FxError as exc:
        assert exc.code in {"RATE_UNDERFLOW", "RATE_OUT_OF_RANGE"}
        return
    assert derived.rate == _correctly_rounded(1 / Fraction(units, 10**12))


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=10**6, max_value=10**16), st.integers(min_value=10**6, max_value=10**16), st.booleans(), st.booleans())
def test_property_cross_is_correctly_rounded(a: int, b: int, a_direct: bool, b_direct: bool) -> None:
    first = rate("EUR", "USD", format(D(a).scaleb(-12), "f")) if a_direct else rate("USD", "EUR", format(D(a).scaleb(-12), "f"))
    second = rate("USD", "INR", format(D(b).scaleb(-12), "f")) if b_direct else rate("INR", "USD", format(D(b).scaleb(-12), "f"))
    leg_a = Fraction(a, 10**12) if a_direct else 1 / Fraction(a, 10**12)
    leg_b = Fraction(b, 10**12) if b_direct else 1 / Fraction(b, 10**12)
    try:
        derived = fx.cross(first, second, "USD")
    except FxError as exc:
        assert exc.code in {"RATE_UNDERFLOW", "RATE_OUT_OF_RANGE"}
        return
    assert (derived.base, derived.quote) == ("EUR", "INR")
    assert derived.rate == _correctly_rounded(leg_a * leg_b)
