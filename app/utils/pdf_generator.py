from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
)
from datetime import datetime
from io import BytesIO
from decimal import Decimal


def _hm(seconds) -> str:
    """Whole hours and minutes (seconds are truncated, never rounded up)."""
    if seconds is None:
        return "-"
    seconds = int(seconds)
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _clock(value) -> str:
    """HH:MM UTC from an ISO timestamp."""
    if not value:
        return "-"
    try:
        from datetime import timezone

        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%H:%M UTC")
    except ValueError:
        return str(value)


class SalarySlipGenerator:
    @staticmethod
    def generate_pdf(payroll_data: dict, employee_data: dict) -> BytesIO:
        """Generate salary slip PDF."""
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        story = []

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "CustomTitle",
            parent=styles["Heading1"],
            fontSize=16,
            textColor=colors.HexColor("#1e3a8a"),
            alignment=1,
            spaceAfter=6,
        )
        heading_style = ParagraphStyle(
            "CustomHeading",
            parent=styles["Heading2"],
            fontSize=11,
            textColor=colors.HexColor("#1f2937"),
            spaceAfter=6,
        )

        story.append(Paragraph("SALARY SLIP", title_style))
        story.append(Spacer(1, 0.2 * inch))

        emp_info_data = [
            ["Employee Name:", employee_data.get("name", "N/A")],
            ["Employee ID:", employee_data.get("employee_id", "N/A")],
            ["Department:", employee_data.get("department", "N/A")],
            ["Position:", employee_data.get("position", "N/A")],
        ]

        emp_info_table = Table(emp_info_data, colWidths=[2 * inch, 4 * inch])
        emp_info_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6")),
                    ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                ]
            )
        )
        story.append(emp_info_table)
        story.append(Spacer(1, 0.2 * inch))

        period = payroll_data.get("salary_month", "N/A")
        story.append(Paragraph(f"<b>Salary Period: {period}</b>", heading_style))
        story.append(Spacer(1, 0.1 * inch))

        earnings_data = [
            ["Description", "Amount"],
            ["Basic Salary", f"₹{payroll_data.get('basic_salary', 0):,.2f}"],
            ["HRA", f"₹{payroll_data.get('hra', 0):,.2f}"],
            ["Dearness Allowance", f"₹{payroll_data.get('da', 0):,.2f}"],
            ["Conveyance", f"₹{payroll_data.get('conveyance', 0):,.2f}"],
            ["Other Allowances", f"₹{payroll_data.get('other_allowances', 0):,.2f}"],
            ["Overtime", f"₹{payroll_data.get('overtime_amount', 0):,.2f}"],
            ["Gross Salary", f"₹{payroll_data.get('gross_salary', 0):,.2f}"],
        ]

        earnings_table = Table(earnings_data, colWidths=[3.5 * inch, 2.5 * inch])
        earnings_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                    ("ALIGN", (0, 0), (0, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                    ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f3f4f6")),
                ]
            )
        )
        story.append(earnings_table)
        story.append(Spacer(1, 0.15 * inch))

        deductions_data = [
            ["Deductions", "Amount"],
            ["Absent Days", f"₹{payroll_data.get('absent_deduction', 0):,.2f}"],
            ["Half Day Deduction", f"₹{payroll_data.get('half_day_deduction', 0):,.2f}"],
            ["Late Penalty", f"₹{payroll_data.get('late_penalty', 0):,.2f}"],
            ["Total Deductions", f"₹{payroll_data.get('total_deductions', 0):,.2f}"],
        ]

        deductions_table = Table(deductions_data, colWidths=[3.5 * inch, 2.5 * inch])
        deductions_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#991b1b")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                    ("ALIGN", (0, 0), (0, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                    ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#fecaca")),
                ]
            )
        )
        story.append(deductions_table)
        story.append(Spacer(1, 0.15 * inch))

        net_salary = payroll_data.get("net_salary", 0)
        net_data = [["Net Salary", f"₹{net_salary:,.2f}"]]

        net_table = Table(net_data, colWidths=[3.5 * inch, 2.5 * inch])
        net_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#065f46")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 11),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                ]
            )
        )
        story.append(net_table)
        story.append(Spacer(1, 0.3 * inch))

        attendance_data = [
            ["Attendance", "Days"],
            ["Total Working Days", str(payroll_data.get("working_days", 0))],
            ["Present", str(payroll_data.get("present_days", 0))],
            ["Absent", str(payroll_data.get("absent_days", 0))],
        ]

        attendance_table = Table(attendance_data, colWidths=[3.5 * inch, 2.5 * inch])
        attendance_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7c2d12")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                ]
            )
        )
        story.append(attendance_table)
        story.append(Spacer(1, 0.3 * inch))

        footer_text = f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        story.append(Paragraph(f"<i>{footer_text}</i>", styles["Normal"]))

        doc.build(story)
        buffer.seek(0)
        return buffer


class AttendanceReportGenerator:
    @staticmethod
    def generate_pdf(report: dict) -> BytesIO:
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        story = []

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "Title",
            parent=styles["Heading1"],
            fontSize=16,
            textColor=colors.HexColor("#1e40af"),
            alignment=1,
            spaceAfter=12,
        )
        story.append(Paragraph("Attendance Report", title_style))

        story.append(Paragraph(f"Period: {report.get('period', 'N/A')}", styles["Normal"]))
        story.append(Spacer(1, 0.2 * inch))

        summary = report.get("summary", {})
        summary_data = [["Metric", "Value"]]
        for key, value in summary.items():
            if str(key).endswith("_seconds"):
                summary_data.append([str(key)[: -len("_seconds")].replace("_", " ").title(), _hm(value)])
            else:
                summary_data.append([str(key).replace("_", " ").title(), str(value)])

        summary_table = Table(summary_data, colWidths=[3 * inch, 3 * inch])
        summary_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                ]
            )
        )
        story.append(summary_table)
        story.append(Spacer(1, 0.3 * inch))

        records = report.get("records", [])
        if records:
            table_data = [["Employee", "Date", "Status", "Check-In", "Check-Out", "Lunch", "Net"]]
            for record in records:
                table_data.append([
                    record.get("employee_name", "N/A"),
                    record.get("date", "N/A"),
                    record.get("status", "N/A"),
                    _clock(record.get("check_in_time")),
                    _clock(record.get("check_out_time")),
                    _hm(record.get("lunch_seconds")),
                    _hm(record.get("net_seconds")),
                ])

            table = Table(table_data, colWidths=[1.7 * inch, 0.9 * inch, 0.8 * inch, 0.9 * inch, 0.9 * inch, 0.7 * inch, 0.7 * inch])
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                    ]
                )
            )
            story.append(table)
        else:
            story.append(Paragraph("No attendance records available for this period.", styles["Normal"]))

        story.append(Spacer(1, 0.3 * inch))
        story.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Italic"]))
        doc.build(story)
        buffer.seek(0)
        return buffer


class PayrollReportGenerator:
    @staticmethod
    def generate_pdf(report: dict) -> BytesIO:
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        story = []

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "Title",
            parent=styles["Heading1"],
            fontSize=16,
            textColor=colors.HexColor("#047857"),
            alignment=1,
            spaceAfter=12,
        )
        story.append(Paragraph("Payroll Report", title_style))
        story.append(Paragraph(f"Period: {report.get('period', 'N/A')}", styles["Normal"]))
        story.append(Spacer(1, 0.2 * inch))

        summary = report.get("summary", {})
        summary_data = [["Metric", "Amount"]]
        for key, value in summary.items():
            summary_data.append([str(key).replace("_", " ").title(), f"₹{float(value):,.2f}"])

        summary_table = Table(summary_data, colWidths=[3 * inch, 3 * inch])
        summary_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#065f46")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                ]
            )
        )
        story.append(summary_table)
        story.append(Spacer(1, 0.3 * inch))

        records = report.get("records", [])
        if records:
            table_data = [["Employee", "Month", "Gross", "Deductions", "Net", "Status"]]
            for record in records:
                table_data.append([
                    record.get("employee_name", "N/A"),
                    record.get("salary_month", "N/A"),
                    f"₹{record.get('gross_salary', 0):,.2f}",
                    f"₹{record.get('total_deductions', 0):,.2f}",
                    f"₹{record.get('net_salary', 0):,.2f}",
                    record.get("status", "N/A"),
                ])

            table = Table(table_data, colWidths=[2 * inch, 1.2 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch, 1.4 * inch])
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#164e63")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                    ]
                )
            )
            story.append(table)
        else:
            story.append(Paragraph("No payroll records available for this period.", styles["Normal"]))

        story.append(Spacer(1, 0.3 * inch))
        story.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Italic"]))
        doc.build(story)
        buffer.seek(0)
        return buffer


class LeaveReportGenerator:
    @staticmethod
    def generate_pdf(report: dict) -> BytesIO:
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        story = []

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "Title",
            parent=styles["Heading1"],
            fontSize=16,
            textColor=colors.HexColor("#9d174d"),
            alignment=1,
            spaceAfter=12,
        )
        story.append(Paragraph("Leave Report", title_style))
        story.append(Paragraph(f"Period: {report.get('period', 'N/A')}", styles["Normal"]))
        story.append(Spacer(1, 0.2 * inch))

        summary = report.get("summary", {})
        summary_data = [["Type", "Applied", "Approved", "Rejected"]]
        for leave_type, stats in summary.items():
            summary_data.append([
                leave_type.replace("_", " ").title(),
                str(stats.get("applied", 0)),
                str(stats.get("approved", 0)),
                str(stats.get("rejected", 0)),
            ])

        summary_table = Table(summary_data, colWidths=[2.2 * inch, 1.2 * inch, 1.2 * inch, 1.2 * inch])
        summary_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7c2d12")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                ]
            )
        )
        story.append(summary_table)
        story.append(Spacer(1, 0.3 * inch))

        records = report.get("records", [])
        if records:
            table_data = [["Employee", "Type", "From", "To", "Days", "Status"]]
            for record in records:
                table_data.append([
                    record.get("employee_name", "N/A"),
                    record.get("leave_type", "N/A"),
                    record.get("start_date", "N/A"),
                    record.get("end_date", "N/A"),
                    str(record.get("days", 0)),
                    record.get("status", "N/A"),
                ])

            table = Table(table_data, colWidths=[1.8 * inch, 1.4 * inch, 1.1 * inch, 1.1 * inch, 0.9 * inch, 1.2 * inch])
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#881337")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("GRID", (0, 0), (-1, -1), 1, colors.grey),
                    ]
                )
            )
            story.append(table)
        else:
            story.append(Paragraph("No leave records available for this period.", styles["Normal"]))

        story.append(Spacer(1, 0.3 * inch))
        story.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Italic"]))
        doc.build(story)
        buffer.seek(0)
        return buffer
