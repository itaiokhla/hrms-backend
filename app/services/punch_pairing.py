"""Punch pairing (04-attendance/03 §3; spine §5.5.3) — pure functions, no database (L0).

AS-M2 ships the interactive part: ``attribute`` finds the shift instance an interactive punch belongs to, whose start
date (in the work zone) is the punch's shift date (step 3; D-AS-06; BUG-AS-02). ``pair()`` for DEVICE / IMPORT /
SUPERVISOR punch sets arrives with the evaluation and devices milestones (AS-M3 / AS-M7).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Sequence, Tuple

import pytz

from app.services import shift_engine as engine

IN_LIKE = ("IN", "BREAK_END")


def attribute(instances: Sequence[engine.ShiftInstance], at: datetime, direction: str) -> Optional[engine.ShiftInstance]:
    """The instance whose matching window ``[match_start_at, match_end_at)`` contains ``at``; several -> IN-like
    punches go to the nearest start, OUT-like to the nearest end, ties to the earlier instance; none -> None."""
    hits = [i for i in instances if i.match_start_at <= at < i.match_end_at]
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]

    def distance(instance: engine.ShiftInstance) -> Tuple[float, datetime]:
        anchor = instance.start_at if direction in IN_LIKE else instance.end_at
        anchor = anchor or instance.match_start_at
        return abs((at - anchor).total_seconds()), instance.match_start_at

    return min(hits, key=distance)


def open_window_date(at: datetime, work_timezone: str, open_day_start_time: str = engine.DEFAULT_OPEN_DAY_START_TIME,
                     *, live_session_date: Optional[date] = None) -> date:
    """Shift date of a punch outside every instance window (OPEN shift, no shift): its local date in the work zone.
    A punch before ``open_day_start_time`` joins the previous date only while a session of that date is still live
    (step 3, AS-NGT-02); the first IN of a date therefore keeps its own local date."""
    zone = pytz.timezone(work_timezone)
    local = at.astimezone(zone)
    hour, minute = (int(x) for x in open_day_start_time.split(":"))
    previous = local.date() - timedelta(days=1)
    if local.time() < time(hour, minute) and live_session_date == previous:
        return previous
    return local.date()


def shift_date(instances: Sequence[engine.ShiftInstance], at: datetime, direction: str, work_timezone: str,
               *, live_session_date: Optional[date] = None) -> Tuple[date, Optional[engine.ShiftInstance]]:
    """(shift date, matched instance or None) of an interactive punch. OPEN instances have no clock window of their
    own: they follow the OPEN-window date rule and are returned for lineage only."""
    instance = attribute([i for i in instances if i.kind != engine.OPEN], at, direction)
    if instance is not None:
        return instance.shift_date, instance
    day = open_window_date(at, work_timezone, live_session_date=live_session_date)
    return day, next((i for i in instances if i.kind == engine.OPEN and i.shift_date == day), None)
