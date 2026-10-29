"""Attendance cycles that differ from the pay period (JC-AS-10; AS 05 §4; owner decision 2026-10-28 "window LOP,
period pay", IG-AS-94).

The pay period keeps its own dates for everything the engine prorates by date (employment, placement, compensation
segments, suspensions, standard hours). The **time facts** of each period date — attendance, leave, manual LOP,
on-duty, holiday and weekly off — come from the attendance window:

* period dates inside the window keep their own facts;
* the window dates before the period (e.g. 26–31 Oct of the 26 Oct – 25 Nov window of November) are re-dated, in
  order, onto the period dates after the window (26–30 Nov);
* a period date after the window without a window date left is **presumed present** (an expected working day gets
  a synthetic STANDARD PRESENT input with no source; leave, LOP and on-duty of that date belong to the next cycle);
* a window date before the period without a period date left (a longer window head than period tail) is an
  overflow: reported as the WARNING ``ATTENDANCE_CYCLE_OVERFLOW`` with its dates, never silently dropped.

Only attendance v2 tenants use it (``attendance.v2.enabled``); v1 tenants keep the blocking
``ATTENDANCE_CYCLE_UNSUPPORTED``. ``map_window`` is pure; ``window_ledger`` reads the ledger once over the union of
the two ranges. Retro (``retro._day_sections``) and the snapshot builder share it, so a correction is recalculated
with the same mapping the run used.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.payroll import ledger

ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class Mapping:
    days: Tuple[ledger.DayInput, ...]
    # period date -> window date whose facts it carries (identity dates included); None = presumed present
    source_dates: Dict[date, Optional[date]]
    overflow: Tuple[date, ...]


def _dates(start: date, end: date) -> List[date]:
    out, day = [], start
    while day <= end:
        out.append(day)
        day += ONE_DAY
    return out


def presumed(day: ledger.DayInput) -> ledger.DayInput:
    """A period date after the window with nothing to map: present when it is an expected working day."""
    off_holiday = day.holiday is not None and day.holiday.type is not ledger.HolidayType.OPTIONAL_NOT_AVAILED
    working = day.employed and not day.weekly_off and not off_holiday
    attendance = ledger.AttendanceInput(status="PRESENT", rules_mode="STANDARD") if working else None
    return replace(day, attendance=attendance, leaves=(), manual_lop=(), on_duty=())


def map_window(by_date: Dict[date, ledger.DayInput], period: Tuple[date, date],
               window: Tuple[date, date]) -> Mapping:
    """Period dates with their time facts taken from the window (see the module doc)."""
    ps, pe = period
    ws, we = window
    head = [d for d in _dates(ws, min(we, ps - ONE_DAY))] if ws < ps else []
    tail = [d for d in _dates(max(ps, we + ONE_DAY), pe)] if we < pe else []
    mapped: Dict[date, date] = dict(zip(tail, head))
    overflow = tuple(head[len(tail):])
    days: List[ledger.DayInput] = []
    sources: Dict[date, Optional[date]] = {}
    for d in _dates(ps, pe):
        base = by_date[d]
        if ws <= d <= we:
            days.append(base)
            sources[d] = d
        elif d in mapped:
            facts = by_date[mapped[d]]
            days.append(replace(facts, date=d, employed=base.employed, suspended_unpaid=base.suspended_unpaid,
                                suspension_id=base.suspension_id, standard_hours=base.standard_hours,
                                assignment_id=base.assignment_id, segment=base.segment))
            sources[d] = mapped[d]
        else:
            days.append(presumed(base))
            sources[d] = None
    return Mapping(tuple(days), sources, overflow)


def split_window(db: Session, company_id: int, period) -> Optional[Tuple[date, date]]:
    """The attendance window of a pay period when it differs from the period on an attendance v2 tenant."""
    start, end = period.attendance_start, period.attendance_end
    if end < start or (start, end) == (period.period_start, period.period_end):
        return None
    from app.services import attendance_access

    if not attendance_access.v2_enabled(db, company_id):
        return None
    return start, end


def window_ledger(db: Session, company_id: int, employee_id: int, period: Tuple[date, date],
                  window: Tuple[date, date], **kwargs) -> Mapping:
    from app.payroll.services import time_inputs

    first, last = min(period[0], window[0]), max(period[1], window[1])
    rows = time_inputs.ledger_inputs(db, company_id, employee_id, first, last, **kwargs)
    return map_window({d.date: d for d in rows}, period, window)


def window_dates_of(mapping: Mapping, dates: Sequence[date]) -> List[date]:
    """The period dates whose facts come from any of ``dates`` (for retro: which period dates a correction moves)."""
    wanted = set(dates)
    return sorted(d for d, source in mapping.source_dates.items() if source in wanted)
