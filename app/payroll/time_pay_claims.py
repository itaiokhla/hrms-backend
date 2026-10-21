"""Reimbursement claims: limits, receipts, duplicates and foreign currency (3.4 §14, 3.1 §5.10).

``check_claim`` applies the claim type of REIMBURSEMENT_POLICY to one claim:
per-claim, per-month and per-year limits (the month and year of the expense
date, counting earlier approved and pending claims of the same type), the receipt
required above ``receipt_required_above``, the ``submission_window_days`` and the
duplicate check. Problems are review items for the approver, never raised; the
approvable amount is the claim capped by every remaining limit.

The duplicate fingerprint is SHA-256 over ``receipt digest | amount | currency |
expense date | merchant`` with the amount normalised and the merchant
case-folded and whitespace-collapsed, so the same receipt claimed again matches
(EC-VAR-04). A foreign-currency claim is converted with the rate the caller
resolved for the policy's rate date and the FX snapshot is kept (EC-VAR-09);
without a rate the claim is blocked. Reimbursements are paid as REIMBURSEMENT
lines that add to net and never count as wages.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from app.payroll.time_pay_common import (
    BLOCKING,
    INVALID_INPUT,
    INVALID_POLICY,
    REIMBURSEMENT,
    WARNING,
    Issue,
    TimePayError,
    TimePayLine,
    amount_str,
    check_currency,
    field_value,
    in_calc_context,
    make_line,
    policy_bool,
    policy_choice,
    policy_int,
    policy_money,
    require_decimal,
    resolve_minor_unit,
    round_money,
    show,
)

COUNTED_STATUSES = frozenset({"SUBMITTED", "PENDING_APPROVAL", "APPROVED", "SCHEDULED", "PAID"})
PAYOUT_ROUTES = ("WITH_PAYROLL", "SEPARATE_PAYMENT")
RATE_DATES = ("EXPENSE_DATE", "APPROVAL_DATE")
TAXABILITY = ("TAXABLE", "EXEMPT", "PARTIAL", "UNCLASSIFIED")
FINGERPRINT_VERSION = "v1"


def _normalise_merchant(merchant: str) -> str:
    text = unicodedata.normalize("NFKC", merchant).casefold()
    return " ".join(text.split())


def claim_fingerprint(receipt_digest: str, amount: Decimal, currency: str, expense_date: date, merchant: str) -> str:
    """Duplicate fingerprint of a claim: SHA-256 hex of the canonical receipt/amount/date/merchant text."""
    digest = receipt_digest.strip().lower() if isinstance(receipt_digest, str) else ""
    if not digest or any(c not in "0123456789abcdef" for c in digest):
        raise TimePayError(INVALID_INPUT, "receipt_digest must be a hexadecimal file hash", "receipt_digest")
    if not isinstance(merchant, str):
        raise TimePayError(INVALID_INPUT, "merchant must be text", "merchant")
    if not isinstance(expense_date, date):
        raise TimePayError(INVALID_INPUT, "expense_date must be a date", "expense_date")
    value = require_decimal(amount, "amount", positive=True)
    check_currency(currency)
    canonical = "|".join((FINGERPRINT_VERSION, digest, format(value.normalize(), "f"), currency,
                          expense_date.isoformat(), _normalise_merchant(merchant)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClaimType:
    code: str
    limit_per_claim: Optional[Decimal]
    limit_per_month: Optional[Decimal]
    limit_per_year: Optional[Decimal]
    receipt_required_above: Optional[Decimal]
    taxability: str = "UNCLASSIFIED"  # from the country pack (EC-VAR-15); never wages either way


def claim_type(policy: Any, code: str, currency: str) -> Optional[ClaimType]:
    """The claim type ``code`` of a REIMBURSEMENT_POLICY with its limits in the pay currency, or ``None``."""
    types = field_value(policy, "claim_types", ())
    if isinstance(types, (str, bytes)) or not isinstance(types, Sequence):
        raise TimePayError(INVALID_POLICY, "claim_types must be a list", "claim_types")
    found = [t for t in types if field_value(t, "code") == code]
    if len(found) > 1:
        raise TimePayError(INVALID_POLICY, f"claim type {code} is defined twice", "claim_types")
    if not found:
        return None
    raw = found[0]
    return ClaimType(code, policy_money(raw, "limit_per_claim", currency), policy_money(raw, "limit_per_month", currency),
                     policy_money(raw, "limit_per_year", currency),
                     policy_money(raw, "receipt_required_above", currency),
                     policy_choice(raw, "taxability", "UNCLASSIFIED", TAXABILITY))


@dataclass(frozen=True)
class Claim:
    claim_id: str
    claim_type: str
    expense_date: date
    amount: Decimal
    currency: str
    merchant: str = ""
    receipt_digests: Tuple[str, ...] = ()
    submitted_on: Optional[date] = None
    status: str = "SUBMITTED"


@dataclass(frozen=True)
class FxSnapshot:
    """The converted amount and the rate used, stored with the claim (EC-VAR-09)."""

    from_currency: str
    to_currency: str
    rate: Decimal
    rate_date: date
    rate_basis: str  # EXPENSE_DATE | APPROVAL_DATE
    source_amount: Decimal
    converted_amount: Decimal


@dataclass(frozen=True)
class ClaimCheck:
    claim_id: str
    amount: Optional[Decimal]  # in the pay currency; None when it cannot be converted
    approvable_amount: Decimal
    receipt_required: bool
    fingerprints: Tuple[str, ...]
    duplicates: Tuple[str, ...]  # claim ids with the same fingerprint or receipt
    fx: Optional[FxSnapshot]
    issues: Tuple[Issue, ...]
    taxability: Optional[str] = None  # the claim type's pack classification; None when the type is unknown

    @property
    def blocked(self) -> bool:
        return any(issue.severity == BLOCKING for issue in self.issues)


def _year_start(expense_date: date, year_start: str) -> date:
    try:
        month, day = (int(part) for part in year_start.split("-"))
        start = date(expense_date.year, month, day)
    except (ValueError, AttributeError):
        raise TimePayError(INVALID_INPUT, "year_start must look like 04-01", "year_start") from None
    return start if start <= expense_date else date(expense_date.year - 1, month, day)


@in_calc_context
def check_claim(
    claim: Claim,
    policy: Any,
    *,
    pay_currency: str,
    prior_claims: Sequence[Tuple[Claim, Decimal]] = (),
    known_fingerprints: Optional[Mapping[str, str]] = None,
    known_receipts: Optional[Mapping[str, str]] = None,
    fx_rate: Optional[Decimal] = None,
    fx_rate_date: Optional[date] = None,
    year_start: str = "01-01",
    enabled: bool = True,
) -> ClaimCheck:
    """Limits, receipt, window and duplicate checks for one claim (3.4 §14).

    ``prior_claims`` are this employee's other claims with their amounts in the pay
    currency; only counted statuses of the same type use up limits. ``known_fingerprints``
    and ``known_receipts`` map a fingerprint or receipt digest to the claim id that used it.
    ``fx_rate`` converts one unit of the claim currency into the pay currency, as resolved
    for the policy's ``fx_rate_date`` basis.
    """
    minor = resolve_minor_unit(pay_currency)
    check_currency(claim.currency, "claim currency")
    source = require_decimal(claim.amount, "amount", positive=True)
    window = policy_int(policy, "submission_window_days", 60, minimum=0, maximum=3660)
    dedupe = policy_bool(policy, "duplicate_receipt_check", True)
    basis = policy_choice(policy, "fx_rate_date", "EXPENSE_DATE", RATE_DATES)
    issues: List[Issue] = []
    ref = claim.claim_id
    if not enabled:
        issues.append(Issue("REIMBURSEMENTS_OFF", BLOCKING, "Reimbursements are switched OFF", claim.expense_date, ref))

    fx = None
    amount: Optional[Decimal] = source
    if claim.currency != pay_currency:
        if fx_rate is None or fx_rate_date is None:
            issues.append(Issue("CLAIM_FX_RATE_MISSING", BLOCKING,
                                f"No {claim.currency}→{pay_currency} rate for the {basis.replace('_', ' ').lower()}; "
                                "the claim cannot be paid until one exists", claim.expense_date, ref))
            amount = None
        else:
            rate = require_decimal(fx_rate, "fx_rate", positive=True)
            amount = round_money(source * rate, minor)
            fx = FxSnapshot(claim.currency, pay_currency, rate, fx_rate_date, basis, source, amount)

    kind = claim_type(policy, claim.claim_type, pay_currency)
    receipt_required = False
    approvable = Decimal(0)
    if kind is None:
        issues.append(Issue("CLAIM_TYPE_UNKNOWN", BLOCKING, f"Claim type {claim.claim_type} is not in the policy",
                            claim.expense_date, ref))
    elif amount is not None:
        approvable = amount
        if kind.receipt_required_above is not None and amount > kind.receipt_required_above:
            receipt_required = True
            if not claim.receipt_digests:
                issues.append(Issue("CLAIM_RECEIPT_REQUIRED", BLOCKING,
                                    f"A receipt is required above {show(kind.receipt_required_above)}",
                                    claim.expense_date, ref))
        month_used = year_used = Decimal(0)
        first_day = _year_start(claim.expense_date, year_start)
        for other, other_amount in prior_claims:
            if other.claim_id == claim.claim_id or other.claim_type != claim.claim_type:
                continue
            if other.status not in COUNTED_STATUSES:
                continue
            value = require_decimal(other_amount, f"prior claim {other.claim_id}", minimum=Decimal(0))
            if (other.expense_date.year, other.expense_date.month) == (claim.expense_date.year, claim.expense_date.month):
                month_used += value
            if _year_start(other.expense_date, year_start) == first_day:
                year_used += value
        for code, limit, used, label in (("CLAIM_ABOVE_PER_CLAIM_LIMIT", kind.limit_per_claim, Decimal(0), "per-claim"),
                                         ("CLAIM_ABOVE_MONTHLY_LIMIT", kind.limit_per_month, month_used, "monthly"),
                                         ("CLAIM_ABOVE_YEARLY_LIMIT", kind.limit_per_year, year_used, "yearly")):
            if limit is None:
                continue
            remaining = max(Decimal(0), limit - used)
            if amount > remaining:
                issues.append(Issue(code, WARNING,
                                    f"{show(amount)} exceeds the {label} limit of {show(limit)}"
                                    + (f" ({show(used)} already claimed)" if used else "")
                                    + f"; at most {show(remaining)} can be approved",
                                    claim.expense_date, ref,
                                    details={"limit": amount_str(limit, minor), "used": amount_str(used, minor),
                                             "approvable": amount_str(remaining, minor)}))
            approvable = min(approvable, remaining)

    if window is not None and claim.submitted_on is not None and (claim.submitted_on - claim.expense_date).days > window:
        issues.append(Issue("CLAIM_OUTSIDE_SUBMISSION_WINDOW", WARNING,
                            f"Submitted {(claim.submitted_on - claim.expense_date).days} days after the expense; "
                            f"the window is {window} days", claim.expense_date, ref))
    if claim.submitted_on is not None and claim.submitted_on < claim.expense_date:
        issues.append(Issue("CLAIM_DATED_IN_FUTURE", BLOCKING, "The expense date is after the submission date",
                            claim.expense_date, ref))

    fingerprints = tuple(claim_fingerprint(d, source, claim.currency, claim.expense_date, claim.merchant)
                         for d in claim.receipt_digests)
    duplicates: List[str] = []
    if dedupe:
        for digest, fingerprint in zip(claim.receipt_digests, fingerprints):
            for match in ((known_fingerprints or {}).get(fingerprint),
                          (known_receipts or {}).get(digest.strip().lower())):
                if match and match != claim.claim_id and match not in duplicates:
                    duplicates.append(match)
        if duplicates:
            issues.append(Issue("CLAIM_POSSIBLE_DUPLICATE", WARNING,
                                f"The receipt matches claim(s) {', '.join(duplicates)}; check before approving",
                                claim.expense_date, ref, details={"claims": ",".join(duplicates)}))
    return ClaimCheck(claim.claim_id, amount, approvable, receipt_required, fingerprints, tuple(duplicates), fx,
                      tuple(issues), kind.taxability if kind else None)


@in_calc_context
def reimbursement_lines(approved: Sequence[Tuple[str, str, Decimal]], currency: str, *,
                        payout_route: str = "WITH_PAYROLL",
                        component: str = "REIMBURSEMENT",
                        taxability: Optional[Mapping[str, str]] = None) -> Tuple[TimePayLine, ...]:
    """REIMBURSEMENT lines (one per claim type) for approved ``(claim_id, claim_type, amount)`` items.

    The lines add to net pay and never count as wages. ``taxability`` maps a claim type to
    its pack classification (``ClaimCheck.taxability``), recorded on each source; a type
    without one is ``UNCLASSIFIED`` (EC-VAR-15). ``SEPARATE_PAYMENT`` claims are paid in
    their own batch, so no payroll line is produced.
    """
    if payout_route not in PAYOUT_ROUTES:
        raise TimePayError(INVALID_INPUT, f"payout_route must be one of {', '.join(PAYOUT_ROUTES)}", "payout_route")
    minor = resolve_minor_unit(currency)
    if payout_route == "SEPARATE_PAYMENT":
        return ()
    grouped: dict = {}
    seen: set = set()
    for claim_id, type_code, value in approved:
        if claim_id in seen:
            raise TimePayError(INVALID_INPUT, f"claim {claim_id} is given twice and would be paid twice", "approved")
        seen.add(claim_id)
        grouped.setdefault(type_code, []).append((claim_id, require_decimal(value, f"claim {claim_id}", positive=True)))
    lines = []
    for type_code in sorted(grouped):
        items = grouped[type_code]
        tax = (taxability or {}).get(type_code, "UNCLASSIFIED")
        if tax not in TAXABILITY:
            raise TimePayError(INVALID_INPUT, f"taxability of {type_code} must be one of {', '.join(TAXABILITY)}",
                               "taxability")
        lines.append(make_line(component, REIMBURSEMENT, "REIMBURSEMENT",
                               sum((v for _, v in items), Decimal(0)), currency, minor_unit=minor, rounding="HALF_UP",
                               method="INPUT", explanation=" + ".join(show(v) for _, v in items)
                               + f" ({len(items)} approved {type_code} claim(s), {tax.lower()}); not wages",
                               sources=[{"type": "claim", "id": c, "claim_type": type_code, "taxability": tax,
                                         "amount": amount_str(v, minor)} for c, v in items]))
    return tuple(lines)
