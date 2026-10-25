"""Shares of a deceased employee's final settlement among nominees and legal heirs (3.5 §15; EC-BNK-15, EC-COR-07).

Pure and deterministic (no database, clock or network). The settlement net is the company's own figure for the
final-settlement run; this module only divides it:

* ``validate_shares``: every share is more than 0 and at most 100 percent with at most 6 decimals, a nominee
  appears once, there is at least one share and the shares add up to exactly 100 (``complete``).
* ``split_net``: each nominee receives ``share % of the net`` rounded DOWN to the currency's minor unit
  (``allocate_net`` PERCENT rule); the nominee with the largest share (the lowest id on a tie) receives the
  remainder, so the amounts always add up to the net exactly and rounding differences never create or lose
  money. A nominee whose share rounds to zero receives no payment line (``amount`` 0, no instruction).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

from app.payroll.banking._currency import fits, strict_decimal
from app.payroll.banking.allocation import MAX_PERCENT_PLACES, Allocation, allocate_net
from app.payroll.banking.errors import BankingError

HUNDRED = Decimal(100)


class NomineeShareError(BankingError):
    """The shares cannot be divided (arguments or rules)."""


@dataclass(frozen=True)
class NomineeShare:
    nominee_id: int
    share_percent: Decimal


@dataclass(frozen=True)
class ShareIssue:
    code: str
    message: str


@dataclass(frozen=True)
class ShareLine:
    nominee_id: int
    share_percent: Decimal
    amount: Decimal
    remainder: bool  # this nominee absorbed the rounding difference
    explanation: str


def _coerce(shares: Iterable[NomineeShare | tuple]) -> list[NomineeShare]:
    out = []
    for item in shares:
        if isinstance(item, NomineeShare):
            out.append(item)
        else:
            nominee_id, percent = item
            out.append(NomineeShare(int(nominee_id), strict_decimal(percent, NomineeShareError, "share")))
    return out


def validate_shares(shares: Iterable[NomineeShare | tuple]) -> tuple[ShareIssue, ...]:
    """Empty when valid and complete; otherwise the reasons (SHARE_RANGE, SHARE_PRECISION, DUPLICATE_NOMINEE,
    NO_NOMINEES, SHARES_INCOMPLETE, SHARES_EXCEED)."""
    items = _coerce(shares)
    issues: list[ShareIssue] = []
    if not items:
        return (ShareIssue("NO_NOMINEES", "at least one nominee must receive the settlement"),)
    seen: set[int] = set()
    total = Decimal(0)
    for item in items:
        if item.nominee_id in seen:
            issues.append(ShareIssue("DUPLICATE_NOMINEE", "a nominee can appear only once"))
        seen.add(item.nominee_id)
        if not Decimal(0) < item.share_percent <= HUNDRED:
            issues.append(ShareIssue("SHARE_RANGE", "a share must be more than 0 and at most 100 percent"))
        elif not fits(item.share_percent, MAX_PERCENT_PLACES):
            issues.append(ShareIssue("SHARE_PRECISION", f"a share has at most {MAX_PERCENT_PLACES} decimals"))
        else:
            total += item.share_percent
    if issues:
        return tuple(issues)
    if total > HUNDRED:
        return (ShareIssue("SHARES_EXCEED", f"the shares add up to {total.normalize():f} percent (at most 100)"),)
    if total < HUNDRED:
        return (ShareIssue("SHARES_INCOMPLETE", f"the shares add up to {total.normalize():f} percent, not 100"),)
    return ()


def split_net(net: Decimal | int | str, currency: str, shares: Sequence[NomineeShare | tuple]) -> tuple[ShareLine, ...]:
    """Divide ``net`` (already in the currency's minor unit) by the shares; lines are in the shares' id order."""
    items = _coerce(shares)
    issues = validate_shares(items)
    if issues:
        raise NomineeShareError(issues[0].code, issues[0].message)
    remainder = min(items, key=lambda s: (-s.share_percent, s.nominee_id))
    rules = [Allocation(s.nominee_id, "REMAINDER" if s is remainder else "PERCENT",
                        None if s is remainder else s.share_percent, order=index, currency=currency)
             for index, s in enumerate(sorted(items, key=lambda s: s.nominee_id))]
    result = allocate_net(net, currency, rules)
    if result.outcome != "ALLOCATED":
        raise NomineeShareError(result.outcome, "the settlement net cannot be divided")
    by_id = {int(line.account_id): line for line in result.lines}
    lines = []
    for share in sorted(items, key=lambda s: s.nominee_id):
        line = by_id[share.nominee_id]
        lines.append(ShareLine(share.nominee_id, share.share_percent, line.amount, share is remainder,
                               line.explanation))
    return tuple(lines)


__all__ = ["NomineeShare", "NomineeShareError", "ShareIssue", "ShareLine", "split_net", "validate_shares"]
