"""Shift allowances: night and weekend shifts (3.4 §12, 3.1 §5.8).

A shift qualifies as a night shift when its net minutes inside ``night_window``
(default ``22:00–06:00``, crossing midnight) reach ``min_minutes_in_window``
(default 120). Unpaid breaks are clipped to the shift and merged before they are
taken out of the window, so no minute is removed twice. ``allowance_type``:

    PER_SHIFT_AMOUNT      amount × qualifying shifts
    PERCENT_OF_HOURLY     percent ÷ 100 × hourly_rate × night hours of qualifying shifts
    FIXED_MONTHLY_IF_ANY  amount once when at least one shift qualifies

``weekend_shift_amount`` is paid per shift worked on a weekend. The percentage
applies to the base hourly rate and is never multiplied by an overtime
multiplier (EC-HOL-02). Shifts are paid on the dates where the DAY-grain
switch is ON; instants are compared in UTC, the window in the shift's local time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from app.payroll.time_pay_common import (
    EARNING,
    INVALID_INPUT,
    INVALID_POLICY,
    MAX_SECONDS_PER_DAY,
    DayRanges,
    TimePayError,
    TimePayLine,
    check_ranges,
    field_value,
    in_calc_context,
    make_line,
    policy_choice,
    policy_decimal,
    policy_int,
    policy_money,
    require_decimal,
    resolve_minor_unit,
    show,
    show_hours,
    switch_on,
    units_4dp,
)

ALLOWANCE_TYPES = ("PER_SHIFT_AMOUNT", "PERCENT_OF_HOURLY", "FIXED_MONTHLY_IF_ANY")
DEFAULT_NIGHT_WINDOW = "22:00-06:00"
SHIFT_ALLOWANCE_COMPONENT = "SHIFT_ALLOWANCE"

Span = Tuple[datetime, datetime]


def _parse_clock(text: str, name: str) -> time:
    try:
        hour_text, minute_text = text.strip().split(":")
        clock = time(int(hour_text), int(minute_text))
    except (ValueError, TypeError):
        raise TimePayError(INVALID_POLICY, f"{name} must look like 22:00-06:00", name) from None
    return clock


def parse_night_window(raw: Any) -> Tuple[time, time]:
    """``"22:00-06:00"`` (or ``–``), ``{"start": "22:00", "end": "06:00"}`` or a ``(time, time)`` pair."""
    if raw is None:
        raw = DEFAULT_NIGHT_WINDOW
    if isinstance(raw, str):
        parts = raw.replace("–", "-").split("-")
        if len(parts) != 2:
            raise TimePayError(INVALID_POLICY, "night_window must look like 22:00-06:00", "night_window")
        start, end = (_parse_clock(part, "night_window") for part in parts)
    elif isinstance(raw, Mapping) or hasattr(raw, "start"):
        start_raw, end_raw = field_value(raw, "start"), field_value(raw, "end")
        start = start_raw if isinstance(start_raw, time) else _parse_clock(str(start_raw), "night_window")
        end = end_raw if isinstance(end_raw, time) else _parse_clock(str(end_raw), "night_window")
    elif isinstance(raw, (tuple, list)) and len(raw) == 2 and all(isinstance(t, time) for t in raw):
        start, end = raw
    else:
        raise TimePayError(INVALID_POLICY, "night_window must look like 22:00-06:00", "night_window")
    if start == end:
        raise TimePayError(INVALID_POLICY, "night_window must not be empty", "night_window")
    return start, end


@dataclass(frozen=True)
class ShiftAllowanceRules:
    night_start: time
    night_end: time
    min_minutes_in_window: int
    allowance_type: str
    amount: Optional[Decimal]
    percent: Optional[Decimal]
    weekend_shift_amount: Optional[Decimal]
    component: str


def parse_shift_allowance_policy(policy: Any, currency: str) -> ShiftAllowanceRules:
    """Read a SHIFT_ALLOWANCE_POLICY (3.1 §5.8); the type's amount or percent is required."""
    start, end = parse_night_window(field_value(policy, "night_window"))
    allowance_type = policy_choice(policy, "allowance_type", "PER_SHIFT_AMOUNT", ALLOWANCE_TYPES)
    amount = policy_money(policy, "amount", currency)
    percent = policy_decimal(policy, "percent", None, minimum=Decimal(0), maximum=Decimal(1000))
    if allowance_type in ("PER_SHIFT_AMOUNT", "FIXED_MONTHLY_IF_ANY") and amount is None:
        raise TimePayError(INVALID_POLICY, f"{allowance_type} needs amount", "amount")
    if allowance_type == "PERCENT_OF_HOURLY" and percent is None:
        raise TimePayError(INVALID_POLICY, "PERCENT_OF_HOURLY needs percent", "percent")
    component = field_value(policy, "component", SHIFT_ALLOWANCE_COMPONENT)
    if not isinstance(component, str) or not component:
        raise TimePayError(INVALID_POLICY, "component must be a component code", "component")
    return ShiftAllowanceRules(
        night_start=start,
        night_end=end,
        min_minutes_in_window=policy_int(policy, "min_minutes_in_window", 120, minimum=0, maximum=24 * 60) or 0,
        allowance_type=allowance_type,
        amount=amount,
        percent=percent,
        weekend_shift_amount=policy_money(policy, "weekend_shift_amount", currency),
        component=component,
    )


@dataclass(frozen=True)
class ShiftWorked:
    """A worked shift; ``date`` is the shift start date and ``weekend`` comes from the work week."""

    date: date
    start: datetime
    end: datetime
    unpaid_breaks: Sequence[Span] = field(default_factory=tuple)
    weekend: bool = False
    ref: Optional[str] = None


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo is not None else moment


def _windows(shift: ShiftWorked, start: time, end: time) -> List[Span]:
    spans = []
    first = shift.start.date() - timedelta(days=1)
    for offset in range((shift.end.date() - first).days + 1):
        day = first + timedelta(days=offset)
        opens = datetime.combine(day, start, tzinfo=shift.start.tzinfo)
        closes = datetime.combine(day + timedelta(days=1) if end <= start else day, end, tzinfo=shift.start.tzinfo)
        spans.append((_utc(opens), _utc(closes)))
    return spans


def _merge(spans: Sequence[Span]) -> List[Span]:
    merged: List[Span] = []
    for begin, finish in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return merged


def _overlap_seconds(spans: Sequence[Span], window: Span) -> int:
    total = 0
    for begin, finish in spans:
        low, high = max(begin, window[0]), min(finish, window[1])
        if high > low:
            total += int((high - low).total_seconds())
    return total


def night_minutes(shift: ShiftWorked, night_window: Any = DEFAULT_NIGHT_WINDOW) -> int:
    """Net whole minutes of ``shift`` inside the night window, unpaid breaks excluded."""
    start, end = parse_night_window(night_window)
    if (shift.start.tzinfo is None) != (shift.end.tzinfo is None):
        raise TimePayError(INVALID_INPUT, f"{shift.date}: start and end must both be timezone-aware or naive", "start")
    begin, finish = _utc(shift.start), _utc(shift.end)
    if finish <= begin or (finish - begin).total_seconds() > MAX_SECONDS_PER_DAY:
        raise TimePayError(INVALID_INPUT, f"{shift.date}: a shift must last between 1 second and 25 hours", "end")
    if shift.start.date() not in (shift.date, shift.date + timedelta(days=1)):
        raise TimePayError(INVALID_INPUT, f"{shift.date}: the shift must start on its date", "start")
    breaks = []
    for break_start, break_end in shift.unpaid_breaks:
        low, high = max(_utc(break_start), begin), min(_utc(break_end), finish)
        if high > low:
            breaks.append((low, high))
    merged = _merge(breaks)
    seconds = 0
    for window in _windows(shift, start, end):
        seconds += _overlap_seconds([(begin, finish)], window) - _overlap_seconds(merged, window)
    return seconds // 60


@dataclass(frozen=True)
class ShiftOutcome:
    date: date
    night_minutes: int
    night_qualifies: bool
    weekend_paid: bool
    paid: bool
    ref: Optional[str]


@dataclass(frozen=True)
class ShiftAllowance:
    lines: Tuple[TimePayLine, ...]
    shifts: Tuple[ShiftOutcome, ...]
    qualifying_night_shifts: int
    weekend_shifts: int
    night_amount: Decimal
    weekend_amount: Decimal
    explanation: str

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))


@in_calc_context
def shift_allowance(
    shifts: Sequence[ShiftWorked],
    policy: Any,
    *,
    currency: str,
    hourly_rate: Optional[Decimal] = None,
    enabled: Optional[DayRanges] = None,
    minor_unit: Optional[int] = None,
    rounding: str = "HALF_UP",
) -> ShiftAllowance:
    """The SHIFT_ALLOWANCE line for one employee and period (3.4 §12).

    Night and weekend parts are one line (the policy names one component) whose
    explanation and sources show both parts. ``hourly_rate`` is the unrounded base
    hourly rate, required for PERCENT_OF_HOURLY.
    """
    minor_unit = resolve_minor_unit(currency, minor_unit)
    check_ranges(enabled, "enabled")
    rules = parse_shift_allowance_policy(policy, currency)
    window = (rules.night_start, rules.night_end)
    outcomes: List[ShiftOutcome] = []
    qualifying: List[Tuple[ShiftWorked, int]] = []
    weekend: List[ShiftWorked] = []
    seen = set()
    for shift in sorted(shifts, key=lambda s: (s.date, _utc(s.start))):
        key = (_utc(shift.start), _utc(shift.end))
        if key in seen:
            raise TimePayError(INVALID_INPUT, f"{shift.date}: the same shift is given twice", "shifts")
        seen.add(key)
        minutes = night_minutes(shift, window)
        on = switch_on(shift.date, enabled)
        qualifies = minutes > 0 and minutes >= rules.min_minutes_in_window
        weekend_paid = on and shift.weekend and rules.weekend_shift_amount is not None
        if on and qualifies:
            qualifying.append((shift, minutes))
        if weekend_paid:
            weekend.append(shift)
        outcomes.append(ShiftOutcome(shift.date, minutes, qualifies, weekend_paid, on, shift.ref))

    parts: List[str] = []
    night = Decimal(0)
    units: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    if qualifying:
        if rules.allowance_type == "PER_SHIFT_AMOUNT":
            assert rules.amount is not None
            night = rules.amount * len(qualifying)
            parts.append(f"night {show(rules.amount)} × {len(qualifying)} shift(s)")
            units, rate = Decimal(len(qualifying)), rules.amount
        elif rules.allowance_type == "FIXED_MONTHLY_IF_ANY":
            assert rules.amount is not None
            night = rules.amount
            parts.append(f"night {show(rules.amount)} for the period ({len(qualifying)} qualifying shift(s))")
        else:
            assert rules.percent is not None
            if hourly_rate is None:
                raise TimePayError(INVALID_INPUT, "PERCENT_OF_HOURLY needs the hourly rate", "hourly_rate")
            base = require_decimal(hourly_rate, "hourly_rate", positive=True)
            minutes = sum(m for _, m in qualifying)
            night = rules.percent * base * minutes / 6000
            parts.append(f"night {show(rules.percent)}% × {show(base)} × {show_hours(minutes)}")
            units, rate = units_4dp(minutes), base
    weekend_total = Decimal(0)
    if weekend:
        assert rules.weekend_shift_amount is not None
        weekend_total = rules.weekend_shift_amount * len(weekend)
        parts.append(f"weekend {show(rules.weekend_shift_amount)} × {len(weekend)} shift(s)")
        if qualifying:
            units, rate = None, None
        else:
            units, rate = Decimal(len(weekend)), rules.weekend_shift_amount
    explanation = "; ".join(parts) if parts else "No qualifying night or weekend shift"
    lines: Tuple[TimePayLine, ...] = ()
    if parts:
        sources = [{"type": "shift", "date": s.date.isoformat(), "night_minutes": str(m), **({"id": s.ref} if s.ref else {})}
                   for s, m in qualifying]
        sources += [{"type": "shift", "date": s.date.isoformat(), "weekend": "true", **({"id": s.ref} if s.ref else {})}
                    for s in weekend]
        lines = (make_line(rules.component, EARNING, "ALLOWANCE", night + weekend_total, currency,
                           minor_unit=minor_unit, rounding=rounding,
                           method="FIXED" if rules.allowance_type == "FIXED_MONTHLY_IF_ANY" and not weekend
                           else "RATE_X_UNITS",
                           explanation=explanation, units=units, rate=rate, sources=sources),)
    return ShiftAllowance(lines, tuple(outcomes), len(qualifying), len(weekend), night, weekend_total, explanation)
