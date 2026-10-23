"""Seed defect 3 (decision I-20): the pay basis per pay period is explicit in validation and previews.

A CTC_BASED basis is annual and a GROSS_BASED one monthly (3.3 §6.1); one pay period's CTC/gross is
annual ÷ periods per year (3.5 §2.1). On a bi-weekly pay group the assignment shows that per-period amount
and the frequency as a PAY_PERIOD_AMOUNT warning, so nobody believes the monthly figure is paid every two
weeks; FIXED lines are amounts per pay period.
"""

from tests.payroll.test_compensation import acme, assign, employee_url, world  # noqa: F401 (fixtures)
from tests.payroll.test_components import STRUCTURES, ok
from tests.payroll.test_settings_engine import sql
from tests.payroll.test_wage_inputs import pay_group, place


def _place_in(admin, employee_id, frequency):
    entity = sql("SELECT id FROM legal_entities WHERE company_id = :c AND code = 'LE01'", c=admin.company_id)[0][0]
    group = pay_group(admin.company_id, entity, f"PG-{frequency}", frequency=frequency)
    place(employee_id, entity, group)
    return group


def test_bi_weekly_assignment_shows_the_per_period_amount(world):
    """CTC 12,48,000 a year on a bi-weekly group: 12,48,000 ÷ 26 = 48,000.00 per pay period."""
    admin, worker = world["admin"], world["worker"]
    _place_in(admin, worker["id"], "BI_WEEKLY")
    proposed = ok(assign(admin, worker["id"], world["version_id"], "1248000.00"), 201)
    assert proposed["pay_period"]["frequency"] == "BI_WEEKLY" and proposed["pay_period"]["periods_per_year"] == 26
    assert proposed["pay_period"]["pay_basis_unit"] == "ANNUAL"
    assert proposed["pay_period"]["per_period"] == {"amount": "48000.00", "currency": "INR"}
    (warning,) = [w for w in proposed["warnings"] if w["code"] == "PAY_PERIOD_AMOUNT"]
    assert "BI_WEEKLY" in warning["message"] and "48000.00" in warning["message"]
    assert "FIXED structure lines are paid as entered" in warning["message"]


def test_bi_weekly_preview_carries_the_pay_period(world):
    admin, worker = world["admin"], world["worker"]
    _place_in(admin, worker["id"], "BI_WEEKLY")
    preview = ok(admin.post(f"{employee_url(worker['id'])}/preview", json={
        "pay_basis_amount": {"amount": "1248000.00", "currency": "INR"},
        "effective_from": proposed_start(admin), "template_version_id": world["version_id"]}))
    assert preview["pay_period"]["per_period"] == {"amount": "48000.00", "currency": "INR"}
    assert "48000.00" in preview["pay_period"]["message"]


def test_monthly_group_has_no_pay_period_warning(world):
    """Monthly: 12,00,000 ÷ 12 = 1,00,000.00, the monthly figure; nothing to warn about."""
    admin, worker = world["admin"], world["worker"]
    _place_in(admin, worker["id"], "MONTHLY")
    proposed = ok(assign(admin, worker["id"], world["version_id"], "1200000.00"), 201)
    assert proposed["pay_period"]["per_period"] == {"amount": "100000.00", "currency": "INR"}
    assert proposed["pay_period"]["message"] is None
    assert not [w for w in proposed["warnings"] if w["code"] == "PAY_PERIOD_AMOUNT"]


def test_structure_preview_explains_the_pay_basis(world):
    admin = world["admin"]
    preview = ok(admin.post(f"{STRUCTURES}/{world['structure']['id']}/preview",
                            json={"pay_basis_amount": {"amount": "1200000.00", "currency": "INR"}}))
    assert "annual CTC" in preview["pay_basis_note"] and "periods per year" in preview["pay_basis_note"]


def proposed_start(admin):
    from tests.payroll.test_compensation import month_start

    return month_start(admin.company_id).isoformat()
