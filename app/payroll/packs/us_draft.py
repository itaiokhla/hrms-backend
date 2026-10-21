"""US — United States federal, DRAFT pack (3.2 §9.4). Not verified: CONFIG_ONLY in production.

Draft data with sources for previews and review (decision I-07); the registry refuses it
outside previews. Each state and locality is a separate pack (none exists):

* Social Security (OASDI) 6.2% employee + 6.2% employer up to the wage base $184,500 (2026) and
  $192,300 (2027, announced 14 Oct 2026), applied with YTD wages (EC-STA-16);
* Medicare 1.45% each, no cap; additional 0.9% employee-only on wages above $200,000 YTD;
* FUTA 6.0% on the first $7,000 less the 5.4% credit → 0.6% (credit-reduction states not modelled);
* supplemental wages (bonus, commission) withheld at the 22% flat rate, 37% above $1M YTD.
  Regular federal income-tax withholding (Publication 15-T with Form W-4) is not implemented:
  a blocking issue is raised instead of a guessed amount.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.payroll.engine.lines import STAGE_STATUTORY, STAGE_TAX, Line
from app.payroll.money import CALC_CONTEXT
from app.payroll.packs.base import (
    HUNDRED,
    ZERO,
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
    choice_switch,
    earning,
    fmt,
    pct,
    tagged,
)
from app.payroll.settings_registry import LegalConstraint, SettingDef
from app.payroll.state_machines import PackVersionStatus

CURRENCY = "USD"
FICA_WAGES, FUTA_WAGES, SUPPLEMENTAL = "US_FICA_WAGES", "US_FUTA_WAGES", "US_SUPPLEMENTAL_WAGES"
SUPPLEMENTAL_CATEGORIES = ("BONUS", "COMMISSION")
_RETRIEVED = date(2026, 10, 20)
_NEXT_REVIEW = date(2027, 1, 20)

FICA = Scheme("US.fica", "Social Security and Medicare (FICA)", True, "Federal Insurance Contributions Act",
              ("US_OASDI_EE", "US_OASDI_ER", "US_MEDICARE_EE", "US_MEDICARE_ER", "US_ADDL_MEDICARE_EE"))
FUTA = Scheme("US.futa", "Federal unemployment tax (FUTA)", True, "Federal Unemployment Tax Act", ("US_FUTA_ER",))
FITW = Scheme("US.fitw", "Federal income-tax withholding", True, "IRS Publication 15 / 15-T", ("US_FITW",))

_FICA_ROW = {"oasdi_rate": Decimal("6.2"), "medicare_rate": Decimal("1.45"), "additional_rate": Decimal("0.9"),
             "additional_threshold": Decimal("200000")}
FICA_TABLE = EffectiveTable("US FICA", (
    RuleRow(date(2026, 1, 1), date(2026, 12, 31), {**_FICA_ROW, "oasdi_base": Decimal("184500")}, "US.fica.2026"),
    RuleRow(date(2027, 1, 1), date(2027, 12, 31), {**_FICA_ROW, "oasdi_base": Decimal("192300")}, "US.fica.2027"),
))
FUTA_TABLE = EffectiveTable("US FUTA", (
    RuleRow(date(2026, 1, 1), date(2027, 12, 31), {"rate": Decimal("6.0"), "credit": Decimal("5.4"),
                                                   "base": Decimal("7000")}, "US.futa"),
))
SUPPLEMENTAL_TABLE = EffectiveTable("US supplemental wages", (
    RuleRow(date(2026, 1, 1), date(2027, 12, 31), {"rate": Decimal("22"), "high_rate": Decimal("37"),
                                                   "high_threshold": Decimal("1000000")}, "US.fitw.supplemental"),
))


def _src(rule: str, title: str, url: str, effective: date, vectors: Tuple[str, ...], note: str = "") -> SourceEntry:
    publisher = "Social Security Administration" if "ssa.gov" in url else "Internal Revenue Service"
    return SourceEntry(rule=rule, title=title, url=url, publisher=publisher, kind="PRIMARY", retrieved=_RETRIEVED,
                       effective_from=effective, test_vectors=vectors, next_review_due=_NEXT_REVIEW,
                       note=note or "Draft value from 3.2 §9.4; not reviewed.")


MANIFEST = SourceManifest((
    _src("US.fica.2026", "Contribution and benefit base 2026", "https://www.ssa.gov/oact/cola/cbb.html",
         date(2026, 1, 1), ("US-V01", "US-V02")),
    _src("US.fica.2026", "Publication 15 (Circular E): Social Security and Medicare rates",
         "https://www.irs.gov/publications/p15", date(2026, 1, 1), ("US-V01", "US-V03")),
    _src("US.fica.2027", "Contribution and benefit base 2027 (announced 14 Oct 2026)",
         "https://www.ssa.gov/oact/cola/cbb.html", date(2027, 1, 1), ("US-V04",)),
    _src("US.futa", "Publication 15: FUTA 6.0% on the first $7,000, credit up to 5.4%",
         "https://www.irs.gov/publications/p15", date(2026, 1, 1), ("US-V01",),
         note="Credit-reduction states pay more; not modelled."),
    _src("US.fitw.supplemental", "Publication 15: supplemental wages 22%, mandatory 37% above $1M",
         "https://www.irs.gov/publications/p15", date(2026, 1, 1), ("US-V05",)),
))

_IRS = "Internal Revenue Service"
COMPONENTS: Tuple[ComponentDef, ...] = (
    ComponentDef("US_OASDI_EE", "Social Security (employee)", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", STAGE_STATUTORY,
                 FICA.code, portion="EMPLOYEE", payee=_IRS, sort_order=600),
    ComponentDef("US_OASDI_ER", "Social Security (employer)", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION",
                 STAGE_STATUTORY, FICA.code, portion="EMPLOYER", payee=_IRS, sort_order=610),
    ComponentDef("US_MEDICARE_EE", "Medicare (employee)", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", STAGE_STATUTORY,
                 FICA.code, portion="EMPLOYEE", payee=_IRS, sort_order=620),
    ComponentDef("US_MEDICARE_ER", "Medicare (employer)", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION",
                 STAGE_STATUTORY, FICA.code, portion="EMPLOYER", payee=_IRS, sort_order=630),
    ComponentDef("US_ADDL_MEDICARE_EE", "Additional Medicare (employee)", "DEDUCTION", "EMPLOYEE_CONTRIBUTION",
                 STAGE_STATUTORY, FICA.code, portion="EMPLOYEE", payee=_IRS, sort_order=625),
    ComponentDef("US_FUTA_ER", "Federal unemployment tax", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION",
                 STAGE_STATUTORY, FUTA.code, portion="EMPLOYER", payee=_IRS, sort_order=640),
    ComponentDef("US_FITW", "Federal income tax", "DEDUCTION", "TAX", STAGE_TAX, FITW.code, portion="EMPLOYEE",
                 payee=_IRS, sort_order=700),
)


class UsFederalDraftPack(BasePack):
    meta = PackVersion(
        code="US", version="US-2026.10-DRAFT", country="US", name="United States — federal (draft)", currency=CURRENCY,
        effective_from=date(2026, 1, 1), effective_to=date(2027, 12, 31), status=PackVersionStatus.DRAFT,
        capability=Capability.CONFIG_ONLY, tax_year_start="01-01",
        notes="Draft data of 3.2 §9.4 (research of 20 Oct 2026); state packs do not exist.",
    )
    manifest = MANIFEST

    def schemes(self) -> List[Scheme]:
        return [FICA, FUTA, FITW]

    def components(self) -> List[ComponentDef]:
        return list(COMPONENTS)

    def employer_choices(self) -> List[SettingDef]:
        return [
            choice_switch(FICA, on="Social Security and Medicare are calculated.", off="Not calculated."),
            choice_switch(FUTA, on="FUTA is calculated.", off="Not calculated."),
            choice_switch(FITW, on="Federal income tax is withheld.", off="Not withheld."),
        ]

    def setting_constraints(self) -> List[LegalConstraint]:
        return [LegalConstraint(s.switch_key, s.legal_source, required_value=True) for s in (FICA, FUTA, FITW)]

    def reports(self) -> List[ReportDef]:
        return [ReportDef("US_FORM_941", "Employer's quarterly federal tax return (Form 941)", "QUARTERLY"),
                ReportDef("US_FORM_940", "Employer's annual FUTA return (Form 940)", "ANNUAL"),
                ReportDef("US_FORM_W2", "Wage and tax statement (Form W-2)", "ANNUAL")]

    def payment_rules(self) -> PaymentRules:
        return PaymentRules(bank_file_formats=("GENERIC_CSV",))

    def ytd_keys(self) -> Tuple[str, ...]:
        return (FICA_WAGES, FUTA_WAGES, SUPPLEMENTAL, "US_OASDI_EE", "US_MEDICARE_EE", "US_FITW")

    def rule_data(self) -> Dict[str, Any]:
        return {"fica": FICA_TABLE.to_json(), "futa": FUTA_TABLE.to_json(), "supplemental": SUPPLEMENTAL_TABLE.to_json(),
                "supplemental_categories": list(SUPPLEMENTAL_CATEGORIES)}

    def test_vectors(self) -> Tuple[TestVector, ...]:
        return _vectors()

    def statutory_bases(self, lines: Sequence[Line], ctx: PackContext) -> Dict[str, Decimal]:
        with localcontext(CALC_CONTEXT):
            supplemental = sum((line.amount for line in lines if line.kind == "EARNING"
                                and line.category in SUPPLEMENTAL_CATEGORIES), ZERO)
        return {FICA_WAGES: tagged(lines, FICA_WAGES), FUTA_WAGES: tagged(lines, FUTA_WAGES), SUPPLEMENTAL: supplemental}

    def _fica(self, bases: Mapping[str, Decimal], ctx: PackContext) -> Optional[Dict[str, Decimal]]:
        row = FICA_TABLE.on(ctx.period_end)
        wages = bases.get(FICA_WAGES, ZERO)
        if row is None or wages <= 0:
            return None
        ytd = ctx.ytd_amount(FICA_WAGES)
        with localcontext(CALC_CONTEXT):
            oasdi = max(ZERO, min(wages, row["oasdi_base"] - ytd))
            threshold = row["additional_threshold"]
            additional = max(ZERO, ytd + wages - threshold) - max(ZERO, ytd - threshold)
        return {"wages": wages, "ytd": ytd, "oasdi": oasdi, "additional": additional, "row": row}

    def employee_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        if not ctx.scheme_on(FICA):
            return []
        info = self._fica(bases, ctx)
        if info is None:
            return []
        row, result = info["row"], []
        with localcontext(CALC_CONTEXT):
            oasdi = info["oasdi"] * row["oasdi_rate"] / HUNDRED
            medicare = info["wages"] * row["medicare_rate"] / HUNDRED
            additional = info["additional"] * row["additional_rate"] / HUNDRED
        cap_text = (f"min(wages {fmt(info['wages'])}, wage base {fmt(row['oasdi_base'])} − YTD {fmt(info['ytd'])})")
        if info["oasdi"] > 0:
            result.append(self.line("US_OASDI_EE", oasdi, ctx, f"{pct(row['oasdi_rate'])} × {cap_text}",
                                    base=info["oasdi"], rate=row["oasdi_rate"], rule=row.source))
        else:
            ctx.issue("ANNUAL_CEILING_REACHED", "FLAG", f"YTD wages {fmt(info['ytd'])} reached the Social Security "
                      f"wage base {fmt(row['oasdi_base'])}.", component="US_OASDI_EE")
        result.append(self.line("US_MEDICARE_EE", medicare, ctx, f"{pct(row['medicare_rate'])} × {fmt(info['wages'])}",
                                base=info["wages"], rate=row["medicare_rate"], rule=row.source, ytd_base_key=FICA_WAGES))
        if info["additional"] > 0:
            result.append(self.line("US_ADDL_MEDICARE_EE", additional, ctx,
                                    f"{pct(row['additional_rate'])} × wages above {fmt(row['additional_threshold'])} YTD "
                                    f"({fmt(info['additional'])})", base=info["additional"],
                                    rate=row["additional_rate"], rule=row.source))
        return result

    def employer_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        result: List[Line] = []
        info = self._fica(bases, ctx) if ctx.scheme_on(FICA) else None
        if info is not None:
            row = info["row"]
            with localcontext(CALC_CONTEXT):
                oasdi = info["oasdi"] * row["oasdi_rate"] / HUNDRED
                medicare = info["wages"] * row["medicare_rate"] / HUNDRED
            if info["oasdi"] > 0:
                result.append(self.line("US_OASDI_ER", oasdi, ctx, f"{pct(row['oasdi_rate'])} × {fmt(info['oasdi'])}",
                                        base=info["oasdi"], rate=row["oasdi_rate"], rule=row.source))
            result.append(self.line("US_MEDICARE_ER", medicare, ctx, f"{pct(row['medicare_rate'])} × {fmt(info['wages'])}",
                                    base=info["wages"], rate=row["medicare_rate"], rule=row.source))
        futa = FUTA_TABLE.on(ctx.period_end)
        wages = bases.get(FUTA_WAGES, ZERO)
        if ctx.scheme_on(FUTA) and futa is not None and wages > 0:
            ytd = ctx.ytd_amount(FUTA_WAGES)
            with localcontext(CALC_CONTEXT):
                base = max(ZERO, min(wages, futa["base"] - ytd))
                rate = futa["rate"] - futa["credit"]
                value = base * rate / HUNDRED
            if base > 0:
                result.append(self.line("US_FUTA_ER", value, ctx,
                                        f"({pct(futa['rate'])} − credit {pct(futa['credit'])}) × min(wages {fmt(wages)}, "
                                        f"{fmt(futa['base'])} − YTD {fmt(ytd)})", base=base, rate=rate, rule="US.futa",
                                        ytd_base_key=FUTA_WAGES))
        return result

    def withholding(self, taxable: Decimal, ytd: Mapping[str, Any], profile: Optional[Mapping[str, Any]],
                    ctx: PackContext) -> List[Line]:
        if not ctx.scheme_on(FITW):
            return []
        ctx.issue("US_FIT_NOT_IMPLEMENTED", "BLOCKING", "Regular federal income-tax withholding (Publication 15-T with "
                  "Form W-4) is not implemented; only supplemental wages are withheld.", component="US_FITW")
        supplemental = sum((line.amount for line in ctx.scratch.get("US.supplemental_lines", ())), ZERO)
        row = SUPPLEMENTAL_TABLE.on(ctx.period_end)
        if row is None or supplemental <= 0:
            return []
        prior = ctx.ytd_amount(SUPPLEMENTAL)
        with localcontext(CALC_CONTEXT):
            high = max(ZERO, prior + supplemental - row["high_threshold"]) - max(ZERO, prior - row["high_threshold"])
            value = (supplemental - high) * row["rate"] / HUNDRED + high * row["high_rate"] / HUNDRED
        text = f"{pct(row['rate'])} × supplemental wages {fmt(supplemental - high)}"
        if high:
            text += f" + {pct(row['high_rate'])} × {fmt(high)} above {fmt(row['high_threshold'])} YTD"
        return [self.line("US_FITW", value, ctx, text, base=supplemental, rule="US.fitw.supplemental",
                          ytd_base_key=SUPPLEMENTAL)]

    def taxable_income(self, lines: Sequence[Line], ctx: PackContext) -> Decimal:
        ctx.scratch["US.supplemental_lines"] = [line for line in lines if line.kind == "EARNING"
                                                and line.category in SUPPLEMENTAL_CATEGORIES]
        return super().taxable_income(lines, ctx)


def _vectors() -> Tuple[TestVector, ...]:
    month = (date(2026, 10, 1), date(2026, 10, 31))
    fica = {FICA.switch_key: True, FUTA.switch_key: True}

    def wages(amount: str, category: str = "BASE", code: str = "BASIC") -> Line:
        return earning(code, amount, CURRENCY, (FICA_WAGES, FUTA_WAGES, "TAXABLE_INCOME"), category=category)

    return (
        TestVector("US-V01", "First wages of the year: 6.2% / 1.45% each, FUTA 0.6% of 7,000", *month,
                   (wages("10000.00"),), {"US_OASDI_EE": "620.00", "US_OASDI_ER": "620.00", "US_MEDICARE_EE": "145.00",
                                          "US_MEDICARE_ER": "145.00", "US_FUTA_ER": "42.00"}, settings=fica,
                   rules=("US.fica.2026", "US.futa")),
        TestVector("US-V02", "Social Security wage base reached mid-period (YTD 180,000)", *month, (wages("10000.00"),),
                   {"US_OASDI_EE": "279.00", "US_OASDI_ER": "279.00", "US_MEDICARE_EE": "145.00", "US_FUTA_ER": "0"},
                   settings=fica, ytd={FICA_WAGES: "180000.00", FUTA_WAGES: "7000.00"}, ec_ids=("EC-STA-16",)),
        TestVector("US-V03", "Additional Medicare above 200,000 YTD", *month, (wages("10000.00"),),
                   {"US_ADDL_MEDICARE_EE": "45.00", "US_OASDI_EE": "0"}, settings=fica,
                   ytd={FICA_WAGES: "195000.00", FUTA_WAGES: "7000.00"}, expected_issues=("ANNUAL_CEILING_REACHED",),
                   ec_ids=("EC-STA-16",)),
        TestVector("US-V04", "2027 wage base 192,300: 7,010.00 of base left", date(2027, 11, 1), date(2027, 11, 30),
                   (wages("10000.00"),), {"US_OASDI_EE": "434.62"}, settings=fica,
                   ytd={FICA_WAGES: "185290.00", FUTA_WAGES: "7000.00"}, ec_ids=("EC-STA-16",)),
        TestVector("US-V05", "Supplemental bonus at 22%; regular withholding not implemented", *month,
                   (wages("5000.00"), wages("2000.00", "BONUS", "BONUS")), {"US_FITW": "440.00"},
                   settings={FITW.switch_key: True}, expected_issues=("US_FIT_NOT_IMPLEMENTED",)),
    )


PACK = UsFederalDraftPack()
