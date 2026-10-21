"""Foreign exchange: rates, staleness, anomalies, derived rates and conversion records (3.2 §4).

The quotation basis is always "1 base = rate quote". A rate is valid when it
joins two different currencies that can hold amounts, is greater than zero and
fits NUMERIC(24,12) (at most 12 integer and 12 decimal digits); zero, negative,
same-currency and inverted pairs are rejected (EC-MON-07). It is usable for a
conversion only when it is ACTIVE (approved), dated on or before the
conversion date and not older than payroll.fx.max_rate_age_hours relative to
it (EC-MON-06). Ages are counted in whole calendar days of 24 hours between
the rate's as_of business date and the conversion date, so no clock is read.

Nothing here ever substitutes 1:1, zero or a "last known" rate: a missing rate
raises RateUnavailable (EC-MON-05), and lookup_rate reports missing, stale,
invalid, unapproved or ambiguous rates as data for run pre-checks and employee
exceptions; when the latest rate is unusable an older one is never used
instead. Conversions multiply exactly and round once to the target minor
unit. Inverse and cross rates are derived explicitly from MID rates, rounded
once to the 12 stored decimals and record their legs (derived_from). A
day-to-day move above payroll.fx.anomaly_threshold_percent needs approval
(EC-MON-08), as does a manual rate when payroll.fx.manual_rate_requires_approval
is on; the maker cannot approve. Each conversion returns a frozen Conversion
record (original amount, target currency, rate, quotation, rate date, rounding
mode, result, purpose) whose to_record() matches the fx_conversions columns
(EC-MON-19). Records are never recomputed with a newer rate: a correction
converts again and retro_difference gives the explicit retro amount
(EC-MON-09). Amounts in several currencies are only ever totalled per
currency, or through converted_total, whose result is labelled with every
rate's source and date (EC-MON-04).
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_05UP, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

from app.payroll import currencies
from app.payroll.money import (
    CALC_CONTEXT,
    ROUNDING_MODES,
    Money,
    amount_str,
    decimal_str,
    ensure_storable,
    exact_product,
    quantize_exact,
    to_decimal,
    totals_by_currency,
)
from app.payroll.money_errors import MoneyError

__all__ = [
    "CONVERSION_DATE_RULES",
    "CONVERSION_PURPOSES",
    "Conversion",
    "ConvertedTotal",
    "DEFAULT_ANOMALY_THRESHOLD_PERCENT",
    "DEFAULT_MAX_RATE_AGE_HOURS",
    "DERIVED_SOURCE",
    "Derivation",
    "FxError",
    "FxIssue",
    "MANUAL_SOURCE",
    "QUOTATION_BASIS",
    "RATE_SCALE",
    "RATE_STATUSES",
    "RATE_TYPES",
    "Rate",
    "RateLookup",
    "RateMove",
    "RateUnavailable",
    "approval_reasons",
    "assess_move",
    "check_approver",
    "convert",
    "converted_total",
    "cross",
    "find_rate",
    "initial_status",
    "inverse",
    "is_stale",
    "lookup_rate",
    "move_percent",
    "needs_anomaly_approval",
    "quantize_rate",
    "rate_age_hours",
    "resolve_conversion_date",
    "retro_difference",
    "validate_rate",
]

RATE_TYPES: tuple[str, ...] = ("MID", "BUY", "SELL")
RATE_STATUSES: tuple[str, ...] = ("ACTIVE", "PENDING_APPROVAL", "REJECTED")
CONVERSION_PURPOSES: tuple[str, ...] = ("CONTRACT_TO_PAY", "PAY_TO_PAYMENT", "REPORTING", "ACCOUNTING", "REIMBURSEMENT")
CONVERSION_DATE_RULES: tuple[str, ...] = ("PERIOD_END", "PAY_DATE", "CALCULATION_DATE")
DERIVATION_METHODS: tuple[str, ...] = ("INVERSE", "CROSS")
MANUAL_SOURCE = "MANUAL"
DERIVED_SOURCE = "DERIVED"
QUOTATION_BASIS = "1 base = rate quote"
RATE_SCALE = 12  # NUMERIC(24,12)
_RATE_QUANTUM = Decimal((0, (1,), -RATE_SCALE))
_RATE_LIMIT = Decimal(10) ** (24 - RATE_SCALE)
DEFAULT_MAX_RATE_AGE_HOURS = 24  # registry defaults of 3.1 §5.13, for documentation and tests
DEFAULT_ANOMALY_THRESHOLD_PERCENT = Decimal("5.0")
_ACTIVE, _PENDING, _REJECTED = RATE_STATUSES
_WIDE_PREC = 60
# Exact sums and products of stored rates (at most 24 digits each), and quotients rounded to odd at 60 digits:
# round-to-odd keeps enough information for the single later rounding to 12 decimals to be correct.
_WIDE_EXACT = decimal.Context(
    prec=_WIDE_PREC, traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow, decimal.Inexact]
)
_ROUND_TO_ODD = decimal.Context(
    prec=_WIDE_PREC, rounding=ROUND_05UP, traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow]
)
_RATE_ROUNDING = decimal.Context(
    prec=_WIDE_PREC, rounding=ROUND_HALF_UP, traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow]
)


class FxError(MoneyError):
    """A rate is invalid, unusable for the conversion asked for, or the request itself is inconsistent."""


class RateUnavailable(FxError):
    """No usable rate exists for the pair; the conversion is blocked, never replaced by 1:1 (EC-MON-05)."""

    def __init__(self, base: str, quote: str | None, detail: str = "") -> None:
        pair = f"{base}/{quote}" if quote else base
        super().__init__("RATE_UNAVAILABLE", f"No usable FX rate for {pair}{': ' + detail if detail else ''}.")
        self.base = base
        self.quote = quote


@dataclass(frozen=True)
class Derivation:
    """How a derived rate was computed: its method, its legs and the value before rounding to 12 decimals
    (the quotient to 60 significant digits, rounded to odd so the one rounding to 12 decimals is exact)."""

    method: str  # INVERSE | CROSS
    legs: tuple[Rate, ...]
    unrounded: Decimal
    pivot: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "pivot": self.pivot,
            "unrounded": decimal_str(self.unrounded),
            "legs": [leg.to_record() for leg in self.legs],
        }


@dataclass(frozen=True)
class Rate:
    """1 ``base`` = ``rate`` ``quote`` on business date ``as_of``, of ``rate_type`` from ``source`` (MANUAL or a provider)."""

    base: str
    quote: str
    rate: Decimal
    rate_type: str
    source: str
    as_of: date
    fetched_at: datetime | None = None
    status: str = _ACTIVE
    rate_id: int | str | None = None
    derivation: Derivation | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "rate", to_decimal(self.rate))

    @property
    def pair(self) -> tuple[str, str]:
        return (self.base, self.quote)

    @property
    def quotation(self) -> str:
        return f"1 {self.base} = {decimal_str(self.rate)} {self.quote}"

    @property
    def derived_from(self) -> tuple[Rate, ...]:
        return () if self.derivation is None else self.derivation.legs

    def to_record(self) -> dict[str, Any]:
        """The fx_rates columns (3.9 §3.3) with JSON-safe values."""
        return {
            "fx_rate_id": self.rate_id,
            "base_currency": self.base,
            "quote_currency": self.quote,
            "rate": decimal_str(self.rate),
            "rate_type": self.rate_type,
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "fetched_at": None if self.fetched_at is None else self.fetched_at.isoformat(),
            "status": self.status,
            "derived_from": None if self.derivation is None else self.derivation.to_record(),
        }


def _require_date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise FxError("INVALID_DATE", f"{name} must be a date (business day), got {type(value).__name__}.")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FxError("INVALID_SETTING", f"{name} must be a whole number of hours of zero or more, got {value!r}.")
    return value


def _percent(value: object, name: str) -> Decimal:
    percent = to_decimal(value)
    if percent < 0:
        raise FxError("INVALID_SETTING", f"{name} cannot be negative.")
    return percent


def quantize_rate(value: Decimal) -> Decimal:
    """A rate rounded half-up to the 12 stored decimals; raises when it rounds to zero or exceeds 12 integer digits."""
    return _round_rate(to_decimal(value))


def _round_rate(value: Decimal) -> Decimal:
    try:
        rounded = value.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP, context=_RATE_ROUNDING)
    except InvalidOperation as exc:
        raise FxError("RATE_OUT_OF_RANGE", f"{decimal_str(value)} does not fit NUMERIC(24,{RATE_SCALE}).") from exc
    if rounded <= 0:
        raise FxError("RATE_UNDERFLOW", f"{decimal_str(value)} rounds to zero at {RATE_SCALE} decimals.")
    if rounded >= _RATE_LIMIT:
        raise FxError("RATE_OUT_OF_RANGE", f"{decimal_str(value)} does not fit NUMERIC(24,{RATE_SCALE}).")
    return rounded


def validate_rate(rate: Rate | None, *, expected_pair: tuple[str, str] | None = None) -> Rate:
    """``rate`` if it is a valid quotation (EC-MON-07); raises FxError (or RateUnavailable for None) otherwise.

    ``expected_pair`` (base, quote) additionally rejects a rate quoted the other way round (INVERTED_PAIR)
    or for another pair: a reversed rate is never silently inverted.
    """
    if rate is None:
        raise RateUnavailable(expected_pair[0] if expected_pair else "?", expected_pair[1] if expected_pair else None)
    if not isinstance(rate, Rate):
        raise FxError("INVALID_RATE", f"Expected a Rate, got {type(rate).__name__}.")
    currencies.require_money_currency(rate.base)
    currencies.require_money_currency(rate.quote)
    if rate.base == rate.quote:
        raise FxError(
            "SAME_CURRENCY",
            f"A rate needs two different currencies; {rate.base} amounts need no conversion and never get a 1:1 rate.",
        )
    if expected_pair is not None and rate.pair != expected_pair:
        if rate.pair == (expected_pair[1], expected_pair[0]):
            raise FxError(
                "INVERTED_PAIR",
                f"Expected a {expected_pair[0]}/{expected_pair[1]} rate but got {rate.base}/{rate.quote}; "
                "derive the inverse explicitly so it is recorded.",
            )
        raise FxError("PAIR_MISMATCH", f"Expected a {expected_pair[0]}/{expected_pair[1]} rate, got {rate.base}/{rate.quote}.")
    if rate.rate <= 0:
        raise FxError("RATE_NOT_POSITIVE", f"An FX rate must be greater than zero, got {decimal_str(rate.rate)}.")
    if rate.rate >= _RATE_LIMIT:
        raise FxError("RATE_OUT_OF_RANGE", f"{decimal_str(rate.rate)} does not fit NUMERIC(24,{RATE_SCALE}).")
    if rate.rate.quantize(_RATE_QUANTUM, context=CALC_CONTEXT) != rate.rate:
        raise FxError(
            "RATE_PRECISION_EXCEEDED",
            f"{decimal_str(rate.rate)} has more than {RATE_SCALE} decimals; round it explicitly with quantize_rate.",
        )
    if rate.rate_type not in RATE_TYPES:
        raise FxError("INVALID_RATE_TYPE", f"{rate.rate_type!r} is not one of {RATE_TYPES}.")
    if rate.status not in RATE_STATUSES:
        raise FxError("INVALID_RATE_STATUS", f"{rate.status!r} is not one of {RATE_STATUSES}.")
    if not isinstance(rate.source, str) or not rate.source.strip():
        raise FxError("INVALID_RATE_SOURCE", "A rate needs a source: MANUAL or a provider code.")
    if (rate.source == DERIVED_SOURCE) != (rate.derivation is not None):
        raise FxError("INVALID_RATE_SOURCE", "Only derived rates, and all of them, carry the DERIVED source and their legs.")
    _require_date(rate.as_of, "as_of")
    if rate.fetched_at is not None and (not isinstance(rate.fetched_at, datetime) or rate.fetched_at.utcoffset() is None):
        raise FxError("NAIVE_TIMESTAMP", "fetched_at must be a timezone-aware instant.")
    return rate


def rate_age_hours(rate: Rate, conversion_date: date) -> int:
    """Hours between the rate's business date and the conversion date (negative for a rate dated later)."""
    return (_require_date(conversion_date, "conversion_date") - _require_date(rate.as_of, "as_of")).days * 24


def is_stale(rate: Rate, conversion_date: date, max_age_hours: int) -> bool:
    """Whether the rate is older than payroll.fx.max_rate_age_hours relative to the conversion date (EC-MON-06)."""
    validate_rate(rate)
    return rate_age_hours(rate, conversion_date) > _non_negative_int(max_age_hours, "max_age_hours")


def move_percent(previous: Rate, new: Rate) -> Decimal:
    """The absolute move from ``previous`` to ``new`` in percent of ``previous``."""
    change = CALC_CONTEXT.subtract(new.rate, previous.rate).copy_abs()
    return CALC_CONTEXT.divide(CALC_CONTEXT.multiply(change, 100), previous.rate)


@dataclass(frozen=True)
class RateMove:
    previous: Rate
    new: Rate
    move_percent: Decimal
    threshold_percent: Decimal
    needs_approval: bool
    possibly_inverted: bool  # the new rate is close to 1 / previous: probably quoted the wrong way round


def assess_move(previous: Rate, new: Rate, threshold_percent: object) -> RateMove:
    """Compare a new rate with the previous one for the same pair and type (EC-MON-08)."""
    validate_rate(previous)
    validate_rate(new, expected_pair=previous.pair)
    if new.rate_type != previous.rate_type:
        raise FxError("PAIR_MISMATCH", f"Cannot compare a {new.rate_type} rate with a {previous.rate_type} rate.")
    if previous.as_of > new.as_of:
        raise FxError("PREVIOUS_RATE_NEWER", "The previous rate must not be dated after the new one.")
    threshold = _percent(threshold_percent, "anomaly_threshold_percent")
    # Exact comparisons, never a rounded percentage: |new - previous| * 100 > threshold * previous.
    change = _WIDE_EXACT.subtract(new.rate, previous.rate).copy_abs()
    above = _WIDE_EXACT.multiply(change, 100) > exact_product(threshold, previous.rate)
    product_gap = _WIDE_EXACT.subtract(exact_product(new.rate, previous.rate), 1).copy_abs()
    return RateMove(
        previous=previous,
        new=new,
        move_percent=move_percent(previous, new),
        threshold_percent=threshold,
        needs_approval=above,
        possibly_inverted=above and _WIDE_EXACT.multiply(product_gap, 100) <= threshold,
    )


def needs_anomaly_approval(previous: Rate | None, new: Rate, threshold_percent: object) -> bool:
    """Whether the day-to-day move exceeds payroll.fx.anomaly_threshold_percent; False without a previous rate."""
    if previous is None:
        validate_rate(new)
        _percent(threshold_percent, "anomaly_threshold_percent")
        return False
    return assess_move(previous, new, threshold_percent).needs_approval


def approval_reasons(
    rate: Rate,
    *,
    previous: Rate | None,
    manual_rate_requires_approval: bool,
    anomaly_threshold_percent: object,
) -> tuple[str, ...]:
    """Why a new rate must wait for approval: MANUAL_RATE and/or ANOMALY (3.2 §4.2, §4.5)."""
    validate_rate(rate)
    reasons: list[str] = []
    if rate.source == MANUAL_SOURCE and manual_rate_requires_approval:
        reasons.append("MANUAL_RATE")
    if needs_anomaly_approval(previous, rate, anomaly_threshold_percent):
        reasons.append("ANOMALY")
    return tuple(reasons)


def initial_status(
    rate: Rate,
    *,
    previous: Rate | None,
    manual_rate_requires_approval: bool,
    anomaly_threshold_percent: object,
) -> str:
    reasons = approval_reasons(
        rate,
        previous=previous,
        manual_rate_requires_approval=manual_rate_requires_approval,
        anomaly_threshold_percent=anomaly_threshold_percent,
    )
    return _PENDING if reasons else _ACTIVE


def check_approver(maker: object, approver: object) -> None:
    """The person who entered a rate cannot approve it."""
    if approver is None:
        raise FxError("APPROVER_REQUIRED", "An approval needs an identified approver.")
    if maker is not None and maker == approver:
        raise FxError("MAKER_CANNOT_APPROVE", "The person who entered this rate cannot approve it.")


def _combined_status(legs: Iterable[Rate]) -> str:
    statuses = {leg.status for leg in legs}
    if _REJECTED in statuses:
        return _REJECTED
    if _PENDING in statuses:
        return _PENDING
    return _ACTIVE


def _require_mid(*legs: Rate) -> None:
    for leg in legs:
        if leg.rate_type != "MID":
            raise FxError(
                "DERIVATION_REQUIRES_MID",
                f"Only MID rates are inverted or crossed; {leg.base}/{leg.quote} is a {leg.rate_type} rate.",
            )


def inverse(rate: Rate) -> Rate:
    """The quote/base rate derived from a MID rate, recorded with its leg (1 INR = 1/83.9… USD)."""
    validate_rate(rate)
    _require_mid(rate)
    exact = _ROUND_TO_ODD.divide(Decimal(1), rate.rate)
    return Rate(
        base=rate.quote,
        quote=rate.base,
        rate=_round_rate(exact),
        rate_type=rate.rate_type,
        source=DERIVED_SOURCE,
        as_of=rate.as_of,
        fetched_at=rate.fetched_at,
        status=rate.status,
        derivation=Derivation("INVERSE", (rate,), exact),
    )


def _cross_value(rate_a: Rate, rate_b: Rate, base: str, pivot: str, quote: str) -> Decimal:
    """base->quote through pivot: legs quoted directly multiply, legs quoted the other way divide; one rounding."""
    numerator, denominator = Decimal(1), Decimal(1)
    for leg, source, target in ((rate_a, base, pivot), (rate_b, pivot, quote)):
        if leg.pair == (source, target):
            numerator = _WIDE_EXACT.multiply(numerator, leg.rate)
        else:
            denominator = _WIDE_EXACT.multiply(denominator, leg.rate)
    return _ROUND_TO_ODD.divide(numerator, denominator)


def cross(rate_a: Rate, rate_b: Rate, pivot: str) -> Rate:
    """The rate between the non-pivot currencies of two MID legs through ``pivot`` (both legs recorded).

    The result's base is ``rate_a``'s other currency and its quote ``rate_b``'s; either leg may be quoted
    in either direction. It takes the earlier as_of and fetched_at of the legs and the weaker status.
    """
    validate_rate(rate_a)
    validate_rate(rate_b)
    _require_mid(rate_a, rate_b)
    currencies.require_money_currency(pivot)
    if pivot not in rate_a.pair or pivot not in rate_b.pair:
        raise FxError("PIVOT_NOT_IN_LEG", f"Both legs must include the pivot currency {pivot}.")
    base = rate_a.quote if rate_a.base == pivot else rate_a.base
    quote = rate_b.quote if rate_b.base == pivot else rate_b.base
    if base == quote:
        raise FxError("SAME_CURRENCY", f"A cross rate through {pivot} needs two different currencies, got {base} twice.")
    exact = _cross_value(rate_a, rate_b, base, pivot, quote)
    fetched = [leg.fetched_at for leg in (rate_a, rate_b)]
    return Rate(
        base=base,
        quote=quote,
        rate=_round_rate(exact),
        rate_type="MID",
        source=DERIVED_SOURCE,
        as_of=min(rate_a.as_of, rate_b.as_of),
        fetched_at=None if None in fetched else min(f for f in fetched if f is not None),
        status=_combined_status((rate_a, rate_b)),
        derivation=Derivation("CROSS", (rate_a, rate_b), exact, pivot),
    )


@dataclass(frozen=True)
class Conversion:
    """One conversion, frozen once made (3.2 §4.3): never recomputed with a newer rate (EC-MON-09)."""

    original: Money
    target: str
    rate: Rate
    quotation: str
    rate_date: date
    rounding_mode: str
    result: Money
    unrounded: Decimal
    purpose: str | None = None
    conversion_date: date | None = None

    def to_record(self) -> dict[str, Any]:
        """The fx_conversions columns (3.9 §3.3); a stored conversion must name its purpose."""
        if self.purpose is None:
            raise FxError("PURPOSE_REQUIRED", f"A stored conversion needs one of {CONVERSION_PURPOSES}.")
        ensure_storable(self.original)
        ensure_storable(self.result)
        return {
            "source_amount": decimal_str(self.original.amount),
            "source_currency": self.original.currency,
            "target_currency": self.target,
            "purpose": self.purpose,
            "fx_rate_id": self.rate.rate_id,
            "rate": decimal_str(self.rate.rate),
            "quotation": self.quotation,
            "rate_date": self.rate_date.isoformat(),
            "rounding_mode": self.rounding_mode,
            "result_amount": amount_str(self.result.amount, self.target),
        }

    def trace(self) -> dict[str, Any]:
        """Everything needed to explain and replay the conversion, including derivation legs."""
        return {
            **self.to_record(),
            "unrounded": decimal_str(self.unrounded),
            "conversion_date": None if self.conversion_date is None else self.conversion_date.isoformat(),
            "rate_source": self.rate.source,
            "rate_type": self.rate.rate_type,
            "derived_from": None if self.rate.derivation is None else self.rate.derivation.to_record(),
        }


def _require_usable(rate: Rate, conversion_date: date | None, max_age_hours: int | None) -> None:
    if rate.status == _PENDING:
        raise FxError("RATE_NOT_APPROVED", f"The {rate.base}/{rate.quote} rate of {rate.as_of} is waiting for approval.")
    if rate.status == _REJECTED:
        raise FxError("RATE_REJECTED", f"The {rate.base}/{rate.quote} rate of {rate.as_of} was rejected.")
    if conversion_date is None:
        if max_age_hours is not None:
            raise FxError("CONVERSION_DATE_REQUIRED", "A staleness check needs the conversion date.")
        return
    if rate_age_hours(rate, conversion_date) < 0:
        raise FxError(
            "RATE_AFTER_CONVERSION_DATE",
            f"The {rate.base}/{rate.quote} rate of {rate.as_of} is dated after the conversion date {conversion_date}.",
        )
    if max_age_hours is not None and is_stale(rate, conversion_date, max_age_hours):
        raise FxError(
            "RATE_STALE",
            f"The {rate.base}/{rate.quote} rate of {rate.as_of} is older than {max_age_hours} hours on {conversion_date}.",
        )


def convert(
    money: Money,
    rate: Rate | None,
    mode: str = "HALF_UP",
    *,
    purpose: str | None = None,
    target: str | None = None,
    conversion_date: date | None = None,
    max_age_hours: int | None = None,
) -> Conversion:
    """Convert ``money`` (in the rate's base) into the rate's quote currency, rounded with ``mode``.

    A missing rate raises RateUnavailable; an invalid, unapproved, future-dated or (when ``max_age_hours`` is
    given) stale rate raises FxError, so no conversion ever falls back to 1:1. Payroll conversions pass the
    conversion date and payroll.fx.max_rate_age_hours; ``purpose`` is required before the record is stored.
    """
    if not isinstance(money, Money):
        raise MoneyError("INVALID_OPERATION", f"Only Money can be converted, got {type(money).__name__}.")
    if rate is None:
        raise RateUnavailable(money.currency, target)
    validate_rate(rate)
    if mode not in ROUNDING_MODES:
        raise MoneyError("UNKNOWN_ROUNDING_MODE", f"{mode!r} is not one of {sorted(ROUNDING_MODES)}.")
    if purpose is not None and purpose not in CONVERSION_PURPOSES:
        raise FxError("INVALID_PURPOSE", f"{purpose!r} is not one of {CONVERSION_PURPOSES}.")
    if money.currency == rate.quote and target in (None, rate.base):
        raise FxError(
            "INVERTED_PAIR",
            f"A {rate.base}/{rate.quote} rate converts {rate.base} amounts; derive the inverse explicitly for {money.currency}.",
        )
    if money.currency != rate.base or (target is not None and target != rate.quote):
        wanted = f"{money.currency}/{target}" if target else money.currency
        raise FxError("PAIR_MISMATCH", f"A {rate.base}/{rate.quote} rate cannot convert {wanted}.")
    _require_usable(rate, conversion_date, max_age_hours)
    unrounded = exact_product(money.amount, rate.rate)
    result = Money(quantize_exact(unrounded, rate.quote, mode), rate.quote)
    return Conversion(
        original=money,
        target=rate.quote,
        rate=rate,
        quotation=rate.quotation,
        rate_date=rate.as_of,
        rounding_mode=mode,
        result=result,
        unrounded=unrounded,
        purpose=purpose,
        conversion_date=conversion_date,
    )


@dataclass(frozen=True)
class FxIssue:
    """A rate problem reported as data, for run pre-checks and employee exceptions (3.2 §4.5)."""

    code: str  # RATE_UNAVAILABLE | RATE_NOT_APPROVED | RATE_STALE | RATE_AMBIGUOUS | RATE_INVALID
    message: str
    base: str
    quote: str
    rate_type: str
    on: date
    rate: Rate | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "base_currency": self.base,
            "quote_currency": self.quote,
            "rate_type": self.rate_type,
            "on": self.on.isoformat(),
            "rate": None if self.rate is None else self.rate.to_record(),
        }


@dataclass(frozen=True)
class RateLookup:
    rate: Rate | None
    issue: FxIssue | None

    @property
    def ok(self) -> bool:
        return self.issue is None


def _instant_key(rate: Rate) -> tuple[int, datetime | None]:
    return (0, None) if rate.fetched_at is None else (1, rate.fetched_at)


def lookup_rate(
    rates: Iterable[Rate],
    base: str,
    quote: str,
    *,
    on: date,
    rate_type: str = "MID",
    max_age_hours: int | None = None,
) -> RateLookup:
    """The rate to use for base->quote on ``on``: the latest one (by as_of not after ``on``, then fetched_at)
    that was not rejected. Problems come back as an FxIssue instead of an exception.

    When that latest rate is invalid or still waiting for approval the lookup is blocked; an older
    approved rate is never used in its place, because that would be a silent "last known" rate (3.2 §4.5).
    """
    currencies.require_money_currency(base)
    currencies.require_money_currency(quote)
    if base == quote:
        raise FxError("SAME_CURRENCY", f"{base} amounts need no conversion and never get a 1:1 rate.")
    if rate_type not in RATE_TYPES:
        raise FxError("INVALID_RATE_TYPE", f"{rate_type!r} is not one of {RATE_TYPES}.")
    _require_date(on, "on")
    if max_age_hours is not None:
        _non_negative_int(max_age_hours, "max_age_hours")

    def issue(code: str, message: str, rate: Rate | None = None) -> RateLookup:
        return RateLookup(None, FxIssue(code, message, base, quote, rate_type, on, rate))

    candidates = [r for r in rates if isinstance(r, Rate) and r.pair == (base, quote) and r.rate_type == rate_type]
    dated = [
        r
        for r in candidates
        if isinstance(r.as_of, date) and not isinstance(r.as_of, datetime) and r.as_of <= on and r.status != _REJECTED
    ]
    if not dated:
        return issue("RATE_UNAVAILABLE", f"No {base}/{quote} {rate_type} rate on or before {on}.")
    latest_day = max(r.as_of for r in dated)
    same_day = [r for r in dated if r.as_of == latest_day]
    try:
        latest_instant = max(_instant_key(r) for r in same_day)
    except TypeError:  # a naive fetched_at cannot be ordered against an aware one
        return issue("RATE_INVALID", f"The {base}/{quote} {rate_type} rates of {latest_day} have invalid fetch times.")
    best = [r for r in same_day if _instant_key(r) == latest_instant]
    for candidate in best:
        try:
            validate_rate(candidate)
        except MoneyError as exc:
            return issue(
                "RATE_INVALID",
                f"The latest {base}/{quote} {rate_type} rate ({latest_day}) is invalid: {exc}",
                candidate,
            )
    pending = [r for r in best if r.status == _PENDING]
    if pending:
        return issue(
            "RATE_NOT_APPROVED",
            f"The latest {base}/{quote} {rate_type} rate ({latest_day}) is waiting for approval.",
            pending[0],
        )
    if len({r.rate for r in best}) > 1:
        return issue("RATE_AMBIGUOUS", f"Several different {base}/{quote} {rate_type} rates were published for {latest_day}.")
    chosen = best[0]
    if max_age_hours is not None and is_stale(chosen, on, max_age_hours):
        return issue(
            "RATE_STALE",
            f"The latest {base}/{quote} {rate_type} rate ({chosen.as_of}) is older than {max_age_hours} hours on {on}.",
            chosen,
        )
    return RateLookup(chosen, None)


def find_rate(
    rates: Iterable[Rate],
    base: str,
    quote: str,
    *,
    on: date,
    rate_type: str = "MID",
    max_age_hours: int | None = None,
) -> Rate:
    """lookup_rate that raises: RateUnavailable when nothing is usable, FxError for the other problems."""
    found = lookup_rate(rates, base, quote, on=on, rate_type=rate_type, max_age_hours=max_age_hours)
    if found.issue is not None:
        if found.issue.code == "RATE_UNAVAILABLE":
            raise RateUnavailable(base, quote, f"none on or before {on}")
        raise FxError(found.issue.code, found.issue.message)
    if found.rate is None:
        raise RateUnavailable(base, quote, f"none on or before {on}")
    return found.rate


def resolve_conversion_date(
    rule: str,
    *,
    period_end: date | None = None,
    pay_date: date | None = None,
    calculation_date: date | None = None,
) -> date:
    """The day whose rate a payroll conversion uses, per payroll.fx.conversion_date_rule (3.1 §5.13)."""
    dates = {"PERIOD_END": period_end, "PAY_DATE": pay_date, "CALCULATION_DATE": calculation_date}
    if rule not in dates:
        raise FxError("UNKNOWN_DATE_RULE", f"{rule!r} is not one of {CONVERSION_DATE_RULES}.")
    chosen = dates[rule]
    if chosen is None:
        raise FxError("CONVERSION_DATE_MISSING", f"The {rule} rule needs {rule.lower()}.")
    return _require_date(chosen, rule.lower())


def retro_difference(finalized: Conversion, corrected: Conversion) -> Money:
    """The explicit retro adjustment when a finalized conversion is corrected: corrected minus finalized result.

    The finalized record is never changed (EC-MON-09); both must convert the same amount into the same currency.
    """
    if finalized.original != corrected.original or finalized.original.currency != corrected.original.currency:
        raise FxError("CONVERSION_MISMATCH", "A retro compares two conversions of the same original amount.")
    if finalized.target != corrected.target:
        raise FxError("CONVERSION_MISMATCH", f"Cannot compare a {finalized.target} result with a {corrected.target} result.")
    return corrected.result - finalized.result


@dataclass(frozen=True)
class ConvertedTotal:
    """A total of several currencies converted into one, never presented without its rates (EC-MON-04)."""

    total: Money
    conversions: tuple[Conversion, ...]
    unconverted: Money | None  # the amounts already in the target currency, if any

    @property
    def label(self) -> str:
        """'Converted to USD: 1 INR = 0.011919 USD (ECB, 2026-10-20); ...'."""
        seen: dict[tuple[str, str, str, date], str] = {}
        for item in self.conversions:
            rate = item.rate
            seen.setdefault((rate.base, rate.quote, rate.source, rate.as_of), f"{rate.quotation} ({rate.source}, {rate.as_of.isoformat()})")
        parts = "; ".join(seen.values())
        return f"Converted to {self.total.currency}: {parts}" if parts else f"All amounts in {self.total.currency}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total.to_json(),
            "label": self.label,
            "conversions": [item.trace() for item in self.conversions],
        }


def converted_total(
    amounts: Iterable[Money],
    target: str,
    rates: Iterable[Rate],
    *,
    on: date,
    mode: str = "HALF_UP",
    purpose: str = "REPORTING",
    rate_type: str = "MID",
    max_age_hours: int | None = None,
) -> ConvertedTotal:
    """Informational total in ``target``: each currency's subtotal converted once with its recorded rate.

    Subtotals are formed per currency first, so each currency is converted (and rounded) once; a missing or
    unusable rate raises (RateUnavailable or FxError) rather than leaving a currency out or using 1:1.
    """
    currencies.require_money_currency(target)
    pool = tuple(rates)
    subtotals = totals_by_currency(amounts)
    conversions: list[Conversion] = []
    total = Money.zero(target)
    unconverted: Money | None = None
    for code, subtotal in subtotals.items():
        if code == target:
            unconverted = subtotal
            total = total + subtotal
            continue
        rate = find_rate(pool, code, target, on=on, rate_type=rate_type, max_age_hours=max_age_hours)
        conversion = convert(
            subtotal, rate, mode, purpose=purpose, target=target, conversion_date=on, max_age_hours=max_age_hours
        )
        conversions.append(conversion)
        total = total + conversion.result
    return ConvertedTotal(total, tuple(conversions), unconverted)
