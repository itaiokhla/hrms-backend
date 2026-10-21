"""Engine stage 8 (``engine.deductions``): priority, protected net, deferral and carry-forward (3.5 §6, G-23)."""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.payroll import packs
from app.payroll.engine import deductions
from app.payroll.engine.deductions import DeductionItem
from app.payroll.engine.lines import STAGE_EARNINGS, STAGE_POST_TAX, STAGE_STATUTORY, Line
from app.payroll.packs import zz_test

D = Decimal
ON = {"payroll.loans.enabled": True, "payroll.advances.enabled": True, "payroll.arrears.recovery_mode": "NEXT_RUN"}


@dataclass
class Ctx:
    currency: str = "INR"
    settings: dict = field(default_factory=lambda: {**ON, "payroll.deductions.min_net_pay_percent": "80"})
    court_orders: tuple = ()
    loans: tuple = ()
    advances: tuple = ()
    deduction_inputs: tuple = ()
    arrears_recoveries: tuple = ()
    penalty_lines: tuple = ()
    deductions: tuple = ()
    final_settlement: bool = False
    pack: Optional[dict] = None
    environment: str = "test"


def gross_and_statutory(gross="50000.00", statutory="5000.00", reimbursement=None):
    lines = [Line("BASIC", "EARNING", "BASE", STAGE_EARNINGS, D(gross), "INR"),
             Line("IN_EPF_EE", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", STAGE_STATUTORY, D(statutory), "INR",
                  statutory=True)]
    if reimbursement:
        lines.append(Line("REIMBURSEMENT", "REIMBURSEMENT", "REIMBURSEMENT", STAGE_EARNINGS, D(reimbursement), "INR"))
    return lines


def added(result):
    return {line.component: line.amount for line in result.added}


def carried(result):
    return {(c.type, c.item_id): (c.recovered, c.carried_forward, c.reason, c.receivable) for c in result.carry_forwards}


def codes(issues):
    return [i.code for i in issues]


# --------------------------------------------------------------------------- protected net and priority


def test_ec_var_05_loan_above_the_protected_net_is_partly_recovered():
    # gross 50,000 − statutory 5,000 = 45,000; floor 80% × 50,000 = 40,000; room 5,000
    ctx = Ctx(loans=({"loan_id": "L1", "amount": "8000.00", "instalment_number": 3},))
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert added(result) == {"LOAN_RECOVERY": D("5000.00")}
    assert carried(result) == {("LOAN", "L1"): (D("5000.00"), D("3000.00"), "PROTECTED_NET", False)}
    (issue,) = result.issues
    assert (issue.code, issue.severity, issue.details["carried_forward"]) == ("DEDUCTION_DEFERRED", "WARNING", "3000.00")
    line = result.added[0]
    assert line.stage == STAGE_POST_TAX and line.category == "LOAN_REPAYMENT" and line.trace["room_before"] == "5000.00"
    assert "carried forward" in line.explanation and line.sources[0]["instalment_number"] == "3"
    assert result.protected_net.floor == D("40000.00")


def test_ec_var_11_court_orders_by_legal_priority_then_voluntary_deferred():
    ctx = Ctx(court_orders=({"id": "CO-B", "amount": "3000.00", "priority": 2},
                            {"id": "CO-A", "amount": "4000.00", "priority": 1}),
              deductions=(DeductionItem("V1", "VOLUNTARY", D("2000.00"), "OTHER_DEDUCTION"),),
              settings={**ON, "payroll.deductions.min_net_pay_percent": "78"})
    # floor 39,000; room 6,000: CO-A 4,000, CO-B 2,000 of 3,000, voluntary nothing
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert [(l.trace["type"], l.amount) for l in result.added] == [("COURT_ORDER", D("4000.00")),
                                                                    ("COURT_ORDER", D("2000.00"))]
    assert [l.sources[0]["id"] for l in result.added] == ["CO-A", "CO-B"]
    assert carried(result) == {("COURT_ORDER", "CO-B"): (D("2000.00"), D("1000.00"), "PROTECTED_NET", False),
                               ("VOLUNTARY", "V1"): (D("0"), D("2000.00"), "PROTECTED_NET", False)}


def test_priority_order_loans_before_voluntary_before_penalties():
    penalty = Line("LATE_PENALTY", "DEDUCTION", "PENALTY_IF_LEGAL", STAGE_POST_TAX, D("500.00"), "INR",
                   sources=({"type": "late_marks", "id": "P1"},), explanation="5 late marks × 100.00")
    ctx = Ctx(penalty_lines=(penalty,), loans=({"loan_id": "L1", "amount": "2000.00"},),
              deductions=(DeductionItem("V1", "VOLUNTARY", D("2500.00"), "OTHER_DEDUCTION"),))
    result = deductions.evaluate(ctx, gross_and_statutory())   # room 5,000
    assert added(result) == {"LOAN_RECOVERY": D("2000.00"), "OTHER_DEDUCTION": D("2500.00"), "LATE_PENALTY": D("500.00")}
    tight = deductions.evaluate(Ctx(penalty_lines=(penalty,), loans=({"loan_id": "L1", "amount": "2000.00"},),
                                    deductions=(DeductionItem("V1", "VOLUNTARY", D("2800.00"), "OTHER_DEDUCTION"),)),
                                gross_and_statutory())
    # reverse-priority deferral: the penalty (lowest) gives way first
    assert added(tight) == {"LOAN_RECOVERY": D("2000.00"), "OTHER_DEDUCTION": D("2800.00"), "LATE_PENALTY": D("200.00")}
    penalty_line = next(l for l in tight.added if l.component == "LATE_PENALTY")
    assert penalty_line.sources == penalty.sources and penalty_line.trace["proposed"] == penalty.trace
    assert penalty_line.stage == STAGE_POST_TAX


def test_g23_unset_percentage_blocks_automated_recoveries():
    penalty = Line("LATE_PENALTY", "DEDUCTION", "PENALTY_IF_LEGAL", STAGE_POST_TAX, D("300.00"), "INR")
    arrears = SimpleNamespace(id=9, component="BASIC", amount=D("-1500.00"), period_ref="2026-09-01..2026-09-30")
    ctx = Ctx(settings=dict(ON), loans=({"loan_id": "L1", "amount": "2000.00"},), penalty_lines=(penalty,),
              arrears_recoveries=(arrears,), court_orders=({"id": "CO", "amount": "1000.00"},),
              deductions=(DeductionItem("V1", "VOLUNTARY", D("700.00"), "OTHER_DEDUCTION"),))
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert added(result) == {"COURT_ORDER": D("1000.00"), "OTHER_DEDUCTION": D("700.00")}
    blocked = {k: v[2] for k, v in carried(result).items()}
    assert blocked == {("LOAN", "L1"): "PROTECTED_NET_NOT_SET", ("PENALTY", "LATE_PENALTY"): "PROTECTED_NET_NOT_SET",
                       ("RECOVERY", "9"): "PROTECTED_NET_NOT_SET"}
    (issue,) = [i for i in result.issues if i.code == "PROTECTED_NET_NOT_SET"]
    assert issue.severity == "BLOCKING" and issue.waivable and len(issue.details["items"]) == 3


def test_g23_non_automated_items_never_take_net_below_zero_without_a_floor():
    ctx = Ctx(settings=dict(ON), court_orders=({"id": "CO", "amount": "50000.00"},))
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert added(result) == {"COURT_ORDER": D("45000.00")}
    assert carried(result)[("COURT_ORDER", "CO")][1] == D("5000.00")


def test_pack_floor_is_a_minimum_for_the_company_percentage():
    zz = packs.pack_snapshot(zz_test.PACK)
    ctx = Ctx(currency="EUR", pack=zz, settings={**ON, "payroll.deductions.min_net_pay_percent": "40"},
              loans=({"loan_id": "L1", "amount": "3000.00"},))
    lines = [Line("BASIC", "EARNING", "BASE", STAGE_EARNINGS, D("4000.00"), "EUR")]
    result = deductions.evaluate(ctx, lines)
    # ZZ floor 50% beats the company 40%: floor 2,000, room 2,000
    assert result.protected_net.percent == D("50") and added(result) == {"LOAN_RECOVERY": D("2000.00")}
    production = deductions.evaluate(Ctx(currency="EUR", pack=zz, environment="production",
                                         settings={**ON, "payroll.deductions.min_net_pay_percent": "40"},
                                         loans=({"loan_id": "L1", "amount": "3000.00"},)), lines)
    assert production.protected_net.percent == D("40")      # a pack that may not run here sets no floor
    # setting unset: automated recoveries stay blocked even though the pack has a floor (G-23)
    unset = deductions.evaluate(Ctx(currency="EUR", pack=zz, settings=dict(ON),
                                    loans=({"loan_id": "L1", "amount": "3000.00"},)), lines)
    assert not unset.added and "PROTECTED_NET_NOT_SET" in codes(unset.issues)


def test_ec_run_10_negative_net_is_never_clipped_and_everything_is_deferred():
    lines = gross_and_statutory(gross="3000.00", statutory="4000.00")
    ctx = Ctx(loans=({"loan_id": "L1", "amount": "1000.00"},), court_orders=({"id": "CO", "amount": "500.00"},))
    result = deductions.evaluate(ctx, lines)
    assert not result.added and result.net_before == D("-1000.00")
    assert list(result.lines) == lines
    assert {k[1] for k in carried(result)} == {"L1", "CO"}


def test_reimbursements_count_in_net_but_not_in_the_floor():
    ctx = Ctx(loans=({"loan_id": "L1", "amount": "8000.00"},))
    result = deductions.evaluate(ctx, gross_and_statutory(reimbursement="1000.00"))
    assert added(result) == {"LOAN_RECOVERY": D("6000.00")}   # net 46,000 − floor 40,000


# --------------------------------------------------------------------------- items


def test_ec_var_07_final_settlement_remainder_becomes_a_receivable():
    ctx = Ctx(final_settlement=True, loans=({"loan_id": "L1", "amount": "20000.00"},))
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert added(result) == {"LOAN_RECOVERY": D("5000.00")}
    assert carried(result)[("LOAN", "L1")] == (D("5000.00"), D("15000.00"), "PROTECTED_NET", True)
    assert codes(result.issues) == ["DEDUCTION_RECEIVABLE"]


def test_ec_var_06_paused_loan_is_skipped():
    ctx = Ctx(loans=({"loan_id": "L1", "amount": "1000.00", "status": "PAUSED"},
                     {"loan_id": "L2", "amount": "1000.00"}))
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert added(result) == {"LOAN_RECOVERY": D("1000.00")} and result.added[0].sources[0]["id"] == "L2"
    assert result.skipped == ({"id": "L1", "type": "LOAN", "reason": "PAUSED"},)
    final = deductions.evaluate(Ctx(final_settlement=True, loans=({"loan_id": "L1", "amount": "1000.00",
                                                                   "status": "PAUSED"},)), gross_and_statutory())
    assert added(final) == {"LOAN_RECOVERY": D("1000.00")}     # a paused loan is still settled on exit


def test_loans_switched_off_are_not_recovered():
    ctx = Ctx(settings={"payroll.deductions.min_net_pay_percent": "50"},
              loans=({"loan_id": "L1", "amount": "1000.00"},), advances=({"id": "A1", "amount": "500.00"},))
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert not result.added and [i.code for i in result.issues] == ["RECOVERY_SWITCHED_OFF"] * 2


def test_negative_arrears_follow_the_recovery_mode():
    arrears = SimpleNamespace(id=9, component="BASIC", amount=D("-1500.00"), period_ref="2026-09-01..2026-09-30")
    result = deductions.evaluate(Ctx(arrears_recoveries=(arrears,)), gross_and_statutory())
    line = result.added[0]
    assert (line.component, line.category, line.amount, line.period_ref) == (
        "ARREARS_RECOVERY", "RECOVERY", D("1500.00"), "2026-09-01..2026-09-30")
    manual = deductions.evaluate(Ctx(arrears_recoveries=(arrears,),
                                     settings={**ON, "payroll.arrears.recovery_mode": "MANUAL",
                                               "payroll.deductions.min_net_pay_percent": "50"}), gross_and_statutory())
    assert not manual.added and codes(manual.issues) == ["ARREARS_RECOVERY_MANUAL"]


def test_deduction_inputs_take_their_type_from_the_component():
    component = {"OTHER_DEDUCTION": SimpleNamespace(category="VOLUNTARY"),
                 "COURT_ORDER": SimpleNamespace(category="GARNISHMENT_IF_LEGAL")}

    class Context(Ctx):
        def component(self, code):
            return component.get(code)

    inputs = (SimpleNamespace(id=1, component="OTHER_DEDUCTION", amount=D("3000.00"), currency=None, reason="Union dues"),
              SimpleNamespace(id=2, component="COURT_ORDER", amount=D("3000.00"), currency="INR", reason=None))
    result = deductions.evaluate(Context(deduction_inputs=inputs), gross_and_statutory())
    # room 5,000: the court order (rank 1) first, then 2,000 of the voluntary dues
    assert [(l.component, l.amount) for l in result.added] == [("COURT_ORDER", D("3000.00")),
                                                               ("OTHER_DEDUCTION", D("2000.00"))]
    assert result.added[1].explanation.startswith("Union dues")


def test_all_or_nothing_items_are_deferred_whole():
    ctx = Ctx(deductions=(DeductionItem("INS", "VOLUNTARY", D("6000.00"), "INSURANCE", partial_allowed=False),
                          DeductionItem("V2", "VOLUNTARY", D("1000.00"), "OTHER_DEDUCTION")))
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert added(result) == {"OTHER_DEDUCTION": D("1000.00")}
    assert carried(result)[("VOLUNTARY", "INS")][:2] == (D("0"), D("6000.00"))


@pytest.mark.parametrize("item", [
    {"loan_id": "L1", "amount": 100.5},
    {"loan_id": "L1", "amount": "-5.00"},
    {"loan_id": "L1", "amount": "1.005"},
    {"amount": "100.00"},
    {"loan_id": "L1", "amount": "100.00", "currency": "USD"},
    {"loan_id": "L1", "amount": "100.00", "priority": "first"},
    "not a mapping",
])
def test_invalid_items_are_blocking_and_never_deducted(item):
    result = deductions.evaluate(Ctx(loans=(item,)), gross_and_statutory())
    assert not result.added and [(i.code, i.severity) for i in result.issues] == [("INVALID_DEDUCTION", "BLOCKING")]


def test_invalid_protected_net_setting_fails_closed():
    ctx = Ctx(settings={**ON, "payroll.deductions.min_net_pay_percent": 50.5}, loans=({"loan_id": "L1", "amount": "1.00"},))
    result = deductions.evaluate(ctx, gross_and_statutory())
    assert not result.added and codes(result.issues) == ["INVALID_DEDUCTION"]
    over = deductions.evaluate(Ctx(settings={**ON, "payroll.deductions.min_net_pay_percent": "120"},
                                   loans=({"loan_id": "L1", "amount": "1.00"},)), gross_and_statutory())
    assert not over.added and codes(over.issues) == ["INVALID_DEDUCTION"]


def test_stage_runs_once_and_nothing_due_adds_nothing():
    lines = gross_and_statutory()
    assert deductions.apply(Ctx(), lines) == (lines, [])
    done = [*lines, Line("LOAN_RECOVERY", "DEDUCTION", "LOAN_REPAYMENT", STAGE_POST_TAX, D("1.00"), "INR")]
    result, issues = deductions.apply(Ctx(loans=({"loan_id": "L1", "amount": "1.00"},)), done)
    assert result == done and codes(issues) == ["DEDUCTIONS_ALREADY_APPLIED"]


def test_carry_forward_records_serialize_for_the_services():
    result = deductions.evaluate(Ctx(loans=({"loan_id": "L1", "amount": "8000.00", "period_ref": "x"},)),
                                 gross_and_statutory())
    assert deductions.carry_forwards_json(result, "INR") == [{
        "item_id": "L1", "type": "LOAN", "component": "LOAN_RECOVERY", "due": "8000.00", "recovered": "5000.00",
        "carried_forward": "3000.00", "reason": "PROTECTED_NET", "receivable": False, "period_ref": "x",
        "currency": "INR"}]


# --------------------------------------------------------------------------- golden (3.5 §17) and the real context


def test_worked_example_3_5_17_net_with_zero_tax():
    lines = [Line("BASIC", "EARNING", "BASE", 4, D("46666.67"), "INR"),
             Line("HRA", "EARNING", "FIXED_ALLOWANCE", 4, D("18666.67"), "INR"),
             Line("FIXED_ALLOWANCE", "EARNING", "FIXED_ALLOWANCE", 4, D("25200.00"), "INR"),
             Line("OT_WORKDAY", "EARNING", "OVERTIME", 4, D("1442.31"), "INR"),
             Line("BONUS", "EARNING", "BONUS", 4, D("10000.00"), "INR"),
             Line("IN_EPF_EE", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", 6, D("3000.00"), "INR", statutory=True),
             Line("IN_PT", "DEDUCTION", "TAX", 6, D("200.00"), "INR", statutory=True),
             Line("IN_EPF_ER", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION", 6, D("3000.00"), "INR", statutory=True)]
    ctx = Ctx(settings={**ON, "payroll.deductions.min_net_pay_percent": "50"},
              loans=({"loan_id": 5, "instalment": "2000.00"},))
    result, issues = deductions.apply(ctx, lines)
    assert not issues
    net = sum(l.amount for l in result if l.kind == "EARNING") - sum(l.amount for l in result if l.kind == "DEDUCTION")
    assert net == D("96775.65")


def _snapshot(**overrides):
    start, end = dt.date(2026, 11, 1), dt.date(2026, 11, 30)
    days, day = [], start
    while day <= end:
        days.append({"date": day.isoformat(), "segment": "S1", "employed": True})
        day += dt.timedelta(days=1)
    data = {"run": {"run_id": 1, "run_employee_id": 11, "environment": "test"},
            "employee": {"employee_id": 501, "hire_date": "2022-04-01", "country": "IN"},
            "period": {"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat()},
            "currency": "INR", "settings": {**ON, "payroll.deductions.min_net_pay_percent": "80"},
            "components": [{"code": "OTHER_DEDUCTION", "kind": "DEDUCTION", "category": "VOLUNTARY"}],
            "segments": [{"key": "S1", "start": start.isoformat(), "end": end.isoformat(), "compensation": None}],
            "days": days, "loans": [{"loan_id": "L1", "amount": "4000.00"}],
            "court_orders": [{"id": "CO", "amount": "2000.00", "priority": 1}],
            "wage_inputs": [{"id": 77, "component": "OTHER_DEDUCTION", "amount": "1000.00"}]}
    data.update(overrides)
    return data


def test_engine_calc_context_feeds_the_stage():
    context = pytest.importorskip("app.payroll.engine.context")
    from app.payroll.engine.snapshot import Snapshot

    ctx = context.build_context(Snapshot.from_dict(_snapshot()))
    result = deductions.evaluate(ctx, gross_and_statutory())
    # room 5,000: court order 2,000, loan 3,000 of 4,000, voluntary input deferred
    assert [(l.component, l.amount) for l in result.added] == [("COURT_ORDER", D("2000.00")),
                                                               ("LOAN_RECOVERY", D("3000.00"))]
    assert {k[1]: v[1] for k, v in carried(result).items()} == {"L1": D("1000.00"), "77": D("1000.00")}
    unset = context.build_context(Snapshot.from_dict(_snapshot(settings=dict(ON))))
    assert "PROTECTED_NET_NOT_SET" in codes(deductions.evaluate(unset, gross_and_statutory()).issues)
