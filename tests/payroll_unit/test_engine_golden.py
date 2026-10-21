"""Golden worked examples through the whole engine, exact to the minor unit.

3.5 §17 (gross to net), 3.3 §11 Examples A-D, and 3.4 §17.1-§17.5, each built as a frozen
snapshot and calculated by ``engine.calculate``. Statutory figures come from a test-only
stage (illustrative EPF 3,000.00, PT 200.00) so the examples do not depend on any real pack.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.payroll.engine import calculate
from tests.payroll_unit.test_engine_fixtures import (
    NOV_END,
    NOV_START,
    absent,
    catalogue,
    component,
    days,
    example_a_structure,
    loan_stage,
    overtime_policy,
    pack_stage,
    settings,
    snapshot,
    use_stages,
)

D = Decimal


def amounts(result) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for line in result.lines:
        totals[line.component] = totals.get(line.component, D(0)) + line.amount
    return totals


def codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def nov(day: int) -> dt.date:
    return dt.date(2026, 11, day)


def worked(seconds: int, **extra) -> dict:
    return {"attendance": {"status": "PRESENT", "session_id": f"s-{seconds}", "worked_seconds": seconds, **extra}}


def all_workdays(seconds: int, *, overrides: dict | None = None) -> dict:
    """Attendance on every Monday-Saturday of November 2026 with ``seconds`` of net time."""
    result = {}
    for day in range(1, 31):
        when = nov(day)
        if when.weekday() != 6:
            result[when] = worked(seconds)
    result.update(overrides or {})
    return result


def gross_structure(basic: str, *, structure_type: str = "GROSS_BASED") -> dict:
    return {"id": 900, "version": 1, "structure_type": structure_type, "currency": "INR", "pay_basis_amount": basic,
            "lines": [{"component": "BASIC", "method": "FIXED", "amount": basic}]}


def segment(compensation: dict, start: dt.date = NOV_START, end: dt.date = NOV_END, key: str = "S1") -> dict:
    return {"key": key, "start": start.isoformat(), "end": end.isoformat(), "assignment_id": 41,
            "compensation": compensation}


# --------------------------------------------------------------------------- 3.5 §17


def section_17_snapshot() -> dict:
    return snapshot(
        days=days(per_day=absent(nov(12), nov(13))),
        settings=settings(payroll__overtime__enabled=True),
        policies={"OVERTIME_POLICY": overtime_policy()},
        overtime_entries=[{"entry_id": "ot-1", "date": "2026-11-05", "minutes": 240}],
        wage_inputs=[{"id": 77, "component": "BONUS", "amount": "10000.00", "reason": "Festival bonus"}],
        loans=[{"loan_id": 5, "instalment": "2000.00"}],
    )


def test_section_17_gross_to_net_golden(monkeypatch):
    """3.5 §17: Acme India, November 2026, 2 LOP days, 4 h workday overtime, bonus, EPF, PT, loan; tax 0."""
    use_stages(monkeypatch, statutory=pack_stage(), deductions=loan_stage)
    result = calculate(section_17_snapshot())
    got = amounts(result)
    assert got["BASIC"] == D("46666.67")
    assert got["HRA"] == D("18666.67")
    assert got["FIXED_ALLOWANCE"] == D("25200.00")
    assert got["OT_WORKDAY"] == D("1442.31")
    assert got["BONUS"] == D("10000.00")
    assert result.totals.gross == D("101975.65")
    assert got["IN_EPF_EE"] == D("3000.00") and got["IN_PT"] == D("200.00") and got["LOAN_RECOVERY"] == D("2000.00")
    assert result.totals.deductions == D("5200.00")
    assert result.totals.net == D("96775.65")
    assert result.totals.employer_cost == D("104975.65")
    assert result.ok, [i.to_json() for i in result.blocking]
    assert [p.amount for p in result.payments] == [D("96775.65")]


def test_section_17_explanations_and_trace(monkeypatch):
    use_stages(monkeypatch, statutory=pack_stage(), deductions=loan_stage)
    result = calculate(section_17_snapshot())
    basic = result.line("BASIC")
    assert basic.explanation == "50,000.00 × 28 ÷ 30 (2 LOP days: 12–13 Nov)"
    assert basic.rate == D("50000") and basic.units == D("28")
    assert basic.trace["method"] == "FORMULA" and basic.trace["formula"] == "ctc.monthly * 50%"
    assert basic.trace["proration"]["basis"] == "CALENDAR_DAYS" and basic.trace["proration"]["basis_days"] == "30"
    assert basic.trace["unrounded"].startswith("46666.6666666")
    assert basic.trace["rounding"] == {"stage": "LINE", "mode": "HALF_UP", "increment": "0.01"}
    assert {"type": "day_ledger", "segment": "S1", "lop_days": "2", "dates": ["2026-11-12", "2026-11-13"]} \
        in basic.sources
    assert basic.component_version_id == 101
    ot = result.line("OT_WORKDAY")
    assert ot.explanation == "50,000.00 ÷ 26 ÷ 8 = 240.3846…; 240.3846… × 1.5 × 4 h"
    assert result.line("BONUS").sources[0]["type"] == "wage_input"
    assert all(line.explanation for line in result.lines)


def test_section_17_with_tax_reduces_net_by_tax(monkeypatch):
    use_stages(monkeypatch, statutory=pack_stage(tax="1234.00"), deductions=loan_stage)
    result = calculate(section_17_snapshot())
    assert result.totals.net == D("96775.65") - D("1234.00")


# --------------------------------------------------------------------------- 3.3 §11 Example A


def test_example_a_ctc_structure_full_month(monkeypatch):
    """3.3 §11 Example A: BASIC 50,000, HRA 20,000, balancing 27,000; gross 97,000; employer cost 1,00,000."""
    use_stages(monkeypatch, statutory=pack_stage())
    result = calculate(snapshot())
    got = amounts(result)
    assert (got["BASIC"], got["HRA"], got["FIXED_ALLOWANCE"]) == (D("50000.00"), D("20000.00"), D("27000.00"))
    assert result.totals.gross == D("97000.00")
    assert result.totals.net == D("93800.00")
    assert result.totals.employer_cost == D("100000.00")
    fixed = result.line("FIXED_ALLOWANCE")
    assert fixed.explanation == "27,000.00 × 30 ÷ 30 (full period)"
    assert any("IN_EPF_ER 3000.00" in note for note in fixed.trace["notes"])


def test_example_a_lop_variant(monkeypatch):
    """Example A with 2 LOP days: 46,666.67 / 18,666.67 / 25,200.00 (the balancing keeps 27,000 and is prorated)."""
    use_stages(monkeypatch, statutory=pack_stage())
    result = calculate(snapshot(days=days(per_day=absent(nov(12), nov(13)))))
    got = amounts(result)
    assert (got["BASIC"], got["HRA"], got["FIXED_ALLOWANCE"]) == (D("46666.67"), D("18666.67"), D("25200.00"))
    assert result.line("FIXED_ALLOWANCE").rate == D("27000")


def test_example_a_before_ceiling_change_balancing_is_28200(monkeypatch):
    """Before 17 Sep 2026 the employer PF inside CTC was 1,800.00, so the balancing allowance is 28,200.00."""
    use_stages(monkeypatch, statutory=pack_stage(epf="1800.00"))
    data = snapshot(segments=[segment(example_a_structure(employer_pf="1800.00"))])
    result = calculate(data)
    assert amounts(result)["FIXED_ALLOWANCE"] == D("28200.00")
    assert result.totals.employer_cost == D("100000.00")


def test_example_a_negative_balancing_is_blocking_ec_cmp_04(monkeypatch):
    """EC-CMP-04: a CTC too low for the structure makes the balancing component negative: blocking, no line."""
    use_stages(monkeypatch)
    data = snapshot(segments=[segment(example_a_structure(ctc="12000", employer_pf="3000.00"))])
    result = calculate(data)
    assert "FIXED_ALLOWANCE" not in amounts(result)
    errors = [i for i in result.blocking if i.code == "FORMULA_ERROR" and i.component == "FIXED_ALLOWANCE"]
    assert errors and errors[0].details["error"]["code"] == "NEGATIVE_RESULT"


# --------------------------------------------------------------------------- 3.3 §11 Example B


def test_example_b_hourly_worker(monkeypatch):
    """Example B: 250.00/h × 182.50 payable hours = 45,625.00; overtime 250 × 1.5 × 10 = 3,750.00; gross 49,375.00."""
    use_stages(monkeypatch)
    hourly = {"id": 950, "version": 1, "structure_type": "HOURLY", "currency": "INR", "pay_basis_amount": "250.00",
              "lines": [{"component": "BASIC", "method": "RATE_X_UNITS",
                         "params": {"rate": "250.00", "units": "att.payable_hours"}}]}
    data = snapshot(
        employee={"employee_id": 502, "hire_date": "2023-01-01", "country": "IN", "salaried": False,
                  "standard_hours_per_day": "8"},
        segments=[segment(hourly)],
        days=days(per_day=all_workdays(26280)),  # 25 working days × 7.3 h = 182.5 h
        settings=settings(payroll__attendance__enabled=True, payroll__overtime__enabled=True),
        policies={"OVERTIME_POLICY": overtime_policy(rate_formula='RATE("BASIC")')},
        overtime_entries=[{"entry_id": "ot-b", "date": "2026-11-10", "minutes": 600}],
    )
    result = calculate(data)
    got = amounts(result)
    assert got["BASIC"] == D("45625.00")
    assert result.line("BASIC").explanation == "250.00 × 182.5 hours"
    assert got["OT_WORKDAY"] == D("3750.00")
    assert result.totals.gross == D("49375.00")


# --------------------------------------------------------------------------- 3.3 §11 Examples C and D


def test_example_c_cycle_is_blocking_ec_cmp_01(monkeypatch):
    """Example C: HRA = FIXED_ALLOWANCE × 10% with a balancing FIXED_ALLOWANCE is a cycle; nobody is paid zero."""
    use_stages(monkeypatch)
    structure = example_a_structure()
    structure["lines"][1] = {"component": "HRA", "method": "FORMULA", "params": {"formula": "FIXED_ALLOWANCE * 10%"},
                             "order": 2}
    result = calculate(snapshot(segments=[segment(structure)]))
    cycle = [i for i in result.blocking if i.code == "FORMULA_ERROR" and i.details["error"]["code"] == "CYCLE_DETECTED"]
    assert cycle and cycle[0].details["error"]["path"] == ["HRA", "FIXED_ALLOWANCE", "HRA"]
    assert not {"HRA", "FIXED_ALLOWANCE"} & set(amounts(result))
    assert not result.ok


def test_example_d_currency_mismatch_ec_cmp_07(monkeypatch):
    """Example D: an AED pay group reading a USD table fails with CURRENCY_MISMATCH; no implicit FX."""
    use_stages(monkeypatch)
    structure = {"id": 960, "version": 1, "structure_type": "GROSS_BASED", "currency": "AED", "pay_basis_amount": "9000",
                 "lines": [{"component": "BASIC", "method": "FORMULA",
                            "params": {"formula": 'LOOKUP("BASIC_USD", emp.grade, 0)'}}]}
    data = snapshot(
        currency="AED",
        employee={"employee_id": 503, "hire_date": "2023-01-01", "country": "AE", "grade": "G3"},
        segments=[segment(structure)],
        tables=[{"code": "BASIC_USD", "kind": "LOOKUP", "value_currency": "USD",
                 "rows": [{"key": "G3", "value": "4000"}]}],
        bank_accounts=[],
    )
    result = calculate(data)
    errors = [i for i in result.blocking if i.code == "FORMULA_ERROR" and i.component == "BASIC"]
    assert errors and errors[0].details["error"]["code"] == "CURRENCY_MISMATCH"
    assert "BASIC" not in amounts(result)


# --------------------------------------------------------------------------- 3.4 §17


def test_3_4_17_1_overtime_daily_then_weekly_ec_ot_03(monkeypatch):
    """3.4 §17.1: 10/9/9/11/9/8 h with daily 9 h and weekly 48 h → 3 daily + 5 weekly hours × 125.00 × 1.5 = 1,500.00."""
    use_stages(monkeypatch)
    week = {nov(2): worked(36000), nov(3): worked(32400), nov(4): worked(32400), nov(5): worked(39600),
            nov(6): worked(32400), nov(7): worked(28800)}
    data = snapshot(
        segments=[segment(gross_structure("26000.00"))],
        days=days(per_day=all_workdays(28800, overrides=week)),
        settings=settings(payroll__attendance__enabled=True, payroll__overtime__enabled=True),
        policies={"OVERTIME_POLICY": overtime_policy(
            source="ATTENDANCE_AUTO", daily_threshold="9", weekly_threshold_hours="48",
            threshold_combination="DAILY_THEN_WEEKLY")},
    )
    result = calculate(data)
    ot = result.line("OT_WORKDAY")
    assert ot.amount == D("1500.00") and ot.units == D("8.0000") and ot.rate == D("125")
    assert ot.explanation.startswith("26,000.00 ÷ 26 ÷ 8 = 125.00; ")


def test_3_4_17_2_holiday_work_premium_and_overtime_ec_hol_02(monkeypatch):
    """3.4 §17.2: 11 h on a public holiday: premium 125 × 1.0 × 8 = 1,000.00 and OT 125 × 2.0 × 3 = 750.00."""
    use_stages(monkeypatch)
    holiday = {nov(11): {"holiday": {"holiday_id": 3, "type": "PUBLIC", "name": "Festival"}}}
    data = snapshot(
        segments=[segment(gross_structure("26000.00"))],
        days=days(per_day=holiday),
        settings=settings(payroll__overtime__enabled=True, payroll__holiday_work_premium__enabled=True),
        policies={"OVERTIME_POLICY": overtime_policy(),
                  "HOLIDAY_PAY_POLICY": {"fields": {"holiday_work_multiplier": "1.0"}}},
        non_working_day_work=[{"date": "2026-11-11", "day_type": "HOLIDAY", "worked_minutes": 660, "ref": "hw-1"}],
    )
    result = calculate(data)
    got = amounts(result)
    assert got["HOLIDAY_PREMIUM"] == D("1000.00")
    assert got["OT_HOLIDAY"] == D("750.00")
    assert got["BASIC"] == D("26000.00")  # salary already pays the holiday itself


def test_3_4_17_2_premium_switch_off_sends_all_hours_to_overtime_f_05(monkeypatch):
    """I-16 split (F-05): with the holiday-work premium OFF, all 11 h go to the overtime HOLIDAY bucket."""
    use_stages(monkeypatch)
    holiday = {nov(11): {"holiday": {"holiday_id": 3, "type": "PUBLIC"}}}
    data = snapshot(
        segments=[segment(gross_structure("26000.00"))], days=days(per_day=holiday),
        settings=settings(payroll__overtime__enabled=True, payroll__holiday_work_premium__enabled=False,
                          payroll__holiday_pay__non_salaried__enabled=True),
        policies={"OVERTIME_POLICY": overtime_policy(), "HOLIDAY_PAY_POLICY": {"fields": {"holiday_work_multiplier": "1.0"}}},
        non_working_day_work=[{"date": "2026-11-11", "day_type": "HOLIDAY", "worked_minutes": 660}],
    )
    result = calculate(data)
    got = amounts(result)
    assert "HOLIDAY_PREMIUM" not in got
    assert got["OT_HOLIDAY"] == D("2750.00")  # 125 × 2.0 × 11


def test_3_4_17_3_task_cap_and_minimum_wage_top_up_ec_tsk_06_07(monkeypatch):
    """3.4 §17.3: 25 × 800 = 20,000 capped at 15,000 (5,000 carried); 160 h × 100 = 16,000 → top-up 1,000.00."""
    use_stages(monkeypatch)
    task_based = {"id": 970, "version": 1, "structure_type": "TASK_BASED", "currency": "INR", "lines": []}
    data = snapshot(
        employee={"employee_id": 504, "hire_date": "2024-01-01", "country": "IN", "salaried": False},
        segments=[segment(task_based)],
        days=days(per_day=all_workdays(23040)),  # 25 days × 6.4 h = 160 h
        settings=settings(payroll__attendance__enabled=True, payroll__task_pay__enabled=True),
        policies={"TASK_PAY_POLICY": {"fields": {
            "rate_source": "RATE_CARD", "cap_per_period": {"amount": "15000.00", "currency": "INR"},
            "cap_action": "TRUNCATE_AND_CARRY", "minimum_wage_floor": "TOP_UP"}}},
        tasks={"records": [{"task_id": "T-1", "task_type": "SITE_INSTALLATION", "status": "APPROVED", "quantity": "25",
                            "assignees": [{"employee_id": "504"}], "approved_on": "2026-11-20"}],
               "rate_card": [{"task_type": "SITE_INSTALLATION", "rate": "800.00", "currency": "INR",
                              "effective_from": "2026-01-01"}],
               "minimum_wage": {"min_hourly_rate": "100.00", "verified": True}},
    )
    result = calculate(data)
    got = amounts(result)
    assert got["TASK_EARNING"] == D("15000.00")
    assert got["MIN_WAGE_TOP_UP"] == D("1000.00")
    assert result.line("TASK_EARNING").trace["carried_out"] == "5000.00"
    assert "TASK_CAP_CARRIED" in codes(result)


def test_3_4_17_4_late_penalty_one_day_lop(monkeypatch):
    """3.4 §17.4: 9 late marks, 1 regularised, 2 free, 3 per 0.5 day → 1.0 day of penalty LOP."""
    use_stages(monkeypatch)
    late_days = [2, 3, 4, 5, 6, 9, 10, 11, 12]
    marks = {nov(d): worked(28800, late=True, regularised=(d == 12)) for d in late_days}
    data = snapshot(
        days=days(per_day=all_workdays(28800, overrides=marks)),
        settings=settings(payroll__attendance__enabled=True, payroll__late_penalty__enabled=True),
        policies={"LATE_PENALTY_POLICY": {"fields": {"free_late_marks": 2, "late_marks_per_unit": 3, "unit_days": "0.5",
                                                     "cap_days_per_period": "2.0"}}},
    )
    result = calculate(data)
    got = amounts(result)
    assert got["BASIC"] == D("48333.33")  # 50,000 × 29 ÷ 30
    assert got["FIXED_ALLOWANCE"] == D("26100.00")
    assert result.line("BASIC").explanation == "50,000.00 × 29 ÷ 30 (1 penalty LOP day)"
    assert result.ledger["totals"]["penalty_lop_days"] == "1"


def test_3_4_17_5_day_ledger_week(monkeypatch):
    """3.4 §17.5: a Monday-Sunday week with present, half leave, holiday, absence, unpaid leave, half day: 2.5 LOP."""
    use_stages(monkeypatch)
    week_start, week_end = nov(2), nov(8)
    per_day = {
        nov(2): {"attendance": {"status": "PRESENT", "worked_seconds": 28800}},
        nov(3): {"leaves": [{"leave_id": 1, "leave_type_code": "CASUAL", "portion": "FIRST_HALF"}],
                 "attendance": {"status": "PRESENT", "worked_seconds": 14400}},
        nov(4): {"holiday": {"holiday_id": 9, "type": "PUBLIC"}},
        nov(5): {"attendance": {"status": "ABSENT"}},
        nov(6): {"leaves": [{"leave_id": 2, "leave_type_code": "UNPAID", "pay_treatment": "UNPAID"}]},
        nov(7): {"attendance": {"status": "HALF_DAY", "worked_seconds": 14400}},
    }
    weekly = {"id": 980, "version": 1, "structure_type": "GROSS_BASED", "currency": "INR",
              "lines": [{"component": "BASIC", "method": "FIXED", "amount": "7000.00"}]}
    data = snapshot(
        period={"start": week_start.isoformat(), "end": week_end.isoformat(), "pay_date": week_end.isoformat(),
                "frequency": "WEEKLY", "periods_per_year": 52},
        segments=[segment(weekly, week_start, week_end)],
        days=days(week_start, week_end, per_day=per_day),
        settings=settings(payroll__attendance__enabled=True, payroll__leave__enabled=True),
    )
    result = calculate(data)
    paid = {d["date"]: (d["paid"], d["lop"]) for d in result.ledger["days"]}
    assert paid == {"2026-11-02": ("1", "0"), "2026-11-03": ("1", "0"), "2026-11-04": ("1", "0"),
                    "2026-11-05": ("0", "1"), "2026-11-06": ("0", "1"), "2026-11-07": ("0.5", "0.5"),
                    "2026-11-08": ("1", "0")}
    assert result.ledger["totals"]["lop_days"] == "2.5"
    assert amounts(result)["BASIC"] == D("4500.00")  # 7,000 × 4.5 ÷ 7
    assert result.line("BASIC").explanation == "7,000.00 × 4.5 ÷ 7 (2.5 LOP days: 5–7 Nov)"


@pytest.mark.parametrize("basis,expected", [
    ("CALENDAR_DAYS", "27857.14"), ("WORKING_DAYS", "27500.00"), ("FIXED_30", "28000.00"), ("FIXED_26", "27692.31"),
])
def test_3_4_5_2_bases_february_two_lop_days(monkeypatch, basis, expected):
    """3.4 §5.2 scenario 1: BASIC 30,000 in February 2026 (Mon-Sat week) with 2 LOP working days."""
    use_stages(monkeypatch)
    start, end = dt.date(2026, 2, 1), dt.date(2026, 2, 28)
    data = snapshot(
        period={"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat()},
        segments=[segment(gross_structure("30000.00"), start, end)],
        days=days(start, end, per_day=absent(dt.date(2026, 2, 10), dt.date(2026, 2, 11))),
        settings=settings(payroll__lop__proration_basis=basis),
    )
    assert amounts(calculate(data))["BASIC"] == D(expected)


@pytest.mark.parametrize("basis,expected", [
    ("CALENDAR_DAYS", "13928.57"), ("WORKING_DAYS", "15000.00"), ("FIXED_30", "13000.00"), ("FIXED_26", "13846.15"),
])
def test_3_4_5_2_bases_february_joiner(monkeypatch, basis, expected):
    """3.4 §5.2 scenario 2: joined Monday 16 February 2026, no LOP."""
    use_stages(monkeypatch)
    start, end, hired = dt.date(2026, 2, 1), dt.date(2026, 2, 28), dt.date(2026, 2, 16)
    data = snapshot(
        employee={"employee_id": 505, "hire_date": hired.isoformat(), "country": "IN"},
        period={"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat()},
        segments=[segment(gross_structure("30000.00"), hired, end)],
        days=days(start, end, segments=[("S1", hired, end)]),
        settings=settings(payroll__lop__proration_basis=basis),
    )
    result = calculate(data)
    assert amounts(result)["BASIC"] == D(expected)
    assert "NEW_JOINER" in codes(result)


def test_catalogue_extra_components_are_optional(monkeypatch):
    """A formula may read a catalogue component outside the structure: it is NULL, so COALESCE decides."""
    use_stages(monkeypatch)
    structure = example_a_structure()
    structure["lines"].append({"component": "SPECIAL", "method": "FORMULA",
                               "params": {"formula": "COALESCE(DA, 0) + 500"}, "order": 4})
    data = snapshot(segments=[segment(structure)],
                    components=catalogue(component("SPECIAL", category="FIXED_ALLOWANCE"),
                                         component("DA", category="FIXED_ALLOWANCE")))
    assert amounts(calculate(data))["SPECIAL"] == D("500.00")
