"""Reconciliation API (3.9 §4 Reconciliation; 3.7 §9; 3.5 §5.10).

/api/v1/payroll/bank-statements/import (multipart: funding account, CSV with a column mapping or CAMT.053;
idempotent by file hash and line hash), /payroll/bank-statements, /payroll/bank-statements/{id}/matches,
/payroll/reconciliation-matches (queues) and .../{id}/accept|link|escalate, /payroll/reconciliation/overdue,
/payroll/runs/{id}/payments and /payroll/runs/{id}/payments/reconcile. All need payroll:payment:reconcile
(403) inside the funding account's legal entity (404 otherwise).
"""

import json
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.errors import bad_request
from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.api_types import StrictModel
from app.payroll.services import idempotency
from app.payroll.services import reconciliation as service

router = APIRouter(prefix="/api/v1", tags=["Reconciliation"])

Queue = Literal["review", "mismatch", "unmatched", "duplicate", "escalated", "resolved", "ignored"]


class ReasonIn(StrictModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


class LinkIn(ReasonIn):
    instruction_id: int = Field(..., ge=1)


class RunReconcileIn(StrictModel):
    reason: Optional[str] = Field(None, max_length=1000)
    row_version: Optional[int] = Field(None, ge=1)


def _if_match(request: Request, body_version: Optional[int]) -> Optional[int]:
    header = request.headers.get("if-match")
    if header:
        value = header.strip().removeprefix("W/").strip('"')
        if value.isdigit():
            return int(value)
    return body_version


def _match_result(data: dict) -> dict:
    return {k: data[k] for k in ("id", "status", "reason", "row_version", "decided_at")}


@router.post("/payroll/bank-statements/import", status_code=201)
async def import_statement(funding_account_id: int = Form(..., ge=1), format: Literal["CSV", "CAMT053"] = Form(...),
                           mapping: Optional[str] = Form(None, max_length=4000), file: UploadFile = File(...),
                           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """Import a statement and reconcile its new lines; the same file (or line) never imports twice (F-11)."""
    content = await file.read(service.MAX_STATEMENT_BYTES + 1)
    parsed_mapping = None
    if mapping:
        try:
            parsed_mapping = json.loads(mapping)
        except ValueError:
            raise bad_request("The column mapping must be a JSON object.", "MAPPING_INVALID")
    result = service.import_statement(db, principal, funding_account_id=funding_account_id, fmt=format,
                                      file_name=file.filename or "statement", content=content, mapping=parsed_mapping)
    db.commit()
    return result


@router.get("/payroll/bank-statements")
def list_statements(funding_account_id: Optional[int] = Query(None, ge=1), limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0), principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    return service.list_imports(db, principal, funding_account_id=funding_account_id, limit=limit, offset=offset)


@router.get("/payroll/bank-statements/{import_id}")
def get_statement(import_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.import_json(service.get_import(db, principal, import_id))


@router.get("/payroll/bank-statements/{import_id}/matches")
def statement_matches(import_id: int, queue: Optional[Queue] = Query(None), principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    return service.list_matches(db, principal, import_id=import_id, queue=queue)


@router.get("/payroll/reconciliation-matches")
def list_matches(queue: Optional[Queue] = Query(None), funding_account_id: Optional[int] = Query(None, ge=1),
                 limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.list_matches(db, principal, queue=queue, funding_account_id=funding_account_id, limit=limit,
                                offset=offset)


@router.post("/payroll/reconciliation-matches/{match_id}/accept")
def accept_variance(match_id: int, data: ReasonIn, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    """Accept an amount difference with a reason (EC-PAY-11, EC-MON-18): settles and records the variance."""
    def handler():
        return 200, _match_result(service.accept_variance(db, principal, match_id, data.reason,
                                                          expected_version=_if_match(request, data.row_version)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/reconciliation-matches/{match_id}/link")
def link_match(match_id: int, data: LinkIn, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    def handler():
        return 200, _match_result(service.link(db, principal, match_id, data.instruction_id, data.reason,
                                               expected_version=_if_match(request, data.row_version)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.post("/payroll/reconciliation-matches/{match_id}/escalate")
def escalate_match(match_id: int, data: ReasonIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    def handler():
        return 200, _match_result(service.escalate(db, principal, match_id, data.reason,
                                                   expected_version=_if_match(request, data.row_version)))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.get("/payroll/reconciliation/overdue")
def overdue(funding_account_id: Optional[int] = Query(None, ge=1), as_of: Optional[date] = Query(None),
            grace: int = Query(0, ge=0, le=60), principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)):
    return service.overdue(db, principal, funding_account_id=funding_account_id, as_of=as_of, grace=grace)


@router.get("/payroll/runs/{run_id}/payments")
def run_payments(run_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.run_payments(db, principal, run_id)


@router.post("/payroll/runs/{run_id}/payments/reconcile")
def reconcile_run(run_id: int, request: Request, data: Optional[RunReconcileIn] = None,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """PAID -> RECONCILED, or PARTIALLY_PAID -> RECONCILED with the unsettled items carried (decision I-17)."""
    data = data or RunReconcileIn()

    def handler():
        return 200, service.reconcile_run(db, principal, run_id, data.reason,
                                          expected_version=_if_match(request, data.row_version))

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())
