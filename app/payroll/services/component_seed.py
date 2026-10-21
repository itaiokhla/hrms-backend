"""The country-neutral pay components every new tenant starts with (3.3 §2.4).

``seed(db, company)`` adds the missing ones (idempotent: existing codes are left alone, so it also tops up
an existing tenant through POST /payroll/components/seed). The provisioning hook
(app.payroll.services.provisioning.provision_tenant) must call it for newly provisioned tenants; the
legal entity's country pack adds its statutory components separately.

Engine-computed components (overtime, holiday and weekly-off premiums, shift allowance, task earnings,
arrears, loan and advance recoveries, net rounding) are seeded as SYSTEM: their amounts come from the
approved records and the policies (no universal overtime multiplier is assumed, contract K:157); a
structure line with another method replaces the engine's line for that component.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.compensation import PayComponent
from app.payroll import audit as payroll_audit
from app.payroll.services import components as comp
from app.services.audit_log_service import AuditLogService

SALARY = ["LOP_APPLICABLE", "PART_OF_CTC", "PART_OF_GROSS"]
GROSS = ["PART_OF_GROSS"]

# (code, name, kind, category, method, fields)
SEED: tuple[tuple[str, str, str, str, str, dict[str, Any]], ...] = (
    ("BASIC", "Basic", "EARNING", "BASE", "FIXED",
     {"tags": [*SALARY, "OT_BASE", "ENCASHMENT_BASE"], "proration": "BY_PAID_DAYS", "payslip": {"order": 10}}),
    ("FIXED_ALLOWANCE", "Fixed allowance", "EARNING", "FIXED_ALLOWANCE", "BALANCING",
     {"tags": SALARY, "proration": "BY_PAID_DAYS", "payslip": {"order": 30}}),
    ("BONUS", "Bonus", "EARNING", "BONUS", "INPUT", {"tags": GROSS, "payslip": {"order": 60}}),
    ("COMMISSION", "Commission", "EARNING", "COMMISSION", "INPUT", {"tags": GROSS, "payslip": {"order": 61}}),
    ("INCENTIVE", "Incentive", "EARNING", "VARIABLE", "INPUT", {"tags": GROSS, "payslip": {"order": 62}}),
    ("OT_WORKDAY", "Overtime (working day)", "EARNING", "OVERTIME", "SYSTEM", {"tags": GROSS, "payslip": {"order": 50}}),
    ("OT_WEEKLY_OFF", "Overtime (weekly off)", "EARNING", "OVERTIME", "SYSTEM",
     {"tags": GROSS, "payslip": {"order": 51}}),
    ("OT_HOLIDAY", "Overtime (holiday)", "EARNING", "OVERTIME", "SYSTEM", {"tags": GROSS, "payslip": {"order": 52}}),
    ("HOLIDAY_PREMIUM", "Holiday work premium", "EARNING", "HOLIDAY_PREMIUM", "SYSTEM",
     {"tags": GROSS, "payslip": {"order": 53}}),
    ("WEEKLY_OFF_PREMIUM", "Weekly-off work premium", "EARNING", "HOLIDAY_PREMIUM", "SYSTEM",
     {"tags": GROSS, "payslip": {"order": 54}}),
    ("SHIFT_ALLOWANCE", "Shift allowance", "EARNING", "SHIFT_PREMIUM", "SYSTEM",
     {"tags": GROSS, "payslip": {"order": 55}}),
    ("TASK_EARNING", "Task earnings", "EARNING", "TASK_EARNING", "SYSTEM", {"tags": GROSS, "payslip": {"order": 56}}),
    ("ARREARS", "Arrears", "EARNING", "RETRO_ADJUSTMENT", "SYSTEM", {"payslip": {"order": 70}}),
    ("LEAVE_ENCASHMENT", "Leave encashment", "EARNING", "LEAVE_ENCASHMENT", "FORMULA",
     {"formula": 'SUM_TAG_RATE("ENCASHMENT_BASE") / period.basis_days * UNITS("LEAVE_ENCASHMENT")',
      "payslip": {"order": 75}}),
    ("REIMBURSEMENT", "Reimbursement", "REIMBURSEMENT", "REIMBURSEMENT", "INPUT", {"payslip": {"order": 80}}),
    ("LOAN_RECOVERY", "Loan recovery", "DEDUCTION", "LOAN_REPAYMENT", "SYSTEM", {"payslip": {"order": 200}}),
    ("ADVANCE_RECOVERY", "Advance recovery", "DEDUCTION", "ADVANCE_RECOVERY", "SYSTEM", {"payslip": {"order": 201}}),
    ("OTHER_DEDUCTION", "Other deduction", "DEDUCTION", "VOLUNTARY", "INPUT", {"payslip": {"order": 210}}),
    ("NET_ROUNDING", "Net pay rounding", "ROUNDING", "ROUNDING", "SYSTEM", {"payslip": {"order": 900}}),
)


def seed(db: Session, company: Company) -> list[str]:
    """Create the seeded components the company does not have yet; returns the codes created."""
    existing = {code for (code,) in db.query(PayComponent.code).filter(PayComponent.company_id == company.id)}
    created = []
    for code, name, kind, category, method, fields in SEED:
        if code in existing:
            continue
        data = comp.ComponentInput(name=name, calc_method=method, **fields)
        comp.create_component(db, None, code=code, kind=kind, category=category, data=data, company_id=company.id)
        created.append(code)
    if created:
        AuditLogService.log(db, None, payroll_audit.COMPENSATION, comp.COMPONENTS_SEEDED,
                            f"Added {len(created)} standard pay components", "company", company.id,
                            company_id=company.id, actor_type="system", details={"codes": created})
    return created
