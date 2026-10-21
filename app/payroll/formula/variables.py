"""Variables a formula may read (3.3 §4.3), their types and where they come from.

Each variable has a declared type and, when its source can be switched off, the
switch that controls it (``source``). A variable whose source is OFF evaluates to
NULL; arithmetic or comparison on NULL is a NULL_VALUE error, so formulas must use
``COALESCE(att.late_marks, 0)`` to choose a default and the editor warns on save
(EC-CMP-03). ``nullable`` marks data that may simply be missing for an employee
(no grade, no exit date). The day ledger always exists, so ``att.paid_days`` and
``att.lop_days`` are always available.

Protected attributes (gender, religion, caste, marital status, nationality, date
of birth, and the age and synonyms derived from them) can never be read by company
formulas (PROTECTED_ATTRIBUTE, EC-CMP-19); country packs may read them where the
law depends on them. The same names are refused as custom ``emp.attr.*`` attributes,
so the rule cannot be bypassed by copying the data into a custom field.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from app.payroll.formula.types import BOOLEAN, DATE, DECIMAL, INTEGER, MONEY_PAY, STRING, FType

NAMESPACES = ("period", "att", "ot", "task", "emp", "ctc")
CUSTOM_ATTRIBUTE_PREFIX = "emp.attr."

ATTENDANCE = "payroll.attendance.enabled"
LEAVE = "payroll.leave.enabled"
OVERTIME = "payroll.overtime.enabled"
TASK_PAY = "payroll.task_pay.enabled"


@dataclass(frozen=True)
class VariableDef:
    name: str
    type: FType  # MONEY_PAY: money in the formula's (pay) currency
    description: str
    source: str | None = None  # setting that can switch the source off (the value is then NULL)
    nullable: bool = False  # may be missing for some employees even when the source is on
    protected: bool = False  # country packs only (EC-CMP-19)

    @property
    def may_be_null(self) -> bool:
        return self.source is not None or self.nullable


def _defs(*items: VariableDef) -> dict[str, VariableDef]:
    return {item.name: item for item in items}


VARIABLES: dict[str, VariableDef] = _defs(
    VariableDef("period.start", DATE, "First day of the pay period"),
    VariableDef("period.end", DATE, "Last day of the pay period"),
    VariableDef("period.days", INTEGER, "Calendar days in the period"),
    VariableDef("period.working_days", DECIMAL, "Scheduled working days in the whole period"),
    VariableDef("period.basis_days", DECIMAL, "Proration basis N (payroll.lop.proration_basis)"),
    VariableDef("period.number_in_tax_year", INTEGER, "Position of the period in the tax year (1 = first)"),
    VariableDef("period.is_last_in_tax_year", BOOLEAN, "The last period of the tax year"),
    VariableDef("period.pay_date", DATE, "Pay date of the period"),
    VariableDef("att.paid_days", DECIMAL, "Paid days from the day ledger (always available)"),
    VariableDef("att.lop_days", DECIMAL, "Loss-of-pay days, including penalty LOP (always available)"),
    VariableDef("att.present_days", DECIMAL, "Days present", ATTENDANCE),
    VariableDef("att.half_days", DECIMAL, "Half days", ATTENDANCE),
    VariableDef("att.leave_days_paid", DECIMAL, "Paid leave days", LEAVE),
    VariableDef("att.leave_days_unpaid", DECIMAL, "Unpaid leave days", LEAVE),
    VariableDef("att.holidays", DECIMAL, "Paid holidays in employment"),
    VariableDef("att.weekly_offs", DECIMAL, "Weekly offs in employment"),
    VariableDef("att.payable_hours", DECIMAL, "Worked plus paid leave and holiday hours", ATTENDANCE),
    VariableDef("att.worked_hours", DECIMAL, "Canonical net worked hours", ATTENDANCE),
    VariableDef("att.night_hours", DECIMAL, "Hours inside the night window", ATTENDANCE),
    VariableDef("att.late_marks", INTEGER, "Late marks", ATTENDANCE),
    VariableDef("ot.hours_workday", DECIMAL, "Approved overtime hours on working days", OVERTIME),
    VariableDef("ot.hours_weekly_off", DECIMAL, "Approved overtime hours on weekly offs", OVERTIME),
    VariableDef("ot.hours_holiday", DECIMAL, "Approved overtime hours on holidays", OVERTIME),
    VariableDef("ot.hours_total", DECIMAL, "All approved overtime hours", OVERTIME),
    VariableDef("ot.hourly_rate", MONEY_PAY, "Overtime base hourly rate (OVERTIME_POLICY.rate_formula)", OVERTIME),
    VariableDef("task.amount", MONEY_PAY, "Approved task earnings attributed to the period", TASK_PAY),
    VariableDef("task.units", DECIMAL, "Approved task units attributed to the period", TASK_PAY),
    VariableDef("emp.grade", STRING, "Grade code", nullable=True),
    VariableDef("emp.employment_type", STRING, "Employment type code"),
    VariableDef("emp.worker_class", STRING, "Worker class (EMPLOYEE, CONTRACTOR, ...)"),
    VariableDef("emp.designation", STRING, "Designation code", nullable=True),
    VariableDef("emp.years_of_service", DECIMAL, "Years of service on the period end date"),
    VariableDef("emp.months_of_service", DECIMAL, "Months of service on the period end date"),
    VariableDef("emp.hire_date", DATE, "Hire date"),
    VariableDef("emp.exit_date", DATE, "Exit date; NULL while employed", nullable=True),
    VariableDef("emp.is_joiner", BOOLEAN, "Joined during the period"),
    VariableDef("emp.is_leaver", BOOLEAN, "Leaves during the period"),
    VariableDef("emp.fte", DECIMAL, "Full-time equivalent (1 = full time)"),
    VariableDef("emp.standard_hours_per_day", DECIMAL, "Standard hours per day"),
    VariableDef("emp.country", STRING, "ISO 3166 country of the legal entity"),
    VariableDef("emp.subdivision", STRING, "ISO 3166-2 subdivision of the branch", nullable=True),
    VariableDef("ctc.annual", MONEY_PAY, "Annual cost to company from the compensation assignment"),
    VariableDef("ctc.monthly", MONEY_PAY, "Monthly cost to company from the compensation assignment"),
    VariableDef("emp.gender", STRING, "Gender (country packs only)", nullable=True, protected=True),
    VariableDef("emp.religion", STRING, "Religion (country packs only)", nullable=True, protected=True),
    VariableDef("emp.caste", STRING, "Caste (country packs only)", nullable=True, protected=True),
    VariableDef("emp.marital_status", STRING, "Marital status (country packs only)", nullable=True, protected=True),
    VariableDef("emp.nationality", STRING, "Nationality (country packs only)", nullable=True, protected=True),
    VariableDef("emp.date_of_birth", DATE, "Date of birth (country packs only)", nullable=True, protected=True),
    VariableDef("emp.age", INTEGER, "Age in completed years on the period end date (country packs only)",
                nullable=True, protected=True),
)

PROTECTED_ATTRIBUTES = frozenset(
    {
        "gender", "sex", "gender_identity", "sexual_orientation", "religion", "caste", "ethnicity", "race",
        "marital_status", "nationality", "citizenship", "date_of_birth", "dob", "birth_date", "birthdate",
        "birth_year", "year_of_birth", "age",
    }
)


def is_protected(name: str) -> bool:
    """True for protected employee attributes, as ``emp.<name>`` or ``emp.attr.<name>``."""
    if name.startswith(CUSTOM_ATTRIBUTE_PREFIX):
        return name[len(CUSTOM_ATTRIBUTE_PREFIX):] in PROTECTED_ATTRIBUTES
    definition = VARIABLES.get(name)
    return definition is not None and definition.protected


class Stage(IntEnum):
    """Pipeline stages that produce component lines (3.5 §7). Earlier stages (1–3) produce variables."""

    EARNINGS = 4
    PRE_TAX_DEDUCTIONS = 5
    STATUTORY = 6
    TAX = 7
    POST_TAX_DEDUCTIONS = 8
    NET = 9
    EMPLOYER_COST = 10
