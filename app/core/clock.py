"""Company-local calendar dates. Timestamps are stored in UTC; "today" depends on the company.

Attendance time-keeping reads the current instant through ``utcnow()`` here
(not app.core.security directly), so tests can run shifts on a deterministic
fake clock without also moving session and token expiry.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from typing import Iterator, Optional

import pytz
from sqlalchemy.orm import Session

from app.core import security
from app.models.company import Company

# An offline punch is processed as if it happened at its reconstructed capture instant (AS 04-attendance/01 §6.2
# O-6): ``at`` sets that instant for the attendance code inside the block only (one request, one context).
_AS_OF: ContextVar[Optional[datetime]] = ContextVar("attendance_as_of", default=None)


def utcnow() -> datetime:
    return _AS_OF.get() or security.utcnow()


@contextmanager
def at(moment: datetime) -> Iterator[datetime]:
    token = _AS_OF.set(moment)
    try:
        yield moment
    finally:
        _AS_OF.reset(token)


def company_today(db: Session, company_id: int) -> date:
    company = db.get(Company, company_id)
    return utcnow().astimezone(pytz.timezone(company.timezone or "UTC")).date()
