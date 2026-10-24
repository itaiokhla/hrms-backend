"""Edge cases that had no test naming them (Goal 2 §21, gate-ec, 24 Oct 2026): pure engine and helpers.

Every expected figure is written out by hand from the scenario; the engine is only called to produce the
result under test. The DB-backed half (and the checks that import the application settings) lives in
tests/payroll/verification/test_edge_cases_gap.py.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.payroll import packs
from app.payroll.engine import calculate
from tests.payroll_unit.test_engine_fixtures import catalogue, component, days, overtime_policy, settings, snapshot

D = Decimal


def nov(day: int) -> dt.date:
    return dt.date(2026, 11, day)


def amounts(result) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for line in result.lines:
        found[line.component] = found.get(line.component, D(0)) + line.amount
    return found


def issue(result, code):
    return next((i for i in result.issues if i.code == code), None)


def worked(seconds: int) -> dict:
    return {"attendance": {"status": "PRESENT", "session_id": f"s-{seconds}", "worked_seconds": seconds}}


# --------------------------------------------------------------------------- EC-CMP-22


def test_ec_cmp_22_fixed_hourly_commission_and_prorated_salary_in_one_period():
    """EC-CMP-22: one November 2026 period, a joiner on 16 Nov with three kinds of pay, each by its own rule:

    * BASIC 30,000.00 FIXED, BY_PAID_DAYS → 30,000 × 15 employed days ÷ 30 = 15,000.00;
    * HOURLY_PAY 200.00/h × attended hours: 13 working days (16–30 Nov without the Sundays 22 and 29) × 4 h =
      52 h → 10,400.00, never prorated by days;
    * COMMISSION, an approved input of 4,321.00, paid in full (inputs are not prorated).
    Gross 29,721.00; no deductions (no pack), so net 29,721.00.
    """
    per_day = {nov(d): worked(4 * 3600) for d in range(16, 31) if nov(d).weekday() != 6}
    mixed = {"id": 960, "version": 1, "structure_type": "GROSS_BASED", "currency": "INR",
             "pay_basis_amount": "30000.00",
             "lines": [{"component": "BASIC", "method": "FIXED", "amount": "30000.00"},
                       {"component": "HOURLY_PAY", "method": "RATE_X_UNITS",
                        "params": {"rate": "200.00", "units": "att.payable_hours"}}]}
    data = snapshot(
        employee={"employee_id": 503, "hire_date": "2026-11-16", "country": "IN", "standard_hours_per_day": "8"},
        components=catalogue(component("HOURLY_PAY", category="BASE", tags=["PART_OF_GROSS"],
                                       calc_method="RATE_X_UNITS"),
                             component("COMMISSION", category="COMMISSION", tags=["PART_OF_GROSS"],
                                       calc_method="INPUT")),
        segments=[{"key": "S1", "start": "2026-11-01", "end": "2026-11-30", "assignment_id": 41,
                   "compensation": mixed}],
        days=days(per_day=per_day, employed_from=nov(16)),
        settings=settings(payroll__attendance__enabled=True),
        wage_inputs=[{"id": 5, "component": "COMMISSION", "amount": "4321.00"}],
    )
    result = calculate(data)
    got = amounts(result)
    assert got["BASIC"] == D("15000.00")
    assert got["HOURLY_PAY"] == D("10400.00") and "52 hours" in result.line("HOURLY_PAY").explanation
    assert got["COMMISSION"] == D("4321.00")
    assert result.totals.gross == D("29721.00") and result.totals.net == D("29721.00")
    assert issue(result, "NEW_JOINER") is not None


# --------------------------------------------------------------------------- EC-CMP-24


def _zz(extra_components=(), wage_inputs=()):
    from app.payroll.packs.zz_test import LEVY, PENSION, WHT

    pack = packs.find_pack("ZZ", environment="test")
    structure = {"id": 1, "structure_type": "GROSS_BASED", "currency": "EUR", "pay_basis_amount": "4000.00",
                 "lines": [{"component": "BASIC", "method": "FIXED", "amount": "4000.00"}]}
    switches = settings(**{s.switch_key.replace(".", "__"): True for s in (PENSION, LEVY, WHT)})
    return snapshot(
        currency="EUR", employee={"employee_id": 9, "hire_date": "2020-01-01", "country": "ZZ"},
        components=catalogue(component("BASIC", tags=["LOP_APPLICABLE", "ZZ_PENSION_WAGE", "TAXABLE_INCOME"],
                                       proration="BY_PAID_DAYS"), *extra_components),
        segments=[{"key": "S1", "start": "2026-11-01", "end": "2026-11-30", "compensation": structure}],
        settings=switches, pack=packs.pack_snapshot(pack), tax_profile={"resident": True},
        wage_inputs=list(wage_inputs))


def test_ec_cmp_24_benefit_in_kind_raises_taxable_income_but_never_net_pay():
    """EC-CMP-24 with the fictional verified ZZ pack (tests only): a company car worth 500.00 a month, tagged
    TAXABLE_INCOME, is a BENEFIT_IN_KIND line. Without it ZZ withholds 20% × 3,760 = 752.00 and net is 3,008.00
    (test_engine_pipeline_packs). With it the taxable base is 4,260 (the levy and pension do not use the car),
    so withholding is 20% × 4,260 = 852.00: 100.00 more tax, and net 2,908.00 = 3,008.00 − 100.00. The car's
    500.00 is never paid in cash: gross stays 4,000.00 and the benefit is reported on its own total."""
    car = component("CAR", kind="BENEFIT_IN_KIND", category="BENEFIT", tags=["TAXABLE_INCOME"], calc_method="INPUT")
    base = calculate(_zz())
    with_car = calculate(_zz([car], [{"id": 1, "component": "CAR", "amount": "500.00"}]))
    assert amounts(base)["ZZ_WHT"] == D("752.00") and base.totals.net == D("3008.00")
    got = amounts(with_car)
    assert got["CAR"] == D("500.00") and with_car.line("CAR").kind == "BENEFIT_IN_KIND"
    assert got["ZZ_HEALTH_LEVY"] == D("40.00") and got["ZZ_PENSION_EE"] == D("200.00")  # bases unchanged
    assert got["ZZ_WHT"] == D("852.00")
    assert with_car.totals.gross == D("4000.00") and with_car.totals.benefits_in_kind == D("500.00")
    assert with_car.totals.net == D("2908.00") == base.totals.net - (got["ZZ_WHT"] - D("752.00"))
    assert with_car.ok, [i.to_json() for i in with_car.blocking]


# --------------------------------------------------------------------------- EC-OT-12


def test_ec_ot_12_unusually_high_overtime_is_flagged_for_review():
    """EC-OT-12: 10 h of workday overtime at the 3.5 §17 rate (OT_BASE 50,000 ÷ 26 ÷ 8 × 1.5 = 360.5769…/h →
    3,605.77) against a trailing history of 400, 450, 500 and 420 (median 435; 3 × 435 = 1,305) is flagged
    COMPONENT_OUTLIER (a review FLAG, never a block); the same overtime against a history around 3,500 is not."""
    def run(history):
        return calculate(snapshot(
            settings=settings(payroll__overtime__enabled=True),
            policies={"OVERTIME_POLICY": overtime_policy()},
            overtime_entries=[{"entry_id": "ot-12", "date": "2026-11-05", "minutes": 600}],
            previous={"period_ref": "2026-10", "currency": "INR", "net": "96000.00", "bank_account_ids": [9001],
                      "history": {"OT_WORKDAY": history}}))

    high = run(["400.00", "450.00", "500.00", "420.00"])
    assert amounts(high)["OT_WORKDAY"] == D("3605.77")
    flag = issue(high, "COMPONENT_OUTLIER")
    assert flag is not None and flag.severity == "FLAG" and flag.component == "OT_WORKDAY"
    assert flag.details["median"] == "435.00"
    usual = run(["3400.00", "3600.00", "3500.00"])
    assert issue(usual, "COMPONENT_OUTLIER") is None


# --------------------------------------------------------------------------- EC-VAR-12 (defect fixed 24 Oct)


def _clawback(amount, **setting_changes):
    return calculate(snapshot(wage_inputs=[{"id": 1, "component": "BONUS", "amount": amount, "reason": "Clawback"}],
                              settings=settings(**setting_changes)))


def test_ec_var_12_bonus_clawback_is_a_negative_input_within_the_protected_net():
    """EC-VAR-12: Example A pays 97,000.00 cash (BASIC 50,000 + HRA 20,000 + FIXED_ALLOWANCE 27,000); the
    protected net is 50% of that, 48,500.00.

    * A clawback of 10,000.00 is a negative BONUS line: gross 87,000.00 = net, above the floor: no issue.
    * A clawback of 80,000.00 would leave 17,000.00 (it used to pass silently): blocking, non-waivable
      CLAWBACK_EXCEEDS_PROTECTED_NET; at most 97,000 − 48,500 = 48,500.00 can be recovered this period.
    * Without a protected net percentage (G-23) a clawback is blocked like every automated recovery, waivably.
    """
    small = _clawback("-10000.00")
    assert amounts(small)["BONUS"] == D("-10000.00")
    assert small.totals.gross == D("87000.00") and small.totals.net == D("87000.00")
    assert not [i for i in small.issues if i.code.startswith("CLAWBACK")]

    large = _clawback("-80000.00")
    found = issue(large, "CLAWBACK_EXCEEDS_PROTECTED_NET")
    assert found.severity == "BLOCKING" and found.waivable is False
    assert found.details["protected_floor"] == "48500.00" and found.details["gross_before_clawback"] == "97000.00"
    assert found.details["recoverable_this_period"] == "48500.00" and found.details["components"] == ["BONUS"]
    assert large.totals.net == D("17000.00")  # never silently shortened: the input must be reduced

    unset = _clawback("-10000.00", payroll__deductions__min_net_pay_percent=None)
    found = issue(unset, "CLAWBACK_PROTECTED_NET_NOT_SET")
    assert found.severity == "BLOCKING" and found.waivable is True


def test_ec_var_12_a_clawback_also_lowers_the_room_left_for_loan_instalments(monkeypatch):
    """The floor is taken on the cash earnings before the clawback (97,000 × 50% = 48,500), not on the reduced
    gross: with a 40,000.00 clawback net before deductions is 57,000.00, so a 20,000.00 loan instalment fits
    only up to 57,000 − 48,500 = 8,500.00 and the rest (11,500.00) is carried forward."""
    from app.payroll.engine import pipeline

    data = snapshot(wage_inputs=[{"id": 1, "component": "BONUS", "amount": "-40000.00"}],
                    loans=[{"loan_id": 5, "instalment": "20000.00"}],
                    settings=settings(payroll__loans__enabled=True))
    real = pipeline._stage
    # No pack: the statutory stage adds nothing; stage 8 is the real one under test.
    monkeypatch.setattr(pipeline, "_stage", lambda name: (lambda ctx, lines: (list(lines), []))
                        if name == "statutory" else real(name))
    result = calculate(data)
    assert amounts(result)["LOAN_RECOVERY"] == D("8500.00")
    deferred = issue(result, "DEDUCTION_DEFERRED")
    assert deferred.details["carried_forward"] == "11500.00" and deferred.details["protected_floor"] == "48500.00"
    assert result.totals.net == D("48500.00")
    assert issue(result, "CLAWBACK_EXCEEDS_PROTECTED_NET") is None


# --------------------------------------------------------------------------- EC-VAR-13


def _benefit_structure(with_benefit: bool) -> dict:
    lines = [{"component": "BASIC", "method": "FIXED", "amount": "30000.00"}]
    if with_benefit:
        lines += [{"component": "MEAL", "method": "FIXED", "amount": "3000.00"},
                  {"component": "GYM", "method": "FIXED", "amount": "900.00"}]
    return {"id": 970 + with_benefit, "version": 1, "structure_type": "GROSS_BASED", "currency": "INR",
            "pay_basis_amount": "30000.00", "lines": lines}


def _election(first: bool, second: bool):
    data = snapshot(
        components=catalogue(
            component("BASIC", tags=["PART_OF_GROSS"], proration="BY_PAID_DAYS", calc_method="FIXED"),
            component("MEAL", category="FIXED_ALLOWANCE", tags=["PART_OF_GROSS"], proration="BY_PAID_DAYS",
                      calc_method="FIXED"),
            component("GYM", category="FIXED_ALLOWANCE", tags=["PART_OF_GROSS"], proration="NONE",
                      calc_method="FIXED")),
        segments=[{"key": "S1", "start": "2026-11-01", "end": "2026-11-10", "assignment_id": 41,
                   "compensation": _benefit_structure(first)},
                  {"key": "S2", "start": "2026-11-11", "end": "2026-11-30", "assignment_id": 41,
                   "compensation": _benefit_structure(second)}],
        days=days(segments=[("S1", nov(1), nov(10)), ("S2", nov(11), nov(30))]))
    return calculate(data)


def test_ec_var_13_benefit_election_starting_or_ending_mid_period_follows_the_component_proration():
    """EC-VAR-13: an election is a compensation change from its date (there is no separate benefits-enrolment
    module), so the period has two segments, 1–10 and 11–30 November.

    * Elected from 11 Nov: MEAL 3,000.00 BY_PAID_DAYS → 3,000 × 20 ÷ 30 = 2,000.00.
    * Ended after 10 Nov: MEAL → 3,000 × 10 ÷ 30 = 1,000.00.
    * GYM 900.00 has proration NONE: paid in full either way, as its component says.
    BASIC (30,000.00 in both segments) adds up to 10,000.00 + 20,000.00 = 30,000.00."""
    starts = _election(False, True)
    got = amounts(starts)
    assert got["MEAL"] == D("2000.00") and got["GYM"] == D("900.00") and got["BASIC"] == D("30000.00")
    ends = _election(True, False)
    got = amounts(ends)
    assert got["MEAL"] == D("1000.00") and got["GYM"] == D("900.00") and got["BASIC"] == D("30000.00")
