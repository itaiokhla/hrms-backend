"""Plain-language explanations, line traces and the calculation hash (3.5 §8.2-§8.4).

Every line carries one sentence the UI shows first ("50,000.00 × 28 ÷ 30 (2 LOP
days: 12–13 Nov)") and a structured trace in the shape of 3.5 §8.3: method, rate,
proration, the unrounded value, the rounding step and the formula steps.
``calc_hash`` is SHA-256 over the snapshot's content hash, the engine version and
the canonical lines, so a replay that changes a single digit, label or source is
detected (EC-STA-04).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from app.payroll.engine.lines import Line
from app.payroll.engine.snapshot import canonical_json, sha256_hex
from app.payroll.proration import plain

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
EN_DASH = "–"


def money_text(amount: Decimal, minor_unit: int) -> str:
    """Grouped amount with exactly the currency's digits: 50,000.00; 1,500 (JPY); 12.345 (KWD)."""
    step = Decimal(1).scaleb(-minor_unit)
    return format(amount.quantize(step, rounding=ROUND_HALF_UP), ",f")


def rate_text(value: Decimal, minor_unit: int, places: int = 4) -> str:
    """A rate: money digits when exact, otherwise shortened with an ellipsis (240.3846…)."""
    exponent = value.normalize().as_tuple().exponent
    digits = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if digits <= minor_unit:
        return money_text(value, minor_unit)
    if digits <= places:
        return format(value.normalize(), ",f")
    return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN), ",f") + "…"


def qty_text(value: Decimal) -> str:
    """A quantity of days, hours or units without trailing zeros: 28, 0.5, 182.5."""
    text = plain(value)
    whole, _, fraction = text.partition(".")
    grouped = format(int(whole), ",") if whole.lstrip("-").isdigit() else whole
    return f"{grouped}.{fraction}" if fraction else grouped


def _day(when: dt.date, with_month: bool = True) -> str:
    return f"{when.day} {MONTHS[when.month - 1]}" if with_month else str(when.day)


def date_ranges(dates: Iterable[dt.date]) -> str:
    """Consecutive dates collapsed: "12–13 Nov", "3 Nov, 12–13 Nov", "30 Nov–1 Dec"."""
    ordered = sorted(set(dates))
    if not ordered:
        return ""
    runs: list[tuple[dt.date, dt.date]] = []
    start = end = ordered[0]
    for when in ordered[1:]:
        if when - end == dt.timedelta(days=1):
            end = when
        else:
            runs.append((start, end))
            start = end = when
    runs.append((start, end))
    parts = []
    for first, last in runs:
        if first == last:
            parts.append(_day(first))
        elif (first.year, first.month) == (last.year, last.month):
            parts.append(f"{first.day}{EN_DASH}{_day(last)}")
        else:
            parts.append(f"{_day(first)}{EN_DASH}{_day(last)}")
    return ", ".join(parts)


def plural(count: Decimal | int, word: str) -> str:
    return f"{qty_text(Decimal(count))} {word}{'' if Decimal(count) == 1 else 's'}"


def lop_note(lop_days: Decimal, dates: Sequence[dt.date]) -> str:
    """"(2 LOP days: 12–13 Nov)", or "" when nothing was unpaid."""
    if lop_days <= 0:
        return ""
    where = date_ranges(dates)
    return f" ({plural(lop_days, 'LOP day')}{': ' + where if where else ''})"


def rounding_trace(stage: str, mode: str, increment: Decimal) -> dict[str, str]:
    return {"stage": stage, "mode": mode, "increment": format(increment, "f")}


def money_json(amount: Decimal | None, currency: str) -> dict[str, str] | None:
    return None if amount is None else {"amount": format(amount, "f"), "currency": currency}


def line_sort_key(line: Line) -> tuple[Any, ...]:
    """Explicit line order: stage, payslip order, code, segment, period reference, amount (3.5 §8.2)."""
    return (line.stage, line.sort_order, line.component, -1 if line.segment is None else line.segment,
            line.period_ref or "", line.kind, format(line.amount, "f"))


def sorted_lines(lines: Iterable[Line]) -> list[Line]:
    return sorted(lines, key=line_sort_key)


def canonical_lines(lines: Iterable[Line]) -> str:
    return canonical_json([line.to_json() for line in sorted_lines(lines)])


def calc_hash(content_hash: str, engine_version: str, lines: Iterable[Line]) -> str:
    """SHA-256 of (snapshot content hash + engine version + canonical lines), 3.5 §8.2."""
    return sha256_hex("\n".join((content_hash, engine_version, canonical_lines(lines))))


def plain_trace(value: Any) -> Any:
    """Trace values as JSON: Decimals as strings, tuples as lists, mappings as dicts."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): plain_trace(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_trace(v) for v in value]
    return value


__all__ = [
    "calc_hash", "canonical_lines", "date_ranges", "line_sort_key", "lop_note", "money_json", "money_text",
    "plain", "plain_trace", "plural", "qty_text", "rate_text", "rounding_trace", "sorted_lines",
]
