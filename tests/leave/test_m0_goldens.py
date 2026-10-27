"""LH-M0 golden baselines (WI-0.3; LH 13 §4.1 baselines (1)-(6), exit 1; LH-MIG-01, LH-PAY-30 pre-state).

Day facts of every cast employee for 2025 and 2026, leave and holiday content hashes, payroll day-ledger inputs,
legacy balances, approver sets of pending requests and attendance dashboard counts on the time-stack dataset
(tests/time_goldens.py), compared with the committed JSON in tests/leave/goldens. scripts/lh_baseline.py runs this
module twice to prove determinism and rewrites the files with ``--write``.
"""

from pathlib import Path

import pytest

from tests import time_goldens as tg

GOLDENS = Path(__file__).resolve().parent / "goldens"
pytestmark = pytest.mark.module_dataset


@pytest.fixture(scope="module")
def dataset():
    from app.main import app

    with tg.frozen_attendance_clock() as fake:
        store = tg.seed(app)
        tg.run_punch_matrix(app, store, fake)
        yield store


def test_m0_hash_goldens(dataset):
    """time_inputs.leave_content() and holiday_service.content() hashes (LH-MIG-03 pre-state)."""
    tg.check(GOLDENS, "content_hashes", tg.capture_content_hashes(dataset))


def test_m0_day_facts_golden(dataset):
    """time_inputs.day_facts for every cast employee x date of 2025-2026 (LH-MIG-13, LH-MIG-14 pre-state)."""
    tg.check(GOLDENS, "day_facts", tg.capture_day_facts(dataset))


def test_lh_pay_30_payroll_day_ledger_golden(dataset):
    """Payroll day-ledger inputs for leave / holiday / weekly-off combinations (LH-MIG-01, LH-PAY-30)."""
    tg.check(GOLDENS, "ledger_inputs", tg.capture_ledger_inputs(dataset))


def test_m0_balance_golden(dataset):
    """leave_service.balances per (employee, type, year) (LH-MIG-07 pre-state)."""
    tg.check(GOLDENS, "balances", tg.capture_balances(dataset))


def test_m0_approver_sets_golden(dataset):
    """Eligible approvers per pending request (oracle for 11 §11.5 step 5)."""
    tg.check(GOLDENS, "approver_sets", tg.capture_approver_sets(dataset))


def test_m0_attendance_dashboard_golden(dataset):
    """today_summary, trend(7), monthly_stats (BUG-12 baseline)."""
    tg.check(GOLDENS, "attendance_counts", tg.capture_attendance_counts(dataset))
