"""Legacy payroll (M0) fixtures: the reference oracle, the golden vectors and API helpers.

`legacy_reference` copies the arithmetic of `app.services.payroll_service.calculate`
(audit 02 §2) without touching the application. It is the oracle that the v2
legacy-compatible policy (decision I-14, 3.3 §10) must reproduce. The module imports
nothing from `app`, so pure tests may import it as well.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

TWO = Decimal("0.01")
LATE_PENALTY = 100.0  # settings.late_penalty_amount: one global env value for every tenant and currency
HOURS_PER_DAY = 8.0  # settings.default_work_hours_per_day
HIRED = "2024-01-15"  # every golden employee is hired before the frozen months


def money(value) -> Decimal:
    """payroll_service._money: str() of the value, quantized to 2 dp HALF_UP."""
    return Decimal(str(value or 0)).quantize(TWO, rounding=ROUND_HALF_UP)


def legacy_reference(parts: Sequence, wdays: int, absent: int = 0, half: int = 0, late: int = 0, ot=0, bonus=0,
                     penalty=LATE_PENALTY, hpd=HOURS_PER_DAY) -> Dict[str, Decimal]:
    """Decimal results of the legacy calculation; `net_salary` is clipped at zero exactly like today."""
    gross = Decimal("0")
    for part in parts:
        gross += money(part)
    per_day = (gross / Decimal(wdays)) if wdays else Decimal("0")
    absent_deduction = money(per_day * absent)
    half_day_deduction = money(per_day * half * Decimal("0.5"))
    late_penalty = money(Decimal(str(penalty)) * late)
    hours_per_day = Decimal(str(hpd))
    overtime_amount = money((per_day / hours_per_day) * money(ot)) if hours_per_day else Decimal("0")
    total = absent_deduction + half_day_deduction + late_penalty
    raw_net = gross + overtime_amount + money(bonus) - total
    return {
        "gross_salary": gross, "absent_deduction": absent_deduction, "half_day_deduction": half_day_deduction,
        "late_penalty": late_penalty, "overtime_hours": money(ot), "overtime_amount": overtime_amount,
        "bonus": money(bonus), "total_deductions": total, "raw_net": raw_net, "net_salary": max(raw_net, Decimal("0")),
    }


def weekdays(year: int, month: int) -> List[date]:
    day, out = date(year, month, 1), []
    while day.month == month:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def all_days(year: int, month: int) -> List[date]:
    day, out = date(year, month, 1), []
    while day.month == month:
        out.append(day)
        day += timedelta(days=1)
    return out


@dataclass(frozen=True)
class Vector:
    """One golden vector (audit 02 §2): inputs, attendance rows to record, exact expected strings."""

    id: str
    year: int
    month: int
    structure: Dict[str, str]
    wdays: int
    attendance: Tuple[Tuple[str, str], ...] = ()  # (ISO date, status)
    weekday_holidays: bool = False  # every weekday of the month is a company-wide holiday
    ot: str = "0"
    bonus: str = "0"
    expected: Dict[str, str] = field(default_factory=dict)

    def count(self, status: str) -> int:
        return sum(1 for _, s in self.attendance if s == status)

    @property
    def parts(self) -> List[str]:
        return [self.structure.get(k, "0") for k in
                ("basic_salary", "house_rent_allowance", "dearness_allowance", "conveyance_allowance", "other_allowances")]


def _rows(status: str, days: Iterable[str]) -> Tuple[Tuple[str, str], ...]:
    return tuple((d, status) for d in days)


VECTORS: Tuple[Vector, ...] = (
    Vector("G1", 2025, 3, {"basic_salary": "30000", "house_rent_allowance": "5000"}, 21,
           expected={"gross_salary": "35000.00", "absent_deduction": "0.00", "half_day_deduction": "0.00",
                     "late_penalty": "0.00", "overtime_hours": "0.00", "overtime_amount": "0.00", "bonus": "0.00",
                     "total_deductions": "0.00", "net_salary": "35000.00"}),
    Vector("G2", 2025, 3, {"basic_salary": "30000", "house_rent_allowance": "5000"}, 21,
           attendance=_rows("absent", ("2025-03-03", "2025-03-04")) + _rows("half-day", ("2025-03-05",))
           + _rows("late", ("2025-03-06", "2025-03-07", "2025-03-10")),
           ot="4.5", bonus="1000.50",
           expected={"gross_salary": "35000.00", "absent_deduction": "3333.33", "half_day_deduction": "833.33",
                     "late_penalty": "300.00", "overtime_hours": "4.50", "overtime_amount": "937.50",
                     "bonus": "1000.50", "total_deductions": "4466.66", "net_salary": "32471.34"}),
    # 100.10 / 20 = 5.005 -> 5.01 with HALF_UP (HALF_EVEN would give 5.00).
    Vector("G3", 2025, 2, {"basic_salary": "100.10"}, 20, attendance=_rows("absent", ("2025-02-03",)),
           expected={"gross_salary": "100.10", "absent_deduction": "5.01", "half_day_deduction": "0.00",
                     "late_penalty": "0.00", "overtime_hours": "0.00", "overtime_amount": "0.00", "bonus": "0.00",
                     "total_deductions": "5.01", "net_salary": "95.09"}),
    # Absent rows on all 28 days, weekends included: deductions exceed gross and net is clipped (defect D2).
    Vector("G4", 2025, 2, {"basic_salary": "1000"}, 20,
           attendance=_rows("absent", tuple(d.isoformat() for d in all_days(2025, 2))),
           expected={"gross_salary": "1000.00", "absent_deduction": "1400.00", "half_day_deduction": "0.00",
                     "late_penalty": "0.00", "overtime_hours": "0.00", "overtime_amount": "0.00", "bonus": "0.00",
                     "total_deductions": "1400.00", "net_salary": "0.00", "raw_net": "-400.00"}),
    # Every weekday a holiday: per-day rate 0, so absences and overtime are free; the late penalty is not (D6).
    Vector("G5", 2025, 6, {"basic_salary": "1000"}, 0, weekday_holidays=True,
           attendance=_rows("absent", ("2025-06-02", "2025-06-03")) + _rows("late", ("2025-06-04",)),
           ot="10", bonus="50",
           expected={"gross_salary": "1000.00", "absent_deduction": "0.00", "half_day_deduction": "0.00",
                     "late_penalty": "100.00", "overtime_hours": "10.00", "overtime_amount": "0.00", "bonus": "50.00",
                     "total_deductions": "100.00", "net_salary": "950.00"}),
    # Overtime hours are rounded to 2 dp first: 1.005 -> 1.01 h; 286.93 (285.51 without the pre-rounding).
    Vector("G7", 2025, 4, {"basic_salary": "50000"}, 22, ot="1.005",
           expected={"gross_salary": "50000.00", "absent_deduction": "0.00", "half_day_deduction": "0.00",
                     "late_penalty": "0.00", "overtime_hours": "1.01", "overtime_amount": "286.93", "bonus": "0.00",
                     "total_deductions": "0.00", "net_salary": "50286.93"}),
    Vector("G8", 2025, 3, {"basic_salary": "10000"}, 21, attendance=_rows("half-day", ("2025-03-12",)),
           expected={"gross_salary": "10000.00", "absent_deduction": "0.00", "half_day_deduction": "238.10",
                     "late_penalty": "0.00", "overtime_hours": "0.00", "overtime_amount": "0.00", "bonus": "0.00",
                     "total_deductions": "238.10", "net_salary": "9761.90"}),
)
VECTOR_BY_ID = {v.id: v for v in VECTORS}

# Keys `calculate` returns as floats (defect D3: money is serialised as JSON numbers).
CALC_MONEY_KEYS = ("basic_salary", "hra", "da", "conveyance", "other_allowances", "gross_salary", "overtime_hours",
                   "overtime_amount", "bonus", "absent_deduction", "half_day_deduction", "late_penalty",
                   "total_deductions", "net_salary")


# ---------------------------------------------------------------------------
# API helpers (database-backed tests)
# ---------------------------------------------------------------------------
def payroll_tenant(app, name: str):
    """A tenant with its founder administrator, a preparer and an approver (both payroll_manager)."""
    from types import SimpleNamespace

    from app.core.rate_limit import limiter
    from tests.helpers import create_employee, invite_and_activate, signup_company

    limiter.reset()  # many sign-ins from one test client address
    admin = signup_company(app, name)
    admin.user_id = admin.me()["user"]["id"]
    preparer = invite_and_activate(app, admin, create_employee(admin, "Paula", "Prep"), ["payroll_manager"])
    approver = invite_and_activate(app, admin, create_employee(admin, "Andy", "Approve"), ["payroll_manager"])
    entities = {e["code"]: e for e in admin.get("/api/v1/legal-entities").json()}
    return SimpleNamespace(app=app, admin=admin, preparer=preparer, approver=approver, company_id=admin.company_id,
                           le01=entities["LE01"])


def dec(value) -> Decimal:
    """A JSON number as the exact Decimal of its repr (what a client parsing the float would see)."""
    assert isinstance(value, float), f"legacy API money is a float today, got {type(value).__name__}"
    return Decimal(str(value))


def structure(admin, employee_id: int, effective_from: str = "2024-01-01", **amounts):
    body = {"employee_id": employee_id, "effective_from": effective_from, **amounts}
    r = admin.post("/api/v1/salary-structures", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def holiday(admin, day: str, name: Optional[str] = None, **extra) -> dict:
    r = admin.post("/api/v1/holidays", json={"name": name or f"Holiday {day}", "date": day, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def mark(admin, employee_id: int, day: str, status: str) -> dict:
    """A manual attendance row (09:00-17:00 UTC) recorded by a non-self administrator."""
    r = admin.post("/api/v1/attendance/manual", json={
        "employee_id": employee_id, "date": day, "check_in_time": f"{day}T09:00:00+00:00",
        "check_out_time": f"{day}T17:00:00+00:00", "status": status})
    assert r.status_code == 200, r.text
    return r.json()


def calc_url(employee_id: int, year: int, month: int, ot="0", bonus="0", action: str = "calculate") -> str:
    return (f"/api/v1/payroll/{action}?employee_id={employee_id}&year={year}&month={month}"
            f"&overtime_hours={ot}&bonus={bonus}")


def payroll_row(payroll_id: int) -> dict:
    from tests.attendance_helpers import owner_rows

    rows = owner_rows("SELECT * FROM payroll WHERE id = :i", i=payroll_id)
    assert len(rows) == 1
    return rows[0]


def documents(payroll_id: int) -> List[dict]:
    from tests.attendance_helpers import owner_rows

    return owner_rows("SELECT * FROM issued_documents WHERE document_type = 'PAYSLIP' AND source_id = :i ORDER BY id",
                      i=payroll_id)


LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}


def pdf_text(data: bytes) -> str:
    """Extracted PDF text with typographic ligatures expanded (NotoSans renders "ff" as U+FB00)."""
    import pymupdf

    text = "".join(page.get_text() for page in pymupdf.open(stream=data, filetype="pdf"))
    for ligature, letters in LIGATURES.items():
        text = text.replace(ligature, letters)
    return text


def insert_rows(company_id: int, rows: Iterable[dict]) -> List[int]:
    """Insert payroll rows as the tenant application role (like an older release or raw SQL would)."""
    from app.database.db import SessionLocal, bind_tenant
    from app.models.payroll import Payroll

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        created = []
        for values in rows:
            row = Payroll(company_id=company_id, **values)
            db.add(row)
            db.flush()
            created.append(row.id)
        db.commit()
        return created
    finally:
        db.close()
