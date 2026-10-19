"""Company-local calendar dates. Timestamps are stored in UTC; "today" depends on the company."""

from datetime import date

import pytz
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models.company import Company


def company_today(db: Session, company_id: int) -> date:
    company = db.get(Company, company_id)
    return utcnow().astimezone(pytz.timezone(company.timezone or "UTC")).date()
