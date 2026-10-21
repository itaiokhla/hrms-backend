"""Engine stages 5-7 (``engine.statutory``): the verified gate, fixtures, previews and pack lines (3.5 §7, I-07, I-15)."""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

import pytest

from app.payroll import packs
from app.payroll.engine import statutory
from app.payroll.engine.lines import STAGE_EARNINGS, Line
from app.payroll.packs import in_fixture, zz_test
from app.payroll.packs.base import earning

D = Decimal
MARCH = (dt.date(2026, 3, 1), dt.date(2026, 3, 31))
ZZ_ON = {"payroll.statutory.ZZ.pension.enabled": True, "payroll.statutory.ZZ.levy.enabled": True,
         "payroll.statutory.ZZ.wht.enabled": True}
IN_ON = dict(in_fixture._ON)


@dataclass
class Ctx:
    currency: str = "EUR"
    period_start: dt.date = MARCH[0]
    period_end: dt.date = MARCH[1]
    settings: dict = field(default_factory=lambda: dict(ZZ_ON))
    pack: Optional[dict] = field(default_factory=lambda: packs.pack_snapshot(zz_test.PACK))
    ytd: dict = field(default_factory=dict)
    tax_profile: Optional[dict] = field(default_factory=lambda: {"resident": True})
    environment: str = "test"
    attestation: Any = None
    employee: dict = field(default_factory=dict)
    pack_state: dict = field(default_factory=dict)
    preview: bool = False
    frequency: str = "MONTHLY"
    country: Optional[str] = None
    paid_days: Optional[Decimal] = None
    period_days: Optional[Decimal] = None


def zz_lines(basic="4000.00"):
    return [earning("BASIC", basic, "EUR", ("ZZ_PENSION_WAGE", "TAXABLE_INCOME"))]


def india_ctx(**changes):
    fields = dict(currency="INR", period_start=dt.date(2026, 10, 1), period_end=dt.date(2026, 10, 31),
                  settings=dict(IN_ON), pack=packs.pack_snapshot(in_fixture.PACK), tax_profile=None,
                  employee={"branch_subdivision": "IN-MH", "gender": "MALE"},
                  ytd={"TAXABLE_INCOME": D("582000.00")})
    fields.update(changes)
    return Ctx(**fields)


def example_a():
    return list(in_fixture._lines("50000.00", "20000.00", "27000.00"))


def by_code(lines):
    return {line.component: line for line in lines if line.statutory}


def codes(issues):
    return [i.code for i in issues]


# --------------------------------------------------------------------------- the gate (I-07, I-15)


def test_ec_sta_02_no_pack_no_statutory_lines_and_blocking_issue():
    lines = zz_lines()
    result, issues = statutory.apply(Ctx(pack=None), lines)
    assert result == lines
    (issue,) = issues
    assert issue.code == "STATUTORY_NOT_VERIFIED" and issue.severity == "BLOCKING"
    assert issue.details["reason"] == "NO_PACK" and "compliance not verified" in issue.message


def test_i15_active_attestation_turns_the_issue_into_a_warning():
    for attestation in (True, {"status": "ACTIVE", "id": 7}, {"active": True}):
        _, issues = statutory.apply(Ctx(pack=None, attestation=attestation), zz_lines())
        assert [(i.code, i.severity) for i in issues] == [("STATUTORY_NOT_VERIFIED", "WARNING")]
        assert issues[0].details["attested"] is True
    _, issues = statutory.apply(Ctx(pack=None, attestation={"status": "REVOKED"}), zz_lines())
    assert issues[0].severity == "BLOCKING"


def test_ec_sta_01_draft_pack_in_a_regular_run_gives_no_lines():
    ctx = Ctx(currency="USD", pack=packs.pack_snapshot(packs.find_pack("US")), environment="production",
              settings={"payroll.statutory.US.fica.enabled": True})
    lines = [earning("BASIC", "10000.00", "USD", ("US_FICA_WAGES",))]
    result, issues = statutory.apply(ctx, lines)
    assert result == lines
    assert [(i.code, i.details["reason"]) for i in issues] == [("STATUTORY_NOT_VERIFIED", "PACK_NOT_VERIFIED")]


def test_draft_pack_in_a_preview_is_labelled_not_verified():
    ctx = Ctx(currency="USD", pack=packs.pack_snapshot(packs.find_pack("US")), environment="production", preview=True,
              settings={"payroll.statutory.US.fica.enabled": True}, period_start=dt.date(2026, 10, 1),
              period_end=dt.date(2026, 10, 31))
    result, issues = statutory.apply(ctx, [earning("BASIC", "10000.00", "USD", ("US_FICA_WAGES",))])
    lines = by_code(result)
    assert lines["US_OASDI_EE"].amount == D("620.00")
    assert all("NOT_VERIFIED" in l.tags and l.explanation.startswith("Compliance not verified") and not l.fixture
               and l.trace["verified"] is False for l in lines.values())
    assert "STATUTORY_NOT_VERIFIED" in codes(issues)


def test_fixture_pack_refused_in_production_and_staging():
    for environment in ("production", "staging", "qa"):
        lines = example_a()
        result, issues = statutory.apply(india_ctx(environment=environment), lines)
        assert result == lines
        assert [(i.code, i.details["reason"]) for i in issues] == [("STATUTORY_NOT_VERIFIED",
                                                                     "PACK_FIXTURE_IN_PRODUCTION")]


def test_fixture_pack_in_test_produces_badged_lines_example_a():
    result, issues = statutory.apply(india_ctx(), example_a())
    lines = by_code(result)
    assert {c: str(l.amount) for c, l in lines.items()} == {
        "IN_EPF_EE": "3000.00", "IN_EPF_ER": "3000.00", "IN_EDLI_ER": "125.00", "IN_EPF_ADMIN_ER": "125.00",
        "IN_PT": "200.00"}
    assert all(l.fixture and l.statutory and "FIXTURE" in l.tags and l.explanation.startswith("FIXTURE — not law")
               for l in lines.values())
    assert [(i.code, i.severity) for i in issues] == [("STATUTORY_FIXTURE", "FLAG"), ("PACK_STATE_DETERMINED", "FLAG")]
    assert issues[1].details["pack_state"]["IN.esi"]["covered"] is False      # 97,000 > 21,000 (EC-STA-17)
    # input lines first, then statutory lines by stage and order
    assert [l.component for l in result][:3] == ["BASIC", "HRA", "FIXED_ALLOWANCE"]


def test_zz_verified_pack_gives_final_lines():
    result, issues = statutory.apply(Ctx(), zz_lines())
    lines = by_code(result)
    assert {c: str(l.amount) for c, l in lines.items()} == {
        "ZZ_HEALTH_LEVY": "40.00", "ZZ_PENSION_EE": "200.00", "ZZ_PENSION_ER": "280.00", "ZZ_WHT": "752.00"}
    assert not issues
    assert all(not l.fixture and l.trace["verified"] is True and l.trace["pack_version"] == "ZZ-2026.1"
               for l in lines.values())
    assert [l.stage for l in result[1:]] == [5, 6, 6, 7]


def test_zz_refused_outside_the_test_environment():
    _, issues = statutory.apply(Ctx(environment="development"), zz_lines())
    assert issues[0].details["reason"] == "PACK_TEST_ONLY"


def test_disabled_pack_blocks_even_with_attestation():
    ctx = Ctx(pack={**packs.pack_snapshot(zz_test.PACK), "status": "DISABLED"}, attestation=True)
    result, issues = statutory.apply(ctx, zz_lines())
    assert len(result) == 1 and [(i.code, i.severity) for i in issues] == [("STATUTORY_PACK_DISABLED", "BLOCKING")]


def test_ec_sta_02_period_outside_the_pack_version():
    ctx = india_ctx(period_start=dt.date(2027, 4, 1), period_end=dt.date(2027, 4, 30))
    _, issues = statutory.apply(ctx, example_a())
    assert issues[0].details["reason"] == "PACK_NOT_EFFECTIVE"


def test_unknown_pack_version_is_not_verified():
    _, issues = statutory.apply(Ctx(pack={"code": "ZZ", "version": "ZZ-1999"}), zz_lines())
    assert issues[0].details["reason"] == "PACK_NOT_FOUND"


def test_ec_sta_03_pack_changed_since_the_snapshot():
    ctx = Ctx(pack={**packs.pack_snapshot(zz_test.PACK), "fingerprint": "0" * 64})
    result, issues = statutory.apply(ctx, zz_lines())
    assert len(result) == 1 and codes(issues) == ["PACK_CHANGED"]


def test_ec_sta_04_replay_is_deterministic():
    first = statutory.apply(Ctx(), zz_lines())
    second = statutory.apply(Ctx(), zz_lines())
    assert [l.to_json() for l in first[0]] == [l.to_json() for l in second[0]]


def test_ec_sta_19_pack_of_another_country_never_applies():
    ctx = india_ctx(country="AE")
    result, issues = statutory.apply(ctx, example_a())
    assert not by_code(result) and codes(issues) == ["PACK_COUNTRY_MISMATCH"]
    # IN tags on a Zedland employee produce nothing Indian
    lines = [earning("BASIC", "4000.00", "EUR", ("IN_PF_WAGE", "IN_ESI_WAGE", "ZZ_PENSION_WAGE", "TAXABLE_INCOME"))]
    result, _ = statutory.apply(Ctx(), lines)
    assert not [c for c in by_code(result) if c.startswith("IN_")]


def test_currency_mismatch_blocks():
    _, issues = statutory.apply(Ctx(currency="USD"), [earning("BASIC", "4000.00", "USD", ("ZZ_PENSION_WAGE",))])
    assert codes(issues) == ["STATUTORY_CURRENCY_MISMATCH"]


def test_statutory_stage_runs_once():
    result, _ = statutory.apply(Ctx(), zz_lines())
    again, issues = statutory.apply(Ctx(), result)
    assert again == result and codes(issues) == ["STATUTORY_ALREADY_APPLIED"]


def test_mandatory_scheme_switched_off_is_a_blocking_issue():
    settings = {**ZZ_ON, "payroll.statutory.ZZ.pension.enabled": False}
    result, issues = statutory.apply(Ctx(settings=settings), zz_lines())
    assert "ZZ_PENSION_EE" not in by_code(result)
    off = [i for i in issues if i.code == "STATUTORY_SCHEME_OFF"]
    assert [(i.severity, i.details["scheme"]) for i in off] == [("BLOCKING", "ZZ.pension")]
    # an optional scheme (the levy) may be off
    result, issues = statutory.apply(Ctx(settings={**ZZ_ON, "payroll.statutory.ZZ.levy.enabled": False}), zz_lines())
    assert not issues and "ZZ_HEALTH_LEVY" not in by_code(result)


def test_malformed_context_values_fail_closed():
    result, issues = statutory.apply(Ctx(paid_days=15.5, period_days=D("31")), zz_lines())
    assert len(result) == 1 and codes(issues) == ["STATUTORY_CALCULATION_ERROR"]
    result, issues = statutory.apply(Ctx(ytd={"ZZ_PENSION_WAGE": 47000.0}), zz_lines())
    assert len(result) == 1 and codes(issues) == ["STATUTORY_CALCULATION_ERROR"]


def test_pack_errors_become_a_blocking_issue(monkeypatch):
    def broken(self, lines, ctx):
        raise ArithmeticError("division impossible")

    monkeypatch.setattr(type(zz_test.PACK), "statutory_bases", broken)
    result, issues = statutory.apply(Ctx(), zz_lines())
    assert len(result) == 1 and codes(issues)[-1] == "STATUTORY_CALCULATION_ERROR"


# --------------------------------------------------------------------------- what the lines carry


def test_ec_sta_13_statutory_rounding_wins_over_company_rounding():
    for mode in ("UP", "DOWN", "HALF_EVEN"):
        result, _ = statutory.apply(Ctx(settings={**ZZ_ON, "payroll.rounding.line_mode": mode}), zz_lines("4321.10"))
        lines = by_code(result)
        assert lines["ZZ_PENSION_EE"].amount == D("216.00")       # 216.055 to the nearest whole unit
        assert lines["ZZ_PENSION_ER"].amount == D("302.00")       # 302.477
        assert lines["ZZ_PENSION_EE"].trace["rounding"] == {"stage": "STATUTORY", "rule": "NEAREST_WHOLE_UNIT",
                                                            "mode": "HALF_UP", "increment": "1"}
        assert lines["ZZ_PENSION_EE"].trace["unrounded"] == "216.055"


def test_ec_sta_10_separate_portions_and_liabilities():
    result = statutory.evaluate(Ctx(), zz_lines())
    found = {(l.component, l.portion, str(l.amount), l.due_date) for l in result.liabilities}
    assert found == {
        ("ZZ_HEALTH_LEVY", "EMPLOYEE", "40.00", dt.date(2026, 4, 30)),
        ("ZZ_PENSION_EE", "EMPLOYEE", "200.00", dt.date(2026, 4, 15)),
        ("ZZ_PENSION_ER", "EMPLOYER", "280.00", dt.date(2026, 4, 15)),
        ("ZZ_WHT", "EMPLOYEE", "752.00", dt.date(2026, 4, 10)),
    }
    ee, er = by_code(result.lines)["ZZ_PENSION_EE"], by_code(result.lines)["ZZ_PENSION_ER"]
    assert ee.kind == "DEDUCTION" and er.kind == "EMPLOYER_CONTRIBUTION"


def test_ec_sta_16_ytd_ceiling_through_the_stage():
    ctx = Ctx(period_start=dt.date(2026, 11, 1), period_end=dt.date(2026, 11, 30),
              ytd={"ZZ_PENSION_WAGE": D("47000.00")})
    result = statutory.evaluate(ctx, zz_lines("6000.00"))
    assert by_code(result.lines)["ZZ_PENSION_EE"].amount == D("150.00")
    assert result.ytd_increments["ZZ_PENSION_WAGE"] == D("3000.00")
    assert result.ytd_increments["ZZ_WHT"] == D("1162.00")
    capped = statutory.evaluate(Ctx(period_start=dt.date(2026, 12, 1), period_end=dt.date(2026, 12, 31),
                                    ytd={"ZZ_PENSION_WAGE": D("50000.00")}), zz_lines("6000.00"))
    assert "ZZ_PENSION_EE" not in by_code(capped.lines) and "ANNUAL_CEILING_REACHED" in codes(capped.issues)


def test_ec_sta_21_missing_tax_profile_blocks_only_withholding():
    result, issues = statutory.apply(Ctx(tax_profile=None), zz_lines())
    assert "ZZ_WHT" not in by_code(result) and "ZZ_PENSION_EE" in by_code(result)
    assert [(i.code, i.severity) for i in issues] == [("TAX_PROFILE_INCOMPLETE", "BLOCKING")]


def test_ec_sta_15_minimum_wage_breach_is_blocking():
    _, issues = statutory.apply(Ctx(), zz_lines("1200.00"))
    assert ("MINIMUM_WAGE_BREACH", "BLOCKING") in [(i.code, i.severity) for i in issues]
    _, issues = statutory.apply(Ctx(paid_days=D("15"), period_days=D("31")), zz_lines("1200.00"))
    assert "MINIMUM_WAGE_BREACH" not in codes(issues)              # 1,500 × 15 ÷ 31 = 725.81


def test_ec_sta_17_pack_state_is_returned_for_persistence():
    lines = list(in_fixture._lines("15000.00", "5000.00"))
    result = statutory.evaluate(india_ctx(employee={"branch_subdivision": "IN-KA"}), lines)
    assert result.pack_state["IN.esi"]["covered"] is True
    assert by_code(result.lines)["IN_ESI_EE"].amount == D("150.00")
    assert result.taxable == D("20000.00")


def test_ec_sta_17_known_determination_raises_no_new_flag():
    state = {"IN.esi": {"contribution_period": "2026-10-01..2027-03-31", "covered": False}}
    _, issues = statutory.apply(india_ctx(pack_state=state), example_a())
    assert codes(issues) == ["STATUTORY_FIXTURE"]
    _, issues = statutory.apply(Ctx(), zz_lines())
    assert "PACK_STATE_DETERMINED" not in codes(issues)


def test_ec_sta_20_contractor_gets_no_statutory_lines_and_a_review_issue():
    lines = zz_lines()
    result, issues = statutory.apply(Ctx(employee={"worker_class": "CONTRACTOR"}), lines)
    assert result == lines
    (issue,) = issues
    assert (issue.code, issue.severity, issue.waivable) == ("WORKER_CLASS_REVIEW", "BLOCKING", True)
    for worker_class in ("EMPLOYEE", "INTERN", "APPRENTICE"):
        result, issues = statutory.apply(Ctx(employee={"worker_class": worker_class}), zz_lines())
        assert "ZZ_PENSION_EE" in by_code(result) and "WORKER_CLASS_REVIEW" not in codes(issues)


def test_ec_sta_09_several_tax_jurisdictions_require_specialist_review():
    profile = {"resident": True, "tax_jurisdictions": ["IN", "AE"]}
    result, issues = statutory.apply(Ctx(tax_profile=profile), zz_lines())
    assert "ZZ_WHT" in by_code(result)                       # withholding still follows the pack
    assert [(i.code, i.severity) for i in issues] == [("TAX_SPECIALIST_REVIEW", "WARNING")]
    assert "Requires specialist review" in issues[0].message
    for single in (["US-CA", "US-NY"], ["IN"], "IN", []):
        _, issues = statutory.apply(Ctx(tax_profile={"resident": True, "tax_jurisdictions": single}), zz_lines())
        assert not issues, single
    _, issues = statutory.apply(Ctx(tax_profile={"resident": True, "tax_jurisdictions": ["IN", "QQ"]}), zz_lines())
    assert codes(issues) == ["TAX_SPECIALIST_REVIEW"]        # unrecognised: reviewed, never guessed
    # residency comes from the declaration, never from citizenship
    result, _ = statutory.apply(Ctx(tax_profile={"resident": False}, employee={"nationality": "ZZ"}), zz_lines())
    assert by_code(result)["ZZ_WHT"].trace["details"]["resident"] is False


def test_frequency_not_supported_by_the_pack_blocks():
    _, issues = statutory.apply(Ctx(frequency="WEEKLY"), zz_lines())
    assert codes(issues) == ["PACK_FREQUENCY_UNSUPPORTED"]


# --------------------------------------------------------------------------- the real engine context


def _snapshot(**overrides):
    start, end = dt.date(2026, 10, 1), dt.date(2026, 10, 31)
    days, day = [], start
    while day <= end:
        days.append({"date": day.isoformat(), "weekly_off": day.weekday() == 6, "segment": "S1", "employed": True})
        day += dt.timedelta(days=1)
    data = {
        "run": {"run_id": 1, "run_employee_id": 11, "environment": "test"},
        "employee": {"employee_id": 501, "hire_date": "2022-04-01", "country": "IN", "subdivision": "IN-MH",
                     "protected": {"gender": "MALE"}},
        "period": {"start": start.isoformat(), "end": end.isoformat(), "pay_date": end.isoformat(),
                   "number_in_tax_year": 7, "tax_year_start": "2026-04-01"},
        "currency": "INR",
        "settings": {k: v for k, v in IN_ON.items()},
        "segments": [{"key": "S1", "start": start.isoformat(), "end": end.isoformat(), "compensation": None}],
        "days": days,
        "ytd": {"TAXABLE_INCOME": "582000.00"},
        "pack": packs.pack_snapshot(in_fixture.PACK),
    }
    data.update(overrides)
    return data


def test_engine_calc_context_feeds_the_pack():
    context = pytest.importorskip("app.payroll.engine.context")
    from app.payroll.engine.snapshot import Snapshot

    ctx = context.build_context(Snapshot.from_dict(_snapshot()))
    result, issues = statutory.apply(ctx, example_a())
    lines = by_code(result)
    assert lines["IN_EPF_EE"].amount == D("3000.00") and lines["IN_PT"].amount == D("200.00")
    assert "IN_TDS" not in lines and codes(issues) == ["STATUTORY_FIXTURE", "PACK_STATE_DETERMINED"]
    production = context.build_context(Snapshot.from_dict(_snapshot(run={"run_id": 1, "environment": "production"})))
    result, issues = statutory.apply(production, example_a())
    assert not by_code(result) and issues[0].details["reason"] == "PACK_FIXTURE_IN_PRODUCTION"
    attested = context.build_context(Snapshot.from_dict(_snapshot(pack=None, attestation={"status": "ACTIVE"})))
    _, issues = statutory.apply(attested, example_a())
    assert [(i.code, i.severity) for i in issues] == [("STATUTORY_NOT_VERIFIED", "WARNING")]


def test_ctx_get_reads_attributes_mappings_and_period():
    assert statutory.ctx_get({"currency": "INR"}, "currency") == "INR"
    period = type("P", (), {"start": dt.date(2026, 1, 1), "end": dt.date(2026, 1, 31)})()
    holder = type("C", (), {"period": period})()
    assert statutory.ctx_get(holder, "period_end") == dt.date(2026, 1, 31)
    assert statutory.ctx_get(holder, "missing", 5) == 5


def test_lines_keep_the_shared_line_contract():
    result, _ = statutory.apply(Ctx(), zz_lines())
    for line in result[1:]:
        payload = line.to_json()
        assert payload["statutory"] is True and payload["fixture"] is False
        assert payload["amount"] == str(line.amount) and isinstance(line, Line) and line.stage > STAGE_EARNINGS
