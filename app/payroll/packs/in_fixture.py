"""IN — India FIXTURE pack: the illustrative values of 3.2 §9.1 as test data. FIXTURE — not law.

``is_fixture`` is set, so the version can never be verified and runs only in development and
test (decision I-07; contract K:791 lets the Alpha test company use fixture PF/ESI/TDS/PT). Every
line it produces is badged "FIXTURE — not law". Values come only from the 3.2 §9.1 table (research
of 20 October 2026, sources in ``MANIFEST``); where the table is silent the fixture is silent too:

* EPF 12% employee / 12% employer (EPS 8.33% capped, the rest EPF), EDLI 0.50%, admin 0.50%, on
  the ``IN_PF_WAGE`` base with the wage ceiling ₹15,000 → ₹25,000 on 17 Sep 2026 (EPS cap
  ₹1,250 → ₹2,083). A period that spans the change is split by calendar days (BY_DAYS, EC-STA-05):
  each part takes its share of the wage and of the ceiling. Contributions round to whole rupees.
* ESI 0.75% / 3.25% on ``IN_ESI_WAGE`` when covered: coverage up to ₹21,000 a month (₹25,000 for
  persons with disabilities), tested on wages without overtime, fixed for the contribution period
  (1 Apr–30 Sep, 1 Oct–31 Mar; EC-STA-17); employee share waived when the average daily wage is
  ≤ ₹176. Contributions round up to the next rupee.
* Professional tax per branch subdivision on the period end (EC-STA-14): Maharashtra (gendered
  slabs) and Karnataka, ₹300 in February, ₹2,500 a year at most (YTD ``IN_PT``).
* Income-tax withholding (new regime only): annual projection = YTD taxable + previous employer
  income + this period + recurring pay × remaining periods; standard deduction ₹75,000; slabs;
  rebate up to ₹60,000 at ≤ ₹12L with marginal relief; spread over the remaining periods, whole rupees.

Not in the table, so not in the fixture: cess and surcharge, the old regime, due dates, EPS
exclusions other than age 58, the ₹500 admin minimum (establishment level), labour welfare fund.
The labour-code wage test (50% rule) is reported for legal review, never applied silently.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.payroll.engine.lines import STAGE_STATUTORY, STAGE_TAX, Line
from app.payroll.money import CALC_CONTEXT
from app.payroll.packs.base import (
    HUNDRED,
    ZERO,
    Band,
    BandTable,
    BasePack,
    Capability,
    ComponentDef,
    EffectiveTable,
    PackContext,
    PackVersion,
    PaymentRules,
    ReportDef,
    RuleRow,
    Scheme,
    SourceEntry,
    SourceManifest,
    TestVector,
    choice_enum,
    choice_flag,
    choice_switch,
    earning,
    fmt,
    gross_cash,
    pct,
    tagged,
)
from app.payroll.settings_registry import Level, LegalConstraint, SettingDef
from app.payroll.state_machines import PackVersionStatus

CURRENCY = "INR"
PF_WAGE, ESI_WAGE = "IN_PF_WAGE", "IN_ESI_WAGE"
CODE_INCLUDED, CODE_EXCLUDED = "IN_CODE_WAGES_INCLUDED", "IN_CODE_WAGES_EXCLUDED"
RECURRING_CATEGORIES = ("BASE", "FIXED_ALLOWANCE")
FIXTURE_NOTE = "FIXTURE — not law: "
_RETRIEVED = date(2026, 10, 20)
_NEXT_REVIEW = date(2027, 1, 20)

EPF = Scheme("IN.epf", "Provident fund (EPF/EPS)", False, "Code on Social Security 2020; EPF Schemes 2026",
             ("IN_EPF_EE", "IN_EPF_ER", "IN_EDLI_ER", "IN_EPF_ADMIN_ER"),
             note="Applicability depends on the establishment's registration; locked ON only by a verified pack.")
ESI = Scheme("IN.esi", "Employees' State Insurance", False, "Code on Social Security 2020; ESIC",
             ("IN_ESI_EE", "IN_ESI_ER"))
PT = Scheme("IN.pt", "Professional tax", False, "Constitution Art. 276; state PT Acts", ("IN_PT",))
TDS = Scheme("IN.tds", "Income-tax withholding", True, "Income-tax Act 2025 s.392", ("IN_TDS",))

EPF_TABLE = EffectiveTable("IN EPF", (
    RuleRow(date(2025, 11, 21), date(2026, 9, 16), {
        "employee_rate": Decimal("12"), "employer_rate": Decimal("12"), "eps_rate": Decimal("8.33"),
        "ceiling": Decimal("15000"), "eps_cap": Decimal("1250"), "edli_rate": Decimal("0.50"),
        "admin_rate": Decimal("0.50")}, "IN.epf.rates"),
    RuleRow(date(2026, 9, 17), None, {
        "employee_rate": Decimal("12"), "employer_rate": Decimal("12"), "eps_rate": Decimal("8.33"),
        "ceiling": Decimal("25000"), "eps_cap": Decimal("2083"), "edli_rate": Decimal("0.50"),
        "admin_rate": Decimal("0.50")}, "IN.epf.ceiling"),
))
EPF_SPLIT_RULE = "BY_DAYS"
EPS_MAX_AGE = 58
ESI_TABLE = EffectiveTable("IN ESI", (
    RuleRow(date(2019, 7, 1), None, {
        "employee_rate": Decimal("0.75"), "employer_rate": Decimal("3.25"), "coverage": Decimal("21000"),
        "coverage_disability": Decimal("25000"), "employee_exempt_daily_wage": Decimal("176")}, "IN.esi.rates"),
))


def _pt(bands: Tuple[Band, ...], name: str) -> BandTable:
    return BandTable(name, "FIXED_AMOUNT", bands)


PT_TABLES: Mapping[str, EffectiveTable] = {
    "IN-MH": EffectiveTable("IN-MH professional tax", (
        RuleRow(date(2023, 4, 1), None, {
            "MALE": _pt((Band(Decimal("7500"), amount=ZERO), Band(Decimal("10000"), amount=Decimal("175")),
                         Band(None, amount=Decimal("200"))), "Maharashtra PT (men)"),
            "FEMALE": _pt((Band(Decimal("25000"), amount=ZERO), Band(None, amount=Decimal("200"))),
                          "Maharashtra PT (women)"),
            "gender_neutral_above": Decimal("25000"),
            "february_top": Decimal("300")}, "IN.pt.MH"),
    )),
    "IN-KA": EffectiveTable("IN-KA professional tax", (
        RuleRow(date(2025, 4, 1), None, {
            "ALL": _pt((Band(Decimal("25000"), amount=ZERO, inclusive=False), Band(None, amount=Decimal("200"))),
                       "Karnataka PT"),
            "february_top": Decimal("300")}, "IN.pt.KA"),
    )),
}
PT_ANNUAL_CAP = Decimal("2500")
TDS_SLABS = BandTable("IN new regime", "PROGRESSIVE", (
    Band(Decimal("400000"), Decimal("0")), Band(Decimal("800000"), Decimal("5")),
    Band(Decimal("1200000"), Decimal("10")), Band(Decimal("1600000"), Decimal("15")),
    Band(Decimal("2000000"), Decimal("20")), Band(Decimal("2400000"), Decimal("25")),
    Band(None, Decimal("30")),
))
TDS_TABLE = EffectiveTable("IN income tax (new regime)", (
    RuleRow(date(2025, 4, 1), date(2027, 3, 31), {
        "slabs": TDS_SLABS, "standard_deduction": Decimal("75000"), "rebate_limit": Decimal("1200000"),
        "rebate_max": Decimal("60000")}, "IN.tds.slabs"),
))


def _src(rule: str, title: str, url: Optional[str], publisher: str, kind: str, effective: date,
         vectors: Tuple[str, ...], *, confidence: str = "HIGH", note: str = "") -> SourceEntry:
    return SourceEntry(rule=rule, title=title, url=url, publisher=publisher, kind=kind, retrieved=_RETRIEVED,
                       effective_from=effective, test_vectors=vectors, next_review_due=_NEXT_REVIEW,
                       confidence=confidence, note=note or "Illustrative value from 3.2 §9.1; not reviewed.")


MANIFEST = SourceManifest((
    _src("IN.code_wages", "Code on Wages, 2019 — definition of wages (50% rule)",
         "https://www.labour.gov.in/static/uploads/2025/06/c328da14bbb15fc4ad571dc33e7a4ab3.pdf",
         "Ministry of Labour and Employment", "PRIMARY", date(2025, 11, 21), ("IN-V01",),
         note="Mapping company components to clauses (a)-(i) is a legal-review decision."),
    _src("IN.code_wages", "Labour Codes in force (PIB release)",
         "https://www.pib.gov.in/PressReleseDetailm.aspx?PRID=2192463", "Press Information Bureau", "PRIMARY",
         date(2025, 11, 21), ("IN-V01",)),
    _src("IN.epf.rates", "EPF contribution rates", "https://www.epfindia.gov.in/site_docs/PDFs/MiscPDFs/ContributionRate.pdf",
         "Employees' Provident Fund Organisation", "PRIMARY", date(2025, 11, 21), ("IN-V01", "IN-V02", "IN-V03"),
         note="Employee 12%; employer 12% (8.33% EPS + 3.67% EPF); unchanged; Schemes 2026 in force 1 Jul 2026."),
    _src("IN.epf.ceiling", "EPF wage ceiling raised to ₹25,000", "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2310811",
         "Press Information Bureau", "PRIMARY", date(2026, 9, 17), ("IN-V01", "IN-V03"),
         note="September 2026 split 1-16 / 17-30 in one return (EC-STA-05)."),
    _src("IN.epf.ceiling", "KPMG flash alert 2026-249", "https://kpmg.com/xx/en/our-insights/gms-flash-alert/2026/flash-alert-2026-249.html",
         "KPMG", "SECONDARY", date(2026, 9, 17), ("IN-V03",)),
    _src("IN.epf.eps_cap", "EPS cap 8.33% of wages up to the ceiling (₹2,083, was ₹1,250)",
         "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2310811", "Press Information Bureau", "PRIMARY",
         date(2026, 9, 17), ("IN-V01", "IN-V02"), confidence="MEDIUM"),
    _src("IN.epf.edli_admin", "EDLI 0.50% and EPF administration charges 0.50%",
         "https://www.epfindia.gov.in/site_docs/PDFs/MiscPDFs/ContributionRate.pdf",
         "Employees' Provident Fund Organisation", "PRIMARY", date(2018, 6, 1), ("IN-V01",),
         note="Admin minimum ₹500 per establishment per month applies at establishment level, not per employee."),
    _src("IN.esi.rates", "ESI contribution rates", "https://www.esic.gov.in/contribution",
         "Employees' State Insurance Corporation", "PRIMARY", date(2019, 7, 1), ("IN-V04", "IN-V10")),
    _src("IN.esi.coverage", "ESI coverage and contribution periods", "https://web.esic.gov.in/web/esic/coverage",
         "Employees' State Insurance Corporation", "PRIMARY", date(2019, 7, 1), ("IN-V04", "IN-V05", "IN-V06"),
         note="Eligibility fixed for a contribution period (contract §21.3, EC-STA-17)."),
    _src("IN.pt.cap", "Constitution of India Art. 276(2): professional tax at most ₹2,500 a year", None,
         "Government of India", "PRIMARY", date(1988, 5, 31), ("IN-V09",),
         note="No URL was recorded by the research; the source must be added before review."),
    _src("IN.pt.MH", "Maharashtra PT rate schedule (updated to 31.03.2025)",
         "https://www.mahagst.gov.in/public/uploads/menufiles/PT%20Rate%20Schedule%20updated%20upto%2031.03.2025%20(1)%20(1).pdf",
         "Maharashtra GST Department", "PRIMARY", date(2023, 4, 1), ("IN-V01", "IN-V07", "IN-V08", "IN-V09")),
    _src("IN.pt.KA", "Karnataka professional tax FAQ", "https://ptax.karnataka.gov.in/FAQ",
         "Karnataka Professional Tax Department", "PRIMARY", date(2025, 4, 1), ("IN-V04",),
         note="₹300 February rule from 1 Apr 2025."),
    _src("IN.tds.slabs", "Income-tax rates (new regime), s.202", "https://www.incometaxindia.gov.in/w/section-202-76",
         "Income Tax Department", "PRIMARY", date(2025, 4, 1), ("IN-V01", "IN-V11", "IN-V12"),
         note="FY 2025-26 and tax year 2026-27; Budget 2026 no change. Cess and surcharge are not in the fixture."),
    _src("IN.tds.standard_deduction", "Standard deduction (salaried), s.19",
         "https://www.incometaxindia.gov.in/w/section-19-206", "Income Tax Department", "PRIMARY",
         date(2025, 4, 1), ("IN-V11",)),
    _src("IN.tds.rebate", "Rebate up to ₹60,000 at income ≤ ₹12L with marginal relief, s.156",
         "https://www.incometaxindia.gov.in/w/section-156-88", "Income Tax Department", "PRIMARY",
         date(2025, 4, 1), ("IN-V01",)),
    _src("IN.tds.withholding", "Salary withholding, s.392 (was s.192); quarterly return Form 138",
         "https://www.incometaxindia.gov.in/w/section-392-6", "Income Tax Department", "PRIMARY",
         date(2026, 4, 1), ("IN-V11", "IN-V12")),
))

_EPFO = "Employees' Provident Fund Organisation"
_ESIC = "Employees' State Insurance Corporation"
COMPONENTS: Tuple[ComponentDef, ...] = (
    ComponentDef("IN_EPF_EE", "Provident fund (employee)", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", STAGE_STATUTORY,
                 EPF.code, rounding="NEAREST_WHOLE_UNIT", portion="EMPLOYEE", payee=_EPFO, sort_order=600),
    ComponentDef("IN_EPF_ER", "Provident fund (employer)", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION",
                 STAGE_STATUTORY, EPF.code, rounding="NEAREST_WHOLE_UNIT", portion="EMPLOYER", payee=_EPFO,
                 sort_order=610),
    ComponentDef("IN_EDLI_ER", "EDLI (employer)", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION", STAGE_STATUTORY,
                 EPF.code, rounding="NEAREST_WHOLE_UNIT", portion="EMPLOYER", payee=_EPFO, sort_order=612,
                 visibility="HR_ONLY"),
    ComponentDef("IN_EPF_ADMIN_ER", "EPF administration charges", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION",
                 STAGE_STATUTORY, EPF.code, rounding="NEAREST_WHOLE_UNIT", portion="EMPLOYER", payee=_EPFO,
                 sort_order=614, visibility="HR_ONLY"),
    ComponentDef("IN_ESI_EE", "ESI (employee)", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", STAGE_STATUTORY, ESI.code,
                 rounding="UP_TO_WHOLE_UNIT", portion="EMPLOYEE", payee=_ESIC, sort_order=620),
    ComponentDef("IN_ESI_ER", "ESI (employer)", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION", STAGE_STATUTORY,
                 ESI.code, rounding="UP_TO_WHOLE_UNIT", portion="EMPLOYER", payee=_ESIC, sort_order=630),
    ComponentDef("IN_PT", "Professional tax", "DEDUCTION", "TAX", STAGE_STATUTORY, PT.code, portion="EMPLOYEE",
                 payee="State professional tax authority", sort_order=640),
    ComponentDef("IN_TDS", "Income-tax withholding", "DEDUCTION", "TAX", STAGE_TAX, TDS.code,
                 rounding="NEAREST_WHOLE_UNIT", portion="EMPLOYEE", payee="Income Tax Department", sort_order=700),
)


def contribution_period(day: date) -> Tuple[date, date]:
    """The ESI contribution period containing ``day``: 1 Apr–30 Sep or 1 Oct–31 Mar."""
    if 4 <= day.month <= 9:
        return date(day.year, 4, 1), date(day.year, 9, 30)
    year = day.year if day.month >= 10 else day.year - 1
    return date(year, 10, 1), date(year + 1, 3, 31)


def new_regime_tax(taxable: Decimal, on: date) -> Tuple[Decimal, List[str]]:
    """Annual tax on ``taxable`` income (after the standard deduction): slabs, rebate, marginal relief."""
    row = TDS_TABLE.on(on)
    if row is None:
        raise ValueError(f"no income-tax table covers {on}")
    result = row["slabs"].apply(max(ZERO, taxable))
    tax, steps = result.value, list(result.steps)
    with localcontext(CALC_CONTEXT):
        if taxable <= row["rebate_limit"]:
            rebate = min(tax, row["rebate_max"])
            if rebate:
                steps.append(f"rebate {fmt(rebate)} (income ≤ {fmt(row['rebate_limit'])})")
            tax -= rebate
        elif tax > taxable - row["rebate_limit"]:
            relief = tax - (taxable - row["rebate_limit"])
            steps.append(f"marginal relief {fmt(relief)}: tax limited to income above {fmt(row['rebate_limit'])}")
            tax -= relief
    return tax, steps


class IndiaFixturePack(BasePack):
    meta = PackVersion(
        code="IN", version="IN-2026.10-FIXTURE", country="IN", name="India (FIXTURE — not law)", currency=CURRENCY,
        effective_from=date(2025, 11, 21), effective_to=date(2027, 3, 31), status=PackVersionStatus.DRAFT,
        capability=Capability.CALC_SUPPORTED, tax_year_start="04-01", is_fixture=True,
        notes="Illustrative values of 3.2 §9.1 (research of 20 Oct 2026) for non-production test companies only.",
    )
    manifest = MANIFEST

    def schemes(self) -> List[Scheme]:
        return [EPF, ESI, PT, TDS]

    def components(self) -> List[ComponentDef]:
        return list(COMPONENTS)

    def employer_choices(self) -> List[SettingDef]:
        return [
            choice_switch(EPF, on="Provident fund is calculated on the IN_PF_WAGE base.", off="No provident fund."),
            choice_enum("payroll.statutory.IN.epf.wage_basis", EPF, ("STATUTORY_CEILING", "ACTUAL_WAGES"),
                        "STATUTORY_CEILING", "Provident fund wage basis",
                        "STATUTORY_CEILING contributes on wages up to the ceiling; ACTUAL_WAGES on the full PF wage "
                        "(the part above the ceiling is voluntary). The pension (EPS) share stays capped.",
                        levels=(Level.LEGAL_ENTITY, Level.EMPLOYEE)),
            choice_flag("payroll.statutory.IN.epf.employer_share_in_ctc", EPF, "Employer PF inside CTC",
                        on="The employer contribution is part of the CTC (balancing components shrink).",
                        off="The employer contribution is on top of the CTC.", default=True),
            choice_switch(ESI, on="ESI is calculated for covered employees.", off="No ESI."),
            choice_switch(PT, on="Professional tax is deducted by the branch's state slab.", off="No professional tax.",
                          levels=(Level.LEGAL_ENTITY, Level.BRANCH)),
            choice_switch(TDS, on="Income tax is withheld from salary.", off="No income-tax withholding."),
            choice_enum("payroll.statutory.IN.tds.default_regime", TDS, ("NEW", "OLD"), "NEW", "Default tax regime",
                        "Regime used when the employee has not declared one; the employee's declaration overrides it."),
        ]

    def setting_constraints(self) -> List[LegalConstraint]:
        return [LegalConstraint(TDS.switch_key, f"{FIXTURE_NOTE}Income-tax Act 2025 s.392", required_value=True)]

    def test_vectors(self) -> Tuple[TestVector, ...]:
        return _vectors()

    def reports(self) -> List[ReportDef]:
        return [ReportDef("IN_FORM_138", "Quarterly salary withholding return (Form 138)", "QUARTERLY",
                          note="Was Form 24Q; due dates are not in the fixture.")]

    def payment_rules(self) -> PaymentRules:
        return PaymentRules(final_settlement_deadline="Within 2 working days of separation (Code on Wages, 3.2 §9.1)")

    def ytd_keys(self) -> Tuple[str, ...]:
        return ("TAXABLE_INCOME", "IN_TDS", "IN_PT", "IN_EPF_EE", "IN_EPF_ER", "IN_ESI_EE", "IN_ESI_ER")

    def rule_data(self) -> Dict[str, Any]:
        return {"epf": EPF_TABLE.to_json(), "epf_split_rule": EPF_SPLIT_RULE, "eps_max_age": EPS_MAX_AGE,
                "esi": ESI_TABLE.to_json(), "pt": {k: v.to_json() for k, v in sorted(PT_TABLES.items())},
                "pt_annual_cap": str(PT_ANNUAL_CAP), "tds": TDS_TABLE.to_json(),
                "recurring_categories": list(RECURRING_CATEGORIES)}

    # -- stage 6
    def statutory_bases(self, lines: Sequence[Line], ctx: PackContext) -> Dict[str, Decimal]:
        with localcontext(CALC_CONTEXT):
            esi_coverage = sum((line.amount for line in lines if ESI_WAGE in line.tags and line.kind == "EARNING"
                                and line.category != "OVERTIME"), ZERO)
        return {PF_WAGE: tagged(lines, PF_WAGE), ESI_WAGE: tagged(lines, ESI_WAGE), "IN_ESI_COVERAGE_WAGE": esi_coverage,
                "GROSS": gross_cash(lines), CODE_INCLUDED: tagged(lines, CODE_INCLUDED),
                CODE_EXCLUDED: tagged(lines, CODE_EXCLUDED)}

    def _epf(self, bases: Mapping[str, Decimal], ctx: PackContext) -> Dict[str, Any]:
        wage = bases.get(PF_WAGE, ZERO)
        actual = ctx.setting("payroll.statutory.IN.epf.wage_basis", "STATUTORY_CEILING") == "ACTUAL_WAGES"
        segments = EPF_TABLE.segments(ctx.period_start, ctx.period_end, EPF_SPLIT_RULE)
        with localcontext(CALC_CONTEXT):
            capped = ceiling = eps_cap = ZERO
            parts = []
            for seg in segments:
                share, limit = wage * seg.fraction, seg.row["ceiling"] * seg.fraction
                capped += min(share, limit)
                ceiling += limit
                eps_cap += seg.row["eps_cap"] * seg.fraction
                parts.append(f"{seg.start:%d %b}–{seg.end:%d %b}: min({fmt(share)}, {fmt(limit)})")
            row = segments[-1].row
            base = wage if actual else capped
        return {"wage": wage, "base": base, "capped": capped, "ceiling": ceiling, "eps_cap": eps_cap, "row": row,
                "split": len(segments) > 1, "parts": parts, "actual": actual,
                "rule": "IN.epf.ceiling" if any(s.row.source == "IN.epf.ceiling" for s in segments) else "IN.epf.rates"}

    def _epf_text(self, epf: Mapping[str, Any]) -> str:
        if epf["actual"]:
            return f"actual PF wage {fmt(epf['wage'])}"
        if epf["split"]:
            return f"split by days ({'; '.join(epf['parts'])}) = {fmt(epf['base'])}"
        return f"min(PF wage {fmt(epf['wage'])}, ceiling {fmt(epf['ceiling'])})"

    def employee_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        result: List[Line] = []
        if ctx.scheme_on(EPF):
            epf = self._epf(bases, ctx)
            if epf["base"] > 0:
                rate = epf["row"]["employee_rate"]
                with localcontext(CALC_CONTEXT):
                    value = epf["base"] * rate / HUNDRED
                result.append(self.line("IN_EPF_EE", value, ctx, f"{pct(rate)} × {self._epf_text(epf)}",
                                        base=epf["base"], rate=rate, rule=epf["rule"],
                                        details={"split_rule": EPF_SPLIT_RULE, "segments": epf["parts"],
                                                 "wage_basis": "ACTUAL_WAGES" if epf["actual"] else "STATUTORY_CEILING"}))
        if ctx.scheme_on(ESI):
            result.extend(self._esi(bases, ctx, employee=True))
        if ctx.scheme_on(PT):
            result.extend(self._pt(bases, ctx))
        return result

    def employer_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        result: List[Line] = []
        employer_pf = ZERO
        if ctx.scheme_on(EPF):
            epf = self._epf(bases, ctx)
            if epf["base"] > 0:
                row = epf["row"]
                with localcontext(CALC_CONTEXT):
                    total = epf["base"] * row["employer_rate"] / HUNDRED
                    age = _age(ctx.employee.get("date_of_birth"), ctx.period_end)
                    eps_exempt = age is not None and age >= EPS_MAX_AGE
                    eps = ZERO if eps_exempt else min(epf["capped"] * row["eps_rate"] / HUNDRED, epf["eps_cap"])
                    eps_amount = min(_whole(eps), _whole(total))
                line = self.line("IN_EPF_ER", total, ctx,
                                 f"{pct(row['employer_rate'])} × {self._epf_text(epf)} (EPS {fmt(eps_amount)}, "
                                 f"EPF {fmt(_whole(total) - eps_amount)})",
                                 base=epf["base"], rate=row["employer_rate"], rule=epf["rule"],
                                 details={"split_rule": EPF_SPLIT_RULE, "segments": epf["parts"], "eps": eps_amount,
                                          "epf": _whole(total) - eps_amount, "eps_unrounded": eps,
                                          "eps_cap": epf["eps_cap"], "eps_exempt_age_58": eps_exempt})
                result.append(line)
                employer_pf = line.amount
                capped = epf["capped"]
                with localcontext(CALC_CONTEXT):
                    edli = capped * row["edli_rate"] / HUNDRED
                    admin = epf["base"] * row["admin_rate"] / HUNDRED
                result.append(self.line("IN_EDLI_ER", edli, ctx, f"{pct(row['edli_rate'])} × {fmt(capped)}",
                                        base=capped, rate=row["edli_rate"], rule="IN.epf.edli_admin"))
                result.append(self.line("IN_EPF_ADMIN_ER", admin, ctx, f"{pct(row['admin_rate'])} × {fmt(epf['base'])}",
                                        base=epf["base"], rate=row["admin_rate"], rule="IN.epf.edli_admin"))
        if ctx.scheme_on(ESI):
            result.extend(self._esi(bases, ctx, employee=False))
        self._code_wage_test(bases, employer_pf, ctx)
        return result

    def _esi(self, bases: Mapping[str, Decimal], ctx: PackContext, *, employee: bool) -> List[Line]:
        row = ESI_TABLE.on(ctx.period_end)
        wage = bases.get(ESI_WAGE, ZERO)
        if row is None or wage <= 0:
            return []
        start, end = contribution_period(ctx.period_end)
        key = f"{start.isoformat()}..{end.isoformat()}"
        state = ctx.pack_state.get("IN.esi")
        threshold = row["coverage_disability"] if ctx.employee.get("disability") is True else row["coverage"]
        if isinstance(state, Mapping) and state.get("contribution_period") == key:
            covered, basis = state.get("covered") is True, f"eligibility fixed for the contribution period {key}"
        else:
            coverage_wage = bases.get("IN_ESI_COVERAGE_WAGE", ZERO)
            covered = coverage_wage <= threshold
            basis = f"coverage wage {fmt(coverage_wage)} {'≤' if covered else '>'} {fmt(threshold)}"
            ctx.state_out["IN.esi"] = {"contribution_period": key, "covered": covered,
                                       "determined_on": ctx.period_end.isoformat()}
        if not covered:
            return []
        with localcontext(CALC_CONTEXT):
            daily = wage / Decimal(ctx.calendar_days)
        if employee:
            if daily <= row["employee_exempt_daily_wage"]:
                ctx.issue("IN_ESI_EMPLOYEE_SHARE_WAIVED", "FLAG",
                          f"Average daily wage {fmt(daily)} ≤ ₹{fmt(row['employee_exempt_daily_wage'], 0)}: "
                          "no employee ESI share.", component="IN_ESI_EE")
                return []
            rate, code = row["employee_rate"], "IN_ESI_EE"
        else:
            rate, code = row["employer_rate"], "IN_ESI_ER"
        with localcontext(CALC_CONTEXT):
            value = wage * rate / HUNDRED
        return [self.line(code, value, ctx, f"{pct(rate)} × ESI wage {fmt(wage)} ({basis})", base=wage, rate=rate,
                          rule="IN.esi.rates", details={"contribution_period": key, "covered": True})]

    def _pt(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        subdivision = ctx.employee.get("branch_subdivision")
        gross = bases.get("GROSS", ZERO)
        if not subdivision:
            ctx.issue("BRANCH_SUBDIVISION_MISSING", "BLOCKING",
                      "Professional tax needs the state of the employee's branch on the period end.", component="IN_PT")
            return []
        table = PT_TABLES.get(subdivision)
        row = table.on(ctx.period_end) if table else None
        if row is None:
            ctx.issue("PT_NOT_IN_FIXTURE", "FLAG", f"The fixture has no professional-tax slab for {subdivision}; "
                      "none deducted.", component="IN_PT", subdivision=subdivision)
            return []
        if "ALL" in row.values:
            slabs = row["ALL"]
        else:
            gender = ctx.employee.get("gender")
            if gross > row["gender_neutral_above"]:
                slabs = row["MALE"]
            elif gender in ("MALE", "FEMALE"):
                slabs = row[gender]
            else:
                ctx.issue("PT_SLAB_UNDETERMINED", "BLOCKING",
                          f"{subdivision} professional tax below ₹{fmt(row['gender_neutral_above'], 0)} depends on the "
                          "slab the state defines by gender; the employee record does not say which applies.",
                          component="IN_PT")
                return []
        result = slabs.apply(gross)
        amount, text = result.value, result.steps[0]
        if result.band == len(slabs.bands) - 1 and ctx.period_end.month == 2:
            amount, text = row["february_top"], f"{text}; February: {fmt(row['february_top'])}"
        ytd = ctx.ytd_amount("IN_PT")
        with localcontext(CALC_CONTEXT):
            room = max(ZERO, PT_ANNUAL_CAP - ytd)
        if amount > room:
            text += f"; annual cap {fmt(PT_ANNUAL_CAP)} − YTD {fmt(ytd)} leaves {fmt(room)}"
            amount = room
        if amount <= 0:
            return []
        return [self.line("IN_PT", amount, ctx, f"{subdivision}: gross {fmt(gross)}, {text}", base=gross,
                          rule=row.source, details={"subdivision": subdivision, "ytd_before": ytd})]

    def _code_wage_test(self, bases: Mapping[str, Decimal], employer_pf: Decimal, ctx: PackContext) -> None:
        included, excluded = bases.get(CODE_INCLUDED, ZERO), bases.get(CODE_EXCLUDED, ZERO)
        if not included and not excluded:
            return
        with localcontext(CALC_CONTEXT):
            excluded += employer_pf
            total = included + excluded
            excess = excluded - total / 2
        if excess > 0:
            ctx.issue("IN_CODE_WAGES_ADDBACK_REVIEW", "WARNING",
                      f"Excluded items {fmt(excluded)} exceed 50% of total remuneration {fmt(total)} by {fmt(excess)}: "
                      "under the Code on Wages the excess counts as wages. Statutory bases were not changed; "
                      "deciding the clause of each component is a legal-review decision.",
                      included=str(included), excluded=str(excluded), excess=str(excess))

    # -- stage 7
    def withholding(self, taxable: Decimal, ytd: Mapping[str, Any], profile: Optional[Mapping[str, Any]],
                    ctx: PackContext) -> List[Line]:
        if not ctx.scheme_on(TDS):
            return []
        regime = (profile or {}).get("regime") or ctx.setting("payroll.statutory.IN.tds.default_regime", "NEW")
        if regime != "NEW":
            ctx.issue("TAX_REGIME_NOT_SUPPORTED", "BLOCKING",
                      f"The fixture implements the new regime only; regime {regime} is not calculated.", component="IN_TDS")
            return []
        row = TDS_TABLE.on(ctx.period_end)
        if row is None:
            ctx.issue("PACK_RULE_MISSING", "BLOCKING", "No income-tax table covers the period.", component="IN_TDS")
            return []
        remaining = 1 if ctx.final_settlement else ctx.periods_remaining()
        if not remaining or remaining < 1:
            ctx.issue("TAX_PROJECTION_PERIODS_UNKNOWN", "BLOCKING",
                      "The number of remaining periods in the tax year is unknown; withholding is not projected.",
                      component="IN_TDS")
            return []
        previous = (profile or {}).get("previous_employment") or {}
        prev_income = _money(previous.get("income"), "previous_employment.income")
        prev_tax = _money(previous.get("tax"), "previous_employment.tax")
        ytd_taxable, ytd_tds = ctx.ytd_amount("TAXABLE_INCOME"), ctx.ytd_amount("IN_TDS")
        recurring = ZERO if ctx.final_settlement else ctx.scratch.get("IN.recurring_taxable", ZERO)
        with localcontext(CALC_CONTEXT):
            projected = ytd_taxable + prev_income + taxable + recurring * (remaining - 1)
            annual_taxable = max(ZERO, projected - row["standard_deduction"])
            annual_tax, steps = new_regime_tax(annual_taxable, ctx.period_end)
            due = annual_tax - ytd_tds - prev_tax
            per_period = due / remaining
        details = {"period_taxable": taxable, "projected_annual": projected, "standard_deduction": row["standard_deduction"],
                   "annual_taxable": annual_taxable, "annual_tax": annual_tax, "ytd_tds": ytd_tds,
                   "previous_employer_income": prev_income, "previous_employer_tax": prev_tax,
                   "remaining_periods": remaining, "steps": steps, "regime": regime}
        ctx.scratch["IN.tds"] = {k: str(v) if isinstance(v, Decimal) else v for k, v in details.items()}
        if per_period <= 0:
            if due < 0:
                ctx.issue("TAX_OVERWITHHELD", "FLAG", f"Tax withheld so far exceeds the projected annual tax by "
                          f"{fmt(-due)}; nothing withheld this period.", component="IN_TDS")
            return []
        text = (f"projected annual income {fmt(projected)} − standard deduction {fmt(row['standard_deduction'])} "
                f"= {fmt(annual_taxable)}; annual tax {fmt(annual_tax)} − withheld {fmt(ytd_tds + prev_tax)} "
                f"over {remaining} period(s)")
        return [self.line("IN_TDS", per_period, ctx, text, base=taxable, rule="IN.tds.slabs", details=details)]

    def taxable_income(self, lines: Sequence[Line], ctx: PackContext) -> Decimal:
        """Σ TAXABLE_INCOME earnings and benefits in kind (new regime: contributions are not deductible);
        the recurring part (basic and fixed allowances) is projected over the remaining periods."""
        ctx.scratch["IN.recurring_taxable"] = sum(
            (line.amount for line in lines if "TAXABLE_INCOME" in line.tags and line.kind == "EARNING"
             and line.category in RECURRING_CATEGORIES), ZERO)
        return super().taxable_income(lines, ctx)


def _whole(value: Decimal) -> Decimal:
    from app.payroll.money import quantize_amount

    return quantize_amount(value, CURRENCY, "HALF_UP", Decimal(1))


def _age(born: Any, on: date) -> Optional[int]:
    if isinstance(born, str):
        born = date.fromisoformat(born)
    if not isinstance(born, date):
        return None
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def _money(value: Any, name: str) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, bool) or not isinstance(value, (str, Decimal, int)):
        raise ValueError(f"{name} must be a decimal string")
    return Decimal(value)


# --------------------------------------------------------------------------- golden vectors

_ON = {EPF.switch_key: True, ESI.switch_key: True, PT.switch_key: True, TDS.switch_key: True}
_MH_MAN = {"branch_subdivision": "IN-MH", "gender": "MALE"}


def _lines(basic: str, hra: str = "0", allowance: str = "0") -> Tuple[Line, ...]:
    result = [earning("BASIC", basic, CURRENCY, (PF_WAGE, ESI_WAGE, "TAXABLE_INCOME", CODE_INCLUDED, "LOP_APPLICABLE"))]
    if Decimal(hra):
        result.append(earning("HRA", hra, CURRENCY, (ESI_WAGE, "TAXABLE_INCOME", CODE_EXCLUDED), category="FIXED_ALLOWANCE"))
    if Decimal(allowance):
        result.append(earning("FIXED_ALLOWANCE", allowance, CURRENCY, (ESI_WAGE, "TAXABLE_INCOME", CODE_INCLUDED),
                              category="FIXED_ALLOWANCE"))
    return tuple(result)


def _month(year: int, month: int) -> Tuple[date, date]:
    start = date(year, month, 1)
    end = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
    return start, end


def _vectors() -> Tuple[TestVector, ...]:
    example_a = _lines("50000.00", "20000.00", "27000.00")
    esi_state = {"IN.esi": {"contribution_period": "2026-10-01..2027-03-31", "covered": True}}
    pt_only = {PT.switch_key: True}
    return (
        TestVector("IN-V01", "3.3 §11 Example A after 17 Sep 2026: EPF at the ₹25,000 ceiling, PT Maharashtra",
                   *_month(2026, 10), example_a,
                   {"IN_EPF_EE": "3000.00", "IN_EPF_ER": "3000.00", "IN_EDLI_ER": "125.00", "IN_EPF_ADMIN_ER": "125.00",
                    "IN_ESI_EE": "0", "IN_ESI_ER": "0", "IN_PT": "200.00", "IN_TDS": "0"},
                   settings=_ON, employee=_MH_MAN, ytd={"TAXABLE_INCOME": "582000.00", "IN_TDS": "0", "IN_PT": "1200.00"},
                   rules=("IN.epf.ceiling", "IN.epf.eps_cap", "IN.pt.MH", "IN.tds.rebate"), ec_ids=("EC-STA-10",),
                   source="3.3 §11 Example A"),
        TestVector("IN-V02", "Example A before 17 Sep 2026: ₹15,000 ceiling, 1,800.00 each", *_month(2026, 8), example_a,
                   {"IN_EPF_EE": "1800.00", "IN_EPF_ER": "1800.00", "IN_EDLI_ER": "75.00", "IN_EPF_ADMIN_ER": "75.00",
                    "IN_PT": "200.00", "IN_TDS": "0"},
                   settings=_ON, employee=_MH_MAN, ytd={"TAXABLE_INCOME": "388000.00"},
                   rules=("IN.epf.rates",), source="3.3 §11 Example A"),
        TestVector("IN-V03", "September 2026 split 1–16 / 17–30 by days", *_month(2026, 9), example_a,
                   {"IN_EPF_EE": "2360.00", "IN_EPF_ER": "2360.00", "IN_EDLI_ER": "98.00", "IN_EPF_ADMIN_ER": "98.00"},
                   settings={EPF.switch_key: True}, employee=_MH_MAN, ec_ids=("EC-STA-05",)),
        TestVector("IN-V04", "ESI covered at ₹20,000; Karnataka PT below ₹25,000", *_month(2026, 10),
                   _lines("15000.00", "5000.00"),
                   {"IN_EPF_EE": "1800.00", "IN_EPF_ER": "1800.00", "IN_ESI_EE": "150.00", "IN_ESI_ER": "650.00",
                    "IN_PT": "0", "IN_TDS": "0"},
                   settings=_ON, employee={"branch_subdivision": "IN-KA"}, ytd={"TAXABLE_INCOME": "120000.00"}),
        TestVector("IN-V05", "ESI eligibility fixed for the contribution period at ₹23,000", *_month(2026, 11),
                   _lines("18000.00", "5000.00"), {"IN_ESI_EE": "173.00", "IN_ESI_ER": "748.00"},
                   settings={ESI.switch_key: True}, pack_state=esi_state, ec_ids=("EC-STA-17",)),
        TestVector("IN-V06", "ESI not covered at ₹23,000 when the period starts", *_month(2026, 10),
                   _lines("18000.00", "5000.00"), {"IN_ESI_EE": "0", "IN_ESI_ER": "0"},
                   settings={ESI.switch_key: True}, ec_ids=("EC-STA-17",)),
        TestVector("IN-V07", "Maharashtra PT: man ₹9,000 → 175", *_month(2026, 10), _lines("9000.00"),
                   {"IN_PT": "175.00"}, settings=pt_only, employee=_MH_MAN),
        TestVector("IN-V08", "Maharashtra PT: woman ₹20,000 → nil", *_month(2026, 10),
                   _lines("20000.00"), {"IN_PT": "0"}, settings=pt_only,
                   employee={"branch_subdivision": "IN-MH", "gender": "FEMALE"}),
        TestVector("IN-V09", "PT February 300 limited by the ₹2,500 annual cap", *_month(2027, 2), _lines("30000.00"),
                   {"IN_PT": "100.00"}, settings=pt_only, employee=_MH_MAN, ytd={"IN_PT": "2400.00"},
                   rules=("IN.pt.cap",)),
        TestVector("IN-V10", "ESI employee share waived at ₹5,000 (≤ ₹176 a day)", *_month(2026, 10), _lines("5000.00"),
                   {"IN_ESI_EE": "0", "IN_ESI_ER": "163.00"}, settings={ESI.switch_key: True},
                   expected_issues=("IN_ESI_EMPLOYEE_SHARE_WAIVED",)),
        TestVector("IN-V11", "New-regime projection above ₹24L", *_month(2026, 10),
                   (earning("BASIC", "100000.00", CURRENCY, ("TAXABLE_INCOME",)),
                    earning("FIXED_ALLOWANCE", "100000.00", CURRENCY, ("TAXABLE_INCOME",), category="FIXED_ALLOWANCE")),
                   {"IN_TDS": "23438.00"}, settings={TDS.switch_key: True},
                   ytd={"TAXABLE_INCOME": "1200000.00", "IN_TDS": "140625.00"},
                   rules=("IN.tds.slabs", "IN.tds.standard_deduction", "IN.tds.withholding")),
        TestVector("IN-V12", "Mid-year joiner with income from a previous employer", *_month(2026, 10),
                   (earning("BASIC", "100000.00", CURRENCY, ("TAXABLE_INCOME",)),), {"IN_TDS": "11458.00"},
                   settings={TDS.switch_key: True},
                   tax_profile={"regime": "NEW", "previous_employment": {"income": "1000000.00", "tax": "40000.00"}},
                   ec_ids=("EC-STA-07",)),
    )


PACK = IndiaFixturePack()
