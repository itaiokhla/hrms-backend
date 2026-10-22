"""A freshly provisioned tenant resolves every payroll setting without a configuration error.

Regression for a defect found while integrating the run lifecycle: the derived defaults of the banking
calendar, the run and compensation approval chains, the funding account and the payslip languages had
no context facts, so every run pre-check of every new tenant raised a blocking CONFIG_INVALID.
"""

from datetime import date

from tests.helpers import signup_company


def _resolve_all(company_id: int):
    from app.database.db import SessionLocal, bind_tenant
    from app.models.org import LegalEntity
    from app.payroll import settings_registry as reg
    from app.payroll.services import settings as engine

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id).first()
        keys = [k for k, d in reg.REGISTRY.items() if d.module == "Payroll"]
        return engine.resolve_many(db, company_id, keys, legal_entity_id=entity.id, on=date.today(), strict=False)
    finally:
        db.close()


def test_new_tenant_resolves_every_payroll_key(app):
    company = signup_company(app, "Fresh Resolution Co")
    resolved = _resolve_all(company.company_id)
    broken = {key: [i.message for i in res.issues] for key, res in resolved.items() if not res.ok}
    assert broken == {}


def test_optional_references_are_unset_or_point_at_real_records(app):
    company = signup_company(app, "Optional References Co")
    resolved = _resolve_all(company.company_id)
    # No approval chain, funding account or national calendar row is configured for a new tenant yet:
    # the settings are unset (None) so their consumers apply the documented fallbacks; never invalid.
    for key in ("payroll.approval.run_chain_id", "payroll.approval.compensation_chain_id",
                "payroll.payments.funding_account_id"):
        assert resolved[key].ok and resolved[key].value is None, key
    calendar = resolved["payroll.calendar.banking_calendar_id"]
    assert calendar.ok and (calendar.value is None or isinstance(calendar.value, int))
    assert resolved["payslip.languages"].ok and resolved["payslip.languages"].value  # tenant/entity locale or "en"


def test_required_derived_defaults_still_fail_closed():
    import pytest

    from app.payroll import settings_registry as reg

    with pytest.raises(reg.SettingValidationError) as error:
        reg.derive_default("payroll.currency", {})
    assert error.value.code == "DEFAULT_UNRESOLVED"
    assert reg.derive_default("payroll.approval.run_chain_id", {}) is None
    assert reg.derive_default("payroll.calendar.banking_calendar_id", {"country_banking_calendar_id": 7}) == 7
