"""Pay calendars and periods API (3.9 §4 Calendars) under /api/v1/payroll.

POST pay-groups/{id}/calendar validates the rules and, unless ``validate_only``, saves the calendar
and generates its periods 18 months ahead (a change of frequency or rules needs ``effective_from``).
"""

from datetime import date
from typing import Any, Dict, Literal, Optional, Union

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.pay_org import PAY_FREQUENCIES
from app.models.pay_runs import PERIOD_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import calendars as service
from app.payroll.services import idempotency

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll calendars"])

Frequency = Literal[PAY_FREQUENCIES]  # type: ignore[valid-type]
PeriodStatus = Literal[PERIOD_STATUSES]  # type: ignore[valid-type]
RuleValue = Union[int, str, None]


class CalendarIn(StrictModel):
    frequency: Frequency
    # CalendarRules fields (or their payroll.calendar.* setting keys); anything else is a 400.
    rules: Dict[str, RuleValue] = Field(default_factory=dict, max_length=20)
    effective_from: Optional[date] = None
    row_version: Optional[int] = Field(None, ge=1)
    validate_only: bool = False


@router.get("/pay-groups/{pay_group_id}/calendar")
def get_calendar(pay_group_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_calendar(db, principal, pay_group_id)


@router.post("/pay-groups/{pay_group_id}/calendar")
def save_calendar(pay_group_id: int, data: CalendarIn, request: Request, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)):
    rules: Dict[str, Any] = dict(data.rules)
    if data.validate_only:
        return service.validate_calendar(db, principal, pay_group_id, frequency=data.frequency, rules=rules,
                                         effective_from=data.effective_from)

    def handler():
        calendar = service.save_calendar(db, principal, pay_group_id, frequency=data.frequency, rules=rules,
                                         effective_from=data.effective_from, row_version=data.row_version)
        return 200, {"calendar": service.calendar_json(calendar),
                     "periods": service.period_count(db, principal.company_id, pay_group_id)}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/pay-groups/{pay_group_id}/periods")
def list_periods(
    pay_group_id: int,
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    status: Optional[PeriodStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = service.list_periods(db, principal, pay_group_id, start=start, end=end, status=status, skip=skip,
                                        limit=limit)
    return {"total": total, "items": items}


@router.get("/pay-periods/{period_id}/pay-date-check")
def pay_date_check(period_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Re-validate a stored pay date against today's banking calendar (EC-CAL-08)."""
    return service.pay_date_check(db, principal, period_id)
