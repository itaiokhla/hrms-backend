"""PDF documents: branded payslips and report exports.

All documents embed the Noto Sans family (app/utils/pdf_fonts.py), so currency
symbols such as ₹ and Arabic or Devanagari names render correctly. Payslips are
rendered only from a frozen snapshot (brand, employer, figures), which is what makes
a reprint identical to the original even after the brand changes.
"""

from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch, mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.utils import pdf_fonts
from app.utils.pdf_fonts import SANS, SANS_BOLD, markup, money

PAYSLIP_TEMPLATE_VERSION = "PAYSLIP_V2"


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
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%H:%M UTC")
    except ValueError:
        return str(value)


def _styles():
    pdf_fonts.register()
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle("t", parent=base["Heading1"], fontName=SANS_BOLD, fontSize=16, leading=20, spaceAfter=4),
        "h": ParagraphStyle("h", parent=base["Heading2"], fontName=SANS_BOLD, fontSize=11, leading=14, spaceAfter=4),
        "body": ParagraphStyle("b", parent=base["Normal"], fontName=SANS, fontSize=9, leading=12),
        "small": ParagraphStyle("s", parent=base["Normal"], fontName=SANS, fontSize=7.5, leading=10,
                                textColor=colors.HexColor("#4B5563")),
        "right": ParagraphStyle("r", parent=base["Normal"], fontName=SANS, fontSize=9, leading=12, alignment=TA_RIGHT),
        "right_bold": ParagraphStyle("rb", parent=base["Normal"], fontName=SANS_BOLD, fontSize=9, leading=12,
                                     alignment=TA_RIGHT),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontName=SANS, fontSize=9, leading=12, alignment=TA_LEFT),
        "cell_bold": ParagraphStyle("cb", parent=base["Normal"], fontName=SANS_BOLD, fontSize=9, leading=12),
        "head_white": ParagraphStyle("hw", parent=base["Normal"], fontName=SANS_BOLD, fontSize=9, leading=12,
                                     textColor=colors.white),
        "head_white_right": ParagraphStyle("hwr", parent=base["Normal"], fontName=SANS_BOLD, fontSize=9, leading=12,
                                           textColor=colors.white, alignment=TA_RIGHT),
    }
    # HarfBuzz shaping (joined Arabic in right-to-left order, Devanagari conjuncts) only runs
    # when a style asks for it.
    for style in styles.values():
        style.shaping = 1
    return styles


def _p(text, style, bold: bool = False) -> Paragraph:
    return Paragraph(markup(text, bold), style)


class SalarySlipGenerator:
    @staticmethod
    def generate_pdf(snapshot: dict, logo_bytes: Optional[bytes] = None) -> BytesIO:
        """Render a payslip from its frozen snapshot (deterministic for the same input)."""
        st = _styles()
        brand = snapshot.get("brand") or {}
        employer = snapshot.get("employer") or {}
        employee = snapshot.get("employee") or {}
        currency = snapshot.get("currency")
        accent = colors.HexColor(brand.get("accent") or "#3730A3")
        strip = colors.HexColor(brand.get("accent_strip") or brand.get("accent") or "#4F46E5")
        draft = snapshot.get("status") == "draft"
        buffer = BytesIO()
        title = f"Payslip {snapshot.get('period', '')} - {employee.get('name', '')}"
        doc = SimpleDocTemplate(
            buffer, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm, bottomMargin=18 * mm,
            title=title, author=employer.get("legal_name") or brand.get("display_name") or "",
            subject="Payslip", creator=brand.get("product_name") or "",
            # No embedded timestamps or random ids: the same snapshot always gives the same bytes.
            invariant=1,
        )
        width = A4[0] - 32 * mm
        story = []

        # Header: logo (or company name) | employer identity
        logo_cell = _p(brand.get("display_name") or employer.get("legal_name"), st["title"], bold=True)
        if logo_bytes:
            try:
                img = Image(BytesIO(logo_bytes))
                ratio = img.imageWidth / float(img.imageHeight or 1)
                height = 16 * mm
                img.drawHeight, img.drawWidth = height, min(height * ratio, width * 0.45)
                if img.drawWidth < height * ratio:
                    img.drawHeight = img.drawWidth / ratio
                img.hAlign = "LEFT"
                logo_cell = img
            except Exception:  # damaged logo file: fall back to the name (never fail a payslip)
                pass
        address = employer.get("address") or {}
        address_line = ", ".join(
            v for v in (address.get("line1"), address.get("line2"), address.get("city"), address.get("postal_code"),
                        address.get("country_code") or employer.get("country_code")) if v
        )
        identity = [_p(employer.get("legal_name") or brand.get("display_name"), st["right_bold"], bold=True)]
        if employer.get("trade_name"):
            identity.append(_p(employer["trade_name"], st["right"]))
        if address_line:
            identity.append(_p(address_line, st["right"]))
        for line in employer.get("registration_lines") or []:
            identity.append(_p(f"{line.get('label')}: {line.get('value')}", st["right"]))
        header = Table([[logo_cell, identity]], colWidths=[width * 0.5, width * 0.5])
        header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        story.append(header)
        story.append(Spacer(1, 3 * mm))
        accent_strip = Table([[""]], colWidths=[width], rowHeights=[2.2 * mm])
        accent_strip.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), strip)]))
        story.append(accent_strip)
        story.append(Spacer(1, 4 * mm))
        heading = f"Payslip · {snapshot.get('period', '')}" + ("  (DRAFT - not issued)" if draft else "")
        story.append(_p(heading, st["title"], bold=True))
        story.append(Spacer(1, 2 * mm))

        info = [
            [_p("Employee", st["cell_bold"], True), _p(employee.get("name"), st["cell"]),
             _p("Employee ID", st["cell_bold"], True), _p(employee.get("employee_code"), st["cell"])],
            [_p("Department", st["cell_bold"], True), _p(employee.get("department") or "-", st["cell"]),
             _p("Designation", st["cell_bold"], True), _p(employee.get("designation") or "-", st["cell"])],
            [_p("Work location", st["cell_bold"], True), _p(employee.get("branch") or "-", st["cell"]),
             _p("Currency", st["cell_bold"], True), _p(currency, st["cell"])],
        ]
        info_table = Table(info, colWidths=[width * 0.17, width * 0.33, width * 0.17, width * 0.33])
        info_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F4F6")),
            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F3F4F6")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(info_table)
        story.append(Spacer(1, 4 * mm))

        def amounts_table(title_text, rows, total_label, total):
            data = [[_p(title_text, st["head_white"], True), _p("Amount", st["head_white_right"], True)]]
            for label, amount in rows:
                data.append([_p(label, st["cell"]), _p(money(amount, currency), st["right"])])
            data.append([_p(total_label, st["cell_bold"], True), _p(money(total, currency), st["right_bold"], True)])
            table = Table(data, colWidths=[width * 0.31, width * 0.18])
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), accent),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F3F4F6")),
            ]))
            return table

        earnings = amounts_table("Earnings", snapshot.get("earnings") or [], "Gross earnings", snapshot.get("gross"))
        deductions = amounts_table("Deductions", snapshot.get("deductions") or [], "Total deductions",
                                   snapshot.get("total_deductions"))
        pair = Table([[earnings, deductions]], colWidths=[width * 0.5, width * 0.5])
        pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
        story.append(pair)
        story.append(Spacer(1, 4 * mm))

        net = Table([[_p("Net pay", st["head_white"], True), _p(money(snapshot.get("net"), currency), st["head_white_right"], True)]],
                    colWidths=[width * 0.7, width * 0.3])
        net.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent), ("TOPPADDING", (0, 0), (-1, -1), 7),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
        story.append(net)
        story.append(Spacer(1, 4 * mm))

        attendance = snapshot.get("attendance") or {}
        att = Table([
            [_p("Working days", st["cell_bold"], True), _p("Present", st["cell_bold"], True),
             _p("Absent", st["cell_bold"], True), _p("Half days", st["cell_bold"], True)],
            [_p(attendance.get("working_days", 0), st["cell"]), _p(attendance.get("present_days", 0), st["cell"]),
             _p(attendance.get("absent_days", 0), st["cell"]), _p(attendance.get("half_days", 0), st["cell"])],
        ], colWidths=[width / 4] * 4)
        att.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                                 ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6"))]))
        story.append(att)
        story.append(Spacer(1, 6 * mm))

        footer = ["This is a computer-generated document and does not require a signature."]
        footer.append(f"Document {snapshot.get('document_number', '-')} · issued {snapshot.get('issued_at', '')[:10]}")
        if brand.get("legal_footer"):
            footer.append(brand["legal_footer"])
        for line in footer:
            story.append(_p(line, st["small"]))
        powered = brand.get("powered_by_label") if brand.get("show_powered_by") else None
        page_label = brand.get("product_name") or ""

        def on_page(canvas, document):
            canvas.saveState()
            canvas.setFont(SANS, 7.5)
            canvas.setFillColor(colors.HexColor("#6B7280"))
            canvas.drawString(16 * mm, 9 * mm, powered or page_label)
            canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"Page {document.page}")
            if draft:
                canvas.setFont(SANS_BOLD, 60)
                canvas.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.12))
                canvas.translate(A4[0] / 2, A4[1] / 2)
                canvas.rotate(35)
                canvas.drawCentredString(0, 0, "DRAFT")
            canvas.restoreState()

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
        buffer.seek(0)
        return buffer


def _report_doc(buffer: BytesIO, title: str, brand_name: Optional[str]):
    st = _styles()
    doc = SimpleDocTemplate(buffer, pagesize=letter, title=title, author=brand_name or "")
    story = []
    if brand_name:
        story.append(_p(brand_name, st["small"]))
    story.append(_p(title, st["title"], bold=True))
    return st, doc, story


def _table(rows, widths, header_color: str):
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_color)),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9CA3AF")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def _row(values, st, header: bool = False):
    style = st["head_white"] if header else st["cell"]
    return [_p(v, style, header) for v in values]


def _generated(st):
    return _p(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", st["small"])


class AttendanceReportGenerator:
    @staticmethod
    def generate_pdf(report: dict, brand_name: Optional[str] = None) -> BytesIO:
        buffer = BytesIO()
        st, doc, story = _report_doc(buffer, "Attendance Report", brand_name)
        story.append(_p(f"Period: {report.get('period', 'N/A')}", st["body"]))
        story.append(Spacer(1, 0.2 * inch))
        summary = report.get("summary", {})
        rows = [_row(["Metric", "Value"], st, True)]
        for key, value in summary.items():
            if str(key).endswith("_seconds"):
                rows.append(_row([str(key)[: -len("_seconds")].replace("_", " ").title(), _hm(value)], st))
            else:
                rows.append(_row([str(key).replace("_", " ").title(), str(value)], st))
        story.append(_table(rows, [3 * inch, 3 * inch], "#1e3a8a"))
        story.append(Spacer(1, 0.3 * inch))
        records = report.get("records", [])
        if records:
            rows = [_row(["Employee", "Date", "Status", "Check-In", "Check-Out", "Lunch", "Net"], st, True)]
            for record in records:
                rows.append(_row([
                    record.get("employee_name", "N/A"), record.get("date", "N/A"), record.get("status", "N/A"),
                    _clock(record.get("check_in_time")), _clock(record.get("check_out_time")),
                    _hm(record.get("lunch_seconds")), _hm(record.get("net_seconds")),
                ], st))
            story.append(_table(rows, [1.7 * inch, 0.9 * inch, 0.8 * inch, 0.9 * inch, 0.9 * inch, 0.7 * inch, 0.7 * inch],
                                "#111827"))
        else:
            story.append(_p("No attendance records available for this period.", st["body"]))
        story.append(Spacer(1, 0.3 * inch))
        story.append(_generated(st))
        doc.build(story)
        buffer.seek(0)
        return buffer


class PayrollReportGenerator:
    @staticmethod
    def generate_pdf(report: dict, brand_name: Optional[str] = None) -> BytesIO:
        buffer = BytesIO()
        st, doc, story = _report_doc(buffer, "Payroll Report", brand_name)
        story.append(_p(f"Period: {report.get('period', 'N/A')}", st["body"]))
        story.append(Spacer(1, 0.2 * inch))
        currency = report.get("currency")
        summary = report.get("summary", {})
        rows = [_row(["Metric", "Amount"], st, True)]
        for key, value in summary.items():
            code = currency or str(key).rsplit("_", 1)[-1]
            rows.append(_row([str(key).replace("_", " ").title(), money(value, code)], st))
        story.append(_table(rows, [3 * inch, 3 * inch], "#065f46"))
        if report.get("currencies") and len(report["currencies"]) > 1:
            story.append(_p("Amounts are shown per currency; different currencies are never added together.", st["small"]))
        story.append(Spacer(1, 0.3 * inch))
        records = report.get("records", [])
        if records:
            rows = [_row(["Employee", "Month", "Gross", "Deductions", "Net", "Status"], st, True)]
            for record in records:
                code = record.get("currency") or currency
                rows.append(_row([
                    record.get("employee_name", "N/A"), record.get("salary_month", "N/A"),
                    money(record.get("gross_salary", 0), code), money(record.get("total_deductions", 0), code),
                    money(record.get("net_salary", 0), code), record.get("status", "N/A"),
                ], st))
            story.append(_table(rows, [1.9 * inch, 1.0 * inch, 1.2 * inch, 1.2 * inch, 1.2 * inch, 1.0 * inch], "#164e63"))
        else:
            story.append(_p("No payroll records available for this period.", st["body"]))
        story.append(Spacer(1, 0.3 * inch))
        story.append(_generated(st))
        doc.build(story)
        buffer.seek(0)
        return buffer


class LeaveReportGenerator:
    @staticmethod
    def generate_pdf(report: dict, brand_name: Optional[str] = None) -> BytesIO:
        buffer = BytesIO()
        st, doc, story = _report_doc(buffer, "Leave Report", brand_name)
        story.append(_p(f"Period: {report.get('period', 'N/A')}", st["body"]))
        story.append(Spacer(1, 0.2 * inch))
        rows = [_row(["Type", "Applied", "Approved", "Rejected"], st, True)]
        for leave_type, stats in report.get("summary", {}).items():
            rows.append(_row([leave_type.replace("_", " ").title(), str(stats.get("applied", 0)),
                              str(stats.get("approved", 0)), str(stats.get("rejected", 0))], st))
        story.append(_table(rows, [2.2 * inch, 1.2 * inch, 1.2 * inch, 1.2 * inch], "#7c2d12"))
        story.append(Spacer(1, 0.3 * inch))
        records = report.get("records", [])
        if records:
            rows = [_row(["Employee", "Type", "From", "To", "Days", "Status"], st, True)]
            for record in records:
                rows.append(_row([
                    record.get("employee_name", "N/A"), record.get("leave_type", "N/A"), record.get("start_date", "N/A"),
                    record.get("end_date", "N/A"), str(record.get("days", 0)), record.get("status", "N/A"),
                ], st))
            story.append(_table(rows, [1.8 * inch, 1.4 * inch, 1.1 * inch, 1.1 * inch, 0.9 * inch, 1.2 * inch], "#881337"))
        else:
            story.append(_p("No leave records available for this period.", st["body"]))
        story.append(Spacer(1, 0.3 * inch))
        story.append(_generated(st))
        doc.build(story)
        buffer.seek(0)
        return buffer
