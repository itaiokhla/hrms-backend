"""Engine pipeline behaviour: exclusions, guards, errors, segments, rounding, payments, validation and replay.

Each test names the edge case it covers (EC-*). Stages 5-8 are test stages unless a test says
it uses the real statutory module.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.payroll.engine import ENGINE_VERSION, calculate, pipeline, replay
from app.payroll.engine.context import build_context
from app.payroll.engine.lines import Line
from app.payroll.engine.net import PaymentLine, totals
from app.payroll.engine.snapshot import Snapshot
from app.payroll.engine.validate import invariants
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
    stat_line,
    use_stages,
)

D = Decimal


def nov(day: int) -> dt.date:
    return dt.date(2026, 11, day)


def amounts(result) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for line in result.lines:
        found[line.component] = found.get(line.component, D(0)) + line.amount
    return found


def issue(result, code: str):
    found = [i for i in result.issues if i.code == code]
    return found[0] if found else None


def fixed(amount: str, *, code: str = "BASIC", structure_type: str = "GROSS_BASED", extra: list | None = None) -> dict:
    return {"id": 600, "version": 1, "structure_type": structure_type, "currency": "INR", "pay_basis_amount": amount,
            "lines": [{"component": code, "method": "FIXED", "amount": amount}, *(extra or [])]}


def seg(compensation, start=NOV_START, end=NOV_END, key="S1") -> dict:
    return {"key": key, "start": start.isoformat(), "end": end.isoformat(), "compensation": compensation}


@pytest.fixture(autouse=True)
def _test_stages(monkeypatch):
    use_stages(monkeypatch, statutory=pack_stage(epf="0.00", pt="0.00"))


# --------------------------------------------------------------------------- exclusions and data errors


def test_no_salary_excluded_never_zero_payslip_ec_cmp_14():
    result = calculate(snapshot(segments=[seg(None)]))
    assert result.excluded and not result.lines and not result.payments
    assert issue(result, "NO_SALARY").severity == "BLOCKING"
    assert result.totals.net == 0


def test_segment_without_salary_is_blocking_but_others_paid():
    data = snapshot(segments=[seg(fixed("30000.00"), NOV_START, nov(15), "S1"), seg(None, nov(16), NOV_END, "S2")],
                    days=days(segments=[("S1", NOV_START, nov(15)), ("S2", nov(16), NOV_END)]))
    result = calculate(data)
    assert not result.excluded
    assert amounts(result)["BASIC"] == D("15000.00")
    assert issue(result, "NO_SALARY").details["segment"] == "S2"


def test_compensation_currency_mismatch_needs_fx_ec_cmp_07():
    structure = fixed("1000.00")
    structure["currency"] = "USD"
    result = calculate(snapshot(segments=[seg(structure)]))
    assert issue(result, "CURRENCY_MISMATCH").severity == "BLOCKING"
    assert not result.lines


def test_missing_dates_are_a_ledger_input_error_f_04():
    result = calculate(snapshot(days=days(NOV_START, nov(29))))
    assert issue(result, "LEDGER_INPUT_ERROR").severity == "BLOCKING"
    assert not result.lines


def test_division_by_zero_blocks_with_trace_never_zero_ec_cmp_02():
    structure = example_a_structure()
    structure["lines"].append({"component": "ODD", "method": "FORMULA",
                               "params": {"formula": "ctc.monthly / (att.paid_days - 30)"}, "order": 4})
    structure["lines"].append({"component": "ODD_TOP_UP", "method": "FORMULA", "params": {"formula": "ODD * 10%"},
                               "order": 5})
    data = snapshot(segments=[seg(structure)], components=catalogue(
        component("ODD", category="VARIABLE"), component("ODD_TOP_UP", category="VARIABLE")))
    result = calculate(data)
    error = [i for i in result.blocking if i.code == "FORMULA_ERROR" and i.component == "ODD"][0]
    assert error.details["error"]["code"] == "DIVISION_BY_ZERO"
    assert error.details["steps"], "the trace up to the failure is kept"
    assert issue(result, "DEPENDS_ON_FAILED").component == "ODD_TOP_UP"
    assert "ODD" not in amounts(result) and "ODD_TOP_UP" not in amounts(result)
    assert amounts(result)["BASIC"] == D("50000.00")
    assert not result.ok


def test_null_variable_needs_coalesce_ec_cmp_03():
    structure = example_a_structure()
    structure["lines"].append({"component": "MEAL", "method": "FORMULA", "params": {"formula": "att.present_days * 100"}})
    data = snapshot(segments=[seg(structure)], components=catalogue(component("MEAL", category="FIXED_ALLOWANCE")))
    result = calculate(data)
    assert [i for i in result.blocking if i.component == "MEAL"][0].details["error"]["code"] == "NULL_VALUE"
    structure["lines"][-1]["params"]["formula"] = "COALESCE(att.present_days, 20) * 100"
    assert amounts(calculate(snapshot(segments=[seg(structure)], components=catalogue(
        component("MEAL", category="FIXED_ALLOWANCE")))))["MEAL"] == D("2000.00")


def test_unknown_component_in_structure_is_blocking():
    structure = fixed("1000.00", extra=[{"component": "GHOST", "method": "FIXED", "amount": "5"}])
    result = calculate(snapshot(segments=[seg(structure)]))
    assert [i for i in result.blocking if i.component == "GHOST"][0].details["error"]["code"] == "UNKNOWN_REFERENCE"


# --------------------------------------------------------------------------- guards


def all_working_days_lop():
    return absent(*(nov(d) for d in range(1, 31) if nov(d).weekday() != 6))


def test_zero_paid_working_days_exception_ec_att_09():
    result = calculate(snapshot(days=days(per_day=all_working_days_lop())))
    guard = issue(result, "ZERO_PAID_WORKING_DAYS")
    assert guard.severity == "BLOCKING"
    assert not result.ok


def test_zero_paid_working_days_pay_zero_ec_att_09():
    data = snapshot(days=days(per_day=all_working_days_lop()), settings=settings(payroll__lop__zero_paid_days="PAY_ZERO"))
    result = calculate(data)
    assert issue(result, "ZERO_PAID_WORKING_DAYS").severity == "FLAG"
    assert amounts(result)["BASIC"] == 0 and amounts(result)["FIXED_ALLOWANCE"] == 0
    assert "PAY_ZERO" in result.line("BASIC").explanation


def test_holiday_calendar_covering_every_working_day_ec_att_10():
    holidays = {nov(d): {"holiday": {"holiday_id": d, "type": "COMPANY"}} for d in range(1, 31) if nov(d).weekday() != 6}
    result = calculate(snapshot(days=days(per_day=holidays)))
    guard = issue(result, "ZERO_PAID_WORKING_DAYS")
    assert guard.severity == "BLOCKING" and "calendar" in guard.message


def test_unmarked_day_with_attendance_on_is_review_item_not_deduction():
    data = snapshot(settings=settings(payroll__attendance__enabled=True))
    result = calculate(data)
    assert issue(result, "UNMARKED_DAY").severity == "BLOCKING"


def test_negative_net_never_clipped_ec_run_10(monkeypatch):
    use_stages(monkeypatch, statutory=pack_stage(epf="0.00", pt="0.00"), deductions=loan_stage)
    result = calculate(snapshot(loans=[{"loan_id": 1, "instalment": "150000.00"}]))
    assert result.totals.net == D("97000.00") - D("150000.00")
    assert issue(result, "NEGATIVE_NET").severity == "BLOCKING"
    assert not result.payments
    assert not any(line.component == "NET_ROUNDING" for line in result.lines)


# --------------------------------------------------------------------------- segments (EC-CMP-16, F-01)


def test_mid_period_revision_splits_into_segments_ec_cmp_16():
    data = snapshot(segments=[seg(fixed("30000.00"), NOV_START, nov(15), "S1"),
                              seg(fixed("36000.00"), nov(16), NOV_END, "S2")],
                    days=days(segments=[("S1", NOV_START, nov(15)), ("S2", nov(16), NOV_END)]))
    result = calculate(data)
    basic = [line for line in result.lines if line.component == "BASIC"]
    assert [(line.segment, line.amount) for line in basic] == [(0, D("15000.00")), (1, D("18000.00"))]
    assert basic[0].explanation == "30,000.00 × 15 ÷ 30 for 1–15 Nov"
    assert basic[1].rate == D("36000.00")


def test_segments_of_fully_paid_period_add_up_to_the_rate_f_01():
    """Cumulative segment rounding: three 10-day segments of 50,000.00 add up to exactly 50,000.00."""
    cuts = [("S1", NOV_START, nov(10)), ("S2", nov(11), nov(20)), ("S3", nov(21), NOV_END)]
    data = snapshot(segments=[seg(fixed("50000.00"), s, e, k) for k, s, e in cuts], days=days(segments=cuts))
    result = calculate(data)
    parts = [line.amount for line in result.lines if line.component == "BASIC"]
    assert parts == [D("16666.67"), D("16666.66"), D("16666.67")]
    assert sum(parts) == D("50000.00")


def test_fixed_basis_segments_split_the_fixed_days():
    cuts = [("S1", NOV_START, nov(15)), ("S2", nov(16), NOV_END)]
    data = snapshot(segments=[seg(fixed("26000.00"), s, e, k) for k, s, e in cuts], days=days(segments=cuts),
                    settings=settings(payroll__lop__proration_basis="FIXED_26"))
    parts = [line.amount for line in calculate(data).lines if line.component == "BASIC"]
    assert sum(parts) == D("26000.00")


# --------------------------------------------------------------------------- components


def test_eligibility_and_frequency_skip_components():
    structure = fixed("1000.00", extra=[
        {"component": "SITE", "method": "FIXED", "amount": "500"},
        {"component": "MARCH_ONLY", "method": "FIXED", "amount": "700"},
    ])
    data = snapshot(segments=[seg(structure)], components=catalogue(
        component("SITE", category="FIXED_ALLOWANCE", eligibility='emp.employment_type = "CONTRACT"'),
        component("MARCH_ONLY", category="FIXED_ALLOWANCE", frequency="SPECIFIC_MONTHS", months=[3])))
    got = amounts(calculate(data))
    assert "SITE" not in got and "MARCH_ONLY" not in got and got["BASIC"] == D("1000.00")


def test_component_cap_limits_full_period_value():
    structure = fixed("40000.00", extra=[{"component": "HRA", "method": "PERCENT_OF",
                                          "params": {"percent": "50", "bases": ["BASIC"]}}])
    data = snapshot(segments=[seg(structure)], components=catalogue(component(
        "HRA", category="FIXED_ALLOWANCE", tags=["LOP_APPLICABLE"], proration="BY_PAID_DAYS", cap="15000")))
    result = calculate(data)
    assert amounts(result)["HRA"] == D("15000.00")
    assert result.line("HRA").trace["notes"] == ["capped at 15,000.00"]


def test_employment_days_proration_ignores_lop():
    structure = fixed("30000.00", extra=[{"component": "PHONE", "method": "FIXED", "amount": "900"}])
    data = snapshot(segments=[seg(structure)], days=days(per_day=absent(nov(3), nov(4), nov(5))),
                    components=catalogue(component("PHONE", category="FIXED_ALLOWANCE", proration="BY_EMPLOYMENT_DAYS")))
    got = amounts(calculate(data))
    assert got["PHONE"] == D("900.00") and got["BASIC"] == D("27000.00")


# --------------------------------------------------------------------------- time-based pay


def test_overtime_day_grain_switch_ec_ot_01():
    """Overtime ON 1-15 Nov only: the entry of the 5th is paid, the one of the 20th is recorded but unpaid."""
    data = snapshot(
        settings=settings(payroll__overtime__enabled=True),
        switch_ranges={"payroll.overtime.enabled": [["2026-11-01", "2026-11-15"]]},
        policies={"OVERTIME_POLICY": overtime_policy()},
        overtime_entries=[{"entry_id": "a", "date": "2026-11-05", "minutes": 120},
                          {"entry_id": "b", "date": "2026-11-20", "minutes": 120}])
    result = calculate(data)
    ot = result.line("OT_WORKDAY")
    assert ot.units == D("2.0000")
    assert [s["id"] for s in ot.sources] == ["a"]


def test_overtime_without_policy_is_blocking():
    data = snapshot(settings=settings(payroll__overtime__enabled=True),
                    overtime_entries=[{"entry_id": "a", "date": "2026-11-05", "minutes": 120}])
    assert issue(calculate(data), "POLICY_MISSING").severity == "BLOCKING"


def test_overtime_policy_without_multipliers_never_assumes_one():
    policy = overtime_policy()
    policy["fields"]["buckets"] = [{"day_type": t, "after_hours": "0"} for t in ("WORKDAY", "WEEKLY_OFF", "HOLIDAY")]
    data = snapshot(settings=settings(payroll__overtime__enabled=True), policies={"OVERTIME_POLICY": policy},
                    overtime_entries=[{"entry_id": "a", "date": "2026-11-05", "minutes": 120}])
    result = calculate(data)
    assert issue(result, "CONFIG_ERROR").severity == "BLOCKING"
    assert "OT_WORKDAY" not in amounts(result)


def test_overtime_switch_off_pays_nothing():
    data = snapshot(policies={"OVERTIME_POLICY": overtime_policy()},
                    overtime_entries=[{"entry_id": "a", "date": "2026-11-05", "minutes": 120}])
    assert "OT_WORKDAY" not in amounts(calculate(data))


def test_structure_line_replaces_engine_overtime_line():
    structure = example_a_structure()
    structure["lines"].append({"component": "OT_WORKDAY", "method": "RATE_X_UNITS",
                               "params": {"rate": "ot.hourly_rate * 2", "units": "ot.hours_workday"}})
    data = snapshot(segments=[seg(structure)], settings=settings(payroll__overtime__enabled=True),
                    policies={"OVERTIME_POLICY": overtime_policy()},
                    overtime_entries=[{"entry_id": "a", "date": "2026-11-05", "minutes": 240}])
    lines = [line for line in calculate(data).lines if line.component == "OT_WORKDAY"]
    assert len(lines) == 1 and lines[0].amount == D("1923.08")  # 240.3846… × 2 × 4


# --------------------------------------------------------------------------- inputs, arrears, reimbursements


def test_wage_input_errors_are_blocking():
    data = snapshot(wage_inputs=[{"id": 1, "component": "NOPE", "amount": "5"},
                                 {"id": 2, "component": "BONUS", "amount": "5", "currency": "USD"},
                                 {"id": 3, "component": "BONUS", "units": "2"}])
    result = calculate(data)
    assert {"UNKNOWN_COMPONENT", "CURRENCY_MISMATCH", "INPUT_NEEDS_RATE"} <= {i.code for i in result.blocking}
    assert "BONUS" not in amounts(result)


def test_wage_inputs_switched_off_are_not_paid():
    data = snapshot(settings=settings(payroll__wage_inputs__enabled=False),
                    wage_inputs=[{"id": 1, "component": "BONUS", "amount": "5000"}])
    result = calculate(data)
    assert "BONUS" not in amounts(result) and issue(result, "WAGE_INPUTS_DISABLED").severity == "WARNING"


def test_duplicate_input_amounts_and_bonus_without_input_flags():
    structure = example_a_structure()
    structure["lines"].append({"component": "BONUS", "method": "FIXED", "amount": "1000"})
    data = snapshot(segments=[seg(structure)],
                    wage_inputs=[{"id": 1, "component": "OTHER_DEDUCTION", "amount": "100"},
                                 {"id": 2, "component": "OTHER_DEDUCTION", "amount": "100"}])
    result = calculate(data)
    assert issue(result, "BONUS_WITHOUT_INPUT").severity == "FLAG"
    assert issue(result, "DUPLICATE_INPUT_AMOUNT").details["inputs"] == [1, 2]


def test_arrears_inherit_tags_and_period_ref():
    data = snapshot(arrears=[{"id": 7, "component": "BASIC", "amount": "1500.00", "period_ref": "2026-09-01..2026-09-30",
                              "tags": ["IN_PF_WAGE", "LOP_APPLICABLE"]},
                             {"id": 8, "component": "BASIC", "amount": "-200.00", "period_ref": "2026-08-01..2026-08-31"}])
    result = calculate(data)
    line = result.line("ARREARS")
    assert line.amount == D("1500.00") and line.period_ref == "2026-09-01..2026-09-30"
    assert line.tags == ("IN_PF_WAGE", "LOP_APPLICABLE") and line.trace["original_component"] == "BASIC"
    assert line.explanation.startswith("Arrears of BASIC for 1–30 Sep 2026")
    ctx = build_context(Snapshot.from_dict(data))
    assert [a.id for a in ctx.arrears_recoveries] == [8]


def test_reimbursement_adds_to_net_not_gross():
    data = snapshot(reimbursements=[{"id": 1, "claim_type": "TRAVEL", "amount": "1200.00", "taxability": "EXEMPT"},
                                    {"id": 2, "claim_type": "TRAVEL", "amount": "300.00"}])
    result = calculate(data)
    line = result.line("REIMBURSEMENT")
    assert line.kind == "REIMBURSEMENT" and line.amount == D("1500.00")
    assert result.totals.gross == D("97000.00") and result.totals.net == D("98500.00")


def test_reimbursements_separate_payment_route_produce_no_line():
    data = snapshot(settings=settings(payroll__reimbursements__payout_route="SEPARATE_PAYMENT"),
                    reimbursements=[{"id": 1, "claim_type": "TRAVEL", "amount": "1200.00"}])
    assert "REIMBURSEMENT" not in amounts(calculate(data))


def test_benefit_in_kind_and_employer_contributions_never_change_net(monkeypatch):
    use_stages(monkeypatch, statutory=pack_stage(epf="3000.00", pt="0.00"))
    data = snapshot(components=catalogue(component("CAR", kind="BENEFIT_IN_KIND", category="BENEFIT")),
                    wage_inputs=[{"id": 1, "component": "CAR", "amount": "2500.00"}])
    result = calculate(data)
    assert result.totals.benefits_in_kind == D("2500.00")
    assert result.totals.net == D("97000.00") - D("3000.00")
    assert result.totals.employer_cost == D("100000.00")


# --------------------------------------------------------------------------- net rounding and payments


def test_net_rounding_line_ec_mon_11():
    data = snapshot(days=days(per_day=absent(nov(12), nov(13))),
                    settings=settings(payroll__rounding__net_pay_increment="1"))
    result = calculate(data)
    line = result.line("NET_ROUNDING")
    assert result.totals.net_before_rounding == D("90533.34")
    assert line.amount == D("-0.34") and result.totals.net == D("90533.00")
    assert line.kind == "ROUNDING" and line.trace["rounding"]["stage"] == "NET"


def test_net_rounding_carry_forward_ec_mon_11():
    data = snapshot(days=days(per_day=absent(nov(12), nov(13))),
                    settings=settings(payroll__rounding__net_pay_increment="1",
                                      payroll__rounding__difference_handling="CARRY_FORWARD"))
    result = calculate(data)
    assert result.totals.net == D("90533.00") and result.totals.rounding_carried == D("0.34")
    assert result.line("NET_ROUNDING").trace["carry_forward"] == {"amount": "0.34", "currency": "INR"}


def test_carried_rounding_comes_back_as_rounding_line():
    data = snapshot(components=catalogue(component("ROUNDING_CARRY", kind="ROUNDING", category="ROUNDING")),
                    wage_inputs=[{"id": 4, "component": "ROUNDING_CARRY", "amount": "0.34"}])
    result = calculate(data)
    assert result.totals.net == D("97000.34") and result.totals.net_rounding == D("0.34")


def test_payment_split_percent_and_remainder():
    data = snapshot(settings=settings(payroll__bank__split_payments_enabled=True),
                    bank_accounts=[{"account_id": 1, "type": "PERCENT", "value": "30", "order": 1},
                                   {"account_id": 2, "type": "REMAINDER", "order": 2, "verified": False}])
    result = calculate(data)
    assert [(p.account_id, p.amount, p.on_hold) for p in result.payments] == [(1, D("29100.00"), False),
                                                                             (2, D("67900.00"), True)]
    assert issue(result, "UNVERIFIED_ACCOUNT_ON_HOLD").severity == "WARNING"
    assert sum(p.amount for p in result.payments) == result.totals.net


def test_payment_split_rules_invalid_when_split_off():
    data = snapshot(bank_accounts=[{"account_id": 1, "type": "PERCENT", "value": "30", "order": 1},
                                   {"account_id": 2, "type": "REMAINDER", "order": 2}])
    result = calculate(data)
    assert issue(result, "PAYMENT_SPLIT_INVALID").severity == "BLOCKING" and not result.payments


def test_no_bank_account_warns():
    assert issue(calculate(snapshot(bank_accounts=[])), "NO_BANK_ACCOUNT").severity == "WARNING"


# --------------------------------------------------------------------------- validation (3.5 §6, §10)


def test_invariant_violation_is_blocking_and_not_waivable_ec_sta_10():
    ctx = build_context(Snapshot.from_dict(snapshot()))
    lines = [Line("BASIC", "EARNING", "BASE", 4, D("100.00"), "INR"), stat_line("IN_EPF_EE", "DEDUCTION", "10.00")]
    figures = totals(lines, "INR")
    assert invariants(ctx, lines, figures, [PaymentLine(1, "PRIMARY", 0, D("90.00"), "INR", "INR", False, False, "")]) == []
    tampered = figures.__class__(**{**figures.__dict__, "net": D("95.00")})
    found = invariants(ctx, lines, tampered, [])
    assert {i.details["rule"] for i in found} >= {"NET", "TOTALS_ARE_SUMS"}
    assert all(i.severity == "BLOCKING" and not i.waivable for i in found)
    odd = [*lines, Line("X", "EARNING", "BASE", 4, D("1.005"), "USD")]
    rules = {i.details["rule"] for i in invariants(ctx, odd, totals(odd, "INR"), [])}
    assert {"SINGLE_CURRENCY", "ROUNDED_LINES"} <= rules
    short = invariants(ctx, lines, figures, [PaymentLine(1, "PRIMARY", 0, D("80.00"), "INR", "INR", False, False, "")])
    assert short[0].details["rule"] == "PAYMENTS_EQUAL_NET"


def test_variance_against_previous_period_must_be_acknowledged():
    data = snapshot(previous={"period_ref": "2026-10-01..2026-10-31", "currency": "INR", "net": "60000.00",
                              "bank_account_ids": [9001]})
    found = issue(calculate(data), "NET_VARIANCE")
    assert found.severity == "WARNING" and found.waivable
    calm = snapshot(previous={"period_ref": "2026-10", "currency": "INR", "net": "96000.00", "bank_account_ids": [9001]})
    assert issue(calculate(calm), "NET_VARIANCE") is None


def test_outlier_new_account_and_payment_currency_flags():
    data = snapshot(
        wage_inputs=[{"id": 1, "component": "BONUS", "amount": "10000.00"}],
        previous={"period_ref": "2026-10", "currency": "INR", "net": "96000.00", "payment_currency": "USD",
                  "bank_account_ids": [1], "history": {"BONUS": ["1000.00", "1200.00", "900.00", "1000.00"]}})
    result = calculate(data)
    assert issue(result, "COMPONENT_OUTLIER").component == "BONUS"
    assert issue(result, "NEW_BANK_ACCOUNT").details["account_id"] == 9001
    assert issue(result, "PAYMENT_CURRENCY_CHANGED").severity == "FLAG"


def test_leaver_flag():
    data = snapshot(employee={"employee_id": 1, "hire_date": "2020-01-01", "country": "IN", "exit_date": "2026-11-20"},
                    days=days(employed_to=nov(20)))
    result = calculate(data)
    assert issue(result, "LEAVER").severity == "FLAG"
    assert amounts(result)["BASIC"] == D("33333.33")  # 50,000 × 20 ÷ 30


# --------------------------------------------------------------------------- stages 5-8


def test_missing_statutory_stage_blocks(monkeypatch):
    monkeypatch.setattr(pipeline, "_stage", lambda name: None)
    result = calculate(snapshot())
    assert issue(result, "STATUTORY_STAGE_MISSING").severity == "BLOCKING"
    assert issue(result, "DEDUCTIONS_STAGE_MISSING") is None  # nothing to deduct
    result = calculate(snapshot(loans=[{"loan_id": 1, "instalment": "10"}]))
    assert issue(result, "DEDUCTIONS_STAGE_MISSING").severity == "BLOCKING"


def test_stage_returning_only_new_lines_is_merged(monkeypatch):
    def only_new(ctx, lines):
        return [stat_line("IN_PT", "DEDUCTION", "200.00")], []

    use_stages(monkeypatch, statutory=only_new)
    result = calculate(snapshot())
    assert amounts(result)["IN_PT"] == D("200.00") and amounts(result)["BASIC"] == D("50000.00")


def test_penalty_lines_reach_the_deductions_stage(monkeypatch):
    use_stages(monkeypatch, statutory=pack_stage(epf="0.00", pt="0.00"), deductions=loan_stage)
    marks = {nov(d): {"attendance": {"status": "PRESENT", "worked_seconds": 28800, "late": True}} for d in range(2, 8)}
    every = {nov(d): {"attendance": {"status": "PRESENT", "worked_seconds": 28800}}
             for d in range(1, 31) if nov(d).weekday() != 6}
    every.update(marks)
    data = snapshot(days=days(per_day=every),
                    settings=settings(payroll__attendance__enabled=True, payroll__late_penalty__enabled=True),
                    policies={"LATE_PENALTY_POLICY": {"fields": {
                        "penalty_type": "FIXED_AMOUNT", "amount_per_late": {"amount": "100.00", "currency": "INR"}}}})
    result = calculate(data)
    penalty = result.line("LATE_PENALTY")
    assert penalty.amount == D("400.00") and penalty.kind == "DEDUCTION" and penalty.stage == 8  # (6 − 2) × 100


# --------------------------------------------------------------------------- determinism and replay (EC-STA-04)


def test_replay_reproduces_calc_hash_ec_sta_04():
    data = snapshot(days=days(per_day=absent(nov(12))))
    first = calculate(data)
    ok, again = replay(data, first.calc_hash)
    assert ok and again.calc_hash == first.calc_hash and first.engine_version == ENGINE_VERSION
    ok, _ = replay(Snapshot.from_dict(data).to_dict(), first.calc_hash)
    assert ok
    changed = snapshot(days=days(per_day=absent(nov(13))))
    assert replay(changed, first.calc_hash)[0] is False


def test_calc_hash_independent_of_json_key_order():
    data = snapshot(wage_inputs=[{"id": 1, "component": "BONUS", "amount": "10.00"}])
    shuffled = {key: data[key] for key in reversed(list(data))}
    shuffled["settings"] = dict(reversed(list(data["settings"].items())))
    assert calculate(data).calc_hash == calculate(shuffled).calc_hash


def test_calc_hash_changes_with_engine_version(monkeypatch):
    data = snapshot()
    before = calculate(data).calc_hash
    monkeypatch.setattr(pipeline, "ENGINE_VERSION", "9.9.9")
    assert calculate(data).calc_hash != before


def test_result_json_is_plain():
    result = calculate(snapshot())
    payload = result.to_json()
    assert payload["totals"]["net"] == "97000.00" and payload["lines"][0]["amount"] == "50000.00"
    assert payload["calc_hash"] == result.calc_hash


def test_input_structure_line_without_an_input_produces_no_line():
    structure = example_a_structure()
    structure["lines"].append({"component": "BONUS", "method": "INPUT"})
    result = calculate(snapshot(segments=[seg(structure)]))
    assert "BONUS" not in amounts(result)
    paid = calculate(snapshot(segments=[seg(structure)], wage_inputs=[{"id": 9, "component": "BONUS", "amount": "750"}]))
    assert amounts(paid)["BONUS"] == D("750.00") and paid.line("BONUS").sources[-1] == {"type": "wage_input", "id": 9}


def test_rate_x_units_cap_limits_the_amount_not_the_rate():
    structure = fixed("1000.00", extra=[{"component": "CONVEYANCE", "method": "RATE_X_UNITS",
                                         "params": {"rate": "100", "units": "att.paid_days"}}])
    data = snapshot(segments=[seg(structure)],
                    components=catalogue(component("CONVEYANCE", category="FIXED_ALLOWANCE", cap="2000")))
    line = calculate(data).line("CONVEYANCE")
    assert line.amount == D("2000.00") and line.rate == D("100.00") and line.units == D("30")
    assert line.explanation == "100.00 × 30 days (capped at 2,000.00)"


def test_cycle_fails_only_its_members():
    structure = example_a_structure()
    structure["lines"][1] = {"component": "HRA", "method": "FORMULA", "params": {"formula": "FIXED_ALLOWANCE * 10%"}}
    result = calculate(snapshot(segments=[seg(structure)]))
    assert amounts(result)["BASIC"] == D("50000.00")
    assert not {"HRA", "FIXED_ALLOWANCE"} & set(amounts(result)) and not result.ok


def test_holiday_on_weekly_off_extra_day_pay_ec_hol_01():
    """EXTRA_DAY_PAY: a paid holiday on Sunday 8 Nov earns Σ LOP-applicable rates ÷ N = 97,000 ÷ 30 = 3,233.33."""
    data = snapshot(days=days(per_day={nov(8): {"holiday": {"holiday_id": 5, "type": "PUBLIC"}}}),
                    settings=settings(payroll__holiday_work_premium__enabled=True),
                    policies={"HOLIDAY_PAY_POLICY": {"fields": {"holiday_on_weekly_off": "EXTRA_DAY_PAY"}}})
    result = calculate(data)
    line = result.line("HOLIDAY_PREMIUM")
    assert line.amount == D("3233.33") and line.units == D("1")
    assert amounts(result)["BASIC"] == D("50000.00")


def net_based(target: str) -> dict:
    structure = example_a_structure()
    structure.update(structure_type="NET_BASED", pay_basis_amount=target, ctc_employer_contributions={})
    return structure


def test_net_based_structure_is_solved_to_a_gross_ec_cmp_15(monkeypatch):
    """Target net 93,800.00 with EPF 3,000 and PT 200 → monthly gross 97,000.00; LOP then applies as usual."""
    use_stages(monkeypatch, statutory=pack_stage())
    data = snapshot(segments=[seg(net_based("93800.00"))], settings=settings(payroll__structures__net_based_enabled=True))
    result = calculate(data)
    got = amounts(result)
    assert (got["BASIC"], got["HRA"], got["FIXED_ALLOWANCE"]) == (D("48500.00"), D("19400.00"), D("29100.00"))
    assert result.totals.net == D("93800.00")
    trace = result.stage_outputs["gross_up"]["S1"]
    assert trace["converged"] and len(trace["iterations"]) <= 50
    assert D(trace["gross"]) == D("97000.00")  # the smallest gross whose net is within one minor unit
    lop = calculate(snapshot(segments=[seg(net_based("93800.00"))], days=days(per_day=absent(nov(12), nov(13))),
                             settings=settings(payroll__structures__net_based_enabled=True)))
    assert amounts(lop)["BASIC"] == D("45266.67")  # 48,500 × 28 ÷ 30
    assert lop.content_hash == calculate(snapshot(segments=[seg(net_based("93800.00"))], days=days(per_day=absent(
        nov(12), nov(13))), settings=settings(payroll__structures__net_based_enabled=True))).content_hash


def test_net_based_requires_its_switch():
    result = calculate(snapshot(segments=[seg(net_based("93800.00"))]))
    assert issue(result, "NET_BASED_DISABLED").severity == "BLOCKING" and not result.lines
