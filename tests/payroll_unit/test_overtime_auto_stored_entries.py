"""Regression (found by the golden verification suite, tests/payroll/verification/test_golden_calculations.py):
with OVERTIME_POLICY.source = ATTENDANCE_AUTO the overtime service stores the attendance proposals as APPROVED
entries (origin THRESHOLD) and the engine ALSO derived the same threshold hours from the day ledger, so every
overtime hour was paid twice (3.4 §17.1 paid 3,000.00 instead of 1,500.00). A date with a stored threshold entry
is now paid from that entry only; dates without one still come from the ledger.

Expected values are worked out by hand: OT_BASE 26,000.00 / 26 days / 8 h = 125.00 an hour; x 1.5 = 187.50.
"""

from __future__ import annotations

from decimal import Decimal

from app.payroll.engine import calculate
from tests.payroll_unit.test_engine_fixtures import days, overtime_policy, settings, snapshot, use_stages
from tests.payroll_unit.test_engine_golden import all_workdays, gross_structure, nov, segment, worked

D = Decimal
# 3.4 §17.1 week (Mon 2 - Sat 7 Nov 2026): 10, 9, 9, 11, 9, 8 h -> daily 1 + 2 h, weekly 53 - 48 = 5 h = 8 h.
WEEK = {nov(2): worked(36000), nov(3): worked(32400), nov(4): worked(32400), nov(5): worked(39600),
        nov(6): worked(32400), nov(7): worked(28800)}
AUTO = {"source": "ATTENDANCE_AUTO", "daily_threshold": "9", "weekly_threshold_hours": "48",
        "threshold_combination": "DAILY_THEN_WEEKLY"}


def _data(entries):
    return snapshot(
        segments=[segment(gross_structure("26000.00"))],
        days=days(per_day=all_workdays(28800, overrides=WEEK)),
        settings=settings(payroll__attendance__enabled=True, payroll__overtime__enabled=True),
        policies={"OVERTIME_POLICY": overtime_policy(**AUTO)},
        overtime_entries=entries,
    )


def _stored(day, minutes, entry_id):
    return {"entry_id": f"overtime:{entry_id}", "date": day.isoformat(), "minutes": minutes, "status": "APPROVED",
            "origin": "THRESHOLD"}


def test_stored_auto_proposals_are_not_paid_again_from_the_ledger(monkeypatch):
    use_stages(monkeypatch)
    stored = [_stored(nov(2), 60, 1), _stored(nov(5), 120, 2), _stored(nov(7), 300, 3)]
    ot = calculate(_data(stored)).line("OT_WORKDAY")
    # 1 + 2 + 5 = 8 h, each once: 8 x 125.00 x 1.5 = 1,500.00 (was 16 h = 3,000.00).
    assert ot.units == D("8") and ot.amount == D("8") * D("125.00") * D("1.5") == D("1500.00")


def test_dates_without_a_stored_entry_still_come_from_the_ledger(monkeypatch):
    use_stages(monkeypatch)
    # Only Monday's proposal was stored (e.g. generated before the rest of the week): Thursday 2 h and Saturday
    # 5 h are derived from the ledger; Monday is not counted twice: 1 + 2 + 5 = 8 h -> 1,500.00.
    ot = calculate(_data([_stored(nov(2), 60, 1)])).line("OT_WORKDAY")
    assert ot.units == D("8") and ot.amount == D("1500.00")


def test_without_stored_entries_the_ledger_pays_the_threshold_hours_once(monkeypatch):
    use_stages(monkeypatch)
    ot = calculate(_data([])).line("OT_WORKDAY")
    assert ot.units == D("8") and ot.amount == D("1500.00")
