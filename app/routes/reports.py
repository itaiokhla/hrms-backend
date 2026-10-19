from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.errors import forbidden
from app.core.principal import Principal, require
from app.database.db import get_db
from app.services import reports_service
from app.services.audit_log_service import AuditLogService
from app.utils.excel_exporter import ExcelExporter
from app.utils.pdf_generator import AttendanceReportGenerator, LeaveReportGenerator, PayrollReportGenerator

router = APIRouter(prefix="/api/v1/reports", tags=["Reports"])

ExportFormat = Annotated[str, Path(pattern=r"^(excel|pdf)$")]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _download(content, media_type: str, filename: str) -> StreamingResponse:
    return StreamingResponse(content, media_type=media_type,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _export_guard(principal: Principal) -> None:
    if not principal.has("report:export"):
        raise forbidden()


@router.get("/attendance")
def attendance(start_date: date, end_date: date, employee_id: Optional[int] = None, department_id: Optional[int] = None,
               principal: Principal = Depends(require("report:read")), db: Session = Depends(get_db)):
    return reports_service.attendance_report(db, principal, start_date, end_date, employee_id, department_id)


@router.get("/attendance/export/{fmt}")
def export_attendance(fmt: ExportFormat, start_date: date, end_date: date, employee_id: Optional[int] = None,
                      department_id: Optional[int] = None, principal: Principal = Depends(require("report:export")),
                      db: Session = Depends(get_db)):
    report = reports_service.attendance_report(db, principal, start_date, end_date, employee_id, department_id)
    AuditLogService.log(db, principal, "Reports", "EXPORT", f"Attendance report {start_date}..{end_date} ({fmt})")
    db.commit()
    if fmt == "excel":
        return _download(ExcelExporter.export_attendance_report(report["records"]), XLSX, f"attendance_{start_date}_{end_date}.xlsx")
    return _download(AttendanceReportGenerator.generate_pdf(report), "application/pdf", f"attendance_{start_date}_{end_date}.pdf")


@router.get("/payroll")
def payroll(year: int = Query(..., ge=2000, le=2100), month: Optional[int] = Query(None, ge=1, le=12),
            department_id: Optional[int] = None, principal: Principal = Depends(require("report:read")),
            db: Session = Depends(get_db)):
    return reports_service.payroll_report(db, principal, year, month, department_id)


@router.get("/payroll/export/{fmt}")
def export_payroll(fmt: ExportFormat, year: int = Query(..., ge=2000, le=2100), month: Optional[int] = Query(None, ge=1, le=12),
                   department_id: Optional[int] = None, principal: Principal = Depends(require("report:export")),
                   db: Session = Depends(get_db)):
    if not principal.has_company_scope("payroll:export"):
        raise forbidden("Exporting payroll requires the Export payroll permission.")
    report = reports_service.payroll_report(db, principal, year, month, department_id)
    AuditLogService.log(db, principal, "Reports", "EXPORT", f"Payroll report {year}-{month or 'all'} ({fmt})")
    db.commit()
    if fmt == "excel":
        return _download(ExcelExporter.export_payroll_report(report["records"]), XLSX, f"payroll_{year}_{month or 'all'}.xlsx")
    return _download(PayrollReportGenerator.generate_pdf(report), "application/pdf", f"payroll_{year}_{month or 'all'}.pdf")


@router.get("/leaves")
def leaves(year: Optional[int] = Query(None, ge=2000, le=2100), employee_id: Optional[int] = None,
           principal: Principal = Depends(require("report:read")), db: Session = Depends(get_db)):
    return reports_service.leave_report(db, principal, year, employee_id)


@router.get("/leaves/export/{fmt}")
def export_leaves(fmt: ExportFormat, year: Optional[int] = Query(None, ge=2000, le=2100), employee_id: Optional[int] = None,
                  principal: Principal = Depends(require("report:export")), db: Session = Depends(get_db)):
    report = reports_service.leave_report(db, principal, year, employee_id)
    AuditLogService.log(db, principal, "Reports", "EXPORT", f"Leave report {year or 'current'} ({fmt})")
    db.commit()
    if fmt == "excel":
        return _download(ExcelExporter.export_leave_report(report["records"]), XLSX, f"leave_{year or 'current'}.xlsx")
    return _download(LeaveReportGenerator.generate_pdf(report), "application/pdf", f"leave_{year or 'current'}.pdf")


@router.get("/department-analytics")
def department_analytics(year: Optional[int] = Query(None, ge=2000, le=2100), month: Optional[int] = Query(None, ge=1, le=12),
                         principal: Principal = Depends(require("report:read")), db: Session = Depends(get_db)):
    return reports_service.department_analytics(db, principal, year, month)


@router.get("/employee-analytics/{employee_id}")
def employee_analytics(employee_id: int, year: Optional[int] = Query(None, ge=2000, le=2100),
                       principal: Principal = Depends(require("report:read")), db: Session = Depends(get_db)):
    return reports_service.employee_analytics(db, principal, employee_id, year)


@router.get("/dashboard")
def dashboard(principal: Principal = Depends(require("report:read")), db: Session = Depends(get_db)):
    return reports_service.dashboard_statistics(db, principal)
