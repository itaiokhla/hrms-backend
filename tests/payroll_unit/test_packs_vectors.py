"""Golden vectors of every pack (guard ALL_VECTORS_PASS) and the pack algorithms checked by hand.

ZZ is fictional; IN is a FIXTURE of the 3.2 §9.1 illustrative table; AE and US are drafts. None of
these numbers is legal advice; the hand computations are written next to each assertion.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.payroll import packs
from app.payroll.engine import statutory
from app.payroll.packs import ae_draft, in_fixture, us_draft, zz_test
from app.payroll.packs.base import PackContext

D = Decimal
ALL = packs.all_packs(environment="test")


@pytest.mark.parametrize("pack", ALL, ids=[p.version for p in ALL])
def test_all_pack_vectors_pass(pack):
    results = packs.run_vectors(pack)
    assert results, f"{pack.version} has no golden vectors"
    failures = {r.vector.id: r.mismatches for r in results if not r.passed}
    assert not failures


def test_vector_runner_reports_mismatches():
    vector = replace(zz_test.PACK.test_vectors()[0], expected={"ZZ_PENSION_EE": "201.00"}, expected_issues=("NOPE",))
    result = statutory.run_vector(zz_test.PACK, vector)
    assert not result.passed
    assert "ZZ_PENSION_EE: expected 201.00, got 200.00" in result.mismatches and "missing issue NOPE" in result.mismatches


def _run(pack, vector_id, **changes):
    vector = next(v for v in pack.test_vectors() if v.id == vector_id)
    vector = replace(vector, **changes)
    ctx = PackContext(version=pack.meta, currency=pack.meta.currency, period_start=vector.period_start,
                      period_end=vector.period_end, settings=vector.settings, employee=vector.employee,
                      tax_profile=vector.tax_profile, ytd=vector.ytd, pack_state=vector.pack_state,
                      choice_defaults=pack.choice_defaults())
    added, issues, bases, taxable = statutory.compute(pack, ctx, vector.lines)
    return {line.component: line for line in added}, issues, ctx, taxable


# --------------------------------------------------------------------------- IN fixture (3.3 §11 Example A)


def test_example_a_epf_at_25000_ceiling_after_17_sep_2026():
    lines, issues, _, _ = _run(in_fixture.PACK, "IN-V01")
    assert lines["IN_EPF_EE"].amount == D("3000.00")             # 12% × min(50,000, 25,000)
    employer = lines["IN_EPF_ER"]
    assert employer.amount == D("3000.00")                        # 12% × 25,000
    assert D(employer.trace["details"]["eps"]) == 2083 and D(employer.trace["details"]["epf"]) == 917  # 8.33% capped
    assert lines["IN_PT"].amount == D("200.00")                   # Maharashtra slab above ₹10,000
    assert "IN_TDS" not in lines                                  # 10,89,000 taxable: rebate takes the 48,900 tax
    assert not [i for i in issues if i.severity == "BLOCKING"]
    assert "min(PF wage 50,000.00, ceiling 25,000.00)" in lines["IN_EPF_EE"].explanation


def test_example_a_before_17_sep_2026_uses_15000_ceiling():
    lines, _, _, _ = _run(in_fixture.PACK, "IN-V02")
    assert lines["IN_EPF_EE"].amount == lines["IN_EPF_ER"].amount == D("1800.00")
    assert D(lines["IN_EPF_ER"].trace["details"]["eps"]) == 1250


def test_ec_sta_05_september_2026_split_period():
    lines, _, _, _ = _run(in_fixture.PACK, "IN-V03")
    # 1–16 Sep: min(50,000 × 16/30, 15,000 × 16/30) = 8,000; 17–30 Sep: min(…, 25,000 × 14/30) = 11,666.67
    assert D(lines["IN_EPF_EE"].trace["base"]).quantize(D("0.01")) == D("19666.67")
    assert lines["IN_EPF_EE"].amount == D("2360.00")
    assert D(lines["IN_EPF_ER"].trace["details"]["eps"]) == 1638  # 8.33% × 19,666.67 under the split cap 1,638.73
    assert len(lines["IN_EPF_EE"].trace["details"]["segments"]) == 2


def test_ec_sta_10_employee_and_employer_portions_are_separate():
    lines, _, _, _ = _run(in_fixture.PACK, "IN-V04")
    assert (lines["IN_ESI_EE"].kind, lines["IN_ESI_EE"].trace["portion"]) == ("DEDUCTION", "EMPLOYEE")
    assert (lines["IN_ESI_ER"].kind, lines["IN_ESI_ER"].trace["portion"]) == ("EMPLOYER_CONTRIBUTION", "EMPLOYER")
    assert lines["IN_ESI_EE"].amount == D("150.00") and lines["IN_ESI_ER"].amount == D("650.00")
    assert lines["IN_ESI_EE"].trace["liability"]["payee"] == "Employees' State Insurance Corporation"


def test_ec_sta_17_esi_eligibility_fixed_for_the_contribution_period():
    covered, _, ctx, _ = _run(in_fixture.PACK, "IN-V05")
    assert covered["IN_ESI_EE"].amount == D("173.00")             # 0.75% × 23,000 = 172.50, up to the next rupee
    assert covered["IN_ESI_ER"].amount == D("748.00")             # 3.25% × 23,000 = 747.50
    assert "IN.esi" not in ctx.state_out                          # the earlier determination is kept
    first, _, ctx, _ = _run(in_fixture.PACK, "IN-V06")
    assert not first
    assert ctx.state_out["IN.esi"] == {"contribution_period": "2026-10-01..2027-03-31", "covered": False,
                                       "determined_on": "2026-10-31"}
    assert in_fixture.contribution_period(date(2027, 2, 28)) == (date(2026, 10, 1), date(2027, 3, 31))
    assert in_fixture.contribution_period(date(2026, 4, 1)) == (date(2026, 4, 1), date(2026, 9, 30))


def test_esi_coverage_ignores_overtime_and_uses_disability_threshold():
    vector = next(v for v in in_fixture.PACK.test_vectors() if v.id == "IN-V06")
    disabled, _, _, _ = _run(in_fixture.PACK, "IN-V06", employee={"disability": True})
    assert disabled["IN_ESI_EE"].amount == D("173.00")            # ₹25,000 threshold for persons with disabilities
    from app.payroll.packs.base import earning

    overtime = earning("OT_WORKDAY", "3000.00", "INR", ("IN_ESI_WAGE",), category="OVERTIME")
    lines, _, _, _ = _run(in_fixture.PACK, "IN-V06", lines=(*vector.lines[:1], overtime), pack_state={})
    assert lines["IN_ESI_EE"].amount == D("158.00")               # 18,000 covers; 21,000 × 0.75% = 157.50 → 158


@pytest.mark.parametrize("subdivision,gender,gross,month,ytd,expected", [
    ("IN-MH", "MALE", "7500.00", 10, "0", None),
    ("IN-MH", "MALE", "9000.00", 10, "0", "175.00"),
    ("IN-MH", "MALE", "10000.01", 10, "0", "200.00"),
    ("IN-MH", "FEMALE", "25000.00", 10, "0", None),
    ("IN-MH", "FEMALE", "25000.01", 10, "0", "200.00"),
    ("IN-MH", None, "30000.00", 10, "0", "200.00"),               # above ₹25,000 both slabs give 200
    ("IN-MH", "MALE", "30000.00", 2, "0", "300.00"),
    ("IN-KA", None, "24999.99", 10, "0", None),
    ("IN-KA", None, "25000.00", 10, "0", "200.00"),
    ("IN-KA", None, "25000.00", 2, "2400.00", "100.00"),          # ₹2,500 a year at most
])
def test_ec_sta_14_professional_tax_slabs(subdivision, gender, gross, month, ytd, expected):
    from app.payroll.packs.base import earning

    year = 2027 if month < 4 else 2026
    end = date(year, month, 28 if month == 2 else 31)
    lines, issues, _, _ = _run(in_fixture.PACK, "IN-V07", lines=(earning("BASIC", gross, "INR"),),
                               employee={"branch_subdivision": subdivision, "gender": gender},
                               period_start=date(year, month, 1), period_end=end, ytd={"IN_PT": ytd})
    assert (lines["IN_PT"].amount if "IN_PT" in lines else None) == (D(expected) if expected else None)
    assert not [i for i in issues if i.severity == "BLOCKING"]


def test_professional_tax_needs_the_branch_state_and_the_slab():
    from app.payroll.packs.base import earning

    _, issues, _, _ = _run(in_fixture.PACK, "IN-V07", employee={})
    assert [i.code for i in issues] == ["BRANCH_SUBDIVISION_MISSING"]
    _, issues, _, _ = _run(in_fixture.PACK, "IN-V07", employee={"branch_subdivision": "IN-MH"})
    assert [i.code for i in issues] == ["PT_SLAB_UNDETERMINED"]
    lines, issues, _, _ = _run(in_fixture.PACK, "IN-V07", employee={"branch_subdivision": "IN-DL"},
                               lines=(earning("BASIC", "50000.00", "INR"),))
    assert not lines and [(i.code, i.severity) for i in issues] == [("PT_NOT_IN_FIXTURE", "FLAG")]


@pytest.mark.parametrize("taxable,expected", [
    ("1200000", "0"),                  # 60,000 tax, rebate 60,000
    ("1210000", "10000"),              # 61,500 tax, marginal relief to the 10,000 above ₹12L
    ("1300000", "75000"),              # 20,000 + 40,000 + 15% × 1,00,000; relief limit 1,00,000 not reached
    ("2325000", "281250"),
    ("300000", "0"),
])
def test_new_regime_tax_rebate_and_marginal_relief(taxable, expected):
    tax, _ = in_fixture.new_regime_tax(D(taxable), date(2026, 10, 31))
    assert tax == D(expected)


def test_tds_projection_and_previous_employer_ec_sta_07():
    lines, _, ctx, _ = _run(in_fixture.PACK, "IN-V11")
    tds = lines["IN_TDS"]
    assert tds.amount == D("23438.00")            # (2,81,250 − 1,40,625) ÷ 6 = 23,437.50 → nearest rupee
    assert tds.trace["details"]["remaining_periods"] == 6
    lines, _, _, _ = _run(in_fixture.PACK, "IN-V12")
    assert lines["IN_TDS"].amount == D("11458.00")  # (1,08,750 − 40,000) ÷ 6 = 11,458.33


def test_tds_regime_and_final_settlement():
    _, issues, _, _ = _run(in_fixture.PACK, "IN-V12", tax_profile={"regime": "OLD"})
    assert [(i.code, i.severity) for i in issues] == [("TAX_REGIME_NOT_SUPPORTED", "BLOCKING")]
    vector = next(v for v in in_fixture.PACK.test_vectors() if v.id == "IN-V11")
    ctx = PackContext(version=in_fixture.PACK.meta, currency="INR", period_start=vector.period_start,
                      period_end=vector.period_end, settings=vector.settings, ytd=vector.ytd, final_settlement=True,
                      choice_defaults=in_fixture.PACK.choice_defaults())
    added, _, _, _ = statutory.compute(in_fixture.PACK, ctx, vector.lines)
    # exit in October: projected = 12,00,000 + 2,00,000; tax on 13,25,000 = 78,750; due now 78,750 − 1,40,625 < 0
    assert not added and D(ctx.scratch["IN.tds"]["annual_tax"]) == 78750


def test_labour_code_wage_test_is_reported_not_applied():
    from app.payroll.packs.base import earning

    lines = (earning("BASIC", "30000.00", "INR", ("IN_PF_WAGE", "IN_CODE_WAGES_INCLUDED")),
             earning("HRA", "40000.00", "INR", ("IN_CODE_WAGES_EXCLUDED",), category="FIXED_ALLOWANCE"))
    added, issues, _, _ = _run(in_fixture.PACK, "IN-V03", lines=lines, period_start=date(2026, 10, 1),
                               period_end=date(2026, 10, 31))
    review = next(i for i in issues if i.code == "IN_CODE_WAGES_ADDBACK_REVIEW")
    # excluded 40,000 + employer PF 3,000 = 43,000; total 73,000; excess 43,000 − 36,500 = 6,500
    assert D(review.details["excess"]) == D("6500.00")
    assert added["IN_EPF_EE"].amount == D("3000.00")              # base unchanged


def test_eps_stops_at_age_58():
    lines, _, _, _ = _run(in_fixture.PACK, "IN-V01", employee={"branch_subdivision": "IN-MH", "gender": "MALE",
                                                               "date_of_birth": date(1968, 10, 1)})
    details = lines["IN_EPF_ER"].trace["details"]
    assert D(details["eps"]) == 0 and D(details["epf"]) == 3000 and details["eps_exempt_age_58"] is True


def test_actual_wages_basis_keeps_eps_capped():
    lines, _, _, _ = _run(in_fixture.PACK, "IN-V01",
                          settings={**in_fixture._ON, "payroll.statutory.IN.epf.wage_basis": "ACTUAL_WAGES"})
    assert lines["IN_EPF_EE"].amount == lines["IN_EPF_ER"].amount == D("6000.00")
    assert D(lines["IN_EPF_ER"].trace["details"]["eps"]) == 2083
    assert lines["IN_EDLI_ER"].amount == D("125.00")              # EDLI on the capped wage


# --------------------------------------------------------------------------- ZZ, AE, US


def test_ec_sta_16_ytd_ceiling_zz():
    lines, _, ctx, _ = _run(zz_test.PACK, "ZZ-V04")
    assert lines["ZZ_PENSION_EE"].trace["base"] == "3000.00"
    assert ctx.scratch["ZZ.pension"] == {"base": "3000.00", "ytd_base_after": "50000.00"}
    assert not ctx.state_out
    increments = zz_test.PACK.ytd_increments(list(lines.values()), taxable=D("5810.00"))
    assert increments["ZZ_PENSION_WAGE"] == D("3000.00") and increments["ZZ_PENSION_EE"] == D("150.00")


def test_ae_gratuity_draft():
    value, text = ae_draft.end_of_service_gratuity(D("10000"), date(2021, 1, 1), date(2023, 12, 31))
    # 1,095 days (no leap day) = 3 years × 21 days × (10,000 × 12 ÷ 365)
    assert value == D("20712.33") and "21" in text
    long_value, _ = ae_draft.end_of_service_gratuity(D("10000"), date(2017, 1, 1), date(2023, 12, 30))
    assert long_value == D("54246.58")                             # (5 × 21 + 2 × 30) days of 328.767…
    assert ae_draft.end_of_service_gratuity(D("10000"), date(2025, 6, 1), date(2026, 5, 1))[0] == 0
    capped, text = ae_draft.end_of_service_gratuity(D("10000"), date(1980, 1, 1), date(2026, 12, 31))
    assert capped == D("240000.00") and "capped" in text


def test_us_supplemental_withholding_above_one_million():
    lines, issues, _, _ = _run(us_draft.PACK, "US-V05", ytd={"US_SUPPLEMENTAL_WAGES": "999000.00"})
    # 1,000 at 22% + 1,000 above $1M at 37%
    assert lines["US_FITW"].amount == D("590.00")
    assert "US_FIT_NOT_IMPLEMENTED" in {i.code for i in issues}
