"""Property tests: invariants hold for random valid snapshots, replay reproduces ``calc_hash``, and the
segments of a fully paid period add up exactly to the full rate (3.5 §6, §8; EC-CMP-16, F-01)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from app.payroll.engine import calculate, pipeline, replay
from app.payroll.engine.lines import Line, STAGE_POST_TAX
from tests.payroll_unit.test_engine_fixtures import (
    absent,
    days,
    example_a_structure,
    pack_stage,
    settings,
    snapshot,
)

D = Decimal
NOV = [dt.date(2026, 11, d) for d in range(1, 31)]
WORKDAYS = [d for d in NOV if d.weekday() != 6]
BASES = ("CALENDAR_DAYS", "WORKING_DAYS", "FIXED_30", "FIXED_26")
PROFILE = hsettings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


def money(low: int, high: int) -> st.SearchStrategy[str]:
    return st.integers(low * 100, high * 100).map(lambda cents: f"{cents // 100}.{cents % 100:02d}")


def deductions_stage(amount: str):
    def apply(ctx, lines):
        added = [Line("OTHER_DEDUCTION", "DEDUCTION", "VOLUNTARY", STAGE_POST_TAX, D(amount), "INR",
                      explanation="random deduction")] if D(amount) else []
        return [*lines, *added], []

    return apply


@st.composite
def random_snapshots(draw):
    ctc = draw(st.integers(400_000, 4_000_000))
    lop = draw(st.lists(st.sampled_from(WORKDAYS), max_size=6, unique=True))
    basis = draw(st.sampled_from(BASES))
    increment = draw(st.sampled_from([None, "1", "10", "0.05"]))
    handling = draw(st.sampled_from(["ROUNDING_LINE", "CARRY_FORWARD"]))
    bonus = draw(st.lists(money(1, 20_000), max_size=2))
    claims = draw(st.lists(money(1, 5_000), max_size=2))
    split = draw(st.booleans())
    accounts = ([{"account_id": 1, "type": "PERCENT", "value": draw(st.sampled_from(["10", "33.333333", "50"])),
                  "order": 1}, {"account_id": 2, "type": "REMAINDER", "order": 2}] if split
                else [{"account_id": 1, "type": "PRIMARY"}])
    values = settings(payroll__lop__proration_basis=basis, payroll__rounding__difference_handling=handling,
                      payroll__bank__split_payments_enabled=split)
    if increment:
        values["payroll.rounding.net_pay_increment"] = increment
    data = snapshot(
        segments=[{"key": "S1", "start": "2026-11-01", "end": "2026-11-30",
                   "compensation": example_a_structure(str(ctc), employer_pf="1800.00")}],
        days=days(per_day=absent(*lop)), settings=values, bank_accounts=accounts,
        wage_inputs=[{"id": i, "component": "BONUS", "amount": a} for i, a in enumerate(bonus, 1)],
        reimbursements=[{"id": i, "claim_type": "TRAVEL", "amount": a} for i, a in enumerate(claims, 1)],
    )
    deduction = draw(money(0, 300_000))
    epf = draw(money(0, 5_000))
    return data, deduction, epf


@PROFILE
@given(random_snapshots())
def test_invariants_hold_for_random_snapshots(monkeypatch, case):
    """EC-STA-10, EC-MON-10: totals are sums of rounded lines, net and employer cost identities hold, payments
    equal net, a negative net is reported (EC-RUN-10), and replay reproduces the hash (EC-STA-04)."""
    data, deduction, epf = case
    stages = {"statutory": pack_stage(epf=epf, pt="200.00"), "deductions": deductions_stage(deduction)}
    monkeypatch.setattr(pipeline, "_stage", lambda name: stages[name])
    result = calculate(data)
    assert not [i for i in result.issues if i.code == "INVARIANT_VIOLATION"], [i.to_json() for i in result.issues]
    t = result.totals
    assert t.net == t.gross + t.reimbursements - t.deductions + t.net_rounding
    assert t.employer_cost == t.gross + t.employer_contributions + t.employer_benefits
    assert t.gross == sum(line.amount for line in result.lines if line.kind == "EARNING")
    if t.net > 0:
        assert sum(p.amount for p in result.payments) == t.net
    else:
        assert any(i.code == "NEGATIVE_NET" for i in result.blocking) or t.net == 0
        assert not any(p.amount for p in result.payments)
    for line in result.lines:
        assert line.currency == "INR" and line.amount == line.amount.quantize(D("0.01"))
        assert line.explanation
    ok, again = replay(data, result.calc_hash)
    assert ok and again.to_json() == result.to_json()


@PROFILE
@given(st.lists(st.integers(2, 30), max_size=4, unique=True), st.sampled_from(BASES), money(1_000, 900_000))
def test_segments_of_fully_paid_period_sum_to_the_rate(monkeypatch, cuts, basis, rate):
    """EC-CMP-16 / F-01: any split of a fully paid period adds up exactly to the full rate."""
    monkeypatch.setattr(pipeline, "_stage", lambda name: pack_stage(epf="0.00", pt="0.00") if name == "statutory"
                        else (lambda ctx, lines: (list(lines), [])))
    starts = [1, *sorted(cuts)]
    bounds = [(s, (starts[i + 1] - 1) if i + 1 < len(starts) else 30) for i, s in enumerate(starts)]
    keys = [(f"S{i}", dt.date(2026, 11, s), dt.date(2026, 11, e)) for i, (s, e) in enumerate(bounds)]
    structure = {"id": 1, "structure_type": "GROSS_BASED", "currency": "INR",
                 "lines": [{"component": "BASIC", "method": "FIXED", "amount": rate}]}
    data = snapshot(
        segments=[{"key": k, "start": s.isoformat(), "end": e.isoformat(), "compensation": structure} for k, s, e in keys],
        days=days(segments=keys), settings=settings(payroll__lop__proration_basis=basis))
    result = calculate(data)
    parts = [line.amount for line in result.lines if line.component == "BASIC"]
    assert len(parts) == len(keys)
    assert sum(parts) == D(rate)
