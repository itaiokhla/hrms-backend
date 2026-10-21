"""Split-payment allocation of a payee's net pay across bank accounts (3.7 §5).

``validate_allocations`` is the save-time check: at least one account and no more than
``payroll.bank.max_accounts_per_payee`` (1 when split payments are OFF), unique accounts and
orders, PERCENT values in (0, 100] with at most 6 decimals and together <= 100, FIXED amounts > 0,
exactly one account that receives the rest (REMAINDER, or PRIMARY when payments are not split),
and account currencies the payment rail supports (EC-BNK-09, EC-BNK-14).

``allocate_net`` runs when payment instructions are created, on the net pay (already rounded to
the pay currency's minor unit):

    FIXED      filled in ``order`` from what is left of the net
    PERCENT    value % of the net, rounded down to the minor unit, then filled in ``order``
    REMAINDER  net - everything above; it absorbs every rounding difference

When fixed (or fixed plus percentage) amounts exceed the net they are filled in order until the net
is exhausted, the remainder account gets zero, and the result carries an EXCEPTION issue explaining
it (EC-BNK-09). The lines always add up to the net exactly. A zero or negative net produces no lines:
no employee instruction is created and a negative net is a recovery receivable elsewhere (EC-PAY-15,
EC-PAY-16). Amounts are in the net's currency; a line for an account in another currency is flagged
``requires_fx`` and converted with an FX snapshot when its instruction is created, which records both
amounts (3.2 §4.4, EC-PAY-17, EC-MON-17).

``cash_payment`` rounds a cash payment to the currency's cash rounding increment and returns the
difference to record (3.7 §7.3, 3.2 §2.2).
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any, Collection, Iterable, Mapping

from app.payroll.banking._currency import (
    AMOUNT_LIMIT,
    EXACT,
    amount_text,
    cash_increment,
    currency_code,
    fits,
    payable_minor_unit,
    quantum,
    strict_decimal,
)
from app.payroll.banking.errors import AllocationError

ALLOCATION_TYPES: tuple[str, ...] = ("PRIMARY", "PERCENT", "FIXED", "REMAINDER")
RECEIVES_REST: frozenset[str] = frozenset({"PRIMARY", "REMAINDER"})
OUTCOMES: tuple[str, ...] = ("ALLOCATED", "ZERO_NET", "NEGATIVE_NET", "INVALID_RULES")
MAX_PERCENT_PLACES = 6
HUNDRED = Decimal(100)
ROUNDING_MODES: Mapping[str, str] = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_EVEN": ROUND_HALF_EVEN,
    "UP": ROUND_UP,
    "DOWN": ROUND_DOWN,
    "CEILING": ROUND_CEILING,
    "FLOOR": ROUND_FLOOR,
}


@dataclass(frozen=True)
class Allocation:
    """One account's share rule: ``value`` is a percentage (PERCENT), an amount in the pay currency
    (FIXED) or None (PRIMARY, REMAINDER); ``currency`` is the account's currency (None = the pay currency)."""

    account_id: int | str
    type: str
    value: Decimal | None = None
    order: int = 0
    currency: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.account_id, bool) or not isinstance(self.account_id, (int, str)) or self.account_id == "":
            raise AllocationError("INVALID_ARGUMENT", "account_id must be an integer or a non-empty string")
        if not isinstance(self.type, str):
            raise AllocationError("INVALID_ARGUMENT", "type must be a string")
        if isinstance(self.order, bool) or not isinstance(self.order, int):
            raise AllocationError("INVALID_ARGUMENT", "order must be an integer")
        if self.currency is not None and not isinstance(self.currency, str):
            raise AllocationError("INVALID_ARGUMENT", "currency must be a string or None")
        object.__setattr__(self, "type", self.type.strip().upper())
        if self.value is not None:
            object.__setattr__(self, "value", strict_decimal(self.value, AllocationError, "allocation value"))
        if self.currency is not None:
            object.__setattr__(self, "currency", self.currency.strip().upper())

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Allocation":
        """From ``{account_id, type, value, order, currency}`` (the bank account's allocation JSON plus id and currency)."""
        unknown = set(data) - {"account_id", "type", "value", "order", "currency"}
        if unknown:
            raise AllocationError("INVALID_ARGUMENT", f"unknown allocation fields: {', '.join(sorted(map(str, unknown)))}")
        return cls(
            account_id=data.get("account_id"),  # type: ignore[arg-type]
            type=data.get("type"),  # type: ignore[arg-type]
            value=data.get("value"),
            order=data.get("order", 0),  # type: ignore[arg-type]
            currency=data.get("currency"),
        )


@dataclass(frozen=True)
class AllocationIssue:
    code: str
    severity: str  # "ERROR": rules rejected, nothing allocated; "EXCEPTION": allocated, needs review
    message: str
    account_ids: tuple[int | str, ...] = ()


@dataclass(frozen=True)
class AllocationLine:
    account_id: int | str
    type: str
    order: int
    amount: Decimal  # in ``currency``, the pay currency
    currency: str
    account_currency: str
    requested: Decimal | None  # what the rule asked for before filling (None for the remainder)
    requires_fx: bool
    explanation: str


@dataclass(frozen=True)
class AllocationResult:
    outcome: str
    net: Decimal
    currency: str
    lines: tuple[AllocationLine, ...]
    issues: tuple[AllocationIssue, ...]

    @property
    def payable_lines(self) -> tuple[AllocationLine, ...]:
        """Lines that become payment instructions (amount > 0)."""
        return tuple(line for line in self.lines if line.amount > 0)

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))


def _coerce(item: Allocation | Mapping[str, Any]) -> Allocation:
    if isinstance(item, Allocation):
        return item
    if isinstance(item, Mapping):
        return Allocation.from_mapping(item)
    raise AllocationError("INVALID_ARGUMENT", "allocations must be Allocation objects or mappings")


def _sort_key(rule: Allocation) -> tuple[int, int, str]:
    account = rule.account_id
    return (rule.order, 0 if isinstance(account, int) else 1, f"{account:020d}" if isinstance(account, int) else account)


def _payable(code: str) -> bool:
    try:
        payable_minor_unit(currency_code(code, AllocationError), AllocationError)
    except AllocationError:
        return False
    return True


def _duplicates(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    repeated: list[Any] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def validate_allocations(
    allocations: Iterable[Allocation | Mapping[str, Any]],
    *,
    max_accounts: int | None = None,
    supported_currencies: Collection[str] | None = None,
    pay_currency: str | None = None,
) -> tuple[AllocationIssue, ...]:
    """Save-time validation of a payee's allocation rules; an empty result means valid (3.7 §5)."""
    rules = tuple(_coerce(item) for item in allocations)
    if max_accounts is not None and (isinstance(max_accounts, bool) or not isinstance(max_accounts, int) or max_accounts < 1):
        raise AllocationError("INVALID_ARGUMENT", "max_accounts must be a positive integer")
    pay_digits = payable_minor_unit(currency_code(pay_currency, AllocationError), AllocationError) if pay_currency else None
    supported = None if supported_currencies is None else {code.strip().upper() for code in supported_currencies}
    issues: list[AllocationIssue] = []

    def error(code: str, message: str, *accounts: int | str) -> None:
        issues.append(AllocationIssue(code, "ERROR", message, tuple(accounts)))

    if not rules:
        error("NO_ACCOUNTS", "at least one bank account must receive the net pay")
    if max_accounts is not None and len(rules) > max_accounts:
        if max_accounts == 1:
            error("TOO_MANY_ACCOUNTS", "split payments are not enabled: one account receives the whole net pay")
        else:
            error("TOO_MANY_ACCOUNTS", f"a payee can have at most {max_accounts} accounts")
    for account in _duplicates(rule.account_id for rule in rules):
        error("DUPLICATE_ACCOUNT", "an account can appear only once", account)
    for order in _duplicates(rule.order for rule in rules):
        error("DUPLICATE_ORDER", f"two accounts share order {order}", *[r.account_id for r in rules if r.order == order])

    percent_total = Decimal(0)
    for rule in rules:
        value = rule.value
        if rule.type not in ALLOCATION_TYPES:
            error("TYPE_INVALID", f"type must be one of {', '.join(ALLOCATION_TYPES)}", rule.account_id)
        elif rule.type in RECEIVES_REST:
            if value is not None:
                error("VALUE_NOT_ALLOWED", f"a {rule.type} account receives the rest and takes no value", rule.account_id)
        elif value is None:
            error("VALUE_REQUIRED", f"a {rule.type} allocation needs a value", rule.account_id)
        elif rule.type == "PERCENT":
            if not Decimal(0) < value <= HUNDRED:
                error("PERCENT_RANGE", "a percentage must be more than 0 and at most 100", rule.account_id)
            elif not fits(value, MAX_PERCENT_PLACES):
                error("PERCENT_PRECISION", f"a percentage has at most {MAX_PERCENT_PLACES} decimals", rule.account_id)
            else:
                percent_total += value
        elif value <= 0:
            error("FIXED_NOT_POSITIVE", "a fixed amount must be more than zero", rule.account_id)
        elif value >= AMOUNT_LIMIT:
            error("FIXED_OUT_OF_RANGE", "the fixed amount is too large", rule.account_id)
        elif pay_digits is not None and not fits(value, pay_digits):
            error("FIXED_PRECISION", f"a fixed amount has at most {pay_digits} decimals in {pay_currency}", rule.account_id)
        if rule.currency is not None and not _payable(rule.currency):
            error("CURRENCY_NOT_PAYABLE", "the account currency is not a currency payroll can pay in", rule.account_id)
            continue
        currency = rule.currency or (pay_currency.strip().upper() if pay_currency else None)
        if supported is not None and currency is not None and currency not in supported:
            error("CURRENCY_NOT_SUPPORTED", f"the payment rail does not support {currency}", rule.account_id)

    receivers = [rule.account_id for rule in rules if rule.type in RECEIVES_REST]
    if rules and not receivers:
        error("REMAINDER_REQUIRED", "exactly one account must receive the remainder of the net pay")
    if len(receivers) > 1:
        error("REMAINDER_NOT_UNIQUE", "only one account can receive the remainder of the net pay", *receivers)
    if percent_total > HUNDRED:
        error("PERCENT_OVER_100", "percentages add up to more than 100")
    return tuple(issues)


def allocate_net(
    net: Decimal | int | str,
    currency: str,
    allocations: Iterable[Allocation | Mapping[str, Any]],
    *,
    max_accounts: int | None = None,
    supported_currencies: Collection[str] | None = None,
) -> AllocationResult:
    """Split a net pay across a payee's accounts per 3.7 §5; problems in the rules are returned as issues."""
    amount = strict_decimal(net, AllocationError, "net pay")
    code = currency_code(currency, AllocationError)
    digits = payable_minor_unit(code, AllocationError)
    if not fits(amount, digits):
        raise AllocationError("NET_PRECISION", f"net pay has more decimals than {code} allows; round it first")
    if abs(amount) >= AMOUNT_LIMIT:
        raise AllocationError("AMOUNT_OUT_OF_RANGE", "net pay is outside the supported range")
    rules = tuple(_coerce(item) for item in allocations)
    if amount == 0:
        return AllocationResult("ZERO_NET", Decimal(0), code, (), ())
    if amount < 0:
        return AllocationResult("NEGATIVE_NET", amount, code, (), ())
    issues = validate_allocations(rules, max_accounts=max_accounts, supported_currencies=supported_currencies, pay_currency=code)
    if issues:
        return AllocationResult("INVALID_RULES", amount, code, (), issues)

    ordered = sorted(rules, key=_sort_key)
    step = quantum(digits)
    net_text = amount_text(amount, digits)
    left = amount
    granted: dict[int, tuple[Decimal, Decimal]] = {}
    short: dict[str, list[int | str]] = {"FIXED": [], "PERCENT": []}
    for kind in ("FIXED", "PERCENT"):
        for index, rule in enumerate(ordered):
            if rule.type != kind or rule.value is None:
                continue
            if kind == "FIXED":
                requested = rule.value
            else:
                requested = EXACT.divide(EXACT.multiply(amount, rule.value), HUNDRED).quantize(step, rounding=ROUND_DOWN, context=EXACT)
            paid = min(requested, left)
            left -= paid
            granted[index] = (requested, paid)
            if paid < requested:
                short[kind].append(rule.account_id)

    lines: list[AllocationLine] = []
    for index, rule in enumerate(ordered):
        account_currency = rule.currency or code
        if rule.type in RECEIVES_REST:
            requested, paid = None, left
            explanation = (
                f"Full net pay {net_text} {code}"
                if len(ordered) == 1
                else f"Remainder of {net_text} {code} after the other accounts: {amount_text(paid, digits)}"
            )
        else:
            requested, paid = granted[index]
            asked = amount_text(requested, digits)
            explanation = (
                f"Fixed {asked} {code}" if rule.type == "FIXED" else f"{rule.value.normalize():f}% of {net_text} {code} = {asked}"  # type: ignore[union-attr]
            )
            if paid < requested:
                explanation += f"; {amount_text(paid, digits)} paid because the net pay was exhausted"
        lines.append(
            AllocationLine(
                account_id=rule.account_id,
                type=rule.type,
                order=rule.order,
                amount=paid.quantize(step, context=EXACT),
                currency=code,
                account_currency=account_currency,
                requested=None if requested is None else requested.quantize(step, context=EXACT),
                requires_fx=account_currency != code,
                explanation=explanation,
            )
        )

    exceptions: list[AllocationIssue] = []
    if short["FIXED"]:
        exceptions.append(
            AllocationIssue(
                "FIXED_EXCEEDS_NET",
                "EXCEPTION",
                "fixed allocations exceed the net pay: they were filled in order until the net pay was exhausted "
                "and the remainder account receives zero",
                tuple(short["FIXED"]),
            )
        )
    if short["PERCENT"]:
        exceptions.append(
            AllocationIssue(
                "ALLOCATIONS_EXCEED_NET",
                "EXCEPTION",
                "fixed and percentage allocations together exceed the net pay: they were filled in order and "
                "the remainder account receives zero",
                tuple(short["PERCENT"]),
            )
        )
    return AllocationResult("ALLOCATED", amount, code, tuple(lines), tuple(exceptions))


@dataclass(frozen=True)
class CashPayment:
    amount: Decimal  # payable in cash
    rounding_difference: Decimal  # amount - original; record it as a rounding line or carry it forward
    increment: Decimal
    currency: str


def cash_payment(amount: Decimal | int | str, currency: str, *, mode: str = "HALF_UP") -> CashPayment:
    """Round a cash payment to the currency's cash increment (or its minor unit when it has none)."""
    value = strict_decimal(amount, AllocationError, "cash amount")
    code = currency_code(currency, AllocationError)
    digits = payable_minor_unit(code, AllocationError)
    if mode not in ROUNDING_MODES:
        raise AllocationError("INVALID_ARGUMENT", f"mode must be one of {', '.join(ROUNDING_MODES)}")
    if value <= 0 or not fits(value, digits) or value >= AMOUNT_LIMIT:
        raise AllocationError("INVALID_AMOUNT", f"a cash payment is a positive amount with at most {digits} decimals in {code}")
    increment = cash_increment(code) or quantum(digits)
    if increment <= 0 or not fits(increment, digits):
        raise AllocationError("INVALID_INCREMENT", f"the cash increment of {code} does not fit its minor unit")
    units = EXACT.divide(value, increment).quantize(Decimal(1), rounding=ROUNDING_MODES[mode], context=EXACT)
    paid = EXACT.multiply(units, increment).quantize(quantum(digits), context=EXACT)
    return CashPayment(paid, paid - value, increment, code)
