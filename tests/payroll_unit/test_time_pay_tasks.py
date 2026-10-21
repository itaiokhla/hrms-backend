"""Task-based pay: rates, attribution, splits, caps, the minimum-wage floor and clawback (3.4 §10, 3.1 §5.6)."""

from datetime import date
from decimal import Decimal as D

import pytest

from app.payroll.time_pay import (
    EmployeeAttributes,
    MinimumWageInput,
    RateCardRow,
    TaskAssignee,
    TaskRecord,
    TimePayError,
    minimum_wage_check,
    split_amount,
    task_clawback,
    task_pay,
    validate_split,
)

START, END = date(2026, 10, 1), date(2026, 10, 31)
CARD = (RateCardRow("SITE_INSTALL", D("800.00"), "INR", date(2026, 1, 1)),)
ME = "emp-1"


def task(task_id="t1", *, quantity="25", status="APPROVED", approved_on=date(2026, 10, 10), assignees=None, **kwargs):
    return TaskRecord(task_id, kwargs.pop("task_type", "SITE_INSTALL"), status, D(quantity),
                      assignees or (TaskAssignee(ME),), approved_on=approved_on, **kwargs)


def pay(tasks, policy=None, **kwargs):
    kwargs.setdefault("rate_card", CARD)
    return task_pay(tasks, policy or {}, employee_id=ME, period_start=START, period_end=END, currency="INR", **kwargs)


# --------------------------------------------------------------------------- golden 3.4 §17.3


def test_golden_3_4_17_3_cap_carry_and_minimum_wage_top_up():
    """3.4 §17.3: 25 × 800 = 20,000 → paid 15,000.00, carried 5,000.00, MIN_WAGE_TOP_UP 1,000.00."""
    policy = {"cap_per_period": {"amount": "15000.00", "currency": "INR"}, "cap_action": "TRUNCATE_AND_CARRY",
              "minimum_wage_floor": "TOP_UP"}
    result = pay([task()], policy, minimum_wage=MinimumWageInput(D("160"), D("100.00"), verified=True))
    assert result.earned == D("20000.00")
    assert (result.paid, result.carried_out, result.forfeited) == (D("15000.00"), D("5000.00"), D("0"))
    assert [(line.component, line.to_dict()["amount"]) for line in result.lines] == [
        ("TASK_EARNING", "15000.00"), ("MIN_WAGE_TOP_UP", "1000.00")]
    assert result.minimum_wage is not None and result.minimum_wage.entitlement == D("16000.00")
    assert result.issues[0].code == "TASK_CAP_CARRIED"


def test_ec_tsk_06_carried_amount_paid_first_next_period():
    policy = {"cap_per_period": "15000", "cap_action": "TRUNCATE_AND_CARRY"}
    result = pay([task(quantity="15")], policy, carried_in=D("5000.00"))
    assert (result.paid, result.carried_out) == (D("15000.00"), D("2000.00"))
    assert result.lines[0].sources[0] == {"type": "task_carry_forward", "amount": "5000.00"}
    assert result.items[0].paid_now == D("10000.00") and result.items[0].carried == D("2000.00")


def test_ec_tsk_06_exception_and_truncate_actions():
    exception = pay([task()], {"cap_per_period": "15000"})
    assert exception.paid == D("20000.00") and exception.issues[0].code == "TASK_CAP_EXCEEDED"
    assert exception.issues[0].severity == "BLOCKING"
    truncate = pay([task()], {"cap_per_period": "15000", "cap_action": "TRUNCATE"})
    assert (truncate.paid, truncate.carried_out, truncate.forfeited) == (D("15000.00"), D("0"), D("5000.00"))


def test_ec_tsk_06_boundary_exactly_at_cap():
    result = pay([task(quantity="18.75")], {"cap_per_period": "15000", "cap_action": "TRUNCATE_AND_CARRY",
                                            "minimum_wage_floor": "OFF"})
    assert result.paid == D("15000.00") and result.carried_out == 0 and result.issues == ()


# --------------------------------------------------------------------------- minimum wage (EC-TSK-07, EC-TSK-13)


def test_ec_tsk_07_without_verified_pack_data_warns_not_verified():
    result = pay([task(quantity="10")], {"minimum_wage_floor": "TOP_UP"},
                 minimum_wage=MinimumWageInput(D("160"), D("100"), verified=False))
    assert [line.component for line in result.lines] == ["TASK_EARNING"]
    assert result.minimum_wage is not None and result.minimum_wage.mode == "WARN"
    assert result.issues[-1].message.startswith("Minimum wage not verified for this jurisdiction")
    no_rate = minimum_wage_check(D("100"), D("10"), mode="TOP_UP", currency="INR", min_hourly_rate=None, verified=True)
    assert no_rate.issues[0].code == "MIN_WAGE_NOT_VERIFIED" and no_rate.line is None


def test_ec_tsk_07_warn_mode_shortfall_and_pack_forced_top_up():
    warn = minimum_wage_check(D("15000"), D("160"), mode="WARN", currency="INR", min_hourly_rate=D("100"),
                              verified=True)
    assert warn.line is None and warn.issues[0].message == "Below minimum wage by 1,000"
    forced = minimum_wage_check(D("15000"), D("160"), mode="WARN", currency="INR", min_hourly_rate=D("100"),
                                verified=True, pack_forces_top_up=True)
    assert forced.line is not None and forced.line.amount == D("1000.00")


def test_ec_tsk_07_boundary_at_minimum_and_off():
    at = minimum_wage_check(D("16000"), D("160"), mode="TOP_UP", currency="INR", min_hourly_rate=D("100"),
                            verified=True)
    assert at.line is None and at.issues == ()
    off = minimum_wage_check(D("0"), D("160"), mode="OFF", currency="INR", min_hourly_rate=D("100"), verified=True)
    assert off.line is None and off.mode == "OFF"
    rounded_up = minimum_wage_check(D("100"), D("1.333"), mode="TOP_UP", currency="INR", min_hourly_rate=D("100"),
                                    verified=True)
    assert rounded_up.line is not None and rounded_up.line.amount == D("33.30")  # 33.3 exactly
    ceiling = minimum_wage_check(D("100"), D("1.33333"), mode="TOP_UP", currency="INR", min_hourly_rate=D("100"),
                                 verified=True)
    assert ceiling.line is not None and ceiling.line.amount == D("33.34")  # 33.333 rounded up, never below the floor


def test_ec_tsk_13_task_pay_coexists_with_base_pay():
    """Task pay never replaces base pay; wages the pack counts toward the minimum reduce only the top-up."""
    result = pay([task(quantity="10")], {"minimum_wage_floor": "TOP_UP"},
                 minimum_wage=MinimumWageInput(D("160"), D("100"), verified=True, other_wages_counted=D("20000")))
    assert [line.component for line in result.lines] == ["TASK_EARNING"]
    without_base = pay([task(quantity="10")], {"minimum_wage_floor": "TOP_UP"},
                       minimum_wage=MinimumWageInput(D("160"), D("100"), verified=True))
    assert without_base.lines[1].amount == D("8000.00") and without_base.lines[0].amount == D("8000.00")


# --------------------------------------------------------------------------- status, quantity, splits


def test_ec_tsk_02_rejected_and_unapproved_tasks_excluded():
    tasks = [task("ok"), task("rej", status="REJECTED"), task("dis", status="DISPUTED"), task("sub", status="SUBMITTED"),
             task("paid", paid_in_run_id="run-9")]
    result = pay(tasks)
    assert [i.task_id for i in result.items] == ["ok"]
    assert {(e.task_id, e.reason) for e in result.excluded} == {
        ("rej", "NOT_APPROVED"), ("dis", "NOT_APPROVED"), ("sub", "NOT_APPROVED"), ("paid", "ALREADY_PAID")}


def test_ec_tsk_04_only_accepted_quantity_is_paid():
    result = pay([task(quantity="12", accepted_quantity=D("10"))])
    assert result.paid == D("8000.00")


def test_ec_tsk_04_accepted_above_submitted_rejected():
    with pytest.raises(TimePayError) as err:
        pay([task(quantity="12", accepted_quantity=D("13"))])
    assert err.value.code == "ACCEPTED_EXCEEDS_SUBMITTED"


def test_ec_tsk_05_split_totalling_100_is_paid_by_share():
    team = (TaskAssignee(ME, D("33.34")), TaskAssignee("emp-2", D("33.33")), TaskAssignee("emp-3", D("33.33")))
    shares = split_amount(D("100.00"), team, "INR")
    assert shares == {ME: D("33.34"), "emp-2": D("33.33"), "emp-3": D("33.33")}
    result = pay([task(quantity="1", assignees=team)], {"allow_split": True})
    assert result.paid == D("266.72")
    others = sum(split_amount(D("800.00"), team, "INR").values())
    assert others == D("800.00")


@pytest.mark.parametrize("percents", [("60", "30"), ("33.33", "33.33", "33.33"), ("50", "50.001")])
def test_ec_tsk_05_split_not_totalling_exactly_100_rejected(percents):
    team = tuple(TaskAssignee(f"e{i}", D(p)) for i, p in enumerate(percents))
    with pytest.raises(TimePayError) as err:
        validate_split(team, allow_split=True)
    assert err.value.code == "INVALID_SPLIT"


def test_ec_tsk_05_split_needs_allow_split():
    team = (TaskAssignee(ME, D("50")), TaskAssignee("emp-2", D("50")))
    with pytest.raises(TimePayError) as err:
        pay([task(assignees=team)])
    assert err.value.code == "SPLIT_NOT_ALLOWED"


def test_ec_tsk_01_duplicate_external_ref_rejected():
    with pytest.raises(TimePayError) as err:
        pay([task("a", external_ref="X-1"), task("b", external_ref="X-1")])
    assert err.value.code == "DUPLICATE_EXTERNAL_REF"


# --------------------------------------------------------------------------- rates and currency


def test_rate_source_precedence():
    own = task(rate=D("900"), rate_currency="INR")
    assert pay([own]).paid == D("22500.00")  # TASK_THEN_RATE_CARD prefers the task's rate
    assert pay([own], {"rate_source": "RATE_CARD"}).paid == D("20000.00")
    missing = pay([task()], {"rate_source": "TASK"})
    assert missing.paid == 0 and missing.issues[0].code == "TASK_RATE_MISSING"


def test_rate_card_specificity_dates_and_ambiguity():
    card = (RateCardRow("SITE_INSTALL", D("800"), "INR", date(2026, 1, 1)),
            RateCardRow("SITE_INSTALL", D("850"), "INR", date(2026, 1, 1), grade="G2"),
            RateCardRow("SITE_INSTALL", D("900"), "INR", date(2026, 1, 1), grade="G2", location="BLR"),
            RateCardRow("SITE_INSTALL", D("950"), "INR", date(2026, 11, 1), grade="G2", location="BLR"))
    assert pay([task(quantity="1")], rate_card=card, employee=EmployeeAttributes(grade="G2")).paid == D("850.00")
    assert pay([task(quantity="1")], rate_card=card,
               employee=EmployeeAttributes(grade="G2", location="BLR")).paid == D("900.00")
    ambiguous = card + (RateCardRow("SITE_INSTALL", D("870"), "INR", date(2026, 1, 1), grade="G2"),)
    result = pay([task(quantity="1")], rate_card=ambiguous, employee=EmployeeAttributes(grade="G2"))
    assert result.paid == 0 and result.issues[0].code == "TASK_RATE_AMBIGUOUS"
    none = pay([task(quantity="1", task_type="PAINTING")])
    assert none.issues[0].code == "TASK_RATE_NOT_FOUND"


def test_ec_tsk_10_rate_in_another_currency_needs_fx():
    usd = task(quantity="2", rate=D("10.00"), rate_currency="USD")
    blocked = pay([usd])
    assert blocked.paid == 0 and blocked.issues[0].code == "TASK_FX_RATE_MISSING"
    assert blocked.excluded[0].reason == "BLOCKED"
    converted = pay([usd], fx_rates={"USD": D("83.4567")})
    assert converted.paid == D("1669.13") and converted.items[0].fx_rate == D("83.4567")


# --------------------------------------------------------------------------- attribution and switch


def test_attribution_by_approval_or_completion_date():
    late = task(approved_on=date(2026, 11, 2), completed_on=date(2026, 10, 28))
    assert pay([late]).excluded[0].reason == "NOT_YET_DUE"
    assert pay([late], {"attribution": "COMPLETION_DATE"}).paid == D("20000.00")
    earlier = pay([task(approved_on=date(2026, 9, 25))])
    assert earlier.items[0].late and earlier.issues[0].code == "TASK_PAID_LATE"
    undated = pay([task(approved_on=None)])
    assert undated.issues[0].code == "TASK_ATTRIBUTION_DATE_MISSING"


def test_ec_tsk_11_switch_on_later_pays_only_tasks_from_its_effective_date():
    enabled = [(date(2026, 10, 15), None)]
    result = pay([task("before", approved_on=date(2026, 10, 10)), task("after", approved_on=date(2026, 10, 15))],
                 enabled=enabled)
    assert [i.task_id for i in result.items] == ["after"]
    assert result.excluded[0].reason == "SWITCH_OFF"


def test_ec_tsk_12_task_earning_line_traces_to_task():
    line = pay([task("t-42", quantity="3")]).lines[0]
    assert line.component == "TASK_EARNING" and line.amount == D("2400.00")
    assert line.sources[0]["type"] == "task" and line.sources[0]["id"] == "t-42"
    assert line.to_dict()["trace"]["unrounded"] == "2400.00"


def test_evidence_required():
    result = pay([task()], {"requires_evidence": True})
    assert result.paid == 0 and result.issues[0].code == "TASK_EVIDENCE_MISSING"
    assert pay([task(evidence_count=1)], {"requires_evidence": True}).paid == D("20000.00")


def test_ec_tsk_09_contractor_gets_invoice_and_no_wage_floor():
    result = pay([task(quantity="1")], {"contractor_document": "SELF_BILLED_INVOICE", "minimum_wage_floor": "TOP_UP"},
                 minimum_wage=MinimumWageInput(D("160"), D("100"), verified=True), worker_class="CONTRACTOR")
    assert result.document == "SELF_BILLED_INVOICE" and result.minimum_wage is None
    assert pay([task(quantity="1")]).document == "PAYSLIP"


# --------------------------------------------------------------------------- clawback (EC-TSK-08)


def test_ec_tsk_08_clawback_within_protected_net_carries_the_rest():
    partial = task_clawback("t1", D("1000.00"), {}, currency="INR", room=D("300.00"))
    assert partial.line is not None and partial.line.amount == D("-300.00")
    assert partial.carried_forward == D("700.00") and partial.issues[0].code == "TASK_CLAWBACK_PARTIAL"
    full = task_clawback("t1", D("1000.00"), {}, currency="INR", room=D("5000"))
    assert full.line is not None and full.line.amount == D("-1000.00") and full.issues == ()


def test_ec_tsk_08_manual_or_unconfigured_clawback_deducts_nothing():
    manual = task_clawback("t1", D("1000"), {"clawback": "MANUAL"}, currency="INR", room=D("5000"))
    assert manual.line is None and manual.issues[0].code == "TASK_CLAWBACK_MANUAL"
    blocked = task_clawback("t1", D("1000"), {}, currency="INR", room=None)
    assert blocked.line is None and blocked.issues[0].code == "PROTECTED_NET_NOT_SET"


# --------------------------------------------------------------------------- review additions


def test_ec_tsk_01_distinct_external_refs_are_both_paid():
    result = pay([task("a", quantity="1", external_ref="X-1"), task("b", quantity="1", external_ref="X-2")])
    assert result.paid == D("1600.00") and [i.task_id for i in result.items] == ["a", "b"]


def test_ec_tsk_02_paid_or_reversed_status_is_never_paid_again():
    result = pay([task("p", status="PAID"), task("r", status="REVERSED")])
    assert result.lines == () and {(e.task_id, e.reason) for e in result.excluded} == {
        ("p", "ALREADY_PAID"), ("r", "NOT_APPROVED")}


def test_ec_tsk_07_missing_minimum_wage_input_is_not_a_silent_pass():
    result = pay([task(quantity="1")], {"minimum_wage_floor": "TOP_UP"})
    assert [line.component for line in result.lines] == ["TASK_EARNING"]
    assert [(i.code, i.severity) for i in result.issues] == [("MIN_WAGE_NOT_VERIFIED", "WARNING")]
    assert pay([task(quantity="1")], {"minimum_wage_floor": "OFF"}).issues == ()


def test_ec_tsk_09_employee_gets_payslip_and_wage_floor():
    result = pay([task(quantity="1")], {"minimum_wage_floor": "TOP_UP"},
                 minimum_wage=MinimumWageInput(D("160"), D("100"), verified=True))
    assert result.document == "PAYSLIP" and result.minimum_wage is not None
    assert result.lines[1].amount == D("15200.00")


def test_ec_tsk_10_same_currency_needs_no_fx_and_bad_fx_is_rejected():
    inr = pay([task(quantity="2", rate=D("10.00"), rate_currency="INR")])
    assert inr.paid == D("20.00") and inr.items[0].fx_rate is None
    with pytest.raises(TimePayError):
        pay([task(quantity="2", rate=D("10.00"), rate_currency="USD")], fx_rates={"USD": D("0")})


def test_ec_tsk_11_boundary_task_on_the_effective_date_is_payable():
    enabled = [(date(2026, 10, 15), None)]
    on_day = pay([task("x", approved_on=date(2026, 10, 15))], enabled=enabled)
    day_before = pay([task("y", approved_on=date(2026, 10, 14))], enabled=enabled)
    assert on_day.paid == D("20000.00") and day_before.paid == 0


def test_ec_tsk_12_no_eligible_task_no_line():
    result = pay([task("x", status="SUBMITTED")])
    assert result.lines == () and result.paid == 0


def test_ec_tsk_13_top_up_covers_only_the_gap_and_never_replaces_base():
    result = pay([task(quantity="10")], {"minimum_wage_floor": "TOP_UP"},
                 minimum_wage=MinimumWageInput(D("160"), D("100"), verified=True, other_wages_counted=D("5000")))
    assert [(line.component, line.amount) for line in result.lines] == [
        ("TASK_EARNING", D("8000.00")), ("MIN_WAGE_TOP_UP", D("3000.00"))]
