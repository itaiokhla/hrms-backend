"""AE — United Arab Emirates, DRAFT pack (3.2 §9.2). Not verified: CONFIG_ONLY in production.

Draft data with sources, for previews and the review that a qualified reviewer must still do
(decision I-07). The registry refuses this version outside previews, so its calculations never
produce finalizable lines:

* no personal income tax on employment income (no withholding stage);
* GPSSA pension for UAE nationals on the ``AE_GPSSA_WAGE`` base: joined on/after 31 Oct 2023
  employee 11%, employer 15% (12.5% when the salary is below AED 20,000: the government pays 2.5%);
  earlier joiners employee 5%, employer 12.5% (government 2.5%). The maximum contribution salary is
  not verified, so no cap is applied and a flag is raised;
* end-of-service gratuity (Federal Decree-Law 33/2021): ≥ 1 year of service; 21 days' basic wage
  per year for years 1–5, 30 days after; pro rata; at most two years' wage; unpaid absence excluded;
* WPS (Ministerial Resolution 340/2026): wages due by the 1st of the next month, ≥ 85% on time.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.payroll.engine.lines import STAGE_EARNINGS, STAGE_STATUTORY, Line
from app.payroll.money import CALC_CONTEXT, quantize_amount
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

CURRENCY = "AED"
GPSSA_WAGE = "AE_GPSSA_WAGE"
_RETRIEVED = date(2026, 10, 20)
_NEXT_REVIEW = date(2027, 1, 20)
NEW_SCHEME_FROM = date(2023, 10, 31)

GPSSA = Scheme("AE.gpssa", "GPSSA pension (UAE nationals)", True, "Federal law on pensions and social security (GPSSA)",
               ("AE_GPSSA_EE", "AE_GPSSA_ER"), note="Abu Dhabi nationals use ADPF (not in this draft).")
EOSG = Scheme("AE.eosg", "End-of-service gratuity", True, "Federal Decree-Law 33/2021", ("AE_EOSG",))

GPSSA_TABLE = EffectiveTable("AE GPSSA", (
    RuleRow(date(2023, 10, 31), None, {
        "new_employee_rate": Decimal("11"), "new_employer_rate": Decimal("15"),
        "legacy_employee_rate": Decimal("5"), "legacy_employer_rate": Decimal("12.5"),
        "government_rate": Decimal("2.5"), "government_threshold": Decimal("20000")}, "AE.gpssa.rates"),
))
EOSG_TABLE = EffectiveTable("AE end-of-service gratuity", (
    RuleRow(date(2022, 2, 2), None, {
        "days_first_five_years": Decimal("21"), "days_after_five_years": Decimal("30"),
        "cap_months": Decimal("24"), "minimum_years": Decimal("1"), "payable_within_days": Decimal("14")},
        "AE.eosg.rules"),
))
WPS_TABLE = EffectiveTable("AE wage protection system", (
    RuleRow(date(2026, 6, 1), None, {"due_day_next_month": Decimal("1"), "on_time_share": Decimal("85")}, "AE.wps"),
))


def _src(rule: str, title: str, url: str, publisher: str, kind: str, effective: date, vectors: Tuple[str, ...],
         *, confidence: str = "HIGH", note: str = "") -> SourceEntry:
    return SourceEntry(rule=rule, title=title, url=url, publisher=publisher, kind=kind, retrieved=_RETRIEVED,
                       effective_from=effective, test_vectors=vectors, next_review_due=_NEXT_REVIEW,
                       confidence=confidence, note=note or "Draft value from 3.2 §9.2; not reviewed.")


MANIFEST = SourceManifest((
    _src("AE.gpssa.rates", "GPSSA distribution of contributions",
         "https://gpssa.gov.ae/pages/en/help/faq/what-distribution-contributions-due-insured-individuals",
         "General Pension and Social Security Authority", "PRIMARY", NEW_SCHEME_FROM, ("AE-V01", "AE-V02", "AE-V03"),
         note="Maximum contribution salary not verified."),
    _src("AE.eosg.rules", "Federal Decree-Law 33/2021 (employment relationship)",
         "https://www.mohre.gov.ae/assets/download/e82f7872/Federal%20Decree-Law%20No.%2033%20of%202021%20Regarding"
         "%20the%20Regulation%20of%20Employment%20Relationship%20and%20its%20amendments_638990571068264034.pdf.aspx",
         "Ministry of Human Resources and Emiratisation", "PRIMARY", date(2022, 2, 2), ("AE-V04",)),
    _src("AE.eosg.rules", "End-of-service benefits (private sector)",
         "https://u.ae/en/information-and-services/jobs/Sector-of-employment/employment-in-the-private-sector/"
         "end-of-service-benefits-for-employees-in-the-private-sector", "UAE Government portal", "PRIMARY",
         date(2022, 2, 2), ("AE-V04",)),
    _src("AE.wps", "UAE enhanced Wage Protection System from 1 June 2026 (MR 340/2026)",
         "https://taxnews.ey.com/news/2026-1119-uae-introduces-enhanced-wage-protection-system-effective-1-june-2026",
         "EY", "SECONDARY", date(2026, 6, 1), ("AE-V01",), confidence="MEDIUM",
         note="Replaced MR 598/2022; the primary resolution text must be checked before review."),
))

_GPSSA = "General Pension and Social Security Authority"
COMPONENTS: Tuple[ComponentDef, ...] = (
    ComponentDef("AE_GPSSA_EE", "GPSSA pension (employee)", "DEDUCTION", "EMPLOYEE_CONTRIBUTION", STAGE_STATUTORY,
                 GPSSA.code, portion="EMPLOYEE", payee=_GPSSA, sort_order=600),
    ComponentDef("AE_GPSSA_ER", "GPSSA pension (employer)", "EMPLOYER_CONTRIBUTION", "EMPLOYER_CONTRIBUTION",
                 STAGE_STATUTORY, GPSSA.code, portion="EMPLOYER", payee=_GPSSA, sort_order=610),
)


def end_of_service_gratuity(last_basic_monthly: Decimal, service_start: date, service_end: date, *,
                            unpaid_absence_days: int = 0) -> Tuple[Decimal, str]:
    """Draft gratuity: 21 days' basic per year for years 1–5, 30 after, pro rata, at most 24 months' basic;
    nothing below one year. No reduction on resignation. Daily basic = monthly × 12 ÷ 365."""
    row = EOSG_TABLE.on(service_end)
    if row is None:
        raise ValueError(f"no gratuity rule on {service_end}")
    with localcontext(CALC_CONTEXT):
        days = Decimal((service_end - service_start).days + 1 - unpaid_absence_days)
        years = days / Decimal(365)
        if years < row["minimum_years"]:
            return ZERO, f"service {fmt(years, 4)} years is below {fmt(row['minimum_years'], 0)} year: no gratuity"
        daily = last_basic_monthly * 12 / Decimal(365)
        first = min(years, Decimal(5)) * row["days_first_five_years"]
        after = max(ZERO, years - 5) * row["days_after_five_years"]
        value = (first + after) * daily
        cap = last_basic_monthly * row["cap_months"]
        text = (f"({fmt(min(years, Decimal(5)), 4)} × {fmt(row['days_first_five_years'], 0)} + "
                f"{fmt(max(ZERO, years - 5), 4)} × {fmt(row['days_after_five_years'], 0)}) days × daily basic {fmt(daily)}")
        if value > cap:
            value, text = cap, f"{text}, capped at {fmt(row['cap_months'], 0)} months' basic {fmt(cap)}"
    return quantize_amount(value, CURRENCY), text


class UaeDraftPack(BasePack):
    meta = PackVersion(
        code="AE", version="AE-2026.10-DRAFT", country="AE", name="United Arab Emirates (draft)", currency=CURRENCY,
        effective_from=date(2026, 6, 1), effective_to=None, status=PackVersionStatus.DRAFT,
        capability=Capability.CONFIG_ONLY, tax_year_start="01-01",
        notes="Draft data of 3.2 §9.2 (research of 20 Oct 2026); awaiting a qualified reviewer.",
    )
    manifest = MANIFEST

    def schemes(self) -> List[Scheme]:
        return [GPSSA, EOSG]

    def components(self) -> List[ComponentDef]:
        return list(COMPONENTS)

    def employer_choices(self) -> List[SettingDef]:
        return [
            choice_switch(GPSSA, on="GPSSA contributions are calculated for UAE nationals.", off="No GPSSA contributions."),
            choice_switch(EOSG, on="End-of-service gratuity is calculated in final settlements.",
                          off="No gratuity calculation."),
        ]

    def setting_constraints(self) -> List[LegalConstraint]:
        return [
            LegalConstraint(GPSSA.switch_key, GPSSA.legal_source, required_value=True, applies_to="UAE nationals"),
            LegalConstraint(EOSG.switch_key, EOSG.legal_source, required_value=True),
        ]

    def reports(self) -> List[ReportDef]:
        return [ReportDef("AE_WPS_SIF", "WPS salary information file", "MONTHLY",
                          note="Record order follows the paying bank's specification (3.7 §7).")]

    def payment_rules(self) -> PaymentRules:
        return PaymentRules(
            pay_deadline="A month's wages are due by the 1st of the next month; compliant when ≥ 85% are paid on time "
                         "(Ministerial Resolution 340/2026, draft)",
            mandated_system="Wage Protection System (MOHRE)", bank_file_formats=("AE_WPS_SIF",),
            final_settlement_deadline="Gratuity payable within 14 days (Federal Decree-Law 33/2021, draft)",
        )

    def ytd_keys(self) -> Tuple[str, ...]:
        return ("AE_GPSSA_EE", "AE_GPSSA_ER")

    def rule_data(self) -> Dict[str, Any]:
        return {"gpssa": GPSSA_TABLE.to_json(), "eosg": EOSG_TABLE.to_json(), "wps": WPS_TABLE.to_json()}

    def test_vectors(self) -> Tuple[TestVector, ...]:
        return _vectors()

    def statutory_bases(self, lines: Sequence[Line], ctx: PackContext) -> Dict[str, Decimal]:
        return {GPSSA_WAGE: tagged(lines, GPSSA_WAGE)}

    def _gpssa(self, bases: Mapping[str, Decimal], ctx: PackContext) -> Optional[Dict[str, Any]]:
        if not ctx.scheme_on(GPSSA) or ctx.employee.get("nationality") != "AE":
            return None
        wage = bases.get(GPSSA_WAGE, ZERO)
        row = GPSSA_TABLE.on(ctx.period_end)
        if row is None or wage <= 0:
            return None
        joined = ctx.employee.get("date_of_joining")
        new = isinstance(joined, date) and joined >= NEW_SCHEME_FROM
        employer = row["new_employer_rate"] if new else row["legacy_employer_rate"]
        government = new and wage < row["government_threshold"]
        if government:
            employer -= row["government_rate"]
        return {"wage": wage, "employee": row["new_employee_rate"] if new else row["legacy_employee_rate"],
                "employer": employer, "government": government, "scheme": "NEW" if new else "LEGACY"}

    def employee_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        info = self._gpssa(bases, ctx)
        if info is None:
            return []
        ctx.issue("AE_GPSSA_CAP_NOT_VERIFIED", "FLAG", "The GPSSA maximum contribution salary is not verified; "
                  "no cap was applied.", component="AE_GPSSA_EE")
        with localcontext(CALC_CONTEXT):
            value = info["wage"] * info["employee"] / HUNDRED
        return [self.line("AE_GPSSA_EE", value, ctx, f"{pct(info['employee'])} × contribution salary {fmt(info['wage'])} "
                          f"({info['scheme'].lower()} scheme)", base=info["wage"], rate=info["employee"],
                          rule="AE.gpssa.rates", details={"scheme": info["scheme"]})]

    def employer_contributions(self, bases: Mapping[str, Decimal], ctx: PackContext) -> List[Line]:
        info = self._gpssa(bases, ctx)
        if info is None:
            return []
        with localcontext(CALC_CONTEXT):
            value = info["wage"] * info["employer"] / HUNDRED
        note = " (the government pays 2.5%)" if info["government"] else ""
        return [self.line("AE_GPSSA_ER", value, ctx, f"{pct(info['employer'])} × contribution salary "
                          f"{fmt(info['wage'])}{note}", base=info["wage"], rate=info["employer"], rule="AE.gpssa.rates",
                          details={"scheme": info["scheme"], "government_share": info["government"]})]

    def termination_entitlements(self, separation: Mapping[str, Any], ctx: PackContext) -> List[Line]:
        if not ctx.scheme_on(EOSG):
            return []
        basic = Decimal(str(separation["last_basic_monthly"]))
        value, text = end_of_service_gratuity(basic, separation["service_start"], separation["service_end"],
                                              unpaid_absence_days=int(separation.get("unpaid_absence_days", 0)))
        if value <= 0:
            return []
        return [Line(component="AE_EOSG", kind="EARNING", category="GRATUITY", stage=STAGE_EARNINGS, amount=value,
                     currency=ctx.currency, tags=("AE_EOSG",), statutory=True, explanation=text,
                     trace={"method": "PACK", "pack": self.code, "pack_version": self.version, "rule": "AE.eosg.rules"})]


_NATIONAL_NEW = {"nationality": "AE", "date_of_joining": date(2024, 1, 1)}


def _vectors() -> Tuple[TestVector, ...]:
    month = (date(2026, 10, 1), date(2026, 10, 31))
    on = {GPSSA.switch_key: True}

    def wage(amount: str) -> Tuple[Line, ...]:
        return (earning("BASIC", amount, CURRENCY, (GPSSA_WAGE,)),)

    return (
        TestVector("AE-V01", "National, new scheme, AED 30,000: 11% / 15%", *month, wage("30000.00"),
                   {"AE_GPSSA_EE": "3300.00", "AE_GPSSA_ER": "4500.00"}, settings=on, employee=_NATIONAL_NEW,
                   expected_issues=("AE_GPSSA_CAP_NOT_VERIFIED",), rules=("AE.gpssa.rates", "AE.wps")),
        TestVector("AE-V02", "National, new scheme, AED 15,000: employer 12.5% (government 2.5%)", *month,
                   wage("15000.00"), {"AE_GPSSA_EE": "1650.00", "AE_GPSSA_ER": "1875.00"}, settings=on,
                   employee=_NATIONAL_NEW),
        TestVector("AE-V03", "National, legacy scheme: 5% / 12.5%", *month, wage("30000.00"),
                   {"AE_GPSSA_EE": "1500.00", "AE_GPSSA_ER": "3750.00"}, settings=on,
                   employee={"nationality": "AE", "date_of_joining": date(2020, 1, 1)}),
        TestVector("AE-V04", "Expatriate: no GPSSA", *month, wage("30000.00"),
                   {"AE_GPSSA_EE": "0", "AE_GPSSA_ER": "0"}, settings=on,
                   employee={"nationality": "IN", "date_of_joining": date(2024, 1, 1)}, rules=("AE.eosg.rules",)),
    )


PACK = UaeDraftPack()
