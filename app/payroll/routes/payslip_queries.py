"""Payslip questions API (3.6; setting payslip.queries_enabled; app.payroll.services.payslip_queries).

Employees (own published payslips only; any other id is 404):
``POST /api/v1/me/payroll/payslips/{document_id}/questions`` (409 QUERIES_DISABLED when the setting is off),
``GET /api/v1/me/payroll/payslip-questions`` (own, filter status / document), ``GET …/{id}``,
``POST …/{id}/close``.

Staff (``payroll:payslip:read`` beyond SELF, legal entity / pay group scope; out-of-scope ids are 404):
``GET /api/v1/payroll/payslip-questions`` (filters status, employee, run, payslip, legal entity), ``GET …/{id}``;
``POST …/{id}/answer`` with ``payroll:payslip:publish`` (optionally closing it; never one's own payslip).

State-changing requests accept ``Idempotency-Key`` and ``row_version`` (409 on a stale version).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.core.rate_limit import limiter
from app.database.db import get_db
from app.models.payslip_queries import ANSWER_MAX, QUERY_STATUSES, QUESTION_MAX
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency
from app.payroll.services import payslip_queries as service

router = APIRouter(prefix="/api/v1", tags=["Payslip questions"])

QueryStatus = Literal[QUERY_STATUSES]  # type: ignore[valid-type]


class QuestionIn(StrictModel):
    question: str = Field(min_length=1, max_length=QUESTION_MAX)


class AnswerIn(StrictModel):
    answer: str = Field(min_length=1, max_length=ANSWER_MAX)
    close: bool = False
    row_version: Optional[int] = Field(None, ge=1)


class VersionIn(StrictModel):
    row_version: Optional[int] = Field(None, ge=1)


def _result(row) -> dict:
    return {"id": row.id, "document_id": row.document_id, "status": row.status, "row_version": row.row_version}


# ---------------------------------------------------------------------------- self-service


@router.post("/me/payroll/payslips/{document_id}/questions", status_code=201)
def ask_question(document_id: int, data: QuestionIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    limiter.check(f"payslip-question:{principal.company_id}:{principal.user_id}", 20, 3600)

    def handler():
        return 201, _result(service.ask(db, principal, document_id, data.question))

    return idempotency.execute(db, principal, request, handler, body={"document_id": document_id,
                                                                      **data.model_dump()})


@router.get("/me/payroll/payslip-questions")
def my_questions(
    status: Optional[QueryStatus] = Query(None),  # type: ignore[valid-type]
    document_id: Optional[int] = Query(None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return service.list_mine(db, principal, status=status, document_id=document_id, skip=skip, limit=limit)


@router.get("/me/payroll/payslip-questions/{query_id}")
def my_question(query_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_mine(db, principal, query_id)


@router.post("/me/payroll/payslip-questions/{query_id}/close")
def close_my_question(query_id: int, request: Request, data: Optional[VersionIn] = None,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data = data or VersionIn()

    def handler():
        return 200, _result(service.close_mine(db, principal, query_id, expected_version=data.row_version))

    return idempotency.execute(db, principal, request, handler, body={"query_id": query_id, **data.model_dump()})


# ---------------------------------------------------------------------------- staff


@router.get("/payroll/payslip-questions")
def list_questions(
    status: Optional[QueryStatus] = Query(None),  # type: ignore[valid-type]
    employee_id: Optional[int] = Query(None, ge=1),
    run_id: Optional[int] = Query(None, ge=1),
    document_id: Optional[int] = Query(None, ge=1),
    legal_entity_id: Optional[int] = Query(None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    return service.list_staff(db, principal, status=status, employee_id=employee_id, run_id=run_id,
                              document_id=document_id, legal_entity_id=legal_entity_id, skip=skip, limit=limit)


@router.get("/payroll/payslip-questions/{query_id}")
def get_question(query_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_staff(db, principal, query_id)


@router.post("/payroll/payslip-questions/{query_id}/answer")
def answer_question(query_id: int, data: AnswerIn, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    def handler():
        row = service.answer(db, principal, query_id, data.answer, close=data.close,
                             expected_version=data.row_version)
        return 200, _result(row)

    return idempotency.execute(db, principal, request, handler, body={"query_id": query_id, **data.model_dump()})
