"""Company-local calendar dates. Timestamps are stored in UTC; "today" depends on the company.

Attendance time-keeping reads the current instant through ``utcnow()`` here
(not app.core.security directly), so tests can run shifts on a deterministic
fake clock without also moving session and token expiry.
"""

from datetime import date, datetime

import pytz
from sqlalchemy.orm import Session

from app.core import security
from app.models.company import Company


def utcnow() -> datetime:
    return security.utcnow()


def company_today(db: Session, company_id: int) -> date:
    company = db.get(Company, company_id)
    return utcnow().astimezone(pytz.timezone(company.timezone or "UTC")).date()
