"""Explanation text helpers and the calculation hash (3.5 §8.2-§8.3)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.payroll.engine.lines import Line
from app.payroll.engine.trace import (
    calc_hash,
    canonical_lines,
    date_ranges,
    lop_note,
    money_text,
    qty_text,
    rate_text,
    sorted_lines,
)

D = Decimal


def nov(day: int) -> dt.date:
    return dt.date(2026, 11, day)


def test_date_ranges_collapse_consecutive_days():
    assert date_ranges([nov(13), nov(12)]) == "12–13 Nov"
    assert date_ranges([nov(3), nov(12), nov(13)]) == "3 Nov, 12–13 Nov"
    assert date_ranges([nov(30), dt.date(2026, 12, 1)]) == "30 Nov–1 Dec"
    assert date_ranges([]) == ""


def test_lop_note_matches_the_spec_sentence():
    assert lop_note(D(2), [nov(12), nov(13)]) == " (2 LOP days: 12–13 Nov)"
    assert lop_note(D("0.5"), [nov(7)]) == " (0.5 LOP days: 7 Nov)"
    assert lop_note(D(1), [nov(7)]) == " (1 LOP day: 7 Nov)"
    assert lop_note(D(0), []) == ""


def test_number_formats():
    assert money_text(D("50000"), 2) == "50,000.00"
    assert money_text(D("1500"), 0) == "1,500"
    assert money_text(D("12.3456"), 3) == "12.346"
    assert rate_text(D("240.384615384615384615"), 2) == "240.3846…"
    assert rate_text(D("125"), 2) == "125.00"
    assert rate_text(D("12.125"), 2) == "12.125"
    assert qty_text(D("28.000")) == "28" and qty_text(D("182.50")) == "182.5" and qty_text(D("1200")) == "1,200"


def line(component: str, amount: str, **extra) -> Line:
    return Line(component, "EARNING", "BASE", 4, D(amount), "INR", **extra)


def test_calc_hash_is_order_independent_and_sensitive_to_every_field():
    a, b = line("BASIC", "100.00", sort_order=10), line("HRA", "40.00", sort_order=20)
    first = calc_hash("snap", "2.0.0", [a, b])
    assert first == calc_hash("snap", "2.0.0", [b, a])
    assert first != calc_hash("snap2", "2.0.0", [a, b])
    assert first != calc_hash("snap", "2.0.1", [a, b])
    assert first != calc_hash("snap", "2.0.0", [a, line("HRA", "40.01", sort_order=20)])
    assert first != calc_hash("snap", "2.0.0", [a, line("HRA", "40.00", sort_order=20, explanation="x")])
    assert [x.component for x in sorted_lines([b, a])] == ["BASIC", "HRA"]
    assert canonical_lines([a]).startswith('[{"amount":"100.00"')
