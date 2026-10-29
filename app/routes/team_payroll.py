"""Team payroll (managers prepare, payroll commits) and payroll-cost invoices.

``GET /payroll/team`` · ``GET /payroll/team/components`` · ``GET|POST /payroll/team/batches`` ·
``GET|PATCH /payroll/team/batches/{id}`` · ``POST …/{id}/submit|withdraw|approve|reject|return`` ·
``POST …/{id}/files`` · ``GET|DELETE …/{id}/files/{file_id}`` · ``GET /payroll/cost-invoices/preview`` ·
``GET|POST /payroll/cost-invoices`` · ``GET /payroll/cost-invoices/{id}`` · ``POST …/{id}/issue|void`` ·
``GET …/{id}/pdf``. Permissions are checked by the services; writes go through ``idempotency.execute``.
"""

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.payroll.services import cost_invoices, idempotency
from app.services import team_payroll as service

router = APIRouter(prefix="/api/v1", tags=["Team payroll"])


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineIn(_Strict):
    employee_id: int = Field(..., ge=1)
    component_code: str = Field(..., min_length=1, max_length=40)
    amount: Optional[str] = Field(None, pattern=r"^-?\d{1,12}(\.\d{1,2})?$")
    units: Optional[str] = Field(None, pattern=r"^\d{1,8}(\.\d{1,2})?$")
    rate: Optional[str] = Field(None, pattern=r"^\d{1,10}(\.\d{1,4})?$")
    reason: str = Field(..., min_length=3, max_length=500)


class BatchIn(_Strict):
    title: str = Field(..., min_length=3, max_length=120)
    period_start: date
    period_end: date
    notes: Optional[str] = Field(None, max_length=2000)
    lines: List[LineIn] = Field(default_factory=list, max_length=service.MAX_LINES)


class BatchPatch(_Strict):
    title: Optional[str] = Field(None, min_length=3, max_length=120)
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    notes: Optional[str] = Field(None, max_length=2000)
    lines: Optional[List[LineIn]] = Field(None, max_length=service.MAX_LINES)


class NoteIn(_Strict):
    note: Optional[str] = Field(None, max_length=500)


class CostInvoiceIn(_Strict):
    run_id: int = Field(..., ge=1)
    group_by: Literal["DEPARTMENT", "BRANCH", "TEAM", "LEGAL_ENTITY", "PAY_GROUP", "COMPANY"] = "COMPANY"
    group_id: Optional[int] = Field(None, ge=1)
    fee_percent: Optional[str] = Field(None, pattern=r"^\d{1,3}(\.\d{1,3})?$")
    taxes: List[Dict[str, Any]] = Field(default_factory=list, max_length=10)
    bill_to: Dict[str, Optional[str]] = Field(default_factory=dict)
    due_date: Optional[date] = None
    notes: Optional[str] = Field(None, max_length=2000)


class IssueIn(_Strict):
    row_version: Optional[int] = Field(None, ge=1)


class VoidIn(_Strict):
    reason: str = Field(..., min_length=3, max_length=500)


def _write(db: Session, principal: Principal, request: Request, body: Any, run, status_code: int = 200):
    payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else (body or {})
    return idempotency.execute(db, principal, request, lambda: (status_code, run()), body=payload)


def _lines(lines) -> list:
    return [line.model_dump() for line in lines]


# ----------------------------------------------------------------------------------------------- team view
@router.get("/payroll/team")
def team(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.team_view(db, principal)


@router.get("/payroll/team/components")
def team_components(on: Optional[date] = None, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    return {"items": service.components(db, principal, on)}


# ----------------------------------------------------------------------------------------------- batches
@router.get("/payroll/team/batches")
def list_batches(box: Literal["mine", "approvals", "all"] = "mine",
                 status: Optional[str] = Query(None, max_length=10),
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.list_batches(db, principal, box=box, status=status)


@router.post("/payroll/team/batches", status_code=201)
def create_batch(data: BatchIn, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def run():
        body = data.model_dump(mode="python")
        body["lines"] = _lines(data.lines)
        row = service.create(db, principal, body)
        return service.get(db, principal, row.id)

    return _write(db, principal, request, data, run, 201)


@router.get("/payroll/team/batches/{batch_id:int}")
def get_batch(batch_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get(db, principal, batch_id)


@router.patch("/payroll/team/batches/{batch_id:int}")
def update_batch(batch_id: int, data: BatchPatch, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def run():
        body = data.model_dump(exclude_unset=True, mode="python")
        if data.lines is not None:
            body["lines"] = _lines(data.lines)
        service.update(db, principal, batch_id, body)
        return service.get(db, principal, batch_id)

    return _write(db, principal, request, data, run)


@router.post("/payroll/team/batches/{batch_id:int}/submit")
def submit_batch(batch_id: int, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    return _write(db, principal, request, {}, lambda: (service.submit(db, principal, batch_id),
                                                       service.get(db, principal, batch_id))[1])


@router.post("/payroll/team/batches/{batch_id:int}/withdraw")
def withdraw_batch(batch_id: int, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    return _write(db, principal, request, {}, lambda: (service.withdraw(db, principal, batch_id),
                                                       service.get(db, principal, batch_id))[1])


@router.post("/payroll/team/batches/{batch_id:int}/files", status_code=201)
async def add_file(batch_id: int, file: UploadFile = File(...), principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    data = await file.read(service.MAX_FILE + 1)
    row = service.add_file(db, principal, batch_id, file.filename or "file", file.content_type or "", data)
    db.commit()
    return {"id": row.id, "file_name": row.file_name, "size_bytes": row.size_bytes, "content_type": row.content_type}


@router.get("/payroll/team/batches/{batch_id:int}/files/{file_id:int}")
def get_file(batch_id: int, file_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    row = service.get_file(db, principal, batch_id, file_id)
    return Response(row.content, media_type=row.content_type,
                    headers={"Content-Disposition": f'attachment; filename="{row.file_name}"',
                             "X-Content-Type-Options": "nosniff"})


@router.delete("/payroll/team/batches/{batch_id:int}/files/{file_id:int}", status_code=204)
def delete_file(batch_id: int, file_id: int, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    service.remove_file(db, principal, batch_id, file_id)
    db.commit()
    return Response(status_code=204)


# Registered after the /files routes: "/{decision}" would otherwise capture POST …/files.
@router.post("/payroll/team/batches/{batch_id:int}/{decision}")
def decide_batch(batch_id: int, decision: Literal["approve", "reject", "return"], data: NoteIn, request: Request,
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: (
        service.decide(db, principal, batch_id, decision.upper(), data.note), service.get(db, principal, batch_id))[1])



# ----------------------------------------------------------------------------------------------- cost invoices
@router.get("/payroll/cost-invoices/preview")
def cost_preview(run_id: int = Query(..., ge=1),
                 group_by: Literal["DEPARTMENT", "BRANCH", "TEAM", "LEGAL_ENTITY", "PAY_GROUP", "COMPANY"] = "COMPANY",
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return cost_invoices.preview(db, principal, run_id, group_by)


@router.get("/payroll/cost-invoices")
def cost_list(run_id: Optional[int] = Query(None, ge=1), status: Optional[str] = Query(None, max_length=8),
              principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return {"items": cost_invoices.list_invoices(db, principal, run_id=run_id, status=status)}


@router.post("/payroll/cost-invoices", status_code=201)
def cost_create(data: CostInvoiceIn, request: Request, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    def run():
        body = data.model_dump(mode="python")
        return cost_invoices.get(db, principal, cost_invoices.create(db, principal, body).id)

    return _write(db, principal, request, data, run, 201)


@router.get("/payroll/cost-invoices/{invoice_id:int}")
def cost_get(invoice_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return cost_invoices.get(db, principal, invoice_id)


@router.post("/payroll/cost-invoices/{invoice_id:int}/issue")
def cost_issue(invoice_id: int, data: IssueIn, request: Request, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: cost_invoices.get(
        db, principal, cost_invoices.issue(db, principal, invoice_id, data.row_version).id))


@router.post("/payroll/cost-invoices/{invoice_id:int}/void")
def cost_void(invoice_id: int, data: VoidIn, request: Request, principal: Principal = Depends(get_principal),
              db: Session = Depends(get_db)):
    return _write(db, principal, request, data, lambda: cost_invoices.get(
        db, principal, cost_invoices.void(db, principal, invoice_id, data.reason).id))


@router.get("/payroll/cost-invoices/{invoice_id:int}/pdf")
def cost_pdf(invoice_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data, name = cost_invoices.pdf(db, principal, invoice_id)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})
