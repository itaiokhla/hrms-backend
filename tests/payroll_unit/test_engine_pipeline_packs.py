"""The pipeline with the real statutory stage (``engine.statutory``) and the shipped test packs.

ZZ is the fictional VERIFIED_FOR_PERIOD pack (tests only); IN is the fixture pack, allowed only
outside production and badged "FIXTURE — not law" (decision I-07). Without a pack the engine
reports STATUTORY_NOT_VERIFIED, blocking unless an attestation exists (decision I-15).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.payroll import packs
from app.payroll.engine import calculate, replay
from tests.payroll_unit.test_engine_fixtures import absent, catalogue, component, days, settings, snapshot

D = Decimal


def amounts(result) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for line in result.lines:
        found[line.component] = found.get(line.component, D(0)) + line.amount
    return found


def issue(result, code):
    return next((i for i in result.issues if i.code == code), None)


def switches(*schemes) -> dict:
    return settings(**{scheme.switch_key.replace(".", "__"): True for scheme in schemes})


def zz_snapshot(**overrides):
    from app.payroll.packs.zz_test import LEVY, PENSION, WHT

    pack = packs.find_pack("ZZ", environment="test")
    structure = {"id": 1, "structure_type": "GROSS_BASED", "currency": "EUR", "pay_basis_amount": "4000.00",
                 "lines": [{"component": "BASIC", "method": "FIXED", "amount": "4000.00"}]}
    data = snapshot(
        currency="EUR", employee={"employee_id": 9, "hire_date": "2020-01-01", "country": "ZZ"},
        components=catalogue(component("BASIC", tags=["LOP_APPLICABLE", "ZZ_PENSION_WAGE", "TAXABLE_INCOME"],
                                       proration="BY_PAID_DAYS")),
        segments=[{"key": "S1", "start": "2026-11-01", "end": "2026-11-30", "compensation": structure}],
        settings=switches(PENSION, LEVY, WHT), pack=packs.pack_snapshot(pack), tax_profile={"resident": True})
    data.update(overrides)
    return data


def test_verified_pack_end_to_end_zz():
    """ZZ: levy 40, pension 200 (employee) and 280 (employer, cost only), withholding 20% × 3,760 = 752."""
    result = calculate(zz_snapshot())
    got = amounts(result)
    assert got["ZZ_HEALTH_LEVY"] == D("40.00") and got["ZZ_PENSION_EE"] == D("200.00")
    assert got["ZZ_PENSION_ER"] == D("280.00") and got["ZZ_WHT"] == D("752.00")
    assert result.totals.net == D("3008.00") and result.totals.employer_cost == D("4280.00")
    assert all(line.statutory and not line.fixture for line in result.lines if line.component.startswith("ZZ_"))
    assert result.ok, [i.to_json() for i in result.blocking]
    assert replay(zz_snapshot(), result.calc_hash)[0]


def test_verified_pack_uses_earned_amounts_after_lop_ec_sta_13():
    """Statutory bases use earned amounts (2 LOP days: 4,000 × 28 ÷ 30) and the pack's whole-unit rounding
    overrides the company line rounding (EC-STA-13)."""
    result = calculate(zz_snapshot(days=days(per_day=absent(dt.date(2026, 11, 12), dt.date(2026, 11, 13)))))
    assert amounts(result)["BASIC"] == D("3733.33")
    assert amounts(result)["ZZ_PENSION_EE"] == D("187.00")  # 5% × 3,733.33, whole units (EC-STA-13)


def test_test_only_pack_refused_in_production():
    result = calculate(zz_snapshot(run={"environment": "production"}))
    found = issue(result, "STATUTORY_NOT_VERIFIED")
    assert found.severity == "BLOCKING" and not any(line.statutory for line in result.lines)


def test_no_pack_is_not_verified_and_blocks_finalization_i_15():
    result = calculate(snapshot())
    assert issue(result, "STATUTORY_NOT_VERIFIED").severity == "BLOCKING"
    attested = calculate(snapshot(attestation={"active": True, "id": 4}))
    assert issue(attested, "STATUTORY_NOT_VERIFIED").severity == "WARNING"
    assert amounts(attested)["BASIC"] == D("50000.00")


def test_in_fixture_pack_section_17_statutory_lines_are_fixture_badged():
    """The IN fixture pack in a test environment: EPF 3,000.00 (₹25,000 ceiling) and Maharashtra PT 200.00."""
    from app.payroll.packs.in_fixture import EPF, PT, TDS

    pack = packs.find_pack("IN", on=dt.date(2026, 11, 30), environment="test")
    pf_tags = ["LOP_APPLICABLE", "PART_OF_CTC", "PART_OF_GROSS", "OT_BASE", "IN_PF_WAGE", "TAXABLE_INCOME"]
    data = snapshot(
        days=days(per_day=absent(dt.date(2026, 11, 12), dt.date(2026, 11, 13))),
        components=catalogue(component("BASIC", tags=pf_tags, proration="BY_PAID_DAYS", version_id=101)),
        employee={"employee_id": 501, "hire_date": "2022-04-01", "country": "IN", "subdivision": "IN-MH",
                  "protected": {"gender": "MALE"}},
        settings=switches(EPF, PT, TDS), pack=packs.pack_snapshot(pack), tax_profile={"regime": "NEW"},
    )
    result = calculate(data)
    got = amounts(result)
    assert got["BASIC"] == D("46666.67")
    assert got["IN_EPF_EE"] == D("3000.00") and got["IN_PT"] == D("200.00")
    statutory = [line for line in result.lines if line.statutory]
    assert statutory and all(line.fixture for line in statutory)
    assert issue(result, "STATUTORY_FIXTURE").severity == "FLAG"
    assert result.totals.net == result.totals.gross - result.totals.deductions
    assert issue(result, "INVARIANT_VIOLATION") is None


def test_in_fixture_never_runs_in_production():
    pack = packs.find_pack("IN", on=dt.date(2026, 11, 30), environment="test")
    result = calculate(snapshot(run={"environment": "production"}, pack=packs.pack_snapshot(pack)))
    assert issue(result, "STATUTORY_NOT_VERIFIED").severity == "BLOCKING"
    assert not any(line.statutory for line in result.lines)


# --------------------------------------------------------------------------- the real stage 8 (engine.deductions)


def real_deductions(monkeypatch, statutory=None):
    from app.payroll.engine import pipeline
    from tests.payroll_unit.test_engine_fixtures import pack_stage, use_stages

    deductions = pipeline._stage("deductions")
    use_stages(monkeypatch, statutory=statutory or pack_stage(), deductions=deductions)


def test_section_17_net_with_the_real_deductions_stage(monkeypatch):
    """3.5 §17 again, with the loan instalment taken by engine.deductions within the protected net: 96,775.65."""
    from tests.payroll_unit.test_engine_golden import section_17_snapshot

    real_deductions(monkeypatch)
    data = section_17_snapshot()
    data["settings"]["payroll.loans.enabled"] = True
    result = calculate(data)
    assert amounts(result)["LOAN_RECOVERY"] == D("2000.00")
    assert result.totals.net == D("96775.65")
    assert result.stage_outputs["deductions"]["carry_forwards"] == []
    assert result.ok, [i.to_json() for i in result.blocking]


def test_recoveries_blocked_without_protected_net_g_23(monkeypatch):
    """With payroll.deductions.min_net_pay_percent unset the loan is not taken and is carried forward."""
    real_deductions(monkeypatch)
    data = snapshot(settings=settings(payroll__loans__enabled=True), loans=[{"loan_id": 5, "instalment": "2000.00"}])
    data["settings"].pop("payroll.deductions.min_net_pay_percent")
    result = calculate(data)
    assert "LOAN_RECOVERY" not in amounts(result)
    blocked = issue(result, "PROTECTED_NET_NOT_SET")
    assert blocked.severity == "BLOCKING" and blocked.waivable
    carry = result.stage_outputs["deductions"]["carry_forwards"]
    assert carry and carry[0]["carried_forward"] == "2000.00"


def test_loan_beyond_protected_net_is_partly_deferred_ec_var_05(monkeypatch):
    real_deductions(monkeypatch)
    data = snapshot(settings=settings(payroll__loans__enabled=True),
                    loans=[{"loan_id": 5, "instalment": "60000.00"}])
    result = calculate(data)
    # gross 97,000 − EPF 3,000 − PT 200 = 93,800; protected 50% of gross = 48,500 → room 45,300
    assert amounts(result)["LOAN_RECOVERY"] == D("45300.00")
    assert result.totals.net == D("48500.00")
    assert result.stage_outputs["deductions"]["carry_forwards"][0]["carried_forward"] == "14700.00"
    assert issue(result, "NEGATIVE_NET") is None


def test_fixed_amount_late_penalty_taken_by_the_deductions_stage(monkeypatch):
    real_deductions(monkeypatch)
    every = {dt.date(2026, 11, d): {"attendance": {"status": "PRESENT", "worked_seconds": 28800,
                                                   "late": 2 <= d <= 7}}
             for d in range(1, 31) if dt.date(2026, 11, d).weekday() != 6}
    data = snapshot(days=days(per_day=every),
                    settings=settings(payroll__attendance__enabled=True, payroll__late_penalty__enabled=True),
                    policies={"LATE_PENALTY_POLICY": {"fields": {
                        "penalty_type": "FIXED_AMOUNT", "amount_per_late": {"amount": "100.00", "currency": "INR"}}}})
    result = calculate(data)
    penalty = [line for line in result.lines if line.component == "LATE_PENALTY"]
    assert len(penalty) == 1 and penalty[0].amount == D("400.00") and penalty[0].stage == 8


def test_statutory_outputs_are_reported():
    result = calculate(zz_snapshot())
    outputs = result.stage_outputs["statutory"]
    assert {item["component"] for item in outputs["liabilities"]} >= {"ZZ_PENSION_EE", "ZZ_PENSION_ER"}
    assert outputs["taxable"] is not None


def test_section_17_with_both_real_stages_and_the_in_fixture_pack():
    """3.5 §17 with no test stage at all: IN fixture statutory lines and the loan taken by engine.deductions.

    Gross is exactly 1,01,975.65; net is gross − EPF 3,000 − PT 200 − TDS − loan 2,000; replay reproduces it.
    """
    from app.payroll.packs.in_fixture import EPF, PT, TDS
    from tests.payroll_unit.test_engine_golden import section_17_snapshot

    pack = packs.find_pack("IN", on=dt.date(2026, 11, 30), environment="test")
    pf_tags = ["LOP_APPLICABLE", "PART_OF_CTC", "PART_OF_GROSS", "OT_BASE", "IN_PF_WAGE", "TAXABLE_INCOME"]
    data = section_17_snapshot()
    data["settings"].update({scheme.switch_key: True for scheme in (EPF, PT, TDS)})
    data["settings"]["payroll.loans.enabled"] = True
    data.update(components=catalogue(component("BASIC", tags=pf_tags, proration="BY_PAID_DAYS", version_id=101)),
                employee={"employee_id": 501, "hire_date": "2022-04-01", "country": "IN", "subdivision": "IN-MH",
                          "protected": {"gender": "MALE"}},
                pack=packs.pack_snapshot(pack), tax_profile={"regime": "NEW"})
    result = calculate(data)
    got = amounts(result)
    assert result.totals.gross == D("101975.65")
    assert got["IN_EPF_EE"] == D("3000.00") and got["IN_PT"] == D("200.00") and got["LOAN_RECOVERY"] == D("2000.00")
    tax = got.get("IN_TDS", D(0))
    assert result.totals.net == D("96775.65") - tax
    assert issue(result, "INVARIANT_VIOLATION") is None
    assert sum(p.amount for p in result.payments) == result.totals.net
    assert replay(data, result.calc_hash)[0]
