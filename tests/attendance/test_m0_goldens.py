"""AS-M0 golden baselines (WI-AS-0.3; AS 13 §4.1 deliverables (1)-(6), exit 1; AS-MIG-09 golden determinism).

Today's attendance sessions, payroll attendance / ledger inputs, standard hours, overtime day types and
shift-allowance sessions on the time-stack dataset (tests/time_goldens.py), compared with the committed JSON in
tests/attendance/goldens. scripts/as_baseline.py runs this module twice to prove determinism and rewrites the
files with ``--write``. Later goals must keep every file identical for v1 tenants (zero-diff, AS 13 §4.3 exit 1).
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


def test_seed_complete(dataset):
    """The dataset itself: three tenants and no refused seed step (a refused step would hide behaviour)."""
    assert sorted(dataset["companies"]) == ["alpha", "beta", "gamma"]
    assert dataset["seed_blocked"] == []


def test_as_wl_01_one_cast_for_both_sets(dataset):
    """AS-WL-01: one Beta cast — BR 51 / BR 52 (LH) and BR 311 / BR 321 (AS); Mariam Saeed is the HR admin and the
    only Layla is the BR 311 associate, so a sign-in as either person means one thing in both suites."""
    import seed_time_three_companies as seed

    beta = seed.COMPANIES["beta"]
    assert [b[3] for b in beta["branches"]] == ["BR51", "BR52", "BR311", "BR321"]
    people = {p[0]: p for p in beta["people"]}
    assert people["mariam"][3] == ["hr_admin"] and people["layla"][5] == "br311"
    names = [f"{p[1]} {p[2]}" for c in seed.COMPANIES.values() for p in c["people"]]
    assert len(names) == len(set(names)) and sum(n.startswith("Layla ") for n in names) == 1
    accounts = dataset["companies"]["beta"]["accounts"]
    assert accounts["mariam"]["system_roles"] == ["hr_admin"] and accounts["layla"]["system_roles"] == ["employee"]


def test_lh_pay_01_alpha_has_its_two_reference_placements(dataset):
    """LH-PAY-01: Pune Plant (BR 22, MON_SAT_2ND_4TH_SAT_OFF) and Bengaluru Office (BR 21, MON_FRI) both exist."""
    import seed_time_three_companies as seed

    branches = {b[3]: b[6][0] for b in seed.COMPANIES["alpha"]["branches"]}
    assert branches["BR22"] == "MON_SAT_2ND_4TH_SAT_OFF" and branches["BR21"] == "MON_FRI"
    ids = dataset["companies"]["alpha"]["ids"]
    assert ids["people"]["ravi"]["branch"] == "br22" and ids["people"]["priya"]["branch"] == "br21"


def test_m0_session_goldens(dataset):
    """Punch matrix and manual history: status, late / early minutes, attendance_hours net seconds, content()."""
    tg.check(GOLDENS, "sessions", tg.capture_sessions(dataset))


def test_m0_payroll_inputs_golden(dataset):
    """_attendance_inputs and ledger_inputs per employee-date, locked and open period (T-3 / zero-diff oracle)."""
    tg.check(GOLDENS, "payroll_inputs", tg.capture_payroll_inputs(dataset))


def test_m0_standard_hours_golden(dataset):
    """Standard hours (time_inputs.ledger_inputs), overtime _day_types / _worked, snapshot shifts_json."""
    tg.check(GOLDENS, "standard_hours", tg.capture_standard_hours(dataset))
