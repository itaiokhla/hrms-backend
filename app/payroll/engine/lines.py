"""Payroll line and calculation-issue types shared by every engine stage (3.5 §7-§8).

A line is one component amount for one employee in one calculation, with its trace. Amounts
are Decimals already rounded at the line stage (3.2 §3.3); the unrounded value lives in the
trace. Lines are immutable; stages return new lines.
"""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

KINDS = (
    "EARNING", "DEDUCTION", "EMPLOYER_CONTRIBUTION", "REIMBURSEMENT", "BENEFIT_IN_KIND", "INFORMATIONAL", "ROUNDING",
)
# Pipeline stages of 3.5 §7 that produce lines.
STAGE_EARNINGS = 4
STAGE_PRE_TAX = 5
STAGE_STATUTORY = 6
STAGE_TAX = 7
STAGE_POST_TAX = 8
STAGE_NET = 9
STAGE_EMPLOYER_COST = 10
VISIBILITIES = ("EMPLOYEE_VISIBLE", "HR_ONLY")
SEVERITIES = ("BLOCKING", "WARNING", "FLAG")


@dataclass(frozen=True)
class Line:
    component: str                       # component code, e.g. "BASIC", "OT_WORKDAY", "ZZ_PENSION_EE"
    kind: str                            # one of KINDS
    category: str                        # 3.3 §2.1 category, e.g. "BASE", "OVERTIME", "EMPLOYEE_CONTRIBUTION"
    stage: int                           # one of the STAGE_* constants
    amount: Decimal                      # rounded to the currency minor unit (or the pack's statutory rule)
    currency: str
    component_version_id: Optional[int] = None
    units: Optional[Decimal] = None      # days, hours or pieces paid
    rate: Optional[Decimal] = None       # full-period rate or per-unit rate
    period_ref: Optional[str] = None     # original period "YYYY-MM-DD..YYYY-MM-DD" for arrears/recoveries
    segment: Optional[int] = None        # assignment segment index for split periods
    tags: Tuple[str, ...] = ()
    visibility: str = "EMPLOYEE_VISIBLE"
    sources: Tuple[Dict[str, Any], ...] = ()   # {"type": "wage_input", "id": 12, ...}
    trace: Dict[str, Any] = field(default_factory=dict)
    explanation: str = ""                # plain sentence shown first in "Why this amount?"
    sort_order: int = 0
    statutory: bool = False              # produced by a country pack
    fixture: bool = False                # produced by a fixture pack: "FIXTURE — not law" (decision I-07)

    def with_amount(self, amount: Decimal, **changes: Any) -> "Line":
        return replace(self, amount=amount, **changes)

    def to_json(self) -> Dict[str, Any]:
        from app.payroll.money import amount_str

        return {
            "component": self.component,
            "component_version_id": self.component_version_id,
            "kind": self.kind,
            "category": self.category,
            "stage": self.stage,
            "amount": amount_str(self.amount, self.currency),
            "currency": self.currency,
            "units": None if self.units is None else str(self.units),
            "rate": None if self.rate is None else str(self.rate),
            "period_ref": self.period_ref,
            "segment": self.segment,
            "tags": list(self.tags),
            "visibility": self.visibility,
            "sources": [dict(s) for s in self.sources],
            "trace": self.trace,
            "explanation": self.explanation,
            "sort_order": self.sort_order,
            "statutory": self.statutory,
            "fixture": self.fixture,
        }


@dataclass(frozen=True)
class CalcIssue:
    """A data-dependent problem found while calculating one employee (never raised; 3.5 §5.2, §10)."""

    code: str                            # e.g. NO_SALARY, NEGATIVE_NET, FORMULA_ERROR, FX_MISSING, ZERO_PAID_WORKING_DAYS
    severity: str                        # one of SEVERITIES
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    component: Optional[str] = None
    waivable: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {
            "code": self.code, "severity": self.severity, "message": self.message, "details": self.details,
            "component": self.component, "waivable": self.waivable,
        }
