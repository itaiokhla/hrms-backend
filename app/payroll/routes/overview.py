"""Payroll overview API (3.8 §5.1).

``GET /api/v1/payroll/overview`` returns the KPI set of the workspace home: total eligible, with and without
salary (they add up), processed, paid (settlement evidence only), on hold, gross / net / employer cost per
currency labelled Draft / Approved / Final by run state, statutory totals only for entities whose pack
defines them (EC-UX-07, labels from the pack), deadlines (input cutoff, approval deadline, pay date) and
open exceptions. ``GET /api/v1/payroll/overview/{kpi}`` is the drill-down list of one count KPI: both come
from the same scoped population query, so a count always equals its list (EC-UX-03).

Filters: period range (default: the current month of the tenant), legal entity, pay group, currency,
run status (FINAL, APPROVED, DRAFT, ALL or one run status; money only) and location (branch).
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import reports as service

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll overview"])

KpiName = Literal[service.OVERVIEW_LISTS]  # type: ignore[valid-type]
CURRENCY_PATTERN = r"^[A-Z]{3}$"


def overview_filters(
    period_from: Optional[date] = Query(None),
    period_to: Optional[date] = Query(None),
    legal_entity_id: Optional[int] = Query(None, ge=1),
    pay_group_id: Optional[int] = Query(None, ge=1),
    currency: Optional[str] = Query(None, pattern=CURRENCY_PATTERN),
    status: Optional[str] = Query(None, min_length=1, max_length=30, pattern=r"^[A-Z_]+$"),
    branch_id: Optional[int] = Query(None, ge=1),
    convert_to: Optional[str] = Query(None, pattern=CURRENCY_PATTERN),
    fx_date: Optional[date] = Query(None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> service.OverviewFilters:
    return service.overview_filters(db, principal, period_from=period_from, period_to=period_to,
                                    legal_entity_id=legal_entity_id, pay_group_id=pay_group_id, currency=currency,
                                    status=status, branch_id=branch_id, convert_to=convert_to, fx_date=fx_date)


@router.get("/overview")
def overview(filters: service.OverviewFilters = Depends(overview_filters),
             principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.overview_kpis(db, principal, filters)


@router.get("/overview/{kpi}")
def overview_list(kpi: KpiName,  # type: ignore[valid-type]
                  skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                  filters: service.OverviewFilters = Depends(overview_filters),
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.overview_list(db, principal, kpi, filters, skip=skip, limit=limit)
