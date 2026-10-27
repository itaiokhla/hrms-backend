"""Local dates per time zone for daily-local jobs (LH-M1 WI-1.2; LH 02 §7.2, §8.2 rule 4).

A tenant's zones are the work zones of its current placements — ``employee_assignments.work_timezone`` (LH-M1
WI-1.3), else the branch, else the legal entity — and always ``companies.timezone``. A zone is "due" for local date D
once its wall clock has passed the job's threshold on D (00:30 unless the job type says otherwise); a threshold that
does not exist on a DST day is reached at the first valid instant after it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List

import pytz
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.org import Branch, EmployeeAssignment, LegalEntity

DEFAULT_THRESHOLD = time(0, 30)


def due_local_date(now: datetime, zone: str, threshold: time = DEFAULT_THRESHOLD) -> date:
    """Pure: the latest local date of ``zone`` whose ``threshold`` has passed at instant ``now``."""
    local = now.astimezone(pytz.timezone(zone))
    shifted = local - timedelta(hours=threshold.hour, minutes=threshold.minute)
    return shifted.date()


def tenant_zones(db: Session, company_id: int, on: date) -> List[str]:
    """Distinct valid zones of placements current on ``on``, plus the company zone (sorted)."""
    company = db.get(Company, company_id)
    zones = {company.timezone} if company is not None and company.timezone else set()
    columns = [Branch.timezone, LegalEntity.timezone]
    work_tz = getattr(EmployeeAssignment, "work_timezone", None)
    if work_tz is not None:
        columns.insert(0, work_tz)
    rows = (db.query(*columns)
            .select_from(EmployeeAssignment)
            .outerjoin(Branch, (Branch.company_id == EmployeeAssignment.company_id)
                       & (Branch.id == EmployeeAssignment.branch_id))
            .outerjoin(LegalEntity, (LegalEntity.company_id == EmployeeAssignment.company_id)
                       & (LegalEntity.id == EmployeeAssignment.legal_entity_id))
            .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.is_primary.is_(True),
                    EmployeeAssignment.effective_from <= on,
                    or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= on))
            .distinct().all())
    for row in rows:
        zone = next((z for z in row if z), None)
        if zone:
            zones.add(zone)
    return sorted(z for z in zones if z in pytz.all_timezones_set)


def zones_due(db: Session, company_id: int, now: datetime, threshold: time = DEFAULT_THRESHOLD) -> Dict[str, date]:
    """{zone: local date due at ``now``} for every zone of the tenant."""
    on = now.date()
    return {zone: due_local_date(now, zone, threshold) for zone in tenant_zones(db, company_id, on)}
