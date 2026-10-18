from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from datetime import date
from app.database.db import get_db
from app.services.reports_service import ReportsService
from app.utils.excel_exporter import ExcelExporter
from app.utils.pdf_generator import AttendanceReportGenerator, PayrollReportGenerator, LeaveReportGenerator
from typing import Optional

router = APIRouter(prefix="/api/v1", tags=["reports"])


@router.get("/reports/attendance")
async def get_attendance_report(
    start_date: date = Query(...),
    end_date: date = Query(...),
    employee_id: int = Query(None),
    department_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Get attendance report for a date range"""
    return ReportsService.get_attendance_report(
        db, start_date, end_date, employee_id, department_id
    )


@router.get("/reports/attendance/export/excel")
async def export_attendance_excel(
    start_date: date = Query(...),
    end_date: date = Query(...),
    employee_id: int = Query(None),
    department_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Export attendance report to Excel"""
    report = ReportsService.get_attendance_report(
        db, start_date, end_date, employee_id, department_id
    )
    excel_file = ExcelExporter.export_attendance_report(report["records"])
    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=attendance_report_{start_date}_{end_date}.xlsx"
        },
    )


@router.get("/reports/attendance/export/pdf")
async def export_attendance_pdf(
    start_date: date = Query(...),
    end_date: date = Query(...),
    employee_id: int = Query(None),
    department_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Export attendance report to PDF"""
    report = ReportsService.get_attendance_report(
        db, start_date, end_date, employee_id, department_id
    )
    pdf_file = AttendanceReportGenerator.generate_pdf(report)
    return StreamingResponse(
        pdf_file,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=attendance_report_{start_date}_{end_date}.pdf"
        },
    )


@router.get("/reports/payroll")
async def get_payroll_report(
    year: int = Query(...),
    month: int = Query(None, ge=1, le=12),
    department_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Get payroll report for a period"""
    return ReportsService.get_payroll_report(db, year, month, department_id)


@router.get("/reports/payroll/export/excel")
async def export_payroll_excel(
    year: int = Query(...),
    month: int = Query(None, ge=1, le=12),
    department_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Export payroll report to Excel"""
    report = ReportsService.get_payroll_report(db, year, month, department_id)
    excel_file = ExcelExporter.export_payroll_report(report["records"])
    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=payroll_report_{year}_{month or 'all'}.xlsx"
        },
    )


@router.get("/reports/payroll/export/pdf")
async def export_payroll_pdf(
    year: int = Query(...),
    month: int = Query(None, ge=1, le=12),
    department_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Export payroll report to PDF"""
    report = ReportsService.get_payroll_report(db, year, month, department_id)
    pdf_file = PayrollReportGenerator.generate_pdf(report)
    return StreamingResponse(
        pdf_file,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=payroll_report_{year}_{month or 'all'}.pdf"
        },
    )


@router.get("/reports/leaves")
async def get_leave_report(
    year: int = Query(None),
    employee_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Get leave analytics report"""
    return ReportsService.get_leave_report(db, year, employee_id)


@router.get("/reports/leaves/export/excel")
async def export_leave_excel(
    year: int = Query(None),
    employee_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Export leave report to Excel"""
    report = ReportsService.get_leave_report(db, year, employee_id)
    excel_file = ExcelExporter.export_leave_report(report["records"])
    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=leave_report_{year or 'all'}.xlsx"
        },
    )


@router.get("/reports/leaves/export/pdf")
async def export_leave_pdf(
    year: int = Query(None),
    employee_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """Export leave report to PDF"""
    report = ReportsService.get_leave_report(db, year, employee_id)
    pdf_file = LeaveReportGenerator.generate_pdf(report)
    return StreamingResponse(
        pdf_file,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=leave_report_{year or 'all'}.pdf"
        },
    )


@router.get("/reports/department-analytics")
async def get_department_analytics(
    year: int = Query(None),
    month: int = Query(None, ge=1, le=12),
    db: Session = Depends(get_db),
):
    """Get department-wise analytics"""
    return ReportsService.get_department_analytics(db, year, month)


@router.get("/reports/employee-analytics/{employee_id}")
async def get_employee_analytics(
    employee_id: int,
    year: int = Query(None),
    db: Session = Depends(get_db),
):
    """Get individual employee analytics"""
    return ReportsService.get_employee_analytics(db, employee_id, year)


@router.get("/reports/dashboard")
async def get_dashboard_statistics(db: Session = Depends(get_db)):
    """Get dashboard statistics for quick overview"""
    return ReportsService.get_dashboard_statistics(db)


@router.get("/reports/summary")
async def get_report_summary(
    report_type: str = Query("all"),
    year: int = Query(None),
    month: int = Query(None, ge=1, le=12),
    db: Session = Depends(get_db),
):
    """Get comprehensive report summary"""
    summaries = {}

    if report_type in ["all", "attendance"]:
        from datetime import date as date_cls
        start = date_cls(year or date_cls.today().year, month or 1, 1)
        end = date_cls.today()
        summaries["attendance"] = ReportsService.get_attendance_report(db, start, end)

    if report_type in ["all", "payroll"]:
        summaries["payroll"] = ReportsService.get_payroll_report(db, year or date.today().year, month)

    if report_type in ["all", "leaves"]:
        summaries["leaves"] = ReportsService.get_leave_report(db, year or date.today().year)

    if report_type in ["all", "department"]:
        summaries["department"] = ReportsService.get_department_analytics(db, year or date.today().year, month)

    if report_type in ["all", "dashboard"]:
        summaries["dashboard"] = ReportsService.get_dashboard_statistics(db)

    return summaries
