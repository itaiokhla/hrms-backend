"""Read-only catalogue APIs: currencies, countries, subdivisions and country payroll capability.

3.2 §2, §6.2, decision I-06; EC-MON-03, EC-MON-14, EC-STA-01, EC-SEC-02.
"""

import pytest
from sqlalchemy import text

from app.database.db import system_session
from tests.helpers import create_employee, invite_and_activate, new_client

BASE = "/api/v1/payroll/catalogues"


def _by(rows, key):
    return {row[key]: row for row in rows}


def test_ec_mon_03_currencies_carry_iso_minor_units_and_payroll_eligibility(three_companies):
    client = three_companies["A"]
    rows = _by(client.get(f"{BASE}/currencies").json(), "code")
    assert (rows["INR"]["minor_unit"], rows["JPY"]["minor_unit"], rows["KWD"]["minor_unit"]) == (2, 0, 3)
    assert rows["XAU"]["payroll_allowed"] is False and rows["XAU"]["minor_unit"] is None
    assert rows["CLF"]["is_fund"] is True and rows["CLF"]["payroll_allowed"] is False
    assert rows["INR"]["payroll_allowed"] is True and rows["INR"]["status"] == "ACTIVE"
    payroll = _by(client.get(f"{BASE}/currencies", params={"payroll_only": True}).json(), "code")
    assert "INR" in payroll and not {"XAU", "CLF", "XTS", "XXX"} & set(payroll)


def test_ec_mon_14_historic_currencies_are_listed_only_on_request(three_companies):
    client = three_companies["A"]
    assert "HRK" not in _by(client.get(f"{BASE}/currencies").json(), "code")
    historic = _by(client.get(f"{BASE}/currencies", params={"include_historic": True}).json(), "code")
    assert historic["HRK"]["status"] == "HISTORIC" and historic["HRK"]["payroll_allowed"] is False
    assert historic["HRK"]["replaced_by"] == "EUR"


def test_countries_and_subdivisions(three_companies):
    client = three_companies["A"]
    countries = _by(client.get(f"{BASE}/countries").json(), "alpha2")
    assert countries["IN"]["default_currency"] == "INR" and countries["IN"]["tax_year_start"] == "04-01"
    assert countries["AE"]["default_currency"] == "AED"
    assert all(c["status"] == "ACTIVE" for c in countries.values())
    with_deprecated = client.get(f"{BASE}/countries", params={"include_deprecated": True}).json()
    assert len(with_deprecated) > len(countries)
    deprecated = [c for c in with_deprecated if c["status"] == "DEPRECATED"]
    assert deprecated and client.get(f"{BASE}/countries/{deprecated[0]['alpha2']}/subdivisions").json() == []
    subdivisions = _by(client.get(f"{BASE}/countries/IN/subdivisions").json(), "code")
    assert subdivisions["IN-MH"]["country"] == "IN"
    assert client.get(f"{BASE}/countries/in/subdivisions").status_code == 200
    for unknown in ("ZZ", "XX1", "1"):
        r = client.get(f"{BASE}/countries/{unknown}/subdivisions")
        assert (r.status_code, r.json()["code"]) == (404, "NOT_FOUND")


def test_ec_sta_01_capability_defaults_never_claim_statutory_support(three_companies):
    client = three_companies["A"]
    rows = _by(client.get(f"{BASE}/capabilities").json(), "alpha2")
    assert {code: rows[code]["capability_level"] for code in ("IN", "AE", "US", "FR")} == {
        "IN": "CONFIG_ONLY", "AE": "CONFIG_ONLY", "US": "CONFIG_ONLY", "FR": "DISPLAY_ONLY"}
    assert all(row["source"] == "DEFAULT" and row["statutory_verified"] is False for row in rows.values())


def test_platform_capability_rows_override_the_default(three_companies):
    with system_session() as db:
        db.execute(text(
            "INSERT INTO country_capabilities (alpha2, capability_level, note, updated_by, updated_at) "
            "VALUES ('FR', 'CALC_SUPPORTED', 'vectors pass', 'ops@platform.io', now())"))
        db.commit()
    client = three_companies["B"]
    [fr] = client.get(f"{BASE}/capabilities", params={"country": "FR"}).json()
    assert (fr["capability_level"], fr["source"], fr["note"], fr["statutory_verified"]) == (
        "CALC_SUPPORTED", "PLATFORM", "vectors pass", False)
    assert _by(client.get(f"{BASE}/capabilities").json(), "alpha2")["FR"]["source"] == "PLATFORM"
    assert client.get(f"{BASE}/capabilities", params={"country": "QQ"}).status_code == 404
    assert client.get(f"{BASE}/capabilities", params={"country": "FRA"}).status_code == 422


def test_catalogues_are_the_same_for_every_tenant_and_any_signed_in_user(three_companies, app):
    founder = three_companies["A"]
    clerk = invite_and_activate(app, founder, create_employee(founder, "Clerk"), ["employee"])
    for path in ("/currencies", "/countries", "/countries/AE/subdivisions", "/capabilities"):
        bodies = [c.get(BASE + path) for c in (founder, clerk, three_companies["B"], three_companies["C"])]
        assert all(r.status_code == 200 for r in bodies), path
        assert all(r.json() == bodies[0].json() for r in bodies), path


@pytest.mark.parametrize("path", ["/currencies", "/countries", "/countries/IN/subdivisions", "/capabilities"])
def test_catalogues_need_a_session_and_are_read_only(app, three_companies, path):
    assert new_client(app).get(BASE + path).status_code == 401
    assert three_companies["A"].post(BASE + path, json={}).status_code == 405
