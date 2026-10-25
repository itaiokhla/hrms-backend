"""ZZ — "Zedland", a fictional country for tests only (decision I-07).

Nothing here is law. The pack is ``VERIFIED_FOR_PERIOD`` so that tests can prove the verified
statutory path end to end; the registry refuses it outside the test environment. Its rules are
chosen to exercise every statutory mechanism with hand-computable numbers:

    stage 5  ZZ_HEALTH_LEVY   1% of gross cash earnings, at most 40.00, pre-tax, whole units
    stage 6  ZZ_PENSION_EE    5% of the ZZ_PENSION_WAGE base, ZZ_PENSION_ER 7% (separate liabilities)
             base = min(wage, monthly ceiling, annual ceiling − YTD base)        (EC-STA-16)
             ceiling 5,000.00 to 30 Jun 2026, 6,000.00 from 1 Jul 2026, PERIOD_END split rule (EC-STA-05)
             annual ceiling 50,000.00 per tax year; whole-unit rounding           (EC-STA-13)
    stage 7  ZZ_WHT           flat-rate bands on taxable income (TAXABLE_INCOME − levy − pension EE):
             ≤ 1,000 0%, ≤ 3,000 10%, ≤ 6,000 20%, above 30%; non-residents 25%; minor-unit rounding
    checks   minimum wage 1,500.00 per month (prorated by paid days), protected net floor 50%
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.payroll.engine.lines import STAGE_PRE_TAX, STAGE_STATUTORY, STAGE_TAX, CalcIssue, Line
from app.payroll.money import CALC_CONTEXT
from app.payroll.packs.base import (
    HUNDRED,
    ZERO,
    Band,
    BandTable,
    BasePack,
    Capability,
    ComponentDef,
    DueRule,
    EffectiveTable,
    PackContext,
    PackVersion,
    PaymentRules,
    ReportDef,
    RuleRow,
    Scheme,
    SourceEntry,
    SourceManifest,
    TaxProfileField,
    TestVector,
    choice_switch,
    earning,
    fmt,
    gross_cash,
    pct,
    tagged,
)
from app.payroll.settings_registry import LegalConstraint, SettingDef
from app.payroll.state_machines import PackVersionStatus

CURRENCY = "EUR"
PENSION_WAGE = "ZZ_PENSION_WAGE"
_RETRIEVED = date(2026, 1, 1)
_REVIEW_DUE = date(2027, 1, 1)
_ACT = "Zedland Pension Act 2020 (fictional)"
_WAGES_ACT = "Zedland Wages Act 2019 (fictional)"
_TAX_ACT = "Zedland Income Tax Act 2021 (fictional)"

PENSION = Scheme("ZZ.pension", "Zedland pension", True, f"{_ACT} s.4", ("ZZ_PENSION_EE", "ZZ_PENSION_ER"))
LEVY = Scheme("ZZ.levy", "Zedland health levy", False, "Zedland Health Levy Order 2022 (fictional)",
              ("ZZ_HEALTH_LEVY",))
WHT = Scheme("ZZ.wht", "Zedland wage withholding", True, f"{_TAX_ACT} s.30", ("ZZ_WHT",))

PENSION_TABLE = EffectiveTable("ZZ pension", (
    RuleRow(date(2026, 1, 1), date(2026, 6, 30), {
        "employee_rate": Decimal("5"), "employer_rate": Decimal("7"),
        "monthly_ceiling": Decimal("5000.00"), "annual_ceiling": Decimal("50000.00")}, "ZZ.pension.rates"),
    RuleRow(date(2026, 7, 1), None, {
        "employee_rate": Decimal("5"), "employer_rate": Decimal("7"),
        "monthly_ceiling": Decimal("6000.00"), "annual_ceiling": Decimal("50000.00")}, "ZZ.pension.ceiling"),
))
LEVY_TABLE = EffectiveTable("ZZ health levy", (
    RuleRow(date(2026, 1, 1), None, {"rate": Decimal("1"), "cap": Decimal("40.00")}, "ZZ.levy.rate"),
))
WHT_BANDS = BandTable("ZZ withholding", "FLAT_RATE", (
    Band(Decimal("1000.00"), Decimal("0")),
    Band(Decimal("3000.00"), Decimal("10")),
    Band(Decimal("6000.00"), Decimal("20")),
    Band(None, Decimal("30")),
))
WHT_TABLE = EffectiveTable("ZZ withholding", (
    RuleRow(date(2026, 1, 1), None, {"bands": WHT_BANDS, "non_resident_rate": Decimal("25")}, "ZZ.wht.bands"),
))
MIN_WAGE_TABLE = EffectiveTable("ZZ minimum wage", (
    RuleRow(date(2026, 1, 1), None, {"monthly": Decimal("1500.00")}, "ZZ.min_wage"),
))
SPLIT_RULE = "PERIOD_END"
FLOOR_PERCENT = Decimal("50")


def _source(rule: str, title: str, vectors: Tuple[str, ...], effective: date = date(2026, 1, 1)) -> SourceEntry:
    return SourceEntry(
        rule=rule, title=title, url="https://example.invalid/zedland", publisher="Zedland Gazette (fictional)",
        kind="FICTIONAL", retrieved=_RETRIEVED, effective_from=effective, test_vectors=vectors,
        next_review_due=_REVIEW_DUE, reviewer="Test fixture reviewer", reviewed_on=_RETRIEVED,
        approval="TEST-ONLY", note="Fictional rule for tests; not law anywhere.",
    )


MANIFEST = SourceManifest((
    _source("ZZ.pension.rates", f"{_ACT} s.4–s.6", ("ZZ-V01", "ZZ-V03")),
    _source("ZZ.pension.ceiling", f"{_ACT} Schedule 1 (ceiling from 1 Jul 2026)", ("ZZ-V02", "ZZ-V06"),
            date(2026, 7, 1)),
    _source("ZZ.pension.annual_ceiling", f"{_ACT} s.7 (annual ceiling)", ("ZZ-V04", "ZZ-V05")),
    _source("ZZ.levy.rate", "Zedland Health Levy Order 2022 art.2", ("ZZ-V01",)),
    _source("ZZ.wht.bands", f"{_TAX_ACT} Schedule 2", ("ZZ-V01", "ZZ-V02", "ZZ-V07")),
    _source("ZZ.wht.profile", f"{_TAX_ACT} s.31 (residency declaration)", ("ZZ-V08",)),
    _source("ZZ.min_wage", f"{_WAGES_ACT} s.3", ("ZZ-V09",)),
    _source("ZZ.protected_net", f"{_WAGES_ACT} s.12", ("ZZ-V01",)),
    _source("ZZ.late_penalty", f"{_WAGES_ACT} s.9", ("ZZ-V01",)),
))

_FUND = "Zedland Pension Fund (fictional)"
_REVENUE = "Zedland Revenue Authority (fictional)"
COMPONENTS: Tuple[ComponentDef, ...] = (
    ComponentDef("ZZ_HEALTH_LEVY", "Health levy", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", STAGE_PRE_TAX, LEVY.code,
                 rounding="NEAREST_WHOLE_UNIT", portion="EMPLOYEE", payee="Zedland Health Fund (fictional)",
                 due=DueRule("LAST_DAY_OF_NEXT_MONTH"), sort_order=500, reduces_taxable=True),
    ComponentDef("ZZ_PENSION_EE", "Pension (employee)", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", STAGE_STATUTORY,
                 PENSION.code, rounding="NEAREST_WHOLE_UNIT", portion="EMPLOYEE", payee=_FUND,
                 due=DueRule("DAY_OF_NEXT_MONTH", 15), sort_order=600, reduces_taxable=True),
    ComponentDef("ZZ_PENSION_ER", "Pension (employer)", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION",
                 STAGE_STATUTORY, PENSION.code, rounding="NEAREST_WHOLE_UNIT", portion="EMPLOYER", payee=_FUND,
                 due=DueRule("DAY_OF_NEXT_MONTH", 15), sort_order=610),
    ComponentDef("ZZ_WHT", "Wage withholding", "DEDUCTION", "TAX", STAGE_TAX, WHT.code, portion="EMPLOYEE",
                 payee=_REVENUE, due=DueRule("DAY_OF_NEXT_MONTH", 10), sort_order=700),
)


class ZedlandPack(BasePack):
    meta = PackVersion(
        code="ZZ", version="ZZ-2026.1", country="ZZ", name="Zedland (fictional test pack)", currency=CURRENCY,
        effective_from=date(2026, 1, 1), effective_to=None, status=PackVersionStatus.VERIFIED_FOR_PERIOD,
        capability=Capability.VERIFIED, tax_year_start="01-01", test_only=True,
        reviewer="Test fixture reviewer", reviewed_on=date(2026, 1, 1),
        notes="Fictional pack used only by tests to exercise the verified statutory path.",
    )
    manifest = MANIFEST
    floor_percent = FLOOR_PERCENT

    # -- declarations
    def schemes(self) -> List[Scheme]:
        return [PENSION, LEVY, WHT]

    def components(self) -> List[ComponentDef]:
        return list(COMPONENTS)

    def employer_choices(self) -> List[SettingDef]:
        return [
            choice_switch(PENSION, on="Pension contributions are calculated.", off="Not calculated (never lawful in Zedland)."),
            choice_switch(LEVY, on="The health levy is deducted before tax.", off="No health levy."),
            choice_switch(WHT, on="Wage withholding is calculated.", off="Not calculated."),
        ]

    def setting_constraints(self) -> List[LegalConstraint]:
        return [
            LegalConstraint(PENSION.switch_key, PENSION.legal_source, required_value=True),
            LegalConstraint(WHT.switch_key, WHT.legal_source, required_value=True),
            LegalConstraint("payroll.deductions.min_net_pay_percent", f"{_WAGES_ACT} s.12", minimum=FLOOR_PERCENT),
            LegalConstraint("payroll.late_penalty.enabled", f"{_WAGES_ACT} s.9", required_value=False),
        ]

    def reports(self) -> List[ReportDef]:
        return [
            ReportDef("ZZ_PENSION_RETURN", "Monthly pension return", "MONTHLY", DueRule("DAY_OF_NEXT_MONTH", 15)),
            ReportDef("ZZ_WHT_RETURN", "Monthly withholding return", "MONTHLY", DueRule("DAY_OF_NEXT_MONTH", 10)),
        ]

    def payment_rules(self) -> PaymentRules:
        return PaymentRules(pay_deadline=f"Wages are due by the 7th of the next month ({_WAGES_ACT} s.5)",
                            bank_file_formats=("GENERIC_CSV",))

    def ytd_keys(self) -> Tuple[str, ...]:
        return (PENSION_WAGE, "ZZ_PENSION_EE", "ZZ_PENSION_ER", "ZZ_WHT")

    def rule_data(self) -> Dict[str, Any]:
        return {"pension": PENSION_TABLE.to_json(), "levy": LEVY_TABLE.to_json(), "wht": WHT_TABLE.to_json(),
                "min_wage": MIN_WAGE_TABLE.to_json(), "split_rule": SPLIT_RULE, "floor_percent": str(FLOOR_PERCENT)}

    # -- stages
    def validate_context(self, ctx: PackContext) -> List[CalcIssue]:
        if ctx.frequency != "MONTHLY":
            return [CalcIssue("PACK_FREQUENCY_UNSUPPORTED", "BLOCKING",
                              f"The ZZ pack has monthly ceilings only; {ctx.frequency} payroll is not supported.")]
        return []

    def pre_tax_deductions(self, lines: Sequence[Line], ctx: PackContext) -> List[Line]:
        if not ctx.scheme_on(LEVY):
            return []
        row = LEVY_TABLE.on(ctx.period_end)
        gross = gross_cash(lines)
        if row is None or gross <= 0:
            return []
        with localcontext(CALC_CONTEXT):
            raw = gross * row["rate"] / HUNDRED
            value = min(raw, row["cap"])
        capped = " (capped)" if raw > row["cap"] else ""
        return [self.line("ZZ_HEALTH_LEVY", value, ctx,
                          f"{pct(row['rate'])} × gross {fmt(gross)}, at most {fmt(row['cap'])}{capped}",
                          base=gross, rate=row["rate"], rule="ZZ.levy.rate")]

    def statutory_bases(self, lines: Sequence[Line], ctx: PackContext) -> Dict[str, Decimal]:
        return {PENSION_WAGE: tagged(lines, PENSION_WAGE), "GROSS": gross_cash(lines)}

    def _pension_base(self, bases: Mapping[str, Decimal], ctx: PackContext) -> Tuple[Decimal, RuleRow, str]:
        segment = PENSION_TABLE.segments(ctx.period_start, ctx.period_end, SPLIT_RULE)[0]
        row = segment.row
        wage = bases.get(PENSION_WAGE, ZERO)
        ytd = ctx.ytd_amount(PENSION_WAGE)
        with localcontext(CALC_CONTEXT):
            room = max(ZERO, row["annual_ceiling"] - ytd)
            base = max(ZERO, min(wage, row["monthly_ceiling"], room))
        text = (f"min(pension wage {fmt(wage)}, ceiling {fmt(row['monthly_ceiling'])}, "
                f"annual ceiling {fmt(row['annual_ceiling'])} − YTD {fmt(ytd)})")
        return base, row, text

    def employee_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        if not ctx.scheme_on(PENSION):
            return []
        base, row, text = self._pension_base(bases, ctx)
        ytd = ctx.ytd_amount(PENSION_WAGE)
        ctx.scratch["ZZ.pension"] = {"base": str(base), "ytd_base_after": str(ytd + base)}
        if base <= 0:
            if bases.get(PENSION_WAGE, ZERO) > 0:
                ctx.issue("ANNUAL_CEILING_REACHED", "FLAG",
                          f"Pension stops: the YTD base {fmt(ytd)} has reached the annual ceiling "
                          f"{fmt(row['annual_ceiling'])}.", component="ZZ_PENSION_EE")
            return []
        with localcontext(CALC_CONTEXT):
            value = base * row["employee_rate"] / HUNDRED
        return [self.line("ZZ_PENSION_EE", value, ctx, f"{pct(row['employee_rate'])} × {text} = {fmt(base)}",
                          base=base, rate=row["employee_rate"], rule=row.source, ytd_base_key=PENSION_WAGE,
                          details={"split_rule": SPLIT_RULE, "ytd_base_before": ytd})]

    def employer_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        if not ctx.scheme_on(PENSION):
            return []
        base, row, text = self._pension_base(bases, ctx)
        if base <= 0:
            return []
        with localcontext(CALC_CONTEXT):
            value = base * row["employer_rate"] / HUNDRED
        return [self.line("ZZ_PENSION_ER", value, ctx, f"{pct(row['employer_rate'])} × {text} = {fmt(base)}",
                          base=base, rate=row["employer_rate"], rule=row.source,
                          details={"split_rule": SPLIT_RULE})]

    def tax_profile_schema(self) -> Tuple[TaxProfileField, ...]:
        """Fictional declaration of the fictional pack: the one fact its withholding needs (ZZ.wht.profile)."""
        return (
            TaxProfileField("resident", "Tax resident of Zedland", "BOOL", required=True, group="Residency",
                            note="Fictional test pack: not law.",
                            help="Never inferred from citizenship (EC-STA-09)."),
            TaxProfileField("dependants", "Number of dependants", "INTEGER", group="Dependants",
                            minimum=Decimal(0), maximum=Decimal(20), proof_required=True,
                            note="Fictional test pack: not law."),
        )

    def withholding(self, taxable: Decimal, ytd: Mapping[str, Any], profile: Optional[Mapping[str, Any]],
                    ctx: PackContext) -> List[Line]:
        if not ctx.scheme_on(WHT):
            return []
        resident = None if profile is None else profile.get("resident")
        if not isinstance(resident, bool):
            ctx.issue("TAX_PROFILE_INCOMPLETE", "BLOCKING",
                      "Wage withholding needs the employee's residency declaration (tax profile field 'resident').",
                      component="ZZ_WHT", missing=["resident"])
            return []
        if taxable <= 0:
            return []
        row = WHT_TABLE.on(ctx.period_end)
        if row is None:
            ctx.issue("PACK_RULE_MISSING", "BLOCKING", "No ZZ withholding table covers the period.", component="ZZ_WHT")
            return []
        if resident:
            result = row["bands"].apply(taxable)
            rate = row["bands"].bands[result.band].rate
            value, text = result.value, result.steps[0]
        else:
            rate = row["non_resident_rate"]
            with localcontext(CALC_CONTEXT):
                value = taxable * rate / HUNDRED
            text = f"non-resident flat {pct(rate)} × {fmt(taxable)}"
        return [self.line("ZZ_WHT", value, ctx, f"{text} on taxable income {fmt(taxable)}", base=taxable,
                          rate=rate, rule="ZZ.wht.bands", details={"resident": resident})]

    def minimum_pay_checks(self, ledger: Any, lines: Sequence[Line], ctx: PackContext) -> List[CalcIssue]:
        row = MIN_WAGE_TABLE.on(ctx.period_end)
        if row is None or ctx.employee.get("worker_class", "EMPLOYEE") != "EMPLOYEE":
            return []
        with localcontext(CALC_CONTEXT):
            minimum = row["monthly"]
            if ctx.paid_days is not None and ctx.period_days:
                minimum = minimum * Decimal(ctx.paid_days) / Decimal(ctx.period_days)
            gross = gross_cash(lines)
        if gross < minimum:
            return [CalcIssue("MINIMUM_WAGE_BREACH", "BLOCKING",
                              f"Gross {fmt(gross)} is below the Zedland minimum wage {fmt(minimum)} for the period "
                              f"({_WAGES_ACT} s.3).", {"gross": str(gross), "minimum": str(minimum)})]
        return []

    # -- golden vectors (hand-computed)
    def test_vectors(self) -> Tuple[TestVector, ...]:
        return _vectors()


_ALL_ON = {PENSION.switch_key: True, LEVY.switch_key: True, WHT.switch_key: True}
_RESIDENT = {"resident": True}


def _basic(amount: str) -> Line:
    return earning("BASIC", amount, CURRENCY, (PENSION_WAGE, "TAXABLE_INCOME", "LOP_APPLICABLE"))


def _vectors() -> Tuple[TestVector, ...]:
    march = (date(2026, 3, 1), date(2026, 3, 31))
    november = (date(2026, 11, 1), date(2026, 11, 30))
    bonus = earning("BONUS", "1000.50", CURRENCY, ("TAXABLE_INCOME",), category="BONUS")
    return (
        TestVector("ZZ-V01", "Below the ceiling: levy capped, 5%/7% pension, 20% band", *march,
                   (_basic("4000.00"), bonus),
                   {"ZZ_HEALTH_LEVY": "40.00", "ZZ_PENSION_EE": "200.00", "ZZ_PENSION_ER": "280.00",
                    "ZZ_WHT": "952.10"},
                   settings=_ALL_ON, tax_profile=_RESIDENT, rules=("ZZ.pension.rates", "ZZ.levy.rate", "ZZ.wht.bands"),
                   ec_ids=("EC-STA-10",)),
        TestVector("ZZ-V02", "Above the monthly ceiling of 5,000.00; 30% band", *march, (_basic("8000.40"),),
                   {"ZZ_HEALTH_LEVY": "40.00", "ZZ_PENSION_EE": "250.00", "ZZ_PENSION_ER": "350.00",
                    "ZZ_WHT": "2313.12"},
                   settings=_ALL_ON, tax_profile=_RESIDENT, rules=("ZZ.pension.rates",)),
        TestVector("ZZ-V03", "Whole-unit statutory rounding regardless of company rounding", *march,
                   (_basic("4321.10"),),
                   {"ZZ_HEALTH_LEVY": "40.00", "ZZ_PENSION_EE": "216.00", "ZZ_PENSION_ER": "302.00",
                    "ZZ_WHT": "813.02"},
                   settings={**_ALL_ON, "payroll.rounding.line_mode": "UP"}, tax_profile=_RESIDENT,
                   ec_ids=("EC-STA-13",)),
        TestVector("ZZ-V04", "Annual ceiling: only 3,000.00 of base left", *november, (_basic("6000.00"),),
                   {"ZZ_HEALTH_LEVY": "40.00", "ZZ_PENSION_EE": "150.00", "ZZ_PENSION_ER": "210.00",
                    "ZZ_WHT": "1162.00"},
                   settings=_ALL_ON, tax_profile=_RESIDENT, ytd={PENSION_WAGE: "47000.00"}, ec_ids=("EC-STA-16",)),
        TestVector("ZZ-V05", "Annual ceiling reached: contributions stop", *november, (_basic("6000.00"),),
                   {"ZZ_HEALTH_LEVY": "40.00", "ZZ_PENSION_EE": "0", "ZZ_PENSION_ER": "0", "ZZ_WHT": "1192.00"},
                   settings=_ALL_ON, tax_profile=_RESIDENT, ytd={PENSION_WAGE: "50000.00"},
                   expected_issues=("ANNUAL_CEILING_REACHED",), ec_ids=("EC-STA-16",)),
        TestVector("ZZ-V06", "Ceiling change inside the period: PERIOD_END rule takes 6,000.00",
                   date(2026, 6, 15), date(2026, 7, 14), (_basic("5500.00"),),
                   {"ZZ_PENSION_EE": "275.00", "ZZ_PENSION_ER": "385.00", "ZZ_WHT": "1045.00"},
                   settings={PENSION.switch_key: True, WHT.switch_key: True}, tax_profile=_RESIDENT,
                   ec_ids=("EC-STA-05",)),
        TestVector("ZZ-V07", "Non-resident flat 25%", *march, (_basic("2000.00"),),
                   {"ZZ_PENSION_EE": "100.00", "ZZ_PENSION_ER": "140.00", "ZZ_WHT": "475.00"},
                   settings={PENSION.switch_key: True, WHT.switch_key: True}, tax_profile={"resident": False}),
        TestVector("ZZ-V08", "Missing residency declaration: no withholding, blocking issue", *march,
                   (_basic("2000.00"),), {"ZZ_PENSION_EE": "100.00", "ZZ_WHT": "0"},
                   settings={PENSION.switch_key: True, WHT.switch_key: True}, tax_profile=None,
                   expected_issues=("TAX_PROFILE_INCOMPLETE",), ec_ids=("EC-STA-21",)),
        TestVector("ZZ-V09", "Pay below the minimum wage: blocking", *march, (_basic("1200.00"),),
                   {"ZZ_PENSION_EE": "60.00", "ZZ_WHT": "114.00"},
                   settings={PENSION.switch_key: True, WHT.switch_key: True}, tax_profile=_RESIDENT,
                   expected_issues=("MINIMUM_WAGE_BREACH",), ec_ids=("EC-STA-15",)),
    )


PACK = ZedlandPack()
