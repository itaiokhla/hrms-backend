"""Pay runs API, calculation and review (3.9 §4 Runs) under /api/v1/payroll.

* POST runs/{id}/calculate (202): lock inputs and start the calculation job, or recalculate all or the
  selected employees (``employee_ids`` are employee ids). ``Idempotency-Key`` is required (the run guard
  IDEMPOTENCY_KEY); a repeat with the same key replays the first response, so one job runs (EC-RUN-02).
  ``row_version`` (optional) makes a second tab's click a 409.
* GET runs/{id}/summary, runs/{id}/employees, runs/{id}/employees/{eid}, runs/{id}/variance, runs/{id}/stale.
* POST runs/{id}/variance/{eid}/acknowledge: acknowledge an employee's variance flags (comment required
  for a net-pay change).
* POST runs/{id}/replay: recalculate from the stored snapshots and compare ``calc_hash``; never writes.

``{eid}`` is the run-employee id (``payroll_run_employees.id``). Responses of state-changing requests carry
ids, statuses and row versions only, so idempotency records hold no pay data.
"""

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.pay_runs import RUN_EMPLOYEE_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import calculation, idempotency, review

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll runs"])

EmployeeStatus = Literal[RUN_EMPLOYEE_STATUSES]  # type: ignore[valid-type]
EmployeeFilter = Literal[review.EMPLOYEE_FILTERS]  # type: ignore[valid-type]


class CalculateIn(StrictModel):
    employee_ids: Optional[List[int]] = Field(None, min_length=1, max_length=10000)
    row_version: Optional[int] = Field(None, ge=1)


class AcknowledgeIn(StrictModel):
    comment: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class ReplayIn(StrictModel):
    employee_ids: Optional[List[int]] = Field(None, min_length=1, max_length=review.REPLAY_LIMIT)


@router.post("/runs/{run_id}/calculate", status_code=202)
def calculate(run_id: int, request: Request, data: Optional[CalculateIn] = None,
              principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or CalculateIn()
    key = idempotency.key_from(request)

    def handler():
        return 202, calculation.calculate(db, principal, run_id, employee_ids=data.employee_ids, idempotency_key=key,
                                          expected_version=data.row_version)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/runs/{run_id}/summary")
def summary(run_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return review.summary(db, principal, run_id)


@router.get("/runs/{run_id}/employees")
def list_employees(
    run_id: int,
    search: Optional[str] = Query(None, max_length=100),
    filter: Optional[EmployeeFilter] = Query(None),  # noqa: A002 - the documented query parameter name
    status: Optional[EmployeeStatus] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return review.list_employees(db, principal, run_id, search=search, filter_name=filter, status=status, skip=skip,
                                 limit=limit)


@router.get("/runs/{run_id}/employees/{run_employee_id}")
def employee_detail(run_id: int, run_employee_id: int, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    return review.employee_detail(db, principal, run_id, run_employee_id)


@router.get("/runs/{run_id}/variance")
def variance(
    run_id: int,
    only_flagged: bool = Query(False),
    sort: Literal["percent", "absolute", "employee"] = Query("percent"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return review.variance(db, principal, run_id, only_flagged=only_flagged, sort=sort, skip=skip, limit=limit)


@router.post("/runs/{run_id}/variance/{run_employee_id}/acknowledge")
def acknowledge(run_id: int, run_employee_id: int, request: Request, data: Optional[AcknowledgeIn] = None,
                principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or AcknowledgeIn()

    def handler():
        return 200, review.acknowledge_variance(db, principal, run_id, run_employee_id, comment=data.comment,
                                                expected_version=data.row_version)

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/runs/{run_id}/stale")
def stale(run_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return review.stale(db, principal, run_id)


@router.post("/runs/{run_id}/replay")
def replay(run_id: int, data: Optional[ReplayIn] = None, skip: int = Query(0, ge=0),
           limit: int = Query(200, ge=1, le=review.REPLAY_LIMIT), principal: Principal = Depends(get_principal),
           db: Session = Depends(get_db)):
    """Never writes (EC-STA-04): no commit, the session is discarded with the request."""
    data = data or ReplayIn()
    result = review.replay(db, principal, run_id, employee_ids=data.employee_ids, skip=skip, limit=limit)
    db.rollback()
    return result
