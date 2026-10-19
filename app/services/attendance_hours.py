"""Canonical attendance hours: the only place gross, lunch and net time is computed.

Every consumer (the live attendance card, team boards, reports, exports and
the payroll snapshot) calls ``compute`` on the same stored instants, so totals
reconcile to the second:

    gross    = punch_out - punch_in
    lunch    = sum of lunch intervals
    excluded = sum of policy-excluded (unpaid) break intervals
    net      = gross - excluded

Instants are converted to UTC and truncated to whole seconds before any
arithmetic, so parts always add up to the whole and DST or timezone changes
cannot move a total. Breaks are clipped to the session and merged before they
are summed, so no break is deducted twice or outside the shift and
0 <= excluded <= gross always holds. "Net working time" is attendance minus
excluded breaks; it is not a measure of productive output.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

LUNCH = "LUNCH"

Span = Tuple[datetime, datetime]


@dataclass(frozen=True)
class BreakInterval:
    start: datetime
    end: Optional[datetime]  # None while the break is still running
    paid: bool = False
    kind: str = LUNCH


@dataclass(frozen=True)
class Hours:
    gross_seconds: Optional[int]  # None when the punch-out is missing
    lunch_seconds: int
    break_seconds: int  # all breaks, paid or unpaid
    excluded_seconds: int  # unpaid breaks, deducted from gross
    net_seconds: Optional[int]
    provisional: bool  # the session is still running; values grow until punch-out
    complete: bool  # False when the punch-out is missing and needs a correction
    issues: Tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "gross_seconds": self.gross_seconds,
            "lunch_seconds": self.lunch_seconds,
            "break_seconds": self.break_seconds,
            "excluded_seconds": self.excluded_seconds,
            "net_seconds": self.net_seconds,
            "provisional": self.provisional,
            "complete": self.complete,
            "issues": list(self.issues),
        }


def to_utc_second(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("attendance instants must be timezone-aware")
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _union(spans: Iterable[Span]) -> List[Span]:
    merged: List[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _seconds(spans: Iterable[Span]) -> int:
    return sum(int((end - start).total_seconds()) for start, end in spans)


def _overlaps(spans: Sequence[Span]) -> bool:
    ordered = sorted(spans)
    return any(later[0] < earlier[1] for earlier, later in zip(ordered, ordered[1:]))


def compute(
    punch_in: datetime,
    punch_out: Optional[datetime],
    breaks: Sequence[BreakInterval] = (),
    *,
    now: Optional[datetime] = None,
    live: bool = True,
) -> Hours:
    """Hours for one attendance session.

    ``live`` is True while the session is open (no punch-out yet and not closed
    by the system); its totals are then provisional up to ``now``. A session
    that was closed without a punch-out has no gross or net time until it is
    corrected, because inventing an end time would fabricate hours.
    """
    issues: List[str] = []
    start = to_utc_second(punch_in)
    if punch_out is not None:
        end: Optional[datetime] = to_utc_second(punch_out)
        provisional, complete = False, True
    elif live:
        if now is None:
            raise ValueError("now is required for a running session")
        end = to_utc_second(now)
        provisional, complete = True, True
    else:
        end, provisional, complete = None, False, False
        issues.append("MISSING_PUNCH_OUT")

    if end is not None and end < start:
        issues.append("END_BEFORE_START")
        end = start

    clipped: List[Tuple[Span, BreakInterval]] = []
    for item in breaks:
        b_start = to_utc_second(item.start)
        if item.end is not None:
            b_end = to_utc_second(item.end)
        elif end is not None:
            b_end = end  # still on break: runs up to now (or to the punch-out)
        else:
            issues.append("OPEN_BREAK_WITHOUT_PUNCH_OUT")
            continue
        if b_end < b_start:
            issues.append("NEGATIVE_BREAK")
            continue
        lo = max(b_start, start)
        hi = b_end if end is None else min(b_end, end)
        if hi < lo or (hi == lo and b_end > b_start):
            issues.append("BREAK_OUTSIDE_SESSION")
            continue
        if lo != b_start or hi != b_end:
            issues.append("BREAK_CLIPPED_TO_SESSION")
        clipped.append(((lo, hi), item))

    spans = [span for span, _ in clipped]
    if _overlaps(spans):
        issues.append("OVERLAPPING_BREAKS")
    break_seconds = _seconds(_union(spans))
    lunch_seconds = _seconds(_union(span for span, b in clipped if b.kind == LUNCH))
    excluded = _seconds(_union(span for span, b in clipped if not b.paid))

    if end is None:
        return Hours(None, lunch_seconds, break_seconds, excluded, None, provisional, complete, tuple(dict.fromkeys(issues)))
    gross = int((end - start).total_seconds())
    excluded = min(max(excluded, 0), gross)
    return Hours(gross, lunch_seconds, break_seconds, excluded, gross - excluded, provisional, complete,
                 tuple(dict.fromkeys(issues)))


def total(results: Iterable[Hours], include_provisional: bool = True) -> dict:
    """Sum of complete sessions; incomplete ones are counted, never guessed.

    Running (provisional) sessions are summed up to "now" for live views; payroll
    passes include_provisional=False so a run never freezes a half-finished shift.
    """
    summary = {"sessions": 0, "complete_sessions": 0, "incomplete_sessions": 0, "provisional_sessions": 0,
               "gross_seconds": 0, "lunch_seconds": 0, "excluded_seconds": 0, "net_seconds": 0}
    for hours in results:
        summary["sessions"] += 1
        if not hours.complete:
            summary["incomplete_sessions"] += 1
            continue
        summary["provisional_sessions"] += int(hours.provisional)
        if hours.provisional and not include_provisional:
            continue
        summary["complete_sessions"] += 1
        summary["gross_seconds"] += hours.gross_seconds or 0
        summary["lunch_seconds"] += hours.lunch_seconds
        summary["excluded_seconds"] += hours.excluded_seconds
        summary["net_seconds"] += hours.net_seconds or 0
    return summary


def decimal_hours(seconds: Optional[int]) -> Optional[float]:
    """Hours with two decimals, for spreadsheets. Totals must be converted after summing seconds."""
    if seconds is None:
        return None
    from decimal import ROUND_HALF_UP, Decimal

    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
