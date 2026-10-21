"""Snapshot builders shared by the engine tests (pure, no database), plus checks of the builders themselves.

The worked examples of the design are built here once: Acme India, November 2026 (30 days,
Sunday weekly off), CTC 12,00,000 with the structure of 3.3 §11 Example A. A test-only
statutory stage ("test pack") reproduces the illustrative EPF 3,000.00 / PT 200.00 of
3.5 §17 so the golden net can be checked without depending on a real pack.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.payroll.engine import pipeline
from app.payroll.engine.lines import STAGE_POST_TAX, STAGE_STATUTORY, CalcIssue, Line
from app.payroll.engine.snapshot import Snapshot

NOV_START, NOV_END = dt.date(2026, 11, 1), dt.date(2026, 11, 30)
OT_FORMULA_26 = 'SUM_TAG_RATE("OT_BASE") / 26 / 8'
MULTIPLIERS = [{"day_type": "WORKDAY", "after_hours": "0", "multiplier": "1.5"},
               {"day_type": "WEEKLY_OFF", "after_hours": "0", "multiplier": "2.0"},
               {"day_type": "HOLIDAY", "after_hours": "0", "multiplier": "2.0"}]


def component(code: str, kind: str = "EARNING", category: str = "BASE", **fields: Any) -> dict[str, Any]:
    return {"code": code, "kind": kind, "category": category, **fields}


SALARY_TAGS = ["LOP_APPLICABLE", "PART_OF_CTC", "PART_OF_GROSS"]
CATALOGUE: dict[str, dict[str, Any]] = {c["code"]: c for c in (
    component("BASIC", tags=[*SALARY_TAGS, "OT_BASE", "ENCASHMENT_BASE"], proration="BY_PAID_DAYS",
              calc_method="FORMULA", version_id=101, version=3, sort_order=10),
    component("HRA", category="FIXED_ALLOWANCE", tags=SALARY_TAGS, proration="BY_PAID_DAYS", calc_method="PERCENT_OF",
              version_id=102, version=1, sort_order=20),
    component("FIXED_ALLOWANCE", category="FIXED_ALLOWANCE", tags=SALARY_TAGS, proration="BY_PAID_DAYS",
              calc_method="BALANCING", version_id=103, version=1, sort_order=30),
    component("BONUS", category="BONUS", tags=["PART_OF_GROSS"], calc_method="INPUT", sort_order=60),
    component("OT_WORKDAY", category="OVERTIME", tags=["PART_OF_GROSS"], calc_method="RATE_X_UNITS", sort_order=50),
    component("OT_WEEKLY_OFF", category="OVERTIME", tags=["PART_OF_GROSS"], calc_method="RATE_X_UNITS", sort_order=51),
    component("OT_HOLIDAY", category="OVERTIME", tags=["PART_OF_GROSS"], calc_method="RATE_X_UNITS", sort_order=52),
    component("HOLIDAY_PREMIUM", category="HOLIDAY_PREMIUM", tags=["PART_OF_GROSS"], calc_method="RATE_X_UNITS",
              sort_order=53),
    component("WEEKLY_OFF_PREMIUM", category="HOLIDAY_PREMIUM", tags=["PART_OF_GROSS"], calc_method="RATE_X_UNITS",
              sort_order=54),
    component("SHIFT_ALLOWANCE", category="SHIFT_PREMIUM", tags=["PART_OF_GROSS"], sort_order=55),
    component("TASK_EARNING", category="TASK_EARNING", tags=["PART_OF_GROSS"], calc_method="RATE_X_UNITS",
              sort_order=56),
    component("MIN_WAGE_TOP_UP", category="TASK_EARNING", tags=["PART_OF_GROSS"], sort_order=57),
    component("ARREARS", category="RETRO_ADJUSTMENT", sort_order=70),
    component("REIMBURSEMENT", kind="REIMBURSEMENT", category="REIMBURSEMENT", calc_method="INPUT", sort_order=80),
    component("LOAN_RECOVERY", kind="DEDUCTION", category="LOAN_REPAYMENT", sort_order=200),
    component("OTHER_DEDUCTION", kind="DEDUCTION", category="VOLUNTARY", calc_method="INPUT", sort_order=210),
    component("LATE_PENALTY", kind="DEDUCTION", category="PENALTY_IF_LEGAL", sort_order=220),
    component("NET_ROUNDING", kind="ROUNDING", category="ROUNDING", sort_order=900),
)}


def catalogue(*extra: Mapping[str, Any], drop: Iterable[str] = ()) -> list[dict[str, Any]]:
    items = {code: dict(c) for code, c in CATALOGUE.items() if code not in set(drop)}
    for item in extra:
        items[item["code"]] = dict(item)
    return list(items.values())


def example_a_structure(ctc: str = "1200000", *, employer_pf: str = "3000.00") -> dict[str, Any]:
    """3.3 §11 Example A: BASIC = ctc.monthly × 50%, HRA = 40% of BASIC, FIXED_ALLOWANCE balancing."""
    return {
        "id": 812, "version": 2, "structure_id": 7, "structure_version_id": 71, "structure_type": "CTC_BASED",
        "currency": "INR", "pay_basis_amount": ctc, "ctc_employer_contributions": {"IN_EPF_ER": employer_pf},
        "lines": [
            {"component": "BASIC", "method": "FORMULA", "params": {"formula": "ctc.monthly * 50%"}, "order": 1},
            {"component": "HRA", "method": "PERCENT_OF", "params": {"percent": "40", "bases": ["BASIC"]}, "order": 2},
            {"component": "FIXED_ALLOWANCE", "method": "BALANCING", "order": 3},
        ],
    }


def days(start: dt.date = NOV_START, end: dt.date = NOV_END, *, weekly_off: Sequence[int] = (6,),
         segments: Sequence[tuple[str, dt.date, dt.date]] | None = None,
         per_day: Mapping[dt.date, Mapping[str, Any]] | None = None, employed_from: dt.date | None = None,
         employed_to: dt.date | None = None) -> list[dict[str, Any]]:
    """Every date of the period; ``per_day`` overrides fields of single dates (attendance, leaves, holidays...)."""
    segments = segments or [("S1", start, end)]
    result = []
    when = start
    while when <= end:
        segment = next((key for key, s, e in segments if s <= when <= e), "")
        employed = (employed_from is None or when >= employed_from) and (employed_to is None or when <= employed_to)
        item: dict[str, Any] = {"date": when.isoformat(), "weekly_off": when.weekday() in weekly_off,
                                "segment": segment, "employed": employed and segment != ""}
        item.update((per_day or {}).get(when, {}))
        result.append(item)
        when += dt.timedelta(days=1)
    return result


def absent(*dates: dt.date) -> dict[dt.date, dict[str, Any]]:
    return {d: {"manual_lop": [{"source_id": f"lop-{d.isoformat()}"}]} for d in dates}


SETTINGS = {
    "payroll.lop.proration_basis": "CALENDAR_DAYS",
    "payroll.attendance.enabled": False,
    "payroll.leave.enabled": False,
    "payroll.overtime.enabled": False,
    "payroll.wage_inputs.enabled": True,
    "payroll.reimbursements.enabled": True,
    "payroll.rounding.line_mode": "HALF_UP",
    "payroll.deductions.min_net_pay_percent": "50",
    "payroll.bank.split_payments_enabled": False,
}


def snapshot(**overrides: Any) -> dict[str, Any]:
    """A November 2026 snapshot for one employee with Example A, no LOP; ``overrides`` replace top-level keys."""
    data: dict[str, Any] = {
        "run": {"run_id": 1, "run_employee_id": 11, "environment": "test"},
        "employee": {"employee_id": 501, "code": "E501", "hire_date": "2022-04-01", "country": "IN",
                     "subdivision": "IN-MH", "standard_hours_per_day": "8"},
        "period": {"period_id": 8, "start": NOV_START.isoformat(), "end": NOV_END.isoformat(),
                   "pay_date": NOV_END.isoformat(), "number_in_tax_year": 8, "tax_year_start": "2026-04-01"},
        "currency": "INR",
        "settings": dict(SETTINGS),
        "components": catalogue(),
        "segments": [{"key": "S1", "start": NOV_START.isoformat(), "end": NOV_END.isoformat(), "assignment_id": 41,
                      "compensation": example_a_structure()}],
        "days": days(),
        "bank_accounts": [{"account_id": 9001, "type": "PRIMARY"}],
    }
    data.update(overrides)
    return data


def settings(**changes: Any) -> dict[str, Any]:
    result = dict(SETTINGS)
    for key, value in changes.items():
        result[key.replace("__", ".")] = value
    return result


def overtime_policy(**fields: Any) -> dict[str, Any]:
    return {"id": 31, "version": 1, "policy_hash": "ot-hash",
            "fields": {"buckets": MULTIPLIERS, "rate_formula": OT_FORMULA_26, "min_minutes": 30,
                       "rounding": {"increment_minutes": 15, "mode": "DOWN"}, **fields}}


def stat_line(code: str, kind: str, amount: str, *, stage: int = STAGE_STATUTORY,
              category: str = "EMPLOYEE_CONTRIBUTION") -> Line:
    return Line(code, kind, category, stage, Decimal(amount), "INR", statutory=True,
                explanation=f"test pack {code}", trace={"pack": "TEST"})


def pack_stage(epf: str = "3000.00", pt: str = "200.00", tax: str = "0.00"):
    """A test-only statutory stage: EPF employee/employer and PT as in 3.5 §17, optional tax."""

    def apply(ctx: Any, lines: Sequence[Line]) -> tuple[list[Line], list[CalcIssue]]:
        added = [stat_line("IN_EPF_EE", "DEDUCTION", epf), stat_line("IN_PT", "DEDUCTION", pt),
                 stat_line("IN_EPF_ER", "EMPLOYER_CONTRIBUTION", epf, category="EMPLOYER_CONTRIBUTION")]
        if Decimal(tax):
            added.append(stat_line("IN_TDS", "DEDUCTION", tax, stage=7, category="TAX"))
        return [*lines, *added], []

    return apply


def loan_stage(ctx: Any, lines: Sequence[Line]) -> tuple[list[Line], list[CalcIssue]]:
    """A test-only stage 8: each loan instalment in the snapshot becomes a LOAN_RECOVERY line."""
    added = [Line("LOAN_RECOVERY", "DEDUCTION", "LOAN_REPAYMENT", STAGE_POST_TAX, Decimal(str(loan["instalment"])),
                  "INR", sources=({"type": "loan", "id": loan["loan_id"]},), explanation="Schedule instalment")
             for loan in ctx.loans]
    added.extend(ctx.penalty_lines)
    return [*lines, *added], []


def no_stage(ctx: Any, lines: Sequence[Line]) -> tuple[list[Line], list[CalcIssue]]:
    return list(lines), []


def use_stages(monkeypatch: Any, statutory: Any = no_stage, deductions: Any = no_stage) -> None:
    """Replace stages 5-8 with test stages (the real modules are exercised in test_engine_pipeline)."""
    stages = {"statutory": statutory, "deductions": deductions}
    monkeypatch.setattr(pipeline, "_stage", lambda name: stages[name])


def parse(data: Mapping[str, Any]) -> Snapshot:
    return Snapshot.from_dict(data)


# --------------------------------------------------------------------------- the builders themselves


def test_builder_snapshot_round_trips():
    snap = parse(snapshot())
    assert Snapshot.from_dict(snap.to_dict()) == snap
    assert len(snap.days) == 30 and snap.days[0].date == NOV_START


def test_builder_absent_days_are_manual_lop():
    data = snapshot(days=days(per_day=absent(dt.date(2026, 11, 12), dt.date(2026, 11, 13))))
    snap = parse(data)
    assert [d.date.day for d in snap.days if d.manual_lop] == [12, 13]
