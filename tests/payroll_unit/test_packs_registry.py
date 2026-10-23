"""Pack registry: which versions exist, how they may be used, capability, setting constraints (3.2 §6, I-07)."""

from datetime import date

import pytest

from app.payroll import packs
from app.payroll.packs import (
    FIXTURE,
    PREVIEW,
    REFUSED,
    VERIFIED,
    PackUnavailable,
    capability,
    country_capability,
    get_pack,
    pack_snapshot,
    packs_for_country,
    setting_constraints,
    usage,
)
from app.payroll.packs.base import Capability, CountryPack
from app.payroll.settings_registry import (
    REGISTRY,
    Level,
    SettingValidationError,
    cross_validate,
    pack_setting_keys,
    register_pack_settings,
    unregister_pack_settings,
    validate_write,
)

OCT = date(2026, 10, 15)


def test_every_pack_implements_the_protocol_and_declares_its_components():
    for pack in packs.all_packs(environment="test"):
        assert isinstance(pack, CountryPack)
        codes = {c.code for c in pack.components()}
        for scheme in pack.schemes():
            assert set(scheme.components) <= codes | {"AE_EOSG"}, scheme.code
            assert scheme.switch_key.startswith(f"payroll.statutory.{pack.code}.")
        assert pack.fingerprint() == pack.fingerprint()


def test_shipped_statuses_follow_i07():
    zz, india = packs.lookup("ZZ", "ZZ-2026.1"), packs.find_pack("IN")
    assert zz.meta.status == "VERIFIED_FOR_PERIOD" and zz.meta.test_only and not zz.meta.is_fixture
    assert india.meta.is_fixture and india.meta.status == "DRAFT"
    for code in ("AE", "US"):
        draft = packs.find_pack(code)
        assert draft.meta.status == "DRAFT" and draft.meta.capability == Capability.CONFIG_ONLY
        assert not draft.meta.is_fixture


def test_zz_is_invisible_and_refused_outside_tests():
    assert packs.find_pack("ZZ") is None and packs_for_country("ZZ") == ()
    assert [p.code for p in packs_for_country("ZZ", environment="test")] == ["ZZ"]
    for environment in ("production", "staging", "development", None, "prod"):
        assert usage(packs.lookup("ZZ", "ZZ-2026.1").meta, environment=environment).reason == "PACK_TEST_ONLY"
    assert get_pack("ZZ", environment="test", on=OCT).version == "ZZ-2026.1"
    with pytest.raises(PackUnavailable) as error:
        get_pack("ZZ")
    assert error.value.code == "PACK_NOT_FOUND"


def test_ec_sta_02_draft_pack_refused_outside_previews():
    us = packs.find_pack("US")
    with pytest.raises(PackUnavailable) as error:
        get_pack("US", on=OCT)
    assert error.value.code == "PACK_NOT_VERIFIED"
    assert get_pack("US", on=OCT, preview=True) is us
    verdict = usage(us.meta, preview=True)
    assert verdict.mode == PREVIEW and not verdict.final and "Compliance not verified" in verdict.message
    assert usage(us.meta, status="UNDER_REVIEW").mode == REFUSED


def test_fixture_runs_only_in_development_and_test():
    meta = packs.find_pack("IN").meta
    assert usage(meta, environment="test").mode == FIXTURE
    assert usage(meta, environment="development").mode == FIXTURE
    for environment in ("production", "staging", "unknown", None):
        verdict = usage(meta, environment=environment, preview=True)
        assert verdict.mode == REFUSED and verdict.reason == "PACK_FIXTURE_IN_PRODUCTION"
    with pytest.raises(PackUnavailable) as error:
        get_pack("IN", on=OCT)
    assert error.value.code == "PACK_FIXTURE_IN_PRODUCTION"
    # a forged "verified" status never turns a fixture into a verified pack
    assert usage(meta, environment="production", status="VERIFIED_FOR_PERIOD").mode == REFUSED


def test_live_status_overrides_the_shipped_one():
    zz = packs.lookup("ZZ", "ZZ-2026.1").meta
    assert usage(zz, environment="test").mode == VERIFIED
    assert usage(zz, environment="test", status="EXPIRED").mode == VERIFIED      # replays of covered periods
    disabled = usage(zz, environment="test", status="DISABLED", preview=True)
    assert disabled.mode == REFUSED and disabled.reason == "PACK_DISABLED"
    assert usage(zz, environment="test", status="DRAFT").mode == REFUSED
    assert usage(zz, environment="test", status="NONSENSE").reason == "PACK_STATUS_UNKNOWN"


def test_ec_sta_02_period_outside_the_version_is_refused():
    meta = packs.find_pack("IN").meta
    verdict = usage(meta, environment="test", period=(date(2027, 4, 1), date(2027, 4, 30)))
    assert verdict.reason == "PACK_NOT_EFFECTIVE"
    assert packs.find_pack("IN", on=date(2025, 1, 1)) is None


def test_capability_never_upgrades_itself():
    india, us = packs.find_pack("IN").meta, packs.find_pack("US").meta
    zz = packs.lookup("ZZ", "ZZ-2026.1").meta
    assert capability(india, environment="test") == Capability.CALC_SUPPORTED
    assert capability(india) == Capability.CONFIG_ONLY
    assert capability(us) == Capability.CONFIG_ONLY
    assert capability(us, status="UNDER_REVIEW") == Capability.CONFIG_ONLY      # declared CONFIG_ONLY stays there
    assert capability(zz, environment="test") == Capability.VERIFIED
    assert capability(zz, environment="test", status="DISABLED") == Capability.CONFIG_ONLY


def test_country_capability():
    assert country_capability("IN", OCT) == Capability.CONFIG_ONLY
    assert country_capability("IN", OCT, environment="test") == Capability.CALC_SUPPORTED
    assert country_capability("AE", OCT) == Capability.CONFIG_ONLY
    assert country_capability("GB", OCT) == Capability.CONFIG_ONLY        # no pack: non-statutory payroll only
    assert country_capability("ZZ", OCT) == Capability.UNSUPPORTED
    assert country_capability("ZZ", OCT, environment="test") == Capability.VERIFIED


@pytest.fixture
def pack_settings():
    """The keys registered by ``packs.register_settings`` on a registry without pack settings.

    The registry is process-global: the API start-up hook (app.main) and DB tests that run earlier in the same
    process (e.g. the settings inheritance test registers ZZ) may already hold pack keys, so they are set aside
    first and restored afterwards, leaving the registry exactly as it was found."""
    before = [REGISTRY[key] for key in pack_setting_keys()]
    for code in {key.split(".")[2] for key in pack_setting_keys()}:
        unregister_pack_settings(code)
    assert pack_setting_keys() == ()
    try:
        yield packs.register_settings(environment="test")
    finally:
        for code in {key.split(".")[2] for key in pack_setting_keys()}:
            unregister_pack_settings(code)
        register_pack_settings(before)
        assert pack_setting_keys() == tuple(sorted(d.key for d in before))


def test_register_settings_is_idempotent(pack_settings):
    assert "payroll.statutory.IN.epf.enabled" in REGISTRY
    assert REGISTRY["payroll.statutory.IN.epf.enabled"].default is False        # never ON by default (I-13)
    assert REGISTRY["payroll.statutory.IN.pt.enabled"].allowed_levels == (Level.LEGAL_ENTITY, Level.BRANCH)
    assert packs.register_settings(environment="test") == ()
    assert "payroll.statutory.ZZ.pension.enabled" in pack_settings


def test_ec_sta_01_unverified_pack_locks_scheme_switches_off(pack_settings):
    constraints = setting_constraints("AE", OCT)
    keys = {c.target for c in constraints}
    assert keys == {"payroll.statutory.AE.gpssa.enabled", "payroll.statutory.AE.eosg.enabled"}
    assert all(c.required_value is False and "Compliance not verified" in c.source for c in constraints)
    with pytest.raises(SettingValidationError):
        validate_write("payroll.statutory.AE.gpssa.enabled", True, level=Level.LEGAL_ENTITY, legal_limits=constraints)
    assert "LEGAL_LIMIT" in {i.rule for i in cross_validate({"payroll.statutory.AE.gpssa.enabled": True},
                                                            pack_constraints=constraints)}
    # the fixture in production is locked off too; in test it applies its own constraints
    assert all(c.required_value is False for c in setting_constraints("IN", OCT))
    test_constraints = setting_constraints("IN", OCT, environment="test")
    assert [(c.target, c.required_value) for c in test_constraints] == [("payroll.statutory.IN.tds.enabled", True)]


def test_ec_cfg_19_verified_pack_constraints_apply(pack_settings):
    constraints = setting_constraints("ZZ", OCT, environment="test")
    targets = {c.target: c for c in constraints}
    assert targets["payroll.statutory.ZZ.pension.enabled"].required_value is True
    assert targets["payroll.deductions.min_net_pay_percent"].minimum == 50
    assert "V-16" in {i.rule for i in cross_validate({"payroll.deductions.min_net_pay_percent": "40"},
                                                     pack_constraints=constraints)}
    assert setting_constraints("ZZ", OCT) == []                                  # not visible in production
    disabled = setting_constraints("ZZ", OCT, environment="test", statuses={"ZZ-2026.1": "DISABLED"})
    assert all(c.required_value is False for c in disabled)


def test_ec_var_10_pack_forbidding_penalties_is_in_the_snapshot():
    zz = pack_snapshot(packs.lookup("ZZ", "ZZ-2026.1"))
    assert zz["penalty_deductions_allowed"] is False
    india = pack_snapshot(packs.find_pack("IN"), status="DRAFT")
    assert india == {"code": "IN", "version": "IN-2026.10-FIXTURE", "country": "IN", "status": "DRAFT",
                     "is_fixture": True, "fingerprint": packs.find_pack("IN").fingerprint(),
                     "penalty_deductions_allowed": True}


def test_manifest_guard_for_review():
    assert packs.manifest_problems(packs.lookup("ZZ", "ZZ-2026.1")) == []
    assert packs.manifest_problems(packs.find_pack("US")) == []
    # honest gap: the research recorded no URL for the PT cap, so the fixture could not be submitted for review
    assert packs.manifest_problems(packs.find_pack("IN")) == ["IN.pt.cap: missing url"]
    review = packs.manifest_problems(packs.find_pack("US"), for_verification=True)
    assert any("missing reviewer" in p for p in review)
    assert "IN.epf.ceiling" in packs.rules_used(packs.find_pack("IN"))
