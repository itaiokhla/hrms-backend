"""FX service and routes: manual rates with maker-checker, platform rates, lookups (stale, missing, pending),
inverse/cross derivation, conversion records and run snapshots.

3.2 §4, 3.9 §3.3/§4; EC-MON-05, EC-MON-06, EC-MON-07, EC-MON-08, EC-MON-09, EC-MON-19, EC-SEC-01, EC-SEC-02, EC-SEC-06.
"""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_session
from app.models.catalogue import FxConversion, FxRate
from app.payroll import fx as fxlib
from app.payroll.money import Money
from app.payroll.services import fx
from tests.helpers import create_employee, invite_and_activate
from tests.white_label_helpers import PLATFORM_HOST, HostClient, platform_admin

D1, D2, D3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
URL = "/api/v1/payroll/fx-rates"


@contextmanager
def tenant(company_id):
    db = SessionLocal()
    bind_tenant(db, company_id)
    try:
        yield db
    finally:
        db.close()


def _rate(base, quote, rate, as_of, *, company_id=None, status="ACTIVE", rate_type="MID", source=None):
    with system_session() as db:
        row = FxRate(company_id=company_id, base_currency=base, quote_currency=quote, rate=Decimal(rate),
                     rate_type=rate_type, source=source or ("MANUAL" if company_id else "ECB"), as_of=as_of,
                     fetched_at=fx.clock.utcnow(), status=status)
        db.add(row)
        db.commit()
        return row.id


def _setting(company_id, key, value):
    from app.models.payroll_config import SettingValue

    with system_session() as db:
        db.query(SettingValue).filter(SettingValue.company_id == company_id, SettingValue.key == key).delete()
        db.add(SettingValue(company_id=company_id, key=key, scope_type="TENANT", scope_id=None, value=value,
                            effective_from=date(2020, 1, 1), status="ACTIVE", source="ADMIN"))
        db.commit()


def _post(client, base, quote, rate, as_of, **extra):
    return client.post(URL, json={"base_currency": base, "quote_currency": quote, "rate": rate,
                                  "as_of": as_of.isoformat(), **extra})


@pytest.fixture
def team(three_companies, app):
    founder = three_companies["A"]
    checker = invite_and_activate(app, founder, create_employee(founder, "Checker"), ["company_admin"])
    clerk = invite_and_activate(app, founder, create_employee(founder, "Clerk"), ["employee"])
    return {"maker": founder, "checker": checker, "clerk": clerk, "B": three_companies["B"], "app": app}


def test_ec_mon_08_manual_rates_wait_for_another_users_approval(team):
    maker, checker = team["maker"], team["checker"]
    r = _post(maker, "USD", "AED", "3.6725", D1)
    assert r.status_code == 201, r.text
    created = r.json()
    assert (created["status"], created["approval_reasons"], created["scope"]) == ("PENDING_APPROVAL", ["MANUAL_RATE"], "TENANT")
    assert created["rate"] == "3.672500000000" and created["quotation"].startswith("1 USD = 3.6725")
    lookup = maker.get(f"{URL}/lookup", params={"base": "USD", "quote": "AED", "date": D1.isoformat()}).json()
    assert lookup["ok"] is False and lookup["issue"]["code"] == "RATE_NOT_APPROVED"

    own = maker.post(f"{URL}/{created['id']}/approve")
    assert (own.status_code, own.json()["code"]) == (403, "MAKER_CANNOT_APPROVE")
    approved = checker.post(f"{URL}/{created['id']}/approve")
    assert approved.status_code == 200 and approved.json()["status"] == "ACTIVE"
    assert approved.json()["approved_by_user_id"] == checker.user_id
    again = checker.post(f"{URL}/{created['id']}/approve")
    assert (again.status_code, again.json()["code"]) == (409, "FX_RATE_NOT_PENDING")
    lookup = maker.get(f"{URL}/lookup", params={"base": "USD", "quote": "AED", "date": D1.isoformat()}).json()
    assert lookup["ok"] is True and lookup["rate"]["fx_rate_id"] == created["id"]
    with system_session() as db:
        actions = [a for (a,) in db.execute(text(
            "SELECT action FROM audit_logs WHERE module = 'Payroll Settings' AND entity_type = 'fx_rate' ORDER BY id"))]
    assert actions == ["FX_RATE_CREATED", "FX_RATE_APPROVED"]


def test_rejected_rates_are_never_used(team):
    maker = team["maker"]
    rate_id = _post(maker, "USD", "AED", "3.6725", D1).json()["id"]
    assert maker.post(f"{URL}/{rate_id}/reject").json()["status"] == "REJECTED"
    lookup = maker.get(f"{URL}/lookup", params={"base": "USD", "quote": "AED", "date": D1.isoformat()}).json()
    assert lookup["issue"]["code"] == "RATE_UNAVAILABLE"


@pytest.mark.parametrize("rate, code", [("0", "RATE_NOT_POSITIVE"), ("-3.6", "RATE_NOT_POSITIVE"),
                                        ("1.1234567890123", "RATE_PRECISION_EXCEEDED")])
def test_ec_mon_07_invalid_rates_are_rejected(team, rate, code):
    r = _post(team["maker"], "USD", "AED", rate, D1)
    assert (r.status_code, r.json()["code"]) == (422, code)


def test_ec_mon_07_same_pair_non_monetary_numbers_and_server_fields_are_rejected(team):
    maker = team["maker"]
    assert _post(maker, "USD", "USD", "1", D1).json()["code"] == "SAME_CURRENCY"
    assert _post(maker, "XAU", "USD", "2400", D1).json()["code"] == "NON_MONETARY_CURRENCY"
    assert _post(maker, "usd", "AED", "3.6", D1).status_code == 422
    number = maker.post(URL, json={"base_currency": "USD", "quote_currency": "AED", "rate": 3.6725, "as_of": "2026-10-01"})
    assert number.status_code == 422  # a JSON number is never a rate (EC-MON-15)
    for field, value in (("status", "ACTIVE"), ("company_id", 2), ("approved_by_user_id", 1), ("source", "ECB")):
        r = _post(maker, "USD", "AED", "3.6725", D1, **{field: value})
        assert r.status_code == 422, field  # EC-SEC-06
    with system_session() as db:
        assert db.query(FxRate).count() == 0


def test_anomalous_moves_need_approval_and_inverted_quotes_are_flagged(team):
    maker = team["maker"]
    _setting(maker.company_id, "payroll.fx.manual_rate_requires_approval", False)
    first = _post(maker, "USD", "AED", "3.6725", D1).json()
    assert (first["status"], first["approval_reasons"]) == ("ACTIVE", [])
    small = _post(maker, "USD", "AED", "3.70", D2).json()
    assert small["status"] == "ACTIVE"
    jump = _post(maker, "USD", "AED", "3.95", D3).json()
    assert (jump["status"], jump["approval_reasons"]) == ("PENDING_APPROVAL", ["ANOMALY"])
    inverted = _post(maker, "USD", "AED", "0.27", D3).json()
    assert inverted["status"] == "PENDING_APPROVAL" and inverted["warnings"] == ["POSSIBLY_INVERTED"]
    # The pending day-3 rates block lookups on day 3; day 2's rate is never used in their place (EC-MON-05).
    with tenant(maker.company_id) as db:
        assert fx.lookup(db, maker.company_id, "USD", "AED", D3).issue.code == "RATE_NOT_APPROVED"


def test_ec_mon_06_stale_rates_block_lookups_and_conversions(team):
    a = team["maker"].company_id
    _rate("USD", "AED", "3.6725", D1, company_id=a)
    with tenant(a) as db:
        assert fx.lookup(db, a, "USD", "AED", D2).ok  # 24 hours old: still within payroll.fx.max_rate_age_hours
        stale = fx.lookup(db, a, "USD", "AED", D3)
        assert stale.issue.code == "RATE_STALE"
        with pytest.raises(fxlib.FxError) as error:
            fx.convert(db, Money(Decimal("100"), "USD"), "AED", on=D3, purpose="CONTRACT_TO_PAY")
        assert error.value.code == "RATE_STALE"
        assert db.query(FxConversion).count() == 0
    _setting(a, "payroll.fx.max_rate_age_hours", 72)
    with tenant(a) as db:
        assert fx.lookup(db, a, "USD", "AED", D3).ok


def test_ec_mon_05_missing_rates_block_and_never_convert_one_to_one(team):
    a = team["maker"].company_id
    with tenant(a) as db:
        with pytest.raises(fxlib.RateUnavailable):
            fx.convert(db, Money(Decimal("100"), "USD"), "INR", on=D1, purpose="REPORTING")
        with pytest.raises(fxlib.FxError) as same:
            fx.convert(db, Money(Decimal("100"), "USD"), "USD", on=D1, purpose="REPORTING")
        assert same.value.code == "SAME_CURRENCY"
        assert db.query(FxConversion).count() == 0
    r = team["maker"].get(f"{URL}/lookup", params={"base": "USD", "quote": "INR", "date": D1.isoformat()})
    assert r.status_code == 200 and r.json()["issue"]["code"] == "RATE_UNAVAILABLE"


def test_ec_mon_19_conversions_are_stored_records_with_purpose_rate_basis_and_date(team):
    a = team["maker"].company_id
    rate_id = _rate("USD", "AED", "3.6725", D1, company_id=a)
    with tenant(a) as db:
        row = fx.convert(db, Money(Decimal("1234.56"), "USD"), "AED", on=D1, purpose="CONTRACT_TO_PAY",
                         ref_type="payroll_line", ref_id=42)
        db.commit()
        record = fx.serialize_conversion(row)
    assert record["result"] == {"amount": "4533.92", "currency": "AED"}  # 4533.9216 HALF_UP
    assert record["source"] == {"amount": "1234.56", "currency": "USD"}
    assert (record["fx_rate_id"], record["purpose"], record["rate_date"], record["rounding_mode"]) == (
        rate_id, "CONTRACT_TO_PAY", D1, "HALF_UP")
    assert record["quotation"] == "1 USD = 3.672500000000 AED" and (record["ref_type"], record["ref_id"]) == ("payroll_line", 42)


def test_inverse_rates_are_derived_recorded_and_reused(team):
    a = team["maker"].company_id
    leg = _rate("USD", "AED", "3.6725", D1, company_id=a)
    with tenant(a) as db:
        first = fx.convert(db, Money(Decimal("367.25"), "AED"), "USD", on=D1, purpose="REPORTING")
        second = fx.convert(db, Money(Decimal("10"), "AED"), "USD", on=D1, purpose="REPORTING")
        db.commit()
        derived = db.get(FxRate, first.fx_rate_id)
        assert first.fx_rate_id == second.fx_rate_id
        assert (derived.source, derived.status, derived.company_id) == ("DERIVED", "ACTIVE", a)
        assert derived.derived_from["method"] == "INVERSE"
        assert [leg_["fx_rate_id"] for leg_ in derived.derived_from["legs"]] == [leg]
        assert Money(Decimal(first.result_amount), "USD").to_json()["amount"] == "100.00"
        # Derived rows never shadow entered rates in later lookups.
        assert fx.lookup(db, a, "USD", "AED", D1).rate.rate_id == leg


def test_cross_rates_go_through_a_pivot_and_record_both_legs(team):
    a = team["maker"].company_id
    inr_usd = _rate("USD", "INR", "84", D1)  # platform legs, quoted either way round
    usd_aed = _rate("USD", "AED", "3.6725", D1)
    with tenant(a) as db:
        found = fx.lookup(db, a, "INR", "AED", D1)
        assert found.ok and found.rate.derivation.method == "CROSS" and found.rate.derivation.pivot == "USD"
        conversion = fx.convert(db, Money(Decimal("84000"), "INR"), "AED", on=D1, purpose="REPORTING")
        db.commit()
        derived = db.get(FxRate, conversion.fx_rate_id)
        assert {leg["fx_rate_id"] for leg in derived.derived_from["legs"]} == {inr_usd, usd_aed}
        assert Decimal(conversion.result_amount) == Decimal("3672.50")


def test_a_pending_leg_blocks_derivation(team):
    a = team["maker"].company_id
    _rate("USD", "AED", "3.6725", D1, company_id=a, status="PENDING_APPROVAL")
    with tenant(a) as db:
        assert fx.lookup(db, a, "AED", "USD", D1).issue.code == "RATE_NOT_APPROVED"


def test_tenant_rates_take_precedence_over_platform_rates(team):
    a, b = team["maker"].company_id, team["B"].company_id
    platform = _rate("USD", "AED", "3.60", D1)
    own = _rate("USD", "AED", "3.6725", D1, company_id=a)
    with tenant(a) as db:
        assert fx.lookup(db, a, "USD", "AED", D1).rate.rate_id == own
    with tenant(b) as db:
        assert fx.lookup(db, b, "USD", "AED", D1).rate.rate_id == platform


def test_ec_mon_09_snapshots_freeze_the_rates_a_run_used(team):
    a, b = team["maker"].company_id, team["B"].company_id
    usd_aed = _rate("USD", "AED", "3.6725", D1, company_id=a)
    usd_inr = _rate("USD", "INR", "84", D1)
    pending = _rate("USD", "EUR", "0.92", D1, company_id=a, status="PENDING_APPROVAL")
    foreign = _rate("USD", "GBP", "0.79", D1, company_id=b)
    duplicate = _rate("USD", "AED", "3.67", D1)
    with tenant(a) as db:
        snapshot_id = fx.capture_snapshot(db, [usd_aed, usd_inr], run_id=11)
        db.commit()
        for ids, code in (([pending], "RATE_NOT_APPROVED"), ([foreign], "RATE_NOT_FOUND"),
                          ([usd_aed, duplicate], "RATE_AMBIGUOUS"), ([999999], "RATE_NOT_FOUND")):
            with pytest.raises(fxlib.FxError) as error:
                fx.capture_snapshot(db, ids)
            assert error.value.code == code
            db.rollback()
    with system_session() as db:  # a later edit of the source rate never reaches the snapshot
        db.execute(text("UPDATE fx_rates SET rate = 9 WHERE id = :i"), {"i": usd_aed})
        db.commit()
    with tenant(a) as db:
        frozen = {(r.base, r.quote): r for r in fx.snapshot_rates(db, snapshot_id)}
        assert frozen[("USD", "AED")].rate == Decimal("3.6725") and frozen[("USD", "AED")].rate_id == usd_aed
        assert set(frozen) == {("USD", "AED"), ("USD", "INR")}
    with tenant(b) as db:
        with pytest.raises(Exception) as other:
            fx.snapshot_rates(db, snapshot_id)
        assert getattr(other.value, "status_code", None) == 404


def test_ec_sec_01_tenants_see_platform_rates_and_only_their_own_manual_rates(team):
    maker, other = team["maker"], team["B"]
    own = _post(maker, "USD", "AED", "3.6725", D1).json()["id"]
    platform = _rate("USD", "INR", "84", D1)
    assert {r["id"] for r in maker.get(URL).json()} == {own, platform}
    assert {r["id"] for r in other.get(URL).json()} == {platform}
    assert other.get(URL, params={"base": "USD", "quote": "AED"}).json() == []
    assert other.get(f"{URL}/{own}").status_code == 404
    for action in ("approve", "reject"):
        r = other.post(f"{URL}/{own}/{action}")
        assert (r.status_code, r.json()["code"]) == (404, "NOT_FOUND")
    seen = other.get(f"{URL}/{platform}").json()
    assert seen["scope"] == "PLATFORM" and seen["created_by_user_id"] is None
    assert maker.get(f"{URL}/{own}").json()["status"] == "PENDING_APPROVAL"


def test_fx_routes_need_payroll_configuration_permissions(team):
    clerk, maker = team["clerk"], team["maker"]
    own = _post(maker, "USD", "AED", "3.6725", D1).json()["id"]
    assert clerk.get(URL).status_code == 403
    assert clerk.get(f"{URL}/lookup", params={"base": "USD", "quote": "AED", "date": "2026-10-01"}).status_code == 403
    assert _post(clerk, "USD", "AED", "3.6725", D1).status_code == 403
    assert clerk.post(f"{URL}/{own}/approve").status_code == 403
    from tests.helpers import new_client

    assert new_client(team["app"]).get(URL).status_code == 401


def test_platform_operators_publish_platform_rates_with_their_own_maker_checker(app, three_companies):
    from app.cli import create_platform_admin

    ops = platform_admin(app)
    create_platform_admin("ops2@platform.io", "Ops Two", "Platf0rmPass!")
    ops2 = HostClient(app, PLATFORM_HOST)
    assert ops2.post("/api/v1/platform/auth/login",
                     json={"email": "ops2@platform.io", "password": "Platf0rmPass!"}).status_code == 200
    body = {"base_currency": "USD", "quote_currency": "AED", "rate": "3.6725", "as_of": "2026-10-01", "source": "ECB"}
    first = ops.post("/api/v1/platform/fx-rates", json=body)
    assert first.status_code == 201 and first.json()["status"] == "ACTIVE" and first.json()["scope"] == "PLATFORM"
    jump = ops.post("/api/v1/platform/fx-rates", json={**body, "rate": "4.10", "as_of": "2026-10-02"}).json()
    assert (jump["status"], jump["approval_reasons"]) == ("PENDING_APPROVAL", ["ANOMALY"])
    assert ops.post(f"/api/v1/platform/fx-rates/{jump['id']}/approve").json()["code"] == "MAKER_CANNOT_APPROVE"
    assert ops2.post(f"/api/v1/platform/fx-rates/{jump['id']}/approve").json()["status"] == "ACTIVE"
    assert {r["id"] for r in ops.get("/api/v1/platform/fx-rates").json()} == {first.json()["id"], jump["id"]}
    assert ops.post("/api/v1/platform/fx-rates", json={**body, "company_id": 1}).status_code == 422
    # Tenants can read platform rates but never manage them.
    tenant_admin = three_companies["A"]
    assert tenant_admin.post("/api/v1/platform/fx-rates", json=body).status_code in (401, 404)
    assert tenant_admin.get("/api/v1/platform/fx-rates").status_code in (401, 404)
    assert jump["id"] in {r["id"] for r in tenant_admin.get(URL).json()}
    with system_session() as db:
        actions = [a for (a,) in db.execute(text(
            "SELECT action FROM audit_logs WHERE module = 'Platform' AND entity_type = 'fx_rate' ORDER BY id"))]
    assert actions == ["FX_RATE_CREATED", "FX_RATE_CREATED", "FX_RATE_APPROVED"]


def test_fx_settings_resolve_through_the_settings_engine_and_fail_closed(team):
    maker = team["maker"]
    a = maker.company_id
    _setting(a, "payroll.fx.rate_type", "SELL")
    with tenant(a) as db:
        assert fx.settings_for(db, a, D1).rate_type == "SELL"
    _setting(a, "payroll.fx.max_rate_age_hours", "soon")  # a corrupted stored value is never replaced by a default
    r = maker.get(f"{URL}/lookup", params={"base": "USD", "quote": "AED", "date": D1.isoformat()})
    assert (r.status_code, r.json()["code"]) == (409, "CONFIG_INVALID")
    assert _post(maker, "USD", "AED", "3.6725", D1).json()["code"] == "CONFIG_INVALID"


def test_platform_rates_use_platform_level_fx_settings(app):
    from app.models.payroll_config import PlatformSettingValue

    with system_session() as db:
        db.add(PlatformSettingValue(level="PLATFORM", key="payroll.fx.anomaly_threshold_percent", value="20",
                                    effective_from=date(2020, 1, 1), created_by="ops@platform.io"))
        db.commit()
    ops = platform_admin(app)
    body = {"base_currency": "USD", "quote_currency": "AED", "rate": "3.6725", "as_of": "2026-10-01"}
    assert ops.post("/api/v1/platform/fx-rates", json=body).json()["status"] == "ACTIVE"
    moved = ops.post("/api/v1/platform/fx-rates", json={**body, "rate": "4.10", "as_of": "2026-10-02"}).json()
    assert (moved["status"], moved["approval_reasons"]) == ("ACTIVE", [])  # 11.6 % is below the platform's 20 %


def test_settings_fall_back_to_registry_defaults(team):
    a = team["maker"].company_id
    with tenant(a) as db:
        cfg = fx.settings_for(db, a, D1)
    assert (cfg.rate_type, cfg.max_rate_age_hours, cfg.anomaly_threshold_percent, cfg.manual_rate_requires_approval) == (
        "MID", 24, Decimal("5.0"), True)
