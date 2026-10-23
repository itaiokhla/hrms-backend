"""Independent verification of decision I-20 (pay basis per pay period) in the compensation service.

Gross 52,000.00 a month = 6,24,000.00 a year; CTC 12,48,000.00 a year. One pay period is the annual amount
÷ the nominal periods of the pay group: weekly 52, bi-weekly 26, semi-monthly 24, monthly 12.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.payroll.test_compensation import acme, assign, world  # noqa: F401 (fixtures)
from tests.payroll.test_components import ok
from tests.payroll.test_seed_defects_db import _place_in

D = Decimal


class _FakeDb:
    def __init__(self, frequency: str) -> None:
        self.group = SimpleNamespace(pay_frequency=frequency)

    def get(self, model, key):
        return self.group


@pytest.mark.parametrize("frequency,per_year,gross_per,ctc_per", [
    ("WEEKLY", 52, "12000.00", "24000.00"),         # 6,24,000 ÷ 52; 12,48,000 ÷ 52
    ("BI_WEEKLY", 26, "24000.00", "48000.00"),      # ÷ 26
    ("SEMI_MONTHLY", 24, "26000.00", "52000.00"),   # ÷ 24
    ("MONTHLY", 12, "52000.00", "104000.00"),       # ÷ 12
])
def test_pay_period_terms_by_frequency(frequency, per_year, gross_per, ctc_per):
    from app.payroll.services.compensation import pay_period_terms

    placement = SimpleNamespace(pay_group_id=7)
    gross = pay_period_terms(_FakeDb(frequency), placement, "GROSS_BASED", D("52000"), "INR")
    ctc = pay_period_terms(_FakeDb(frequency), placement, "CTC_BASED", D("1248000"), "INR")
    assert gross["periods_per_year"] == per_year == ctc["periods_per_year"]
    assert gross["per_period"] == {"amount": gross_per, "currency": "INR"}
    assert ctc["per_period"] == {"amount": ctc_per, "currency": "INR"}
    assert (gross["pay_basis_unit"], ctc["pay_basis_unit"]) == ("MONTHLY", "ANNUAL")
    if frequency == "MONTHLY":
        assert gross["message"] is None and ctc["message"] is None
    else:
        assert frequency in gross["message"] and gross_per in gross["message"] and ctc_per in ctc["message"]
    hourly = pay_period_terms(_FakeDb(frequency), placement, "HOURLY", D("48"), "INR")
    assert hourly["per_period"] is None and hourly["message"] is None and hourly["pay_basis_unit"] == "PER_HOUR"


def test_no_pay_group_is_treated_as_monthly():
    from app.payroll.services.compensation import pay_period_terms

    terms = pay_period_terms(_FakeDb("WEEKLY"), None, "CTC_BASED", D("1200000"), "INR")
    assert terms["frequency"] == "MONTHLY" and terms["per_period"]["amount"] == "100000.00"
    assert terms["message"] is None


@pytest.mark.parametrize("frequency,expected", [("WEEKLY", "24000.00"), ("SEMI_MONTHLY", "52000.00")])
def test_assignment_on_weekly_and_semi_monthly_groups(world, frequency, expected):
    """CTC 12,48,000: weekly ÷ 52 = 24,000.00, semi-monthly ÷ 24 = 52,000.00 a pay period."""
    admin, worker = world["admin"], world["worker"]
    _place_in(admin, worker["id"], frequency)
    proposed = ok(assign(admin, worker["id"], world["version_id"], "1248000.00"), 201)
    assert proposed["pay_period"]["frequency"] == frequency
    assert proposed["pay_period"]["per_period"] == {"amount": expected, "currency": "INR"}
    (warning,) = [w for w in proposed["warnings"] if w["code"] == "PAY_PERIOD_AMOUNT"]
    assert frequency in warning["message"] and expected in warning["message"]
