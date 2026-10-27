"""Roster provider seam of the work-calendar resolver (LH 04-holiday/03 §3.5; SPINE V3-062; AS JC-AS-31).

A published roster entry outranks every work-week pattern (04-holiday/03 §5.4 step 1). The roster module belongs to
attendance-shift (``ShiftRosterProvider`` from AS-M5); until it ships every employee is pattern-based through
``NullRosterProvider``. Consumers call ``provider()`` so nothing changes when the real provider registers itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Optional, Protocol

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class RosterEntry:
    kind: str  # WORK | OFF
    roster_version: int
    published_at: datetime
    shift_id: Optional[int] = None


class RosterProvider(Protocol):
    def is_roster_based(self, db: Session, company_id: int, employee_id: int, day: date) -> bool: ...

    def entries(self, db: Session, company_id: int, employee_id: int, start: date,
                end: date) -> Mapping[date, RosterEntry]: ...


class NullRosterProvider:
    """Every employee follows a pattern (no rosters before AS-M5)."""

    def is_roster_based(self, db: Session, company_id: int, employee_id: int, day: date) -> bool:
        return False

    def entries(self, db: Session, company_id: int, employee_id: int, start: date,
                end: date) -> Mapping[date, RosterEntry]:
        return {}


_PROVIDER: RosterProvider = NullRosterProvider()


def register(provider: RosterProvider) -> None:
    """Called by the roster module at import (AS-M5)."""
    global _PROVIDER
    _PROVIDER = provider


def provider() -> RosterProvider:
    return _PROVIDER
