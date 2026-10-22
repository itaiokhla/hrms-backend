"""Payroll reports API (3.8 §5.13) and accounting journals (3.1 §5.18, 3.3 §2.2).

* ``GET /api/v1/payroll/reports``: the catalogue the caller may run, grouped by purpose.
* ``GET /api/v1/payroll/reports/{code}/preview``: the first 50 rows, the full row count, per-currency
  totals (or a converted total labelled with its FX source, rate and date) and notes (suppressed groups).
* ``POST /api/v1/payroll/reports/{code}/exports``: queue a CSV / XLSX / PDF export (202, background job);
  ``GET /api/v1/payroll/report-exports[/{id}[/download]]``: the caller's own exports only.
* ``/api/v1/payroll/accounting/journals``: posted or failed journals of runs in scope, their GENERIC_CSV
  file, and a manual post / retry of a run's journal; ``/api/v1/payroll/accounting/gl-mappings``: the GL
  accounts of components without their own ``gl`` (pack-owned statutory components).

Every filter is applied inside the caller's access scope (the services own that), so counts and totals
never include out-of-scope rows (EC-SEC-05). Unknown or out-of-scope objects are 404, a missing permission
403. State-changing responses are compact (ids, statuses, row versions).
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.principal import Principal, get_principal
from app.database.db import get_db
from app.models.reports_compliance import JOURNAL_STATUSES
from app.payroll.api_types import StrictModel
from app.payroll.services import accounting, idempotency
from app.payroll.services import reports as service

router = APIRouter(prefix="/api/v1/payroll", tags=["Payroll reports"])

FILE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
FormatName = Literal[service.FORMATS]  # type: ignore[valid-type]
GroupBy = Literal[service.GROUP_BY]  # type: ignore[valid-type]
JournalStatus = Literal[JOURNAL_STATUSES]  # type: ignore[valid-type]
CURRENCY_PATTERN = r"^[A-Z]{3}$"


class ReportFiltersIn(StrictModel):
    """Report filters (the same names as the preview query parameters)."""

    period_from: Optional[date] = None
    period_to: Optional[date] = None
    legal_entity_id: Optional[int] = Field(None, ge=1)
    pay_group_id: Optional[int] = Field(None, ge=1)
    department_id: Optional[int] = Field(None, ge=1)
    cost_center_id: Optional[int] = Field(None, ge=1)
    branch_id: Optional[int] = Field(None, ge=1)
    currency: Optional[str] = Field(None, pattern=CURRENCY_PATTERN)
    status: Optional[str] = Field(None, min_length=1, max_length=30, pattern=r"^[A-Z_]+$")
    group_by: Optional[GroupBy] = None  # type: ignore[valid-type]
    convert_to: Optional[str] = Field(None, pattern=CURRENCY_PATTERN)
    fx_date: Optional[date] = None
    tax_year: Optional[str] = Field(None, min_length=4, max_length=10)

    def to_filters(self) -> service.Filters:
        return service.Filters(**self.model_dump())


class ExportIn(StrictModel):
    format: FormatName  # type: ignore[valid-type]
    filters: ReportFiltersIn = Field(default_factory=ReportFiltersIn)


class GlMappingIn(StrictModel):
    component_code: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
    legal_entity_id: Optional[int] = Field(None, ge=1)
    debit_account: str = Field(min_length=1, max_length=60)
    credit_account: str = Field(min_length=1, max_length=60)
    allocate_by_cost_center: bool = False
    row_version: Optional[int] = Field(None, ge=1)

    @field_validator("debit_account", "credit_account")
    @classmethod
    def _account(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Give an account.")
        return value.strip()


def report_filters(
    period_from: Optional[date] = Query(None),
    period_to: Optional[date] = Query(None),
    legal_entity_id: Optional[int] = Query(None, ge=1),
    pay_group_id: Optional[int] = Query(None, ge=1),
    department_id: Optional[int] = Query(None, ge=1),
    cost_center_id: Optional[int] = Query(None, ge=1),
    branch_id: Optional[int] = Query(None, ge=1),
    currency: Optional[str] = Query(None, pattern=CURRENCY_PATTERN),
    status: Optional[str] = Query(None, min_length=1, max_length=30, pattern=r"^[A-Z_]+$"),
    group_by: Optional[GroupBy] = Query(None),  # type: ignore[valid-type]
    convert_to: Optional[str] = Query(None, pattern=CURRENCY_PATTERN),
    fx_date: Optional[date] = Query(None),
    tax_year: Optional[str] = Query(None, min_length=4, max_length=10),
) -> service.Filters:
    return service.Filters(period_from=period_from, period_to=period_to, legal_entity_id=legal_entity_id,
                           pay_group_id=pay_group_id, department_id=department_id, cost_center_id=cost_center_id,
                           branch_id=branch_id, currency=currency, status=status, group_by=group_by,
                           convert_to=convert_to, fx_date=fx_date, tax_year=tax_year)


# ---------------------------------------------------------------------------
# Catalogue, preview, exports
# ---------------------------------------------------------------------------
@router.get("/reports")
def catalogue(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.catalogue(db, principal)


@router.get("/reports/{code}/preview")
def preview(code: str, filters: service.Filters = Depends(report_filters),
            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.preview(db, principal, code, filters)


@router.post("/reports/{code}/exports", status_code=202)
def request_export(code: str, data: ExportIn, request: Request, principal: Principal = Depends(get_principal),
                   db: Session = Depends(get_db)):
    def handler():
        job = service.request_export(db, principal, code, data.filters.to_filters(), data.format)
        return 202, {"id": job.id, "job_id": job.id, "status": "QUEUED", "report_code": code,
                     "format": data.format}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump(mode="json"))


@router.get("/report-exports")
def list_exports(limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    return {"items": service.list_exports(db, principal, limit=limit)}


@router.get("/report-exports/{export_id}")
def get_export(export_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return service.get_export(db, principal, export_id)


@router.get("/report-exports/{export_id}/download")
def download_export(export_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data, file_name, media_type = service.export_file(db, principal, export_id)
    db.commit()  # the download audit event
    return Response(data, media_type=media_type,
                    headers={**FILE_HEADERS, "Content-Disposition": f'attachment; filename="{file_name}"'})


# ---------------------------------------------------------------------------
# Accounting journals
# ---------------------------------------------------------------------------
@router.get("/accounting/journals")
def list_journals(
    run_id: Optional[int] = Query(None, ge=1),
    status: Optional[JournalStatus] = Query(None),  # type: ignore[valid-type]
    legal_entity_id: Optional[int] = Query(None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
):
    total, items = accounting.list_journals(db, principal, run_id=run_id, status=status,
                                            legal_entity_id=legal_entity_id, skip=skip, limit=limit)
    return {"total": total, "items": items}


@router.get("/accounting/journals/{journal_id}")
def get_journal(journal_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    return accounting.serialize(db, accounting.get_journal(db, principal, journal_id), detail=True)


@router.get("/accounting/journals/{journal_id}/export")
def export_journal(journal_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    data, journal = accounting.export_journal(db, principal, journal_id)
    db.commit()  # the export audit event
    return Response(data, media_type="text/csv",
                    headers={**FILE_HEADERS, "Content-Disposition": f'attachment; filename="{journal.number}.csv"'})


@router.post("/runs/{run_id}/journal")
def post_journal(run_id: int, request: Request, principal: Principal = Depends(get_principal),
                 db: Session = Depends(get_db)):
    def handler():
        journal = accounting.post_manually(db, principal, run_id)
        return 200, {"id": journal.id, "number": journal.number, "run_id": journal.run_id,
                     "status": journal.status, "row_version": journal.row_version,
                     "errors": [e.get("code") for e in journal.errors or []]}

    return idempotency.execute(db, principal, request, handler)


@router.get("/accounting/gl-mappings")
def list_gl_mappings(legal_entity_id: Optional[int] = Query(None, ge=1), principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)):
    return {"items": accounting.list_mappings(db, principal, legal_entity_id=legal_entity_id)}


@router.put("/accounting/gl-mappings")
def save_gl_mapping(data: GlMappingIn, request: Request, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    def handler():
        row = accounting.upsert_mapping(
            db, principal, component_code=data.component_code, debit_account=data.debit_account,
            credit_account=data.credit_account, allocate_by_cost_center=data.allocate_by_cost_center,
            legal_entity_id=data.legal_entity_id, expected_version=data.row_version)
        return 200, {"id": row.id, "component_code": row.component_code, "legal_entity_id": row.legal_entity_id,
                     "row_version": row.row_version}

    return idempotency.execute(db, principal, request, handler, body=data.model_dump())


@router.delete("/accounting/gl-mappings/{mapping_id}", status_code=204)
def delete_gl_mapping(mapping_id: int, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    accounting.delete_mapping(db, principal, mapping_id)
    db.commit()
    return Response(status_code=204)
