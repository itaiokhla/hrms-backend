"""Country-pack framework primitives (3.2 §6): versions, capability, rule tables, bands, sources, rounding."""

from datetime import date
from decimal import Decimal

import pytest

from app.payroll.engine.lines import STAGE_EARNINGS, STAGE_STATUTORY, Line
from app.payroll.packs.base import (
    Band,
    BandTable,
    Capability,
    ComponentDef,
    DueRule,
    EffectiveTable,
    PackContext,
    PackError,
    PackVersion,
    RuleRow,
    SourceEntry,
    SourceManifest,
    canonical_hash,
    liabilities,
    round_statutory_amount,
    tagged,
    with_labels,
)
from app.payroll.state_machines import PackVersionStatus

D = Decimal


def version(**changes):
    fields = dict(code="QQ", version="QQ-1", country="QQ", name="Q", currency="EUR", effective_from=date(2026, 1, 1),
                  effective_to=None, status=PackVersionStatus.DRAFT, capability=Capability.CONFIG_ONLY)
    fields.update(changes)
    return PackVersion(**fields)


# --------------------------------------------------------------------------- versions and capability


def test_capability_levels_are_ordered():
    order = [c.rank for c in (Capability.UNSUPPORTED, Capability.DISPLAY_ONLY, Capability.CONFIG_ONLY,
                              Capability.CALC_SUPPORTED, Capability.VERIFIED, Capability.FILING_INTEGRATED)]
    assert order == sorted(order) and len(set(order)) == 6
    assert "Compliance not verified" in Capability.CONFIG_ONLY.label


def test_fixture_version_can_never_be_verified_i07():
    with pytest.raises(PackError) as error:
        version(is_fixture=True, status=PackVersionStatus.VERIFIED_FOR_PERIOD, reviewer="R", reviewed_on=date(2026, 1, 1))
    assert error.value.code == "FIXTURE_NOT_VERIFIABLE"
    with pytest.raises(PackError):
        version(is_fixture=True, status=PackVersionStatus.EXPIRED, reviewer="R", reviewed_on=date(2026, 1, 1))


def test_verified_version_needs_reviewer_sign_off():
    with pytest.raises(PackError) as error:
        version(status=PackVersionStatus.VERIFIED_FOR_PERIOD)
    assert error.value.code == "REVIEW_MISSING"
    assert version(status=PackVersionStatus.VERIFIED_FOR_PERIOD, reviewer="R", reviewed_on=date(2026, 1, 2)).covers(
        date(2030, 1, 1))


def test_version_dates_and_tax_year_are_validated():
    with pytest.raises(PackError):
        version(effective_to=date(2025, 12, 31))
    with pytest.raises(PackError):
        version(tax_year_start="13-01")
    v = version(effective_to=date(2026, 12, 31))
    assert v.covers_period(date(2026, 3, 1), date(2026, 3, 31))
    assert not v.covers_period(date(2026, 12, 16), date(2027, 1, 15))
    assert not v.covers(date(2025, 12, 31))


def test_pack_context_tax_year_and_remaining_periods():
    ctx = PackContext(version=version(tax_year_start="04-01"), currency="INR", period_start=date(2026, 10, 1),
                      period_end=date(2026, 10, 31))
    assert ctx.tax_year_bounds() == (date(2026, 4, 1), date(2027, 3, 31))
    assert ctx.periods_remaining() == 6
    jan = PackContext(version=version(tax_year_start="04-01"), currency="INR", period_start=date(2027, 1, 1),
                      period_end=date(2027, 1, 31))
    assert jan.tax_year_bounds() == (date(2026, 4, 1), date(2027, 3, 31)) and jan.periods_remaining() == 3
    weekly = PackContext(version=version(), currency="EUR", period_start=date(2026, 3, 2), period_end=date(2026, 3, 8),
                         frequency="WEEKLY")
    assert weekly.periods_remaining() is None          # unknown: never guessed
    assert PackContext(version=version(), currency="EUR", period_start=date(2026, 3, 2), period_end=date(2026, 3, 8),
                       frequency="WEEKLY", tax_periods_remaining=43).periods_remaining() == 43


def test_pack_context_ytd_rejects_floats():
    ctx = PackContext(version=version(), currency="EUR", period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
                      ytd={"A": "10.50", "B": 1.5})
    assert ctx.ytd_amount("A") == D("10.50") and ctx.ytd_amount("missing") == 0
    with pytest.raises(PackError):
        ctx.ytd_amount("B")


# --------------------------------------------------------------------------- rule data


def test_effective_table_rejects_overlaps_and_open_rows_before_others():
    with pytest.raises(PackError):
        EffectiveTable("t", (RuleRow(date(2026, 1, 1), date(2026, 6, 30), {}), RuleRow(date(2026, 6, 30), None, {})))
    with pytest.raises(PackError):
        EffectiveTable("t", (RuleRow(date(2026, 1, 1), None, {}), RuleRow(date(2026, 7, 1), None, {})))


def test_ec_sta_05_split_rules_by_days_and_period_end():
    table = EffectiveTable("t", (RuleRow(date(2026, 1, 1), date(2026, 9, 16), {"c": D("15000")}),
                                 RuleRow(date(2026, 9, 17), None, {"c": D("25000")})))
    by_days = table.segments(date(2026, 9, 1), date(2026, 9, 30), "BY_DAYS")
    assert [(s.start, s.end, s.days) for s in by_days] == [(date(2026, 9, 1), date(2026, 9, 16), 16),
                                                           (date(2026, 9, 17), date(2026, 9, 30), 14)]
    assert sum(s.fraction for s in by_days) == 1
    (whole,) = table.segments(date(2026, 9, 1), date(2026, 9, 30), "PERIOD_END")
    assert whole.row["c"] == D("25000") and whole.days == 30
    (start,) = table.segments(date(2026, 9, 1), date(2026, 9, 30), "PERIOD_START")
    assert start.row["c"] == D("15000")
    with pytest.raises(PackError):
        table.segments(date(2025, 12, 31), date(2026, 1, 30), "BY_DAYS")      # a gap fails closed
    with pytest.raises(PackError):
        table.segments(date(2026, 9, 1), date(2026, 9, 30), "AVERAGE")


def test_band_tables_progressive_flat_and_fixed():
    progressive = BandTable("p", "PROGRESSIVE", (Band(D("400000"), D("0")), Band(D("800000"), D("5")),
                                                 Band(None, D("10"))))
    assert progressive.apply(D("1000000")).value == D("40000")      # 5% × 400,000 + 10% × 200,000
    assert progressive.apply(D("300000")).value == 0
    flat = BandTable("f", "FLAT_RATE", (Band(D("1000"), D("0")), Band(None, D("20"))))
    assert flat.apply(D("1000")).value == 0 and flat.apply(D("1000.01")).value == D("200.002")
    exclusive = BandTable("x", "FIXED_AMOUNT", (Band(D("25000"), amount=D("0"), inclusive=False),
                                                Band(None, amount=D("200"))))
    assert exclusive.apply(D("24999.99")).value == 0 and exclusive.apply(D("25000")).value == D("200")
    with pytest.raises(PackError):
        BandTable("bad", "PROGRESSIVE", (Band(D("10")), Band(D("5")), Band(None)))
    with pytest.raises(PackError):
        BandTable("bad", "PROGRESSIVE", (Band(D("10")),))


def test_due_rules():
    assert DueRule("DAY_OF_NEXT_MONTH", 15).due_date(date(2026, 1, 31)) == date(2026, 2, 15)
    assert DueRule("DAY_OF_NEXT_MONTH", 31).due_date(date(2026, 1, 31)) == date(2026, 2, 28)
    assert DueRule("LAST_DAY_OF_NEXT_MONTH").due_date(date(2026, 12, 31)) == date(2027, 1, 31)
    assert DueRule("DAYS_AFTER_PERIOD_END", 7).due_date(date(2026, 3, 31)) == date(2026, 4, 7)
    assert DueRule("NEXT_QUARTER_MONTH_DAY", 30).due_date(date(2026, 11, 30)) == date(2027, 1, 30)
    with pytest.raises(PackError):
        DueRule("WHENEVER")


# --------------------------------------------------------------------------- sources


def test_source_manifest_reports_missing_fields_and_rules():
    complete = SourceEntry("R.a", "Act", "https://example.invalid/a", "Gazette", "PRIMARY", date(2026, 1, 1),
                           date(2026, 1, 1), ("V1",), date(2027, 1, 1))
    no_url = SourceEntry("R.b", "Act", None, "Gazette", "PRIMARY", date(2026, 1, 1), date(2026, 1, 1), ("V9",))
    manifest = SourceManifest((complete, no_url))
    problems = manifest.problems(["R.a", "R.b", "R.c"], ["V1"])
    assert "R.c: no source" in problems
    assert "R.b: missing url" in problems and "R.b: missing next_review_due" in problems
    assert "R.b: unknown test vector V9" in problems
    assert not SourceManifest((complete,)).problems(["R.a"], ["V1"])
    assert "R.a: missing reviewer" in SourceManifest((complete,)).problems(["R.a"], ["V1"], for_verification=True)
    with pytest.raises(PackError):
        SourceEntry("R", "t", None, "p", "BLOG", date(2026, 1, 1), None)


# --------------------------------------------------------------------------- rounding, labels, liabilities


def test_ec_sta_13_statutory_rounding_rules():
    assert round_statutory_amount(D("2082.5"), "INR", "NEAREST_WHOLE_UNIT")[0] == D("2083.00")
    assert round_statutory_amount(D("162.01"), "INR", "UP_TO_WHOLE_UNIT")[0] == D("163.00")
    amount, trace = round_statutory_amount(D("952.105"), "EUR", "MINOR_UNIT")
    assert amount == D("952.11") and trace == {"stage": "STATUTORY", "rule": "MINOR_UNIT", "mode": "HALF_UP",
                                               "increment": "0.01"}
    assert round_statutory_amount(D("1500.4"), "JPY", "NEAREST_WHOLE_UNIT")[0] == D("1500")
    with pytest.raises(PackError):
        round_statutory_amount(D("1"), "EUR", "BANKERS")


def test_component_definition_validation():
    with pytest.raises(PackError):
        ComponentDef("X", "x", "DEDUCTION", "TAX", STAGE_EARNINGS, "QQ.x")
    with pytest.raises(PackError):
        ComponentDef("X", "x", "DEDUCTION", "TAX", STAGE_STATUTORY, "QQ.x", rounding="CEIL")
    with pytest.raises(PackError):
        ComponentDef("X", "x", "DEDUCTION", "TAX", STAGE_STATUTORY, "QQ.x", portion="BOTH")


def _stat(code, kind, amount, portion, payee="Fund", due="2026-04-15"):
    return Line(code, kind, "EMPLOYEE_CONTRIBUTION", STAGE_STATUTORY, D(amount), "EUR", statutory=True,
                trace={"scheme": "QQ.f", "portion": portion, "liability": {"payee": payee, "due_date": due}})


def test_ec_sta_10_one_liability_per_portion():
    lines = [_stat("EE", "DEDUCTION", "50.00", "EMPLOYEE"), _stat("ER", "EMPLOYER_CONTRIBUTION", "70.00", "EMPLOYER"),
             _stat("ZERO", "DEDUCTION", "0.00", "EMPLOYEE"),
             Line("BASIC", "EARNING", "BASE", STAGE_EARNINGS, D("1000.00"), "EUR")]
    found = liabilities(lines, date(2026, 3, 31))
    assert [(l.component, l.portion, l.amount) for l in found] == [("EE", "EMPLOYEE", D("50.00")),
                                                                    ("ER", "EMPLOYER", D("70.00"))]
    assert found[0].to_json()["due_date"] == "2026-04-15" and found[0].to_json()["remittance_status"] == "PENDING"


def test_labels_for_fixture_and_unverified_lines():
    line = _stat("EE", "DEDUCTION", "50.00", "EMPLOYEE")
    fixture = with_labels(line, fixture=True, verified=False, status="DRAFT")
    assert fixture.fixture and fixture.statutory and "FIXTURE" in fixture.tags
    assert fixture.explanation.startswith("FIXTURE — not law")
    preview = with_labels(line, fixture=False, verified=False, status="DRAFT")
    assert not preview.fixture and "NOT_VERIFIED" in preview.tags
    assert preview.explanation.startswith("Compliance not verified")
    final = with_labels(line, fixture=False, verified=True, status="VERIFIED_FOR_PERIOD")
    assert final.tags == line.tags and final.trace["verified"] is True


def test_tagged_sums_only_earnings_with_the_tag():
    lines = [Line("A", "EARNING", "BASE", 4, D("100.00"), "EUR", tags=("W",)),
             Line("B", "EARNING", "BONUS", 4, D("50.00"), "EUR"),
             Line("C", "BENEFIT_IN_KIND", "BENEFIT", 4, D("20.00"), "EUR", tags=("W",))]
    assert tagged(lines, "W") == D("100.00")
    assert tagged(lines, "W", kinds=("EARNING", "BENEFIT_IN_KIND")) == D("120.00")


def test_canonical_hash_is_stable_and_sensitive():
    assert canonical_hash({"b": D("1.0"), "a": [date(2026, 1, 1)]}) == canonical_hash({"a": [date(2026, 1, 1)], "b": D("1.0")})
    assert canonical_hash({"a": D("1")}) != canonical_hash({"a": D("2")})
