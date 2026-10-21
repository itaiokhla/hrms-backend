"""Proration bases, kinds and segments (3.4 §5, 3.3 §9 and §11 Example A)."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.payroll import ledger as L
from app.payroll import proration as P
from app.payroll.proration import Basis, PeriodFacts, ProrationError, ProrationKind, UnitFacts
from app.payroll.workweek import MON_FRI, MON_SAT, MON_SAT_2ND_4TH_SAT_OFF, SUN_THU, WorkWeekPattern, dates_between, is_weekly_off

D = Decimal
CENT = D("0.01")
R = D("30000.00")
FEB_1, FEB_28 = dt.date(2026, 2, 1), dt.date(2026, 2, 28)
FEB_PERIOD = PeriodFacts(days=28, working_days=24, non_weekly_off_days=24, standard_hours=D(192))
DAY_BASES = (Basis.CALENDAR_DAYS, Basis.WORKING_DAYS, Basis.FIXED_30, Basis.FIXED_26)


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def facts(employment: int, working: int, non_weekly_off: int, lop: str = "0", lop_working: str | None = None,
          payable: str = "0", standard: str = "0") -> UnitFacts:
    return UnitFacts(D(employment), D(working), D(non_weekly_off), D(lop), D(lop if lop_working is None else lop_working),
                     D(payable), D(standard))


def month_ledger(
    start: dt.date,
    end: dt.date,
    pattern: WorkWeekPattern = MON_SAT,
    *,
    hired: dt.date | None = None,
    left: dt.date | None = None,
    lop_dates: frozenset[dt.date] = frozenset(),
    absent_dates: frozenset[dt.date] = frozenset(),
    segments: tuple[tuple[dt.date, str], ...] = (),
    cfg: L.LedgerConfig | None = None,
) -> L.DayLedger:
    """A period ledger: manual LOP on ``lop_dates``; with attendance ON, ``absent_dates`` are absent, others present."""
    config = cfg or L.LedgerConfig()
    days = []
    for day in dates_between(start, end):
        segment = ""
        for since, key in segments:
            if day >= since:
                segment = key
        attendance = None
        if config.attendance_enabled:
            status = L.AttendanceStatus.ABSENT if day in absent_dates else L.AttendanceStatus.PRESENT
            attendance = L.AttendanceInput(status, session_id=f"s-{day.isoformat()}",
                                           worked_seconds=None if day in absent_dates else 8 * 3600)
        days.append(L.DayInput(
            date=day,
            employed=(hired is None or day >= hired) and (left is None or day <= left),
            weekly_off=is_weekly_off(pattern, day),
            manual_lop=(L.ManualLop(f"lop-{day.isoformat()}"),) if day in lop_dates else (),
            attendance=attendance,
            segment=segment,
        ))
    return L.build_ledger(days, config)


def earned(ledger: L.DayLedger, basis: Basis, rate: Decimal = R, kind: ProrationKind = ProrationKind.BY_PAID_DAYS,
           custom_days: int | None = None) -> Decimal:
    amount, _ = P.prorate_component(rate, basis, kind, ledger.period, ledger.unit_facts(), custom_days=custom_days)
    return cents(amount)


# --- 3.4 §5.2 golden table: February 2026, BASIC 30,000.00, Monday–Saturday ---------------------------------------

GOLDEN_ROW_1 = {Basis.CALENDAR_DAYS: "27857.14", Basis.WORKING_DAYS: "27500.00", Basis.FIXED_30: "28000.00",
                Basis.FIXED_26: "27692.31"}
GOLDEN_ROW_2 = {Basis.CALENDAR_DAYS: "13928.57", Basis.WORKING_DAYS: "15000.00", Basis.FIXED_30: "13000.00",
                Basis.FIXED_26: "13846.15"}


@pytest.mark.parametrize("basis", DAY_BASES)
def test_doc_3_4_5_2_row_1_full_month_two_lop_working_days(basis: Basis) -> None:
    """3.4 §5.2 row 1 (EC-ATT-14, EC-ATT-20): full month with 2 LOP working days."""
    amount, explanation = P.prorate_component(R, basis, ProrationKind.BY_PAID_DAYS, FEB_PERIOD, facts(28, 24, 24, "2"))
    assert cents(amount) == D(GOLDEN_ROW_1[basis])
    assert explanation.basis is basis


@pytest.mark.parametrize("basis", DAY_BASES)
def test_doc_3_4_5_2_row_2_joined_monday_16_february(basis: Basis) -> None:
    """3.4 §5.2 row 2 (EC-ATT-13, EC-ATT-14): joined Monday 16 Feb, 13 calendar days, 12 working days."""
    period = PeriodFacts(28, 24, 24, D(192), employed_all_period=False)
    amount, _ = P.prorate_component(R, basis, ProrationKind.BY_PAID_DAYS, period, facts(13, 12, 12))
    assert cents(amount) == D(GOLDEN_ROW_2[basis])


@pytest.mark.parametrize("basis", DAY_BASES)
def test_doc_3_4_5_2_rows_1_and_2_end_to_end_from_the_ledger(basis: Basis) -> None:
    """3.4 §5.2 rows 1-2 computed from a real day ledger, attendance ON and OFF."""
    lop = frozenset({dt.date(2026, 2, 10), dt.date(2026, 2, 11)})
    assert earned(month_ledger(FEB_1, FEB_28, lop_dates=lop), basis) == D(GOLDEN_ROW_1[basis])
    on = L.LedgerConfig(attendance_enabled=True)
    assert earned(month_ledger(FEB_1, FEB_28, absent_dates=lop, cfg=on), basis) == D(GOLDEN_ROW_1[basis])
    joined = month_ledger(FEB_1, FEB_28, hired=dt.date(2026, 2, 16))
    assert (joined.totals.employment_days, joined.totals.scheduled_working_days) == (13, 12)
    assert earned(joined, basis) == D(GOLDEN_ROW_2[basis])


@pytest.mark.parametrize("basis", DAY_BASES)
def test_doc_3_4_5_2_row_3_absent_every_working_day_hits_the_guard(basis: Basis) -> None:
    """3.4 §5.2 row 3 / §5.3 (EC-ATT-09): absent on all 24 working days → guard, never a silent amount."""
    working = frozenset(d for d in dates_between(FEB_1, FEB_28) if not is_weekly_off(MON_SAT, d))
    ledger = month_ledger(FEB_1, FEB_28, absent_dates=working, cfg=L.LedgerConfig(attendance_enabled=True))
    assert ledger.guard.triggered and ledger.guard.reason is L.ZeroPayReason.NO_PAID_WORKING_DAYS
    assert [e.code for e in ledger.blocking] == [L.ExceptionCode.ZERO_PAID_WORKING_DAYS]
    pay_zero = month_ledger(FEB_1, FEB_28, absent_dates=working,
                            cfg=L.LedgerConfig(attendance_enabled=True, zero_paid_days=L.ZeroPaidDaysPolicy.PAY_ZERO))
    assert pay_zero.guard.pay_zero and not pay_zero.blocking
    amount, explanation = P.prorate_component(R, basis, ProrationKind.BY_PAID_DAYS, pay_zero.period,
                                              pay_zero.unit_facts(), zero_pay=pay_zero.guard.pay_zero)
    assert amount == 0 and explanation.formula == "0 (zero-pay guard)"


def test_doc_3_4_5_2_calendar_days_quirk_pays_sundays_without_the_guard() -> None:
    """3.4 §5.2 quirk: CALENDAR_DAYS alone would pay the 4 Sundays of an all-absent month (why §5.3 exists)."""
    working = frozenset(d for d in dates_between(FEB_1, FEB_28) if not is_weekly_off(MON_SAT, d))
    ledger = month_ledger(FEB_1, FEB_28, absent_dates=working, cfg=L.LedgerConfig(attendance_enabled=True))
    assert earned(ledger, Basis.CALENDAR_DAYS) == D("4285.71")
    assert earned(ledger, Basis.WORKING_DAYS) == D("0.00")


def test_ec_att_14_fixed_30_february_joiner_pays_90_percent() -> None:
    """EC-ATT-14 quirk: FIXED_30, joining on 2 February pays 27 ÷ 30 = 90% for 96% of the month."""
    joined = month_ledger(FEB_1, FEB_28, hired=dt.date(2026, 2, 2))
    assert earned(joined, Basis.FIXED_30) == D("27000.00")
    assert earned(month_ledger(FEB_1, FEB_28), Basis.FIXED_30) == R


def test_ec_att_14_fixed_26_on_a_five_day_week() -> None:
    """EC-ATT-14 quirk: FIXED_26 on a 5-day week pays R for a full month but well under half for half a month."""
    assert earned(month_ledger(FEB_1, FEB_28, MON_FRI), Basis.FIXED_26) == R
    half = month_ledger(FEB_1, FEB_28, MON_FRI, hired=dt.date(2026, 2, 16))
    assert half.totals.non_weekly_off_days == 10
    assert earned(half, Basis.FIXED_26) == D("11538.46") < R / 2


def test_ec_att_20_bases_differ_for_the_same_month() -> None:
    """EC-ATT-20: calendar vs fixed 26/30 vs scheduled working days give the golden values, not one another's."""
    ledger = month_ledger(FEB_1, FEB_28, lop_dates=frozenset({dt.date(2026, 2, 2), dt.date(2026, 2, 3)}))
    results = {basis: earned(ledger, basis) for basis in DAY_BASES}
    assert results == {basis: D(value) for basis, value in GOLDEN_ROW_1.items()}
    assert len(set(results.values())) == 4


def test_ec_att_20_full_month_without_lop_pays_r_on_every_basis() -> None:
    """EC-ATT-20 (negative): with no LOP and no joining or leaving, every basis pays exactly R."""
    ledger = month_ledger(FEB_1, FEB_28)
    assert {basis: earned(ledger, basis) for basis in DAY_BASES} == {basis: R for basis in DAY_BASES}
    assert earned(ledger, Basis.CUSTOM_DAYS, custom_days=25) == R


def test_ec_att_21_work_week_pattern_sets_the_working_days_denominator() -> None:
    """EC-ATT-21: the employee's own pattern decides N for WORKING_DAYS (24 on MON_SAT, 22 with 2nd/4th Sat, 20 MON_FRI)."""
    lop = frozenset({dt.date(2026, 2, 3)})
    n = {p: month_ledger(FEB_1, FEB_28, p, lop_dates=lop).period.working_days
         for p in (MON_SAT, MON_SAT_2ND_4TH_SAT_OFF, MON_FRI)}
    assert n == {MON_SAT: 24, MON_SAT_2ND_4TH_SAT_OFF: 22, MON_FRI: 20}
    assert earned(month_ledger(FEB_1, FEB_28, MON_FRI, lop_dates=lop), Basis.WORKING_DAYS) == D("28500.00")
    assert earned(month_ledger(FEB_1, FEB_28, MON_SAT, lop_dates=lop), Basis.WORKING_DAYS) == D("28750.00")


def test_ec_att_21_sunday_thursday_week_counts_fridays_and_saturdays_off() -> None:
    """EC-ATT-21 (negative): a Friday LOP on a SUN_THU week is not a working-day LOP at all."""
    friday = dt.date(2026, 2, 6)
    ledger = month_ledger(FEB_1, FEB_28, SUN_THU, lop_dates=frozenset({friday}))
    assert ledger.period.working_days == 20
    assert ledger.totals.lop_days == 0 and earned(ledger, Basis.WORKING_DAYS) == R


# --- 3.3 §11 Example A -----------------------------------------------------------------------------------------------


def test_doc_3_3_11_example_a_two_lop_days_in_a_30_day_month() -> None:
    """3.3 §11 Example A: BASIC 46,666.67, HRA 18,666.67, FIXED_ALLOWANCE 25,200.00 (CALENDAR_DAYS, 2 LOP of 30)."""
    september = month_ledger(dt.date(2026, 9, 1), dt.date(2026, 9, 30), MON_FRI,
                             lop_dates=frozenset({dt.date(2026, 9, 14), dt.date(2026, 9, 15)}))
    assert september.totals.lop_days == 2 and september.period.days == 30
    structure = {"BASIC": D("50000.00"), "HRA": D("20000.00"), "FIXED_ALLOWANCE": D("27000.00")}
    got = {code: earned(september, Basis.CALENDAR_DAYS, rate) for code, rate in structure.items()}
    assert got == {"BASIC": D("46666.67"), "HRA": D("18666.67"), "FIXED_ALLOWANCE": D("25200.00")}
    _, explanation = P.prorate_component(D("50000.00"), Basis.CALENDAR_DAYS, ProrationKind.BY_PAID_DAYS,
                                         september.period, september.unit_facts())
    assert explanation.to_dict() == {
        "method": "BY_PAID_DAYS", "basis": "CALENDAR_DAYS", "rate": "50000.00", "basis_days": "30",
        "paid_units": "28", "capped": False, "formula": "50000.00 × 28 ÷ 30",
        "note": "30 employment days − 2 LOP days",
    }


# --- §5.1 rules: caps, counted days, kinds -------------------------------------------------------------------------


def test_fixed_bases_cap_at_the_rate_in_a_31_day_month() -> None:
    """3.4 §5.1: N − min(lop, N) when employed all period; min(N, counted − lop) for a joiner; min(R, R × U ÷ N)."""
    period = PeriodFacts(31, 27, 27, D(216))
    for basis in (Basis.FIXED_30, Basis.FIXED_26):
        assert P.paid_units(basis, ProrationKind.BY_PAID_DAYS, period, facts(31, 27, 27)) == P.basis_days(basis, period)
        assert P.paid_units(basis, ProrationKind.BY_PAID_DAYS, period, facts(31, 27, 27, "31", "27")) == 0
    joiner = PeriodFacts(31, 27, 27, D(216), employed_all_period=False)
    assert P.paid_units(Basis.FIXED_30, ProrationKind.BY_PAID_DAYS, joiner, facts(31, 27, 27)) == 30
    assert P.paid_units(Basis.FIXED_30, ProrationKind.BY_PAID_DAYS, joiner, facts(30, 26, 26)) == 30
    assert P.paid_units(Basis.FIXED_30, ProrationKind.BY_PAID_DAYS, joiner, facts(30, 26, 26, "1")) == 29
    amount, explanation = P.prorate(R, Basis.FIXED_30, D(30), D(31), cap_at_rate=True)
    assert amount == R and explanation.capped and explanation.formula.startswith("min(30000.00, ")


def test_custom_days_basis_counts_calendar_days() -> None:
    period = PeriodFacts(28, 24, 24, D(192), employed_all_period=False)
    assert P.basis_days(Basis.CUSTOM_DAYS, period, custom_days=25) == 25
    assert P.paid_units(Basis.CUSTOM_DAYS, ProrationKind.BY_PAID_DAYS, period, facts(13, 12, 12, "1"), custom_days=25) == 12
    full = P.paid_units(Basis.CUSTOM_DAYS, ProrationKind.BY_PAID_DAYS, FEB_PERIOD, facts(28, 24, 24, "3"), custom_days=25)
    assert full == 22


def test_hours_basis_and_by_paid_hours() -> None:
    """3.4 §5.1 HOURS: U = payable hours, N = standard hours, min(R, R × U ÷ N); BY_PAID_HOURS always uses HOURS."""
    unit = facts(28, 24, 24, payable="180", standard="192")
    amount, explanation = P.prorate_component(R, Basis.CALENDAR_DAYS, ProrationKind.BY_PAID_HOURS, FEB_PERIOD, unit)
    assert explanation.basis is Basis.HOURS and cents(amount) == D("28125.00")
    over, explanation = P.prorate_component(R, Basis.HOURS, ProrationKind.BY_PAID_DAYS, FEB_PERIOD,
                                            facts(28, 24, 24, payable="200", standard="192"))
    assert over == R and explanation.capped
    assert P.effective_basis(Basis.FIXED_26, ProrationKind.BY_PAID_HOURS) is Basis.HOURS
    assert P.effective_basis(Basis.FIXED_26, ProrationKind.BY_PAID_DAYS) is Basis.FIXED_26


def test_by_employment_days_ignores_lop_but_prorates_joining() -> None:
    """3.3 §9: BY_EMPLOYMENT_DAYS uses the same formulas with lop_days = 0."""
    with_lop = facts(28, 24, 24, "2")
    for basis in DAY_BASES:
        amount, _ = P.prorate_component(R, basis, ProrationKind.BY_EMPLOYMENT_DAYS, FEB_PERIOD, with_lop)
        assert amount == R
    joined = PeriodFacts(28, 24, 24, D(192), employed_all_period=False)
    amount, _ = P.prorate_component(R, Basis.CALENDAR_DAYS, ProrationKind.BY_EMPLOYMENT_DAYS, joined, facts(13, 12, 12, "2"))
    assert cents(amount) == D("13928.57")
    hours, _ = P.prorate_component(R, Basis.HOURS, ProrationKind.BY_EMPLOYMENT_DAYS, FEB_PERIOD,
                                   facts(28, 24, 24, "2", payable="100", standard="192"))
    assert hours == R


def test_none_is_never_prorated() -> None:
    amount, explanation = P.prorate_component(R, Basis.CALENDAR_DAYS, ProrationKind.NONE, FEB_PERIOD, facts(13, 12, 12, "5"))
    assert amount == R and explanation.basis is None and "not prorated" in explanation.formula
    with pytest.raises(ProrationError) as caught:
        P.paid_units(Basis.CALENDAR_DAYS, ProrationKind.NONE, FEB_PERIOD, facts(28, 24, 24))
    assert caught.value.code == "NOT_PRORATED"


def test_lop_beyond_units_gives_zero_not_negative() -> None:
    unit = facts(5, 4, 4, "5", "4")
    assert P.paid_units(Basis.WORKING_DAYS, ProrationKind.BY_PAID_DAYS, FEB_PERIOD, unit) == 0
    period = PeriodFacts(28, 24, 24, D(192), employed_all_period=False)
    assert P.paid_units(Basis.FIXED_26, ProrationKind.BY_PAID_DAYS, period, facts(5, 4, 4, "5", "4")) == 0


def test_amounts_are_unrounded_decimals() -> None:
    amount, _ = P.prorate(D("30000.00"), Basis.CALENDAR_DAYS, D(28), D(26), cap_at_rate=False)
    assert isinstance(amount, Decimal) and amount == D("27857.14285714285714285714286")


# --- §5.4 segments (EC-CMP-16) ----------------------------------------------------------------------------------------


def test_ec_cmp_16_revision_on_the_16th_divides_each_segment_by_the_full_period() -> None:
    """EC-CMP-16: September 2026 revision on the 16th, CALENDAR_DAYS: 30,000 × 15 ÷ 30 + 36,000 × 15 ÷ 30."""
    ledger = month_ledger(dt.date(2026, 9, 1), dt.date(2026, 9, 30), MON_FRI,
                          segments=((dt.date(2026, 9, 1), "A"), (dt.date(2026, 9, 16), "B")))
    assert ledger.segment_keys == ("A", "B")
    results = P.prorate_segments((R, D("36000.00")), Basis.CALENDAR_DAYS, ProrationKind.BY_PAID_DAYS,
                                 ledger.period, ledger.segment_facts())
    assert [a for a, _ in results] == [D("15000.00"), D("18000.00")]
    assert all(e.basis_days == 30 for _, e in results)


@pytest.mark.parametrize("basis", (*DAY_BASES, Basis.HOURS))
def test_ec_cmp_16_segments_of_a_fully_paid_period_sum_to_the_rate(basis: Basis) -> None:
    """EC-CMP-16 / 3.4 §5.4: equal rates over a fully paid February split on the 16th add up exactly to R."""
    ledger = month_ledger(FEB_1, FEB_28, segments=((FEB_1, "A"), (dt.date(2026, 2, 16), "B")))
    results = P.prorate_segments((R, R), basis, ProrationKind.BY_PAID_DAYS, ledger.period, ledger.segment_facts())
    assert sum((a for a, _ in results), D(0)) == R
    assert sum(P.round_segments([a for a, _ in results], CENT)) == R


def test_ec_cmp_16_segments_with_lop_do_not_sum_to_the_rate() -> None:
    """EC-CMP-16 (negative): with an LOP day in segment B the total is below R, by exactly one day's pay."""
    ledger = month_ledger(dt.date(2026, 9, 1), dt.date(2026, 9, 30), MON_FRI, lop_dates=frozenset({dt.date(2026, 9, 21)}),
                          segments=((dt.date(2026, 9, 1), "A"), (dt.date(2026, 9, 16), "B")))
    results = P.prorate_segments((R, R), Basis.CALENDAR_DAYS, ProrationKind.BY_PAID_DAYS, ledger.period,
                                 ledger.segment_facts())
    assert [a for a, _ in results] == [D("15000.00"), D("14000.00")]


def test_fixed_basis_segments_share_n_by_counted_days() -> None:
    """Fixed bases, employed all period: each segment gets its calendar (FIXED_30) or non-weekly-off (FIXED_26) share of N."""
    ledger = month_ledger(FEB_1, FEB_28, segments=((FEB_1, "A"), (dt.date(2026, 2, 15), "B")))
    units_30 = P.segment_units(Basis.FIXED_30, ProrationKind.BY_PAID_DAYS, ledger.period, ledger.segment_facts())
    assert sum(units_30) == 30 and units_30[0] == D(30) * 14 / 28
    units_26 = P.segment_units(Basis.FIXED_26, ProrationKind.BY_PAID_DAYS, ledger.period, ledger.segment_facts())
    assert sum(units_26) == 26 and units_26[0] == D(26) * 12 / 24


def test_segments_must_cover_the_period_when_employed_all_period() -> None:
    with pytest.raises(ProrationError) as caught:
        P.segment_units(Basis.FIXED_30, ProrationKind.BY_PAID_DAYS, FEB_PERIOD, (facts(14, 12, 12),))
    assert caught.value.code == "SEGMENTS_DO_NOT_COVER_PERIOD"
    with pytest.raises(ProrationError) as caught:
        P.segment_units(Basis.CALENDAR_DAYS, ProrationKind.BY_PAID_DAYS, FEB_PERIOD, (facts(20, 18, 18), facts(10, 8, 8)))
    assert caught.value.code == "SEGMENTS_EXCEED_PERIOD"
    with pytest.raises(ProrationError) as caught:
        P.prorate_segments((R,), Basis.CALENDAR_DAYS, ProrationKind.BY_PAID_DAYS, FEB_PERIOD, (facts(14, 12, 12),) * 2)
    assert caught.value.code == "SEGMENT_MISMATCH"
    with pytest.raises(ProrationError) as caught:
        P.segment_units(Basis.CALENDAR_DAYS, ProrationKind.BY_PAID_DAYS, FEB_PERIOD, ())
    assert caught.value.code == "NO_SEGMENTS"


def test_round_segments_keeps_the_rounded_total() -> None:
    thirds = P.prorate_segments((D(100),) * 3, Basis.CALENDAR_DAYS, ProrationKind.BY_PAID_DAYS,
                                PeriodFacts(30, 30, 30, D(0)), (facts(10, 10, 10),) * 3)
    amounts = [a for a, _ in thirds]
    assert sum(amounts) == 100
    assert P.round_segments(amounts, CENT) == (D("33.33"), D("33.34"), D("33.33"))
    assert P.round_segments([D("333.5"), D("333.5")], D(1)) == (D(334), D(333))
    with pytest.raises(ProrationError) as caught:
        P.round_segments(amounts, D(0))
    assert caught.value.code == "INVALID_QUANTUM"
    with pytest.raises(ProrationError) as caught:
        P.round_segments(amounts, CENT, "HALF_UP")
    assert caught.value.code == "INVALID_ROUNDING"


# --- validation ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda: P.basis_days("DAYS", FEB_PERIOD), "INVALID_BASIS"),
        (lambda: P.basis_days(Basis.CUSTOM_DAYS, FEB_PERIOD), "CUSTOM_DAYS_REQUIRED"),
        (lambda: P.basis_days(Basis.CUSTOM_DAYS, FEB_PERIOD, custom_days=32), "INVALID_CUSTOM_DAYS"),
        (lambda: P.basis_days(Basis.CUSTOM_DAYS, FEB_PERIOD, custom_days=True), "INVALID_CUSTOM_DAYS"),
        (lambda: P.basis_days(Basis.WORKING_DAYS, PeriodFacts(7, 0, 0, D(0))), "ZERO_BASIS"),
        (lambda: P.basis_days(Basis.HOURS, PeriodFacts(7, 5, 5, D(0))), "ZERO_BASIS"),
        (lambda: P.prorate(R, Basis.CALENDAR_DAYS, D(0), D(1), cap_at_rate=False), "ZERO_BASIS"),
        (lambda: P.prorate(R, Basis.CALENDAR_DAYS, D(28), D(-1), cap_at_rate=False), "NEGATIVE_UNITS"),
        (lambda: P.prorate(30000.0, Basis.CALENDAR_DAYS, D(28), D(1), cap_at_rate=False), "INVALID_NUMBER"),  # type: ignore[arg-type]
        (lambda: P.prorate(D("NaN"), Basis.CALENDAR_DAYS, D(28), D(1), cap_at_rate=False), "INVALID_NUMBER"),
        (lambda: P.prorate(R, Basis.CALENDAR_DAYS, D(28), D(1), cap_at_rate=False, kind="DAILY"), "INVALID_KIND"),
        (lambda: PeriodFacts(0, 0, 0, D(0)), "INVALID_FACTS"),
        (lambda: PeriodFacts(28, 25, 24, D(0)), "INVALID_FACTS"),
        (lambda: facts(28, 25, 24), "INVALID_FACTS"),
        (lambda: facts(10, 8, 8, "11"), "INVALID_FACTS"),
        (lambda: facts(10, 8, 8, "1", "2"), "INVALID_FACTS"),
        (lambda: facts(10, 8, 8, "-1"), "INVALID_FACTS"),
    ],
)
def test_invalid_inputs_raise_typed_errors(call: object, code: str) -> None:
    with pytest.raises(ProrationError) as caught:
        call()  # type: ignore[operator]
    assert caught.value.code == code


def test_decimal_helpers() -> None:
    assert P.plain(D("28.000")) == "28" and P.plain(D("0.50")) == "0.5" and P.plain(D("-0")) == "0"
    assert P.canonical(D("1.50")) == D("1.5") and str(P.canonical(D("2E+1"))) == "20"
    assert P.decimal_value(" 12.5 ", "x") == D("12.5")
    with pytest.raises(ProrationError):
        P.decimal_value("abc", "x")
    with pytest.raises(ProrationError):
        P.decimal_value(True, "x")


# --- properties ------------------------------------------------------------------------------------------------------

PATTERNS = (MON_FRI, MON_SAT, SUN_THU, MON_SAT_2ND_4TH_SAT_OFF)


@settings(max_examples=60, deadline=None)
@given(
    year=st.integers(2024, 2028),
    month=st.integers(1, 12),
    pattern=st.sampled_from(PATTERNS),
    splits=st.sets(st.integers(2, 28), max_size=3),
    basis=st.sampled_from(tuple(Basis)),
    rate=st.decimals(min_value=D("0.01"), max_value=D("9999999.99"), places=2, allow_nan=False, allow_infinity=False),
)
def test_property_segments_of_a_fully_paid_period_sum_to_r(
    year: int, month: int, pattern: WorkWeekPattern, splits: set[int], basis: Basis, rate: Decimal
) -> None:
    """3.4 §5.4: whatever the split points and basis, the segments of a fully paid period add up exactly to R."""
    start = dt.date(year, month, 1)
    end = (start + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    cuts = tuple((dt.date(year, month, day), f"seg{day:02d}") for day in sorted({1, *splits}))
    ledger = month_ledger(start, end, pattern, segments=cuts)
    results = P.prorate_segments([rate] * len(ledger.segment_keys), basis, ProrationKind.BY_PAID_DAYS,
                                 ledger.period, ledger.segment_facts(), custom_days=31)
    amounts = [amount for amount, _ in results]
    assert sum(amounts, D(0)) == rate
    assert sum(P.round_segments(amounts, CENT), D(0)) == rate


@settings(max_examples=80, deadline=None)
@given(
    employment=st.integers(1, 31),
    lop_halves=st.integers(0, 62),
    basis=st.sampled_from(DAY_BASES),
    all_period=st.booleans(),
)
def test_property_earned_is_between_zero_and_r(employment: int, lop_halves: int, basis: Basis, all_period: bool) -> None:
    days = 31 if all_period else max(employment, 28)
    employment = days if all_period else employment
    lop = min(D(lop_halves) / 2, D(employment))
    working = max(0, employment - employment // 7)
    period = PeriodFacts(days, max(0, days - days // 7), max(0, days - days // 7), D(0), employed_all_period=all_period)
    unit = UnitFacts(D(employment), D(working), D(working), lop, min(lop, D(working)))
    if period.working_days == 0 and basis is Basis.WORKING_DAYS:
        return
    amount, _ = P.prorate_component(R, basis, ProrationKind.BY_PAID_DAYS, period, unit)
    assert D(0) <= amount <= R
