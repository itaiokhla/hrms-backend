"""Statutory attendance registers (AS-M9, G-20; 10-analytics §6.1-§6.5, §7).

A register is a **rendering of the evaluation ledger** (``attendance_days``, approved ``overtime_entries``, day
facts), never a separate store (D-AS-87). Two forms ship:

* ``FORM_25`` — muster roll (Factories Act Form 25 / OSH FORM XIV layout): employee × date grid of mark codes (§6.4:
  ``P`` present, ``LT`` late, ``½P`` half day, ``A`` absent, ``NM`` not marked, ``MP`` missing punch, ``…`` pending,
  ``OD`` on duty, ``RW`` remote, ``L`` leave (never the type name), ``H`` holiday, ``W`` weekly off, ``–`` not
  employed) with totals: present units, absent units, leave units, holidays, weekly offs, approved OT hours, night
  minutes.
* ``FORM_D`` — register of attendance (Equal Remuneration / Minimum Wages Form D layout): employee identity
  (designation, sex as held), hours worked per date (net, shift date basis R-3), days worked, total hours, approved
  OT hours; wage columns print "Held in payroll register" (G-2).

Rules: per establishment (a branch, or a legal entity as a whole) and period (≤ 92 days; one month is the normal
use); people are placed by the branch of their day facts **on each date** (P-3), so a transfer splits the rows;
days not FINAL print ``…`` and are counted in the footer (G-4); corrections after the first FINAL version are listed
in the appendix (G-3) from ``attendance_day_versions``; masking: no coordinates, photos, scores, comments or leave
type names (R-5). Every export carries a seal: generator, instant, user, form, period, row count and the SHA-256 of
the canonical register content, also written to the audit log (``REPORT_EXPORTED``, module Attendance, X-2).
Permission ``attendance:export`` beyond SELF (R-1); the rows are limited to that scope.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import authz, clock
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.principal import Principal
from app.models.attendance import AttendanceDay, AttendanceDayVersion
from app.models.employee import Employee
from app.models.work_items import OvertimeEntry
from app.services.audit_log_service import AuditLogService

EXPORT = "attendance:export"
FORMS = {"FORM_25": "Form 25 — Muster roll", "FORM_D": "Form D — Register of attendance"}
FORMATS = ("JSON", "CSV", "XLSX", "PDF")
MAX_DAYS = 92
GENERATOR = "HRMS attendance registers"
LEGEND = ("P present · LT late · ½P half day · A absent · NM not marked · MP missing punch · … pending · "
          "OD on duty · RW remote · L leave · H holiday · W weekly off · – not employed")
MEDIA = {"CSV": ("text/csv", "csv"),
         "XLSX": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
         "PDF": ("application/pdf", "pdf")}


@dataclass
class Register:
    form: str
    title: str
    establishment: dict
    start: date
    end: date
    dates: List[date]
    rows: List[dict] = field(default_factory=list)
    pending_days: int = 0
    corrections: List[dict] = field(default_factory=list)
    seal: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"form": self.form, "title": self.title, "establishment": self.establishment,
                "from": self.start.isoformat(), "to": self.end.isoformat(),
                "dates": [d.isoformat() for d in self.dates], "rows": self.rows, "legend": LEGEND,
                "pending_days": self.pending_days, "corrections": self.corrections, "seal": self.seal,
                "total": len(self.rows)}


# ------------------------------------------------------------------------------------------------ marks
def mark(row: Optional[AttendanceDay], fact, expectation) -> str:
    """§6.4 code of one employee-date (screen and paper agree)."""
    if fact is None or not fact.employed:
        return "–"
    portion = expectation.expected_portion if expectation is not None else "NONE"
    if portion == "NONE":
        if fact.holiday is not None:
            return "H"
        if fact.weekly_off or fact.working_portion == "NONE":
            return "W"
        if expectation is not None and expectation.on_leave_portion != "NONE":
            return "L"
        return "W"
    if row is None or row.evaluation_state != "FINAL":
        return "…"
    leave_half = expectation is not None and expectation.on_leave_portion in ("FIRST_HALF", "SECOND_HALF")
    result = row.day_result
    if result == "PRESENT":
        if row.presence_basis == "ON_DUTY":
            code = "OD"
        elif row.presence_basis == "REMOTE_WORK":
            code = "RW"
        else:
            code = "LT" if row.late_mark and not row.late_excused else "P"
    else:
        code = {"HALF_DAY": "½P", "ABSENT": "A", "UNMARKED": "NM", "MISSING_PUNCH": "MP", "PENDING": "…",
                "OFF": "W"}.get(result, "…")
    return f"½L/{code}" if leave_half else code


UNITS = {"P": Decimal(1), "LT": Decimal(1), "OD": Decimal(1), "RW": Decimal(1), "MP": Decimal(1),
         "½P": Decimal("0.5")}


# ------------------------------------------------------------------------------------------------ build
def _establishment(db: Session, principal: Principal, branch_id: Optional[int],
                   legal_entity_id: Optional[int]) -> dict:
    from app.models.org import Branch, LegalEntity

    if branch_id is None and legal_entity_id is None:
        raise bad_request("Choose the establishment: a branch or a legal entity.", "ESTABLISHMENT_REQUIRED")
    if branch_id is not None:
        branch = db.query(Branch).filter(Branch.company_id == principal.company_id, Branch.id == branch_id).first()
        if branch is None:
            raise not_found("Branch")
        return {"kind": "BRANCH", "id": branch.id, "name": branch.name,
                "legal_entity_id": getattr(branch, "legal_entity_id", None)}
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == principal.company_id,
                                          LegalEntity.id == legal_entity_id).first()
    if entity is None:
        raise not_found("Legal entity")
    return {"kind": "LEGAL_ENTITY", "id": entity.id, "name": entity.trade_name or entity.legal_name,
            "legal_entity_id": entity.id}


def build(db: Session, principal: Principal, form: str, *, start: date, end: date, branch_id: Optional[int] = None,
          legal_entity_id: Optional[int] = None, department_id: Optional[int] = None) -> Register:
    from app.payroll.services import time_inputs as ti

    if form not in FORMS:
        raise not_found("Register")
    if not (principal.scopes(EXPORT) - {"SELF"}):
        raise forbidden()
    if end < start:
        raise bad_request("The end date is before the start date.", "INVALID_RANGE")
    if (end - start).days + 1 > MAX_DAYS:
        raise AppError(422, f"A register covers at most {MAX_DAYS} days; export one month at a time.",
                       "REPORT_RANGE_TOO_LARGE")
    company_id = principal.company_id
    establishment = _establishment(db, principal, branch_id, legal_entity_id)
    scope = authz.employee_scope(db, principal, EXPORT)
    query = db.query(Employee).filter(Employee.company_id == company_id, scope.clause(Employee.id),
                                      Employee.hire_date <= end,
                                      or_(Employee.termination_date.is_(None), Employee.termination_date >= start))
    if department_id is not None:
        query = query.filter(Employee.department_id == department_id)
    employees = query.order_by(Employee.employee_code, Employee.id).all()
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    register = Register(form, FORMS[form], establishment, start, end, dates)
    if not employees:
        return _sealed(db, principal, register)
    ids = [e.id for e in employees]
    facts_all: Dict[int, list] = {}
    expectations_all: Dict[int, list] = {}
    for index in range(0, len(ids), 200):
        chunk = ids[index:index + 200]
        facts = ti.day_facts_many(db, company_id, chunk, start, end)
        facts_all.update(facts)
        expectations_all.update(ti.expected_presence(db, company_id, chunk, start, end, facts=facts))
    rows_by = {(r.employee_id, r.shift_date): r for r in db.query(AttendanceDay).filter(
        AttendanceDay.company_id == company_id, AttendanceDay.employee_id.in_(ids),
        AttendanceDay.shift_date >= start, AttendanceDay.shift_date <= end)}
    ot = defaultdict(int)
    for employee_id, day, minutes in db.query(OvertimeEntry.employee_id, OvertimeEntry.date,
                                              func.coalesce(OvertimeEntry.minutes_approved, 0)).filter(
            OvertimeEntry.company_id == company_id, OvertimeEntry.employee_id.in_(ids),
            OvertimeEntry.status == "APPROVED", OvertimeEntry.date >= start, OvertimeEntry.date <= end):
        ot[(employee_id, day)] += int(minutes)

    def placed(fact) -> bool:
        if fact is None or not fact.employed:
            return False
        if establishment["kind"] == "BRANCH":
            return fact.branch_id == establishment["id"]
        return fact.legal_entity_id == establishment["id"]

    for e in employees:
        facts = {f.date: f for f in facts_all.get(e.id, [])}
        expected = {x.date: x for x in expectations_all.get(e.id, [])}
        if not any(placed(facts.get(d)) for d in dates):
            continue
        cells, hours = [], []
        totals = defaultdict(Decimal)
        for d in dates:
            fact = facts.get(d)
            if not placed(fact):
                cells.append("–")
                hours.append(None)
                continue
            row = rows_by.get((e.id, d))
            code = mark(row, fact, expected.get(d))
            cells.append(code)
            base = code.split("/")[-1]
            if code == "…":
                register.pending_days += 1
            totals["present_units"] += UNITS.get(base, Decimal(0)) * (Decimal("0.5") if code.startswith("½L/")
                                                                        else Decimal(1))
            if base in ("A", "NM"):
                totals["absent_units"] += Decimal("0.5") if code.startswith("½L/") else Decimal(1)
            if code == "L" or code.startswith("½L/"):
                totals["leave_units"] += Decimal(1) if code == "L" else Decimal("0.5")
            totals["holidays"] += 1 if code == "H" else 0
            totals["weekly_offs"] += 1 if code == "W" else 0
            net = row.net_minutes if row is not None and row.evaluation_state == "FINAL" else None
            hours.append(None if net is None else format((Decimal(net) / 60).quantize(Decimal("0.01")), "f"))
            if net:
                totals["worked_minutes"] += Decimal(net)
                totals["days_worked"] += 1
            totals["ot_minutes"] += ot.get((e.id, d), 0)
            totals["night_minutes"] += Decimal(row.night_minutes or 0) if row is not None else 0
        entry = {
            "employee_id": e.id, "employee_code": e.employee_code, "name": e.full_name,
            "designation": e.position or "", "sex": (e.gender or "Not held in HRMS"),
            "marks" if form == "FORM_25" else "hours": cells if form == "FORM_25" else hours,
            "totals": {
                "present_units": format(totals["present_units"], "f"),
                "absent_units": format(totals["absent_units"], "f"),
                "leave_units": format(totals["leave_units"], "f"),
                "holidays": int(totals["holidays"]), "weekly_offs": int(totals["weekly_offs"]),
                "days_worked": int(totals["days_worked"]),
                "hours_worked": format((totals["worked_minutes"] / 60).quantize(Decimal("0.01")), "f"),
                "ot_hours": format((Decimal(totals["ot_minutes"]) / 60).quantize(Decimal("0.01")), "f"),
                "night_minutes": int(totals["night_minutes"]),
            },
        }
        if form == "FORM_D":
            entry["marks"] = cells
            entry["wages"] = "Held in payroll register"
        register.rows.append(entry)
    register.corrections = _corrections(db, company_id, [r["employee_id"] for r in register.rows], start, end)
    return _sealed(db, principal, register)


def _corrections(db: Session, company_id: int, ids: List[int], start: date, end: date) -> List[dict]:
    """G-3: versions after the first FINAL version of a day, with the old / new result (no free text)."""
    if not ids:
        return []
    versions = (db.query(AttendanceDayVersion, AttendanceDay.employee_id, AttendanceDay.shift_date)
                .join(AttendanceDay, (AttendanceDay.company_id == AttendanceDayVersion.company_id)
                      & (AttendanceDay.id == AttendanceDayVersion.day_id))
                .filter(AttendanceDayVersion.company_id == company_id, AttendanceDay.employee_id.in_(ids),
                        AttendanceDay.shift_date >= start, AttendanceDay.shift_date <= end,
                        AttendanceDayVersion.version > 1)
                .order_by(AttendanceDay.shift_date, AttendanceDay.employee_id, AttendanceDayVersion.version).all())
    names = {e.id: e.full_name for e in db.query(Employee).filter(Employee.company_id == company_id,
                                                                  Employee.id.in_(ids))}
    out = []
    for version, employee_id, day in versions:
        changed = getattr(version, "changed_outputs", None) or getattr(version, "outputs", None) or {}
        result = changed.get("day_result") if isinstance(changed, dict) else None
        out.append({"date": day.isoformat(), "employee": names.get(employee_id), "version": version.version,
                    "day_result": result, "trigger": getattr(version, "trigger", None),
                    "at": version.created_at.isoformat() if version.created_at else None})
    return out[:500]


def _sealed(db: Session, principal: Principal, register: Register) -> Register:
    canonical = json.dumps({k: v for k, v in register.as_json().items() if k != "seal"}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False, default=str)
    now = clock.utcnow().replace(microsecond=0)
    register.seal = {
        "generator": GENERATOR, "generated_at": now.isoformat(), "generated_by": principal.full_name or
        principal.email, "form": register.form, "period": f"{register.start.isoformat()}..{register.end.isoformat()}",
        "rows": len(register.rows), "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "pack_version": "NO_PACK", "statement": f"Generated by {GENERATOR} on {now.isoformat()} by "
        f"{principal.full_name or principal.email}; electronic register",
    }
    return register


# ------------------------------------------------------------------------------------------------ render
def _escape(value: Any) -> Any:
    from app.payroll.banking.files import escape_csv_cell

    return escape_csv_cell(value) if isinstance(value, str) else value


def _grid(register: Register) -> Tuple[List[str], List[List[Any]]]:
    day_cols = [d.strftime("%d %a") for d in register.dates]
    if register.form == "FORM_25":
        head = ["Code", "Name", "Designation"] + day_cols + ["Present", "Absent", "Leave", "Holidays", "Weekly off",
                                                             "OT hours", "Night min"]
        body = [[r["employee_code"], r["name"], r["designation"]] + r["marks"] + [
            r["totals"]["present_units"], r["totals"]["absent_units"], r["totals"]["leave_units"],
            r["totals"]["holidays"], r["totals"]["weekly_offs"], r["totals"]["ot_hours"],
            r["totals"]["night_minutes"]] for r in register.rows]
    else:
        head = ["Code", "Name", "Designation", "Sex"] + day_cols + ["Days worked", "Hours worked", "OT hours",
                                                                    "Wages"]
        body = [[r["employee_code"], r["name"], r["designation"], r["sex"]]
                + [h if h is not None else m for h, m in zip(r["hours"], r["marks"])]
                + [r["totals"]["days_worked"], r["totals"]["hours_worked"], r["totals"]["ot_hours"], r["wages"]]
                for r in register.rows]
    return head, body


def _header(register: Register) -> List[str]:
    return [register.title,
            f"Establishment: {register.establishment['name']} ({register.establishment['kind'].replace('_', ' ').lower()})"
            f" · Period {register.start.isoformat()} to {register.end.isoformat()}",
            f"Legend: {LEGEND}"]


def _footer(register: Register) -> List[str]:
    lines = [register.seal["statement"], f"Seal SHA-256: {register.seal['sha256']} · rows {register.seal['rows']}"]
    if register.pending_days:
        lines.append(f"{register.pending_days} days pending device sync or approval (…); re-export after they settle.")
    for c in register.corrections:
        lines.append(f"Correction · {c['date']} · {c['employee']} · v{c['version']} · {c['day_result'] or ''} · "
                     f"{c['trigger'] or ''} · {c['at'] or ''}")
    return lines


def render(register: Register, fmt: str) -> bytes:
    head, body = _grid(register)
    if fmt == "CSV":
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        for line in _header(register):
            writer.writerow([_escape(line)])
        writer.writerow([_escape(h) for h in head])
        for row in body:
            writer.writerow([_escape("" if v is None else str(v)) for v in row])
        for line in _footer(register):
            writer.writerow([_escape(line)])
        return out.getvalue().encode("utf-8-sig")
    if fmt == "XLSX":
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = register.form.replace("_", " ")
        for line in _header(register):
            sheet.append([_escape(line)])
            sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True)
        sheet.append([_escape(h) for h in head])
        for i in range(1, len(head) + 1):
            cell = sheet.cell(row=sheet.max_row, column=i)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        for row in body:
            sheet.append([_escape(v) if isinstance(v, str) else v for v in row])
        for line in _footer(register):
            sheet.append([_escape(line)])
        workbook.properties.title = register.title
        workbook.properties.creator = GENERATOR
        workbook.properties.description = json.dumps(register.seal)
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()
    if fmt == "PDF":
        return _pdf(register, head, body)
    raise bad_request("Choose CSV, XLSX or PDF.", "INVALID_FORMAT")


def _pdf(register: Register, head: List[str], body: List[List[Any]]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    from app.utils import pdf_fonts

    pdf_fonts.register()
    tiny = ParagraphStyle("cell", fontName=pdf_fonts.SANS, fontSize=5.5, leading=6.5, alignment=1)
    tiny_left = ParagraphStyle("left", parent=tiny, alignment=0)
    bold = ParagraphStyle("head", fontName=pdf_fonts.SANS_BOLD, fontSize=5.5, leading=6.5, alignment=1)
    title = ParagraphStyle("title", fontName=pdf_fonts.SANS_BOLD, fontSize=12, leading=15)
    note = ParagraphStyle("note", fontName=pdf_fonts.SANS, fontSize=7, leading=9, textColor=colors.HexColor("#374151"))
    for style in (tiny, tiny_left, bold, title, note):
        style.shaping = 1
    buffer = io.BytesIO()
    seal = register.seal

    def decorate(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont(pdf_fonts.SANS, 6.5)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawString(20, 12, f"{seal['statement']} · SHA-256 {seal['sha256'][:16]}… · page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=18, rightMargin=18, topMargin=20,
                            bottomMargin=26, title=register.title, author=GENERATOR, subject=seal["sha256"])
    story: List[Any] = [Paragraph(pdf_fonts.markup(register.title, True), title)]
    for line in _header(register)[1:]:
        story.append(Paragraph(pdf_fonts.markup(line), note))
    story.append(Spacer(1, 4))
    data = [[Paragraph(pdf_fonts.markup(h, True), bold) for h in head]]
    for row in body:
        data.append([Paragraph(pdf_fonts.markup("" if v is None else str(v)), tiny_left if i < 3 else tiny)
                     for i, v in enumerate(row)])
    table = LongTable(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9CA3AF")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.5), ("RIGHTPADDING", (0, 0), (-1, -1), 1.5),
    ]))
    story.append(table)
    story.append(Spacer(1, 6))
    for line in _footer(register):
        story.append(Paragraph(pdf_fonts.markup(line), note))
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()


def export(db: Session, principal: Principal, register: Register, fmt: str) -> Tuple[bytes, str, str]:
    """Render and audit one export (X-2): (content, media type, file name)."""
    fmt = fmt.upper()
    if fmt not in MEDIA:
        raise bad_request("Choose CSV, XLSX or PDF.", "INVALID_FORMAT")
    content = render(register, fmt)
    media, ext = MEDIA[fmt]
    stamp = clock.utcnow().strftime("%Y%m%d_%H%M%S")
    name = f"ATT_{register.form}_{register.start.strftime('%Y%m%d')}_{stamp}.{ext}"
    AuditLogService.log(db, principal, "Attendance", "REPORT_EXPORTED",
                        f"{register.title} exported ({fmt}, {len(register.rows)} rows)", "attendance_register", None,
                        details={"form": register.form, "format": fmt, "rows": len(register.rows),
                                 "from": register.start.isoformat(), "to": register.end.isoformat(),
                                 "establishment": register.establishment, "seal_sha256": register.seal["sha256"],
                                 "file_sha256": hashlib.sha256(content).hexdigest()})
    return content, media, name


def catalogue(principal: Principal) -> dict:
    """``GET /attendance/registers``: the forms the caller can run (absent otherwise, R-1)."""
    allowed = bool(principal.scopes(EXPORT) - {"SELF"})
    items = [{"code": code, "name": name, "formats": list(MEDIA), "max_days": MAX_DAYS}
             for code, name in FORMS.items()] if allowed else []
    return {"items": items}
