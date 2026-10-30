"""Leave years ("cycles") and the arithmetic of entitlement (SPINE §6.3, §6.9). Pure: no database.

A cycle is ``[start, end]`` inclusive. CALENDAR_YEAR = Jan 1; FISCAL_YEAR = the legal entity's ``fiscal_year_start``
(MM-DD); CUSTOM = the plan's ``custom_start``; ANNIVERSARY = the service start's month and day (29 Feb anchors on
28 Feb in non-leap years). Credits are rounded with the cumulative method: post round(earned to date) minus what was
already posted, so the yearly total is exactly the rounded annual figure (no drift).
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import List, Optional, Tuple

ZERO = Decimal(0)
TWO_DP = Decimal("0.01")
PERIOD_MONTHS = {"MONTHLY": 1, "QUARTERLY": 3, "HALF_YEARLY": 6}


@dataclass(frozen=True)
class Cycle:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def key(self) -> str:
        return self.start.isoformat()


def add_months(day: date, months: int) -> date:
    """Calendar month arithmetic; the day clamps to the month end (31 Jan + 1 month = 28/29 Feb)."""
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _anchor(year: int, month: int, day: int) -> date:
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def cycle_for(kind: str, on: date, *, custom_start: Optional[str] = None, fiscal_start: str = "01-01",
              anniversary: Optional[date] = None) -> Cycle:
    """The cycle containing ``on``."""
    if kind == "CALENDAR_YEAR":
        month, day = 1, 1
    elif kind == "FISCAL_YEAR":
        month, day = (int(p) for p in (fiscal_start or "01-01").split("-"))
    elif kind == "CUSTOM":
        month, day = (int(p) for p in (custom_start or "01-01").split("-"))
    elif kind == "ANNIVERSARY":
        base = anniversary or date(on.year, 1, 1)
        month, day = base.month, base.day
    else:
        raise ValueError(f"unknown cycle kind {kind!r}")
    start = _anchor(on.year, month, day)
    if start > on:
        start = _anchor(on.year - 1, month, day)
    following = _anchor(start.year + 1, month, day)
    return Cycle(start, following - timedelta(days=1))


def next_cycle(cycle: Cycle, kind: str, **kw) -> Cycle:
    return cycle_for(kind, cycle.end + timedelta(days=1), **kw)


def round_units(value: Decimal, mode: str) -> Decimal:
    if mode == "NEAREST_HALF":
        return (value * 2).quantize(Decimal(1), ROUND_HALF_UP) / 2
    if mode == "UP_HALF":
        return (value * 2).quantize(Decimal(1), ROUND_CEILING) / 2
    if mode == "DOWN_HALF":
        return (value * 2).quantize(Decimal(1), ROUND_FLOOR) / 2
    if mode == "NEAREST_QUARTER":
        return (value * 4).quantize(Decimal(1), ROUND_HALF_UP) / 4
    if mode == "NEAREST_WHOLE":
        return value.quantize(Decimal(1), ROUND_HALF_UP)
    return value.quantize(TWO_DP, ROUND_HALF_UP)


def periods(cycle: Cycle, frequency: str) -> List[Tuple[date, date]]:
    """PERIODIC accrual periods of a cycle (monthly, quarterly, half-yearly from the cycle start)."""
    step = PERIOD_MONTHS[frequency]
    out, start = [], cycle.start
    while start <= cycle.end:
        end = min(add_months(start, step) - timedelta(days=1), cycle.end)
        out.append((start, end))
        start = end + timedelta(days=1)
    return out


def months_between(start: date, end: date) -> int:
    """Whole months completed from ``start`` to ``end`` (service months for tenure slabs)."""
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day and not (end.day == calendar.monthrange(end.year, end.month)[1]):
        months -= 1
    return max(months, 0)


def slab_amount(slabs: List[dict], service_start: date, on: date) -> Decimal:
    months = months_between(service_start, on)
    amount = ZERO
    for slab in sorted(slabs, key=lambda s: s["from_months"]):
        if months >= slab["from_months"]:
            amount = Decimal(str(slab["amount"]))
    return amount


def earned_to_date(entitlement: dict, cycle: Cycle, on: date, *, joined: date, left: Optional[date] = None,
                   joiner: str = "PRORATE", leaver: str = "PRORATE", service_start: Optional[date] = None,
                   rounding: str = "NONE_2DP") -> Decimal:
    """Units earned in ``cycle`` up to ``on`` (inclusive), before rounding to what was already posted.

    FRONT_LOADED / TENURE_SLABS(front-loaded): the year's amount at the cycle start, prorated by calendar days for a
    joiner (PRORATE) or a leaver; PERIODIC: the amount ÷ periods per period posted at its START or END; NONE and
    EVENT_GRANT: nothing (events grant their own units).
    """
    method = entitlement.get("method") or "NONE"
    if method in ("NONE", "EVENT_GRANT", "PER_DAYS_WORKED") or on < cycle.start:
        return ZERO
    first = max(cycle.start, joined)
    last = min(cycle.end, left) if left else cycle.end
    if last < first or on < first:
        return ZERO
    if method == "TENURE_SLABS":
        annual = slab_amount(entitlement.get("tenure_slabs") or [], service_start or joined, max(first, cycle.start))
    else:
        annual = Decimal(str(entitlement.get("amount") or "0"))
    if method in ("FRONT_LOADED", "TENURE_SLABS"):
        share = Decimal(1)
        if first > cycle.start and joiner == "PRORATE":
            share = Decimal((cycle.end - first).days + 1) / Decimal(cycle.days)
        if left and left < cycle.end and leaver == "PRORATE":
            share = Decimal((last - first).days + 1) / Decimal(cycle.days)
        return round_units(annual * share, rounding)
    # PERIODIC
    plist = periods(cycle, entitlement["frequency"])
    per = annual / Decimal(len(plist))
    total = ZERO
    for p_start, p_end in plist:
        post_day = p_start if entitlement.get("post_at") == "START" else p_end
        if post_day > on or p_end < first or p_start > last:
            continue
        covered = (min(p_end, last) - max(p_start, first)).days + 1
        length = (p_end - p_start).days + 1
        if covered < length and ((p_start < first and joiner == "PRORATE") or (p_end > last and leaver == "PRORATE")):
            total += per * Decimal(covered) / Decimal(length)
        else:
            total += per
    return round_units(total, rounding)
